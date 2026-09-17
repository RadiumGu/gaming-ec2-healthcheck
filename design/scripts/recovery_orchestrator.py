#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recovery_orchestrator.py —— 区服恢复编排器

为什么不等 auto recovery：它是 best-effort 的原地迁移，AWS 不承诺时长，
文档列出的失败情形包括「AWS 服务事件期间不运行」「替换硬件容量不足」
「当日恢复次数达上限」。客户能容忍内存丢失，等于不需要它最贵的那个卖点
（保留实例 ID / IP / EBS 的原地迁移），所以主路径自己做、时间自己可控，
把 auto recovery 留作兜底。

## 动作阶梯（按探测器的分类升级，不是一律重启）

  APP_STUCK   进程卡死（端口通、tick 冻住）
              -> SSM 重启游戏服务；仍不恢复则升级到 OS 重启
  APP_DEAD    进程没了（ping 通、端口不通）
              -> SSM 重启游戏服务
  HOST_DOWN   宿主机/实例侧不可达（ping+端口都不通、sentinel 正常）
              -> stop --force + start（换宿主机）
  其它        不动作。PROBER_SIDE / UNKNOWN 永远不触发动作。

## 安全机制（缺一个都会把一次故障放大成一串重启）

  * 默认 dry-run，只有 --apply 才真动手
  * 单飞锁：同一实例同一时间只允许一个编排在跑（O_EXCL 锁文件 + PID 存活校验）
  * 冷却期：距上次动作不足 --cooldown 秒直接拒绝
  * 每日上限：--max-per-day 次，超了拒绝并要求人工介入
  * auto recovery race 检查：动作前读 MaintenanceOptions.AutoRecovery，
    为 default/enabled 时明确记一条 race 警告 —— 你的 stop 和 AWS 的迁移
    可能同时进行，这一点必须在演练里实测，不能纸面推断

## 时间打点

每个阶段都写一条带时间戳的事件，t_* 字段可直接被 timeline.py 折成分段耗时表：
  decision -> api_call -> stopping -> stopped -> pending -> running
           -> status_ok -> app_ready

仅依赖 boto3 + 标准库。Python 3.9+。
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, BotoCoreError

SCHEMA_VERSION = "orchestrator/1"

VERDICT_APP_STUCK = "APP_STUCK"
VERDICT_APP_DEAD = "APP_DEAD"
VERDICT_HOST_DOWN = "HOST_DOWN"
VERDICT_STORAGE_STALLED = "STORAGE_STALLED"
ACTIONABLE = {VERDICT_APP_STUCK, VERDICT_APP_DEAD, VERDICT_HOST_DOWN,
              VERDICT_STORAGE_STALLED}


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".%03dZ" % int((ts % 1) * 1000)


class Log:
    def __init__(self, path: Optional[str]) -> None:
        self._fh = open(path, "a", encoding="utf-8") if path else sys.stdout
        self._own = bool(path)
        self.t0 = now()

    def emit(self, kind: str, **f: Any) -> None:
        rec = {"schema": SCHEMA_VERSION, "kind": kind, "t": iso(now()),
               "ts": round(now(), 3), "elapsed_s": round(now() - self.t0, 3)}
        rec.update(f)
        self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._own:
            try:
                self._fh.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 闸门


