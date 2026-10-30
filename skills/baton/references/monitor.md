# monitor 操作说明

## monitor 是什么

monitor 是 `baton` 拉起的只读证据审阅者。它只读卷宗和代码快照，对照章程、判例与已存档消息写监督简报，不做决定，也不和 executor 对话。executor 的汇报只是当事人陈述，数字以卷宗里的 Baton 亲跑结果为准。

## 怎么读监督简报

先读 `baton wait <task>` 给出的简报路径，再读 `charter.md`、`evidence.md`、`diff.patch`、`report.html` 和 `memory.md`。检查每条 F 编号的类别、严重度、证据位置和规则出处；汇报有「申辩」时，逐条核对它引用的证据。

每条偏离都必须在裁决里写 `adopt` 或 `reject` 及理由，即使建议结论看起来是 APPROVE。用 `baton rule <task> <dossier> <verdict-file>` 入账，不能把自己的判断写进简报替代裁决。

## 简报失败

monitor 失败时可以等待进程退出、用 `baton monitor <task> <dossier> --rerun` 重跑，或把情况交给用户。不能跳过失败的简报直接 `baton rule`、`resume` 或 close。

额度耗尽和容量不足属于基础设施问题，不是方法失败；记录重置时间（能取到时），等待重置或换账号后用 `baton monitor <task> <dossier> --rerun`，不要因此放弃或 REVISE。

## 轮中结论

- `DRIFT`：逐条读简报；认可就写消息 `baton steer`，不认可就 `baton supervised <task> --dismiss F1 --reason "..."`。
- `BREAKING`：先停轮，核对 diff、验证和回滚点，再按简报处理；不能继续放任 executor 修改。
- monitor 关闭、lean 模式或连续两次失败时，`TICK` 是 v1 兜底，只说明需要 conductor 重新读状态。
