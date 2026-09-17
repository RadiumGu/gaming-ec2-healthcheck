#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stop_matrix.py —— EC2 停止耗时的模式对比实验编排器

存在的理由：第一轮 D4 用单次测量得出「挂死实例 force stop 257s，健康实例 16.8s，
差 15.3 倍」，并据此断言「自建恢复路径的恢复段没有改善」。这个结论有三个问题：

  1. 机制解释错了。`Force=True` 不跳过优雅关机 —— API 参考原文是
     「will first attempt a graceful shutdown ... if fails to complete within
     the timeout period, shuts down forcibly」。所以 257 秒极可能就是那个
     优雅关机超时，而真正能绕过它的参数是 `SkipOsShutdown=True`，当时没用。
  2. n=1。单点不能支撑一个倍数比值。
  3. 故障类型与客户场景不匹配（guest panic 只让 instance check 失败，
     客户抱怨的是 system check 失败 = 宿主机侧）。第 3 条注入不了，
     本脚本解决第 1、2 条。

## 实验矩阵

  guest 状态 × 停止模式 × 重复次数

  guest 状态：healthy（OS 正常，能完成关机）/ panicked（kernel.panic=0 后 panic 挂死）
  停止模式：plain（都不传）/ force（Force=True）/ skip_os（SkipOsShutdown=True）
            / force_skip（两个都传）

关键判据是 `stopping → stopped` 的墙钟耗时，1 秒粒度轮询。
其余段（stopped→running、running→app_ready）一并记录，但注意
app_ready 的测量在本脚本里是并发的，不像 recovery_orchestrator.py 那样
被 wait_status_ok 串行挡住 —— 那个缺陷在这里顺手修掉了。

## 输出

NDJSON，一行一个事件；每完成一轮 trial 追加一条 `trial_result`，
末尾一条 `summary` 给出每个 (state, mode) 组合的 n / min / median / max。

用法：
  python3 stop_matrix.py --instance-id i-xxx --health-url http://IP:8080/health \\
      --state healthy --modes plain force skip_os --reps 3 --out raw/D8/matrix.ndjson
  python3 stop_matrix.py --instance-id i-xxx --health-url http://IP:8080/health \\
      --state panicked --modes force skip_os --reps 2 --out raw/D8/matrix.ndjson

依赖 boto3 + 标准库。Python 3.9+。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

SCHEMA = "stop_matrix/1"

MODE_KWARGS: Dict[str, Dict[str, Any]] = {
    "plain": {},
    "force": {"Force": True},
    "skip_os": {"SkipOsShutdown": True},
    "force_skip": {"Force": True, "SkipOsShutdown": True},
}


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".%03dZ" % int((ts % 1) * 1000)


class Log:
    def __init__(self, path: Optional[str]) -> None:
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8") if path else sys.stdout
        self.own = bool(path)

    def emit(self, kind: str, **f: Any) -> None:
        rec = {"schema": SCHEMA, "kind": kind, "t": iso(now()), "ts": round(now(), 3)}
        rec.update(f)
        self.fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.fh.flush()

    def close(self) -> None:
        if self.own:
            self.fh.close()


