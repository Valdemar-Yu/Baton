---
name: baton
description: >-
  Claude 作为唯一决策者的 conductor，调用 Hermes profile baton-exec-v2 上的 gpt-5.6-luna 执行编码任务，困难轮次升级到 gpt-6.1-sol；独立 monitor 只读证据并写监督简报，记忆由 mem0 和本地 Ollama 提供。conductor 写简报、处理裁决和监管，不审原始代码、不亲手改执行者负责的代码。触发：让 codex 干活、指挥 codex、交给 codex / GPT 执行、Claude 监工 Codex、/baton、delegate to codex、hermes。
---

# Baton：conductor、monitor、executor

你是主会话的 **conductor**，也是唯一决策者。你定义任务边界、写简报、读取 monitor 的证据简报、用 `baton rule` 裁决并决定是否续跑。代码审阅由独立 monitor 完成；你不直接审原始代码、不亲手修改 executor 负责的代码。需要改动时写消息交给 executor，涉及用户选择的动作由用户在自己的终端执行。

三个角色各自只有一条职责线：

| 角色 | 默认运行位置 | 负责 |
|---|---|---|
| conductor | Claude 主会话，模型对应 `conductor.model` | 简报、范围和决策；处理事件、裁决、最终验收 |
| executor | Hermes `baton-exec-v2`，默认 `gpt-5.6-luna`，困难轮升级 `gpt-6.1-sol` | 按简报改代码、验证、记录 `.baton/log.md`、交小修改日志或大修改 HTML 汇报 |
| monitor | 独立只读进程，模型由 `monitor.model` 决定 | 只读卷宗、快照、章程、判例和消息，写监督简报；不做决定、不与 executor 对话 |

记忆使用 mem0、本地 Ollama 的 `baton-qwen3-embed:latest` 和项目外台账；Ollama 不可用时检索退回关键词，并在证据中写明原因。

## 启动一项任务

在项目目录运行，先确认主会话模型与 `conductor.model` 一致；不一致时提醒用户执行 `/model <conductor.model>`。不要再用 v1 的 `judge.model` 派子 agent 当裁判。

```bash
baton doctor
baton config
baton init                    # 首次安装；需要跨 git 子目录时加 --accept-boundary
baton memory install          # conductor 可直接运行；运行前告诉用户
baton init --upgrade-profile  # 用户确认后覆盖 Hermes profile 模板
baton new <task>
```

`doctor` 会检查 codex、Hermes、profile、Ollama/记忆、台账、快照和 v1 指令告警。首次 init 会创建 `baton.json`、准备 `baton-exec-v2` profile，并打印登录提示；登录由用户执行：

```bash
hermes -p baton-exec-v2 auth add openai-codex --type oauth
hermes -p baton-exec-v2 auth list openai-codex
hermes -p baton-exec-v2 auth remove openai-codex <从根目录带进来的条目>
```

`baton memory install` 只写 `$BATON_HOME/venv` 和本地 Ollama 模型，运行前告诉用户。只有 Hermes 登录和带 `--user-approved` 的命令需要用户在自己的终端执行。写 `.baton/tasks/<task>/brief.md` 时填完整验收、允许/禁止路径和验证命令，并包含范围块：

```markdown
```baton-scope
{"allow":["src"],"deny":["data"],"acceptance":["tests"],"frozen":[],"verify":"auto"}
```
```

确认简报后启动并交给后台等待：

用 Claude Code 的 Bash 工具、设置 `run_in_background: true` 执行：

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
| `baton start/resume/wait <task>` | 启动、按裁决续跑、后台等待 |
| `baton status/diff <task>` | 监管快照和证据差异 |
| `baton monitor <task> [dossier]` | 运行或重跑监督 monitor |
| `baton rule <task> <dossier> <file>` | 逐条入账裁决 |
| `baton decide <task> <D号> <file>` | 入账决定文件 |
| `baton supervised <task> --verdict ...` | 记录 TICK 监管结论 |
| `baton restore-frozen` / `baton amend` | 恢复冻结或提交用户核准的修正案 |
| `baton close <task>` | 验收通过后关闭任务 |
| `baton close <task> --abandon "<原因>"` | 不再继续、也不会有 ACCEPT 终审的任务（如以 REPORT 结束）：账本记一条 abandon，任务 closed，清理卷宗代码 |
| `baton memory install/verify/reindex` | 安装、校验、重建记忆索引 |

## 文件布局

项目内：`.baton/tasks/<task>/brief.md` 是简报，`reports/` 是 executor 汇报，`reviews/` 是 verdict，`decisions/` 是请求，`runs/` 保存轮次事件，`log.md` 是修改日志，`supervision.md` 是监管记录；根目录 `baton.json` 保存项目模型和执行设置。

项目外：`$BATON_HOME/projects/<project-id>/` 保存 state、影子仓库、回滚备份、卷宗、记忆台账和 qdrant；`$BATON_HOME/venv` 是 mem0/mcp 环境。Hermes profile 在 `~/.hermes/profiles/baton-exec-v2`，不写入项目 git。

`BATON_HOME` 默认 `~/.baton`，可设到项目之外的其他磁盘。卷宗代码省略项目 git ignore 规则忽略的未跟踪文件；非 git 项目保持完整复制，省略统计写在 manifest 的 `code_omissions`。
同一轮的检查卷宗只保留最新一份 `code/`；`close` 清理该任务所有卷宗的 `code/`，保留记录和账本。
`baton gc --dry-run` 预览当前项目已关闭任务的清理数量和字节数，`baton gc` 执行清理。已清理卷宗不能重跑 monitor，回滚快照仍可使用。

## v2 配置示例

只在需要覆盖默认值时写这些 v2 键：

```json
{
  "conductor": {"model": "opus", "council_for_major_decisions": true},
  "monitor": {"model": "gpt-5.6-sol", "reasoning_effort": null, "final_reasoning_effort": null},
  "executor": {
    "backend": "hermes", "hermes_profile": "baton-exec-v2", "model": "gpt-5.6-luna",
    "reasoning_effort": "xhigh", "toolsets": ["file", "terminal", "todo", "baton-memory"],
    "escalation": {"enabled": true, "model": "gpt-6.1-sol", "reasoning_effort": "xhigh"}
  },
  "memory": {"backend": "mem0", "embedder": {"provider": "ollama", "model": "qwen3-embedding:0.6b", "dims": 1024}},
  "mode": "standard",
  "supervision": {"interval_minutes": 30, "max_unsupervised_minutes": 70}
}
```

示例中的 `qwen3-embedding:0.6b` 是基础模型；实际运行使用由它派生的 `baton-qwen3-embed:latest`。
