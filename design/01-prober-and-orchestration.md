# 探测器与恢复编排 —— 实现方案

## 1. 为什么不是「TCP 还是 ping」的二选一

问题问的是选哪一种。答案是**一个触发器 + 若干分类器**，因为这两件事要回答的问题不同：

- 触发器回答「要不要动作」
- 分类器回答「动作到哪一级」

把 TCP 当触发器会踩一个结构性假阴性：进程死锁、GC、检查点 stop-the-world 时，
内核的 accept queue 照样接住连接，TCP 探测一路绿灯。

**本地已实测证实**（`validation/reports/` 有原始输出）：桩程序主循环冻住后，
TCP 端口仍 `open` 并正常返回数据、HTTP `/health` 仍回 200，只有 `tick` 停在 60 不动。
纯 TCP 判据在这种状态下判健康，本探测器判 `APP_STUCK`。

## 2. 分层结构

```
                      ┌─────────────────────────────────────┐
   触发器（1-2s）      │ app  : GET /health -> tick 是否推进  │  ← 唯一的动作触发依据
                      └─────────────────────────────────────┘
                      ┌─────────────────────────────────────┐
   分类器             │ tcp  : connect 游戏端口              │  ← 区分「进程没了」
                      │ icmp : ping                          │  ← 区分「内核网络栈还活着」
                      │ sent : 同 AZ 同子网对照实例          │  ← 区分「探测侧坏了」
                      └─────────────────────────────────────┘
                      ┌─────────────────────────────────────┐
   旁路信号（不触发）  │ DescribeInstanceStatus 直接轮询      │  ← 比等告警快一到两分钟
                      │ StatusCheckFailed_* / EBS 指标       │
                      │ AWS Health / 实例事件                │
                      └─────────────────────────────────────┘
```

### 判据表（`shard_prober.py` 的分类逻辑，一支对一级动作）

| app tick | tcp | icmp | sentinel | 判定 | 动作阶梯 |
|---|---|---|---|---|---|
| 推进 | — | — | — | `HEALTHY` | 无 |
| 推进但 rtt 高 | — | — | — | `APP_SLOW` | 仅告警 |
| **冻住** | **通** | — | — | `APP_STUCK` | 重启服务 → 换宿主机 |
| 端点不通 | 通 | — | — | `APP_STUCK` | 重启服务 → 换宿主机 |
| — | 不通 | 通 | — | `APP_DEAD` | 重启服务 → 换宿主机 |
| — | 不通 | 不通 | **正常** | `HOST_DOWN` | 直接换宿主机 |
| — | 不通 | 不通 | **也不通** | `PROBER_SIDE` | **永不动作** |
| — | 不通 | 测不出 | 测不出 | `UNKNOWN` | **永不动作** |

`PROBER_SIDE` 与 `UNKNOWN` 永不升级到动作，是抑制误动作的最后一道闸。
一个区服 = 一整批在线玩家，误判的代价是把他们全踢下线，所以宁可漏动作也不能误动作。

### 这套判据的盲区：存储故障　`[实测]`

D2 实测：根卷 I/O 停 3 分钟，**探测器 427 轮全 `HEALTHY`**，tick 25408→33910 无回退，
ICMP 全程通，`instance_status` / `system_status` 始终 `ok`，玩家侧毫无感知。
原因是游戏主循环全在内存里跑，页缓存够用，不需要新的磁盘读。

这是整套方案里**唯一一个 EC2 侧信号优于应用侧探针**的场景（D1/D5 都是反过来），
必须靠旁路信号补上，而且要作为**独立触发条件**，不能只当归因材料：

| 优先级 | 判据 | 实测延迟 |
|---|---|---|
| 1 | `DescribeVolumeStatus` 的 `io-performance` 不为 `normal` | **+112.7 s** |
| 2 | `DescribeInstanceStatus` 的 `AttachedEbsStatus` | **+155.7 s** |
| 3 | `StatusCheckFailed_AttachedEBS` 告警 | +218.9 s |
| 不可用 | `VolumeStalledIOCheck` | **零数据点，永不触发** |
| 不可用 | `VolumeReadOps`/`WriteOps` 高于阈值 | 注入期归零，方向相反 |

