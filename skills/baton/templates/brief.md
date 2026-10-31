# 任务简报：{{TASK}}

```baton-scope
{"allow": ["src/", "tests/"], "deny": ["data/"], "acceptance": ["tests/acceptance/"], "frozen": [], "unfreeze": [], "verify": "auto"}
```

范围块必须放在简报开头附近。`allow`、`deny`、`acceptance`、`frozen` 是相对项目目录的路径或 glob；可选的 `unfreeze` 是已有测试文件的精确路径列表，文件名必须符合 `test_*`、`*_test.*`、`*.spec.*` 或 `*.test.*`，不接受 glob、目录、路径不存在或测试配置文件。它只能列出原本会因 `supervision.test_command` 自动冻结的测试文件；非空时启动必须使用 `baton start <task> --user-approved`，并在 `/dev/tty` 逐字确认任务名。`verify` 可写 `auto`（默认）或 `none`。`acceptance`、`frozen`、测试配置和测试命令引用的其他文件会在任务开始时冻结；`unfreeze` 文件不进 freeze manifest，但删除、减少测试和跳过测试仍会触发 ALERT。

## 目标
（待填）一两句话说明要做成什么，以及为什么做。

## 验收标准
（待填）可以检查的条件，例如：
- [ ] `pytest tests/test_x.py` 全部通过
- [ ] 新接口 `foo(bar)` 返回 ...

## 范围
- 允许修改：（待填）目录或文件
- 禁止修改：（待填）例如公共 API、数据库迁移、CI 配置；没有就写「无」

## 约束
（待填）语言版本、不能引入的依赖、性能/兼容性要求；没有就写「无」

## 验证方式
（待填）执行者每次修改后应运行的命令

## 背景
（待填）相关文件、已知问题、用户原话里的关键要求
