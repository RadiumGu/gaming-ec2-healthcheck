# Teardown 记录

执行时间：2026-09-17 06:30–06:35 UTC，区域 `ap-northeast-1`，账号 `111122223333`

## 本次演练创建/修改过的全部资源与处置结果

| # | 资源 | 处置 | 核对证据 |
|---|---|---|---|
| 1 | 被测实例 `i-TARGET00000000001` m7i.large | **已终止** | `State=terminated`，`BlockDeviceMappings=[]` |
| 2 | 根卷 `vol-REDACTED000000003` gp3 20GB | **已随实例删除** | `DescribeVolumes` 返回 `InvalidVolume.NotFound` |
| 3 | 专用安全组 `sg-REDACTED0000000007`（本次新建，含 3 条入站规则） | **整组已删除** | `DescribeSecurityGroups` 返回 `InvalidGroup.NotFound` |
| 4 | CloudWatch 告警 `ec2-failure-drill-recover-i-TARGET00000000001` | **已删除** | `describe-alarms` 匹配数 `0` |
| 5 | FIS 模板 `EXTDcumNJi6CaTNBA`（d2-pause-volume-io） | **已删除** | 本次 `Project` 标签下模板列表为空 |
| 6 | sentinel 安全组 `sg-SENTINEL000000001` 的 2 条入站规则 | **已 revoke** | 该 SG 入站规则数回到 **0**（本次之前就是 0，即完全复原） |
| 7 | 被测实例 guest 内的 `dd` I/O 负载与 `/var/tmp/ioload` | **已停止并删除**（D2 结束时） | 根分区回到 10% 使用率 |
| 8 | 本机采集进程（取证器 / 探测器 / 桩程序） | **已全部停止** | `ps -eo cmd` 按完整命令行核对，无匹配 |

**第 6 项要特别说明**：这两条规则是加在**别人既有的**安全组上的
（`sentinel-host` 的 SG，本次演练前入站规则为空）。
所以处置方式是按规则 ID 精确 `revoke`，**不能删组**。已核对该 SG 回到原状。

## 标签查询里残留的 3 条记录 —— 不是活资源

`resourcegroupstaggingapi` 仍返回三个 ARN，逐个核实后确认都不是可计费的活资源：

| ARN | 实际状态 |
|---|---|
| `instance/i-TARGET00000000001` | 已 `terminated`。终止实例的标签记录会保留一段时间后自行消失，不产生费用 |
| `volume/vol-REDACTED000000003` | 已删除（`InvalidVolume.NotFound`），标签记录滞后 |
| `fis:experiment/EXPREDACTED0000005` | FIS **实验历史记录**，不是模板。FIS 只提供删除模板的接口，已完成的实验记录不可删除，属只读审计痕迹，不产生费用 |

所以准确的说法是：**没有遗留任何可计费的活资源**；
标签索引里的三条是已删除资源的滞后记录与一条不可删除的实验审计记录。

## 未改动的东西（明确声明）

- **`FISExperimentRole` / `your-fis-role` 的权限未做任何修改。**
  D2 用的是 `your-fis-role`（其既有内联策略 `your-fis-ebs-policy` = `ec2:*`
  已覆盖 `pause-volume-io`），因此没有给任何角色新挂策略。
- 实例的 `MaintenanceOptions.AutoRecovery` 保持出厂 `default`，未关闭。
- 探测器主机 `i-PROBER00000000000` 与 sentinel 实例 `i-SENTINEL0000000001`
  本身未做任何配置变更（sentinel 只被临时改了所属 SG 的入站规则，已复原）。
- 本机文件只写在 `/home/ec2-user/works/ec2-failure/` 下。

## 一处操作瑕疵，如实记录

- 第一次执行清理脚本时用了 `pkill -f ec2_forensics.py`，
  该模式匹配到了**运行脚本自身的 shell 命令行**，导致脚本被 SIGTERM 杀死。
  改为 `pgrep` 取 PID、排除自身、再按 PID `kill` 后正常完成。
  同一个坑在演练用例 D5 里已经写过一次提醒（「`pkill -f` 会匹配到自己」），
  这次仍然踩了，说明提醒写在文档里不等于执行时会照做。
- 删安全组的重试循环用「输出为空」判成功，而新版 CLI 的
  `delete-security-group` 返回 `{}` 而非空串，所以第一次删除其实已成功，
  循环却又重试了 11 次并打印 `InvalidGroup.NotFound`。
  结果正确（组确实已删），判据写法不严谨 —— 应改为按 exit code 判定。

## 数据保留

原始数据与报告全部保留在本机，未上传任何地方：

```
/home/ec2-user/works/ec2-failure/
├── README.md
├── LOOP-GOAL.md
├── design/                      方案、脚本、FIS 模板
│   ├── 01-prober-and-orchestration.md
│   ├── 03-fis-drill-cases.md
│   ├── scripts/                 5 个脚本
│   └── fis/d2-pause-volume-io.json
└── validation/
    ├── raw/{D0,D1,D1b,D2,D3,D4,D4b,D5,D5b,D6}/   NDJSON 原始采集
    └── reports/
        ├── 00-summary.md         ← 总结，先看这个
        ├── D0-D5-probe-criteria.md
        ├── D1-status-check-latency.md
        ├── D2-ebs-status-check.md
        ├── D3-recovery-chain-not-injectable.md
        ├── D4-recovery-segments.md
        ├── D6-suppression-reverse-verification.md
        └── teardown.md           ← 本文件
```

