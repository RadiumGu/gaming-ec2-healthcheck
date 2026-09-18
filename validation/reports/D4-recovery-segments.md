# 验证报告 D4 —— 自建 stop/start 的恢复段分段耗时

> ## 本报告的核心结论已被 D8 推翻，请先读 `D8-stop-mode-matrix.md`
>
> 保留本文件是为了留下错误的完整痕迹，不要拿它的数字去做决策。三处已知错误：
>
> | # | 本报告写的 | 实际 | 依据 |
> |---|---|---|---|
> | 1 | 「`Force` 跳过优雅关机，省不掉硬下电超时」 | **反了**。`Force=True` 是「先试优雅关机，超时后才硬下电」；真正绕过 OS 关机的是 `SkipOsShutdown=True`，本轮没用 | `StopInstances` API 参考原文 |
> | 2 | 257.2 s 是「挂死 guest 的固有代价」 | 是**用错参数**的代价。同样挂死的 guest 用 `SkipOsShutdown=True` 只需 **11.5 s**（22 倍） | D8，n=2，两组极差 3.8 s / 1.2 s |
> | 3 | `running → app_ready` 89.7 s / 121.8 s | 实测 **7.014 s**（n=12）。旧数字里 13–17 倍是本报告自己的 `wait_status_ok` 串行缺陷造成的 | D8 |
>
> 修正后的恢复段：挂死 guest **21.8 s**（而非 352.7 s），端到端约 **37 s**（而非 6 分 8 秒）。
> 因此本报告「自建路径恢复段没有改善」的结论**予以撤销**。
>
> **改善倍数不要用本报告的数字做分母。** 正确的口径是同为 D8 实测的两个参数之比：
> 恢复段 263.1 s（`Force`）→ 21.8 s（`SkipOsShutdown`），**12.1 倍**。
> 若拿本报告的 352.7 s 当分母会算出「16 倍」——**那是用一个已撤销的错误数字当基准**，
> 属于同一个错误的二次传播。检测段的 16.5 倍（255 → 15.5 s）不受此影响，仍然成立。


执行时间：2026-09-17 05:46:26 – 05:52:18 UTC
被测对象：`i-TARGET00000000001`，处于 D1b 注入后的 **内核 panic 挂死** 状态
（`running` + `instance_status=impaired`，已挂死约 14 分钟）
原始数据：`validation/raw/D4/orchestrator.ndjson`

## dry-run 先行

`--apply` 之前先跑一次 dry-run 确认动作阶梯：`verdict=HOST_DOWN → ladder=["stop_start"]`，
计划 `stop_instances(Force=True) → wait stopped → start_instances → wait running`。
同时打出 `race_warning`：`auto_recovery=default`，即 AWS 的简化自动恢复对这台实例是开着的。

**实测的 race 结果**：整个 D1b 的 14 分钟里 `system_status` 一直是 `ok`，
AWS 从未发起过恢复（因为它只对 system check 失败动作）。
所以本例中两者**没有实际相撞** —— 但这不是「race 不存在」的证据，
只说明 guest 侧故障不会激活 AWS 那一侧。宿主机真故障时的相撞行为仍属 `[待测]`。

---

## 分段耗时表

| 事件 | 时刻 (UTC) | 相对 t_action | 段耗时 |
|---|---|---|---|
| `decision` | 05:46:26.319 | 0 | — |
| `StopInstances(Force=True)` API 返回 | 05:46:26.749 | +0.64 s | API 调用 **0.64 s** |
| state → `stopping` | 05:46:29.085 | +2.97 s | 2.3 s |
| **state → `stopped`** | 05:50:43.970 | **+257.86 s** | **force stop 257.2 s** |
| `StartInstances` API 返回 | 05:50:44.931 | +258.82 s | — |
| state → `pending` | 05:50:47.072 | +260.96 s | 2.1 s |
| **state → `running`** | 05:50:49.196 | **+263.09 s** | **start 4.3 s** |
| status → `ok/ok` | 05:52:17.844 | +351.73 s | running 后 **88.6 s** |
| `app_endpoint_up`（tick=1688） | 05:52:17.846 | +351.74 s | — |
| **`app_ready`**（tick 1688→1708 推进） | 05:52:18.849 | **+352.74 s** | tick 推进确认 1.0 s |

