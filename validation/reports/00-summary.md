# 总结报告 —— EC2 故障检测与恢复实测

场景：游戏客户纯 EC2 部署，**一台 EC2 = 一个区服**，无高可用设计，靠云厂商自动恢复，
能容忍内存丢失但要求恢复快。诉求是压缩「应用感知故障 → 服务恢复可用」的总时长。

演练环境：东京 `ap-northeast-1`，被测实例 `i-TARGET00000000001` m7i.large / AL2023 x86，
探测器与 sentinel 同子网同 AZ。执行时间 2026-09-17 05:15–06:25 UTC。
全部数字来自真机实测，原始数据在 `validation/raw/`，逐用例报告在同目录。

---

## 一、客户两个抱怨的实测答案

### 抱怨 1「system status check 报警时间慢，比应用慢」

**实测证实，且比"慢"更严重。** 同一次故障（kernel panic 挂死）的四个通道：

| 通道 | 相对故障时刻 | 倍数 |
|---|---|---|
| 应用侧探针判定故障 | **3.5 s** | 1× |
| 应用侧探针请求恢复动作 | **15.5 s** | 4× |
| `DescribeInstanceStatus` 直接轮询看到 `impaired` | **255.0 s** | **73×** |
| CloudWatch 指标可见（告警能触发的最早时刻） | **319.2 s** | **91×** |

其中指标发布延迟单独占 **84 秒**（D1 测 83.96 s、D2 测 83.1 s，两次独立印证）。

**三条比"慢"更严重的发现：**

1. **短于检查周期的故障根本不上报。** 第一次注入 panic 时 AL2023 默认 `kernel.panic=5`
   触发自动重启，故障窗口约 12 秒。取证器以 1 秒粒度轮完整个窗口，
   EC2 侧**零上报**——没有 impaired 转换，没有值为 1 的指标数据点，
   生命周期状态也始终 `running`。**不是报得慢，是根本不报。**

2. **`StatusCheckFailed_System` 对 guest 侧故障永远不会失败。** 内核挂死 5 分钟里
   `system_status` 始终 `ok`，只有 `instance_status` 变 `impaired` —— 这是对的，
   宿主机确实没坏。但客户要 ST 改进的正是 system status check，
   而它对这一整类故障（内核挂死、OOM、文件系统损坏、驱动 hang）本就不该报。

3. **AWS Health 事件通道不可用。** 该账号无 Business/Enterprise 支持计划，
   `DescribeEvents` 返回 `SubscriptionRequiredException`。
   任何依赖 Health 事件提前得知宿主机降级的方案在这个等级下不成立。

### 抱怨 2「自动恢复时间不可控，成功也要 6-7 分钟」

**实测证实，并且找到了可以拿回来的那一段 —— 关键在用对停止参数。**

> 本节结论在第二轮被修正过。第一轮曾报「恢复段没有改善」，
> 那是因为用错了 API 参数、且被自己的测量缺陷误导。完整经过见
> `D8-stop-mode-matrix.md`，错误痕迹保留在 `D4-recovery-segments.md`。

#### 停止参数决定一切（挂死 guest，n=2/组）

| 停止模式 | min | median | max | 组内极差 |
|---|---|---|---|---|
| `Force=True` | 250.318 | **252.197 s** | 254.076 | 3.8 s |
| **`SkipOsShutdown=True`** | 10.838 | **11.453 s** | 12.067 | 1.2 s |

**22 倍差异。** 机制（`[文档]` `StopInstances` API 参考）：

- `Force=True` **不跳过**优雅关机 —— 原文是「will first attempt a graceful shutdown ...
  if the graceful shutdown fails to complete within the timeout period, shuts down forcibly」。
  对一个内核已 panic、永远完不成关机的实例，它必须把那个超时整个走完。
- `SkipOsShutdown=True` 才是真正绕过 OS 关机流程的参数。

#### 恢复段正确构成（挂死 guest，用 `SkipOsShutdown`）

