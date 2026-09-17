# FIS 演练用例集

目标：把「检测 + 恢复」这条链路上每一段耗时都变成实测数字，而不是「观察下来 6-7 分钟」。

## 可注入性总表（先看这个，避免设计出跑不起来的用例）

| 目标信号 | 能不能注入 | 手段 | 取证等级 |
|---|---|---|---|
| `StatusCheckFailed_Instance` | **能** | guest 内 kernel panic / 断网卡 | `[文档]` instance check 就是「EC2 向网卡发 ARP 请求」 |
| `StatusCheckFailed_AttachedEBS` | **能** | FIS `aws:ebs:pause-volume-io` | `[文档]` AWS 侧真注入 |
| `VolumeStalledIOCheck` | **能** | 同上 | `[文档]` I/O 暂停 >60s 置 1 |
| 卷状态 `impaired` | **能** | 同上 | `[文档]` 约 120s 转 impaired |
| 恢复动作链路（告警→recover） | **不能** | `set-alarm-state` 会投递动作但 recover 对健康实例是空操作 | `[实测]` 见 `validation/reports/D3-recovery-chain-not-injectable.md`：告警报 `Action completed successfully`，而 CloudTrail 零 `RecoverInstances`、1s 轮询零状态转换、tick 无回退 |
| 实例不可用 → 恢复耗时 | **能** | FIS `aws:ec2:stop-instances` / 编排器 stop+start | `[实测]` 挂死 guest 恢复段 **21.8s**（`SkipOsShutdown=True`）／**263.1s**（`Force=True`，错参数）。见 D8 |
| **`StatusCheckFailed_System`** | **不能** | 无。FIS 无此动作，system check 测宿主机侧，guest 内影响不到 | `[文档]` + `[实测]` FIS 动作清单核对 |

**「让 ENI 不响应 ARP」触发的是 instance check，不是 system check。**
后果：auto recovery 在只有 instance check 失败时根本不动作，所以那条路测不到恢复链路。
两类检查都是每分钟一次、1 分钟粒度指标，所以在「上报速度」这个量级上可以互相参考，
但**不能说复现了 system check 的真实场景**。

---

## D0 —— 环境搭建与阳性对照（所有用例的前置）

**目的**：证明测量装置本身是活的。跳过这一步，后面每个用例的结论都不可信。

**关键前提（本次实测已踩到）**：现有 EC2 直接拿来当 sentinel，在当前安全组下**全部不可达**，
`sg-SENTINEL000000001`（sentinel-host）入站规则一条都没有，ICMP 与 TCP 全 timeout。
不做这一步就上线，sentinel 会永远失败，探测器把每次真实故障都判成 `PROBER_SIDE`
而抑制动作，**整套自愈静默失效，且外观上一切正常**。

```bash
# 1) 给 sentinel 补最小入站规则（加性变更，revoke 可完全回退）
aws ec2 authorize-security-group-ingress --region ap-northeast-1 \
  --group-id sg-SENTINEL000000001 \
  --ip-permissions 'IpProtocol=icmp,FromPort=8,ToPort=-1,UserIdGroupPairs=[{GroupId=sg-PROBER00000000001}]'
aws ec2 authorize-security-group-ingress --region ap-northeast-1 \
  --group-id sg-SENTINEL000000001 \
  --ip-permissions 'IpProtocol=tcp,FromPort=22,ToPort=22,UserIdGroupPairs=[{GroupId=sg-PROBER00000000001}]'

# 2) 阳性对照：必须 0% 丢包 + TCP open，否则停手修可达性
ping -c3 -W2 203.0.113.20
python3 -c "import socket;s=socket.socket();s.settimeout(2);s.connect(('203.0.113.20',22));print('open')"

# 3) 探测器自检（sentinel 不可达时它会拒绝启动，这是设计如此）
python3 scripts/shard_prober.py --target-host <SHARD_IP> --game-port 7777 \
  --health-url http://<SHARD_IP>:8080/health \
  --sentinel-host 203.0.113.20 --sentinel-port 22 --max-rounds 3
```

