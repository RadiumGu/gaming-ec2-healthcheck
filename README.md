# gaming-ec2-healthcheck

单实例 EC2 游戏服的**故障快速检测与恢复**：一套探测器 + 编排器实现，
以及在东京区真机上把每条结论逐一验证出来的完整记录。

面向的架构：**一台 EC2= 一个区服**，无高可用设计，靠云厂商自动恢复兜底，
能容忍内存丢失但要求恢复快。这类架构在游戏行业很常见，
而 AWS 原生的状态检查与自动恢复对它有几个不太直观的盲区。

**所有数字都来自真机实测**，不是文档推断。原始采集数据（NDJSON）随仓库提供。

---

## 结论摘要

### 检测：应用侧比 EC2 侧快两个数量级

| 检测通道 | 从故障发生到能看见 | 倍数 |
|---|---|---|
| 应用侧探针判定故障 | **3.5 秒** | 1× |
| 应用侧探针请求恢复动作 | **15.5 秒** | 4× |
| 直接轮询 `DescribeInstanceStatus` | **255 秒** | 73× |
| CloudWatch 指标可见（告警最早能触发的时刻） | **319 秒** | 91× |

三个比「慢」更值得注意的发现：

1. **短于 60 秒的故障，EC2 侧根本不上报。** 不是慢，是漏 ——
   AL2023 默认 `kernel.panic=5`，panic 后 12 秒的故障窗口在 1 秒粒度轮询下零上报。
2. **`StatusCheckFailed_System` 对 guest 侧故障永远不会失败**，
   因此**自动恢复不会介入**内核挂死、OOM、文件系统损坏这一整类故障。
   「靠自动恢复兜底」的假设对最常见的一类故障不成立。
3. CloudWatch 指标发布延迟实测 **84 秒**（两次独立印证），
   自定义指标只要 **0.871 秒**（约 95 倍差异）。

### 恢复：一个 API 参数造成 22 倍差异

对**已经挂死、关不掉**的实例：

| 停止方式 | `stopping → stopped` 耗时 |
|---|---|
| **`SkipOsShutdown=True`** | **11.5 秒** |
| `Force=True` | **252 秒** |

`Force=True` **不跳过优雅关机** —— API 参考原文是「先尝试优雅关机、超时后才硬下电」。
真正绕过 OS 关机的是 `SkipOsShutdown=True`。这两个参数在健康实例上表现无法区分
（四种模式 n=3 范围全重叠），**差异只在「关不掉的实例」上暴露**，
所以日常演练发现不了。

端到端：检测 15.5 秒 + 恢复 21.8 秒 ≈ **37 秒**。

### 判据：端口通和响应码对都不够

| 判据 | 实测失效方式 |
|---|---|
| TCP 端口可连 | 进程被 `SIGSTOP` 冻死后，内核 backlog 代答，**连续 6 秒仍返回可连** |
| HTTP 200 | 健康端点由独立线程提供时，主循环冻死它照常回 200，**连续 12 轮假健康** |
| ICMP 可达 | 内核活着而应用死了时照样通 —— 它是分类器，不是存活判据 |

**能用的判据是「响应体里的主循环推进计数在变」。**
详见 [健康端点参考实现](design/scripts/health_endpoint_reference.py)。

### 存储故障是应用侧判据的盲区

根卷 I/O 全停 3 分钟，探测器 **427 轮全部判健康**、推进计数无回退 ——
游戏主循环在内存里跑，不读盘就不受影响。必须另走控制面旁路补上。

而 AWS 文档明文推荐用于检测 I/O 停滞的 `VolumeStalledIOCheck` 指标，
本环境实测**零数据点**，照文档配的告警会永远停在 `INSUFFICIENT_DATA`。

---

## 仓库内容

### 代码 `design/scripts/`

| 文件 | 说明 |
|---|---|
| **`health_endpoint_reference.py`** | **游戏服健康端点参考实现，可直接抄。** 六条约束每条对应一个真实故障场景 |
| `shard_prober.py` | 探测器。四条主探针 + 两条旁路，纯标准库（boto3 仅用于可选旁路） |
| `recovery_orchestrator.py` | 恢复编排器。默认 dry-run，单飞锁 + 冷却 + 每日上限 |
| `ec2_forensics.py` | 常驻取证采集，三层时间轴分开记 |
| `game_stub.py` | 演练用被测桩程序，可注入卡死与检查点失败 |
| `stop_matrix.py` | 停止模式对比实验编排 |
| `metric_publish_latency.py` | 自定义指标发布延迟测量 |
| `alarm_action_test.py` | 告警动作可注入性测试 |
| `timeline.py` | 分段耗时折叠分析 |

### 方案 `design/`

