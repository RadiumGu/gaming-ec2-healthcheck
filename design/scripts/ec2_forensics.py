#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ec2_forensics.py —— EC2 故障与恢复的常驻取证器

存在的理由：`StatusCheckFailed_System` 注入不了。system status check 测的是宿主机侧，
guest 内做任何事都影响不到它，FIS 也没有对应动作。所以「system check 上报到底有多慢」
这件事只能被动取证 —— 常驻采集，等下一次真实故障，然后拿出精确的分段耗时表。
拿这个去跟服务团队谈，比「观察下来 6-7 分钟」有说服力得多。

## 三层时间轴必须分开记（本脚本的核心设计）

同一个故障有三个不同的时间，混用会算出错误的分段耗时：

  t_fault      故障真正发生的时刻        —— 注入器自报 / console output / 实例事件
  t_datapoint  指标数据点自己声明的时间戳  —— CloudWatch Timestamp 字段
  t_observed   我们第一次能看到它的时刻    —— 本脚本发起 API 调用并拿到该值的本地时间

  t_datapoint - t_fault      = AWS 侧的检测延迟（客户要 ST 改进的那一段）
  t_observed  - t_datapoint  = 指标发布延迟
  告警评估周期               = 再叠加上去的那一段

## 采集通道

  A. DescribeInstanceStatus（1s 轮询，IncludeAllInstances=true）
     —— 最快能看到状态翻转的通道，比等 CloudWatch 告警评估快一到两分钟。
        每次调用都记 API 往返耗时，状态一变就单独打一条 transition 事件。
  B. DescribeInstances       —— 实例生命周期（running/stopping/stopped/pending）
  C. CloudWatch GetMetricData —— StatusCheckFailed / _Instance / _System / _AttachedEBS
     以及 EBS VolumeStalledIOCheck / VolumeQueueLength。
     每个数据点同时记 Timestamp 与本次观测时刻。
  D. DescribeVolumeStatus    —— 卷 impaired 转换（pause-volume-io 演练的主判据之一）
  E. GetConsoleOutput        —— 故障期抓取内核 oops / panic 现场
  F. DescribeInstanceEvents（describe_instance_status 的 Events 字段）—— 预定维护/退役
  G. AWS Health DescribeEvents —— 需要 Business/Enterprise 支持计划，
     无权限时降级为 unavailable 并记明原因，不静默跳过。

## 已知坑（写进代码而不是只写进文档）

  * `describe_instance_status` 默认只返回「非 running 或有异常」的实例。
    必须传 IncludeAllInstances=True，否则健康实例返回空列表，
    空列表与「实例不见了」无法区分。
  * pause-volume-io 期间 VolumeReadOps/WriteOps/吞吐全部降为 0，
    任何「量高于阈值就告警」的规则根本不会触发。主判据必须是
    VolumeStalledIOCheck=1 与卷状态 impaired，辅以 VolumeQueueLength 非零。
  * 采集失败绝不返回与合法测量同形的值。本脚本所有采集结果都带 ok 字段，
    失败时 ok=False 且不写任何数值，下游统计一律排除。
  * 1s 轮询会撞 API 限流。默认只轮询状态类只读 API，指标类按
    --metric-interval（默认 20s）单独节流；遇 Throttling 指数退避并记录。

仅依赖 boto3 + 标准库。Python 3.9+。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError, BotoCoreError
except Exception as _exc:  # pragma: no cover
    sys.stderr.write("boto3 import failed: %s\n" % _exc)
    raise

SCHEMA_VERSION = "forensics/1"

STATUS_METRICS = [
    ("AWS/EC2", "StatusCheckFailed"),
    ("AWS/EC2", "StatusCheckFailed_Instance"),
    ("AWS/EC2", "StatusCheckFailed_System"),
    ("AWS/EC2", "StatusCheckFailed_AttachedEBS"),
]
EBS_METRICS = [
    ("AWS/EBS", "VolumeStalledIOCheck"),
    ("AWS/EBS", "VolumeQueueLength"),
    ("AWS/EBS", "VolumeReadOps"),
    ("AWS/EBS", "VolumeWriteOps"),
]


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".%03dZ" % int((ts % 1) * 1000)


# ---------------------------------------------------------------- 输出