**判据**：`preflight_sentinel` 事件的 `healthy=true`；探测器不返回 exit 3。

**回退**：
```bash
aws ec2 revoke-security-group-ingress --region ap-northeast-1 \
  --group-id sg-SENTINEL000000001 --security-group-rule-ids sgr-0bf2974ce96ffac26 sgr-00391dc6a61054fb9
```

---

## D1 —— instance status check 的真实上报速度

**目的**：量出「故障发生 → 指标数据点 → 我们看得见 → 告警 ALARM」四个时刻，
得到 EC2 侧检测延迟的量级。这是唯一能替 system check 提供参考数量级的用例。

**注入**：kernel panic。比断网干净，因为它同时终结用户态与网络栈，最接近「OS 没了」。

```bash
# 取证器先起（必须先起，否则测不到故障发生那一刻）
python3 scripts/ec2_forensics.py --instance-ids <SHARD_ID> \
  --out-dir ../validation/raw/D1 --interval 1 --metric-interval 15 &

# 记下注入时刻，然后 panic
date -u +%Y-%m-%dT%H:%M:%S.%3NZ | tee ../validation/raw/D1/t_fault.txt
aws ssm send-command --region ap-northeast-1 --instance-ids <SHARD_ID> \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["echo 1 > /proc/sys/kernel/sysrq","echo c > /proc/sysrq-trigger"]' \
  --no-cli-pager
```

**预期观测**：`instance_status` 转 `impaired`（`reachability: failed`），
`system_status` 保持 `ok`（宿主机没坏），`StatusCheckFailed_Instance` 出现值为 1 的数据点。

**判据**：
- `t_datapoint - t_fault` ≤ 120s（每分钟一次检查 + 指标粒度）
- `t_observed - t_datapoint` 单独记录，这是指标发布延迟
- `system_status` 若也变 impaired，说明这个注入手段的语义与预期不符，用例判无效并说明

**回滚**：panic 后实例挂死，需 `stop`（**必须传 `SkipOsShutdown=True`**）+ `start` 恢复。
用 `Force=True` 对挂死实例要走完约 252 秒超时，实测差 22 倍（D8）。这一步顺便产出 D4 的数据。

**坑**：
- `sysrq-trigger` 需要先打开 `kernel.sysrq`，AL2023 默认值不一定是 1，命令里已包含。
- SSM 命令会因实例 panic 而报失败/超时，**这是预期的**，不要当成注入未生效 ——
  以取证器观测到的状态转换为准，不以 SSM 返回码为准。

---

## D2 —— attached EBS status check 与 VolumeStalledIOCheck

**目的**：量存储类故障的上报速度，并验证告警判据选对了指标。

**注入**：FIS `aws:ebs:pause-volume-io`。

**前提**：`FISExperimentRole` **未挂 EBS 策略**（已实测：只有 EC2/SSM/EKS/Network 四个）。
需要补 `AWSFaultInjectionSimulatorEBSAccess`，或改用带内联 `your-fis-ebs-policy`
的 `your-fis-role`（该角色内容待核）。

**预期观测**（`[文档]`）：
- I/O 暂停 >60s → `VolumeStalledIOCheck = 1`
- 约 120s → 卷状态转 `impaired`
- `VolumeQueueLength` 非零升高
- **`VolumeReadOps` / `VolumeWriteOps` / 吞吐全部降为 0**

**判据**：`StatusCheckFailed_AttachedEBS` 出现 1；卷状态转 impaired 的时刻被记下。

**坑（这条最容易做错）**：
量归零意味着**任何「量高于阈值就告警」的规则根本不会触发**，配上
`--treat-missing-data notBreaching` 更糟：会一直显示正常。
主判据必须是 `VolumeStalledIOCheck=1` 与卷状态 `impaired`，
`VolumeQueueLength` 非零作辅证；Ops / 吞吐只能用异常检测或跌破下限。

注入时长要 ≥ `nvme_core.io_timeout` 才测得出 OS 侧超时行为，
所以 duration 取 180s 而不是 30s 冒烟。要有 I/O 在跑，否则暂停一个空闲卷什么都观测不到。