| 分段 | 中位数 |
|---|---|
| `StopInstances` API 返回 | 0.4 s |
| stopping → stopped | **11.5 s** |
| stopped → running | 3.3 s |
| **running → app_ready（应用真正可服务）** | **7.0 s**（n=12，范围 6.0–7.1） |
| **恢复段合计** | **21.8 s** |

`running → app_ready` 只要 7 秒这一点也是第二轮才测准的：第一轮的编排器把
`wait_status_ok` 串行在 `wait_app_ready` 前面，把这一段测成了 89.7 s / 121.8 s，
13–17 倍虚高。已改为并发，并以 app_ready 为恢复完成判据。

顺带一个仍然成立的重要事实：**status check 从 `running` 到 `ok` 要 88–122 秒且方差大，
而应用在 7 秒就能服务。用 status check 当恢复完成判据会白等一分半以上。**

#### 端到端

| | 值 |
|---|---|
| 检测段（应用侧探针到请求动作） | **15.5 s** |
| 恢复段（`SkipOsShutdown`） | **21.8 s** |
| **合计** | **约 37 s** |
| 客户观测的 AWS auto recovery | 6–7 分钟 |
| **比值** | **约 10–11 倍** |

**结论：检测段与恢复段分别有 16.5 倍与 12.1 倍改善。** 不是只有检测段能优化。

#### 代价与前提

`SkipOsShutdown=True` 跳过 OS 关机，可能丢内存内容与在途 I/O、跳过关机脚本。
客户已声明容忍内存丢失、文件系统用 journaling 即可接受，
但**检查点一致性必须由游戏服自己保证**（周期性检查点 + 启动时校验），
不能指望关机流程帮它刷缓存。

### AWS auto recovery 自身的恢复段：测不到

`set-alarm-state` 强制告警进 ALARM 后，告警历史报 `Action completed successfully`，
但实例**从未被恢复**（CloudTrail 零 `RecoverInstances`、1 秒轮询零状态转换、
tick 单调无回退、`Events` 为 `null`）。recover 动作对系统检查真实通过的实例是空操作，
那句成功只描述 CloudWatch 的投递，不描述 EC2 的执行。

**所以 AWS 恢复段只能等一次真实宿主机故障来测。** 这使常驻取证从可选项变成唯一路径。

---

## 二、探测判据选型：实测判决

### TCP connect 不能当存活触发器

`SIGSTOP` 让进程进入 `State: T (stopped)` 后，**TCP connect 连续 6 秒仍返回 `open`** ——
内核用 listen backlog 代替一个冻死的进程完成了三次握手；backlog 填满后才开始 timeout。
假阴性窗口的长度由 backlog 深度和探测频率决定，**与进程健康无关**。

### HTTP 200 也不够

只冻主循环（HTTP/TCP 线程继续服务）时，**连续 12 轮 HTTP 200 且 TCP `open`，
而 tick 冻在 300 不动**。纯 TCP 判据和纯 HTTP-200 判据在这 12 轮里全部判健康。

**判据必须是响应体内容在推进，不是响应码。** 检测时序：
tick 冻住 → 判定翻转 5 s → soft 2 s → hard 4 s → 请求动作，**合计约 11 秒**。

### ICMP 的作用是分类，不是检测

它把 `APP_DEAD`（进程没了，ICMP 通）与 `HOST_DOWN`（宿主机没了，ICMP 不通）区分开，
从而决定"重启进程"还是"换宿主机"。

### 存储故障是应用探针的盲区 —— 唯一需要反向依赖 EC2 侧的场景

根卷 I/O 停 3 分钟，**探测器 427 轮全 `HEALTHY`**，tick 25408→33910 无回退，
玩家侧毫无感知。因为游戏主循环全在内存里跑，页缓存够用，不需要新的磁盘读。

存储故障必须靠旁路信号，实测有效性排序：

