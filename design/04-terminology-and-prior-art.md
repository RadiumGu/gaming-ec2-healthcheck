# 调研：术语校正与同类方案

起因：内部评审质疑「存盘年龄」这个用词是否合适。顺着查了行业实践与现成方案，
结果有三处需要修正，其中一条改变了建议的层次。

---

## 一、术语：「存盘年龄」不合适，三个理由

| 问题 | 说明 |
|---|---|
| 「存盘」用词偏 | 「存盘」是通用计算用语（save to disk）。**游戏语境的标准词是「存档」**；服务端周期性把内存状态持久化的动作，学术与工程界统一叫 **checkpoint（检查点）** |
| 「年龄」是直译 | 从 `age` 直译而来。中文技术语境不用「年龄」描述时间差，应为「距今时长」「滞后」 |
| 错过了已有的成熟概念 | 这个指标测的其实是**当前实际的数据丢失暴露量**，业界已有名字，见下 |

### 1.1 这个指标的行业名字：RPO 暴露量 / 检查点滞后

「上次成功存档距今 N 秒」= **此刻宕机会丢掉 N 秒的玩家进度** = **当前实际的 RPO**
（Recovery Point Objective，恢复点目标）。

用 RPO 来表述有一个实际好处：**它是业务方和架构方都已经懂的词。**
跟运营说「存盘年龄超过 30 秒」需要解释；说「当前 RPO 已经从 5 秒劣化到 30 秒，
现在宕机会丢半分钟进度」，对方立刻知道该不该踢玩家下线。

学术侧的对应术语来自 MMO 检查点恢复研究（VLDB 2009，
《An Evaluation of Checkpoint Recovery for Massively Multiplayer Online Games》）：
MMO 把世界状态整个放在内存里、按 tick 推进，持久化靠周期性 **checkpoint**，
论文推荐 copy-on-update + 双备份磁盘布局。所以「检查点滞后」（checkpoint lag）
也是准确的说法。

### 1.2 字段该暴露时间戳，不是「距今多少秒」

Prometheus 官方 instrumentation 实践对这类指标有明确约定：
**暴露「上次成功的时间戳」，由消费方自己算差值**，命名形如
`*_last_success_timestamp_seconds`。

理由：消费方用自己的时钟算差值，不受采集延迟影响；而且原始时间戳信息量更大
（能看出是「一直没成功」还是「刚刚成功过」）。

### 1.3 修正后的命名

| 层 | 原（不用） | 改为 |
|---|---|---|
| 游戏服暴露的字段 | `last_successful_save_age_s` | **`last_checkpoint_success_timestamp`**（Unix 秒） |
| 探测器侧的派生判据 | 存盘年龄 | **检查点滞后**（checkpoint lag），或对业务讲 **RPO 暴露量** |
| 判定名 | `SAVE_FAILING` | **`CHECKPOINT_LAGGING`** |
| 中文表述 | 存盘年龄 | 「上次成功存档距今时长」／「检查点滞后」 |

保留一个兼容考虑：游戏服若已经在暴露「距今秒数」，探测器应两种都认
（有时间戳优先用时间戳），不要为了改名逼应用团队返工。

---

## 二、Prometheus 那条建议指出了我们设计的一个不足

原文（`prometheus.io/docs/practices/instrumentation/`）：

> Knowing the last time that a system processed something is useful for detecting
> if it has stalled, but it is **very localised information**. A better approach is
> to **send a heartbeat through the system**: some dummy item that gets passed all
> the way through and includes the timestamp when it was inserted.

翻到我们的场景：只暴露「我上次成功写了」是**局部信息** ——
它只证明 `write()` 返回了，**不证明数据真的能被读回来**。

我们目前的实现做了 `fsync`（比不做强，能避免页缓存给出假成功），
但仍然只是**写侧**验证。更强的做法是**金丝雀存档**：

1. 周期性写一条带时间戳的哨兵记录（与真实存档同路径、同介质）
2. **再读回来校验内容**
3. 只有读回成功才更新 `last_checkpoint_success_timestamp`

这样验证的是整条持久化链路，而不是一次系统调用的返回值。
存储降级时（比如只读挂载、静默损坏、外置存储写成功但读失败）只有 read-back 抓得到。

**状态**：待实现，已记入待办。

---

## 三、AWS 对这个场景有专门指引，而且比我们的建议更根本

**AWS Well-Architected —— Games Industry Lens，GAMEREL03**
标题就是客户的问题：**「How do you persist game state during infrastructure disruptions?」**

### 3.1 GAMEREL03-BP01 的处方是「状态外置」，不是「本地存得更勤」

原文：