**并且存在比「服务不可用」更糟的状态**：游戏还在推进 tick、玩家还在玩，
但所有检查点都失败，一局注定丢档的游戏。外部探针探不到这个，
只能由游戏服在写路径上自检：**健康端点必须额外暴露 `last_checkpoint_success_timestamp`**，
探针把它纳入判据（滞后超过检查点周期的若干倍即判 `CHECKPOINT_LAGGING`）。

> **这里原先写的是「即判 `APP_STUCK`」，是错的，已改正。**
> `APP_STUCK` 在 `ACTIONABLE` 集合里，会触发重启进程或 stop/start ——
> 而此刻最危险的东西正是那些未落盘的玩家进度，**任何动作都会把它彻底销毁**。
> `CHECKPOINT_LAGGING` 刻意**不在** `ACTIONABLE` 内：只告警，交人工决定
> （是先想办法把数据抢救出来，还是接受损失后重启）。
> 照原文实现会在丢档时自动执行重启，把可能还能救的进度一次性抹掉。

> **卷型硬前提（D10 实测）**：`io-performance` 仅 `io1` / `io2` / `gp3` 支持，
> **`gp2` 卷返回 `not-applicable`，这条判据完全失效**。
> 代码已按此显式列举状态集合并在启动时预检卷型 ——
> 早期版本判「不等于 `normal` 即故障」，会把 `not-applicable` 当成停滞，
> 对每个 gp2 卷触发一次 stop/start。详见 `validation/reports/D10-metric-latency-volume-type-alarm-actions.md`。

这两条是 D2 暴露的方案缺口，`shard_prober.py` 当前版本**尚未实现**（`[待测]`）：

- 增加 `--volume-ids` 与旁路轮询，`io-performance: stalled` 作独立触发条件
- 健康端点判据从「tick 推进」扩展为「tick 推进 **且** 检查点滞后正常」


## 3. sentinel 对照：本方案里最容易被做成装饰品的一环

对照的价值在于回答「是被测方坏了，还是我这边坏了」。它有两个必须满足的条件：

1. **对照必须先被证明是活的。** 对照目标在注入前就不可达时，「不可达 → 不可达」什么也证明不了。
2. **对照失败必须真的能抑制动作**，且这个抑制要被反向验证过（演练用例 D6）。

**本次实测踩到的真实缺陷**：账号里现有 EC2 直接拿来当 sentinel，在当前安全组下**全部不可达** ——
`sg-SENTINEL000000001`（sentinel-host）入站规则一条都没有；
`sg-REDACTED0000000001`（openclaw）只放行特定 SG 与 `11.0.0.0/16`，探测器所在网段两个条件都不匹配。

如果不做阳性对照就上线，后果是：sentinel 永远失败 → 每次真实故障都判 `PROBER_SIDE`
→ **整套自愈静默失效，而监控面板上一切正常**。这类「门禁在结构上永不触发」的缺陷
比没有门禁更危险，因为它给人以为有保护的错觉。

因此 `shard_prober.py` 把阳性对照做成**启动硬条件**：`--require-sentinel-up` 默认开，
sentinel 不可达就 exit 3 并打印修复提示，不允许静默降级。要无对照运行必须显式
`--no-require-sentinel-up`，把这个决定留下痕迹。

**已实测的对照配置**：sentinel `203.0.113.20`（sentinel-host，同子网 `subnet-EXAMPLE0000001`、
同 AZ `ap-northeast-1a`、按记录长期零流量），补最小入站规则后 ICMP 0% 丢包 / RTT 0.219ms、TCP 22 open。

## 4. 部署形态

