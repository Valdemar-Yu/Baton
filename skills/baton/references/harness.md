# 任务 harness.md 约定

`harness.md` 是 Baton 为单个任务生成、再由 conductor 审定的项目上下文。它会按 executor backend 渲染成原生指令文件，让 executor 只看到当前启动目录对应的长期项目事实。每个任务有自己的副本；Codex 的 cache/plugins/packages 软链接共享，Hermes 的 skills/bin/cache 保持任务私有，因为运行时会写入这些目录；项目事实、记忆和会话不能跨任务混用。

用户级 executor 基底位于 `$BATON_HOME/harness-base.md`（默认 `~/.baton/harness-base.md`）。它由 conductor 从 `~/.claude/CLAUDE.md` 一次改写，只保留 executor 会用到的稳定规则。第一次使用或 `baton doctor` 报告缺失/过期时，运行 `baton harness base --init` 生成待审初稿，删改主会话流程、写作 Skill 调用规则和无关文风后运行 `baton harness base --approve`；审定时会刷新源文件哈希。源文件之后变化时，重新判断是否同步基底，再运行 `--approve`。

新建任务时，Baton 先生成自动初稿。初稿从启动目录到 git 根的项目指令链摘录原文并标明出处：`.hermes.md`、`HERMES.md`、`AGENTS.md`、项目内 `CLAUDE.md` 和 `.cursorrules`；如果用户级基底已审定，还会在「项目约定」最前面带入其正文。基底缺失或待审时只写提示，不会把 `~/.claude/CLAUDE.md` 原文带入。初稿同时加入记忆台账中 conductor 认可的 pinned 条目，以及项目验证命令。初稿顶部必须保留下面这一行；conductor 审阅后运行 `baton harness approve <task>`，命令会删除整行并记录审定时间和内容哈希。标记存在时不得 `baton start`：

`<!-- BATON_HARNESS_REVIEW_REQUIRED（待审：conductor 审改后删除本行，才能 baton start） -->`

审阅初稿时删除只对 conductor 主会话有效的流程指令，例如“写代码走 Baton”或“主会话不改代码”，并删除与本项目无关的上层目录内容。确认内容非空后运行 `baton harness approve <task>`；总长度超过 4000 字时，先压缩再启动；长期维护建议控制在 2000 字以内。

## 项目约定

记录 executor 在本项目中必须遵守的稳定事实：目录布局、运行时约束、接口或数据格式约定、环境初始化方式，以及不会随本任务改变的命名规则。只写可以由项目文件或已核准记忆验证的内容。

## 常用命令

记录开发、检查、构建和验证命令，并说明它们的工作目录或必要环境。验证命令来自项目配置或任务初稿；本任务额外的目标、范围和验收条件仍写在 brief 中。

## 相关文件

列出实现入口、配置、脚本、测试和数据格式说明等稳定路径，并用一句话说明用途。不要把某一轮临时生成的输出、卷宗路径或个人机器私有路径当成项目事实。

## 已知坑

记录已确认的兼容性问题、边界条件、常见失败原因和回避方式。不要把未经验证的猜测写成约定，也不要保留已经被新证据取代的旧结论。

## brief 与 harness 的分工

`brief.md` 管本任务的目标、允许和禁止路径、锁定文件、验收标准、验证命令和本轮决策；它随任务轮次变化。`harness.md` 管跨轮次可复用的项目长期事实；它由 Baton 自动起草、由 conductor 审定后才可作为 executor 上下文。两者冲突时，以当前 brief 和更近的明确指令为准，并在下次任务中更新 harness。

不得写入 conductor 的调度、审阅、记忆裁决或权限流程指令；不得写入用户个人文风规则；不得写入与当前 brief 冲突的旧约定。生成文件应保持在建议长度预算内，避免把整份项目文档或完整日志复制进来。
