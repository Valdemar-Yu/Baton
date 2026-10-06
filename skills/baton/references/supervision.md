# 30 分钟监管检查

触发：`baton wait` 输出 `TICK` 或 `PAUSED_UNSUPERVISED`。目的是尽早发现执行者走偏或把代码改坏。检查控制在几分钟内，不重做执行者的工作。

## 1. 读快照

```bash
baton status <task>
```

快照包含：上次监管以来执行者跑过的命令（含退出码）、改过的文件、最近的消息和错误；相对任务起点、本轮起点、上次监管的 diff 统计；被删除的文件、改到的构建/依赖/CI 文件和测试文件；log.md 新增内容；新的汇报和决策请求；额度。

## 2. 对照简报判断

- 方向：命令、文件、消息是否服务于简报目标和验收标准；有没有在做简报之外的事，例如无关重构、批量格式化、升级依赖。
- 范围：改到的文件是否都在「允许修改」之内；是否碰了「禁止修改」的路径。
- 破坏信号：删除文件或测试、放宽断言、跳过或注释掉测试、`except: pass` 一类吞异常、把期望输出硬编码、删除量远大于新增量、改 CI 让检查变松。可疑时看具体 diff：`baton diff <task> --since tick`（相对上次监管）或 `--since leg`（相对本轮起点）。
- 进展：30 分钟内有没有实质推进；同一条命令反复失败、在同一处来回改，都算停滞。
- 记录：有代码改动但 log.md 没有新增；改动量已超过 major 阈值却没有汇报。

配置了 `supervision.test_command` 时跑一次。执行者正在改的文件可能处于中间状态，单次失败不直接判为改坏，要结合 diff 看失败是否由执行者的改动引起。

## 3. 给结论并处理

**on_track**

```bash
baton supervised <task> --verdict on_track --note "<一两句依据>"
```

然后后台运行 `baton wait <task>`。

**drifting**：方向或范围偏了、停滞、漏记录、隐瞒了大修改。

1. 写纠偏消息到 `.baton/tasks/<task>/steer-<YYYYmmdd-HHMM>.md`：偏在哪里（引用命令或 diff）、应该回到哪里、具体下一步、哪些改动要撤回。
2. `baton supervised <task> --verdict drifting --note "<依据>"`
3. `baton steer <task> <消息文件>`（停止当前轮，带纠偏消息续跑）。如果这一轮在你检查期间已经自己结束（交了汇报、提了决策或完成），steer 会打印那个事件并以退出码 4 停下，不发纠偏；先按事件处理，再把纠偏内容并进审阅或决策消息里。
4. 后台 `baton wait <task>`

**breaking**：代码被改坏、测试被删弱、接口被破坏。

1. `baton stop <task>`（这一轮若已自己结束，stop 会打印它的事件，先处理事件）
2. `baton supervised <task> --verdict breaking --note "<依据>"`
3. `baton diff <task> --since leg` 确认范围。
4. 能让执行者自己修复：写修复指令（必须撤回哪些改动、恢复到什么行为），`baton resume <task> <指令文件> --kind 纠偏`，后台 wait。
5. 需要整体回滚：告诉用户可用的回滚点（`baton refs <task>`）和你的建议，用户同意后再 `baton rollback <task> <ref> --yes`。回滚只改工作区文件，HEAD、分支和暂存区不变；执行前当前状态会先存成回滚点，被覆盖的 git 忽略文件备份到 `.baton/rollback-backup/`，都可以恢复。

## 4. 告知用户

用一句话说明结论，例如「14:30 监管：on_track，执行者在补 parser 的边界测试，已改 3 个文件」。遇到 drifting 或 breaking 时说明依据和处理方式。
