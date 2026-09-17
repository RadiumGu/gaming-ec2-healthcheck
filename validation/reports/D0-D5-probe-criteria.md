# 验证报告 D0 / D5 —— 探测判据选型

执行时间：2026-09-17 05:15–05:20 UTC
被测实例：`i-TARGET00000000001` m7i.large / 203.0.113.102 / ap-northeast-1a / AL2023 x86
探测器主机：`i-PROBER00000000000` 203.0.113.10（同子网同 AZ，跨主机真实网络路径）
对照 sentinel：`203.0.113.20`（sentinel-host）
原始数据：`validation/raw/D0/`、`validation/raw/D5/`、`validation/raw/D5b/`

---

## D0 —— 阳性对照与跨主机基线　`[实测] 通过`

| 项 | 值 |
|---|---|
| `preflight_sentinel.healthy` | `true`（ICMP up rtt 0.243ms，TCP 22 up rtt 0.293ms） |
| 被测实例 app 探针 rtt | 1.54 – 3.83 ms（首轮 3.83 含连接建立） |
| 被测实例 TCP 7777 rtt | 0.16 – 0.25 ms |
| 被测实例 ICMP rtt | 0.153 – 0.269 ms |
| tick 推进速率 | 20 / 秒（与 `--tick-hz 20` 一致） |
| 10 轮判定 | 全部 `HEALTHY` |

被测实例健康时探测器**不探 sentinel**（`detail=not_probed_target_healthy`），
避免给一台别人的实例带无意义流量。sentinel 只在被测出问题时才作为对照被调用。

**实例创建即验证的一点**：`MaintenanceOptions.AutoRecovery = default`。
即 AWS 的简化自动恢复对这台实例是**开着**的，自建编排与它会 race。
编排器会为此打 `race_warning`，D4 必须实测这个相互影响。

---

## D5 —— SIGSTOP 冻结整个进程　`[实测] 通过，且结论强于预期`

注入：SSM 对 `game_stub.py` 主进程发 `SIGSTOP`，`/proc/<pid>/status` 确认 `State: T (stopped)`。

| 轮次 | 判定 | tcp_up | http_up | 说明 |
|---|---|---|---|---|
| 1 – 6 | `APP_STUCK` | **true** | false | **进程完全冻死，TCP 仍握手成功** |
| 7 – 18 | `APP_DEAD` | false | false | listen backlog 填满后 TCP 才开始 timeout |

**这是比原设计主张更强的一条证据**：一个 `State: T` 的进程，
内核用 listen backlog 代替它完成了三次握手，**TCP connect 连续 6 秒返回 `open`**。
假阴性窗口的长度由 backlog 深度和探测频率决定，**与进程健康无关**。

判定从 `APP_STUCK` 正确迁移到 `APP_DEAD`（动作阶梯从「重启服务」到「重启服务→换宿主机」），
`action_request` 只在第 6 轮发出一次，之后 12 轮全部被冷却期抑制 —— 冷却机制生效。

### 与本地冒烟结果不一致，已修正

本地冒烟用桩程序的 `--stall-after`（只冻主循环），得到「HTTP 200 + tick 冻住」；
真机用 `SIGSTOP` 冻整个进程，健康端点随进程一起死，**那一支没有被复现**。
`SIGSTOP` 不是「HTTP 仍可服务但 tick 冻住」的正确注入手段，因此补跑 D5b。
不做这一步，就会拿一个本地才成立的现象当真机结论。

---

## D5b —— 只冻主循环（游戏服真实故障形态）　`[实测] 通过`

注入：改 systemd 单元加 `--stall-after 15`，重启服务；HTTP 与 TCP 服务线程继续工作，仅 tick 停。

| 轮次 | 判定 | tick | tcp_up | http_up |
|---|---|---|---|---|
| 1 – 5 | `HEALTHY` | 204→284 推进 | true | true |
| 6 – 10 | `HEALTHY` | **300 冻住** | true | true |
| 11 – 22 | `APP_STUCK` | 300 冻住 | **true** | **true** |

**连续 12 轮 TCP `open` 且 HTTP 200，而服务实际已停止推进。**
纯 TCP 判据在这 12 轮里全部判健康；纯 HTTP-200 判据同样全部判健康。
只有「tick 是否推进」这一判据抓到了它。

### 检测时序

| 段 | 值 |
|---|---|
| tick 冻住 → 判定翻转 `APP_STUCK` | ≈ 5 s（`tick_stall_seconds=4` + 1s 探测间隔） |
| 判定翻转 → `soft`（3 连续） | 2 s |
| `soft` → `hard`（7 连续且持续 ≥5s） | 4 s |
| **tick 冻住 → `action_request`** | **≈ 11 s**（`held_seconds=6.0`） |

对比 EC2 侧：状态检查周期 60 s + 指标 1 分钟粒度 + 告警评估。
应用侧探针把检测段从**分钟级压到 11 秒**，这一段是客户完全自主可控的。

---

## 结论

1. **TCP connect 不能当存活触发器。** 两个实验都证实了，且 D5 给出机制：
   内核 backlog 会在进程冻死后继续完成握手，假阴性窗口与进程健康无关。
2. **HTTP 200 也不够。** D5b 里健康端点连续 12 轮回 200，服务却已停摆。
   判据必须是**响应体内容在推进**，不是响应码。
3. **ICMP 的作用是分类不是检测。** D5 中 ICMP 一直 up，正是它把
   `APP_DEAD`（进程没了）与 `HOST_DOWN`（宿主机没了）区分开。
4. 检测段实测 ≈ 11 s，`hard` 的「次数与时长同时满足」条件按预期生效，
   冷却期把 12 次重复动作请求压成 1 次。

## 待测

- D6 抑制机制反向验证（sentinel 与被测同时不可达必须判 `PROBER_SIDE` 且无 `action_request`）
- D1 instance status check 真实上报延迟
- D4 编排器 `--apply` 恢复段分段耗时，含与 `AutoRecovery=default` 的 race
- D3 恢复链路耗时；D2 需先补 FIS 的 EBS 权限
