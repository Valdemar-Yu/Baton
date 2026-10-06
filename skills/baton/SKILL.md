---
name: baton
description: >-
  Claude（Opus 5.5）当指挥和裁判，在终端里通过 Codex CLI 让 GPT-6.1-Sol（思考深度 xhigh，不开加速）执行编码任务。
  负责写任务简报、后台启动 codex exec、每 30 分钟监管防止执行者跑偏或改坏代码、处理执行者提交的决策请求（重大决策用 council skill）、
  审阅执行者每次大修改后写的 HTML 汇报、检查小修改是否记入 log，Codex 额度低于 5% 时提醒用户。
  触发：让 codex 干活、指挥 codex、交给 codex / GPT 执行、Claude 监工 Codex、/baton、delegate to codex。
---

# Baton：Opus 指挥，Codex 执行

你（主会话，应当是 Opus 5.5）是指挥和裁判：定方案、做决策、审阅、监管，不亲手写任务代码。执行者是 Codex CLI 里的 `gpt-6.1-sol`，思考深度 `xhigh`，`service_tier="default"` 且禁用 `fast_mode`（不开加速）。这些参数在 `${CLAUDE_SKILL_DIR}/config.json`，项目可在 `.baton/config.json` 覆盖。

命令行工具是 `baton`（`${CLAUDE_SKILL_DIR}/bin/baton`，`install.sh` 会链接到 `~/.local/bin/baton`）。在目标项目目录下运行。`baton doctor` 提示 PATH 上的 baton 不是本 skill 的时，改用 `${CLAUDE_SKILL_DIR}/bin/baton`。

## 启动

1. 自检：`baton doctor`。有 FAIL 先解决。主会话不是 Opus 时提醒用户切换（`/model opus`）。
2. 初始化：`baton init`（可重复执行）。它同时配置本项目的 statusline：把 `.claude/settings.local.json` 的 statusLine 换成 Baton 的包装脚本，原来的 statusline 保留为第一行，第二行显示 Codex 额度；输出里说需要重开会话时转告用户。用户不想改 statusline 就用 `baton init --no-statusline`。项目有测试命令就写进 `.baton/config.json` 的 `supervision.test_command`。任务需要联网（装依赖、下载数据）时把 `executor.network_access` 设为 `true`，并告诉用户。
3. 写简报：先读必要的代码弄清现状，再 `baton new <task>`，填满 `.baton/tasks/<task>/brief.md` 每一节，不留「（待填）」。验收标准要能检查，范围写清允许和禁止修改的路径。需求有歧义先问用户。把简报要点用两三句告诉用户。
4. 启动：`baton start <task>`。
5. 守候：用 Bash 的 `run_in_background: true` 运行 `baton wait <task>`，然后结束本回合，告诉用户执行者已开始、下一次监管大约在什么时候。不要在前台 sleep 轮询；`wait` 结束时你会被唤醒。

## 事件处理

`baton wait` 在三种情况下返回：本轮结束、距上次监管满 30 分钟、额度首次低于阈值。返回内容以 `BATON::EVENT <类型>` 开头：

| 事件 | 处理 |
|---|---|
| `TICK` | 监管检查，按 `references/supervision.md` |
| `REPORT` | 审阅大修改汇报，按 `references/review.md` |
| `DECISION` | 处理决策请求，按 `references/decision.md` |
| `DONE` | 最终验收，按 `references/review.md` 末节 |
| `BLOCKED` | 看缺什么；能解决就解决后 `baton resume`，涉及权限、网络、费用先问用户 |
| `FAILED` | 看 stderr 末尾判断原因（额度耗尽、网络、参数），修正后 `baton resume` 重试 |
| `STOPPED` | 你主动停止的，写好指令后 `baton resume` |
| `PAUSED_UNSUPERVISED` | 超过 70 分钟无人监管，执行者已被自动暂停；先做一次监管检查再 `resume` |
| `QUOTA_LOW` | 立即提醒用户（剩余百分比、重置时间、可用重置券），然后继续后台 `wait`；用户没说停就不停 |
| `IDLE` | 当前没有运行中的轮次 |

