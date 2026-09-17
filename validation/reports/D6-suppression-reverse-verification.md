# 验证报告 D6 —— 误动作抑制机制反向验证

执行时间：2026-09-17 05:21–05:24 UTC
原始数据：`validation/raw/D6/`（`prober-preflight-block.ndjson`、`prober-suppress.ndjson`、`prober-restored.ndjson`）

## 为什么必须做这一步

`PROBER_SIDE` 抑制是整套自愈的最后一道闸：探测侧自己坏了的时候，
它负责阻止编排器去重启一台其实健康的区服。一个区服 = 一整批在线玩家，
误动作的代价比漏动作大。

而这类「结构上永不触发」的门禁是最危险的一种缺陷 —— 它给人以为有保护的错觉。
所以判据是**注入真缺陷必须被抓到**，不是「代码里写了这一支」。
注入手段：同时收回 sentinel 与被测实例的入站规则，让两者一起不可达。

## 断言 1：默认必须拒绝启动　`[实测] PASS`

| 项 | 结果 |
|---|---|
| `preflight_sentinel.healthy` | `false` |
| 事件 | `fatal / sentinel_not_reachable` |
| 退出码 | **3**（期望 3） |
| 是否进入探测循环 | 否 |

sentinel 不可达时探测器直接拒绝启动，而不是带着一个死掉的对照静默运行。
这一支正是为了防止上一轮发现的那个真实场景：账号里现有 EC2 原状全不可达，
若允许静默降级，探测器上线后会把每一次真实故障都判成 `PROBER_SIDE`。

## 断言 2：显式无对照运行时必须抑制动作　`[实测] PASS`

用 `--no-require-sentinel-up` 明确接受无对照运行（把这个决定留下痕迹），跑 12 轮：

| 项 | 结果 |
|---|---|
| 判定集合 | `{PROBER_SIDE}` —— 12 轮全部，无一例外 |
| `tcp.up` / `icmp.up` | 全 `false` |
| `sentinel_icmp.up` / `sentinel_tcp.up` | 全 `false` |
| 升级档位 | 停在 `soft`，**12 轮从未到 hard** |
| `action_request` 计数 | **0** |

`fail_streak` 一路涨到 12，仍然没有升级到 `hard` —— 因为 `PROBER_SIDE`
不在可动作集合里，`escalation()` 对它最高只给 `soft`。
如果这里出现了任何一次 `action_request`，抑制机制就是坏的，必须先修再继续，
绝不能带着坏的抑制机制去开 `--apply`。

## 复原验证　`[实测] PASS`

复原动作挂在 `trap EXIT` 上，保证脚本中途失败也不会把安全组留在收回状态。
复原后重跑阳性对照（空的抑制列表本身不能当作已复原的证据）：

| 项 | 结果 |
|---|---|
| ping 203.0.113.102（被测） | 0% 丢包，rtt avg 0.333 ms |
| ping 203.0.113.20（sentinel） | 0% 丢包，rtt avg 0.217 ms |
| 探测器退出码 | 0 |
| `preflight_sentinel.healthy` | `true` |
| 4 轮判定 | 全 `HEALTHY`，tick 5233→5293 推进 |
| 5 条入站规则 | 全部在位（`describe-security-group-rules` 核对） |

## 一个会让 teardown 失败的副作用

**安全组规则 ID 在 revoke / re-authorize 之后变了。** 收回再加回不是幂等操作，
AWS 生成的是全新的 `sgr-*`。台账里原先记的 teardown 目标 ID 已失效：

| 安全组 | 旧 ID（已失效） | 新 ID（当前在位） |
|---|---|---|
| `sg-SENTINEL000000001` icmp | `sgr-0bf2974ce96ffac26` | `sgr-06896e38d87786989` |
| `sg-SENTINEL000000001` tcp22 | `sgr-00391dc6a61054fb9` | `sgr-0e7efe28f48e34e8c` |
| `sg-REDACTED0000000007` tcp7777 | `sgr-0d21c8b1a07151865` | `sgr-026e122869f00c456` |
| `sg-REDACTED0000000007` tcp8080 | `sgr-09d9bc6ce5b85967e` | `sgr-05198268545445163` |
| `sg-REDACTED0000000007` icmp | `sgr-0f7f1dbdce3e22c53` | `sgr-0041786d3b6961745` |

被测实例的 SG 是本次新建的，teardown 时整组删除即可；
**sentinel 的两条是加在别人的既有安全组上的，必须按新 ID 精确 revoke，不能删组**。
台账已更新为新 ID。

## 结论

抑制机制经反向验证有效：默认拒绝无对照启动（exit 3），
显式无对照运行时 12/12 判 `PROBER_SIDE` 且零动作请求，升级档位封顶在 `soft`。
可以开 `--apply` 进入 D1 / D4。
