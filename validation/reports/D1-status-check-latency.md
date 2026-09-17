# 验证报告 D1 —— EC2 状态检查的真实上报延迟

执行时间：2026-09-17 05:28–05:38 UTC
被测实例：`i-TARGET00000000001` m7i.large / 203.0.113.102 / AL2023 x86 / ap-northeast-1a
原始数据：`validation/raw/D1/`（第一次注入）、`validation/raw/D1b/`（修正后注入）

---

## 采集通道 preflight　`[实测]`

| 通道 | 结果 | 延迟 |
|---|---|---|
| `DescribeInstanceStatus` | ok | 91.8 ms |
| `DescribeInstances` | ok | 126.7 ms |
| `GetMetricData` | ok，64 个数据点 | 43.1 ms |
| `GetConsoleOutput` | ok | — |
| **AWS Health `DescribeEvents`** | **不可用** `SubscriptionRequiredException` | — |

**这是一条对客户直接有影响的发现**：该账号没有 Business/Enterprise 支持计划，
所以 **AWS Health 事件这条检测通道对客户不可用**。任何依赖 Health 事件做
「提前得知宿主机退役/降级」的方案在这个支持等级下都不成立。
取证器按预期降级记录（`health_unavailable`）而不是当成缺陷。

---

## 第一次注入：kernel panic 却自动重启了　`[实测]` 用例设计被自己的测量推翻

`t_fault = 05:28:57.892`，SSM 发 `echo c > /proc/sysrq-trigger`。

预期实例挂死。实际发生的是：

| 时刻 | 相对 | 观测 |
|---|---|---|
| 05:28:57.728 | -0.2s | 探测器 round 32 `HEALTHY`，tick=12062 |
| 05:29:01.733 | **+3.8s** | 探测器 `HOST_DOWN`（icmp+tcp 全断，sentinel 正常） |
| 05:29:08.764 | +10.9s | 探测器 `APP_DEAD`（icmp 恢复，游戏端口 **refused**）—— 内核回来了，服务还没起 |
| 05:29:09.783 | **+11.9s** | 探测器 `HEALTHY`，**tick=9** —— 计数器归零，说明进程重新启动 |

console output 给出了根因原文：

```
[  777.799916] sysrq: Trigger a crash
[  777.800284] Kernel panic - not syncing: sysrq triggered crash
[  777.865710] Rebooting in 5 seconds..
```

`sysctl kernel.panic` = **5**（AL2023 默认值），`uptime -s` = `2026-09-17 05:29:04`。
所以 panic 触发了一次约 12 秒的自动重启，而不是持续故障。

### 由此得到的独立发现：短于检查周期的故障，EC2 侧根本不报

取证器以 **1 秒粒度**轮询整个 12 秒窗口，`status_transition` 只记到一条基线
（`inst=ok sys=ok ebs=ok`），**全程没有任何 impaired 转换**，
`StatusCheckFailed_*` 没有产生过值为 1 的数据点。

客户的抱怨是「system status check 报警慢」。这次测量说明存在比慢更严重的一档：
**故障窗口短于 60 秒检查周期时，EC2 侧完全不会报出来 —— 不是慢，是漏。**
这类事件只有应用侧探针能看到（本例 3.8 秒）。
而且 `reboot` 不改变 EC2 生命周期状态（实例始终 `running`），
生命周期通道同样看不见它。

---

## 修正后注入：`kernel.panic=0` 让内核挂住　`[实测]` 得到目标测量

`t_fault = 05:32:04.760`。先 `sysctl -w kernel.panic=0`（已核对生效），再注入 panic。

### 三层时间轴分段表

| 事件 | 时刻 (UTC) | 相对 t_fault | 出处 |
|---|---|---|---|
| 探测器最后一轮 `HEALTHY` | 05:32:04.248 | −0.5 s | `prober.ndjson` round 19，tick=3508 |
| **探测器判 `HOST_DOWN`** | 05:32:08.253 | **+3.5 s** | round 20，sentinel 同时正常 |
| 探测器升 `soft` | 05:32:14.275 | +9.5 s | 3 连续失败 |
| **探测器 `hard` + `action_request`** | 05:32:20.296 | **+15.5 s** | `held_seconds=12.04` |
| AWS 声明的 `ImpairedSince` | 05:36:00 | +235.2 s | `InstanceStatus.Details` |
| 指标数据点 `t_datapoint` | 05:36:00 | +235.2 s | `StatusCheckFailed_Instance=1` |
| **取证器 API 观测到 `impaired`** | 05:36:19.802 | **+255.0 s** | 1s 轮询 `DescribeInstanceStatus` |
| console 快照自动抓取 | 05:36:20.240 | +255.5 s | 状态翻转触发，含 panic 原文 |
| **指标可见 `t_observed`** | 05:37:23.957 | **+319.2 s** | `publish_lag = 83.96 s` |

| 分段 | 秒 | 含义 |
|---|---|---|
| `prober_detect` | **3.5** | 应用侧探针检测延迟 |
| `prober_to_action` | **15.5** | 探针到请求恢复动作 |
| `aws_detect` | **235.2** | AWS 侧检测延迟（`ImpairedSince` − `t_fault`） |
| `api_visible` | **255.0** | 直接轮询 API 能看到的最早时刻 |
| `publish_lag` | **84.0** | 指标发布延迟 |
| `metric_visible` | **319.2** | 走指标/告警通道的最早时刻 |

### 两条通道的差距

| 对比 | 倍数 |
|---|---|
| 应用侧探针 3.5 s ↔ API 通道 255.0 s | **73 倍** |
| 应用侧到动作 15.5 s ↔ 指标可见 319.2 s | **21 倍** |

直接轮询 `DescribeInstanceStatus` 比等指标可见**提前 64 秒**（255.0 vs 319.2），
这一段是不改任何架构、只改读取方式就能拿回来的。

---

## 最重要的一条：`StatusCheckFailed_System` 全程为 0

整个 5 分钟里 `system_status` 始终是 **`ok`**，只有 `instance_status` 变 `impaired`
（`reachability: failed`）。这是正确的 —— 宿主机没坏，死的是 guest 内核。

**而 auto recovery 只对 system status check 失败动作**（`[文档]`：
"automatic instance recovery does not operate if only the instance status check fails"）。

所以：**内核挂死、OOM、文件系统损坏、驱动 hang 这一整类 guest 侧故障，
auto recovery 完全不会介入。** 客户以为「靠云厂商自动恢复兜底」，
实际上对最常见的一类故障没有任何兜底 —— 实例会一直 `running` 且 `impaired`，
永远等不到自动恢复，直到有人手工介入。

这一条重新框定了客户的原始问题。他们问的是「system status check 能不能报得更早」，
而测量显示：对 guest 侧故障，system status check **根本不会报**。

---

## 结论

1. 应用侧探针 **3.5 秒**检测、**15.5 秒**请求动作；EC2 侧 **255 秒**（API）／**319 秒**（指标）。
2. 指标发布延迟单独占 **84 秒**，所以自建探测应直接轮询 `DescribeInstanceStatus` 而非等告警。
3. **短于 60 秒的故障 EC2 侧不报**，只有应用侧探针能看到。
4. **guest 侧故障不触发 auto recovery**，客户当前的兜底假设不成立。
5. AWS Health 通道因支持计划等级不可用。
6. 取证器的三层时间轴设计成立：`t_datapoint`、`t_observed`、`ImpairedSince`
   三个值互不相同，混用任意两个都会得出错误的分段耗时。

恢复段的测量见 `D4-recovery-segments.md`（本次注入产生的挂死实例直接用于 D4）。
