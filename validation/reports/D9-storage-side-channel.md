# 验证报告 D9 —— 存储故障旁路触发的真机验证

执行时间：2026-09-17 07:24–07:32 UTC
被测实例：`i-TARGET00000000002` m7i.large / 203.0.113.30 / 根卷 `vol-REDACTED000000004`
注入：FIS `aws:ebs:pause-volume-io`，模板 `EXTEvJkgN8gsMSwtt`，实验 `EXPREDACTED0000006`，`PT3M`
原始数据：`validation/raw/D9/`

## 要验的是什么

D2 实测过一个盲区：根卷 I/O 停 3 分钟，探测器 **427 轮全 `HEALTHY`**、
tick 25408→33910 无回退，玩家侧毫无感知。为此新增了 `STORAGE_STALLED` 判定档
与 `StorageSideChannel` 旁路轮询。

**判据：同一个注入下，探测器必须判出 `STORAGE_STALLED` 而不是 `HEALTHY`。**
写了代码不做这个验证，等于把 D2 的盲区换个地方藏起来。

---

## 结果：通过

`t_fault = 07:25:31.058`

| 事件 | 时刻 | 相对 t_fault |
|---|---|---|
| 旁路首次读到 `normal`（基线） | 07:25:20.491 | −10.6 s |
| 卷状态 API 转 `io-performance: stalled` | 07:27:55.633 | **+144.6 s** |
| **探测器判定 `HEALTHY → STORAGE_STALLED`**（round 157） | 07:27:56.458 | **+145.4 s** |
| 升 `soft`（3 连续，round 159） | 07:27:58.458 | +147.4 s |
| 升 `hard` 并发 `action_request`（round 165） | 07:28:04.459 | **+153.4 s** |

判定翻转前后各三轮，关键点是 **tick 一直在推进**：

```
round 155 HEALTHY          stalled=False  tick=15070
round 156 HEALTHY          stalled=False  tick=15090
round 157 STORAGE_STALLED  stalled=True   tick=15110   ← 翻转
round 158 STORAGE_STALLED  stalled=True   tick=15130
round 159 STORAGE_STALLED  stalled=True   tick=15150   esc=soft
```

**应用侧看上去完全健康（tick 每轮 +20），只有旁路信号抓到了故障。**
这正是 D2 里 427 轮全绿的那个场景，现在被抓住了。

旁路本身只在卷状态 API 之上加了 **0.8 秒**（轮询间隔 3 秒），
所以端到端检测延迟由 AWS 侧决定：本次 144.6 s，D2 测到 112.7 s，
两次都落在 110–150 秒带内。

### 一个设计细节得到验证

round 1 的 `storage_stalled=None` / `detail=not_started` —— 旁路线程还没产出读数时
显式表达「测不出来」，**没有被写成「正常」**。这与 `False`（取到了且正常）
是两件事，混在一起会让首轮故障被漏掉。

---

## 验证过程抓出一个真缺陷：写了但没人读

探测器判出 `STORAGE_STALLED`、升到 hard、07:28:04 发出 `action_request`。
拿这个判定去调编排器，得到的是：

```
NO_ACTION  {"verdict": "STORAGE_STALLED", "note": "非可动作判定"}
```

**整条新检测链走到头没人接。** 编排器的 `ACTIONABLE` 集合与 `_ladder` 都不认识
这个新判定，于是探测器辛苦抓到的故障被下游丢掉了，而两边各自看起来都正常。

这是本项目笔记里出现频率最高的一类缺陷（「写了但没人读」）。
它躲过了语法检查、躲过了探测器自身的验证，只有把**检测方与消费方接起来实测**
才会暴露 —— 也正是「修一个静默缺陷时先查它的反向孪生」这条要求的东西。

### 修复与反向验证

| 判定 | 动作阶梯 | 说明 |
|---|---|---|
| `APP_STUCK` | `["ssm_restart_service", "stop_start"]` | 未改动（用作对照，证明不是把所有档改成一样） |
| `APP_DEAD` | `["ssm_restart_service", "stop_start"]` | 未改动 |
| `HOST_DOWN` | `["stop_start"]` | 未改动 |
| **`STORAGE_STALLED`** | **`["stop_start"]`** | 新增，**刻意不含 `ssm_restart_service`** |

实测确认：

```
DECISION             verdict=STORAGE_STALLED  ladder=["stop_start"]
STORAGE_ACTION_NOTE  never="ssm_restart_service（会让进程挂在 D 状态）"
DRY_RUN              stop_kwargs={"SkipOsShutdown": true}
--- 对照 ---
APP_STUCK            ladder=["ssm_restart_service", "stop_start"]
```

**为什么存储故障绝不能先重启进程**：卷 I/O 已停滞，重启后的进程一去读盘就挂在
D 状态起不来，比不动更糟。

**为什么 stop/start 只能修好其中一类**（`[文档]` attached EBS status check）：
「If the EBS status check indicates an impairment ... stop and start the instance
to move it to a new host」—— 它治的是**宿主机到卷的可达性**那一类。
若是卷自身的存储子系统故障，换宿主机无效，必须从快照替换卷，
这超出自动化范围，编排器明确不做，记 `storage_action_note` 后交人工。

---

## 顺带：sentinel 启动硬条件在一个没计划的场合真实生效

第一次尝试起探测器时直接 `exit 3`：

```
FATAL  sentinel_not_reachable
```

原因是上一轮 teardown 已按要求收回了 sentinel 的两条入站规则，
它回到了原始的不可达状态。**这不是故障，是设计如此** ——
D6 里用人工注入验证过这一支，这次是它在真实场合自己挡住了一次
「会让所有故障被判成 `PROBER_SIDE`」的运行。

补回规则后 ping 0% 丢包、`preflight_sentinel.healthy=true`，
`preflight_storage.available=true`，才继续。

新的 sentinel 规则 ID（teardown 需按新 ID 收回）：
`sgr-019a8dbda16d0dd9b`（icmp）、`sgr-08891b6fe2aeec06d`（tcp 22）。

---

## 清理

- 探测器已停；远端 `dd` I/O 负载已停、`/var/tmp/ioload` 已删、根分区回到 10%
- 应用健康，tick 17431 继续推进
- 卷状态在实验结束后仍短暂停留 `impaired / stalled`（D2 也观察到同样的滞后），
  随后自行恢复
- FIS 模板 `EXTEvJkgN8gsMSwtt` 待 teardown 删除

## 仍然待测

- 卷自身受损（而非宿主机-卷可达性）这一类，`stop/start` 无效的路径未验证 ——
  `pause-volume-io` 注入的是哪一类，本次无法区分
- `STORAGE_STALLED` 触发的 `stop_start` 端到端未跑（本次只做到 dry-run 反向验证），
  因为在卷仍 stalled 时启动实例有不能引导的风险，属需谨慎设计的独立用例
