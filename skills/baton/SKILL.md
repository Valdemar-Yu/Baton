---
name: baton
description: >-
  对所有需要创建、修改、重构、调试、测试或审阅代码的任务，使用 Baton 的 conductor、monitor、executor 流程。默认 conductor 为 gpt-6-astra ultra，executor 为 gpt-6.1-sol ultra，二者开启 Codex Fast；monitor 使用 gpt-5.6-luna 的最高可用 effort（max）并只读证据。触发：任何 coding、code、编程、修复、实现、重构、测试任务，或 /baton、delegate to codex。
---

# Baton：conductor、monitor、executor

你是 Codex 主会话的 **conductor**，也是唯一决策者。你定义任务边界、写简报、读取 monitor 的证据简报、用 `baton rule` 裁决并决定是否续跑。代码审阅由独立 monitor 完成；你不直接审原始代码、不亲手修改 executor 负责的代码。需要改动时写消息交给 executor，涉及用户选择的动作由用户在自己的终端执行。

三个角色各自只有一条职责线：

| 角色 | 默认运行位置 | 负责 |
|---|---|---|
| conductor | Codex 主会话，`gpt-6-astra` / `ultra` / Fast | 简报、范围和决策；处理事件、裁决、最终验收 |
| executor | Codex CLI，`gpt-6.1-sol` / `ultra` / Fast | 按简报改代码、验证、记录 `.baton/log.md`、交小修改日志或大修改 HTML 汇报 |
| monitor | Codex 独立只读进程，`gpt-5.6-luna` / `max` | 只读卷宗、快照、章程、判例和消息，写监督简报；不做决定、不与 executor 对话 |

记忆使用 mem0、本地 Ollama 的 `baton-qwen3-embed:latest` 和项目外台账；Ollama 不可用时检索退回关键词，并在证据中写明原因。

## 启动一项任务

在项目目录运行，先确认主会话使用 `/model gpt-6-astra`、`/reasoning ultra`，并让 `service_tier="priority"` 保持 Codex Fast；不一致时提醒用户调整。不要再用 v1 的 `judge.model` 派子 agent 当裁判。

```bash
baton doctor
baton config
baton init                    # 首次安装；默认使用 Codex executor
baton memory install          # conductor 可直接运行；运行前告诉用户
baton new <task>
```

`doctor` 会检查 Codex、Ollama/记忆、台账、快照和 v1 指令告警。首次 init 会创建 `baton.json`；如果项目显式选择 Hermes backend，再按项目配置准备 `baton-exec-v2` profile。

第一次使用（或 `doctor` 提示用户级基底缺失/过期）时，先运行 `baton harness base --init`，由 conductor 改写并运行 `baton harness base --approve`；之后每个任务的 harness 初稿都会自动带入已审定基底。

`baton memory install` 只写 `$BATON_HOME/venv` 和本地 Ollama 模型，运行前告诉用户。只有 Hermes 登录和带 `--user-approved` 的命令需要用户在自己的终端执行。写 `.baton/tasks/<task>/brief.md` 时填完整验收、允许/禁止路径和验证命令，并包含范围块：

```markdown
```baton-scope
{"allow":["src"],"deny":["data"],"acceptance":["tests"],"frozen":[],"verify":"auto"}
```
```

`allow` 是 executor 可以修改或新建的路径，新写的回归测试放这里；`deny` 禁止触碰。
`acceptance` 只列已存在、必须通过的测试文件或 pytest node id，它们属于锁定级，不能修改、删除或新建；不要列源码或待写的新测试，没有现成验收测试时留空 `[]`。
`frozen` 列其他需要锁定的已有文件；`verify` 为 `auto` 时运行配置的验证命令，`none` 时跳过自动验证。验收标准的文字描述写在简报正文。
每个任务的 `harness.md` 是经 conductor 审定的长期项目上下文；固定小节、初稿来源和审定规则见 [`references/harness.md`](references/harness.md)。
`baton start` 前必须完成审阅并删除待审标记，brief 只负责本任务的目标、范围和验收条件。
审阅完成后运行 `baton harness approve <task>`；该命令会删除待审标记、记录审定时间和内容哈希，随后才能 `baton start`。

确认简报后启动并交给后台等待：

用 Codex 的 shell 工具在后台执行：

```bash
baton wait <task>
```

然后结束本回合；不要前台轮询或用 shell 的 `&`。`wait` 会在轮结束、监管时间到或额度低于阈值时自动唤醒。只要还有运行中的轮次或 monitor，处理完事件后就重新在后台启动 `baton wait <task>`。

## 事件处理

事件先读对应 references，再读 `baton wait` 输出的卷宗或路径。不能跳过监督简报直接裁决，也不能派子 agent 代替 monitor 审代码。