class Runner:
    def __init__(self, a: argparse.Namespace, log: Log) -> None:
        cfg = BotoConfig(region_name=a.region, retries={"max_attempts": 3, "mode": "standard"},
                         connect_timeout=5, read_timeout=15)
        sess = boto3.session.Session()
        self.ec2 = sess.client("ec2", config=cfg)
        self.ssm = sess.client("ssm", config=cfg)
        self.a = a
        self.log = log
        self.results: List[Dict[str, Any]] = []

    # ---------------------------------------------------------------- 基础

    def state(self) -> str:
        try:
            r = self.ec2.describe_instances(InstanceIds=[self.a.instance_id])
            for res in r.get("Reservations", []) or []:
                for i in res.get("Instances", []) or []:
                    return (i.get("State", {}) or {}).get("Name", "?")
        except (ClientError, BotoCoreError) as exc:
            self.log.emit("api_error", api="DescribeInstances", error=str(exc))
        return "?"

    def wait_state(self, want: str, timeout: float, tag: str) -> Optional[float]:
        """返回从调用本函数到进入目标状态的秒数；超时返回 None。"""
        t0 = now()
        last = ""
        while now() - t0 < timeout:
            s = self.state()
            if s != last:
                self.log.emit("state", tag=tag, state=s, since_s=round(now() - t0, 3))
                last = s
            if s == want:
                return now() - t0
            time.sleep(self.a.poll)
        self.log.emit("wait_timeout", tag=tag, want=want, last=last, waited_s=round(now() - t0, 3))
        return None

    def app_tick(self) -> Optional[int]:
        if not self.a.health_url:
            return None
        try:
            with urllib.request.urlopen(self.a.health_url, timeout=2) as r:
                d = json.loads(r.read(65536).decode("utf-8", "replace"))
            t = d.get("tick")
            return int(t) if isinstance(t, int) else None
        except Exception:
            return None

    def wait_app_ready(self, timeout: float, tag: str) -> Optional[float]:
        """等到 tick 真的在推进。与状态检查并发，不被它挡住。"""
        t0 = now()
        first: Optional[int] = None
        while now() - t0 < timeout:
            t = self.app_tick()
            if t is not None:
                if first is None:
                    first = t
                    self.log.emit("app_endpoint_up", tag=tag, tick=t, since_s=round(now() - t0, 3))
                elif t != first:
                    return now() - t0
            time.sleep(1.0)
        self.log.emit("wait_timeout", tag=tag, want="app_ready", waited_s=round(now() - t0, 3))
        return None

    def ssm_run(self, cmds: List[str], tag: str, wait: bool = True) -> bool:
        try:
            r = self.ssm.send_command(
                InstanceIds=[self.a.instance_id], DocumentName="AWS-RunShellScript",
                Parameters={"commands": cmds}, TimeoutSeconds=60, Comment="stop_matrix %s" % tag)
        except (ClientError, BotoCoreError) as exc:
            self.log.emit("ssm_failed", tag=tag, error=str(exc))
            return False
        cid = r["Command"]["CommandId"]
        if not wait:
            self.log.emit("ssm_sent", tag=tag, command_id=cid, wait=False)
            return True
        deadline = now() + 90
        while now() < deadline:
            time.sleep(3)
            try:
                inv = self.ssm.get_command_invocation(CommandId=cid, InstanceId=self.a.instance_id)
            except ClientError as exc:
                if "InvocationDoesNotExist" in str(exc):
                    continue
                self.log.emit("ssm_failed", tag=tag, error=str(exc))
                return False
            if inv.get("Status") in ("Success", "Failed", "Cancelled", "TimedOut"):
                self.log.emit("ssm_done", tag=tag, status=inv["Status"],
                              out=(inv.get("StandardOutputContent") or "")[-1500:],
                              err=(inv.get("StandardErrorContent") or "")[-800:])
                return inv["Status"] == "Success"
        self.log.emit("ssm_failed", tag=tag, error="timeout")
        return False

    def ssm_online(self, timeout: float) -> bool:
        t0 = now()
        while now() - t0 < timeout:
            try:
                r = self.ssm.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [self.a.instance_id]}])
                for i in r.get("InstanceInformationList", []) or []:
                    if i.get("PingStatus") == "Online":
                        return True
            except (ClientError, BotoCoreError):
                pass
            time.sleep(5)
        return False

    # ---------------------------------------------------------------- 准备

    def ensure_running_healthy(self) -> bool:
        """把实例带回「running 且应用在推进」的状态，作为每轮 trial 的统一起点。

        起点不统一，测出来的分布就是几种不同东西混在一起。
        """
        s = self.state()
        if s == "stopped":
            try:
                self.ec2.start_instances(InstanceIds=[self.a.instance_id])
            except (ClientError, BotoCoreError) as exc:
                self.log.emit("api_error", api="StartInstances", error=str(exc))
                return False
            if self.wait_state("running", self.a.start_timeout, "prep_start") is None:
                return False
        elif s in ("stopping", "pending", "shutting-down"):
            self.log.emit("prep_wait", note="实例处于过渡状态 %s，等它稳定" % s)
            time.sleep(15)
            return self.ensure_running_healthy()
        elif s != "running":
            self.log.emit("prep_failed", state=s)
            return False

        if self.wait_app_ready(self.a.app_timeout, "prep_app") is None:
            return False
        if not self.ssm_online(self.a.ssm_timeout):
            self.log.emit("prep_failed", note="SSM 未上线，panic 类 trial 无法注入")
            return False
        return True

    def make_panicked(self) -> bool:
        """让 guest 内核 panic 并挂住（不自动重启）。

        必须先 kernel.panic=0；AL2023 默认 5 会在 5 秒后自动重启，
        产生的是一次约 12 秒的自愈重启而不是持续故障。
        sysctl -w 不持久，所以每轮 trial 都要重新设。
        """
        if not self.ssm_run(["sysctl -w kernel.panic=0", "sysctl kernel.panic"], "set_panic_0"):
            return False
        # panic 命令自身不等返回：实例会在执行瞬间死掉，SSM 必然报失败
        self.ssm_run(["sync", "echo c > /proc/sysrq-trigger"], "panic", wait=False)
        # 确认真的不可达了，再往下走 —— 否则测的是一台还活着的实例
        t0 = now()
        while now() - t0 < 120:
            if self.app_tick() is None:
                # 端点没了；再等几秒确认不是瞬时抖动
                time.sleep(5)
                if self.app_tick() is None:
                    self.log.emit("panicked_confirmed", after_s=round(now() - t0, 3))
                    return True
            time.sleep(2)
        self.log.emit("panic_failed", note="注入后应用仍可达，panic 未生效")
        return False

    # ---------------------------------------------------------------- 一轮

    def trial(self, guest_state: str, mode: str, rep: int) -> Optional[Dict[str, Any]]:
        tag = "%s/%s/#%d" % (guest_state, mode, rep)
        self.log.emit("trial_begin", guest_state=guest_state, mode=mode, rep=rep,
                      stop_kwargs=MODE_KWARGS[mode])

        if not self.ensure_running_healthy():
            self.log.emit("trial_aborted", tag=tag, reason="prep_failed")
            return None

        if guest_state == "panicked":
            if not self.make_panicked():
                self.log.emit("trial_aborted", tag=tag, reason="panic_failed")
                return None

        kw = MODE_KWARGS[mode]
        t_call = now()
        try:
            self.ec2.stop_instances(InstanceIds=[self.a.instance_id], **kw)
        except (ClientError, BotoCoreError) as exc:
            self.log.emit("trial_aborted", tag=tag, reason="stop_api_failed", error=str(exc))
            return None
        t_api = now() - t_call
        self.log.emit("stop_api_ok", tag=tag, stop_kwargs=kw, api_s=round(t_api, 3))

        # 关键测量：从 StopInstances 返回到 state=stopped
        stop_s = self.wait_state("stopped", self.a.stop_timeout, "stop:" + tag)
        if stop_s is None:
            self.log.emit("trial_result", tag=tag, guest_state=guest_state, mode=mode, rep=rep,
                          stop_seconds=None, note="stop 超时未完成，记为未测到而不是填估算值")
            return None

        try:
            self.ec2.start_instances(InstanceIds=[self.a.instance_id])
        except (ClientError, BotoCoreError) as exc:
            self.log.emit("trial_aborted", tag=tag, reason="start_api_failed", error=str(exc))
            return None
        start_s = self.wait_state("running", self.a.start_timeout, "start:" + tag)
        app_s = self.wait_app_ready(self.a.app_timeout, "app:" + tag) if start_s is not None else None

        row = {
            "guest_state": guest_state, "mode": mode, "rep": rep,
            "stop_kwargs": kw,
            "stop_api_seconds": round(t_api, 3),
            "stop_seconds": round(stop_s, 3),
            "start_seconds": round(start_s, 3) if start_s is not None else None,
            "app_ready_seconds": round(app_s, 3) if app_s is not None else None,
        }
        self.log.emit("trial_result", **row)
        self.results.append(row)
        return row

    # ---------------------------------------------------------------- 汇总

    def summarize(self) -> None:
        groups: Dict[str, List[float]] = {}
        for r in self.results:
            if r.get("stop_seconds") is None:
                continue
            groups.setdefault("%s/%s" % (r["guest_state"], r["mode"]), []).append(r["stop_seconds"])
        summary = {}
        for k, vals in sorted(groups.items()):
            summary[k] = {
                "n": len(vals),
                "min": round(min(vals), 3),
                "median": round(statistics.median(vals), 3),
                "max": round(max(vals), 3),
                "values": [round(v, 3) for v in vals],
            }
        self.log.emit("summary", stop_seconds_by_group=summary,
                      note="n 小于 3 的组不足以支撑倍数比较，只能作为单点观测报告")
        print(json.dumps(summary, ensure_ascii=False, indent=2))


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="EC2 停止耗时模式对比实验")
    p.add_argument("--instance-id", required=True)
    p.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1"))
    p.add_argument("--health-url", default="")
    p.add_argument("--state", choices=["healthy", "panicked"], required=True)
    p.add_argument("--modes", nargs="+", default=["plain", "force", "skip_os"],
                   choices=sorted(MODE_KWARGS.keys()))
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--stop-timeout", type=float, default=600.0)
    p.add_argument("--start-timeout", type=float, default=300.0)
    p.add_argument("--app-timeout", type=float, default=240.0)
    p.add_argument("--ssm-timeout", type=float, default=300.0)
    p.add_argument("--out", default="")
    a = p.parse_args(argv)

    log = Log(a.out or None)
    r = Runner(a, log)
    log.emit("run_begin", instance_id=a.instance_id, guest_state=a.state,
             modes=a.modes, reps=a.reps, region=a.region)
    try:
        for rep in range(1, a.reps + 1):
            for mode in a.modes:
                r.trial(a.state, mode, rep)
        r.summarize()
    finally:
        log.emit("run_end", trials=len(r.results))
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