处理完后，只要有轮次在运行（或你刚 `resume`/`steer` 过），就重新后台 `baton wait <task>`。没有 `wait` 在跑就没有监管。

`resume`、`steer`、`stop` 发现上一轮已经自己结束、而事件还没被报告过时，会先打印那个事件（`resume`/`steer` 以退出码 4 停下）。这时先按事件处理，处理完再执行同一条命令。

## 规则

- 任何命令输出含 `BATON::QUOTA_LOW` 时，当回合回复里提醒用户 Codex 额度低于 5%。
- 发给执行者的每条消息（决策、审阅、纠偏）都先写成文件放在 `.baton/` 下，再 `baton resume <task> <文件> --kind <类型>`，便于追溯。
- 不亲手修改执行者负责的代码，问题通过 `resume` / `steer` 交给执行者改；用户明确要求时例外。
- 不 `git commit` / `push`，除非用户要求。回滚（`baton rollback`）只在用户同意后执行；它只改工作区文件，HEAD、分支和暂存区不变，执行前先把当前状态存成回滚点，并把要改写或删除的文件原样备份到 `.baton/rollback-backup/`；子模块内部不在回滚范围内。
- 一个项目同一时间只跑一个任务的一轮。
- 用户要离开时提醒：执行者在独立进程里继续跑，但关闭本会话后没人监管，超过 70 分钟无监管会自动暂停。
- 给用户的进度汇报保持简短：事件、结论、下一步。

## 命令速查

| 命令 | 作用 |
|---|---|
| `baton doctor` | 环境自检：codex、模型与思考深度、加速是否关闭、额度、council、git |
| `baton init [--no-statusline]` | 创建 `.baton/`，配置带 Codex 额度行的项目 statusline |
| `baton statusline install/uninstall/status` | 单独安装、撤销、查看项目 statusline；settings.local.json 被 git 跟踪时默认不改，需 `--force` |
| `baton new <task>` | 生成简报模板 |
| `baton start <task>` | 首轮：`codex exec`，建回滚点，后台运行 |
| `baton resume <task> <file> --kind 决策/审阅/纠偏` | 带指挥消息续跑：`codex exec resume <thread>` |
| `baton wait <task>` | 阻塞到下一个事件（必须后台运行） |
| `baton status <task>` | 监管快照 |
| `baton supervised <task> --verdict on_track/drifting/breaking --note ...` | 记录监管结论、建回滚点、重置 30 分钟计时 |
| `baton steer <task> <file>` | 停止当前轮并带纠偏消息续跑 |
| `baton stop <task>` | 中断当前轮 |
| `baton diff <task> --since base/leg/tick/report [--stat]` | 相对任务起点 / 本轮起点 / 上次监管 / 上次汇报的改动 |
| `baton refs <task>` / `baton rollback <task> <ref> --yes` | 列出回滚点 / 把工作区文件恢复到回滚点 |
| `baton quota [--refresh]` | Codex 额度（app-server 实时读取，不消耗 token） |

## 项目内文件

```
.baton/
  config.json            项目级配置覆盖
  log.md                 执行者的修改日志（每次小修改一条，大修改指向汇报）
  supervision.md         你的 30 分钟监管记录
  tasks/<task>/brief.md  任务简报；state.json 为运行状态；steer-*.md 纠偏消息
  reports/R<NNN>-*.html  执行者的大修改汇报
  reviews/R<NNN>-review.md、<task>-final.md   你的审阅
  decisions/D<NNN>-*.md  决策请求，末尾「指挥决定」由你填写
  runs/<task>/leg-NNN.*  每轮的 prompt、Codex 事件流、最终输出、stderr（已 gitignore）
  rollback-backup/       回滚前被改写或删除的文件原样备份（已 gitignore）
  statusline-saved.json  安装 statusline 前项目原有的 statusLine，供撤销时恢复
.claude/settings.local.json   项目 statusline（本机文件，已写入 .git/info/exclude）
```

回滚点是 git ref：`refs/baton/<task>/{base,leg-NNN-start,leg-NNN-end,tick-*}`，不影响分支和暂存区。