| 事件 | 先读 | 处理 |
|---|---|---|
| `REPORT` | `references/review.md`、监督简报、`baton diff --since report` | 逐条写 verdict，`baton rule` 后按结论 resume |
| `DONE` | `references/review.md` 终审、task-final 简报 | 写 `ACCEPT`/`REVISE`，通过后 `baton close` |
| `VIOLATION` | `references/review.md`、`references/monitor.md`、violation 简报 | 只选 `REVERT`、`ADOPT`、`ASK_USER`；恢复或修正后再续跑 |
| `DECISION` | `references/decision.md`、决策请求 | 写单独决定文件，`baton decide`，再 resume |
| `DRIFT` | `references/supervision.md`、轮中检查简报 | `baton steer` 或 `supervised --dismiss` |
| `BREAKING` | `references/supervision.md`、轮中检查简报、回滚点 | 先停轮并核对证据，再裁决 |
| `DRIFT_UNHANDLED` | `references/supervision.md`、未处理检查简报 | 先 steer 或 dismiss，不能直接放行 |
| `MONITOR_FAILED` | `references/monitor.md`、stderr | 等待退出、`baton monitor --rerun`，或交用户；不得跳过简报 |
| `BLOCKED` | executor 结果、`references/executor-protocol.md` | 补足缺失条件；权限、网络、费用先问用户 |
| `FAILED` | stderr、`references/executor-protocol.md`、容量记录 | capacity 自动重试全部用完、或网络/TLS 失败时，先确认网络已恢复，再用一条纠偏消息说明情况并 resume；不要接手写代码 |
| `STOPPED` | `references/supervision.md` | 写纠偏消息后 resume，或保持停止 |
| `TICK` | `references/supervision.md`、`baton status` | 监管并运行 `baton supervised --verdict on_track/drifting/breaking`；monitor 关闭/lean 时它是兜底 |
| `PAUSED_UNSUPERVISED` | `references/supervision.md`、状态快照 | 先监管，再 resume |
| `QUOTA_LOW` | `references/supervision.md`、quota 输出和当前事件 | 提醒用户额度、重置时间和重置券；用户未要求停止则继续 wait |
| `IDLE` | `references/supervision.md`、`baton status` | 没有运行中轮次，确认是否需要 start/resume |

容量失败由 runner 自动立即重试，默认最多 3 次；仍失败时等待 15 分钟再试 1 次。重试成功会清零计数；全部用尽产生 `FAILED`/capacity，不能把它当成方法失败，需向用户说明并由 conductor 决定下一步。

## 裁决、续跑和修正

监督简报是独立证据，HTML 汇报只是 executor 的陈述。裁决文件用 `baton-verdict` fenced JSON，逐条回应 finding：

```markdown
```baton-verdict
{"conclusion":"APPROVE","findings":{"F1":{"decision":"reject","reason":"证据与范围一致"}}}
```
```

```bash
baton rule <task> <dossier> <verdict-file>
baton resume <task> --verdict <dossier>
```

普通回复、REPORT、DONE 和 VIOLATION 都必须先 `rule`。决策请求不要回写请求文件：新建 `.baton/messages/D001-decision.md` 或任意决定文件，然后：

```bash
baton decide <task> D001 <decision-file>
baton resume <task> --decision D001
```

`ROLLBACK` 只在用户同意后运行 `baton rollback <task> <ref> --yes`。`VIOLATION` 的 `ADOPT`、放宽冻结、改配置等修正案必须由用户在自己的终端运行带 `--user-approved` 的命令：

```bash
baton amend <task> <amendment-file> --user-approved
baton amend <task> --adopt <path> --user-approved
```

一次任务同一时间只运行一轮；不 commit、不 push（用户明确要求时例外）。

## lean、记忆与整理

在 `baton.json` 写 `{"mode":"lean"}` 可关闭 monitor、轮中监督和自动升级，只保留 executor 完成后 conductor 的一次裁决；`max_unsupervised_minutes` 变为 0。lean 仍须遵守范围、验证、日志和最终裁决门槛。

`baton rule` 默认按 monitor 的记忆建议核准；需要拒绝时用 `--memory none` 或在 verdict 的 `memory` 中逐条写 `reject`。`baton rule` 入账成功后，Baton 会自动把核准条目写进台账和索引，再 resume。任务结束或每三份汇报可生成整理卷宗：读取 `memory.md` 和候选，写整理 verdict 后用 `baton rule --curation`；整理支持 merge、supersede、pin、unpin、retire，台账是唯一判例来源。回滚后挂起的来源条目，按建议选择 restore 或 retire。

```bash
baton memory verify
baton memory seed <task>
baton monitor <task> --curate
baton rule <task> <curation-dossier> <verdict-file> --curation
```

## 命令速查