---

# Teardown 记录（第二轮：恢复段重测与补完）

执行时间：2026-09-17 07:50–07:55 UTC

第二轮为重测恢复段（D8）与验证存储旁路（D9）又新建了一批资源。处置结果：

| # | 资源 | 处置 | 核对证据 |
|---|---|---|---|
| 1 | 被测实例 `i-TARGET00000000002` m7i.large | **已终止** | `State=terminated` |
| 2 | 根卷 `vol-REDACTED000000004` gp3 20GB | **已随实例删除** | `DescribeVolumes` 返回 `InvalidVolume.NotFound` |
| 3 | 专用安全组 `sg-DRILL000000000001`（3 条入站规则） | **整组已删除** | 首次 `delete-security-group` 即成功（按 exit code 判定）；`Describe` 返回 `InvalidGroup.NotFound` |
| 4 | FIS 模板 `EXTEvJkgN8gsMSwtt`（d9-pause-volume-io） | **已删除** | 本次 `Project` 标签下模板列表为空 |
| 5 | sentinel SG `sg-SENTINEL000000001` 的 2 条入站规则（D9 重新加的） | **已 revoke** | 该 SG 入站规则数回到 **0**，完全复原 |
| 6 | guest 内的 `dd` I/O 负载与 `/var/tmp/ioload` | **已停止并删除** | 根分区回到 10% |
| 7 | 本机采集进程（探测器 / 取证器 / 矩阵编排 / 桩程序） | **已全部停止** | `ps -eo cmd` 按完整命令行核对，计数 0 |

## 全账号核对

| 检查 | 结果 |
|---|---|
| 带 `Project=ec2-failure-drill` 标签的运行中／停止中实例 | **空** |
| 带该标签的 EBS 卷 | **空** |
| `ec2-failure-drill` 前缀的 CloudWatch 告警 | **0** |
| 带该标签的 FIS 模板 | **空** |
| IAM 角色权限改动 | **无**（D9 复用 `your-fis-role` 既有的 `ec2:*` 内联策略，未新挂任何策略） |

## 两类不可删除的残留（均不计费）

| 残留 | 说明 |
|---|---|
| FIS **实验**记录 `EXPREDACTED0000005`、`EXPREDACTED0000006` | FIS 只提供删除**模板**的接口，已完成的实验是只读审计痕迹，无法删除 |
| CloudWatch 自定义指标 `GameShard/ProberDrill` 下的 `ProberHeartbeat`、`ShardUnhealthy` | CloudWatch 自定义指标无法主动删除，**15 个月后自行过期**。已停止发布，不再产生新费用 |

标签索引里还会短暂列出已终止实例与已删卷的滞后记录，逐个核实后均已不存在。

## 上一轮两处操作瑕疵的改进情况

| 瑕疵 | 本轮情况 |
|---|---|
| `pkill -f` / `pgrep -f` 匹配到自己所在的 shell 而自杀 | **本轮又犯了一次（第三次）**。随后改为 `ps -eo pid,cmd \| awk` 按完整命令行取 PID，本轮最终的清理脚本用这个写法，未再自杀 |
| 删安全组用「输出为空」判成功 | **已改**为按 exit code 判定。本轮首次尝试即成功并正确退出循环，不再打印 11 次 `NotFound` |

## 第二轮新增的交付物

| 文件 | 内容 |
|---|---|
| `design/scripts/stop_matrix.py` | 停止模式矩阵实验编排器 |
| `validation/reports/D8-stop-mode-matrix.md` | 推翻 D4 恢复段结论的报告 |
| `validation/reports/D9-storage-side-channel.md` | 存储旁路真机验证 |
| `validation/raw/D8/`、`validation/raw/D9/` | 原始采集数据 |

代码改动：`shard_prober.py`（`STORAGE_STALLED` + `CHECKPOINT_LAGGING` 两档判定、存储旁路、
探测器心跳）、`recovery_orchestrator.py`（停止参数语义修正、`SkipOsShutdown`、
并发验收、存储故障动作阶梯）、`game_stub.py`（检查点时间戳字段与检查点失败注入）。

---

## 第三轮清理（D10，2026-09-17 09:2x UTC）

新建资源与去向：

| 资源 | ID | 去向 |
|---|---|---|
| 测试实例 t3.micro | `i-TARGET00000000003` | **由 terminate 告警动作自行销毁**，核对状态 `terminated` |
| gp2 探针卷 1 GiB | `vol-REDACTED000000008` | 已 detach 并删除，复查已不存在 |
| CloudWatch 告警 ×3 | `d10-action-{reboot,stop,terminate}-i-TARGET00000000003` | 已删除，`describe-alarms` 剩余 0 |

核对结果：Project 标签下仅剩历史演练的 terminated 实例与 FIS 实验记录，**均不可计费**。

自定义指标 `GameShard/LatencyProbe`：CloudWatch 不支持主动删除指标，
15 个月无数据后自动过期，不产生持续费用。

代码改动：`shard_prober.py`（存储判据改为显式列举状态集合 + 启动时预检卷型能力，
修复 `not-applicable` 被误判为停滞的缺陷）。
新增脚本：`metric_publish_latency.py`、`alarm_action_test.py`、`d10_teardown.py`。