| 优先级 | 判据 | 延迟 |
|---|---|---|
| 1 | `DescribeVolumeStatus` 的 `io-performance: stalled` | **+112.7 s** |
| 2 | `DescribeInstanceStatus` 的 `AttachedEbsStatus` | **+155.7 s** |
| 3 | `StatusCheckFailed_AttachedEBS` 告警 | **+218.9 s** |
| 不可用 | `VolumeStalledIOCheck` | **永不触发**（见下） |
| 不可用 | `VolumeReadOps`/`WriteOps` 高于阈值 | **永不触发**，注入期归零方向相反 |

**卷型硬前提（D10 实测）**：`io-performance` 只有 `io1` / `io2` / `gp3` 卷才有，
**`gp2` 卷返回 `not-applicable`，这条判据完全失效**。老实例根卷默认往往是 gp2，
上线前必须逐台核实。

我们在这里犯过一个会造成误动作的错误并已修复：原代码判「状态不等于 `normal`
即故障」，于是 `not-applicable` 被当成停滞，**每个 gp2 卷都会被触发一次
stop/start**，而日志显示「检测到存储停滞」，外观完全正常。
已改为显式列举状态集合 + 启动时预检卷型，用真 gp2 与 gp3 卷五条断言反向验证。

**并且存在比"服务不可用"更糟的状态**：游戏还在推进 tick、玩家还在玩，
但所有检查点都失败，一局注定丢档的游戏。这个从外部探不到，
必须由游戏服在写路径上自检并反映到健康端点（例如暴露 `last_checkpoint_success_timestamp`）。

---

## 三、五个会造成监控盲区的陷阱（全部实测）

| # | 陷阱 | 后果 |
|---|---|---|
| 1 | **`VolumeStalledIOCheck` 零数据点** —— `list-metrics` 列得出，`get-metric-data` 返回 0 个点，而同期卷状态明确 `stalled`。与 AWS 文档明文推荐矛盾 | 建在它上的告警永久 `INSUFFICIENT_DATA`；配 `--treat-missing-data notBreaching` 会一直显示正常 |
| 2 | **sentinel 对照默认是死的** —— 账号里现有 EC2 原状全不可达（一个 SG 入站规则为空，另一个只放行别的网段） | 探测器把每次真实故障判成"探测侧问题"而抑制动作，**自愈静默失效且外观正常** |
| 3 | **`describe_instance_status` 默认只返回异常实例** | 健康实例返回空列表，空与"实例不见了"无法区分 |
| 4 | **`Action completed successfully` 只描述投递** | 会让人以为恢复链路已验证，实则什么都没发生 |
| 5 | **`revoke` + `authorize` 不幂等** | 安全组规则 ID 全变，按旧 ID 写的清理脚本会失败 |

陷阱 1 与 2 是同一类：**门禁在结构上永不触发，比没有门禁更危险**，
因为它给人以为有保护的错觉。两者都只能靠反向验证发现 ——
本次 D6 用"注入真缺陷必须被抓到"证实了抑制机制有效（12/12 判 `PROBER_SIDE`、
零动作请求、默认模式 exit 3 拒绝启动）。

---

## 四、给客户的建议

### 立即可做，收益最大（检测段 255 s → 15.5 s）

1. **游戏服暴露带推进计数的健康端点。** 判据是 tick 推进，不是 HTTP 200，
   不是 TCP 可连。额外暴露 `last_checkpoint_success_timestamp` 以覆盖存储故障。
2. **自建探测器，1–2 秒间隔，≥3 个探测点跨 AZ，2/3 quorum。**
   TCP 与 ICMP 作分类器决定动作级别，不作触发器。
3. **sentinel 对照做成启动硬条件**，并定期反向验证抑制机制真的会触发。
4. **旁路信号直接轮询 `DescribeInstanceStatus` 与 `DescribeVolumeStatus`，
   不要等 CloudWatch 告警** —— 实测分别快 64 秒和 63 秒。

### 恢复段：用对停止参数就够，不必先上 ASG 或备机池