| 组件 | 位置 | 数量 | 理由 |
|---|---|---|---|
| `shard_prober.py` | 独立探测节点 | **≥3，跨 AZ** | 单探测点的一次网络抖动不能变成一次区服重启；2/3 quorum |
| `recovery_orchestrator.py` | 同探测节点或独立编排节点 | 1 主 | 单飞锁保证同一实例同时只有一个编排 |
| `ec2_forensics.py` | 常驻，任意有 API 权限的节点 | 1 | 只读，采全部区服 |
| `game_stub.py` | 仅演练用 | — | 生产由游戏服自己实现同形状的 `/health` |

**带内 agent 只能是补充信号。** 网络一断它就静默，而「静默」与「它自己挂了」分不开。
它的价值在故障后归因：`dmesg` / nvme I/O error、ENA 限速计数、内存压力 ——
用来区分「宿主机坏了」和「游戏进程自己 OOM 了」。

**quorum 的落地方式**（本轮未实现，`[待测]`）：每个探测器把判定写进一个共享位置
（DynamoDB 条件写 / 一个小型 HTTP 端点），编排器动作前读三票，
`HOST_DOWN` 需 ≥2 票且无 `PROBER_SIDE` 票。本轮验证用单探测器 + sentinel 对照，
quorum 属于生产化时必须补上的一层。

## 5. 阈值：从这个区服自己的历史分布推，不要拍整数

| 参数 | 默认 | 依据 |
|---|---|---|
| `--interval` | 1.0s | 检测下限由它决定 |
| `--soft-consecutive` | 3 | 3 次 ≈ 3s，只告警 |
| `--hard-consecutive` | 5 | 允许动作 |
| `--hard-min-seconds` | 8.0 | **和次数是「与」关系** |
| `--tick-stall-seconds` | 6.0 | 必须 > 该区服检查点与 GC 停顿的 P99.9 |
| `--action-cooldown` | 600s | 防止把一次故障放大成一串重启 |

`hard` 同时要求「连续次数」和「已持续时长」，是因为只用次数时，
一串 50ms 内连续失败的探针就能立刻触发一次区服重启。

`tick_stall_seconds` 必须实测：跑一段正常业务，统计 tick 间隔的分布，
取观测最大值的倍数，并记录「这个阈值在历史上会误触发多少次」。
**不要选一个看起来安全的整数**。

## 6. 恢复编排：为什么不等 auto recovery

| | auto recovery | 自建 stop(SkipOsShutdown) + start |
|---|---|---|
| 保留实例 ID / IP / EBS | 是 | 是（stop/start 不换 ID/IP，EBS 跟着走） |
| 换到新硬件 | 是（原地迁移） | 是 |
| 时长 | **AWS 不承诺**，`[文档]` 列出失败情形：服务事件期间不运行、替换硬件容量不足、当日恢复次数达上限 | 自己可控可测 |
| 触发延迟 | 检测 60s 周期 + 指标 1min 粒度 + 告警评估 | 探针 1s + 判据 8s |
| 内存 | 丢 | 丢 |

客户明确「能容忍内存丢失」，等于宣布他们不需要 auto recovery 最贵的那个卖点里
最花时间的部分。所以主路径自己做，auto recovery 留作兜底。

### 动作阶梯

```
APP_STUCK / APP_DEAD ──► SSM systemctl restart <service>
                          └─ 未在超时内 app ready ──► stop(SkipOsShutdown=True) + start
HOST_DOWN ─────────────► stop(SkipOsShutdown=True) + start
STORAGE_STALLED ───────► stop(SkipOsShutdown=True) + start（刻意不含重启进程）
PROBER_SIDE / UNKNOWN ─► 不动作
```

**这里原先写的是 `--force`，是错的，已按 D8 实测改正。**
`Force=True` 并不跳过优雅关机 —— API 参考原文是「先尝试优雅关机、超时后才硬下电」，
所以对一个关不掉的 guest 仍要走完约 252 秒超时。真正绕过 OS 关机的参数是
**`SkipOsShutdown=True`**，实测 11.5 秒，**22 倍差异**。
跳过 OS 关机的代价是可能丢在途写入，
客户已声明容忍内存丢失、文件系统用 journaling 即可接受。
编排器的升级路径因此是「升级到 `SkipOsShutdown`」而不是「升级到 `Force`」，
代码里 `--skip-os-shutdown` 默认开、`--force-stop` 默认关，升级动作单独打点。

