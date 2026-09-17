> **适用范围已被 D10 收窄（2026-09-17）。** 本报告的结论只对 `recover` 一种动作成立。
> D10 实测 `reboot` / `stop` / `terminate` **三种动作都能被 `set-alarm-state` 触发**
> （reboot 由 uptime 回退证实，stop/terminate 由 t+1 s 状态转换证实，
> CloudTrail 发起方为 `events.amazonaws.com`）。
> 原文把只测了一种动作的结果写成了对所有告警动作成立，是过度概括。
> 仍然成立的是「`Action completed successfully` 只描述投递不描述执行」——
> 而且 D10 给了更强证据：同一句话在三种动作上对应真实执行，在 `recover` 上什么都没发生。
> 详见 `D10-metric-latency-volume-type-alarm-actions.md`。

---

# 验证报告 D3 —— 恢复链路耗时：用例被实测推翻

执行时间：2026-09-17 06:02:23 – 06:12 UTC
原始数据：`validation/raw/D3/`
**结论：`set-alarm-state` 无法用来测量 auto recovery 的恢复链路。用例 D3 按原设计不成立。**

---

## 执行过程

| 时刻 | 事件 |
|---|---|
| 06:02:23.091 | 告警创建（`StatusCheckFailed_System` / Minimum / period 60 / 1 个评估周期 / 阈值 1，动作 `arn:aws:automate:ap-northeast-1:ec2:recover`） |
| 06:03:23.860 | 告警 `INSUFFICIENT_DATA → OK`（**75 秒**才拿到第一个数据点，证明它能取到数据） |
| 06:03:51.259 | `t_action`：`set-alarm-state --state-value ALARM` |
| 06:03:51.683 | 告警 `OK → ALARM` |
| **06:03:52.901** | 告警历史：**`Action completed successfully`** |
| 06:05:39.056 | 告警自行回到 `OK`（`set-alarm-state` 只是临时状态，下个评估周期恢复真实值） |

告警动作在 1.6 秒内报告成功。**然后什么都没有发生。**

## 四条独立证据证明实例从未被恢复

| 证据 | 观测 |
|---|---|
| 取证器 1 秒粒度轮询，8 分钟 | `status_transition` **1 条**、`lifecycle_transition` **1 条**，都是触发前 06:03:44 的基线；触发后**零转换** |
| 探测器 547 轮 | 非 `HEALTHY` 轮数 **0** |
| tick 计数器 | 8825 → 19721 单调递增，**回退次数 0** —— 进程从未重启 |
| `DescribeInstanceStatus.Events` | `null` —— AWS 侧没登记任何恢复/维护事件 |
| **CloudTrail** | 窗口内 **`RecoverInstances` 0 条、`RebootInstances` 0 条**；唯一的 `StopInstances`/`StartInstances` 是 05:55:55 / 05:56:13 我自己 D4b 编排器的调用（`Username=Radium`） |

CloudTrail 是决定性的：EC2 侧**没有产生任何 API 调用**。

---

## 机制与「误导性成功信号」

`Action completed successfully` 描述的是 **CloudWatch 成功把动作投递给了 EC2**，
不是 **EC2 执行了迁移**。两者被同一句话覆盖，而它们的差别正是这次要测的东西。

EC2 的 recover 动作看起来会校验实例是否真的处于受损状态：
本例 `StatusCheckFailed_System` 真实值是 0（宿主机确实健康），
所以 EC2 拒绝/忽略了这次恢复请求，且不产生任何可见记录。

### 需要限定的地方，不要过度推广

设计 D3 时我引用了 AWS Incident Detection and Response 文档的警告
——「测试前先 `disable-alarm-actions` 以免意外重启实例」——推断 `set-alarm-state`
会触发 recover 动作。**这个推断只对了一半**：动作确实被投递了（告警历史可证），
但 recover 这一种动作对健康实例是空操作。

本次**只测了 recover 一种动作**。`reboot` / `stop` / `terminate` 这三种告警动作
是否同样被校验，**未测**（`[待测]`）。那条文档警告对它们可能完全成立，
所以不要因为本次结果就认为 `set-alarm-state` 对所有告警动作都安全。

---

## 对客户问题的影响

客户的 6–7 分钟是「检测段 + 恢复段」之和。本次演练已经量到检测段
（D1：API 通道 255 秒 / 指标通道 319 秒）。**恢复段量不到**：

- `StatusCheckFailed_System` 注入不了（FIS 无此动作，测的是宿主机侧）
- 强制告警进 ALARM 也触发不了 recover（本报告）

**所以 AWS auto recovery 的真实恢复段耗时，只能等一次真实宿主机故障来测。**
这把 D7（常驻取证）从「可选的长期项」变成了**唯一可行的路径**：
取证器现在就得常驻起来，下一次真实故障才能给出那一半的数字。

可用的替代对照是自建路径的实测值（D4 / D4b）：

| 路径 | 恢复段 | 取证等级 |
|---|---|---|
| **自建 stop/start，`SkipOsShutdown=True`（挂死 guest）** | **21.8 s** | `[实测]` D8，n=2 |
| 自建 stop/start，`Force=True`（挂死 guest，错的参数） | 263.1 s | `[实测]` D8，n=2 |
| AWS auto recovery | — | **无法注入，需等真实故障** |

---

## 用例文档已修正

`design/03-fis-drill-cases.md` 里 D3 原写「能注入 → `set-alarm-state`」，
以及可注入性总表里「恢复动作链路（告警→recover）能注入」两处均已改为
**不能，附本次证据**。留着原判断会让下一个人重复这次白跑。

## 花费与副作用

- 本次**未消耗**实例的每日恢复次数额度（因为没有真的发生恢复）。
- 1 秒粒度轮询 `DescribeInstanceStatus` + `DescribeInstances` 在 CloudTrail 里
  产生了可观的事件量（20 分钟窗口的 lookup 直接被自己的 Describe 事件填满并触发
  `ThrottlingException`）。生产常驻取证要考虑这部分 CloudTrail 数据事件的成本，
  以及给 lookup 类查询留出限流退避。