| 文件 | 内容 |
|---|---|
| `01-prober-and-orchestration.md` | 探测器与编排器设计，含真机验证结果 |
| `02-prober-quorum-design.md` | 多探测器投票设计（**只设计，未实现**） |
| `03-fis-drill-cases.md` | FIS 演练用例集与可注入性总表 |
| `04-terminology-and-prior-art.md` | 术语依据、行业实践、同类开源方案对照 |
| `05-prober-deployment.md` | 部署位置与执行方式，五类共担故障域 |
| `fis/*.json` | FIS 实验模板（EBS I/O 暂停） |

### 验证记录 `validation/`

`reports/` 是逐用例报告，`raw/` 是原始 NDJSON 采集数据。

按重要性而非编号：

| 报告 | 为什么重要 |
|---|---|
| `10-customer-briefing.md` | **完整汇报，13 章 + 附录，先看这个** |
| `D8-stop-mode-matrix.md` | 推翻了第一轮的头条结论（`SkipOsShutdown` vs `Force`） |
| `D10-...-alarm-actions.md` | 指标延迟、卷型盲区（含一个已修复的误动作缺陷）、告警动作可注入性 |
| `D0-D5-probe-criteria.md` | 探测判据选型的三个判决性实验 |
| `D9-storage-side-channel.md` | 存储旁路真机验证 |
| `D2-ebs-status-check.md` | 存储盲区与 `VolumeStalledIOCheck` 陷阱 |
| `D1-status-check-latency.md` | 状态检查上报延迟 |
| `D6-suppression-reverse-verification.md` | 误动作抑制的反向验证 |
| `D3-recovery-chain-not-injectable.md` | `recover` 不可注入（适用范围已被 D10 收窄） |
| `D4-recovery-segments.md` | **含撤销声明，作为错误痕迹保留** |
| `teardown.md` | 三轮演练资源清理的逐项核对 |

---

## 快速开始

### 1. 游戏服暴露健康端点

抄 [`health_endpoint_reference.py`](design/scripts/health_endpoint_reference.py)，
或按这个形状自己实现：

```json
{
  "tick": 481923,
  "last_checkpoint_success_timestamp": 1789636724,
  "players": 812,
  "loop_lag_ms": 3.1
}
```

`tick` 必须由**主循环自己**自增，健康端点**绝不做磁盘 I/O**（原因见参考实现的注释）。

### 2. 在被测实例之外跑探测器

```bash
python3 shard_prober.py \
  --target-host <区服私有IP> --game-port 7777 \
  --health-url http://<区服私有IP>:8080/health \
  --instance-id <区服实例ID> \
  --sentinel-host <对照实例IP> --sentinel-port 22 \
  --volume-ids <根卷ID> \
  --max-checkpoint-lag-s 30 \
  --heartbeat-namespace GameShard/Prober \
  --interval 1 --out prober.ndjson
```

对照实例（sentinel）不可达时探测器会**拒绝启动**（exit 3），
这是刻意设计：没有阳性对照的话，探测器侧的网络故障会被误判成区服故障。

### 3. 恢复编排（默认 dry-run）

```bash
python3 recovery_orchestrator.py \
  --instance-id <区服实例ID> --verdict HOST_DOWN \
  --health-url http://<区服私有IP>:8080/health
# 确认动作阶梯无误后，加 --apply 真正执行
```

`--skip-os-shutdown` 默认开、`--force-stop` 默认关。这个默认值是实测结论。

---

## 诚实边界

这份工作有明确的未验证部分，不要外推：

- **宿主机侧故障那一整档全部未验证。** FIS 没有对应动作，
  `set-alarm-state` 也触发不了 `recover`。只能常驻取证器等真实事件。
- **探测器与被测实例在同一 AZ、同一子网**。跨 AZ 拓扑未实测，
  一次真正的 AZ 事件会同时带走两者。
- **多探测器投票只设计未实现。**
- **控制面依赖去不掉**：区域级控制面故障时本方案与自动恢复同时失效。
  故障域从「一台实例」缩到「一个区域的控制面」，没有缩到零。
- **卷型硬前提**：`io-performance` 仅 `io1`/`io2`/`gp3` 支持，
  `gp2` 返回 `not-applicable`，存储旁路对它完全失效。

报告里所有结论都标了取证等级：`[实测]` / `[文档]` / `[待测]` / `[观测值]`。
只引用 `[实测]` 和 `[文档]`。

### 关于两次自我推翻

第一轮的头条结论（「自建恢复路径没有改善」）后来被自己推翻了，
根因是只测了一种参数、只测一次、且被自己代码的测量缺陷误导。
`D4` 报告保留了原文并在顶部加了撤销声明，**作为错误痕迹保留** ——
这比悄悄改数字诚实，也更有参考价值。

---

## 说明

- 文档保留了咨询交付的第二人称口吻（「您」），因为这本来就是一次真实项目的交付物。
- 所有 AWS 账号 ID、实例 ID、子网、安全组、私有 IP 已替换为占位符。
  测量数据（时间戳、耗时、轮次、判定）**未作任何改动**。
- 验证环境：`ap-northeast-1`，m7i.large / t3.micro，Amazon Linux 2023。
- 本机 Python 3.9，脚本不用 3.10+ 语法。

## License

MIT
