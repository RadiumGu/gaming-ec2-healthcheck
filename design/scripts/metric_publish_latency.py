#!/usr/bin/env python3
"""D10a：测量 CloudWatch 自定义指标的发布延迟。

补的是探测器心跳那条 `[待测]`：心跳走自定义指标，那么「探测器死了」
这件事最快多久能被 CloudWatch 侧看见？

已知对照：AWS 托管的 EC2 status check 指标实测发布延迟 84 秒（D1 两次独立印证）。
自定义指标是否更快，此前只是假设，没有测。

方法：
  1. `PutMetricData` 写一个带唯一名字的指标（避免与历史数据点混淆）
  2. 立刻开始紧密轮询 `GetMetricData`，直到该数据点出现
  3. 记录 `t_put` → `t_visible` 的差值

两个必须分清的时间概念：
  * **发布延迟** = 从 PutMetricData 返回，到该数据点能被 GetMetricData 读到
  * 指标时间戳本身由调用方指定，不代表可见时间

为什么每轮用新指标名：同名指标若已有数据点，GetMetricData 可能返回旧点，
把「已经可见」误判成「刚发布就可见」。唯一名字保证测的是首个数据点。
"""

import argparse
import json
import sys
import time
import uuid

try:
    import boto3
    from botocore.config import Config
except ImportError:
    sys.stderr.write("需要 boto3\n")
    sys.exit(2)


def one_trial(cw, namespace, period, poll_interval, timeout, sink):
    """发一个唯一指标并轮询到可见，返回发布延迟秒数（超时返回 None）。"""
    metric = "PublishLatencyProbe-%s" % uuid.uuid4().hex[:12]
    # 指标时间戳对齐到秒：CloudWatch 按秒存储，亚秒部分会被归整
    ts = time.time()
    cw.put_metric_data(
        Namespace=namespace,
        MetricData=[{
            "MetricName": metric,
            "Value": 1.0,
            "Unit": "Count",
            "Timestamp": ts,
            # 标准分辨率（60s）。高分辨率需 StorageResolution=1，单独测
            "StorageResolution": period,
        }],
    )
    t_put = time.time()
    sink({"kind": "put", "metric": metric, "t_put": t_put,
          "resolution_s": period})

    deadline = t_put + timeout
    polls = 0
    while time.time() < deadline:
        polls += 1
        # 查询窗口刻意开宽：避免因窗口边界对齐问题把「已可见」测成「不可见」
        r = cw.get_metric_data(
            MetricDataQueries=[{
                "Id": "q1",
                "MetricStat": {
                    "Metric": {"Namespace": namespace,
                               "MetricName": metric},
                    "Period": max(1, period),
                    "Stat": "Sum",
                },
                "ReturnData": True,
            }],
            StartTime=t_put - 300,
            EndTime=t_put + 300,
        )
        vals = r["MetricDataResults"][0].get("Values") or []
        if vals:
            t_vis = time.time()
            lat = t_vis - t_put
            sink({"kind": "visible", "metric": metric, "t_visible": t_vis,
                  "publish_latency_s": round(lat, 3), "polls": polls,
                  "values": vals})
            return lat
        time.sleep(poll_interval)

    sink({"kind": "timeout", "metric": metric, "polls": polls,
          "waited_s": round(time.time() - t_put, 1)})
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-northeast-1")
    ap.add_argument("--namespace", default="GameShard/LatencyProbe")
    ap.add_argument("--trials", type=int, default=3,
                    help="重复次数。n>=3 才支撑范围陈述")
    ap.add_argument("--resolution", type=int, default=60, choices=[1, 60],
                    help="StorageResolution：60=标准，1=高分辨率")
    ap.add_argument("--poll-interval", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--gap", type=float, default=10.0,
                    help="两次试验之间的间隔，避免相互影响")
    ap.add_argument("--out", default="-")
    a = ap.parse_args()

    fh = sys.stdout if a.out == "-" else open(a.out, "a", encoding="utf-8")

    def sink(rec):
        rec["t_wall"] = time.time()
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()

    cw = boto3.session.Session().client(
        "cloudwatch",
        config=Config(region_name=a.region,
                      retries={"max_attempts": 3, "mode": "standard"}))

    lats = []
    for i in range(a.trials):
        sys.stderr.write("trial %d/%d ...\n" % (i + 1, a.trials))
        lat = one_trial(cw, a.namespace, a.resolution,
                        a.poll_interval, a.timeout, sink)
        if lat is not None:
            lats.append(lat)
            sys.stderr.write("  可见于 %.1f s\n" % lat)
        else:
            sys.stderr.write("  超时未可见\n")
        if i < a.trials - 1:
            time.sleep(a.gap)

    lats.sort()
    summary = {
        "kind": "summary",
        "resolution_s": a.resolution,
        "n": len(lats),
        "min_s": round(lats[0], 3) if lats else None,
        "median_s": round(lats[len(lats) // 2], 3) if lats else None,
        "max_s": round(lats[-1], 3) if lats else None,
        # 自我约束：与 D8 同一口径
        "note": "n<3 不得用于倍数比较" if len(lats) < 3 else "n>=3，可作范围陈述",
    }
    sink(summary)
    sys.stderr.write("\n=== 汇总 ===\n%s\n"
                     % json.dumps(summary, ensure_ascii=False, indent=2))
    if fh is not sys.stdout:
        fh.close()
    return 0 if lats else 1


if __name__ == "__main__":
    sys.exit(main())
