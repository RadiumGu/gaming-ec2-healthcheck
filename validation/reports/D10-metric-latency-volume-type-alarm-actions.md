# D10：三项补测（自定义指标延迟 / 卷型盲区 / 告警动作可注入性）

日期：2026-09-17　区域：`ap-northeast-1`　实例：`i-TARGET00000000003`（t3.micro，用后由 terminate 动作自行销毁）

三项都是补此前标注 `[待测]` 的空白。**其中两项推翻了先前的结论，一项抓出了会造成误动作的代码缺陷。**

---

## 一、自定义指标发布延迟：0.871 秒，比托管指标快约 95 倍

补的是探测器心跳那条 `[待测]`。

### 方法

`PutMetricData` 写一个**唯一命名**的指标，随即紧密轮询 `GetMetricData`
直到该数据点可读，记录 `t_put → t_visible`。

每轮换新名字，是因为同名指标若已有历史数据点，`GetMetricData` 可能返回旧点，
把「早就可见」误判成「刚发布就可见」。

### 一个自己造成的测量误差，值得记下来

第一轮用 1 秒轮询间隔，三次结果全是 **1.046–1.048 秒**，整齐得可疑。
查 `polls` 计数发现全是 `polls=2` —— 即第一次轮询没看到、第二次才看到，
**这个「1.0 秒」是轮询间隔本身，不是被测量。**

改 0.1 秒粒度重测：

| 指标 | 值 |
|---|---|
| n | 5 |
| min | 0.602 s |
| **median** | **0.871 s** |
| max | 0.942 s |

教训与 D4 那次一样：**测量工具的粒度会伪装成被测对象的属性。**
数字整齐到等于自己的采样参数时，先怀疑测量方法。

### 结论与它推翻的说法

| 通道 | 发布延迟 | 等级 |
|---|---|---|
| **自定义指标**（`PutMetricData`） | **0.871 s**（n=5） | `[实测]` |
| AWS 托管 EC2 状态检查指标 | 84 s（D1 两次印证） | `[实测]` |

**约 95 倍差异。**

**撤销**：客户汇报第 9.9 节原写「自定义指标这一段未测，**不应假设是秒级**」——
这句话过于保守，实测就是亚秒级。已改正。

**但结论方向不变**：心跳告警仍是**分钟量级兜底**而不是秒级检测通道。
因为瓶颈不在发布延迟，而在**告警评估周期**（最小 60 秒 × 评估点数）。
把 0.871 秒的发布延迟当成「心跳能秒级发现探测器死亡」是错的推论 ——
发布快，不等于告警快。

---

## 二、`io-performance` 卷型限制：抓出一个会造成误动作的缺陷

### 起因

回答「存储延迟如何带外判断」时，先去核实一个前提，结果查到文档明文：

> `Normal` / `Degraded` / `Severely Degraded` / `Stalled`
> — **For `io1`, `io2`, and `gp3` volumes only.**

也就是说 **gp2 卷没有 `io-performance` 检查**。那它返回什么？

### 实测

| 卷型 | `io-performance` 返回 |
|---|---|
| gp3（8 GiB 根卷） | `normal` |
| **gp2**（1 GiB，新建挂载） | **`not-applicable`** |

### 缺陷：`!= "normal"` 把能力缺失当成了故障

原代码：

```python
if d["Name"] == "io-performance" and d["Status"] != "normal":
    stalled_vols.append(...)      # ← not-applicable 落进这里
```

`not-applicable != "normal"` 为真 → **判定 `STORAGE_STALLED`**
→ 该判定在 `ACTIONABLE` 集合里 → 动作阶梯 `["stop_start"]`
→ **每一个 gp2 卷都会被无条件触发一次 stop/start。**

这不是理论风险：客户若有任何区服用 gp2 根卷（很常见，老实例默认就是 gp2），
上线即误动作，而且**探测器日志会显示「检测到存储停滞」，看起来完全正常**。

### 修复

显式列举状态集合，把三种语义分开：

```python
IOPERF_BAD = ("degraded", "severely-degraded", "stalled")   # 确认故障
IOPERF_OK  = ("normal",)                                     # 确认正常
IOPERF_NOT_JUDGEABLE = ("not-applicable", "insufficient-data")  # 测不出来
```

外加**启动时预检卷型**：配置的卷全都不支持该检查时，
`available()` 返回 `False` 并说明「该卷型不支持，本旁路对该区服无效，
需换卷型或改用应用侧检查点滞后判据」——
不允许带着一条永久失效的旁路静默运行。

### 反向验证（真 gp2 + 真 gp3 卷，五条断言）

| # | 断言 | 结果 |
|---|---|---|
| 1 | 只配 gp2 → 拒绝启用并说明卷型不支持 | **PASS** |
| 2 | 只配 gp3 → 正常启用（`ok`） | **PASS** |
| 3 | gp2+gp3 混配 → 启用但标注 `partial_blind` | **PASS** |
| 4 | 运行时 gp2 判 `None`（测不出来），**不是** `True` | **PASS** |
| 5 | gp3 正常时判 `False`（正常），不是 `None` | **PASS** |

第 4 条是这次修复的核心：修复前它会是 `True`。

### 附带回答：存储**延迟**如何带外判断

