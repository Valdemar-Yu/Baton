# 审阅监督简报与最终验收

## 汇报后的裁决

1. 先读 `baton wait <task>` 给出的监督简报和 `baton diff <task> --since report`。简报是独立证据；汇报 HTML 只是 executor 的当事人陈述。
2. 逐条回应简报里的 F1、F2……，包括 monitor 对「申辩」引用证据的核对结果。裁决文件用 `baton-verdict` fenced JSON，例如：

   ````markdown
   ```baton-verdict
   {"conclusion":"APPROVE","findings":{"F1":{"decision":"reject","reason":"验证结果与章程一致"}}}
   ```
   ````

   `conclusion` 取 `APPROVE`、`REVISE` 或 `ROLLBACK`；每条 finding 都要有 `adopt` 或 `reject`（也可用 `--memory as-advised` 核准记忆建议）。
3. 用 `baton rule <task> <dossier> <verdict-file>` 入账。只接受绑定当前卷宗和最新监督简报哈希的裁决；漏掉一条 finding 会被拒绝。对同一卷宗重裁决必须加 `--rerule`，旧裁决仍留在账本。
4. 汇报裁决为 `APPROVE` 后，用 `baton resume <task> --verdict <dossier>` 续跑；最终裁决为 `ACCEPT` 后用 `baton close <task>`，验证门槛拒绝时用 `--waive "原因" --user-approved` 或 `--abandon "原因"`。普通回复也必须先有裁决。裁决为 `REVISE` 时，executor 下一份汇报开头写「修订自 R003」，把同一事项串起来。连续三次 REVISE 或升级轮仍未通过，停下交用户。

## 终审与 close

1. executor 交 `done` 后，读取 `task-final` 监督简报，写 `ACCEPT` 或 `REVISE` 裁决并用 `baton rule` 入账。
2. 只有最新终审卷宗绑定 `ACCEPT`、Baton 亲跑的验收验证全部通过、终点快照和裁决之后代码没有变化时，运行 `baton close <task>`。验证因环境失败时可用 `baton close <task> --waive "原因" --user-approved` 豁免验证门槛；也可用 `--abandon "原因"` 放弃关闭。close 会重新建快照，并列出被驳回的偏离。
3. monitor 将 `self_report_mismatch` 判为 `note` 时，不应仅因此写 `REVISE`；只有 blocking finding 才要求修订。
4. `ROLLBACK`：把原因和回滚点告知用户；用户同意后才运行 `baton rollback <task> <ref> --yes`，不同意就按 `REVISE` 续跑。
5. `VIOLATION`：读简报后只能选择 `REVERT`、`ADOPT` 或 `ASK_USER`。REVERT 先 `baton restore-frozen`；ADOPT 由用户在自己的终端运行带 `--user-approved` 的 `baton amend --adopt`；ASK_USER 停住等待用户。

## 记忆核准

`baton rule` 默认按 monitor 建议核准候选；不同意时用 `--memory none` 或在裁决里逐条改写。核准成功后才会追加台账，之后再 `baton resume`。不要直接编辑 ledger、reviews 或历史卷宗。