**不做成「先普通 stop、超时再升级」的两段式**，理由是 D11 实测：
「应用冻死但 OS 正常」这一档普通 stop 要 103.4 秒（systemd 等满 90 秒 `TimeoutStopSec`），
两段式等于先白等约 100 秒再做本来第一步就该做的事。
也没有「留一次最后存盘机会」的收益 —— `APP_STUCK` 时主循环已冻住、关机钩子跑不起来，
`APP_DEAD` 时进程已经没了，而 `stop_start` 只在应用已经不工作时才被调用。

因此默认配置下升级分支不可达，这是**刻意的**；该分支只为显式传 `--no-skip-os-shutdown` 的用户存在。
已用 `SkipOsShutdown` 仍超时时记 `escalation_exhausted`（`Force` 比它弱，不存在更强选项）。

### 验收判据：`running` 不等于可服务

编排器的成功判据是 **`app_ready`：健康端点的 tick 在推进**，不是实例 `running`，
也不是 status check `ok`。恢复总时长里 OS 启动 + 游戏进程加载往往比检测那一段更长，
必须单独量，这才是客户能自己优化的部分。

编排器把恢复段拆成可分别优化的子段并逐段打点：
`stop_api → stopped → start_api → running → app_endpoint_up → app_ready`。

### 安全机制

| 机制 | 作用 | 缺了会怎样 |
|---|---|---|
| 默认 dry-run | 只有 `--apply` 才动手 | 一个参数写错就重启生产区服 |
| 单飞锁（O_EXCL + PID 存活校验） | 同实例同时只一个编排 | 两个编排同时 stop/start，互相打断 |
| 冷却期 | 两次动作最小间隔 | 一次故障被放大成一串重启 |
| 每日上限 | 超了拒绝并要人工介入 | 反复重启掩盖真实根因 |
| `race_warning` | 动作前读 `MaintenanceOptions.AutoRecovery` | 你的 stop 和 AWS 的迁移同时进行，行为未知 |

**auto recovery race 必须实测，不能纸面推断。** 编排器只警告不改配置；
若确定自己接管全部恢复，用
`modify-instance-maintenance-options --auto-recovery disabled` 显式关掉。

## 7. 更快的两条路（本方案未实现，列出取舍）

| 方案 | 预期恢复时长 | 代价 |
|---|---|---|
| 自建 stop(SkipOsShutdown) + start（本方案） | `[实测]` **21.8 s** | 无额外资源 |
| ASG min=max=1 + 自定义健康检查 | 2-3 min `[观测值]`，来自第三方博客非 AWS 承诺 | 实例 ID/IP 变，需 EIP 重绑或 NLB/DNS；状态必须外置 |
| 备机池 + 数据卷 detach/attach + EIP 迁移 | 理论 60-90s `[待测]` | 从真坏的宿主机上 detach 卷本身可能 hang，需 force detach，必须实测 |

三条路的共同前提是**存档数据不在实例的易失部分**。客户「一台 EC2 一个区服」
若把存档放在 root volume 上，前两条路都要先解决数据归属问题。

## 8. 已验证 / 待验证的分界

`[实测]` 本次已完成：
- 五个脚本语法通过，探测器健康路径与 `APP_STUCK` 路径本地跑通
- TCP 通 + tick 冻住 → `APP_STUCK`，同时 TCP 判据判健康（核心设计主张成立）
- soft → hard 升级、动作请求、冷却抑制的完整序列
- sentinel 阳性对照：三台现有 EC2 原状全不可达，补最小入站规则后可达
- 东京区环境：VPC / 子网 / 密钥 / AMI / m7i-m8i 可用性 / FIS 角色现状

