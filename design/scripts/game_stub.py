#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game_stub.py —— 被测「区服」桩程序

作用是把探测器的判据变成可注入的：
  * 主循环持续自增 tick，模拟游戏服的帧循环
  * :8080/health 返回 {"tick", "loop_lag_ms", "players", "pid", ...}
  * :7777 一个裸 TCP 监听端口，模拟游戏客户端连接口

关键：health 端点由**另一个线程**提供，而 tick 由主循环推进。
这样对主循环发 SIGSTOP（或让它卡在锁上）时，HTTP 端点仍然回 200、
TCP 7777 仍然 accept，但 tick 冻住 —— 正是 TCP 探测看不见的那一类故障。
演练用例 D5 就靠这个证明「TCP 通不等于服务活着」。

  --stall-after N   跑 N 秒后主动冻住 tick（不用 SIGSTOP 也能复现）
  --lag-ms M        每帧额外睡 M 毫秒，制造 APP_SLOW

仅标准库。Python 3.9+。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    "tick": 0,
    "loop_lag_ms": 0.0,
    "players": 0,
    "pid": os.getpid(),
    "started_at": time.time(),
    "stalled": False,
    # 检查点（checkpoint）状态：D2 实测「根卷 I/O 停 3 分钟而 tick 照常推进」
    # 说明 tick 判据看不见存储故障。玩家在玩一局注定丢档的游戏，比服务不可用更糟。
    # 外部探针探不到这个，只能由游戏服在写路径上自检并暴露出来。
    #
    # 首选暴露**时间戳**而非「距今多少秒」：这是 Prometheus instrumentation
    # 的约定 —— 消费方用自己的时钟算差值，不受采集延迟影响，且原始时间戳
    # 能区分「一直没成功」与「刚刚成功过」。
    "last_checkpoint_success_timestamp": 0.0,
    # 兼容字段：已有实现若只暴露距今秒数，探测器也认。新实现不必提供。
    "last_successful_save_age_s": 0.0,
    "checkpoint_failing": False,
}
_LOCK = threading.Lock()


class HealthHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] not in ("/health", "/", "/healthz"):
            self.send_error(404, "not found")
            return
        with _LOCK:
            body = json.dumps(dict(STATE)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *a: object) -> None:
        return  # 探测器每秒一次，不刷日志


class TcpEcho(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            self.request.sendall(b"GAMESRV/1 tick=%d\n" % STATE["tick"])
        except Exception:
            pass


def serve_http(port: int) -> None:
    ThreadingHTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


def serve_tcp(port: int) -> None:
    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    S(("0.0.0.0", port), TcpEcho).serve_forever()


def do_checkpoint(path: str, fail: bool) -> bool:
    """执行一次检查点（把内存状态持久化）。真实游戏服这里是写存档或落库。

    两个关键点：

      1. **必须真的落盘并 fsync**，否则「成功」只是写进了页缓存，
         存储故障时会给出假的成功 —— 这正是要暴露的那类问题。
      2. 本函数只做**写侧**验证。更强的做法是金丝雀检查点：写一条带时间戳的
         哨兵记录后**再读回来校验内容**，才算验证了整条持久化链路。
         只读挂载、静默损坏、外置存储写成功但读失败这几类只有 read-back 抓得到。
         该增强尚未实现，见 design/04-terminology-and-prior-art.md 第二节。
    """
    if fail:
        return False
    try:
        with open(path, "wb") as fh:
            fh.write(b"SAVE " + str(int(time.time())).encode() + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="区服桩程序")
    ap.add_argument("--health-port", type=int, default=8080)
    ap.add_argument("--game-port", type=int, default=7777)
    ap.add_argument("--tick-hz", type=float, default=20.0)
    ap.add_argument("--lag-ms", type=float, default=0.0)
    ap.add_argument("--stall-after", type=float, default=0.0,
                    help="跑够这么多秒后冻住 tick（0 = 不冻）")
    ap.add_argument("--checkpoint-interval", type=float, default=5.0,
                    help="检查点间隔秒；健康端点暴露 last_checkpoint_success_timestamp")
    ap.add_argument("--checkpoint-path", default="/var/tmp/gameserver.checkpoint")
    ap.add_argument("--fail-checkpoints-after", type=float, default=0.0,
                    help="跑够这么多秒后所有检查点都失败（0 = 不注入）。"
                         "用来复现「tick 照常推进但进度全丢」这个最坏状态")
    a = ap.parse_args()

    threading.Thread(target=serve_http, args=(a.health_port,), daemon=True).start()
    threading.Thread(target=serve_tcp, args=(a.game_port,), daemon=True).start()
    sys.stderr.write("game_stub up pid=%d health=:%d game=:%d checkpoint=%s\n"
                     % (os.getpid(), a.health_port, a.game_port, a.checkpoint_path))
    sys.stderr.flush()

    period = 1.0 / max(0.1, a.tick_hz)
    t_start = time.monotonic()
    last_ckpt_ok_mono = time.monotonic()
    last_ckpt_ok_wall = time.time()
    next_ckpt = time.monotonic()
    while True:
        t0 = time.monotonic()
        elapsed = t0 - t_start

        # 检查点与 tick 是两条独立的失败轴，必须分开注入、分开暴露
        if t0 >= next_ckpt:
            next_ckpt = t0 + a.checkpoint_interval
            fail = bool(a.fail_checkpoints_after and elapsed >= a.fail_checkpoints_after)
            if do_checkpoint(a.checkpoint_path, fail):
                last_ckpt_ok_mono = t0
                last_ckpt_ok_wall = time.time()
            with _LOCK:
                STATE["checkpoint_failing"] = fail
        with _LOCK:
            # 时间戳是首选字段；滞后量用单调时钟算，免受墙钟跳变影响
            STATE["last_checkpoint_success_timestamp"] = round(last_ckpt_ok_wall, 3)
            STATE["last_successful_save_age_s"] = round(t0 - last_ckpt_ok_mono, 3)

        if a.stall_after and elapsed >= a.stall_after:
            with _LOCK:
                STATE["stalled"] = True
            # 冻住 tick 但不退出：HTTP 与 TCP 仍然可服务
            time.sleep(1.0)
            continue
        if a.lag_ms:
            time.sleep(a.lag_ms / 1000.0)
        with _LOCK:
            STATE["tick"] += 1
            STATE["loop_lag_ms"] = round((time.monotonic() - t0) * 1000.0, 3)
            STATE["players"] = 800 + (STATE["tick"] % 37)
        time.sleep(max(0.0, period - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