> 第一轮这一节写的是「不要指望 stop/start，必须避开对故障实例 stop」，已撤销。
> 那个判断建立在错误的参数选择上。

| 路径 | 恢复段 | 状态 |
|---|---|---|
| **自建 stop/start，`SkipOsShutdown=True`** | **21.8 s** | `[实测]` n=2 |
| 自建 stop/start，`Force=True`（错的参数） | 263.1 s | `[实测]` n=2，作为反例保留 |
| ASG 替换（terminate + 新实例） | — | `[待测]`，**不再是必需**，可作为后续优化 |
| 备机池 + 卷 detach/attach | — | `[待测]`，同上；原先担心的「force detach 撞超时」风险也不再是阻塞项 |

**要做的事只有一件：把编排器的停止调用改成 `SkipOsShutdown=True`。**
`--skip-os-shutdown` 已实现并默认开启，`--force-stop` 默认关闭。
该默认值的依据在 D11 中被换掉了：原先拿健康实例那组（四模式无法区分）论证，
而健康实例的应用会响应 `SIGTERM`、从来碰不到 systemd 超时 —— 用错了条件。
D11 补测「应用冻死但 OS 正常」这一档，普通 stop **103.4 秒**对 **5.7 秒**，结论不变但依据成立了。

代价是跳过 OS 关机流程，所以**检查点一致性必须由游戏服自己保证**：
周期性检查点 + 启动时校验存档完整性。这是这条路唯一需要应用侧配合的地方。

### 必须纠正的一个假设

**客户当前"靠云厂商自动恢复兜底"的假设，对最常见的一类故障不成立。**
auto recovery 只对 system status check 失败动作，而内核挂死、OOM、
文件系统损坏、驱动 hang 都只让 instance status check 失败。
这类故障下实例会一直 `running` + `impaired`，**永远等不到自动恢复**，直到人工介入。

所以自建探测 + 编排不是"优化"，而是**补上一个本来就不存在的兜底**。

### 现在就该做的一件长期事

**把常驻取证器起起来。** AWS 侧的检测段与恢复段都无法注入，
只能等真实故障。取证器同时记三个时间轴（`t_fault` / `t_datapoint` / `t_observed`），
下一次真实宿主机故障就能给出精确分段表 ——
拿这个跟服务团队谈"报得太慢"，比"观察下来 6-7 分钟"有说服力得多，
因为它能指出慢在哪一段。

---

## 四点五、客户视角复核：上生产的硬前提与可后补项

把这套东西交给客户跑 N 个区服，逐条问「还缺什么、缺了会怎样」。
判定分两档：**硬前提**（缺了这套东西不该上生产）与**可后补**（值得做但不阻塞）。

### 硬前提

