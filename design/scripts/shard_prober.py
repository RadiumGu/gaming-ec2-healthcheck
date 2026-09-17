#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shard_prober.py —— 区服健康探测器（一台 EC2 = 一个区服）

设计要点（每一条都对应一个真实踩过的坑）：

1. 触发器只能是「应用内 tick 推进」，不能是 TCP connect。
   进程死锁 / GC / 存盘 stop-the-world 时，内核 accept queue 照样接住连接，
   TCP 探测一路绿灯 —— 这是 TCP 作为存活判据最大的假阴性。

2. ICMP 与 TCP 是分类器，不是触发器。
   ping 通 + 端口不通  -> 进程级问题，重启进程即可，不必换宿主机
   ping 不通 + 端口不通 -> 宿主机/网络问题，升级到 stop/start

3. 必须有 sentinel 对照，且对照本身要先证明是活的。
   对照目标在注入前就不可达时，「不可达 -> 不可达」什么也证明不了。
   本脚本启动时强制做一次阳性对照（--require-sentinel-up，默认开），
   sentinel 不可达就拒绝启动 —— 否则 sentinel 会永远失败，
   探测器把每一次真实故障都判成「探测侧问题」而抑制动作，
   整套自愈静默失效，而外观上一切正常。

4. 双档阈值。游戏服有存盘/GC 造成的秒级停顿，
   soft 只告警不动作，hard 才允许触发恢复动作。

5. 采集失败的返回值绝不能与合法测量值同形。
   本脚本每个 Probe 结果都带显式 ok 标记，
   下游判据一律排除 ok=False 的点，不做「看起来健康的默认值」。

输出：NDJSON 事件流（stdout 或 --out 文件），一行一个 JSON。
本脚本自己不执行任何恢复动作；判定结果交给 recovery_orchestrator.py。

仅依赖标准库。Python 3.9+。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Deque, Dict, List, Optional, Tuple

SCHEMA_VERSION = "prober/1"

# ---------------------------------------------------------------- 判定结果

VERDICT_HEALTHY = "HEALTHY"            # 一切正常
VERDICT_APP_SLOW = "APP_SLOW"          # tick 在推进但慢，soft 档
VERDICT_APP_STUCK = "APP_STUCK"        # 端口通、tick 不推进 -> 进程卡死
VERDICT_APP_DEAD = "APP_DEAD"          # ping 通、端口不通 -> 进程没了
VERDICT_HOST_DOWN = "HOST_DOWN"        # ping 与端口都不通、sentinel 正常 -> 宿主机/实例侧
VERDICT_STORAGE_STALLED = "STORAGE_STALLED"  # 卷 I/O 停滞，见下方说明
VERDICT_CHECKPOINT_LAGGING = "CHECKPOINT_LAGGING"  # 检查点滞后但 tick 照常，见下方说明
VERDICT_PROBER_SIDE = "PROBER_SIDE"    # sentinel 也失败 -> 探测侧或网络侧，抑制动作
VERDICT_UNKNOWN = "UNKNOWN"            # 判据不足，显式表达「测不出来」

# 允许触发恢复动作的判定（PROBER_SIDE / UNKNOWN 一律不动作）
#
# STORAGE_STALLED 在列，但它的动作阶梯与其它几档**不同：绝不要重启进程**。
# 重启解决不了存储故障，而且重启会让进程立刻去读盘、挂在 D 状态起不来，
# 比不动更糟。它只能走「换宿主机」或人工介入。
#
# CHECKPOINT_LAGGING **刻意不在列**。它表示 tick 还在推进、玩家还在玩，
# 但检查点（周期性把内存状态持久化）已经停止成功 —— 也就是当前实际 RPO
# 正在劣化。未落盘的进度正是此刻最危险的东西，
# 而**每一种自动恢复动作都会把它销毁**：重启进程丢内存、stop/start 丢内存。
# 所以对它唯一正确的自动行为是「不动作 + 告警 + 让运营决定是否停服务」，
# 由人来权衡「继续玩但可能丢档」与「主动踢下线保住已落盘进度」。
# 这是本方案里唯一一处「检测到了却刻意不自动处置」的判定。
ACTIONABLE = {VERDICT_APP_STUCK, VERDICT_APP_DEAD, VERDICT_HOST_DOWN,
              VERDICT_STORAGE_STALLED}


def now_wall() -> float:
    return time.time()


def now_mono() -> float:
    return time.monotonic()


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".%03dZ" % int((ts % 1) * 1000)


# ---------------------------------------------------------------- 探针结果


@dataclass
class ProbeResult:
    """单次探针结果。

    ok 语义严格区分三件事，不允许合并：
      ok=True,  up=True   探到了，目标正常
      ok=True,  up=False  探到了，目标不正常（这是有效测量）
      ok=False            没探成（工具缺失/自身异常），不是「目标不正常」
    """

    name: str
    ok: bool
    up: bool = False
    rtt_ms: Optional[float] = None
    detail: str = ""
    payload: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------- 各层探针


def probe_tcp(host: str, port: int, timeout: float) -> ProbeResult:
    t0 = now_mono()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return ProbeResult("tcp", ok=True, up=True, rtt_ms=(now_mono() - t0) * 1000.0)
    except socket.timeout:
        return ProbeResult("tcp", ok=True, up=False, detail="timeout")
    except ConnectionRefusedError:
        # 明确的 RST：端口没监听。与 timeout 语义不同，必须分开记 ——
        # refused 说明网络路径通、进程没了；timeout 说明路径本身可能断了。
        return ProbeResult("tcp", ok=True, up=False, detail="refused")
    except OSError as exc:
        return ProbeResult("tcp", ok=True, up=False, detail="oserror:%s" % exc.__class__.__name__)
    except Exception as exc:  # 探针自身异常，不是目标的问题
        return ProbeResult("tcp", ok=False, detail="probe_error:%s" % exc)
    finally:
        try:
            s.close()
        except Exception:
            pass