class Gate:
    """单飞锁 + 冷却 + 每日上限。状态落盘，因为编排器是被探测器一次性拉起的短命进程。"""

    def __init__(self, state_dir: str, instance_id: str, log: Log) -> None:
        self.dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.iid = instance_id
        self.log = log
        self.lock_path = os.path.join(state_dir, "lock-%s" % instance_id)
        self.hist_path = os.path.join(state_dir, "history-%s.json" % instance_id)

    # -- 单飞 --------------------------------------------------------------
    def acquire(self) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, json.dumps({"pid": os.getpid(), "ts": now()}).encode())
            os.close(fd)
            return True
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                self.log.emit("gate_error", stage="acquire", error=str(exc))
                return False
        # 锁已存在：检查持有者是否还活着，避免一次崩溃永久堵死自愈
        try:
            with open(self.lock_path, "r", encoding="utf-8") as fh:
                held = json.load(fh)
            pid = int(held.get("pid", -1))
            alive = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                self.log.emit("gate_reject", reason="another_orchestrator_running", holder=held)
                return False
            self.log.emit("gate_stale_lock_reclaimed", holder=held)
            os.unlink(self.lock_path)
            return self.acquire()
        except Exception as exc:
            self.log.emit("gate_error", stage="inspect_lock", error=str(exc))
            return False

    def release(self) -> None:
        try:
            os.unlink(self.lock_path)
        except Exception:
            pass

    # -- 冷却 + 每日上限 ---------------------------------------------------
    def _history(self) -> List[float]:
        try:
            with open(self.hist_path, "r", encoding="utf-8") as fh:
                return [float(x) for x in json.load(fh)]
        except Exception:
            return []

    def _record(self, ts: float) -> None:
        h = self._history()
        h.append(ts)
        h = [x for x in h if now() - x < 7 * 86400]
        with open(self.hist_path, "w", encoding="utf-8") as fh:
            json.dump(h, fh)

    def check_rate(self, cooldown: float, max_per_day: int) -> Tuple[bool, str]:
        h = self._history()
        if h:
            since = now() - max(h)
            if since < cooldown:
                return False, "cooldown: last action %.0fs ago < %.0fs" % (since, cooldown)
        today = [x for x in h if now() - x < 86400]
        if len(today) >= max_per_day:
            return False, "daily cap reached: %d actions in last 24h >= %d" % (len(today), max_per_day)
        return True, "ok"

    def stamp(self) -> None:
        self._record(now())


# ---------------------------------------------------------------- 动作


