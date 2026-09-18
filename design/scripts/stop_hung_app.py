#!/usr/bin/env python3
"""D11：补测第三档 —— OS 存活但应用冻死时的停止耗时。

为什么必须补这一档
────────────────────────────────────────────────────────────
D8 只测了两个极端：
  * healthy   —— 应用响应 SIGTERM，正常退出，关机很快
  * panicked  —— 内核已死，关机流程永远完不成

而动作阶梯里最常走的 `APP_STUCK` / `APP_DEAD` 落在**中间**：
OS 完全正常，但游戏进程冻住、不响应 SIGTERM。
这时 systemd 要等满 `TimeoutStopSec`（AL2023 默认 **90 秒**）才 SIGKILL。

这一档决定了 `--skip-os-shutdown` 到底该不该默认开：
  * 若普通 stop 在这档也很快 → 默认关更好（保留最后一次检查点的机会）
  * 若普通 stop 在这档要多花一个 90 秒超时 → 默认开是对的

D8 的「健康实例四模式无法区分」不能外推到这一档 ——
那组的应用是**响应** SIGTERM 的。

方法
────────────────────────────────────────────────────────────
装一个 systemd 服务，其主进程用 `trap '' TERM` 显式忽略 SIGTERM，
模拟「进程活着但不肯退出」。然后对同一台实例反复测：
  plain    stop_instances()                    → 走完整 OS 关机
  skip_os  stop_instances(SkipOsShutdown=True) → 绕过 OS 关机

每次测完 start 回来，重新拉起服务，再测下一次。
"""

import argparse
import json
import sys
import time

import boto3
from botocore.config import Config

REGION = "ap-northeast-1"

# 忽略 SIGTERM 的服务：模拟冻死的游戏进程。
# `trap '' TERM` 让 shell 忽略信号；systemd 只能等满超时后 SIGKILL。
UNIT = r"""[Unit]
Description=hung game server (ignores SIGTERM)
[Service]
Type=simple
ExecStart=/bin/bash -c 'trap "" TERM; while true; do sleep 1; done'
Restart=no
[Install]
WantedBy=multi-user.target
"""


class Runner:
    def __init__(self, iid, sink):
        cfg = Config(region_name=REGION,
                     retries={"max_attempts": 5, "mode": "standard"})
        sess = boto3.session.Session()
        self.ec2 = sess.client("ec2", config=cfg)
        self.ssm = sess.client("ssm", config=cfg)
        self.iid = iid
        self.sink = sink

    def state(self):
        r = self.ec2.describe_instances(InstanceIds=[self.iid])
        return r["Reservations"][0]["Instances"][0]["State"]["Name"]

    def wait_state(self, want, timeout=420):
        """1 秒粒度轮询，返回首次达到目标状态的耗时。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = self.state()
            if s == want:
                return time.time() - t0
            time.sleep(1)
        return None

    def ssm_run(self, cmds, timeout=180):
        cid = self.ssm.send_command(
            InstanceIds=[self.iid], DocumentName="AWS-RunShellScript",
            Parameters={"commands": cmds})["Command"]["CommandId"]
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(3)
            try:
                r = self.ssm.get_command_invocation(
                    CommandId=cid, InstanceId=self.iid)
            except Exception:
                continue
            if r["Status"] in ("Success", "Failed", "TimedOut"):
                return r["Status"], r.get("StandardOutputContent", "")
        return "Timeout", ""

    def wait_ssm_online(self, timeout=300):
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = self.ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [self.iid]}])
            for i in r.get("InstanceInformationList", []):
                if i.get("PingStatus") == "Online":
                    return True
            time.sleep(5)
        return False

    def install_hung_service(self):
        st, out = self.ssm_run([
            "cat > /etc/systemd/system/hungapp.service <<'EOF'\n%s\nEOF" % UNIT,
            "systemctl daemon-reload",
            "systemctl enable --now hungapp.service",
            "sleep 2",
            "systemctl is-active hungapp.service",
            # 记录实际生效的停止超时，作为对照依据
            "systemctl show -p TimeoutStopUSec hungapp.service",
        ])
        self.sink(kind="service_installed", status=st, detail=out.strip()[:200])
        return st == "Success"

    def trial(self, mode, rep):
        """测一次停止耗时。mode: plain | skip_os"""
        kwargs = {"SkipOsShutdown": True} if mode == "skip_os" else {}
        self.sink(kind="trial_begin", mode=mode, rep=rep, stop_kwargs=kwargs)

        t0 = time.time()
        self.ec2.stop_instances(InstanceIds=[self.iid], **kwargs)
        t_api = time.time() - t0
        stop_s = self.wait_state("stopped")
        self.sink(kind="trial_result", mode=mode, rep=rep,
                  api_seconds=round(t_api, 3),
                  stop_seconds=round(stop_s, 3) if stop_s else None)

        # 恢复现场：启动 + 等 SSM 上线 + 重新装冻死服务
        self.ec2.start_instances(InstanceIds=[self.iid])
        run_s = self.wait_state("running")
        self.sink(kind="restarted", running_seconds=round(run_s, 3) if run_s else None)
        if not self.wait_ssm_online():
            self.sink(kind="ssm_not_online", note="无法重装服务，后续轮次不可信")
            return stop_s
        self.install_hung_service()
        return stop_s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance-id", required=True)
    ap.add_argument("--reps", type=int, default=2,
                    help="每种模式重复次数。n<3 不得用于倍数比较，只作量级判断")
    ap.add_argument("--out", default="-")
    a = ap.parse_args()

    fh = sys.stdout if a.out == "-" else open(a.out, "a", encoding="utf-8")

    def sink(**rec):
        rec["schema"] = "d11/1"
        rec["ts"] = time.time()
        rec["t"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()

    r = Runner(a.instance_id, sink)
    sink(kind="run_begin", instance_id=a.instance_id, reps=a.reps)

    sys.stderr.write("等 SSM 上线...\n")
    if not r.wait_ssm_online():
        sys.stderr.write("SSM 未上线，放弃\n")
        return 1
    if not r.install_hung_service():
        sys.stderr.write("装冻死服务失败\n")
        return 1
    sys.stderr.write("冻死服务已就绪\n")

    results = {}
    # 交替顺序，避免「先测的那个占便宜」这类顺序效应
    for rep in range(1, a.reps + 1):
        for mode in ("plain", "skip_os"):
            sys.stderr.write("  %s rep%d ...\n" % (mode, rep))
            s = r.trial(mode, rep)
            results.setdefault(mode, []).append(s)
            sys.stderr.write("    stopping->stopped %.3f s\n" % (s or -1))

    summary = {}
    for m, v in results.items():
        vv = sorted(x for x in v if x is not None)
        if vv:
            summary[m] = {"n": len(vv), "min": round(vv[0], 3),
                          "median": round(vv[len(vv) // 2], 3),
                          "max": round(vv[-1], 3),
                          "values": [round(x, 3) for x in vv]}
    sink(kind="summary", results=summary,
         note="n<3 不支撑倍数比较；本实验只用于判断量级差异")
    sys.stderr.write("\n=== 汇总 ===\n%s\n"
                     % json.dumps(summary, ensure_ascii=False, indent=2))
    if fh is not sys.stdout:
        fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
