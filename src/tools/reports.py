#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation
# All rights reserved.
#
# MIT License
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and
# to permit persons to whom the Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED *AS IS*, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
# BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND # NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import urllib.parse
import argparse
import logging
import datetime
import json
import collections
import re
import sys
import math

import sqlite3
import requests

logger = logging.getLogger(__name__)


def walk_json_field_safe(obj, *fields):
    """ for example a=[{"a": {"b": 2}}]
    walk_json_field_safe(a, 0, "a", "b") will get 2
    walk_json_field_safe(a, 0, "not_exist") will get None
    """
    try:
        for f in fields:
            obj = obj[f]
        return obj
    except:
        return None


def request_with_error_handling(url):
    try:
        response = requests.get(url, allow_redirects=True, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.exception(e)
        return None


def format_time(timestamp):
    d = datetime.datetime.fromtimestamp(timestamp)
    return d.strftime("%Y/%m/%d-%H:%M:%S")


def get_ip(ip_port):
    """ return 1.2.3.4 on 1.2.3.4:123 """
    m = re.match("([0-9]+[.][0-9]+[.][0-9]+[.][0-9]+):?.*", ip_port)
    if m:
        return m.groups()[0]
    return ip_port


class JobInfo(object):
    def __init__(self, job_count=0, elapsed_time=0, cpu_sec=0, mem_sec=0, gpu_sec=0,
            user="unknown", vc="unknown", start_time=0, finished_time=0, retries=0,
            status="unknown", exit_code="N/A"):
        """ elapsed_time is seconds, cpu_sec is vcore-seconds, mem_sec is
        megabyte-seconds, gpu_sec is card-seconds """
        self.job_count = job_count
        self.elapsed_time = elapsed_time
        self.cpu_sec = cpu_sec
        self.mem_sec = mem_sec
        self.gpu_sec = gpu_sec

        self.user = user
        self.vc = vc
        self.start_time = start_time
        self.finished_time = finished_time
        self.retries = retries
        self.status = status
        self.exit_code = exit_code

    def __iadd__(self, o):
        self.job_count += o.job_count
        self.elapsed_time += o.elapsed_time
        self.cpu_sec += o.cpu_sec
        self.mem_sec += o.mem_sec
        self.gpu_sec += o.gpu_sec
        return self

    def __add__(self, o):
        return JobInfo(
                job_count=self.job_count + o.job_count,
                elapsed_time=self.elapsed_time + o.elapsed_time,
                cpu_sec=self.cpu_sec + o.cpu_sec,
                mem_sec=self.mem_sec + o.mem_sec,
                gpu_sec=self.gpu_sec + o.gpu_sec)

    def __repr__(self):
        # NOTE this is used to generate final report
        return "%d,%d,%d,%d,%d" % (self.job_count, self.elapsed_time,
                self.cpu_sec, self.mem_sec, self.gpu_sec)


class JobReportEntries(object):
    def __init__(self, username, vc, total_job_info, success_job_info,
            failed_job_info, stopped_job_info, running_job_info, waiting_job_info):
        self.username = username
        self.vc = vc
        self.total_job_info = total_job_info
        self.success_job_info = success_job_info
        self.failed_job_info = failed_job_info
        self.stopped_job_info = stopped_job_info
        self.running_job_info = running_job_info
        self.waiting_job_info = waiting_job_info

    def __repr__(self):
        # NOTE this is used to generate final report
        return "%s,%s,%s,%s,%s,%s,%s,%s" % (
                self.username,
                self.vc,
                self.total_job_info,
                self.success_job_info,
                self.failed_job_info,
                self.stopped_job_info,
                self.running_job_info,
                self.waiting_job_info)


class Alert(object):
    default_get_ip = lambda a: get_ip(a["instance"])
    host_ip_mapping = {
            "NodeNotReady": lambda a: get_ip(a["name"]),
            "k8sApiServerNotOk": lambda a: get_ip(a["host_ip"]),
            "NodeDiskPressure": lambda a: get_ip(a["name"]),
            "NodeNotReady": lambda a: get_ip(a["name"]),
            "PaiServicePodNotRunning": lambda a: get_ip(a["host_ip"]),
            "PaiServicePodNotReady": lambda a: get_ip(a["host_ip"]),
            }

    src_mapping = {
            "NvidiaSmiEccError": lambda a: a["minor_number"],
            "NvidiaMemoryLeak": lambda a: a["minor_number"],
            "NvidiaZombieProcess": lambda a: a["NvidiaZombieProcess"],
            "GpuUsedByExternalProcess": lambda a: a["minor_number"],
            "GpuUsedByZombieContainer": lambda a: a["minor_number"],
            "PaiJobsZombie": lambda a: a["minor_number"],
            "k8sApiServerNotOk": lambda a: a["error"],
            "k8sDockerDaemonNotOk": lambda a: a["error"],
            "NodeFilesystemUsage": lambda a: a["device"],
            "NodeDiskPressure": lambda a: get_ip(a["name"]),
            "NodeNotReady": lambda a: get_ip(a["name"]),
            "AzureAgentConsumeTooMuchMem": lambda a: a["cmd"],
            "PaiServicePodNotRunning": lambda a: a["name"],
            "PaiServicePodNotReady": lambda a: a["name"],
            "PaiServiceNotUp": lambda a: a["pai_service_name"],
            "JobExporterHangs": lambda a: a["name"],
            }

    def __init__(self, alert_name, start, durtion, labels):
        """ alert_name are derived from labels, start/durtion is timestamp
        value """
        self.alert_name = alert_name
        self.start = start
        self.durtion = durtion
        self.labels = labels

        #f.write("alert_name,host_ip,source,start,durtion,labels\n")

    @staticmethod
    def get_info(alert_name, labels, mapping):
        return mapping.get(alert_name, Alert.default_get_ip)(labels)

    def labels_repr(self):
        r = []
        for k, v in self.labels.items():
            if k in {"__name__", "alertname", "alertstate", "job", "type"}:
                continue
            r.append("%s:%s" % (k, v))
        return "|".join(r)

    def __repr__(self):
        # NOTE this is used to generate final report
        return "%s,%s,%s,%s,%s,%s" % (
                self.alert_name,
                Alert.get_info(self.alert_name, self.labels, Alert.host_ip_mapping),
                Alert.get_info(self.alert_name, self.labels, Alert.src_mapping),
                format_time(self.start),
                self.durtion,
                self.labels_repr())


class DB(object):
    # If app is running, the finished_time is 0, should not delete it in delete_old_data
    CREATE_APPS_TABLE = """CREATE TABLE IF NOT EXISTS apps (
                            app_id text NOT NULL,
                            finished_time integer NOT NULL,
                            content text NOT NULL
                            )"""
    CREATE_APP_ID_INDEX = "CREATE INDEX IF NOT EXISTS app_id_index ON apps (app_id);"
    CREATE_APP_TIME_INDEX = "CREATE INDEX IF NOT EXISTS app_time_index ON apps (finished_time);"

    # If job is running, the finished_time is 0, should not delete it in delete_old_data
    CREATE_FRAMEWORKS_TABLE = """CREATE TABLE IF NOT EXISTS frameworks (
                            name text NOT NULL,
                            start_time integer NOT NULL,
                            finished_time integer NOT NULL,
                            content text NOT NULL
                            )"""
    CREATE_FRAMEWORK_NAME_INDEX = "CREATE INDEX IF NOT EXISTS framework_name_index ON frameworks (name);"
    CREATE_FRAMEWORK_TIME_INDEX = "CREATE INDEX IF NOT EXISTS framework_time_index ON frameworks (start_time, finished_time);"

    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        cursor = self.conn.cursor()
        cursor.execute(DB.CREATE_APPS_TABLE)
        cursor.execute(DB.CREATE_APP_ID_INDEX)
        cursor.execute(DB.CREATE_APP_TIME_INDEX)
        cursor.execute(DB.CREATE_FRAMEWORKS_TABLE)
        cursor.execute(DB.CREATE_FRAMEWORK_NAME_INDEX)
        cursor.execute(DB.CREATE_FRAMEWORK_TIME_INDEX)
        self.conn.commit()


def get_yarn_apps(yarn_url):
    apps_url = urllib.parse.urljoin(yarn_url, "/ws/v1/cluster/apps")
    result = []

    obj = request_with_error_handling(apps_url)

    if obj.get("apps") is None or obj["apps"].get("app") is None:
        return result

    apps = obj["apps"]["app"]
    for app in apps:
        app_id = walk_json_field_safe(app, "id")
        if app_id is None:
            continue

        finished_time = walk_json_field_safe(app, "finishedTime") or 0
        finished_time = int(finished_time / 1000) # yarn's time is in millisecond
        content = json.dumps(app)
        result.append({"app_id": app_id,
            "finished_time": finished_time, "content": content})

    return result


def get_frameworks(launcher_url):
    launcher_url = urllib.parse.urljoin(launcher_url, "/v1/Frameworks")
    result = []

    obj = request_with_error_handling(launcher_url)

    if obj.get("summarizedFrameworkInfos") is None:
        return result

    frameworks = obj["summarizedFrameworkInfos"]
    for framework in frameworks:
        name = walk_json_field_safe(framework, "frameworkName")
        if name is None:
            continue

        finished_time = walk_json_field_safe(framework, "frameworkCompletedTimestamp") or 0
        finished_time = int(finished_time / 1000) # yarn's time is in millisecond
        start_time = walk_json_field_safe(framework, "firstRequestTimestamp") or 0
        start_time = int(start_time / 1000) # yarn's time is in millisecond
        content = json.dumps(framework)
        result.append({"name": name, "start_time": start_time,
            "finished_time": finished_time, "content": content})

    return result


def refresh_cache(database, yarn_url, launcher_url):
    db = DB(database)

    apps = get_yarn_apps(yarn_url)
    logger.info("get %d of apps from yarn", len(apps))

    with db.conn:
        cursor = db.conn.cursor()

        for app in apps:
            cursor.execute("""SELECT COUNT(*) FROM apps
                            WHERE app_id=?""",
                            (app["app_id"],))
            result = cursor.fetchone()

            if result[0] > 0:
                cursor.execute("""UPDATE apps SET finished_time=?, content=?
                                WHERE app_id=?""",
                                (app["finished_time"], app["content"], app["app_id"]))
            else:
                cursor.execute("""INSERT INTO apps(app_id,finished_time,content)
                                VALUES(?,?,?)""",
                                (app["app_id"], app["finished_time"], app["content"]))

        db.conn.commit()

    frameworks = get_frameworks(launcher_url)
    logger.info("get %d of frameworks from launcher", len(frameworks))

    with db.conn:
        cursor = db.conn.cursor()

        for framework in frameworks:
            cursor.execute("""SELECT COUNT(*) FROM frameworks
                            WHERE name=?""",
                            (framework["name"],))
            result = cursor.fetchone()

            if result[0] > 0:
                cursor.execute("""UPDATE frameworks SET finished_time=?, content=?
                                WHERE name=?""",
                                (framework["finished_time"], framework["content"], framework["name"]))
            else:
                cursor.execute("""INSERT INTO frameworks(name,start_time,finished_time,content)
                                VALUES(?,?,?,?)""",
                                (framework["name"],
                                    framework["start_time"],
                                    framework["finished_time"],
                                    framework["content"]))

        db.conn.commit()


# https://github.com/Microsoft/pai/blob/pai-0.9.y/src/rest-server/src/models/job.js#L45
def convert_job_state(framework_state, exit_code):
    if framework_state in {
            "FRAMEWORK_WAITING",
            "APPLICATION_CREATED",
            "APPLICATION_LAUNCHED",
            "APPLICATION_WAITING"}:
        return "WAITING"
    elif framework_state in {
            "APPLICATION_RUNNING",
            "APPLICATION_RETRIEVING_DIAGNOSTICS",
            "APPLICATION_COMPLETED"}:
        return "RUNNING"
    elif framework_state == "FRAMEWORK_COMPLETED":
        if exit_code is not None:
            if exit_code == 0:
                return "SUCCEEDED"
            elif exit_code == 214:
                return "STOPPED"
            else:
                return "FAILED"
        else:
            return "FAILED"

    return "UNKNOWN"


def get_job_report(database, since, until):
    """ return two values, one is aggregated job info, the other is raw job status """
    db = DB(database)

    with db.conn:
        cursor = db.conn.cursor()
        cursor.execute("""SELECT content FROM apps
                        WHERE (finished_time>? AND finished_time<?)
                            OR finished_time=0""",
                        (since, until))
        apps = cursor.fetchall()

        logger.info("get %d apps entries", len(apps))

        cursor.execute("""SELECT content FROM frameworks
                        WHERE ((start_time>? AND start_time<?)
                            OR start_time=0) AND
                          ((finished_time>? AND finished_time<?)
                            OR finished_time=0)""",
                        (since, until, since, until))
        frameworks = cursor.fetchall()

        logger.info("get %d frameworks entries", len(frameworks))

    # key is framework_name, value is JobInfo
    processed_apps = collections.defaultdict(lambda : JobInfo())

    pattern = re.compile(u"\[([^[\]]+)\]+_.*")

    for content, in apps:
        app = json.loads(content)

        name = walk_json_field_safe(app, "name")
        if name is None:
            continue

        match = pattern.match(name)
        if match is None:
            continue

        job_name = match.groups()[0]

        elapsed_time = walk_json_field_safe(app, "elapsedTime") or 0
        elapsed_time = int(elapsed_time / 1000)
        cpu_sec = walk_json_field_safe(app, "vcoreSeconds") or 0
        mem_sec = int((walk_json_field_safe(app, "memorySeconds") or 0) / 1024)
        gpu_sec = walk_json_field_safe(app, "gpuSeconds") or 0

        info = JobInfo(job_count=0, elapsed_time=elapsed_time,
                cpu_sec=cpu_sec, mem_sec=mem_sec, gpu_sec=gpu_sec)

        processed_apps[job_name] += info

    statistic = collections.defaultdict(lambda : # key is username
                collections.defaultdict(lambda : # key is vc
                collections.defaultdict(lambda : JobInfo()))) # key is job_status

    for content, in frameworks:
        framework = json.loads(content)

        name = walk_json_field_safe(framework, "frameworkName")
        username = walk_json_field_safe(framework, "userName")
        vc = walk_json_field_safe(framework, "queue")
        start_time = walk_json_field_safe(framework, "firstRequestTimestamp") or 0
        start_time = int(start_time / 1000)
        finished_time = walk_json_field_safe(framework, "frameworkCompletedTimestamp") or 0
        finished_time = int(finished_time / 1000)
        retries = walk_json_field_safe(framework, "frameworkRetryPolicyState", "retriedCount")

        state = walk_json_field_safe(framework, "frameworkState")
        exit_code = walk_json_field_safe(framework, "applicationExitCode")
        job_status = convert_job_state(state, exit_code)

        if name in processed_apps:
            job = processed_apps[name]

            job.job_count = 1
            job.user = username
            job.vc = vc
            job.start_time = start_time
            job.finished_time = finished_time
            job.retries = retries
            job.status = job_status
            if exit_code is not None:
                job.exit_code = exit_code
            else:
                job.exit_code = "N/A"

            statistic[username][vc][job_status] += job

    result = []
    for username, val in statistic.items():
        for vc, sub_val in val.items():
            total_job_info = JobInfo()
            mapping = {
                    "SUCCEEDED": JobInfo(),
                    "FAILED": JobInfo(),
                    "STOPPED": JobInfo(),
                    "RUNNING": JobInfo(),
                    "WAITING": JobInfo()}

            for job_status, job_info in sub_val.items():
                if job_status in mapping:
                    mapping[job_status] += job_info
                total_job_info += job_info

            result.append(JobReportEntries(username, vc, total_job_info,
                mapping["SUCCEEDED"], mapping["FAILED"], mapping["STOPPED"],
                mapping["RUNNING"], mapping["WAITING"]))

    return result, processed_apps


def get_alerts(prometheus_url, since, until):
    args = urllib.parse.urlencode({
        "query": "ALERTS{alertstate=\"firing\"}",
        "start": str(since),
        "end": str(until),
        "step": "5m",
        })

    url = urllib.parse.urljoin(prometheus_url,
            "/prometheus/api/v1/query_range") + "?" + args

    logger.debug("requesting %s", url)
    result = []

    obj = request_with_error_handling(url)

    if walk_json_field_safe(obj, "status") != "success":
        logger.warning("requesting %s failed, body is %s", url, obj)
        return result

    gap = 5 * 60 # because the step is 5m, the gap between two data point should be this

    metrics = walk_json_field_safe(obj, "data", "result")

    for metric in metrics:
        labels = walk_json_field_safe(metric, "metric")
        alert_name = walk_json_field_safe(labels, "alertname") or "unknown"

        values = walk_json_field_safe(metric, "values")
        if values is not None and len(values) > 0:
            start = end = values[0][0]
            events = []

            for i, value in enumerate(values):
                if i == len(values) - 1:
                    events.append({"start": start, "end": value[0]})
                    break

                if value[0] - end <= gap:
                    end = value[0]
                    continue
                else:
                    events.append({"start": start, "end": end})
                    start = end = value[0]

            for event in events:
                # because the end is the last time alert still happening, if we
                # treat end - start equals to be the durtion of the alert,
                # the alert with start == end will have durtion of 0, which is
                # quite confusing, so we set durtion to be end - start + gap
                result.append(Alert(alert_name, int(event["start"]),
                    int(event["end"] - event["start"] + gap),
                    labels))
        else:
            logger.warning("unexpected zero values in alert %s", alert_name)

    logger.info("get %d alert entries", len(result))

    return result


def delete_old_data(database, days):
    db = DB(database)
    now = datetime.datetime.now()
    delta = datetime.timedelta(days=days)

    ago = int(datetime.datetime.timestamp(now - delta))

    with db.conn:
        cursor = db.conn.cursor()

        # should not delete entries if finished_time is 0, they are running apps
        cursor.execute("""DELETE FROM apps WHERE finished_time<? AND finished_time!=0""",
                        (ago,))

        # should not delete entries if finished_time is 0, they are running jobs
        cursor.execute("""DELETE FROM frameworks WHERE finished_time<? AND finished_time!=0""",
                        (ago,))

        db.conn.commit()


def gen_report(database, prometheus_url, path, since, until):
    job_report, processed_apps = get_job_report(database, since, until)
    job_file = "%s_job.csv" % path
    with open(job_file, "w") as f:
        f.write("user,vc," +
        "total_count,total_time,total_cpu_sec,total_mem_sec,total_gpu_sec," +
        "succ_count,succ_time,succ_cpu_sec,succ_mem_sec,succ_gpu_sec," +
        "fail_count,fail_time,fail_cpu_sec,fail_mem_sec,fail_gpu_sec," +
        "stop_count,stop_time,stop_cpu_sec,stop_mem_sec,stop_gpu_sec," +
        "run_count,run_time,run_cpu_sec,run_mem_sec,run_gpu_sec," +
        "wait_count,wait_time,wait_cpu_sec,wait_mem_sec,wait_gpu_sec\n")
        for r in job_report:
            f.write("%s\n" % r)

    job_raw_file = "%s_raw_job.csv" % path
    with open(job_raw_file, "w") as f:
        f.write("user,vc,job,start_time,finish_time,waiting_time,run_time,retries,status,exit_code,cpu,mem,gpu\n")

        for job_name, job in processed_apps.items():
            if job.user == "unknown" or job.vc == "unknown":
                # this is due to Framework do not have job info, but yarn have
                continue

            elapsed_time = job.elapsed_time
            cpu = math.ceil(job.cpu_sec / elapsed_time)
            mem = math.ceil(job.mem_sec / elapsed_time)
            gpu = math.ceil(job.gpu_sec / elapsed_time)

            if job.finished_time == 0:
                waiting_time = 0 # Unable to generate waiting time
            else:
                waiting_time = job.finished_time - job.start_time - elapsed_time

            f.write("%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n" % (
                job.user,
                job.vc,
                job_name,
                format_time(job.start_time),
                format_time(job.finished_time),
                waiting_time,
                job.elapsed_time,
                job.retries,
                job.status,
                job.exit_code,
                cpu,
                mem,
                gpu))

    alert_report = get_alerts(prometheus_url, since, until)
    alert_file = "%s_alert.csv" % path
    with open(alert_file, "w") as f:
        f.write("alert_name,host_ip,source,start,durtion,labels\n")
        for r in alert_report:
            f.write("%s\n" % r)

    logger.info("write csv file into %s, %s and %s",
            job_file, job_raw_file, alert_file)

def main(args):
    if args.action == "refresh":
        delete_old_data(args.database, args.retain)
        refresh_cache(args.database, args.yarn_url, args.launcher_url)
    elif args.action == "report":
        gen_report(args.database, args.prometheus_url, args.file, args.since, args.until)
    else:
        sys.stderr.write("unknown action %s\n" % (args.action))
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)s - %(message)s",
            level=logging.INFO)

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["refresh", "report"])
    parser.add_argument("--yarn_url", "-y", required=True,
            help="Yarn rest api address, eg: http://127.0.0.1:8088")
    parser.add_argument("--prometheus_url", "-p", required=True,
            help="Prometheus url, eg: http://127.0.0.1:9091")
    parser.add_argument("--launcher_url", "-l", required=True,
            help="Framework launcher url, eg: http://127.0.0.1:9086")

    parser.add_argument("--retain", "-r", type=int, default=6*31,
            help="How many days to retain cache")
    parser.add_argument("--file", "-f", required=False,
            help="Output file prefix, required argument when action is report",
            default="cluster_report")

    parser.add_argument("--database", "-d", required=True,
            help="which sqlite db file to use")

    now = datetime.datetime.now()
    delta = datetime.timedelta(days=31)
    one_month_ago = int(datetime.datetime.timestamp(now - delta))
    now = int(datetime.datetime.timestamp(now))

    parser.add_argument("--since", "-s", type=int, default=one_month_ago,
            help="start time for generating report")
    parser.add_argument("--until", "-u", type=int, default=now,
            help="end time for generating report")

    args = parser.parse_args()

    main(args)