> configure your game to continuously perform **asynchronous updates of a player's
> game state to a highly-available cache or database** such as Amazon ElastiCache
> (Redis OSS) or Amazon MemoryDB. If a server failure occurs, the player's last
> saved game state can be fetched from the external data store, and their session
> can be restored on a new game server instance.

**这在很大程度上消解了我们 D2 发现的那个风险**：如果玩家状态在 ElastiCache／MemoryDB 里，
根卷 I/O 停滞就不会导致丢档，而且新实例可以直接恢复会话。

### 3.2 Lens 同时给了反面情形与第二条正规路线

> may not be suitable for fast-paced or competitive games where the state changes
> are so frequent and happening at such a significant scale that introducing even
> a performant in-memory cache data store would result in replication lag that is
> too significant to be useful to restore a session from. For games of this nature,
> the optimal approach is to **accept the loss of the server and send the player
> back to a game lobby** to find another session.

所以官方认可的是**两条**路线，不是一条：

| 路线 | 做法 | 适用 |
|---|---|---|
| **A：状态外置** | 异步写 ElastiCache／MemoryDB，故障后在新实例上恢复会话 | 状态变更频率与规模允许复制延迟的游戏 |
| **B：接受服务器丢失** | 不做外置，故障后把玩家送回大厅重新匹配 | 快节奏／竞技类，复制延迟大到无法用于恢复 |

### 3.3 这对我们的建议意味着什么

客户「一个 EC2 = 一个区服、无高可用、靠云厂商自动恢复」的现状，
**既没走 A 也没走 B** —— 它是在赌一台机器不出问题。

因此建议应分成两个层次，都讲，不要混：

| 层次 | 内容 | 时间尺度 |
|---|---|---|
| **战术层**（本次交付） | 在现有架构下把「故障 → 可服务」从 6–7 分钟压到约 37 秒 | 数周可落地 |
| **架构层**（GAMEREL03） | 按 A 或 B 改架构，让单机故障不再等于区服中断 | 需产品与研发排期 |

战术层不替代架构层。**37 秒仍然是 37 秒的中断**，而路线 A 的目标是让玩家会话
在新实例上继续，路线 B 的目标是让玩家几秒内进入另一局。
把战术层讲成「问题解决了」是误导。

### 3.4 顺带找到的两个可用指标与工具

| 项 | 用途 |
|---|---|
| `ServerProcessAbnormalTerminations`（GameLift） | 检测游戏服进程异常终止。客户不用 GameLift，但这个指标名指出了值得自建的等价信号 |
| CloudWatch Synthetics（GAMEREL03-BP03 推荐） | 对登录等关键路径做合成检查，是我们「应用侧探针」的托管替代品之一 |
| 持久世界状态存储的四个选项（AWS 博客） | 存档文件直接写 S3 ／ 本地库 dump 同步到 S3 ／ DynamoDB 键值 ／ 自建 API 层。对「一个 EC2 一个持久世界」场景比 ElastiCache 更贴 |

---

## 四、GitHub 现成方案：四个，一个架构匹配度很高

### 4.1 `keirans/aws_autoheal` —— 与客户架构最贴，可直接借鉴编排思路

自述的目标原文：
> provide resilience to an application that **cannot autoscale**,
> and to look to make it as robust in a **single AZ** as possible.

**这就是客户的架构描述。** 做法（单个 CloudFormation 模板）：

- ASG 期望容量 1（不是为了扩容，是为了拿到「实例挂了自动换一台」）
- 独立的 EBS 数据卷挂在 `/data`，与实例解耦
- EIP 与实例解耦
- ASG 生命周期钩子把新实例卡在 `Pending:Wait`
- EventBridge 规则触发 Lambda：轮询新实例就绪 → attach EBS 卷 → 关联 EIP → 放行钩子
- 实例 userdata 检测块设备、没有文件系统就建、然后挂载

**对我们的价值**：这正好补上了我们标为「待测且有风险」的备机池／卷迁移路线的**编排部分**。
它把「新实例接管旧卷与旧 IP」做成了可复制的模式，而且刻意不给实例 IAM 角色
（所有 AWS 操作在实例外的 Lambda 里做，实例本身无权限可被滥用）——
这个安全取舍值得照搬。

**必须注意的限制**：
- star 数 4，是教学示例不是生产件，单模板结构作者自己也说生产不该这样
- **只用 EC2 健康检查**。README 自己写道：生产环境应加 ELB 健康检查做应用级校验，
  否则只能发现实例死亡、发现不了应用假活 —— **这正是我们探测器要占的位置**
- 它绕开的仍然是「等实例停下」，没有回答「从一个无响应实例上 detach 卷要多久」

### 4.2 `aws-samples/aws-ec2-statuscheck-failures-logging` —— 已归档，但它的示例配置本身是个反面教材