| # | 项 | 缺了会怎样 | 状态 |
|---|---|---|---|
| 1 | **探测器自身存活可观测** | 探测器主机一死，所有区服**静默**失去监控，面板上什么都不变。误判至少看得见，这个看不见 | 已实现并验证：`--heartbeat-namespace` 发 `ProberHeartbeat` / `ShardUnhealthy`，实测有真实数据点。自定义指标发布延迟实测 **0.871 s**（n=5，对比 AWS 托管指标 84 s，约 95 倍），但告警仍是分钟量级 —— 瓶颈在告警评估周期不在发布延迟。告警必须配「缺数据即报警」 |
| 2 | **sentinel 对照 + 启动硬条件** | 对照是死的时候，每次真实故障都被判成 `PROBER_SIDE` 而抑制动作，自愈静默失效 | 已实现，D6 反向验证，且在 D9 一个没计划的场合真实挡住过一次运行 |
| 3 | **健康端点暴露推进计数 + 检查点滞后** | 只有 HTTP 200／TCP 可连的话，进程冻死时探不到（D5/D5b 实测连续 12 轮假健康）；没有检查点滞后的话，玩家会在存储故障期玩一局注定丢档的游戏 | 探测器侧已实现，桩程序已示范；**游戏服自己必须实现这两个字段** |
| 4 | **停止调用必须用 `SkipOsShutdown=True`** | 三档都实测了：**应用冻死、OS 正常** 时普通 stop 要 103.4 秒（systemd 等满 90 秒 `TimeoutStopSec`）而 `SkipOsShutdown` 只要 5.7 秒（**18.2 倍**，D11）；内核已死时 252.2 对 11.5 秒（22 倍，D8）；健康实例看不出差异。**恢复动作从不作用于健康实例**，所以默认开是对的 | 已实现为默认（`--skip-os-shutdown` 默认开、`--force-stop` 默认关） |
| 5 | **检查点一致性由应用保证**（周期性检查点 + 启动时校验） | `SkipOsShutdown` 跳过 OS 关机，不会帮你刷文件系统缓存。不周期性检查点就等于用「容忍内存丢失」换来了「丢玩家进度」 | **游戏服侧改动**，本方案只能提供检测（`CHECKPOINT_LAGGING`），修不了根因 |
| 6 | **现在就把常驻取证器起起来** | 客户抱怨的 system status check 那一档**注入不了**（FIS 无此动作，`set-alarm-state` 也触发不了 recover）。不常驻取证，下一次真实宿主机故障又只能得到「观察下来 6-7 分钟」 | 取证器已交付，客户需部署 |

第 3 与第 5 项是唯一两个**必须客户自己动手**的，其余都在交付物里。

### 可后补

| # | 项 | 为什么不阻塞 |
|---|---|---|
| 7 | 多探测器 quorum | 误动作已被 sentinel 对照 + 「连续次数 与 已持续时长」双档阈值压住（D6 反向验证）。探测器单点这个真风险由第 1 项覆盖。quorum 是风险再降一档，不是从无到有 |
| 8 | ASG 替换 / 备机池 | 原先以为必需，是因为误判了 stop 慢。实测 stop 只要 11.5 秒，主路径已够快，这两条降级为可选优化 |
| 9 | 降低 1 秒轮询的 CloudTrail 成本 | 稳态用 5–10 秒轮询即可（仍远快于 60 秒检查周期），怀疑故障时再切 1 秒。本次 20 分钟窗口的 CloudTrail lookup 被自己的 Describe 事件填满并触发限流，说明量确实可观 |
| 10 | 区分「卷自身受损」与「宿主机-卷可达性」 | 两者对应的动作不同（换宿主机 vs 从快照替换卷），但需要单独设计用例才能分辨。当前编排器对存储故障只做 stop/start 并显式记录「若卷本身受损则此动作无效，交人工」 |

### 无法消除，只能如实告知

**宿主机侧故障（system status check 失败）那一整档全部未验证。**
它注入不了，所以：AWS 侧的检测延迟、auto recovery 的恢复段耗时、
`SkipOsShutdown` 在宿主机不可达时是否同样有效、自建编排与 auto recovery 的
真实相撞行为 —— 这四件事都只能等一次真实故障。
本次所有恢复段数字都来自 **guest 内 kernel panic**（instance status check 失败），
与客户抱怨的那一档不是同一个故障域。

这不是方案的缺陷，是可注入性的边界。对应的动作就是第 6 项：现在就常驻取证。

## 五、用例执行清单