---

## D3 —— 恢复链路耗时（**本用例已被实测推翻，不要按原设计执行**）

原设计：用 `set-alarm-state --state-value ALARM` 强制触发挂了
`arn:aws:automate:<region>:ec2:recover` 的告警，以此测量「告警 → 恢复完成」耗时。

**实测结果：不成立。** 见 `validation/reports/D3-recovery-chain-not-injectable.md`。

告警在 1.6 秒内报告 `Action completed successfully`，但实例**从未被恢复**：
CloudTrail 窗口内零 `RecoverInstances`、1 秒粒度轮询零状态转换、
tick 计数器 8825→19721 无回退、`DescribeInstanceStatus.Events` 为 `null`。

机制：`Action completed successfully` 描述的是 CloudWatch 成功**投递**动作，
不是 EC2 **执行**了迁移。EC2 的 recover 动作会校验实例是否真的受损，
`StatusCheckFailed_System` 真实值为 0 时它是空操作。

**限定范围**：这里说的「空操作」只对 `recover` 成立。
`reboot` / `stop` / `terminate` 三种动作已由 D10 实测，**都会真的执行**。

所以 AWS 文档那条「测试前先 `disable-alarm-actions` 以免意外重启实例」的警告
**已被实测证实**，而且比警告本身说的更严重：它同样适用于 `stop` 和 `terminate`，
实测 terminate 从触发到实例销毁只用了 7 秒。

不要因为 `recover` 是空操作，就认为 `set-alarm-state` 对所有告警动作都安全。

### 替代做法

AWS auto recovery 的真实恢复段耗时**无法注入，只能等真实宿主机故障**。
所以：

1. 用 D4 / D4b 的自建路径实测值作为对照基线
   （挂死 guest 用 `SkipOsShutdown=True` 恢复段 21.8 s；用 `Force=True` 是 263.1 s）；
2. 把 D7 的常驻取证器**现在就起起来**，下一次真实故障才能补上 AWS 那一半的数字。

仍然值得保留的一条操作性结论：新建告警必须**至少进入过一次 OK**
才证明它能取到数据完成评估。本次实测从创建到进入 OK 用了 **75 秒**。
永远停在 `INSUFFICIENT_DATA` 的告警等于不存在，要复查而不是假定它自己会好。


---

## D4 —— 自建 stop/start 的恢复段耗时（要优化的就是这一段）

**目的**：对比「等 auto recovery」与「自己 force stop + start」的总耗时，
并把恢复段拆到底：API 调用 → stopping → stopped → pending → running → status ok → **app ready**。
经验上 OS 启动 + 游戏进程加载往往比检测那一段更长，这一段才是客户能自己优化的。

```bash
python3 scripts/recovery_orchestrator.py --instance-id <SHARD_ID> \
  --verdict HOST_DOWN --reason "drill D4" \
  --health-url http://<SHARD_IP>:8080/health \
  --log ../validation/raw/D4/orchestrator.ndjson --apply --ignore-rate-limits
```

**判据**：`recovered` 事件出现，且 `t_app_ready - t_action` 显著小于客户观测的 6-7 分钟。

**坑**：
- `--apply` 之前先跑一次 dry-run 看动作阶梯对不对。
- `--ignore-rate-limits` 只在演练用；生产靠冷却与每日上限防止把一次故障放大成一串重启。
- 实例的 `MaintenanceOptions.AutoRecovery` 为 `default` 时，你的 stop 与 AWS 的迁移
  **会 race**。编排器会打一条 `race_warning`。这一点必须实测，不能纸面推断。

---

## D5 —— 证明 TCP 探测在进程卡死时是假阴性

**目的**：这是「用 TCP 还是 ping」这个问题的判决性实验。
如果 TCP 探测在进程冻死时仍然绿灯，那它就不能当触发器。

```bash
# 桩程序在被测实例上跑；对主循环发 SIGSTOP
aws ssm send-command --region ap-northeast-1 --instance-ids <SHARD_ID> \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["pkill -STOP -f game_stub.py"]' --no-cli-pager
```