`io-performance` 本来就有四档，不只「停滞」一档。原代码虽然 bug 了，
但判 `!= normal` 在 gp3 上的效果恰好覆盖了 `degraded` 与 `severely-degraded`。
修复后是显式覆盖，语义清楚。

| 想判断的 | 带外手段 | 粒度与延迟 |
|---|---|---|
| I/O 完全停滞 | `io-performance = stalled` | 检查每 5 分钟跑一次；D2 实测判出 +112.7 s |
| **性能劣化（延迟升高）** | `io-performance = degraded` / `severely-degraded` | 同上 |
| 卷数据可能不一致 | `io-enabled = failed` / 卷 `impaired` | 同上 |

**但要诚实说明带外手段的固有局限**：延迟是**写入方感受到的东西**，
带外只能拿到聚合后的、分钟级粒度的判定。所以：

- **快路径必须在带内**：游戏服自己报检查点耗时（p99）或检查点滞后。
  存储变慢的直接后果就是检查点滞后增大 —— 这已经被 `CHECKPOINT_LAGGING` 判据覆盖，
  **不需要另外去带外测延迟**
- **带外通道的作用是归因与兜底**：确认「是卷的问题不是应用的问题」，
  以及在应用侧字段缺失时提供一个虽慢但存在的信号

一句话：**带外看得见「卷不正常」，但看不清「有多慢」；
「有多慢」的业务后果由带内的检查点滞后表达。**

---

## 三、告警动作可注入性：D3 的结论适用范围过宽，已修正

### D3 原结论与问题

D3 测出：`set-alarm-state` 强制告警进 `ALARM` 后，告警历史显示
`Action completed successfully`，但 CloudTrail 零 `RecoverInstances`、
1 秒轮询零状态转换 —— 于是写下「`set-alarm-state` **无法**测恢复链路」。

**问题**：只测了 `recover` 一种动作，却把结论写成了对所有告警动作成立。

### 本次实测三种动作

| 动作 | 是否真的执行 | 证据 | 首次可见 |
|---|---|---|---|
| **reboot** | **是** | uptime 由 377 s **回退**到 226 s | 反推约 t+26 s |
| **stop** | **是** | 状态转 `stopping` | **t+1 s**，t+23 s 到 `stopped` |
| **terminate** | **是** | 状态转 `shutting-down` | **t+1 s**，t+7 s 到 `terminated` |
| `recover`（D3） | **否** | CloudTrail 零记录、零状态转换 | 未发生 |

CloudTrail 交叉验证发起方：

```
RebootInstances      1 条  invokedBy=events.amazonaws.com
StopInstances        1 条  invokedBy=events.amazonaws.com
TerminateInstances   0 条  （CloudTrail 投递延迟；实例确已 terminated）
```

`events.amazonaws.com` 证明调用是**告警动作本身**发起的，不是我手工调的 API。

### 一个检测方法上的坑

**reboot 用实例状态轮询完全看不见** —— `RebootInstances` 不改变实例状态，
1 秒粒度轮询 150 秒全程 `running`。只有 uptime 回退能证明它发生了。

所以验证 reboot 必须用 uptime 或 `/proc/stat` 的 `btime`，
**不能用 `describe-instances` 的状态字段**。按状态判断会得出「reboot 没触发」的错误结论。

### 修正后的结论

> `set-alarm-state` **可以**用于演练 `reboot` / `stop` / `terminate` 三种告警动作，
> **唯独 `recover` 不行**。

这对客户是**好消息**：这三条恢复路径可以用一条 API 调用低成本反复演练，
不需要 FIS、不需要真的弄坏机器。只有 `recover` 依赖真实硬件故障，仍不可注入。

### 为什么 `recover` 是例外（推测，未验证）

`recover` 需要 AWS 侧确认底层硬件确实故障才会执行原地迁移；
强制置 `ALARM` 只是改了告警状态，并没有产生它所要求的硬件故障事实。
**这是推测**，本次没有验证机制层面的原因。

### 仍然成立的那条结论

`Action completed successfully` **只描述投递，不描述执行** ——
这一条 D3 说得对，而且本次给出了更强的证据：
同样这句话，在 reboot/stop/terminate 上对应真实执行，在 recover 上对应什么都没发生。
**同一条消息文本，两种截然不同的事实。** 必须独立验证效果。

---

## 四、清理核对

| 项 | 结果 |
|---|---|
| 三个告警删除 | PASS，剩余 0 |
| gp2 探针卷 | PASS，已删除 |
| 测试实例 | PASS，`terminated`（由 terminate 动作自行销毁） |
| Project 标签遗留 | 仅历史 terminated 实例与 FIS 实验记录，**均不可计费** |
| 自定义指标 | `GameShard/LatencyProbe` 无法主动删除，15 个月无数据自动过期，不产生持续费用 |

## 五、由本次产生的文档修改

| # | 修改 | 状态 |
|---|---|---|
| 1 | `shard_prober.py` 存储判据显式列举状态 + 启动预检卷型 | 已改，五条断言反向验证 |
| 2 | 客户汇报 9.9 节撤销「不应假设是秒级」 | 待改 |
| 3 | `D3` 报告限定结论适用范围为 `recover` 一种动作 | 待改 |
| 4 | `03-fis-drill-cases.md` 可注入性总表更新三行 | 待改 |
| 5 | 第 9 章补入存储探测与卷型硬前提 | 待改 |