`[待测]` 需要在真机上完成：
- 被测实例上的端到端探测（跨主机而非 localhost）
- 编排器 `--apply` 的真实 stop/start 分段耗时
- instance status check 的真实上报延迟（演练 D1）
- attached EBS check 与 `VolumeStalledIOCheck`（演练 D2，需补 FIS 的 EBS 权限）
- 恢复链路耗时（演练 D3）
- auto recovery 与自建编排的 race 行为
- 多探测器 quorum

---

## 9. 真机验证结果（2026-09-17，东京区）

以上 `[待测]` 项已全部执行，结论见 `validation/reports/00-summary.md`。要点：

| 项 | 结果 |
|---|---|
| 跨主机端到端探测 | `[实测]` 通过，app rtt 1.5–3.8 ms |
| 检测段 | `[实测]` 应用侧 **3.5 s** / 动作请求 **15.5 s**；EC2 侧 API **255 s**、指标 **319 s** |
| 指标发布延迟 | `[实测]` **约 84 s**，D1 与 D2 两次独立印证 |
| 恢复段（挂死 guest，`SkipOsShutdown=True`） | `[实测]` **21.8 s**（stop 11.5 + start 3.3 + app_ready 7.0），D8 n=2 |
| 恢复段（挂死 guest，`Force=True`＝错的参数） | `[实测]` **263.1 s**。`Force` 不跳过优雅关机，对关不掉的 guest 要走完约 252 s 超时 |
| 停止参数的影响 | `[实测]` 三档：**应用冻死、OS 正常** 上 **18.2 倍**（103.4 s 对 5.7 s，D11）；挂死 guest 上 **22 倍**（252.2 s 对 11.5 s，D8）；健康 guest 上四种模式无法区分（n=3 范围全重叠）。差异在「应用不响应 `SIGTERM`」时暴露，而恢复动作从不作用于健康实例 |
| attached EBS check | `[实测]` +155.7 s；卷状态 API 更早，+112.7 s |
| `VolumeStalledIOCheck` | `[实测]` **零数据点，与 AWS 文档矛盾，不可用** |
| auto recovery race | `[实测]` 本例未相撞（guest 侧故障不激活 AWS 那一侧）；宿主机真故障时的相撞行为仍 `[待测]` |
| 恢复链路耗时（D3） | **用例被推翻**，`set-alarm-state` 触发不了 recover，只能等真实故障 |
| 多探测器 quorum | 仍 `[待测]`，本次用单探测器 + sentinel 对照 |

**第 7 节的预期值已由实测确认，但依赖一个当时没写的前提**：
「自建 stop + start」那一行原先写的「目标 < 3 min」实测达到了 —— 挂死 guest
用 `SkipOsShutdown=True` 恢复段 **21.8 秒**，端到端约 37 秒。
但前提是**停止调用必须用 `SkipOsShutdown=True`**：
用 `Force=True` 是 263.1 秒，因为 `Force` 只是「先试优雅关机、超时后硬下电」，
对关不掉的 guest 要把约 252 秒的超时整个走完。

第二轮曾一度得出「自建路径恢复段没有改善」的错误结论并写进报告，
根因是只测了 `Force=True` 一种参数、只测一次，且被自己
`wait_status_ok` 串行缺陷造成的 89.7 s 误导。该结论已撤销，
完整经过见 `validation/reports/D8-stop-mode-matrix.md`。

**因此第 7 节的路线取舍也要更新**：ASG 替换与备机池**不再是必需**，
因为 stop 本身不慢；它们降级为可选优化，值得做但不阻塞主路径。

**新增一条必须纠正的客户假设**：auto recovery 只对 system status check 失败动作，
而内核挂死 / OOM / 文件系统损坏 / 驱动 hang 只让 instance status check 失败。
这类故障下实例会一直 `running` + `impaired`，**永远等不到自动恢复**。
所以自建探测 + 编排不是「优化」，而是补上一个本来就不存在的兜底。

