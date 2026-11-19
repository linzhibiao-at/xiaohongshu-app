# Superpowers 使用约束

本项目在 `.qoder/skills/` 下内置了 superpowers 技能集。本规则替代上游插件的 SessionStart hook，作用是让 Agent 在每个会话开始时就按这些技能行事。

建议在 Qoder 的 Rules 面板把本规则设为 **Always Apply**（若为 Model Decision，请把触发场景理解为"任何编码或创作任务开始前"）。

## 核心约束

- 只要存在 1% 的可能性某个技能适用于当前任务，就必须在给出任何回复或执行任何动作之前调用该技能——包括澄清性提问、探索代码库和查看文件。
- 多个技能同时适用时，先执行流程类技能（`brainstorming`、`systematic-debugging`），再用实现类技能落地。
- 调用技能后先声明「Using [skill] to [purpose]」，再严格按技能内容执行；技能内含清单时，为每一项建立待办。
- 用户指令（本项目规则、AGENTS.md、用户直接要求）优先于技能；技能优先于默认行为。只有用户明确豁免时才跳过技能流程。

## 可用技能（`.qoder/skills/`，用 Skill 工具按名称调用）

| 技能 | 用途 |
| --- | --- |
| `using-superpowers` | 引导技能：如何发现并使用其余技能，含 Qoder 工具映射 |
| `brainstorming` | 任何创作类工作前的需求澄清与方案对齐 |
| `writing-plans` | 把需求或设计写成多步骤实现计划 |
| `executing-plans` | 在当前会话内联执行计划 |
| `subagent-driven-development` | 子代理逐任务实现 + 评审的执行循环 |
| `dispatching-parallel-agents` | 2 个以上互不依赖的并行任务 |
| `test-driven-development` | 先写失败测试再写实现 |
| `systematic-debugging` | 定位根因而非猜测修复 |
| `requesting-code-review` / `receiving-code-review` | 请求评审 / 接收评审意见 |
| `verification-before-completion` | 声称完成前先跑验证并给出证据 |
| `using-git-worktrees` | 需要隔离工作区时创建 worktree |
| `finishing-a-development-branch` | 集成已完成的开发分支 |
| `writing-skills` | 编写和测试新技能 |
| `diagnosing-superpowers` | 复盘超时、超预算、被忽略的执行过程 |

## 工具差异

Qoder 工具名与 Claude Code 不同。涉及子代理派发、待办管理、技能调用时，以 `.qoder/skills/using-superpowers/references/qoder-tools.md` 为准。Qoder 内置子代理只有 `CodeReview`、`ComputerUse` 等专用类型，无实现者子代理时改用 `executing-plans` 内联执行。
