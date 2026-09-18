# 验证报告 D2 —— attached EBS 状态检查与存储故障的可观测性

执行时间：2026-09-17 06:17:44 – 06:25 UTC
注入：FIS `aws:ebs:pause-volume-io`，模板 `EXTDcumNJi6CaTNBA`，实验 `EXPREDACTED0000005`，`duration=PT3M`
目标卷：`vol-REDACTED000000003`，**即被测实例的根卷**（`nvme0n1p1` 挂载 `/`）
角色：`your-fis-role`（内联 `your-fis-ebs-policy` = `ec2:*`，信任 `fis.amazonaws.com`）
原始数据：`validation/raw/D2/`

## 建模板前先对账了动作的真实 targets

不按文档描述猜，用 `aws fis get-action` 读出来：

```
targets:    Volumes -> aws:ec2:ebs-volume
parameters: duration (ISO 8601, required)
```

同时读了 guest 侧的 `nvme_core.io_timeout` = **4294967295**（AL2023 默认，近似无限）。
含义：I/O 暂停后 OS **不会**返回错误，而是无限挂住等待。
用例文档原先写「注入时长要 ≥ `nvme_core.io_timeout` 才测得出 OS 侧超时行为」，
在这个默认值下不成立 —— 永远等不到超时。

---

## 分段时序表（t_fault = 06:17:44.233）

| 事件 | 相对 t_fault | 绝对时刻 | 通道 |
|---|---|---|---|
| **卷状态 → `impaired`**，`io-performance: stalled`，事件 `I/O throughput (0%) is severely impacted` | **+112.7 s** | 06:19:36.974 | `DescribeVolumeStatus` |
| **`AttachedEbsStatus` → `impaired`**（`reachability: failed`） | **+155.7 s** | 06:20:19.899 | `DescribeInstanceStatus` |
| `StatusCheckFailed_AttachedEBS` = 1 的 `t_datapoint` | +136 s | 06:20:00 | CloudWatch |
| **指标可见 `t_observed`** | **+218.9 s** | 06:21:43 | `publish_lag = 83.1 s` |
| FIS 实验 completed | ≈+180 s | — | — |
| `AttachedEbsStatus` → `ok` | +334.9 s | 06:23:19.171 | — |

`VolumeQueueLength` 的堆积过程：0.27 → **16.4** → **33.9** → **35.0**，恢复后回落 0.65。
`[文档]` 说的「队列非零升高」在这里得到实测印证，且升幅很大（约 7000 倍）。

**指标发布延迟再次约 84 秒**：D1 测到 83.96 s，D2 测到 83.1 s。
两次独立测量互相印证，这个约 84 秒是稳定的结构性延迟，不是偶发。

---

## 反直觉的主结果：应用完全没有受影响

| 项 | 值 |
|---|---|
| 探测器总轮数 | 427 |
| **非 `HEALTHY` 轮数** | **0** |
| tick | 25408 → 33910，**回退 0 次** |
| ICMP | 全程 up |
| `instance_status` / `system_status` | 全程 `ok` |

根卷 I/O 全停 3 分钟，玩家侧毫无感知。原因：游戏主循环全在内存里跑，
页缓存里已有的内容继续可用，正在运行的进程不需要新的磁盘读，所以 tick 照常推进。
只有真正要碰盘的东西（本次注入的 `dd` 负载、日志写入）挂住了。

### 这条结论对方案的两个影响

**一、tick 判据对存储故障是盲的。** 这是整个演练里**第一个 EC2 侧信号比应用侧探针更有效**的场景 ——
D1 / D5 都是反过来（应用侧 3.5 s vs EC2 侧 255 s）。存储故障必须靠
`DescribeVolumeStatus` 与 `StatusCheckFailed_AttachedEBS` 补上，
不能只依赖应用探针。**探测器需要增加这条旁路信号作为独立触发条件。**

**二、存在比「服务不可用」更糟的状态。** 如果游戏服在存储故障期间还在推进 tick、
还在接受玩家操作，但所有存盘都失败，那玩家是在玩一局**注定丢档**的游戏。
这比直接不可服务更坏。应用必须自己检测写失败并主动拒绝服务 ——
这一点探测器从外部做不到，只能由游戏服在写路径上自检并反映到健康端点里
（例如健康响应增加 `last_checkpoint_success_timestamp` 字段，探针据此算出检查点滞后并纳入判据；
术语与命名依据见 `design/04-terminology-and-prior-art.md`）。

---

## 关键陷阱：`VolumeStalledIOCheck` 零数据点，与文档不符

`[文档]` 明确推荐：「The CloudWatch metric for `VolumeStalledIOCheck` will be 1
if volume I/O is paused for over 60 seconds」。

`[实测]` 本账号本区的结果：

| 检查 | 结果 |
|---|---|
| `list-metrics` 能否列出 | **能**，`VolumeStalledIOCheck` / `VolumeId=vol-REDACTED000000003` |
| `get-metric-data` 数据点数 | **0**（`StatusCode: Complete`，`Timestamps: []`） |
| 期间卷是否真的 stalled | **是**，卷状态明确 `io-performance: stalled`，事件写着 `I/O throughput (0%)` |

也就是说：I/O 确实暂停了 3 分钟，卷状态确实报了 stalled，但这个指标**一个数据点都没发**。

**后果**：任何建在 `VolumeStalledIOCheck` 上的 CloudWatch 告警会**永久停在
`INSUFFICIENT_DATA`，永不触发**。若再配 `--treat-missing-data notBreaching`，
它会一直显示「正常」——一个看起来健康的监控盲区。

「名字列得出」不等于「在发布数据」。建告警前必须 `get-metric-data` 实际取一次并数数据点个数。

### 修正后的存储故障告警判据（按实测有效性排序）

| 优先级 | 判据 | 实测延迟 | 说明 |
|---|---|---|---|
| 1 | `DescribeVolumeStatus` 直接轮询 `io-performance: stalled` | **+112.7 s** | 最快，且带人可读的 `events` 描述 |
| 2 | `DescribeInstanceStatus` 的 `AttachedEbsStatus` | **+155.7 s** | 直接轮询，比指标通道早 63 秒 |
| 3 | `StatusCheckFailed_AttachedEBS` 告警 | **+218.9 s** | 含 84 秒发布延迟 |
| 4 | `VolumeQueueLength` 非零升高 | 同指标通道 | 只能作辅证，正常值也非零（0.001–0.005） |
| 不可用 | `VolumeStalledIOCheck` | **永不触发** | 本环境零数据点，不要用 |
| 不可用 | `VolumeReadOps` / `WriteOps` 高于阈值 | **永不触发** | 注入期间归零，方向相反 |

又一次印证「直接轮询 API 快于指标通道」：本例快 **63 秒**（155.7 vs 218.9）。

---

## 清理

- `dd` I/O 负载已停，`/var/tmp/ioload` 已删除，根分区回到 10% 使用率
- 卷状态已自行恢复 `ok` / `io-performance: normal`，`Events: null`
- 应用健康，tick 34495 继续推进
- FIS 模板 `EXTDcumNJi6CaTNBA` 待 teardown 删除