| 分段 | 秒 | 占比 |
|---|---|---|
| `recover_stop`（stopping → stopped） | **257.2** | **72.9 %** |
| `recover_start`（stopped → running） | 4.3 | 1.2 % |
| `recover_boot_and_app`（running → app_ready） | 89.7 | 25.4 % |
| **`recover_total`（decision → app_ready）** | **352.7** | 100 % |

---

## 反直觉的主导项：慢在停止，不在启动

直觉是恢复慢在启动和应用加载。实测相反：**force stop 占了 72.9%，启动只占 1.2%。**

机制上说得通：guest 内核已经 panic，无法响应任何关机流程，
Nitro 侧只能等自己的超时到点后强制下电。`--force` 已经是最快的路径，
它省掉的是「先尝试优雅关机」那一段，省不掉硬下电的超时。

### 这改变了方案建议

把探测器的 15.5 秒接上编排器的 352.7 秒，端到端约 **368 秒 ≈ 6 分 8 秒**。
客户观测 auto recovery 是 6–7 分钟。**也就是说自建 stop/start 这条路
在总时长上几乎没有改善**，收益全部来自检测段（255 s → 15.5 s），
恢复段被 force stop 的 257 秒吃掉了。

要真正压缩总时长，必须**避开对故障实例本身做 stop 这个动作**：

| 路径 | 是否需要 stop 故障实例 | 预期 |
|---|---|---|
| 自建 force stop + start（本次实测） | 是 | 恢复段 **352.7 s** `[实测]` |
| ASG 替换（terminate + 新实例） | 否，terminate 不等关机 | `[待测]` |
| 备机池 + 卷 detach/attach + EIP 迁移 | 否（但 force detach 可能同样卡超时） | `[待测]` |

---

## D4b 对照实验：健康态 force stop　`[实测]` 归属已确定

原始数据：`validation/raw/D4b/orchestrator.ndjson`，执行 05:55:55 – 05:58:20。
前提已核对：实例 `ok/ok/ok`、tick 5821 推进、ping 0% 丢包、`kernel.panic=5`（`sysctl -w` 不持久，
stop/start 后已回到 AL2023 默认值）、`gameserver` active。

| 分段 | 挂死 guest（D4） | 健康 guest（D4b） | 倍数 |
|---|---|---|---|
| `StopInstances` API 返回 | 0.64 s | 0.64 s | 1.0 |
| running → stopping | 2.34 s | 2.14 s | 1.1 |
| **stopping → stopped** | **257.2 s** | **16.8 s** | **15.3** |
| stopped → running | 4.3 s | 4.3 s | 1.0 |
| running → app_ready（上界） | 89.7 s | 121.8 s | 0.7 |
| **decision → app_ready** | **352.7 s** | **145.6 s** | **2.4** |

**结论：257 秒是「挂死 guest」特有的代价，不是 force stop 固有的。**
健康 guest 能响应关机流程，16.8 秒就停下；panic 后的内核无法响应，
Nitro 只能等自己的超时到点硬下电。`--force` 省掉的是「先试优雅关机」，
省不掉硬下电超时。

**但这个结论对客户是坏消息，不是好消息**：生产中要 stop 的，
正是那台已经挂死的实例。所以真实故障场景落在 **257 秒那一档**，
健康态的 16.8 秒在故障恢复里永远用不上。

### 一个附带发现：`running → status ok` 方差很大

两次实测分别是 88.6 s 与 121.8 s（相差 37 %），而应用其实早就起来了
（两次 `app_endpoint_up` 都紧贴 `status ok` 后 2 毫秒，见下节的缺陷说明）。
这进一步说明**不能用 status check `ok` 当服务可用判据** ——
它自身抖动就有半分钟量级，而玩家能不能连上跟它无关。