AWS 官方示例，**2026-01-21 已归档为只读**。做的是把 status check 失败事件
经 SNS → Lambda 送到集中日志（OpenSearch／ELK）做合规审计，**不做修复**。

它 README 给的示例告警命令是这样的：

```
--period 300 --evaluation-periods 2
```

**300 秒周期 × 2 个评估点 = 最快 10 分钟才告警。**
而我们实测 AWS 侧的指标本身就有 84 秒发布延迟、检测已耗 235 秒。
照这个官方示例配，检测段会到十几分钟量级。

这条正好印证了我们的核心建议：**不要靠 CloudWatch 告警做检测**。
它也说明「按官方示例配」不等于配得快 —— 那个示例的目的是审计留痕，不是快速检测，
但很容易被当成推荐配置照抄。

### 4.3 `hassantahhan/ec2recovery` —— 覆盖率审计，客户 N 个区服时直接有用

用 Lambda 统计账号内有多少 EC2 配了自动恢复告警、有多少在 ASG 里，
给出**「EC2 恢复覆盖率」**。

**对客户的价值**：在讨论方案之前先量一个数 —— 现有 N 个区服里，
到底有几台配了自动恢复。按我们 D1 的发现（guest 侧故障根本不触发自动恢复），
这个覆盖率数字还要再打折，但它是个便宜的起点。

### 4.4 `aws-samples/amazon-cloudwatch-auto-alarms` —— 按 tag 批量建标准告警

客户有 N 个区服，逐台建告警不现实。这个工具按 EC2 tag 自动创建一套标准告警。

**用它的时候必须带上我们 D2 的教训**：批量创建标准告警很容易把
`VolumeStalledIOCheck` 这种**本环境零数据点**的指标也批量创建进去，
于是得到 N 个永久 `INSUFFICIENT_DATA` 的告警。批量工具会把一个盲区复制 N 份。

### 4.5 没找到的东西

搜索没有找到**「应用级 tick 推进判据 + 分类器 + 对照抑制」这一整套**的现成实现。
找到的方案分两类：

- 基础设施层自愈（ASG／Lambda 重挂卷与 IP）—— 解决「换一台机器」
- 状态检查事件的记录与告警 —— 解决「留痕」

**中间那层缺失的正是我们做的**：判断「这台机器上的服务到底还活着吗」，
以及「这个判断可信吗」。这也解释了为什么客户会卡在「等云厂商自动恢复」——
现成方案里没有这一层。

---

## 五、由本次调研产生的修改清单

| # | 修改 | 状态 |
|---|---|---|
| 1 | 术语：`SAVE_FAILING` → `CHECKPOINT_LAGGING`；字段改为暴露时间戳；中文改「检查点滞后」 | **已做**，代码与全部文档已改，`grep` 复核无残留 |
| 2 | 探测器兼容两种字段（时间戳优先，兼容「距今秒数」） | **已做并本地双路径验证**：时间戳路径 round 11 判出、兼容路径 round 4 判出，两者均 `action_request=0`、升级封顶 soft |
| 3 | 金丝雀存档 + 读回校验（Prometheus 那条建议） | 待做 |
| 4 | 客户汇报文档新增架构层建议章节，引用 GAMEREL03 的两条路线，并明确战术层不替代架构层 | 待做 |
| 5 | 客户汇报文档新增现成方案对比，含 `aws_autoheal` 的借鉴点与官方示例告警配置的反面教材 | 待做 |
| 6 | 备机池／卷迁移路线的编排部分可参照 `aws_autoheal`，但「从无响应实例 detach 卷的耗时」仍需实测 | 待测 |

## 附：引用来源

| 来源 | 用途 | 等级 |
|---|---|---|
| AWS Well-Architected Games Industry Lens GAMEREL03 / BP01 / BP03 | 状态持久化的官方处方与两条路线 | `[文档]` |
| `prometheus.io/docs/practices/instrumentation/` | 时间戳约定与「心跳穿透系统」建议 | `[文档]` |
| VLDB 2009《An Evaluation of Checkpoint Recovery for MMOs》 | checkpoint 术语与 MMO 持久化模型 | 学术 |
| AWS 博客《Host persistent world games on Amazon GameLift Servers》 | 持久世界状态存储的四个选项 | `[文档]` |
| `github.com/keirans/aws_autoheal` | 单 AZ 不可扩容应用的自愈编排模式 | 社区示例，star 4，非生产件 |
| `github.com/aws-samples/aws-ec2-statuscheck-failures-logging` | 状态检查事件留痕；其示例告警配置作反面教材 | AWS 官方，**已归档** |
| `github.com/hassantahhan/ec2recovery` | 恢复覆盖率审计 | 社区 |
| `github.com/aws-samples/amazon-cloudwatch-auto-alarms` | 按 tag 批量建告警 | AWS 官方 |