| 用例 | 内容 | 结果 | 报告 |
|---|---|---|---|
| D0 | 阳性对照与跨主机基线 | 通过 | `D0-D5-probe-criteria.md` |
| D5 | SIGSTOP 冻结整个进程 | 通过，结论强于预期 | 同上 |
| D5b | 只冻主循环（游戏服真实形态） | 通过 | 同上 |
| D6 | 误动作抑制反向验证 | 两个断言均通过 | `D6-suppression-reverse-verification.md` |
| D1 | instance check 上报延迟 | 通过（第二次注入） | `D1-status-check-latency.md` |
| D4 | 挂死实例恢复段 | 通过 | `D4-recovery-segments.md` |
| D4b | 健康实例恢复段对照 | 通过，归属确定 | 同上 |
| D3 | 恢复链路耗时 | **用例被推翻**，已修正文档 | `D3-recovery-chain-not-injectable.md` |
| D2 | attached EBS 状态检查 | 通过，两条重要发现 | `D2-ebs-status-check.md` |
| D7 | system check 被动取证 | 常驻项，未结束 | 见 `03-fis-drill-cases.md` |
| D8 | 停止模式矩阵（四种参数组合） | **推翻第一轮头条结论** | `D8-stop-mode-matrix.md` |
| D9 | 存储旁路真机验证 | 通过，过程中抓出「写了没人读」缺陷 | `D9-storage-side-channel.md` |
| D10 | 指标延迟 / 卷型盲区 / 告警动作可注入性 | **推翻 D3 结论适用范围，并抓出一个误动作缺陷** | `D10-metric-latency-volume-type-alarm-actions.md` |
| D11 | 第三档停止耗时（应用冻死、OS 存活） | **补上 D8 缺的那一档，修正了默认值的依据** | `D11-stop-hung-app.md` |

## 六、已知缺陷与待办

| 项 | 说明 |
|---|---|
| 编排器 `wait_status_ok` 串行在 `wait_app_ready` 前 | **已修**（第二轮）。改为 `wait_recovered()` 并发、以 app_ready 为判据后，实测 `running → app_ready` 7.014 s，旧记录的 89.7 s / 121.8 s 是这个缺陷造成的 13–17 倍虚高 |
| 探测器缺存储故障旁路触发 | **已实现**（第二轮）。新增 `STORAGE_STALLED` 判定档与 `--volume-ids` 旁路轮询，真机验证待做 |
| 多探测器 quorum 未实现 | 本次验证用单探测器 + sentinel 对照 |
| ~~`reboot`/`stop`/`terminate` 告警动作~~ | **已测（D10）**：三种都真的执行，只有 `recover` 是空操作 |
| ASG 替换与备机池两条恢复路径 | 未测，是压缩恢复段的关键 |
| 1 秒轮询的 CloudTrail 成本 | 本次 20 分钟窗口的 lookup 被自己的 Describe 事件填满并触发限流，生产要评估数据事件成本 |

---

## 七、交付物索引

### 方案文档

| 文件 | 内容 |
|---|---|
| `design/01-prober-and-orchestration.md` | 探测器与编排器方案，含真机验证结果 |
| `design/02-prober-quorum-design.md` | 多探测器投票设计（**只设计，未实现**） |
| `design/03-fis-drill-cases.md` | FIS 演练用例集与可注入性总表 |
| `design/04-terminology-and-prior-art.md` | 术语校正依据、行业实践、GitHub 现成方案对照 |
| `design/05-prober-deployment.md` | 探测器部署与执行方式，含共担故障域清单 |

### 脚本

| 文件 | 用途 |
|---|---|
| `shard_prober.py` | 探测器主体（纯标准库 + boto3 可选旁路） |
| `recovery_orchestrator.py` | 恢复编排器（默认 dry-run） |
| `ec2_forensics.py` | 常驻取证采集 |
| `health_endpoint_reference.py` | **给客户应用团队的健康端点参考实现**，已实测 |
| `game_stub.py` | 演练用被测桩程序 |
| `stop_matrix.py` | 停止模式对比实验 |
| `stop_hung_app.py` | 第三档停止耗时实验（应用冻死但 OS 存活） |
| `metric_publish_latency.py` | 自定义指标发布延迟测量 |
| `alarm_action_test.py` | 告警动作可注入性测试 |
| `timeline.py` | 分段耗时折叠分析 |

### 面向客户的汇报文档

`validation/reports/10-customer-briefing.md` —— 13 章 + 附录，
含章节索引与按听众推荐的读法。**对外汇报请用这一份**，本文件是内部总结。
