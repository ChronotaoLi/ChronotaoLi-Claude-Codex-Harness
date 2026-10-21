# 设计取舍与参考

本项目把模型当作可替换的决策与执行部件，把任务状态、权限边界、证据和接受条件留在确定性的运行时。指挥保有逻辑上的连续性；它的历史由检查点提供。执行者与审阅者是一次性的。

## 参考项目

参考时间：2026-10-21。

| 来源 | 阅读内容 | 本项目采用的部分 |
| --- | --- | --- |
| [stablyai/orca orchestration](https://github.com/stablyai/orca/blob/main/skill-guides/orchestration.md) | Run、Task、Dispatch 的职责与完成归属 | 每次尝试独立 UUID，结构化结果校验，不根据空闲或缺少消息推断成功 |
| [Orca CLI orchestration docs](https://github.com/stablyai/orca/blob/main/docs/site/content/docs/cli/orchestration.mdx) | 生命周期、派发、完成与恢复 | 保留任务与派发分层，进程未确认退出时不重复派发 |
| [ECC](https://github.com/affaan-m/ECC/tree/90606c8315ee09cafc303515c5f33024db6b9882) | 共用 rules / skills / hooks、memory vault、strategic compact | 核心行为一份源文件，两种 CLI 仅负责适配；记忆是参考资料；阶段边界交接 |
| [ECC memory vault](https://github.com/affaan-m/ECC/blob/90606c8315ee09cafc303515c5f33024db6b9882/docs/design/ecc-memory-vault.md) | 来源、可检查性与按项目隔离 | 本地来源记录、显式版本、可导出记忆；未采用其 Markdown 作为唯一真相的存储实现 |
| [OpenWiki](https://github.com/Neuroant/OpenWiki#second-brain--agent-memory-path-b-opt-in) | Second Brain、记忆替换、三层上下文、session handoff | 必要状态与可检索知识分离；旧版本保留；摘要引用证据；本地词法检索首版 |

这里按本机发现的 stablyai/orca（CLI 1.4.237）参考 Orca；它有多个同名项目。当前实现不需要运行 Orca，也不向它发送任务。用户要求的“一任务一实例”比 Orca 可复用 worker 的策略更严格。

OpenWiki 参考的是 Neuroant/OpenWiki。如果原意是 OpenViking，它是另一个项目；本项目没有把二者混称，也不依赖它们的服务。

## 厂商接口依据

- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)：exec、JSON 输出、schema、已有登录复用。
- [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference)：模型、原生多 agent、记忆、hooks 设置。
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)：stdio / HTTP 连接配置。
- [Claude Code CLI](https://code.claude.com/docs/en/cli-reference)：print、JSON schema、会话持久化、工具许可和 MCP 参数。
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)：环境变量引用和 strict MCP 配置。
- [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)：逐行 JSON-RPC；stdout 仅用于协议。
- [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)：工具声明和调用结果。
- [OpenAI models](https://developers.openai.com/api/docs/models) 与 [Claude models](https://platform.claude.com/docs/en/models/overview)：保留用户指定模型 ID，账户可用性由实际 CLI 调用确认。

实际实现同时对照了本机 Codex CLI 0.163.0、Claude Code 2.1.306 的 `--help`。命令行接口可能变化；模型 ID 保存在配置中，不自动替换。

## 状态与接受条件

通常为 `planning → running → completed`，最后还必须通过整体目标验收。可选计划确认进入 `awaiting_approval`，分步执行在任务边界进入 `paused`；异常进入 `blocked` 或 `interrupted`。这些是本地运行状态，与任何外部产品的自动化目标状态无关。所有 worker 和 reviewer dispatch 始终使用新进程，指挥也通过台账恢复上下文。

一个任务只有在以下条件同时满足时才进入 accepted：

1. 当前 dispatch 返回匹配 ID 的合法结果，声明完成且无 blocker。
2. Git HEAD 未被 agent 改动，文件变更落在任务的 allowed_paths。
3. 所有配置验证命令成功，且没有修改待审阅文件。
4. 新 Sol 实例返回 approve，没有未处理的 blocking findings；advisory 可以保留。
5. 接受前文件快照等于审阅时快照。

分支提交、结果记忆和台账都带来源引用。review 失败或测试失败只保留诊断，不提升为长期成功经验。权限错误、非法输出和范围违例停止运行，避免把运行时故障误作普通返工。

审阅问题使用稳定 ID、严重级别、类别、位置、说明和证据。偏好只能是 advisory。返工审阅收到历轮问题与对应证据，既要复核旧问题，也要独立检查整个当前修改；运行时不因为 worker 的辩解而豁免门禁。原始目标和任务义务独立保留，全部任务接受后重新执行验证，并由新的 reviewer 审阅原始 base 到当前 HEAD 的完整 diff。最终文件快照不变才允许 completed。整体拒绝需要通过正常门禁的修复任务，重复 resume 不能对同一拒绝结果重新抽签。

## 调度、再规划与恢复

当且仅当一个任务就绪、没有失败反馈且尚未尝试时，运行时可直接发送包含目标、任务、依赖结果、验收和可用命令的完整简报。选择或返工仍交给指挥。指挥是否复用会话与是否支持再规划是两个独立问题，本设计保留每次重新组装上下文。

再规划只处理任务拆分、缺少前置工作和整体验收发现的工程缺口。worker 通过 `blocker_kind` 区分 task 与 environment；CLI 错误、权限、非法结果、范围违例和现场不一致不会被包装成自动再规划。指挥返回完整修订计划或 stop；运行时重新检查 DAG，并要求：

- 保留每个既有任务 ID，已接受任务完全不变。
- 未接受任务的验收和 allowed_paths 不变，已有依赖不得删除；可改进简报并增加前置依赖。
- 新任务只使用已有 allowed_paths 中的模式；不通过推测 glob 包含关系放大范围。
- 原始任务继续承担最终集成义务，拆分不能删除或重命名它来避开失败。
- 每次修订保存版本与原因；重新分配前归档未接受修改，由运行时恢复隔离工作树，保留已接受提交。

每任务尝试数、整个 run 的 worker 尝试数、再规划数和最终验收次数均持久保存且受上限约束。新任务与 resume 都不清零累计预算。每任务耗尽后仍停止，操作者可显式增加允许调整的预算再恢复。

恢复配置仅允许增加上下文预算、agent/验证超时、Claude 轮数、每任务尝试数和总 worker 尝试数；所有变化记录旧值和新值。模型 ID、供应商、effort、工具权限、验证命令、再规划上限和共享资产不允许在同一 run 中改变。进程存活和 Git HEAD 检查仍在恢复前执行。台账版本 2 不自动迁移旧运行，以免替旧证据补造新接受语义。

计划确认和单步执行为显式选项：默认连续执行；选择暂停确认后必须显式批准。任何无回复或重复恢复都不能代替批准。step 以一个已接受任务为边界；最后一个任务之后还需要整体检查。

## 项目记忆的提升与过时

任务历史与可复用项目知识分别保存。任务结果使用 run/task 唯一 key，不互相覆盖。worker 可提交有界的知识提案，但没有直接写记忆工具。审阅者逐项核实来源并返回 approved_memory_keys；代码通过不能隐含批准提案。

只有已接受任务中的明确获批提案才能写入 `knowledge:` 命名空间。写入校验相对来源路径、文件哈希、批准 key 和操作者所有权；存在操作者历史的 key 不得被自动提案替代。可选记忆的来源或所有权不满足条件时，记录诊断并丢弃该批提案；非法审阅批准 key 属于协议错误，阻止接受。

自动任务历史和项目知识默认有 180 天召回期限，操作者事实默认无期限；均保留历史证据。知识来源改变或提交不可核实时，召回标记 possibly_stale 并降权。类型权重只作为相关性排序的辅助信号，不把人工记录视为永远正确。没有自动模型整理或语义冲突覆盖。

## 共享工具与 hooks

生命周期 hooks 是可配置的运行时门禁，默认不附带策略。上下文组装事件命名为 context_prepared，保留旧名 context_compacted 作为互斥别名。暂不增加原生工具 hooks 翻译层：两端事件语义和权限效果需要按实际需求单独验证。

post_accept 的待执行状态与接受台账一起保存，失败后恢复先重试该门禁，成功后才清除。该 hook 具有至少一次执行语义，脚本需能处理重复执行。Git 文件快照还包含暂存区条目摘要；范围检查同时检查暂存变更，提交前从已接受 HEAD 重建暂存区，再仅加入审阅过的路径。再规划归档同时保存工作区与暂存差异，遇到路径祖先是符号链接时停止，避免归档写入跟随链接。

Claude 只读角色获得受限 Git 查询命令，worker 可配置额外的精确 argv 授权。CLI 授权是能力配置，不能替代 OS 隔离。Codex 适配器当前只提供只读模式，配置其为 worker 时提前拒绝；不会替换模型来迁就适配器。

完整输出契约保存在 schema.json 并由本地校验。两种供应商使用仅含基础类型、字段、必填、枚举与数组项结构的投影，Codex 对应 provider-schema.json；长度、唯一性等增强约束仍在本地强制执行，避免假设两端原生 JSON Schema 扩展完全一致。

## 为什么先串行

多模型分工不要求同时写代码。串行 DAG 能先验证模型路由、一次性实例、门禁和恢复协议；每个 run 独立工作树保证原始工作区不受普通任务编辑影响。后续并行需要进一步实现每任务工作树、依赖提交集成和冲突处理，不能只加线程池。

## 仍需明确的限制

这不是恶意代码隔离系统。原生 CLI、配置的 hooks、验证命令和 MCP 服务在用户权限下运行；只读声明、prompt 和 Git 范围检查不是完整的 OS 权限边界。强隔离应使用专用 OS 用户或容器，并在那个边界内运行本编排器。

SQLite 是长期记忆的存储真相；JSON 导出和 Markdown handoff 提供可检查产物。没有自动学习新技能、语义冲突检测、向量数据库、夜间 consolidation 或精确 token 计费。这些都可独立增加，不改变 task/dispatch/acceptance 契约。