### 路线取舍（更新）

| 路径 | 是否 stop 故障实例 | 恢复段 |
|---|---|---|
| 自建 force stop + start | 是 | **352.7 s** `[实测]` |
| ASG 替换（terminate + 新实例） | 否，terminate 不等关机 | `[待测]`，预期接近 D4b 的 145.6 s 加新实例分配开销 |
| 备机池 + 卷 detach/attach + EIP 迁移 | 否 | `[待测]`，**风险**：卷仍挂在无响应的实例上，`force detach` 很可能撞同一个硬下电超时，未必比 stop/start 快 |

备机池那条路的风险值得单独强调：它绕开的是「等实例停下」，
但没绕开「卷从一个无响应实例上摘下来」这个物理约束，
两者背后是同一个超时。这条路线在实测之前不应写进方案建议。


---

## 测量暴露出的编排器自身缺陷

`recover_boot_and_app` 这 89.7 秒**无法再往下拆**，原因是编排器自己的实现顺序：
`wait_status_ok()` 串在 `wait_app_ready()` 前面，所以应用可能早就 ready 了，
只是编排器还在等 status check 变 ok。证据是两个事件几乎同一毫秒：
`status ok/ok` 在 +351.733 s，`app_endpoint_up` 在 +351.735 s —— 相差 2 毫秒，
这不可能是应用刚好在那一刻起来，只能是探测被前一个等待挡住了。

这既是测量缺陷也是**生产缺陷**：status check `ok` 不是服务可用的前提，
玩家能连上才是。正确做法是两个探测并行，取先到者作为恢复完成时刻。
按「恢复只做到最后一个可核验的步骤」的原则，这里如实记为：
**running → app_ready 上界 89.7 秒，真实值更小但本次未测出。**

修法（待实施）：把 `wait_status_ok` 与 `wait_app_ready` 改成并发，
`app_ready` 一到就记时并返回，`status_ok` 仅作为附加观测继续记录。

---

## 结论（已按 D8 修正，逐条标注去留）

| # | 原结论 | 状态 |
|---|---|---|
| 1 | 挂死实例恢复段 352.7 s，force stop 257.2 s 占 72.9% | **数字成立但归因错**。252–257 s 是 `Force=True` 走完优雅关机超时；换 `SkipOsShutdown=True` 后恢复段 21.8 s |
| 2 | 健康实例 force stop 16.8 s，差 15.3 倍 | **撤销倍数**。D8 以 n=3 复测，健康 guest 四种模式范围全重叠（5.5–20.5 s），模式差异不成立 |
| 3 | 端到端约 6 分 8 秒，与 auto recovery 持平 | **撤销**。正确参数下端到端约 **37 s**，对客户观测的 6–7 分钟约 10–11 倍 |
| 4 | 生产中要 stop 的正是挂死实例，所以恒定落在 257 s 档 | **撤销**。挂死实例用对参数只需 11.5 s |
| 5 | 要压缩总时长必须避开对故障实例 stop | **撤销前提**。stop 本身不慢，慢的是错参数。ASG 替换与备机池不再是必需，可作为可选优化 |
| 6 | `auto_recovery=default` 与自建编排本例未相撞（guest 侧故障不激活 AWS 那一侧） | **成立** |
| 7 | 编排器 `wait_status_ok` 串行导致 boot 段与 app 段无法分离 | **成立，且已修**。并发后实测 `running → app_ready` 7.014 s |
| 8 | `running → status ok` 方差大（88.6 / 121.8 s），不能当服务可用判据 | **成立**。这两个值本身是 status check 的真实耗时，与 app_ready 无关 |

第 8 条值得保留强调：status check 从 `running` 到 `ok` 确实要 88–122 秒且方差大，
而应用在 7 秒就能服务。**两者差一个数量级以上，用 status check 当恢复完成判据
会白等一分半以上。**
