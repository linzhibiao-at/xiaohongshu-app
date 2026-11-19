# Qoder 工具适配说明

Qoder 的技能词汇与 Claude Code 大体一致，但工具体系不同。技能正文里出现的
Claude Code 工具名，按下面的映射理解；映射未覆盖的场景，以当前会话实际可用的
工具为准，不要凭空编造工具调用。

## 工具映射

| 技能里的说法 | Qoder 中的对应 |
| --- | --- |
| `Skill` 工具调用某个技能 | `Skill` 工具，参数直接用技能名（本仓库技能装在 `.qoder/skills/`，无插件命名空间），如 `brainstorming` |
| `TodoWrite` | `TodoWrite`，多步骤任务先建待办，逐项推进并更新状态 |
| `Read` / `Write` / `Edit` | 读取 `Read`、新建或整体覆写 `Write`、增量修改 `SearchReplace` |
| `Bash` | `Bash` |
| `Grep` / `Glob` | `Grep` / `Glob`，语义检索用 `SearchCodebase` |
| `WebSearch` / `WebFetch` | `WebSearch` / `WebFetch` |
| 向用户提问 | `AskUserQuestion`，一次最多提 4 个问题，选项写清取舍 |
| 进入计划模式 | `SwitchMode` 切到 plan 模式；进入前先完成 `brainstorming` |

## 子代理派发

技能里的 "dispatch a subagent" / `Agent` 工具在 Qoder 中对应 `Agent` 工具，
但可用 `subagent_type` 取决于当前环境（内置如 `CodeReview`、`ComputerUse`，
或在 `.qoder/agents/` 中自定义的子代理）。

- 派发前先确认当前环境存在可用的 `subagent_type`；没有合适子代理时，按
  `executing-plans` 的做法在当前会话内联执行，不要假装已派发。
- `subagent-driven-development` 的"每个任务一个实现者 + 一个评审者"
  循环需要可派发通用实现子代理的能力。若当前环境只能派发专用子代理，改用
  `executing-plans`，或把每个任务拆成独立会话执行。
- 代码评审类技能（`requesting-code-review`）在 Qoder 中可直接用内置
  `CodeReview` 子代理承载，把待评审范围和要求写进派发提示。

## 记忆与规则

Qoder 有常驻的记忆、知识与规则机制（`SearchMemory`、`.qoder/rules`）。
技能要求"记住项目约定"时，优先落在 `.qoder/rules` 或项目 `AGENTS.md`，
而不是塞进技能文件本身。