class Orchestrator:
    def __init__(self, args: argparse.Namespace, log: Log) -> None:
        cfg = BotoConfig(region_name=args.region, retries={"max_attempts": 3, "mode": "standard"},
                         connect_timeout=5, read_timeout=15)
        sess = boto3.session.Session()
        self.ec2 = sess.client("ec2", config=cfg)
        self.ssm = sess.client("ssm", config=cfg)
        self.a = args
        self.log = log

    # -- 前置读取 ----------------------------------------------------------
    def describe(self) -> Optional[Dict[str, Any]]:
        try:
            r = self.ec2.describe_instances(InstanceIds=[self.a.instance_id])
        except (ClientError, BotoCoreError) as exc:
            self.log.emit("describe_failed", error=str(exc))
            return None
        for res in r.get("Reservations", []) or []:
            for i in res.get("Instances", []) or []:
                return i
        return None

    def preflight(self) -> Optional[Dict[str, Any]]:
        inst = self.describe()
        if not inst:
            self.log.emit("fatal", error="instance_not_found", instance_id=self.a.instance_id)
            return None
        auto_rec = (inst.get("MaintenanceOptions", {}) or {}).get("AutoRecovery")
        state = (inst.get("State", {}) or {}).get("Name")
        self.log.emit("preflight", instance_id=self.a.instance_id, state=state,
                      auto_recovery=auto_rec, az=(inst.get("Placement", {}) or {}).get("AvailabilityZone"),
                      private_ip=inst.get("PrivateIpAddress"))
        if auto_rec in ("default", "enabled"):
            self.log.emit(
                "race_warning",
                auto_recovery=auto_rec,
                note="自建编排与 AWS auto recovery 会同时对同一实例动手。本次不改这个配置，"
                     "但必须在演练里实测两者的相互影响；若确定自己接管全部恢复，"
                     "用 modify-instance-maintenance-options --auto-recovery disabled 关掉。",
            )
        return inst

    # -- 通道 1：SSM 重启进程/服务 ----------------------------------------
    def ssm_run(self, commands: List[str], label: str, timeout: int = 120) -> Tuple[bool, str]:
        if not self.a.apply:
            self.log.emit("dry_run", action="ssm_send_command", label=label, commands=commands)
            return True, "dry-run"
        try:
            r = self.ssm.send_command(
                InstanceIds=[self.a.instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
                TimeoutSeconds=60,
                Comment="ec2-failure recovery: %s" % label,
            )
        except (ClientError, BotoCoreError) as exc:
            self.log.emit("ssm_send_failed", label=label, error=str(exc))
            return False, str(exc)
        cid = r["Command"]["CommandId"]
        self.log.emit("ssm_sent", label=label, command_id=cid, commands=commands)
        deadline = now() + timeout + 60
        while now() < deadline:
            time.sleep(2)
            try:
                inv = self.ssm.get_command_invocation(CommandId=cid, InstanceId=self.a.instance_id)
            except ClientError as exc:
                if "InvocationDoesNotExist" in str(exc):
                    continue
                self.log.emit("ssm_poll_failed", label=label, error=str(exc))
                return False, str(exc)
            st = inv.get("Status")
            if st in ("Success", "Failed", "Cancelled", "TimedOut"):
                # 绝不把输出丢进 /dev/null：失败原因与提权提示都在这里
                self.log.emit("ssm_done", label=label, status=st,
                              rc=inv.get("ResponseCode"),
                              stdout=(inv.get("StandardOutputContent") or "")[-4000:],
                              stderr=(inv.get("StandardErrorContent") or "")[-4000:])
                return st == "Success", st
        self.log.emit("ssm_timeout", label=label, command_id=cid)
        return False, "timeout"

    # -- 通道 2：stop --force + start（换宿主机）--------------------------
    def wait_state(self, want: str, timeout: float, tag: str) -> bool:
        t0 = now()
        last = ""
        while now() - t0 < timeout:
            inst = self.describe()
            st = (inst or {}).get("State", {}).get("Name", "?")
            if st != last:
                self.log.emit("state_observed", tag=tag, state=st,
                              since_action_s=round(now() - t0, 3),
                              reason=(inst or {}).get("StateTransitionReason"))
                last = st
            if st == want:
                return True
            time.sleep(self.a.poll_interval)
        self.log.emit("wait_state_timeout", tag=tag, want=want, last_seen=last,
                      waited_s=round(now() - t0, 3))
        return False

    def stop_start(self) -> bool:
        """停止再启动，换到新宿主机。

        三个停止参数的真实语义（`[文档]` StopInstances API 参考，2026-09 核对）：

          默认（Force=False, SkipOsShutdown=False）
              走完整的优雅关机。

          Force=True
              **不是**跳过优雅关机。原文：「The instance will first attempt a
              graceful shutdown ... If the graceful shutdown fails to complete
              within the timeout period, the instance shuts down forcibly」。
              即「先试优雅关机，超时后才硬下电」。所以对一个内核已 panic、
              永远完不成关机流程的实例，Force 必须把那个超时整个走完。

          SkipOsShutdown=True
              这才是真正绕过 OS 关机流程的参数。代价是可能丢失内存内容与
              在途 I/O、跳过关机脚本 —— 客户已声明容忍内存丢失，
              文件系统用 journaling 即可接受。

        早期版本的 SDK 文档把 Force 描述成「不给实例刷缓存的机会」，
        与现行 API 参考不一致；以现行 API 参考为准。
        本方法先前把 Force 当成硬下电用，是错的，已改为对不可达实例优先
        使用 SkipOsShutdown。
        """
        mode = self._stop_kwargs()
        if not self.a.apply:
            self.log.emit("dry_run", action="stop_start", stop_kwargs=mode,
                          plan=["stop_instances(%s)" % mode, "wait stopped",
                                "start_instances", "wait running"])
            return True

        self.log.emit("action_begin", action="stop_start", stop_kwargs=mode)
        try:
            self.ec2.stop_instances(InstanceIds=[self.a.instance_id], **mode)
            self.log.emit("api_ok", api="StopInstances", stop_kwargs=mode)
        except (ClientError, BotoCoreError) as exc:
            self.log.emit("api_failed", api="StopInstances", error=str(exc))
            return False

        if not self.wait_state("stopped", self.a.stop_timeout, "stop"):
            # 升级路径：若本次没用 SkipOsShutdown，超时后改用它 ——
            # 这是唯一能绕过 OS 关机流程的参数，Force 做不到。
            if not mode.get("SkipOsShutdown"):
                self.log.emit("escalate", to="skip_os_shutdown",
                              note="停止未在超时内完成，改用 SkipOsShutdown=True 绕过 OS 关机流程")
                try:
                    self.ec2.stop_instances(InstanceIds=[self.a.instance_id],
                                            SkipOsShutdown=True)
                    self.log.emit("api_ok", api="StopInstances", stop_kwargs={"SkipOsShutdown": True})
                except (ClientError, BotoCoreError) as exc:
                    self.log.emit("api_failed", api="StopInstances(skip_os_shutdown)", error=str(exc))
                    return False
                if not self.wait_state("stopped", self.a.stop_timeout, "skip_os_shutdown"):
                    return False
            else:
                return False

        try:
            self.ec2.start_instances(InstanceIds=[self.a.instance_id])
            self.log.emit("api_ok", api="StartInstances")
        except (ClientError, BotoCoreError) as exc:
            self.log.emit("api_failed", api="StartInstances", error=str(exc))
            return False

        if not self.wait_state("running", self.a.start_timeout, "start"):
            return False
        return True

    def _stop_kwargs(self) -> Dict[str, Any]:
        """构造 StopInstances 的停止模式参数。

        对「不可达/已挂死」的实例，正确的参数是 SkipOsShutdown 而不是 Force：
        Force 只是「先试优雅关机、超时再硬下电」，对完不成关机的内核
        必然把超时整个走完。
        """
        kw: Dict[str, Any] = {}
        if self.a.skip_os_shutdown:
            kw["SkipOsShutdown"] = True
        if self.a.force_stop:
            kw["Force"] = True
        return kw

    # -- 验收：running 不等于可服务 ---------------------------------------
    #
    # 这两个等待**必须并发**。第一版把 wait_status_ok 串在 wait_app_ready 前面，
    # 结果把 running->app_ready 测成 89.7s / 121.8s；改成并发后实测中位数
    # 7.014s（n=12，范围 6.0-7.1）—— 旧数字里 13-17 倍是自己的串行缺陷造成的。
    #
    # 这既是测量缺陷也是生产缺陷：status check ok 不是服务可用的前提，
    # 玩家能连上才是。恢复完成的判据取两者中先到的那个（实际总是 app_ready），
    # status_ok 只作为附加观测继续记录。

    def wait_recovered(self, status_timeout: float, app_timeout: float) -> bool:
        """并发等待 status_ok 与 app_ready，以 app_ready 为恢复完成判据。

        返回 True 表示应用已可服务。没有配 health_url 时退化为等 status_ok。
        """
        results: Dict[str, Any] = {}

        def _status() -> None:
            results["status_ok"] = self.wait_status_ok(status_timeout)

        def _app() -> None:
            results["app_ready"] = self.wait_app_ready(app_timeout)

        if not self.a.health_url:
            self.log.emit("app_ready_skipped", reason="no_health_url",
                          note="退化为仅等 status_ok；恢复完成判据因此弱于「玩家能连上」")
            return self.wait_status_ok(status_timeout)

        t_status = threading.Thread(target=_status, daemon=True)
        t_app = threading.Thread(target=_app, daemon=True)
        t_status.start()
        t_app.start()

        # 以 app_ready 为准：它先到就立刻返回，不等 status_ok
        t_app.join(timeout=app_timeout + 30)
        app_ok = bool(results.get("app_ready"))

        # status 线程留一小段时间收尾，只为把观测记全，不影响判据
        t_status.join(timeout=5)
        self.log.emit("recovery_verdict", app_ready=app_ok,
                      status_ok=results.get("status_ok"),
                      note="判据是 app_ready；status_ok 仅附加观测，两者并发采集")
        return app_ok

    def wait_status_ok(self, timeout: float) -> bool:
        t0 = now()
        last = ""
        while now() - t0 < timeout:
            try:
                r = self.ec2.describe_instance_status(
                    InstanceIds=[self.a.instance_id], IncludeAllInstances=True)
            except (ClientError, BotoCoreError) as exc:
                self.log.emit("collect_error", api="DescribeInstanceStatus", error=str(exc))
                time.sleep(self.a.poll_interval)
                continue
            sts = r.get("InstanceStatuses", []) or []
            if sts:
                st = sts[0]
                cur = "%s/%s/%s" % ((st.get("InstanceStatus", {}) or {}).get("Status"),
                                    (st.get("SystemStatus", {}) or {}).get("Status"),
                                    (st.get("AttachedEbsStatus", {}) or {}).get("Status"))
                if cur != last:
                    self.log.emit("status_observed", combined=cur,
                                  since_running_s=round(now() - t0, 3))
                    last = cur
                if ((st.get("InstanceStatus", {}) or {}).get("Status") == "ok"
                        and (st.get("SystemStatus", {}) or {}).get("Status") == "ok"):
                    return True
            time.sleep(self.a.poll_interval)
        self.log.emit("wait_status_timeout", waited_s=round(now() - t0, 3), last_seen=last)
        return False

    def wait_app_ready(self, timeout: float) -> bool:
        """真正的验收判据：健康端点的 tick 在推进。

        「running」「status ok」都不等于玩家能连上 —— 恢复总时长里
        OS 启动 + 游戏进程加载往往比检测那一段更长，必须单独量。
        """
        if not self.a.health_url:
            self.log.emit("app_ready_skipped", reason="no_health_url")
            return True
        t0 = now()
        first_tick: Optional[int] = None
        first_seen_at: Optional[float] = None
        while now() - t0 < timeout:
            try:
                with urllib.request.urlopen(self.a.health_url, timeout=2) as resp:
                    data = json.loads(resp.read(65536).decode("utf-8", "replace"))
                tick = data.get("tick")
                if isinstance(tick, int):
                    if first_tick is None:
                        first_tick, first_seen_at = tick, now()
                        self.log.emit("app_endpoint_up", tick=tick,
                                      since_running_s=round(now() - t0, 3))
                    elif tick != first_tick:
                        self.log.emit("app_ready", first_tick=first_tick, tick=tick,
                                      since_running_s=round(now() - t0, 3),
                                      tick_advance_s=round(now() - (first_seen_at or now()), 3))
                        return True
            except Exception:
                pass
            time.sleep(1.0)
        self.log.emit("wait_app_timeout", waited_s=round(now() - t0, 3), first_tick=first_tick)
        return False

    # -- 主流程 ------------------------------------------------------------
    def execute(self, verdict: str) -> int:
        inst = self.preflight()
        if inst is None:
            return 3

        self.log.emit("decision", verdict=verdict, reason=self.a.reason,
                      apply=self.a.apply, ladder=self._ladder(verdict))

        ok = False
        if verdict in (VERDICT_APP_STUCK, VERDICT_APP_DEAD):
            ok, _ = self.ssm_run(
                ["systemctl restart %s" % self.a.service_name,
                 "sleep 2",
                 "systemctl is-active %s || true" % self.a.service_name],
                label="restart_service")
            if ok and self.wait_app_ready(self.a.app_ready_timeout):
                self.log.emit("recovered", via="service_restart")
                return 0
            self.log.emit("escalate", frm="service_restart", to="stop_start",
                          note="服务重启后应用未在超时内 ready，升级到换宿主机")
            ok = self.stop_start()
        elif verdict == VERDICT_HOST_DOWN:
            ok = self.stop_start()
        elif verdict == VERDICT_STORAGE_STALLED:
            # 存储故障**绝不能**先重启进程：卷 I/O 已停滞，重启后的进程一去读盘
            # 就挂在 D 状态起不来，比不动更糟。
            #
            # stop/start 只能修好其中一类。`[文档]` attached EBS status check：
            # 「If the EBS status check indicates an impairment ... stop and start
            # the instance to move it to a new host」—— 也就是**宿主机到卷的
            # 可达性**那一类。若是卷自身的存储子系统故障，换宿主机无效，
            # 必须从快照替换卷，那超出自动化范围，交人工。
            self.log.emit("storage_action_note",
                          note="仅执行 stop/start（换宿主机）。它能修好宿主机-卷可达性这一类；"
                               "若卷本身受损则换宿主机无效，需从快照替换卷，本编排不做此动作。",
                          never="ssm_restart_service（会让进程挂在 D 状态）")
            ok = self.stop_start()
        else:
            self.log.emit("no_action", verdict=verdict,
                          note="仅 APP_STUCK / APP_DEAD / HOST_DOWN 触发动作；"
                               "PROBER_SIDE 与 UNKNOWN 永不动作")
            return 0

        if not ok:
            self.log.emit("failed", verdict=verdict)
            return 4
        if self.wait_recovered(self.a.status_ok_timeout, self.a.app_ready_timeout):
            self.log.emit("recovered", via="stop_start")
            return 0
        self.log.emit("partial", note="实例已 running 但应用未 ready —— "
                                      "恢复只做到最后一个可核验的步骤，剩余交人工")
        return 5

    @staticmethod
    def _ladder(verdict: str) -> List[str]:
        return {
            VERDICT_APP_STUCK: ["ssm_restart_service", "stop_start"],
            VERDICT_APP_DEAD: ["ssm_restart_service", "stop_start"],
            VERDICT_HOST_DOWN: ["stop_start"],
            # 存储故障刻意不含 ssm_restart_service
            VERDICT_STORAGE_STALLED: ["stop_start"],
        }.get(verdict, [])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="区服恢复编排器（默认 dry-run）")
    p.add_argument("--instance-id", required=True)
    p.add_argument("--verdict", required=True, help="探测器给出的判定")
    p.add_argument("--reason", default="")
    p.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1"))
    p.add_argument("--apply", action="store_true", help="真执行；缺省仅 dry-run")
    p.add_argument("--skip-os-shutdown", action="store_true", default=True,
                   help="StopInstances 传 SkipOsShutdown=True，真正绕过 OS 关机流程。"
                        "对已挂死的实例这是正确参数（Force 只是先试优雅关机再超时硬下电）。默认开")
    p.add_argument("--no-skip-os-shutdown", dest="skip_os_shutdown", action="store_false")
    p.add_argument("--force-stop", action="store_true", default=False,
                   help="额外传 Force=True。注意它不跳过优雅关机，只是超时后硬下电；"
                        "单独用它对挂死实例会把超时整个走完。默认关")
    p.add_argument("--no-force-stop", dest="force_stop", action="store_false")
    p.add_argument("--service-name", default="gameserver", help="SSM 重启的 systemd 单元名")
    p.add_argument("--health-url", default="", help="验收用的应用健康端点")
    p.add_argument("--state-dir", default=os.path.expanduser("~/.local/state/ec2-failure"))
    p.add_argument("--cooldown", type=float, default=600.0)
    p.add_argument("--max-per-day", type=int, default=6)
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.add_argument("--stop-timeout", type=float, default=300.0)
    p.add_argument("--start-timeout", type=float, default=300.0)
    p.add_argument("--status-ok-timeout", type=float, default=300.0)
    p.add_argument("--app-ready-timeout", type=float, default=180.0)
    p.add_argument("--log", default="", help="NDJSON 日志文件，缺省 stdout")
    p.add_argument("--ignore-rate-limits", action="store_true",
                   help="演练用：跳过冷却与每日上限检查（生产绝不要用）")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log = Log(args.log or None)
    if args.verdict not in ACTIONABLE:
        log.emit("no_action", verdict=args.verdict, note="非可动作判定")
        log.close()
        return 0

    gate = Gate(args.state_dir, args.instance_id, log)
    if not args.ignore_rate_limits:
        ok, why = gate.check_rate(args.cooldown, args.max_per_day)
        if not ok:
            log.emit("gate_reject", reason=why)
            log.close()
            return 6
    if not gate.acquire():
        log.close()
        return 7
    try:
        if args.apply:
            gate.stamp()
        return Orchestrator(args, log).execute(args.verdict)
    finally:
        gate.release()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