| 命令 | 作用 |
|---|---|
| `baton doctor` | 环境、profile、记忆、v1 指令和 monitor 隔离检查 |
| `baton config [--json]` | 查看合并后的 v2 配置 |
| `baton init [--migrate] [--upgrade-profile]` | 初始化、迁移 v1 配置、更新 profile |
| `baton new <task>` | 生成简报模板 |
| `baton harness approve <task>` | 审定 harness.md，删除待审标记并登记内容哈希 |
| `baton harness base [--init\|--approve]` | 查看、生成或审定用户级 harness-base.md |
| `baton start/resume/wait <task>` | 启动、按裁决续跑、后台等待 |
| `baton status/diff <task>` | 监管快照和证据差异 |
| `baton monitor <task> [dossier]` | 运行或重跑监督 monitor |
| `baton rule <task> <dossier> <file>` | 逐条入账裁决 |
| `baton decide <task> <D号> <file>` | 入账决定文件 |
| `baton supervised <task> --verdict ...` | 记录 TICK 监管结论 |
| `baton restore-frozen` / `baton amend` | 恢复冻结或提交用户核准的修正案 |
| `baton preset list` / `baton preset use <名> [--task <任务>]` | 列出或切换 baton.json `presets` 里的执行者/monitor 预设；带 `--task` 时同步已启动任务的配置快照（该任务不能有运行中的一轮） |
| `baton close <task>` | 验收通过后关闭任务；验证门槛因环境失败时可用 `--waive "原因" --user-approved` 豁免，或用 `--abandon "原因"` 放弃关闭 |
| `baton close <task> --abandon "<原因>"` | 不再继续、也不会有 ACCEPT 终审的任务（如以 REPORT 结束）：账本记一条 abandon，任务 closed，并执行与 close 相同的卷宗代码、事件日志、frozen 文件副本和 task home 非会话内容清理 |
| `baton memory install/verify/reindex` | 安装、校验、重建记忆索引 |

## 文件布局

项目内：`.baton/tasks/<task>/brief.md` 是简报，`reports/` 是 executor 汇报，`reviews/` 是 verdict，`decisions/` 是请求，`runs/` 保存轮次事件，`log.md` 是修改日志，`supervision.md` 是监管记录；根目录 `baton.json` 保存项目模型和执行设置。

项目外：`$BATON_HOME/projects/<project-id>/` 保存 state、影子仓库、回滚备份、卷宗、记忆台账和 qdrant；`$BATON_HOME/venv` 是 mem0/mcp 环境。Hermes profile 在 `~/.hermes/profiles/baton-exec-v2`，不写入项目 git。任务级 Codex home 共享 `cache`、`plugins`、`packages` 软链接，Hermes 的可写 `skills`、`bin`、`cache` 保持任务私有。

`BATON_HOME` 默认 `~/.baton`，可设到项目之外的其他磁盘。卷宗代码省略项目 git ignore 规则忽略的未跟踪文件；非 git 项目保持完整复制，省略统计写在 manifest 的 `code_omissions`。
同一轮的检查卷宗只保留最新一份 `code/`；新的 `R` 加数字、`<task>-final`/`<task>-final-N` 或 `<task>-leg-N-violation` report 类卷宗会按创建顺序清理更早卷宗的 `code/`。`close` 和 `gc` 清理已 closed 任务的卷宗代码、事件日志、frozen 文件副本和 task home（保留会话、结果与审核元数据）。
`baton gc --dry-run [--stale-days N]` 按类别预览文件数和字节数，`baton gc` 执行清理；默认清理 closed 任务及超过 14 天未更新的非运行任务。已清理卷宗不能重跑 monitor，回滚快照仍可使用。

DONE/终审收尾：先用 `baton rule` 登记 ACCEPT，再运行 `baton close <task>`；close 成功会自动完成上述保留规则。没有终审时用 `--abandon`，它也会执行同样的清理。需要预览项目外状态释放量时，在 close 前或定期运行 `baton gc --dry-run`，长期未更新任务可加 `--stale-days N`。

## v2 配置示例

只在需要覆盖默认值时写这些 v2 键：

```json
{
  "conductor": {"model": "gpt-6-astra", "reasoning_effort": "ultra", "service_tier": "priority", "disable_fast_mode": false, "council_for_major_decisions": true},
  "monitor": {"model": "gpt-5.6-luna", "reasoning_effort": "max", "final_reasoning_effort": "max"},
  "executor": {
    "backend": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "ultra",
    "service_tier": "priority", "disable_fast_mode": false,
    "toolsets": ["file", "terminal", "todo", "baton-memory"]
  },
  "memory": {"backend": "mem0", "embedder": {"provider": "ollama", "model": "qwen3-embedding:0.6b", "dims": 1024}},
  "mode": "standard",
  "supervision": {"interval_minutes": 30, "max_unsupervised_minutes": 70},
  "approvals": {"delegate_to_conductor": false}
}
```

无人值守运行（如评测）可在 `baton.json` 设 `"approvals": {"delegate_to_conductor": true}`；此时带 `--user-approved` 的命令由 conductor 直接执行，日常使用保持关闭。

示例中的 `qwen3-embedding:0.6b` 是基础模型；实际运行使用由它派生的 `baton-qwen3-embed:latest`。