**预期**：TCP 7777 仍然 accept、HTTP /health 仍回 200（由独立线程提供），
但 `tick` 冻住 → 探测器判 `APP_STUCK`，而纯 TCP 判据会判健康。

**判据**：同一时间窗内，`round` 事件的 `tcp.up=true` 且 `verdict=APP_STUCK`。
这一条同时成立，就证明了 TCP 单独作判据不成立。

**回滚**：`pkill -CONT -f game_stub.py`

**坑**：`pkill -f` 会匹配到自己所在的 shell 命令行，导致自杀。
在 SSM 里用 `pgrep -f` 先确认 PID 再对 PID 操作更稳。

---

## D6 —— 证明误动作抑制真的会触发（sentinel 对照的反向验证）

**目的**：门禁类机制必须做反向验证：现状绿 → 注入真缺陷必须被抓到 → 恢复复测。
这里要证明：sentinel 一起失败时，探测器**拒绝**触发恢复动作。

```bash
# 让 sentinel 也不可达：临时收回它的入站规则（不动被测实例）
aws ec2 revoke-security-group-ingress --region ap-northeast-1 \
  --group-id sg-SENTINEL000000001 --security-group-rule-ids sgr-0bf2974ce96ffac26 sgr-00391dc6a61054fb9
# 同时让被测实例也不可达（收回被测实例 SG 的探测入站规则）
```

**判据**：`verdict=PROBER_SIDE`，且**没有** `action_request` 事件。
若出现了 `action_request`，抑制机制是坏的，必须先修再继续任何演练。

**坑**：这一步会短暂让 sentinel 对照失效，做完必须立刻恢复规则并重跑 D0 的阳性对照。

---

## D7 —— system status check：不可注入，只能被动取证

**目的**：诚实地回答客户「能不能模拟真实上报速度」。不能。
所以方案是：**现在就把取证器常驻起来**，等下一次真实故障，给出精确分段耗时表。

```bash
# systemd 常驻（见 ../systemd/ec2-forensics.service）
python3 scripts/ec2_forensics.py --instance-ids <ALL_SHARD_IDS> \
  --out-dir /var/log/ec2-forensics --interval 1 --metric-interval 20
```

**产出**：真实故障发生后，用 `scripts/timeline.py` 折出
`t_fault / t_datapoint / t_observed / t_alarm / t_recovery_*` 的分段表。

**为什么值得做**：拿实测分段表跟服务团队谈「system status check 报得太慢」，
比「观察下来 6-7 分钟」有说服力得多，前者能指出慢在哪一段。

**坑**：`describe_instance_status` 默认只返回异常实例，必须 `IncludeAllInstances=True`，
否则健康实例返回空列表，而空列表与「实例不见了」无法区分。取证器已处理。

---

## 执行顺序（按依赖排，不按编号）

1. **D0** —— 不通过就停手，后面全部结论不可信
2. **D5** —— 零成本、只动桩程序，先把判据选型钉死
3. **D6** —— 证明抑制机制有效，之后才敢开 `--apply`
4. **D1** —— 第一个真实故障注入，顺带产出 D4 的恢复数据
5. **D4** —— 恢复段拆解（客户唯一能自己优化的一段）
6. **D3** —— 恢复链路对比（注意每日恢复次数上限）
7. **D2** —— 需要先补 FIS 的 EBS 权限
8. **D7** —— 常驻，不是一次性用例

> **D10 补充（2026-09-17）**：可注入性总表新增三行 ——
> `set-alarm-state` 触发 **reboot / stop / terminate 告警动作**均 `[实测]` **可注入**
> （reboot 首次可见约 t+26 s；stop / terminate 均 t+1 s 起状态转换）。
> 唯独 `recover` 不可注入。这意味着三条恢复路径可用一条 API 调用低成本反复演练，
> 不需要 FIS。验证 reboot 必须用 uptime 判定：`RebootInstances` 不改变实例状态，
> 按 `describe-instances` 状态判断会得出「未触发」的错误结论。
