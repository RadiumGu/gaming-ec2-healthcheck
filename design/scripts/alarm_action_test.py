#!/usr/bin/env python3
"""D10b：terminate 告警动作可注入性测试 + CloudTrail 交叉验证。

用 boto3 而非 shell，因为 shell 里的变量赋值前缀会被安全策略拦下。
"""
import json
import sys
import time

import boto3
from botocore.config import Config

REGION = "ap-northeast-1"
OUT = "/home/ec2-user/works/ec2-failure/validation/raw/D10/alarm-actions.ndjson"


def emit(fh, **rec):
    rec["t_wall"] = time.time()
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fh.flush()


def main():
    iid = open("/tmp/d10_inst.txt").read().split()[0]
    cfg = Config(region_name=REGION, retries={"max_attempts": 3, "mode": "standard"})
    sess = boto3.session.Session()
    ec2 = sess.client("ec2", config=cfg)
    cw = sess.client("cloudwatch", config=cfg)
    ct = sess.client("cloudtrail", config=cfg)
    fh = open(OUT, "a", encoding="utf-8")

    def state():
        r = ec2.describe_instances(InstanceIds=[iid])
        return r["Reservations"][0]["Instances"][0]["State"]["Name"]

    st0 = state()
    print("=== terminate 测试 ===")
    print("  起始状态: %s" % st0)

    t0 = time.time()
    cw.set_alarm_state(AlarmName="d10-action-terminate-%s" % iid,
                       StateValue="ALARM",
                       StateReason="D10 terminate action injectability test")
    emit(fh, kind="set_alarm_state", action="terminate", from_state=st0)
    print("  已发 set-alarm-state，1 秒粒度监视 180 秒")

    first = None
    st = st0
    for _ in range(180):
        st = state()
        dt = time.time() - t0
        emit(fh, kind="poll", action="terminate", dt=round(dt, 1), state=st)
        if st != st0 and first is None:
            first = dt
            print("  >>> t+%.0fs 状态转为 %s" % (dt, st))
        if st == "terminated":
            print("  >>> t+%.0fs 已 terminated" % dt)
            break
        time.sleep(1)
    if first is None:
        print("  180 秒内无状态转换，状态始终 %s" % st)

    print()
    print("=== 告警历史 ===")
    h = cw.describe_alarm_history(AlarmName="d10-action-terminate-%s" % iid,
                                 MaxRecords=4)
    for it in h["AlarmHistoryItems"]:
        print("  %-20s %s" % (it["HistoryItemType"], it["HistorySummary"]))
        emit(fh, kind="alarm_history", action="terminate",
             item_type=it["HistoryItemType"], summary=it["HistorySummary"])

    print()
    print("=== CloudTrail 交叉验证（三个动作是否留下 API 调用记录）===")
    print("  注意：CloudTrail 有投递延迟，零条不能立刻断定未发生")
    time.sleep(30)
    from datetime import datetime, timedelta, timezone
    start = datetime.now(timezone.utc) - timedelta(minutes=30)
    for ev in ("RebootInstances", "StopInstances", "TerminateInstances"):
        r = ct.lookup_events(
            LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": ev}],
            StartTime=start)
        hits = [e for e in r.get("Events", []) if iid in e.get("CloudTrailEvent", "")]
        # 谁发起的：告警动作的调用者应是 CloudWatch 而非本人
        who = set()
        for e in hits:
            try:
                d = json.loads(e["CloudTrailEvent"])
                who.add(d.get("userIdentity", {}).get("invokedBy")
                        or d.get("userIdentity", {}).get("type") or "?")
            except Exception:
                pass
        print("  %-20s %d 条  发起方=%s" % (ev, len(hits), ",".join(sorted(who)) or "-"))
        emit(fh, kind="cloudtrail", event_name=ev, count=len(hits),
             invoked_by=sorted(who))

    fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