_PING_BIN = shutil.which("ping")


def probe_icmp(host: str, count: int, timeout: float) -> ProbeResult:
    """ICMP 探针。

    非 root 无法开 raw socket，所以走 ping 二进制。
    ping 不存在时返回 ok=False（能力缺失），绝不返回 up=False —— 后者会把
    「本机没装 ping」伪装成「目标不可达」，是最典型的假阳性来源。
    """
    if not _PING_BIN:
        return ProbeResult("icmp", ok=False, detail="ping_binary_missing")
    t0 = now_mono()
    try:
        proc = subprocess.run(
            [_PING_BIN, "-c", str(count), "-W", str(int(max(1, timeout))), "-q", host],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout * count + 3,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult("icmp", ok=True, up=False, detail="ping_wall_timeout")
    except Exception as exc:
        return ProbeResult("icmp", ok=False, detail="probe_error:%s" % exc)

    out = (proc.stdout or b"").decode("utf-8", "replace")
    rtt: Optional[float] = None
    for line in out.splitlines():
        if "rtt min/avg/max" in line and "=" in line:
            try:
                rtt = float(line.split("=")[1].strip().split("/")[1])
            except Exception:
                rtt = None
    up = proc.returncode == 0
    return ProbeResult(
        "icmp",
        ok=True,
        up=up,
        rtt_ms=rtt if rtt is not None else ((now_mono() - t0) * 1000.0 if up else None),
        detail="rc=%d" % proc.returncode,
    )


def probe_app(url: str, timeout: float) -> ProbeResult:
    """应用内健康端点。

    判据不是「HTTP 200」，而是响应体里的 tick 计数在推进。
    tick 由调用方在 Judge 里跨轮比较；这里只负责把它取回来。

    期望响应体（游戏服自己实现，见 game_stub.py）：
      {"tick": 123456, "loop_lag_ms": 3.2, "players": 812, "pid": 1234}
    """
    t0 = now_mono()
    req = urllib.request.Request(url, headers={"User-Agent": "shard-prober/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(64 * 1024)
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        return ProbeResult("app", ok=True, up=False, detail="http_%s" % exc.code)
    except socket.timeout:
        return ProbeResult("app", ok=True, up=False, detail="timeout")
    except urllib.error.URLError as exc:
        return ProbeResult("app", ok=True, up=False, detail="urlerror:%s" % exc.reason)
    except Exception as exc:
        return ProbeResult("app", ok=False, detail="probe_error:%s" % exc)

    rtt = (now_mono() - t0) * 1000.0
    if code != 200:
        return ProbeResult("app", ok=True, up=False, rtt_ms=rtt, detail="http_%s" % code)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        # 端点回了 200 但不是我们要的形状 —— 这是有效测量：应用不健康
        return ProbeResult("app", ok=True, up=False, rtt_ms=rtt, detail="body_not_json")
    if not isinstance(data, dict) or "tick" not in data:
        return ProbeResult("app", ok=True, up=False, rtt_ms=rtt, detail="no_tick_field")
    return ProbeResult("app", ok=True, up=True, rtt_ms=rtt, payload=data)


# ---------------------------------------------------------------- 存储旁路探针


class StorageSideChannel:
    """存储故障旁路探针（后台线程轮询 DescribeVolumeStatus）。

    为什么必须有它：D2 实测根卷 I/O 停 3 分钟，探测器 427 轮全 HEALTHY、
    tick 25408->33910 无回退 —— 游戏主循环全在内存里跑，页缓存够用，
    不需要新的磁盘读，所以 tick 判据对存储故障**完全是盲的**。
    这是整套方案里唯一一个 EC2 侧信号优于应用侧探针的场景。

    判据选择（D2 实测的延迟与有效性）：
      DescribeVolumeStatus 的 io-performance: stalled   +112.7 s  ← 用这个，最快
      DescribeInstanceStatus 的 AttachedEbsStatus       +155.7 s
      StatusCheckFailed_AttachedEBS 告警                +218.9 s
      VolumeStalledIOCheck                              零数据点，永不触发，不要用
      VolumeReadOps/WriteOps 高于阈值                   注入期归零，方向相反

    boto3 是可选依赖：探测器主体是纯 stdlib，没装 boto3 或没配凭证时
    本旁路显式降级为 unavailable 并说明原因，不静默跳过 —— 否则会退回
    「存储故障永远探不到」而外观正常。
    """

    # io-performance 的取值集合，**必须显式列举**而不是用「!= normal」判断。
    # 实测（D10）：gp2 卷返回 `not-applicable` —— 用 `!= "normal"` 会把它
    # 判成停滞，于是**每个 gp2 卷都会触发一次 stop/start**。这是真实误动作源。
    # 文档明写四档状态仅适用于 io1 / io2 / gp3。
    IOPERF_BAD = ("degraded", "severely-degraded", "stalled")
    IOPERF_OK = ("normal",)
    # 能力缺失或未知：既不是正常也不是故障，必须表达为「测不出来」
    IOPERF_NOT_JUDGEABLE = ("not-applicable", "insufficient-data")

    def __init__(self, volume_ids: List[str], region: str, interval: float,
                 sink: "EventSink") -> None:
        self.volume_ids = volume_ids
        self.region = region
        self.interval = interval
        self.sink = sink
        self.stop = False
        self._lock = threading.Lock()
        # None = 尚未取到 / 不可用；这与「取到了且正常」必须分开表达
        self._stalled: Optional[bool] = None
        self._detail: str = "not_started"
        self._client: Any = None
        self._thread: Optional[threading.Thread] = None

    def available(self) -> Tuple[bool, str]:
        if not self.volume_ids:
            return False, "no_volume_ids_configured"
        try:
            import boto3  # 可选依赖，延迟导入
            from botocore.config import Config as _Cfg
        except Exception as exc:
            return False, "boto3_unavailable:%s" % exc.__class__.__name__
        try:
            self._client = boto3.session.Session().client(
                "ec2", config=_Cfg(region_name=self.region,
                                   retries={"max_attempts": 2, "mode": "standard"},
                                   connect_timeout=3, read_timeout=8))
            # 起线程前先真调一次：证明凭证与权限可用，而不是等到故障时才发现
            r = self._client.describe_volume_status(VolumeIds=self.volume_ids)
        except Exception as exc:
            return False, "probe_call_failed:%s: %s" % (exc.__class__.__name__, exc)

        # 预检卷型能力：io-performance 仅 io1/io2/gp3 支持（`[文档]`），
        # gp2 返回 not-applicable（`[实测]` D10）。这个盲区必须在启动时说出来，
        # 不能等到真出故障时才发现这条旁路对该卷从来没生效过。
        blind = []
        for vs in r.get("VolumeStatuses", []) or []:
            st = None
            for d in (vs.get("VolumeStatus", {}) or {}).get("Details", []) or []:
                if d.get("Name") == "io-performance":
                    st = (d.get("Status") or "").lower()
            if st in self.IOPERF_NOT_JUDGEABLE or st is None:
                blind.append("%s:%s" % (vs.get("VolumeId"), st or "absent"))
        if blind and len(blind) == len(self.volume_ids):
            return False, ("io_performance_not_supported:%s "
                           "-- 该卷型不支持 io-performance 检查（仅 io1/io2/gp3 支持），"
                           "存储旁路对本区服无效，需换卷型或改用应用侧检查点滞后判据"
                           % ",".join(blind))
        if blind:
            return True, "ok(partial_blind:%s)" % ",".join(blind)
        return True, "ok"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self.stop:
            try:
                resp = self._client.describe_volume_status(VolumeIds=self.volume_ids)
                bad_vols: List[str] = []      # 确认劣化/停滞
                unjudgeable: List[str] = []   # 该卷无法判断（如 gp2）
                for vs in resp.get("VolumeStatuses", []) or []:
                    vid = vs.get("VolumeId")
                    vstat = vs.get("VolumeStatus", {}) or {}
                    ioperf = None
                    for d in vstat.get("Details", []) or []:
                        if d.get("Name") == "io-performance":
                            ioperf = (d.get("Status") or "").lower()
                    if ioperf in self.IOPERF_BAD:
                        bad_vols.append("%s:%s" % (vid, ioperf))
                    elif ioperf in self.IOPERF_NOT_JUDGEABLE or ioperf is None:
                        # gp2 等不支持该检查的卷型。绝不当作故障，也绝不当作正常
                        unjudgeable.append("%s:%s" % (vid, ioperf or "absent"))
                    # 卷整体 impaired 是独立的故障信号（io-enabled 失败），仍要算
                    if vstat.get("Status") == "impaired" \
                            and vid not in [x.split(":")[0] for x in bad_vols]:
                        bad_vols.append("%s:impaired" % vid)
                with self._lock:
                    prev = self._stalled
                    if bad_vols:
                        self._stalled = True
                        self._detail = ",".join(bad_vols)
                    elif unjudgeable and len(unjudgeable) == len(self.volume_ids):
                        # 配置的卷全都无法判断 -> 这条旁路对本区服等于不存在
                        self._stalled = None
                        self._detail = "not_judgeable:" + ",".join(unjudgeable)
                    else:
                        self._stalled = False
                        self._detail = "normal"
                        if unjudgeable:
                            self._detail += " (partial_blind:%s)" % ",".join(unjudgeable)
                if prev != self._stalled:
                    self.sink.emit("storage_transition", stalled=self._stalled,
                                   detail=self._detail, volumes=self.volume_ids)
            except Exception as exc:
                with self._lock:
                    # 采集失败绝不写成「正常」—— 置 None 表示测不出来
                    self._stalled = None
                    self._detail = "collect_error:%s" % exc.__class__.__name__
                self.sink.emit("storage_collect_error", error=str(exc)[:200])
            time.sleep(self.interval)

    def read(self) -> Tuple[Optional[bool], str]:
        with self._lock:
            return self._stalled, self._detail


# ---------------------------------------------------------------- 探测器自身存活


class Heartbeat:
    """把探测器自己的存活发到 CloudWatch，让「探测器死了」这件事可被发现。

    为什么这是最高优先级的缺口：sentinel 对照 + 双档阈值已经把**误动作**压住了，
    但没有任何机制看着探测器自己。探测器主机一死，所有区服**静默**失去监控，
    而监控面板上什么都不会变 —— 误判至少看得见，这个看不见。

    机制：每约 60 秒发一个 `ProberHeartbeat=1`，客户在其上建
    `TreatMissingData=breaching` 的告警，探测器停发即告警。
    同时发 `ShardUnhealthy`（0/1，取自探测器自己的判定），
    这样客户不必在 CloudWatch 侧重新推导健康与否。

    两个必须注意的坑（都踩过）：
      * `put_metric_data` 调用成功**不等于**指标在发布数据点。
        上线前必须 `get_metric_data` 实际取一次并数数据点个数。
      * 建在其上的告警必须**至少进入过一次 OK**，才证明它能取到数据完成评估。
        永远停在 `INSUFFICIENT_DATA` 的告警等于不存在；刚建时短暂
        `INSUFFICIENT_DATA` 是正常的，但要复查而不是假定它自己会好。

    boto3 是可选依赖：不可用时显式降级并说明后果，不静默跳过。
    """

    def __init__(self, namespace: str, region: str, shard_instance_id: str,
                 interval: float, sink: "EventSink") -> None:
        self.ns = namespace
        self.region = region
        self.shard = shard_instance_id or "unknown"
        self.host = socket.gethostname()
        self.interval = interval
        self.sink = sink
        self._client: Any = None
        self._next_send = 0.0
        self._sent = 0
        self._failed = 0

    def available(self) -> Tuple[bool, str]:
        if not self.ns:
            return False, "no_namespace_configured"
        try:
            import boto3
            from botocore.config import Config as _Cfg
        except Exception as exc:
            return False, "boto3_unavailable:%s" % exc.__class__.__name__
        try:
            self._client = boto3.session.Session().client(
                "cloudwatch", config=_Cfg(region_name=self.region,
                                          retries={"max_attempts": 2, "mode": "standard"},
                                          connect_timeout=3, read_timeout=8))
            # 起循环前先真发一次，证明凭证与权限可用
            self._put(1, None)
        except Exception as exc:
            return False, "put_failed:%s: %s" % (exc.__class__.__name__, exc)
        return True, "ok"

    def _dims(self) -> List[Dict[str, str]]:
        return [{"Name": "ShardInstanceId", "Value": self.shard},
                {"Name": "ProberHost", "Value": self.host}]

    def _put(self, alive: int, unhealthy: Optional[int]) -> None:
        data = [{"MetricName": "ProberHeartbeat", "Value": float(alive),
                 "Unit": "Count", "Dimensions": self._dims()}]
        if unhealthy is not None:
            data.append({"MetricName": "ShardUnhealthy", "Value": float(unhealthy),
                         "Unit": "Count", "Dimensions": self._dims()})
        self._client.put_metric_data(Namespace=self.ns, MetricData=data)

    def maybe_send(self, verdict: str) -> None:
        """按 interval 节流发送。放在主循环里调用，不额外起线程 ——
        这样心跳停发就真的意味着主循环停了，而不是只有心跳线程还活着。"""
        now_m = now_mono()
        if now_m < self._next_send:
            return
        self._next_send = now_m + self.interval
        unhealthy = 0 if verdict in (VERDICT_HEALTHY, VERDICT_APP_SLOW) else 1
        try:
            self._put(1, unhealthy)
            self._sent += 1
        except Exception as exc:
            self._failed += 1
            self.sink.emit("heartbeat_failed", error=str(exc)[:200],
                           sent=self._sent, failed=self._failed)

    def stats(self) -> Dict[str, Any]:
        return {"namespace": self.ns, "shard": self.shard, "prober_host": self.host,
                "sent": self._sent, "failed": self._failed}


# ---------------------------------------------------------------- 判据引擎


@dataclass
class Thresholds:
    # soft：只告警，不动作
    soft_consecutive: int = 3
    # hard：允许触发恢复动作
    hard_consecutive: int = 5
    # hard 还要求「失联已持续这么久」，防止一串快速失败立刻触发
    hard_min_seconds: float = 8.0
    # tick 停滞多少秒算卡死（要大于该区服的存盘/GC 停顿 P99.9）
    tick_stall_seconds: float = 6.0
    # 应用响应慢的软阈值
    app_slow_rtt_ms: float = 500.0
    # 检查点滞后上限秒（= 可接受的 RPO 暴露量）。0 = 关闭该判据。
    # 取值应为游戏服检查点周期的若干倍，从该区服自己的历史分布推导，不要拍整数。
    max_checkpoint_lag_s: float = 0.0


@dataclass
class RoundSnapshot:
    seq: int
    t_wall: float
    t_mono: float
    app: ProbeResult
    tcp: ProbeResult
    icmp: ProbeResult
    sentinel_icmp: ProbeResult
    sentinel_tcp: ProbeResult
    tick: Optional[int] = None
    # None = 旁路未启用或测不出来；True = 卷 I/O 停滞；False = 卷正常
    storage_stalled: Optional[bool] = None
    storage_detail: str = "disabled"
    # None = 游戏服没暴露相关字段，无法判断（不等于正常）
    checkpoint_lag_s: Optional[float] = None
    verdict: str = VERDICT_UNKNOWN
    reason: str = ""
    fail_streak: int = 0
    fail_since_mono: Optional[float] = None


class Judge:
    """把一轮探针结果折成一个判定。

    分类逻辑刻意写成显式的表，而不是一串 if 嵌套 ——
    每一支都要能对应到「该采取哪一级动作」，否则分类就没有意义。
    """

    def __init__(self, th: Thresholds) -> None:
        self.th = th
        self._last_tick: Optional[int] = None
        self._last_tick_change_mono: Optional[float] = None
        self._fail_streak = 0
        self._fail_since_mono: Optional[float] = None

    def _sentinel_healthy(self, s_icmp: ProbeResult, s_tcp: ProbeResult) -> Optional[bool]:
        """sentinel 是否正常。

        返回 None 表示「测不出来」（两个探针都 ok=False，例如本机没 ping
        且 sentinel 没开 TCP 端口）。测不出来时不能当成「sentinel 正常」，
        否则对照形同不存在。
        """
        votes = [p.up for p in (s_icmp, s_tcp) if p.ok]
        if not votes:
            return None
        return any(votes)

    def evaluate(self, snap: RoundSnapshot) -> RoundSnapshot:
        th = self.th
        app, tcp, icmp = snap.app, snap.tcp, snap.icmp

        # --- tick 推进判据 -------------------------------------------------
        tick: Optional[int] = None
        if app.ok and app.up and app.payload and isinstance(app.payload.get("tick"), int):
            tick = int(app.payload["tick"])
        snap.tick = tick

        tick_advancing: Optional[bool] = None
        if tick is not None:
            if self._last_tick is None:
                self._last_tick = tick
                self._last_tick_change_mono = snap.t_mono
                tick_advancing = True  # 首轮无从比较，按推进处理，由后续轮次纠正
            elif tick != self._last_tick:
                self._last_tick = tick
                self._last_tick_change_mono = snap.t_mono
                tick_advancing = True
            else:
                stalled_for = snap.t_mono - (self._last_tick_change_mono or snap.t_mono)
                tick_advancing = stalled_for < th.tick_stall_seconds

        # --- sentinel 对照 -------------------------------------------------
        sent = self._sentinel_healthy(snap.sentinel_icmp, snap.sentinel_tcp)

        # --- 分类 -----------------------------------------------------------
        app_reachable = app.ok and app.up
        tcp_up = tcp.ok and tcp.up
        icmp_measurable = icmp.ok
        icmp_up = icmp.ok and icmp.up

        # 存储判据必须**优先于**应用侧判据来判。
        # D2 实测：根卷 I/O 停 3 分钟，应用侧 427 轮全 HEALTHY、tick 无回退。
        # 如果先看应用侧，这一支永远进不来 —— 那正是 tick 判据的盲区。
        if snap.storage_stalled is True:
            snap.verdict = VERDICT_STORAGE_STALLED
            snap.reason = "volume io stalled (%s); app-side may still look healthy" % snap.storage_detail
            self._bump_streak(snap)
            return snap

        # 检查点滞后判据：排在存储判据之后、应用判据之前。
        # 它抓的是「tick 照常推进但进度全丢」这个最坏状态 —— 应用活着、
        # 玩家在玩，但玩的是一局注定丢档的游戏。此刻的滞后量就是**当前实际 RPO**。
        # 只有游戏服自己能报这个，外部探针无从得知，所以字段缺失时
        # 显式记为「无法判断」而不是当作正常。
        #
        # 兼容两种字段形态，优先用时间戳（Prometheus instrumentation 约定：
        # 暴露「上次成功的时间戳」，由消费方自己算差值，不受采集延迟影响）：
        #   last_checkpoint_success_timestamp  Unix 秒，首选
        #   last_successful_save_age_s         已经算好的距今秒数，兼容
        if th.max_checkpoint_lag_s > 0 and app.ok and app.up and app.payload:
            lag: Optional[float] = None
            ts = app.payload.get("last_checkpoint_success_timestamp")
            if isinstance(ts, (int, float)) and ts > 0:
                lag = max(0.0, snap.t_wall - float(ts))
            else:
                aged = app.payload.get("last_successful_save_age_s")
                if isinstance(aged, (int, float)):
                    lag = float(aged)
            snap.checkpoint_lag_s = lag
            if lag is not None and lag > th.max_checkpoint_lag_s:
                snap.verdict = VERDICT_CHECKPOINT_LAGGING
                snap.reason = ("checkpoint lag %.1fs > %.1fs (current RPO exposure); "
                               "tick still advancing -- players are playing a session "
                               "that will be lost. NO automatic action: every action "
                               "destroys the unsaved state."
                               % (lag, th.max_checkpoint_lag_s))
                self._bump_streak(snap)
                return snap

        if app_reachable and tick_advancing:
            rtt = app.rtt_ms or 0.0
            if rtt > th.app_slow_rtt_ms:
                verdict, reason = VERDICT_APP_SLOW, "tick advancing, app rtt %.0fms > %.0fms" % (
                    rtt, th.app_slow_rtt_ms)
            else:
                verdict, reason = VERDICT_HEALTHY, "tick advancing"
        elif app_reachable and tick_advancing is False:
            # 端点还能回 200，但 tick 冻住了 —— 正是 TCP 探测看不见的那一类
            verdict, reason = VERDICT_APP_STUCK, "endpoint responds but tick frozen for >= %.1fs" % (
                th.tick_stall_seconds)
        elif tcp_up and not app_reachable:
            # 端口通、健康端点不通：进程还在但内部坏了，或健康端点自己挂了
            verdict, reason = VERDICT_APP_STUCK, "tcp open but health endpoint %s" % (app.detail or "down")
        elif (not tcp_up) and icmp_up:
            verdict, reason = VERDICT_APP_DEAD, "icmp up, game port %s" % (tcp.detail or "down")
        elif (not tcp_up) and icmp_measurable and not icmp_up:
            if sent is True:
                verdict, reason = VERDICT_HOST_DOWN, "icmp+tcp down, sentinel healthy"
            elif sent is False:
                verdict, reason = VERDICT_PROBER_SIDE, "icmp+tcp down AND sentinel also down"
            else:
                verdict, reason = VERDICT_UNKNOWN, "icmp+tcp down but sentinel unmeasurable"
        elif not tcp_up and not icmp_measurable:
            # 没有 ICMP 能力，只能靠 TCP + sentinel
            if sent is True:
                verdict, reason = VERDICT_HOST_DOWN, "tcp down, no icmp capability, sentinel healthy"
            elif sent is False:
                verdict, reason = VERDICT_PROBER_SIDE, "tcp down AND sentinel also down (no icmp)"
            else:
                verdict, reason = VERDICT_UNKNOWN, "tcp down, neither icmp nor sentinel measurable"
        else:
            verdict, reason = VERDICT_UNKNOWN, "unclassified: app=%s tcp=%s icmp=%s" % (
                app.detail, tcp.detail, icmp.detail)

        snap.verdict = verdict
        snap.reason = reason
        self._bump_streak(snap)
        return snap

    def _bump_streak(self, snap: RoundSnapshot) -> None:
        """更新连续失败计数。存储支路与常规支路共用，避免两处各写一份而漂移。"""
        if snap.verdict in (VERDICT_HEALTHY, VERDICT_APP_SLOW):
            self._fail_streak = 0
            self._fail_since_mono = None
        else:
            if self._fail_streak == 0:
                self._fail_since_mono = snap.t_mono
            self._fail_streak += 1
        snap.fail_streak = self._fail_streak
        snap.fail_since_mono = self._fail_since_mono

    def escalation(self, snap: RoundSnapshot) -> str:
        """返回 none / soft / hard。

        hard 需要同时满足「连续次数」和「已持续时长」两个条件 ——
        只用次数会让一串 50ms 内连续失败的探针立刻触发一次区服重启。
        """
        if snap.verdict not in ACTIONABLE:
            # PROBER_SIDE / UNKNOWN 永远不升级 —— 这是抑制误动作的最后一道闸
            return "soft" if snap.fail_streak >= self.th.soft_consecutive else "none"
        held = (snap.t_mono - snap.fail_since_mono) if snap.fail_since_mono else 0.0
        if snap.fail_streak >= self.th.hard_consecutive and held >= self.th.hard_min_seconds:
            return "hard"
        if snap.fail_streak >= self.th.soft_consecutive:
            return "soft"
        return "none"


# ---------------------------------------------------------------- 事件输出


class EventSink:
    def __init__(self, path: Optional[str]) -> None:
        self._fh = open(path, "a", encoding="utf-8") if path else sys.stdout
        self._own = bool(path)

    def emit(self, kind: str, **fields: Any) -> None:
        rec = {"schema": SCHEMA_VERSION, "kind": kind, "t": iso(now_wall()), "ts": round(now_wall(), 3)}
        rec.update(fields)
        self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._own:
            try:
                self._fh.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 主循环


class Prober:
    def __init__(self, args: argparse.Namespace, sink: EventSink) -> None:
        self.a = args
        self.sink = sink
        self.th = Thresholds(
            soft_consecutive=args.soft_consecutive,
            hard_consecutive=args.hard_consecutive,
            hard_min_seconds=args.hard_min_seconds,
            tick_stall_seconds=args.tick_stall_seconds,
            app_slow_rtt_ms=args.app_slow_rtt_ms,
            max_checkpoint_lag_s=args.max_checkpoint_lag_s,
        )
        self.judge = Judge(self.th)
        self.seq = 0
        self.stop = False
        self.history: Deque[RoundSnapshot] = deque(maxlen=600)
        self._last_escalation_emitted = ""
        self._hard_fired_at_mono: Optional[float] = None
        # 存储旁路：D2 证明应用侧 tick 判据对存储故障全盲，必须独立采集
        # 探测器自身存活：没有它，探测器主机一死所有区服静默失去监控
        self.hb: Optional[Heartbeat] = None
        if args.heartbeat_namespace:
            self.hb = Heartbeat(namespace=args.heartbeat_namespace, region=args.region,
                                shard_instance_id=args.instance_id,
                                interval=args.heartbeat_interval, sink=sink)
        self.storage: Optional[StorageSideChannel] = None
        if args.volume_ids:
            self.storage = StorageSideChannel(
                volume_ids=list(args.volume_ids), region=args.region,
                interval=args.storage_interval, sink=sink)

    # -- 阳性对照：启动前必须证明 sentinel 是活的 --------------------------
    def preflight_sentinel(self) -> bool:
        s_icmp = probe_icmp(self.a.sentinel_host, 2, self.a.icmp_timeout) if self.a.sentinel_host else \
            ProbeResult("icmp", ok=False, detail="no_sentinel_configured")
        s_tcp = probe_tcp(self.a.sentinel_host, self.a.sentinel_port, self.a.tcp_timeout) \
            if (self.a.sentinel_host and self.a.sentinel_port) else \
            ProbeResult("tcp", ok=False, detail="no_sentinel_port_configured")
        healthy = any(p.up for p in (s_icmp, s_tcp) if p.ok)
        self.sink.emit(
            "preflight_sentinel",
            sentinel_host=self.a.sentinel_host,
            sentinel_port=self.a.sentinel_port,
            icmp=asdict(s_icmp),
            tcp=asdict(s_tcp),
            healthy=healthy,
            ping_binary=_PING_BIN or None,
        )
        return healthy

    def one_round(self) -> RoundSnapshot:
        self.seq += 1
        t_wall, t_mono = now_wall(), now_mono()
        app = probe_app(self.a.health_url, self.a.app_timeout)
        tcp = probe_tcp(self.a.target_host, self.a.game_port, self.a.tcp_timeout)
        icmp = probe_icmp(self.a.target_host, self.a.icmp_count, self.a.icmp_timeout)

        # sentinel 只在被测目标出问题时才探 —— 正常时每轮都探 sentinel
        # 是纯浪费，且会给一台别人的实例带来无意义流量。
        need_sentinel = not ((app.ok and app.up) and (tcp.ok and tcp.up))
        if need_sentinel and self.a.sentinel_host:
            s_icmp = probe_icmp(self.a.sentinel_host, self.a.icmp_count, self.a.icmp_timeout)
            s_tcp = probe_tcp(self.a.sentinel_host, self.a.sentinel_port, self.a.tcp_timeout)
        else:
            s_icmp = ProbeResult("icmp", ok=False, detail="not_probed_target_healthy")
            s_tcp = ProbeResult("tcp", ok=False, detail="not_probed_target_healthy")

        snap = RoundSnapshot(
            seq=self.seq, t_wall=t_wall, t_mono=t_mono,
            app=app, tcp=tcp, icmp=icmp,
            sentinel_icmp=s_icmp, sentinel_tcp=s_tcp,
        )
        if self.storage is not None:
            snap.storage_stalled, snap.storage_detail = self.storage.read()
        return self.judge.evaluate(snap)

    def run(self) -> int:
        if self.a.require_sentinel_up:
            if not self.preflight_sentinel():
                self.sink.emit(
                    "fatal",
                    error="sentinel_not_reachable",
                    hint="sentinel 在注入前就不可达。对照是死的：一旦上线，每次真实故障都会被"
                         "判成 PROBER_SIDE 而抑制动作，自愈静默失效。先修 sentinel 可达性"
                         "（安全组入站 / ICMP），或用 --no-require-sentinel-up 明确接受无对照运行。",
                )
                return 3

        # 心跳可用性自检：不可用要说清后果，不静默跳过
        if self.hb is not None:
            ok, why = self.hb.available()
            self.sink.emit("preflight_heartbeat", available=ok, detail=why,
                           **self.hb.stats(),
                           note="不可用时「探测器自己死了」无法被发现；"
                                "可用时请在 ProberHeartbeat 上建 TreatMissingData=breaching 告警，"
                                "并确认该告警至少进入过一次 OK")
            if not ok:
                self.hb = None
        else:
            self.sink.emit("preflight_heartbeat", available=False, detail="not_configured",
                           note="未配 --heartbeat-namespace：探测器进程死亡将无人知晓")

        # 存储旁路的可用性自检：不可用就显式说明原因，绝不静默跳过 ——
        # 静默跳过会退回「存储故障永远探不到」而外观一切正常。
        if self.storage is not None:
            ok, why = self.storage.available()
            self.sink.emit("preflight_storage", volume_ids=self.a.volume_ids,
                           available=ok, detail=why,
                           note="不可用时存储故障（D2 实测应用侧全盲）将无法被探到")
            if ok:
                self.storage.start()
            elif self.a.require_storage:
                self.sink.emit("fatal", error="storage_side_channel_unavailable", detail=why,
                               hint="配了 --volume-ids 且 --require-storage 时旁路必须可用。"
                                    "装 boto3 并确保有 ec2:DescribeVolumeStatus 权限，"
                                    "或去掉 --require-storage 明确接受存储故障探不到。")
                return 3
        else:
            self.sink.emit("preflight_storage", available=False,
                           detail="not_configured",
                           note="未配 --volume-ids：存储故障探不到（D2 实测应用侧 tick 判据全盲）")

        self.sink.emit(
            "start",
            target_host=self.a.target_host, game_port=self.a.game_port,
            health_url=self.a.health_url, sentinel_host=self.a.sentinel_host,
            interval=self.a.interval, thresholds=asdict(self.th),
            instance_id=self.a.instance_id, volume_ids=self.a.volume_ids,
        )

        next_at = now_mono()
        while not self.stop:
            snap = self.one_round()
            self.history.append(snap)
            esc = self.judge.escalation(snap)

            # 每轮都写一条 round 记录：取证靠的是完整序列，不是只记异常。
            self.sink.emit(
                "round",
                seq=snap.seq, verdict=snap.verdict, reason=snap.reason,
                escalation=esc, tick=snap.tick, fail_streak=snap.fail_streak,
                storage_stalled=snap.storage_stalled, storage_detail=snap.storage_detail,
                checkpoint_lag_s=snap.checkpoint_lag_s,
                app=asdict(snap.app), tcp=asdict(snap.tcp), icmp=asdict(snap.icmp),
                sentinel_icmp=asdict(snap.sentinel_icmp), sentinel_tcp=asdict(snap.sentinel_tcp),
            )

            if self.hb is not None:
                self.hb.maybe_send(snap.verdict)

            if esc != self._last_escalation_emitted:
                self.sink.emit("escalation_change", frm=self._last_escalation_emitted or "none",
                               to=esc, verdict=snap.verdict, reason=snap.reason, seq=snap.seq)
                self._last_escalation_emitted = esc

            if esc == "hard":
                cooled = (self._hard_fired_at_mono is None or
                          (snap.t_mono - self._hard_fired_at_mono) >= self.a.action_cooldown)
                if cooled:
                    self._hard_fired_at_mono = snap.t_mono
                    self.sink.emit(
                        "action_request",
                        verdict=snap.verdict, reason=snap.reason, seq=snap.seq,
                        instance_id=self.a.instance_id,
                        fail_streak=snap.fail_streak,
                        held_seconds=round(snap.t_mono - (snap.fail_since_mono or snap.t_mono), 3),
                    )
                    if self.a.on_hard:
                        self._invoke_orchestrator(snap)
                else:
                    self.sink.emit("action_suppressed", reason="cooldown", seq=snap.seq,
                                   cooldown=self.a.action_cooldown)

            if self.a.max_rounds and self.seq >= self.a.max_rounds:
                self.sink.emit("stop", reason="max_rounds", rounds=self.seq,
                               heartbeat=self.hb.stats() if self.hb else None)
                break

            next_at += self.a.interval
            sleep_for = next_at - now_mono()
            if sleep_for < 0:
                # 追不上节拍就重置，不累积漂移
                next_at = now_mono()
                sleep_for = 0
            time.sleep(sleep_for)
        return 0

    def _invoke_orchestrator(self, snap: RoundSnapshot) -> None:
        cmd = self.a.on_hard.split() + [
            "--verdict", snap.verdict,
            "--instance-id", self.a.instance_id or "",
            "--reason", snap.reason,
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
            self.sink.emit("orchestrator_invoked", cmd=cmd, rc=proc.returncode,
                           output=(proc.stdout or b"").decode("utf-8", "replace")[-4000:])
        except Exception as exc:
            self.sink.emit("orchestrator_error", cmd=cmd, error=str(exc))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="区服健康探测器（分层探针 + sentinel 对照）")
    p.add_argument("--target-host", required=True, help="被测区服的私有 IP")
    p.add_argument("--game-port", type=int, default=7777, help="游戏服 TCP 端口")
    p.add_argument("--health-url", required=True, help="应用内健康端点，返回带 tick 的 JSON")
    p.add_argument("--instance-id", default="", help="被测实例 InstanceId，交给编排器用")

    p.add_argument("--sentinel-host", default="", help="同 AZ 同子网的对照实例 IP")
    p.add_argument("--sentinel-port", type=int, default=22, help="对照实例上必然开放的 TCP 端口")
    p.add_argument("--require-sentinel-up", dest="require_sentinel_up",
                   action="store_true", default=True,
                   help="启动时强制阳性对照，sentinel 不可达就拒绝启动（默认开）")
    p.add_argument("--no-require-sentinel-up", dest="require_sentinel_up", action="store_false",
                   help="明确接受无对照运行（会失去误动作抑制）")

    p.add_argument("--interval", type=float, default=1.0, help="探测间隔秒")
    p.add_argument("--app-timeout", type=float, default=1.0)
    p.add_argument("--tcp-timeout", type=float, default=1.0)
    p.add_argument("--icmp-timeout", type=float, default=1.0)
    p.add_argument("--icmp-count", type=int, default=1)

    p.add_argument("--soft-consecutive", type=int, default=3)
    p.add_argument("--hard-consecutive", type=int, default=5)
    p.add_argument("--hard-min-seconds", type=float, default=8.0)
    p.add_argument("--tick-stall-seconds", type=float, default=6.0)
    p.add_argument("--app-slow-rtt-ms", type=float, default=500.0)
    p.add_argument("--max-checkpoint-lag-s", type=float, default=0.0,
                   help="检查点滞后上限秒（= 可接受的 RPO 暴露量）。健康端点的 "
                        "last_checkpoint_success_timestamp（首选）或 "
                        "last_successful_save_age_s（兼容）换算出的滞后超过此值即判 "
                        "CHECKPOINT_LAGGING。0 = 关闭。该判定刻意不触发任何自动动作 —— "
                        "重启与 stop/start 都会销毁未落盘的进度，只能告警交人工决定")

    p.add_argument("--volume-ids", nargs="*", default=[],
                   help="被测实例的 EBS 卷 ID。配了才启用存储故障旁路探测 —— "
                        "D2 实测应用侧 tick 判据对存储故障完全是盲的")
    p.add_argument("--storage-interval", type=float, default=5.0,
                   help="存储旁路轮询间隔秒（DescribeVolumeStatus）")
    p.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1"))
    p.add_argument("--require-storage", action="store_true",
                   help="配了 --volume-ids 时要求旁路必须可用，否则拒绝启动")

    p.add_argument("--heartbeat-namespace", default="",
                   help="配上即向该 CloudWatch namespace 发 ProberHeartbeat 与 ShardUnhealthy，"
                        "使探测器进程死亡可被 TreatMissingData=breaching 的告警发现。"
                        "不配则探测器自己死了无人知晓")
    p.add_argument("--heartbeat-interval", type=float, default=60.0,
                   help="心跳发送间隔秒（默认 60，控自定义指标成本）")

    p.add_argument("--action-cooldown", type=float, default=600.0,
                   help="两次动作请求之间的最小间隔秒，防止把一次故障放大成反复重启")
    p.add_argument("--on-hard", default="", help="hard 档时调用的命令（编排器），会追加 --verdict/--instance-id/--reason")
    p.add_argument("--out", default="", help="NDJSON 输出文件，缺省写 stdout")
    p.add_argument("--max-rounds", type=int, default=0, help="跑够多少轮退出，0 = 不限")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    sink = EventSink(args.out or None)
    prober = Prober(args, sink)

    def _sig(_signum: int, _frame: Any) -> None:
        prober.stop = True
        sink.emit("stop", reason="signal")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    try:
        return prober.run()
    finally:
        sink.close()


if __name__ == "__main__":
    sys.exit(main())