class Sink:
    """按天分文件的 NDJSON 输出。常驻进程必须能被 logrotate 之外的方式安全轮转。"""

    def __init__(self, out_dir: str, prefix: str = "forensics") -> None:
        self.dir = out_dir
        self.prefix = prefix
        os.makedirs(out_dir, exist_ok=True)
        self._day = ""
        self._fh = None

    def _rotate(self) -> None:
        day = time.strftime("%Y%m%d", time.gmtime())
        if day != self._day:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
            self._day = day
            path = os.path.join(self.dir, "%s-%s.ndjson" % (self.prefix, day))
            self._fh = open(path, "a", encoding="utf-8")

    def emit(self, kind: str, **fields: Any) -> None:
        self._rotate()
        rec = {"schema": SCHEMA_VERSION, "kind": kind,
               "t_observed": iso(now()), "ts_observed": round(now(), 3)}
        rec.update(fields)
        assert self._fh is not None
        self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 采集器


@dataclass
class Call:
    """一次 API 调用的结果，含往返耗时。ok=False 时 data 无意义。"""
    ok: bool
    data: Any = None
    latency_ms: float = 0.0
    error: str = ""


class Collector:
    def __init__(self, region: str, sink: Sink) -> None:
        cfg = BotoConfig(
            region_name=region,
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=3,
            read_timeout=8,
        )
        self.sess = boto3.session.Session()
        self.ec2 = self.sess.client("ec2", config=cfg)
        self.cw = self.sess.client("cloudwatch", config=cfg)
        try:
            # Health 是全局服务，端点固定在 us-east-1
            self.health = self.sess.client("health", config=BotoConfig(region_name="us-east-1"))
        except Exception:
            self.health = None
        self.sink = sink
        self.region = region

    def _call(self, fn, **kw) -> Call:
        t0 = time.monotonic()
        try:
            data = fn(**kw)
            return Call(True, data, (time.monotonic() - t0) * 1000.0)
        except (ClientError, BotoCoreError) as exc:
            return Call(False, None, (time.monotonic() - t0) * 1000.0, "%s: %s" % (type(exc).__name__, exc))
        except Exception as exc:
            return Call(False, None, (time.monotonic() - t0) * 1000.0, "%s: %s" % (type(exc).__name__, exc))

    # -- A/F: 状态检查 + 实例事件 ----------------------------------------
    def instance_status(self, ids: List[str]) -> Call:
        # IncludeAllInstances 必须为 True，否则健康实例返回空，空与「实例消失」不可区分
        return self._call(self.ec2.describe_instance_status,
                          InstanceIds=ids, IncludeAllInstances=True)

    # -- B: 生命周期 -------------------------------------------------------
    def instances(self, ids: List[str]) -> Call:
        return self._call(self.ec2.describe_instances, InstanceIds=ids)

    # -- D: 卷状态 ---------------------------------------------------------
    def volume_status(self, vol_ids: List[str]) -> Call:
        if not vol_ids:
            return Call(False, None, 0.0, "no_volumes")
        return self._call(self.ec2.describe_volume_status, VolumeIds=vol_ids)

    # -- C: 指标 -----------------------------------------------------------
    def metrics(self, instance_id: str, vol_ids: List[str], lookback_s: int) -> Call:
        end = int(now())
        start = end - lookback_s
        queries: List[Dict[str, Any]] = []
        for i, (ns, name) in enumerate(STATUS_METRICS):
            queries.append({
                "Id": "i%d" % i,
                "MetricStat": {
                    "Metric": {"Namespace": ns, "MetricName": name,
                               "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
                    "Period": 60, "Stat": "Maximum",
                },
                "ReturnData": True,
            })
        for vi, vol in enumerate(vol_ids):
            for mi, (ns, name) in enumerate(EBS_METRICS):
                queries.append({
                    "Id": "v%d_%d" % (vi, mi),
                    "MetricStat": {
                        "Metric": {"Namespace": ns, "MetricName": name,
                                   "Dimensions": [{"Name": "VolumeId", "Value": vol}]},
                        "Period": 60, "Stat": "Maximum",
                    },
                    "ReturnData": True,
                    "Label": "%s|%s" % (vol, name),
                })
        if not queries:
            return Call(False, None, 0.0, "no_queries")
        return self._call(self.cw.get_metric_data,
                          MetricDataQueries=queries,
                          StartTime=start, EndTime=end, ScanBy="TimestampDescending")

    # -- E: console output -------------------------------------------------
    def console(self, instance_id: str, latest: bool = True) -> Call:
        kw: Dict[str, Any] = {"InstanceId": instance_id}
        if latest:
            kw["Latest"] = True
        return self._call(self.ec2.get_console_output, **kw)

    # -- G: AWS Health -----------------------------------------------------
    def health_events(self, lookback_s: int) -> Call:
        if self.health is None:
            return Call(False, None, 0.0, "health_client_unavailable")
        start = now() - lookback_s
        return self._call(self.health.describe_events,
                          filter={"startTimes": [{"from": start}],
                                  "regions": [self.region]})


# ---------------------------------------------------------------- 状态折叠


def fold_status(resp: Dict[str, Any]) -> Dict[str, Any]:
    """把 describe_instance_status 的响应折成一个扁平可比较的 key。

    状态转换检测靠比较这个 key，而不是比较整个响应 —— 响应里含
    每次都变的时间戳字段，整体比较会把每一轮都判成「变了」。
    """
    out: Dict[str, Any] = {}
    for st in resp.get("InstanceStatuses", []) or []:
        iid = st.get("InstanceId", "?")
        det_inst = {d.get("Name"): d.get("Status")
                    for d in (st.get("InstanceStatus", {}) or {}).get("Details", []) or []}
        det_sys = {d.get("Name"): d.get("Status")
                   for d in (st.get("SystemStatus", {}) or {}).get("Details", []) or []}
        det_ebs = {d.get("Name"): d.get("Status")
                   for d in (st.get("AttachedEbsStatus", {}) or {}).get("Details", []) or []}
        out[iid] = {
            "instance_state": (st.get("InstanceState", {}) or {}).get("Name"),
            "instance_status": (st.get("InstanceStatus", {}) or {}).get("Status"),
            "system_status": (st.get("SystemStatus", {}) or {}).get("Status"),
            "attached_ebs_status": (st.get("AttachedEbsStatus", {}) or {}).get("Status"),
            "instance_details": det_inst,
            "system_details": det_sys,
            "ebs_details": det_ebs,
            "events": [
                {"code": e.get("Code"), "desc": e.get("Description"),
                 "not_before": str(e.get("NotBefore")), "not_after": str(e.get("NotAfter"))}
                for e in (st.get("Events", []) or [])
            ],
        }
    return out


# ---------------------------------------------------------------- 主循环


class Forensics:
    def __init__(self, args: argparse.Namespace) -> None:
        self.a = args
        self.sink = Sink(args.out_dir, args.prefix)
        self.c = Collector(args.region, self.sink)
        self.stop = False
        self._last_status_key: Optional[str] = None
        self._last_lifecycle: Optional[str] = None
        self._next_metric = 0.0
        self._next_health = 0.0
        self._next_volstatus = 0.0
        self._seen_datapoints: set = set()
        self._backoff = 0.0
        self._vol_ids: List[str] = []
        self._console_captured_for: set = set()

    # -- 启动自检：证明每个通道真的能取到东西 ----------------------------
    def preflight(self) -> bool:
        ids = self.a.instance_ids
        st = self.c.instance_status(ids)
        inst = self.c.instances(ids)
        ok_core = st.ok and inst.ok

        vols: List[str] = []
        if inst.ok:
            for r in inst.data.get("Reservations", []) or []:
                for i in r.get("Instances", []) or []:
                    for bd in i.get("BlockDeviceMappings", []) or []:
                        vid = (bd.get("Ebs", {}) or {}).get("VolumeId")
                        if vid:
                            vols.append(vid)
        self._vol_ids = sorted(set(vols))

        met = self.c.metrics(ids[0], self._vol_ids, 900) if ids else Call(False, error="no_ids")
        hlt = self.c.health_events(3600)
        con = self.c.console(ids[0]) if ids else Call(False, error="no_ids")

        # 指标通道要单独判「有没有数据点」，不能只判 API 成功 ——
        # list/get 成功但零数据点，与「指标不存在」是两件事，必须分开报。
        dp_count = 0
        if met.ok:
            for res in met.data.get("MetricDataResults", []) or []:
                dp_count += len(res.get("Timestamps", []) or [])

        self.sink.emit(
            "preflight",
            region=self.a.region, instance_ids=ids, volumes=self._vol_ids,
            channel_instance_status={"ok": st.ok, "error": st.error, "latency_ms": round(st.latency_ms, 1)},
            channel_instances={"ok": inst.ok, "error": inst.error, "latency_ms": round(inst.latency_ms, 1)},
            channel_metrics={"ok": met.ok, "error": met.error, "datapoints": dp_count,
                             "latency_ms": round(met.latency_ms, 1)},
            channel_console={"ok": con.ok, "error": con.error},
            channel_health={"ok": hlt.ok, "error": hlt.error,
                            "note": "Health API 需要 Business/Enterprise 支持计划；"
                                    "SubscriptionRequiredException 属预期降级，不是缺陷"},
            verdict="ok" if ok_core else "degraded",
        )
        return ok_core

    def tick_status(self) -> None:
        call = self.c.instance_status(self.a.instance_ids)
        if not call.ok:
            low = call.error.lower()
            if "throttl" in low or "requestlimitexceeded" in low:
                self._backoff = min(30.0, max(1.0, self._backoff * 2 or 1.0))
                self.sink.emit("throttled", api="DescribeInstanceStatus",
                               error=call.error, backoff_s=self._backoff)
            else:
                self.sink.emit("collect_error", api="DescribeInstanceStatus", error=call.error)
            return
        self._backoff = 0.0
        folded = fold_status(call.data)
        key = json.dumps(folded, sort_keys=True)
        if key != self._last_status_key:
            self.sink.emit(
                "status_transition",
                api_latency_ms=round(call.latency_ms, 1),
                previous=json.loads(self._last_status_key) if self._last_status_key else None,
                current=folded,
            )
            self._last_status_key = key
            # 状态一变就抓一次 console output：内核 oops/panic 现场只在当时存在
            for iid, cur in folded.items():
                bad = (cur.get("instance_status") != "ok" or cur.get("system_status") != "ok"
                       or cur.get("attached_ebs_status") not in ("ok", None))
                stamp = "%s|%s|%s|%s" % (iid, cur.get("instance_status"),
                                         cur.get("system_status"), cur.get("attached_ebs_status"))
                if bad and stamp not in self._console_captured_for:
                    self._console_captured_for.add(stamp)
                    con = self.c.console(iid)
                    body = ""
                    if con.ok:
                        body = (con.data.get("Output") or "")
                        if self.a.console_tail and len(body) > self.a.console_tail:
                            body = body[-self.a.console_tail:]
                    self.sink.emit("console_snapshot", instance_id=iid, ok=con.ok,
                                   error=con.error, state_stamp=stamp, tail=body)

    def tick_lifecycle(self) -> None:
        call = self.c.instances(self.a.instance_ids)
        if not call.ok:
            self.sink.emit("collect_error", api="DescribeInstances", error=call.error)
            return
        cur: Dict[str, Any] = {}
        for r in call.data.get("Reservations", []) or []:
            for i in r.get("Instances", []) or []:
                cur[i["InstanceId"]] = {
                    "state": (i.get("State", {}) or {}).get("Name"),
                    "reason": (i.get("StateReason", {}) or {}).get("Message"),
                    "transition": i.get("StateTransitionReason"),
                    "private_ip": i.get("PrivateIpAddress"),
                    "public_ip": i.get("PublicIpAddress"),
                    "az": (i.get("Placement", {}) or {}).get("AvailabilityZone"),
                    "launch_time": str(i.get("LaunchTime")),
                    # 记录 auto recovery 配置：自建编排会和它抢，必须知道它开没开
                    "auto_recovery": (i.get("MaintenanceOptions", {}) or {}).get("AutoRecovery"),
                }
        key = json.dumps(cur, sort_keys=True)
        if key != self._last_lifecycle:
            self.sink.emit("lifecycle_transition",
                           api_latency_ms=round(call.latency_ms, 1),
                           previous=json.loads(self._last_lifecycle) if self._last_lifecycle else None,
                           current=cur)
            self._last_lifecycle = key

    def tick_metrics(self) -> None:
        for iid in self.a.instance_ids:
            call = self.c.metrics(iid, self._vol_ids, self.a.metric_lookback)
            if not call.ok:
                self.sink.emit("collect_error", api="GetMetricData", instance_id=iid, error=call.error)
                continue
            for res in call.data.get("MetricDataResults", []) or []:
                label = res.get("Label") or res.get("Id")
                tss = res.get("Timestamps", []) or []
                vals = res.get("Values", []) or []
                for ts, val in zip(tss, vals):
                    ts_epoch = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
                    dedupe = "%s|%s|%.0f" % (iid, label, ts_epoch)
                    if dedupe in self._seen_datapoints:
                        continue
                    self._seen_datapoints.add(dedupe)
                    # 三层时间轴的落点：t_datapoint 与 t_observed 分开记，
                    # publish_lag 是两者之差 —— 这就是「指标发布延迟」
                    self.sink.emit(
                        "metric_datapoint",
                        instance_id=iid, label=label, value=val,
                        t_datapoint=iso(ts_epoch), ts_datapoint=round(ts_epoch, 3),
                        publish_lag_s=round(now() - ts_epoch, 3),
                        status_code=res.get("StatusCode"),
                    )
            if len(self._seen_datapoints) > 200000:
                self._seen_datapoints.clear()

    def tick_volume_status(self) -> None:
        if not self._vol_ids:
            return
        call = self.c.volume_status(self._vol_ids)
        if not call.ok:
            self.sink.emit("collect_error", api="DescribeVolumeStatus", error=call.error)
            return
        rows = []
        for vs in call.data.get("VolumeStatuses", []) or []:
            rows.append({
                "volume_id": vs.get("VolumeId"),
                "status": (vs.get("VolumeStatus", {}) or {}).get("Status"),
                "details": [{"n": d.get("Name"), "s": d.get("Status")}
                            for d in (vs.get("VolumeStatus", {}) or {}).get("Details", []) or []],
                "events": [{"type": e.get("EventType"), "desc": e.get("Description")}
                           for e in (vs.get("Events", []) or [])],
            })
        self.sink.emit("volume_status", api_latency_ms=round(call.latency_ms, 1), volumes=rows)

    def tick_health(self) -> None:
        call = self.c.health_events(self.a.health_lookback)
        if not call.ok:
            # SubscriptionRequiredException 是预期降级，按 info 记而不是 error
            kind = "health_unavailable" if "Subscription" in call.error else "collect_error"
            self.sink.emit(kind, api="DescribeEvents", error=call.error)
            return
        evs = []
        for e in call.data.get("events", []) or []:
            evs.append({
                "arn": e.get("arn"), "service": e.get("service"),
                "type_code": e.get("eventTypeCode"), "category": e.get("eventTypeCategory"),
                "region": e.get("region"), "status": e.get("statusCode"),
                "start": str(e.get("startTime")), "last_update": str(e.get("lastUpdatedTime")),
            })
        if evs:
            self.sink.emit("health_events", count=len(evs), events=evs)

    def run(self) -> int:
        if not self.preflight() and not self.a.force:
            self.sink.emit("fatal", error="preflight_failed",
                           hint="核心通道（DescribeInstanceStatus / DescribeInstances）不可用，"
                                "取证器起来了也采不到东西。用 --force 明确接受降级运行。")
            return 3
        self.sink.emit("start", instance_ids=self.a.instance_ids, region=self.a.region,
                       status_interval=self.a.interval, metric_interval=self.a.metric_interval,
                       volumes=self._vol_ids)
        t_end = now() + self.a.duration if self.a.duration else None
        while not self.stop:
            loop_t0 = time.monotonic()
            self.tick_status()
            self.tick_lifecycle()
            n = now()
            if n >= self._next_metric:
                self.tick_metrics()
                self._next_metric = n + self.a.metric_interval
            if n >= self._next_volstatus:
                self.tick_volume_status()
                self._next_volstatus = n + self.a.volume_interval
            if n >= self._next_health:
                self.tick_health()
                self._next_health = n + self.a.health_interval
            if t_end and now() >= t_end:
                self.sink.emit("stop", reason="duration_reached")
                break
            spent = time.monotonic() - loop_t0
            time.sleep(max(0.0, self.a.interval + self._backoff - spent))
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EC2 故障/恢复常驻取证器（三层时间轴）")
    p.add_argument("--instance-ids", nargs="+", required=True)
    p.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1"))
    p.add_argument("--out-dir", required=True)
    p.add_argument("--prefix", default="forensics")
    p.add_argument("--interval", type=float, default=1.0, help="状态类 API 轮询间隔秒")
    p.add_argument("--metric-interval", type=float, default=20.0, help="指标轮询间隔秒")
    p.add_argument("--metric-lookback", type=int, default=900, help="每次指标查询回看秒数")
    p.add_argument("--volume-interval", type=float, default=10.0)
    p.add_argument("--health-interval", type=float, default=120.0)
    p.add_argument("--health-lookback", type=int, default=7200)
    p.add_argument("--console-tail", type=int, default=20000, help="console output 只留尾部多少字节")
    p.add_argument("--duration", type=float, default=0.0, help="跑多少秒后退出，0 = 常驻")
    p.add_argument("--force", action="store_true", help="preflight 失败也继续（降级运行）")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    f = Forensics(args)

    def _sig(_s: int, _fr: Any) -> None:
        f.stop = True
        f.sink.emit("stop", reason="signal")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    try:
        return f.run()
    finally:
        f.sink.close()


if __name__ == "__main__":
    sys.exit(main())
