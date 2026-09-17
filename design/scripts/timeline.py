#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
timeline.py —— 把取证器/编排器的 NDJSON 折成分段耗时表

这是取证的产出物：一张能拿去跟服务团队谈的表，而不是「观察下来 6-7 分钟」。

三层时间轴严格分开（混用会算出错误的分段）：
  t_fault      故障真正发生       —— --t-fault 传入，或从 console/lifecycle 推
  t_datapoint  指标点自己的时间戳  —— metric_datapoint.ts_datapoint
  t_observed   我们看到它的时刻    —— 每条事件的 ts_observed

输出分段：
  detect_aws     t_datapoint - t_fault        AWS 侧检测延迟（要服务团队改的那段）
  publish_lag    t_observed  - t_datapoint    指标发布延迟
  api_visible    t_api_impaired - t_fault     直接轮询 API 能多快看到（对比指标通道）
  alarm_lag      t_alarm - t_datapoint        告警评估延迟
  recover_*      恢复段各子段
  total          t_app_ready - t_fault        客户真正关心的那个总时长

用法：
  python3 timeline.py --raw-dir ../validation/raw/D1 [--t-fault 2026-09-17T05:00:00Z]
  python3 timeline.py --raw-dir ... --json   # 机器可读

仅标准库。Python 3.9+。
"""

from __future__ import annotations

import argparse
import calendar
import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


def parse_iso(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().rstrip("Z")
    frac = 0.0
    if "." in s:
        s, f = s.split(".", 1)
        try:
            frac = float("0." + f)
        except ValueError:
            frac = 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            import time as _t
            return calendar.timegm(_t.strptime(s, fmt)) + frac
        except ValueError:
            continue
    return None


def load(raw_dir: str) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(raw_dir, "**", "*.ndjson"), recursive=True)):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                r["_src"] = os.path.basename(path)
                recs.append(r)
    recs.sort(key=lambda r: r.get("ts_observed") or r.get("ts") or 0.0)
    return recs


def ts_of(r: Dict[str, Any]) -> float:
    return float(r.get("ts_observed") or r.get("ts") or 0.0)


class Marks:
    """从事件流里挑出关键时刻。每个都记「值 + 出处」，不允许出现来源不明的数字。"""

    def __init__(self) -> None:
        self.m: Dict[str, Tuple[float, str]] = {}

    def put(self, key: str, ts: Optional[float], src: str) -> None:
        if ts is None or ts <= 0:
            return
        if key not in self.m or ts < self.m[key][0]:
            self.m[key] = (ts, src)

    def get(self, key: str) -> Optional[float]:
        v = self.m.get(key)
        return v[0] if v else None

    def src(self, key: str) -> str:
        v = self.m.get(key)
        return v[1] if v else ""


def extract(recs: List[Dict[str, Any]], t_fault_arg: Optional[float]) -> Marks:
    mk = Marks()
    if t_fault_arg:
        mk.put("t_fault", t_fault_arg, "--t-fault")

    for r in recs:
        kind = r.get("kind")
        t = ts_of(r)

        # --- 状态检查通道：第一次出现非 ok --------------------------------
        if kind == "status_transition":
            cur = r.get("current") or {}
            for iid, st in cur.items():
                if st.get("instance_status") not in (None, "ok"):
                    mk.put("t_api_instance_impaired", t, "status_transition/%s" % iid)
                if st.get("system_status") not in (None, "ok"):
                    mk.put("t_api_system_impaired", t, "status_transition/%s" % iid)
                if st.get("attached_ebs_status") not in (None, "ok"):
                    mk.put("t_api_ebs_impaired", t, "status_transition/%s" % iid)
                if st.get("instance_status") == "ok" and st.get("system_status") == "ok":
                    mk.put("t_api_status_ok_again", t, "status_transition/%s" % iid)

        # --- 指标通道：第一个值为 1 的失败点 ------------------------------
        if kind == "metric_datapoint" and (r.get("value") or 0) >= 1:
            label = str(r.get("label") or "")
            ts_dp = r.get("ts_datapoint")
            key = None
            if "StatusCheckFailed_Instance" in label or label == "i1":
                key = "t_dp_instance"
            elif "StatusCheckFailed_System" in label or label == "i2":
                key = "t_dp_system"
            elif "StatusCheckFailed_AttachedEBS" in label or label == "i3":
                key = "t_dp_ebs"
            elif "VolumeStalledIOCheck" in label:
                key = "t_dp_stalled_io"
            if key:
                mk.put(key, ts_dp, "metric_datapoint/%s" % label)
                mk.put(key + "_observed", t, "metric_datapoint/%s(observed)" % label)

        # --- 卷状态 -------------------------------------------------------
        if kind == "volume_status":
            for v in r.get("volumes") or []:
                if v.get("status") not in (None, "ok"):
                    mk.put("t_volume_impaired", t, "volume_status/%s" % v.get("volume_id"))

        # --- 生命周期 -----------------------------------------------------
        if kind == "lifecycle_transition":
            cur = r.get("current") or {}
            for iid, st in cur.items():
                state = st.get("state")
                if state:
                    mk.put("t_lifecycle_%s" % state, t, "lifecycle_transition/%s" % iid)

        # --- 编排器打点 ---------------------------------------------------
        if kind == "decision":
            mk.put("t_action_decision", t, "orchestrator/decision")
        if kind == "api_ok" and r.get("api") == "StopInstances":
            mk.put("t_action_stop_api", t, "orchestrator/StopInstances")
        if kind == "api_ok" and r.get("api") == "StartInstances":
            mk.put("t_action_start_api", t, "orchestrator/StartInstances")
        if kind == "state_observed" and r.get("state") == "stopped":
            mk.put("t_stopped", t, "orchestrator/state_observed")
        if kind == "state_observed" and r.get("state") == "running":
            mk.put("t_running", t, "orchestrator/state_observed")
        if kind == "app_endpoint_up":
            mk.put("t_app_endpoint_up", t, "orchestrator/app_endpoint_up")
        if kind == "app_ready":
            mk.put("t_app_ready", t, "orchestrator/app_ready")
        if kind == "recovered":
            mk.put("t_recovered", t, "orchestrator/recovered")

        # --- 探测器打点 ---------------------------------------------------
        if kind == "escalation_change" and r.get("to") == "soft":
            mk.put("t_prober_soft", t, "prober/escalation_change")
        if kind == "escalation_change" and r.get("to") == "hard":
            mk.put("t_prober_hard", t, "prober/escalation_change")
        if kind == "action_request":
            mk.put("t_prober_action_request", t, "prober/action_request")

    # t_fault 兜底：没传就用最早的异常观测，并明确标注是推断值
    if mk.get("t_fault") is None:
        for k in ("t_prober_soft", "t_api_instance_impaired", "t_api_ebs_impaired",
                  "t_api_system_impaired"):
            v = mk.get(k)
            if v:
                mk.put("t_fault", v, "INFERRED from %s (不是真实故障时刻)" % k)
                break
    return mk


SEGMENTS: List[Tuple[str, str, str, str]] = [
    # (段名, 起点, 终点, 说明)
    ("prober_detect", "t_fault", "t_prober_soft", "应用侧探针检测延迟（秒级，客户已有能力）"),
    ("prober_to_action", "t_fault", "t_prober_action_request", "探针从故障到请求动作"),
    ("api_instance_visible", "t_fault", "t_api_instance_impaired", "直接轮询 API 看到 instance 异常"),
    ("api_system_visible", "t_fault", "t_api_system_impaired", "直接轮询 API 看到 system 异常"),
    ("api_ebs_visible", "t_fault", "t_api_ebs_impaired", "直接轮询 API 看到 EBS 异常"),
    ("dp_instance_detect", "t_fault", "t_dp_instance", "AWS 侧 instance check 检测延迟"),
    ("dp_system_detect", "t_fault", "t_dp_system", "AWS 侧 system check 检测延迟"),
    ("dp_ebs_detect", "t_fault", "t_dp_ebs", "AWS 侧 EBS check 检测延迟"),
    ("dp_stalled_io", "t_fault", "t_dp_stalled_io", "VolumeStalledIOCheck 置位延迟"),
    ("volume_impaired", "t_fault", "t_volume_impaired", "卷状态转 impaired 延迟"),
    ("publish_lag_instance", "t_dp_instance", "t_dp_instance_observed", "指标发布延迟"),
    ("publish_lag_system", "t_dp_system", "t_dp_system_observed", "指标发布延迟"),
    ("recover_stop", "t_action_stop_api", "t_stopped", "stop 完成耗时"),
    ("recover_start", "t_action_start_api", "t_running", "start 到 running 耗时"),
    ("recover_os_boot", "t_running", "t_app_endpoint_up", "running 到健康端点可达（OS 启动）"),
    ("recover_app", "t_app_endpoint_up", "t_app_ready", "端点可达到 tick 推进（游戏进程 ready）"),
    ("recover_total", "t_action_decision", "t_app_ready", "编排决策到应用真正可服务"),
    ("TOTAL", "t_fault", "t_app_ready", "故障到应用可服务：客户真正关心的总时长"),
]


def render(mk: Marks, as_json: bool) -> int:
    rows = []
    for name, a, b, desc in SEGMENTS:
        ta, tb = mk.get(a), mk.get(b)
        if ta is None or tb is None:
            rows.append({"segment": name, "seconds": None,
                         "missing": a if ta is None else b, "desc": desc})
            continue
        rows.append({"segment": name, "seconds": round(tb - ta, 3),
                     "from": a, "to": b, "from_src": mk.src(a), "to_src": mk.src(b),
                     "desc": desc})
    marks = {k: {"iso": __import__("time").strftime("%Y-%m-%dT%H:%M:%S", __import__("time").gmtime(v[0]))
                 + ".%03dZ" % int((v[0] % 1) * 1000), "src": v[1]}
             for k, v in sorted(mk.m.items(), key=lambda kv: kv[1][0])}

    if as_json:
        print(json.dumps({"marks": marks, "segments": rows}, ensure_ascii=False, indent=2))
        return 0

    print("## 关键时刻\n")
    print("| 标记 | UTC | 出处 |")
    print("|---|---|---|")
    for k, v in marks.items():
        print("| `%s` | %s | %s |" % (k, v["iso"], v["src"]))
    print("\n## 分段耗时\n")
    print("| 段 | 秒 | 起 → 终 | 说明 |")
    print("|---|---|---|---|")
    for r in rows:
        if r["seconds"] is None:
            print("| `%s` | — | 缺 `%s` | %s |" % (r["segment"], r["missing"], r["desc"]))
        else:
            print("| `%s` | **%.3f** | `%s` → `%s` | %s |"
                  % (r["segment"], r["seconds"], r["from"], r["to"], r["desc"]))
    missing = [r["segment"] for r in rows if r["seconds"] is None]
    if missing:
        print("\n> 缺段：%s —— 缺失即未测到，不得用估算值填充。" % ", ".join(missing))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="把取证 NDJSON 折成分段耗时表")
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--t-fault", default="", help="故障真实发生时刻 ISO8601Z（强烈建议传）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    recs = load(a.raw_dir)
    if not recs:
        sys.stderr.write("no ndjson records under %s\n" % a.raw_dir)
        return 2
    mk = extract(recs, parse_iso(a.t_fault) if a.t_fault else None)
    sys.stderr.write("loaded %d records\n" % len(recs))
    return render(mk, a.json)


if __name__ == "__main__":
    sys.exit(main())
