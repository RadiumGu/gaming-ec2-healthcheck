#!/usr/bin/env python3
"""游戏服健康端点参考实现 —— 可直接抄。

这是给客户应用团队的最小可用实现。用 Python 写是为了能直接跑起来验证，
但**每一条约束都与语言无关**，用 C++ / Go / C# 实现时同样成立。

────────────────────────────────────────────────────────────────
六条必须遵守的约束（每一条都对应一个真实故障场景）
────────────────────────────────────────────────────────────────

1. `tick` 只能由**主循环自己**自增。
   交给定时器或独立线程维护，判据当场失效 —— 那个线程活着不代表游戏在跑。

2. 健康端点跑在**独立线程**，不能阻塞主循环。
   但它只**读** `tick`，绝不参与推进。

3. 健康端点**绝不做磁盘 I/O**。
   这条最容易被忽略，后果最隐蔽：存储停滞时，若端点要读盘才能应答，
   它会一起挂住 → 外部探测器判成「应用死了」→ 动作阶梯执行重启进程 →
   重启后的进程一去读盘就卡在不可中断等待里，起不来。
   端点必须只读内存里的计数与时间戳，这样存储停滞时它仍能回答
   「我还在应答，但 tick 冻住了 / 检查点在超时」，探测器才能分类正确。

4. 检查点必须真的 `fsync`。
   只写进页缓存的话，「检查点成功」在存储故障时会给出假的成功。

5. 检查点失败时**绝不更新时间戳**。
   时间戳的语义是「上次**成功**落盘的时刻」。失败还更新，
   等于把「一直在丢档」伪装成健康。

6. `tick` 单调递增，**不重置、不回绕**。
   探测器判的是「变了没」，重置也算变，会掩盖一次进程重启。

────────────────────────────────────────────────────────────────
关于并发读取
────────────────────────────────────────────────────────────────

健康端点读 `tick` 时可能读到「上一轮的旧值」，这**完全没问题** ——
探测器要的是「跨轮次有没有变化」，不是某一瞬间的精确值。
所以热路径上不需要加锁。

各语言的做法：
  * Python：GIL 保证 int 赋值原子，直接读即可
  * C++：`std::atomic<uint64_t>`，`memory_order_relaxed` 足够
  * Go：`atomic.AddUint64` / `atomic.LoadUint64`
  * C#：`Interlocked.Increment`，或 `volatile long`

**不要为此引入互斥锁**：主循环是热路径，锁竞争的代价远大于收益，
而且一旦健康端点持锁期间被调度出去，反而会拖慢主循环。
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ───────────────────────────────────────────── 对外暴露的状态
# 只有主循环写，健康端点只读。故意用最简单的容器，不加锁（见文件头说明）。


class GameState:
    def __init__(self):
        # 约束 1、6：只由主循环自增，单调递增
        self.tick = 0
        # 约束 5：只在检查点**成功**时更新
        self.last_checkpoint_success_timestamp = 0.0
        # 可选：便于运营判断影响面
        self.players = 0
        # 可选：主循环单圈耗时，用于「慢」告警（区别于「死」）
        self.loop_lag_ms = 0.0


STATE = GameState()


# ───────────────────────────────────────────── 健康端点


class HealthHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path not in ("/health", "/healthz", "/"):
            self.send_error(404)
            return

        # 约束 3：只读内存，绝不碰磁盘。
        # 也不要在这里做「检查存档文件是否可读」之类的自检 ——
        # 那会让存储故障伪装成应用故障。
        body = json.dumps({
            "tick": STATE.tick,
            "last_checkpoint_success_timestamp":
                round(STATE.last_checkpoint_success_timestamp, 3),
            "players": STATE.players,
            "loop_lag_ms": round(STATE.loop_lag_ms, 3),
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # 不缓存：中间层缓存会让探测器一直看到同一个 tick，
        # 把健康的服务判成卡死
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # 关掉访问日志：1 秒一次的探测会把日志刷爆
    def log_message(self, *args):
        pass


def start_health_server(port: int) -> None:
    """约束 2：独立线程，绝不阻塞主循环。"""
    srv = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()


# ───────────────────────────────────────────── 检查点


def do_checkpoint(path: str) -> bool:
    """把世界状态持久化。返回是否成功。

    约束 4：必须真的 fsync，否则存储故障时会给出假的成功。
    写临时文件再 rename，保证不会出现「写了一半的存档」。
    """
    tmp = path + ".tmp"
    try:
        payload = json.dumps({
            "tick": STATE.tick,
            "players": STATE.players,
            "saved_at": time.time(),
        }).encode("utf-8")

        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())          # ← 关键：真的落到盘上

        os.replace(tmp, path)              # 原子替换

        # 目录也要 fsync，否则 rename 本身可能未落盘
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return True
    except Exception:
        # 约束 5：失败就是失败，不更新时间戳，让滞后量自己涨上去
        return False


# ───────────────────────────────────────────── 主循环


def main_loop(tick_hz: float = 30.0,
              checkpoint_interval_s: float = 5.0,
              checkpoint_path: str = "/var/lib/gameserver/world.checkpoint") -> None:
    period = 1.0 / tick_hz
    next_checkpoint = time.monotonic()

    while True:
        t0 = time.monotonic()

        # ── 你原本的游戏逻辑放这里 ──
        # process_input()
        # update_world()
        # resolve_combat()
        STATE.players = 800          # 示意

        # 约束 1：只有这里自增。约束 6：只加不减。
        STATE.tick += 1
        STATE.loop_lag_ms = (time.monotonic() - t0) * 1000.0

        # 检查点与 tick 是两条独立的失败轴：
        # 检查点全失败时 tick 照常推进（玩家在玩注定丢档的局），
        # 这正是要能分别观测的状态。
        if t0 >= next_checkpoint:
            next_checkpoint = t0 + checkpoint_interval_s
            if do_checkpoint(checkpoint_path):
                STATE.last_checkpoint_success_timestamp = time.time()

        time.sleep(max(0.0, period - (time.monotonic() - t0)))


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="健康端点参考实现")
    ap.add_argument("--health-port", type=int, default=8080)
    ap.add_argument("--tick-hz", type=float, default=30.0)
    ap.add_argument("--checkpoint-interval", type=float, default=5.0)
    ap.add_argument("--checkpoint-path", default="/tmp/world.checkpoint")
    a = ap.parse_args()

    start_health_server(a.health_port)
    # 启动时先做一次检查点，让时间戳不是 0 ——
    # 否则探测器一上线就会看到一个「无穷大的滞后量」
    if do_checkpoint(a.checkpoint_path):
        STATE.last_checkpoint_success_timestamp = time.time()
    main_loop(a.tick_hz, a.checkpoint_interval, a.checkpoint_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
