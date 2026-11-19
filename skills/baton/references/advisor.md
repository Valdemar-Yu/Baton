# Advisor 操作说明（Baton v3 · `baton advise`）

## Advisor 是什么

Advisor（默认 **Gemini 4 Neon XHigh**，运行在隔离只读的 `agy -p` 中）是 Baton v3 的首席设计与高阶冷眼顾问。它**严格无状态冷启动**，不改代码、不驱动状态机、不与 Executor 对话；每次调用的上下文由 Baton CLI 确定性打包，结果落盘 `.baton/advice/A<NNN>.{json,md}` 并记入哈希链账本。

## 什么时候必须调用 `baton advise`

1. **开题门控（`design`）**：研究型任务在 `baton start` 前，尤其是涉及实验设计、数据集划分、评测指标、对照基线或复杂架构改造时：
   ```bash
   baton advise design <task> --files src/eval.py
   ```
   根据返回的 `readiness`、`missing_invariants`、`recommended_freeze` 和 `stop_criteria` 完善 `brief.md` 与 `baton-scope` 后再 `baton start`。

2. **关键决策（`decide`）**：收到 `DECISION` 事件且涉及研究路线、公共接口、数据格式、评测口径或不可逆改动时：
   ```bash
   baton advise decide <task> --decision D001
   ```
   采纳或调整其 `draft_precedent` 后，通过 `--advice` 关联入账：
   ```bash
   baton decide <task> D001 .baton/messages/D001-decision.md --advice A002
   ```

3. **高阶冷眼审计（`audit`）**：
   - **自动单轨路由**（无需手动触发）：在默认 `research` 模式下，`DONE` 终审、`VIOLATION` 违例以及连续 ≥2 次 `REVISE` 的卷宗会自动跳过 Hydrogen Monitor，**直接由 Neon Advisor 单轨审计**，产出标准监督简报供 `baton rule` 使用。
   - **手动升级**：对任何常规卷宗，若 Carbon Conductor 拿不准或需要高阶复核，可显式运行：
     ```bash
     baton advise audit <task> --dossier <dossier-id> --rerun
     ```
   - 严禁在 `audit` 时附带 Conductor 的个人辩护或结论诱导，必须让 Advisor 只读封存卷宗。

4. **卡壳解阻（`unblock`）**：收到 `BLOCKED`、连续调试无进展或出现反复报错死循环时：
   ```bash
   baton advise unblock <task> -q "训练收敛在 epoch 2 出现 NaN，请基于最近两轮证据定位根因"
   ```
   若 Advisor 返回 `should_reset_session: true`，下一轮交由 Baton 自动/显式重置 Claude Executor 会话，避免旧推理路径污染。

## 职责零重叠原则

- **Conductor (`Carbon High`)**：拥有执行与裁决权（写简报、调 `advise`、逐条 `baton rule`、向用户负责）。
- **Monitor (`Hydrogen XHigh`)**：拥有常规 `REPORT` 冷眼初审权（只出事实偏离 `F1/F2...` 与记忆建议）。
- **Advisor (`Neon XHigh`)**：拥有设计把关权 + 终审/违例/疑难单轨审计权。
- **Executor (`Claude Opus XHigh`)**：唯一代码写入者。
