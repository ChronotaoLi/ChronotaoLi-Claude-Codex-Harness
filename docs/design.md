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

分支提交、结果记忆和台账都带来源引用。review 失败或测试失败只保留诊断，不提升为长期成功经验。身份不匹配、结构或语义非法、权限错误和范围违例停止运行；有界格式修复与这些硬错误分开，不能据此放宽接受条件。

审阅问题使用稳定 ID、严重级别、类别、位置、说明和证据。偏好只能是 advisory。返工审阅收到历轮问题与对应证据，既要复核旧问题，也要独立检查整个当前修改；运行时不因为 worker 的辩解而豁免门禁。原始目标和任务义务独立保留，全部任务接受并提交后重新执行验证，并由新的 reviewer 审阅原始 base 到当前 HEAD 的完整 diff。任务验证发生在接受提交之前，最终验证覆盖提交后的状态，不能以命令相同为由省略。最终文件快照不变才允许 completed。整体拒绝需要通过正常门禁的修复任务，重复 resume 不能对同一拒绝结果重新抽签。

文件范围从仓库根目录匹配，普通 `*`、`?`、字符集合只作用于单个路径段；独立 `**` 路径段匹配零个或多个层级。因而 `src/*` 不包含 `src/a/b.py`，`**/*.py` 包含根目录的 `main.py`。规划时拒绝不合法范围、显式受保护根路径和不存在的 skill；接受门禁仍独立拒绝受保护路径变更。路径不做裁剪或 Unicode 规范化，以免改变实际文件名。

## 调度、再规划与恢复

当且仅当一个任务就绪、没有失败反馈且尚未尝试时，运行时可直接发送包含目标、任务、依赖结果、验收和可用命令的完整简报。选择或返工仍交给指挥。指挥是否复用会话与是否支持再规划是两个独立问题，本设计保留每次重新组装上下文。

再规划只处理任务拆分、缺少前置工作和整体验收发现的工程缺口。worker 通过 `blocker_kind` 区分 task 与 environment；CLI 错误、权限、非法结果、范围违例和现场不一致不会被包装成自动再规划。指挥返回 `{action, reason, new_tasks, updates}`：每个 update 仅包含 `id`、`description`、`add_depends_on`，stop 必须提供空操作数组。运行时在原计划上应用增量并重新检查 DAG，要求：

- 保留每个既有任务 ID，已接受任务完全不变。
- 未接受任务只可更新描述并添加依赖；标题、skills、验收和 allowed_paths 均从台账保留，已有依赖不得删除。
- 新任务只使用已有 allowed_paths 中的模式；不通过推测 glob 包含关系放大范围。
- 原始任务继续承担最终集成义务，拆分不能删除或重命名它来避开失败。
- 每次修订保存版本与原因。校验通过后先持久化待应用修订，再归档未接受修改并恢复隔离工作树，保留已接受提交。归档阶段中断时，恢复继续应用同一修订，不重复调用指挥或增加再规划次数；新的归档尝试保留独立目录。

每任务尝试数、整个 run 的 worker 尝试数、再规划数和最终验收次数均持久保存且受上限约束。分别由 `max_attempts`、`max_total_attempts`、`max_replans`、`max_final_attempts` 控制；最终验收默认 3 次，独立于任务预算。新任务与 resume 都不清零累计预算。每任务耗尽后仍停止，操作者可显式增加允许调整的预算再恢复。

恢复配置仅允许增加上下文预算、agent/验证超时、Claude 轮数、每任务尝试数、总 worker 尝试数和最终验收次数；所有变化记录旧值和新值。模型 ID、供应商、effort、工具权限、验证命令、再规划上限和共享资产不允许在同一 run 中改变。进程存活和 Git HEAD 检查仍在恢复前执行；无法探测进程组权限时按仍存活处理。台账版本 3 不自动迁移旧运行，以免替旧证据补造新接受语义。

普通运行错误记录类型、证据并进入 blocked；用户中断进入 interrupted。验证超时在清理进程组后记录为 `timed_out`、退出码 124，进入正常验证反馈。缺少 CLI、损坏供应商 JSON 等明确阻断，不作通用网络重试，也不切换模型。默认配置属于 Harness 自身，`--repo` 不自动选择项目配置；真实项目应显式指定 `--config`，启动时显示所用配置和验证 argv。

计划确认和单步执行为显式选项：默认连续执行；选择暂停确认后必须显式批准。任何无回复或重复恢复都不能代替批准。step 以一个已接受任务为边界；最后一个任务之后还需要整体检查。

## 项目记忆的提升与过时

任务历史与可复用项目知识分别保存。任务结果使用 run/task 唯一 key，不互相覆盖。接受台账、后续依赖简报和任务记忆使用 reviewer 基于代码与证据独立形成的摘要；worker summary 留在执行证据中，不直接提升为成功知识。worker 可提交有界的知识提案，但没有直接写记忆工具。审阅者逐项核实来源并返回 approved_memory_keys；代码通过不能隐含批准提案。

必要上下文保留原始目标、验收、范围和未解决问题，日志与重复内容通过证据引用缩减。每条验证命令的 stdout/stderr 摘录合计最多 500 字符，保留 argv、退出状态和完整日志路径。审阅历史仅合并完全相同的 finding；变化后的严重程度、说明或证据仍完整可见，旧摘要保留产物引用。当前计划与原始计划相同则传入 `tasks_ref: "original_tasks"`，避免重复嵌入。最终文件快照独立落盘，台账只保存内容摘要和路径；门禁仍重新哈希完整内容，不使用 mtime/size 缓存代替完整性检查。必要输入仍超预算时停止，允许操作者增大预算或拆小任务。

只有已接受任务中的明确获批提案才能写入 `knowledge:` 命名空间。写入校验相对来源路径、文件哈希、批准 key 和操作者所有权；存在操作者历史的 key 不得被自动提案替代。可选记忆的来源或所有权不满足条件时，记录诊断并丢弃该批提案；非法审阅批准 key 属于协议错误，阻止接受。

自动任务历史和项目知识默认有 180 天召回期限，操作者事实默认无期限；均保留历史证据。知识来源改变或提交不可核实时，召回标记 possibly_stale 并降权。类型权重只作为相关性排序的辅助信号，不把人工记录视为永远正确。没有自动模型整理或语义冲突覆盖。

## 共享工具与 hooks

生命周期 hooks 是可配置的运行时门禁，默认不附带策略。初始化时完整校验事件对象、脚本列表、argv 和正有限超时值，任何事件的无效配置都会在脚本执行前被拒绝。上下文组装事件命名为 context_prepared，保留旧名 context_compacted 作为互斥别名。暂不增加原生工具 hooks 翻译层：两端事件语义和权限效果需要按实际需求单独验证。

pre_accept 前持久化审阅通过的快照摘要、证据和待接受阶段。hook 拒绝、超时或无法启动时保留结构化错误与日志引用；恢复时快照相同则只重试门禁，无需重复派发该任务的 worker/reviewer，且每次尝试保存独立日志。快照变化则丢弃旧批准，重新执行任务、验证及审阅，不能把先前结论用于新文件。post_accept 的待执行状态与接受台账一起保存，失败后恢复先重试该门禁，成功后才清除。这些 hook 具有至少一次执行语义，脚本需能处理重复执行。共享资产摘要覆盖配置文件和共享规则、skills，不递归追踪任意脚本及其依赖，后者由配置维护者保证一致。

Git 文件快照还包含暂存区条目摘要；范围检查同时检查暂存变更，提交前从已接受 HEAD 重建暂存区，再仅加入审阅过的路径。再规划归档同时保存工作区与暂存差异，遇到路径祖先是符号链接时停止，避免归档写入跟随链接。验证产生的未忽略文件也属于快照变化，会留下文件差异诊断；应配置或忽略实际生成产物，不普遍豁免新文件。

Claude 只读角色获得受限 Git 查询命令，worker 可配置额外的精确 argv 授权，包括必要时带明确路径的删除或移动命令；默认不开放通用 `rm`、`mv`。CLI 授权是能力配置，不能替代 OS 隔离。Codex 适配器当前只提供只读模式，配置其为 worker 时提前拒绝；不会替换模型来迁就适配器。

完整输出契约保存在 schema.json 并由本地校验。两种供应商使用仅含基础类型、字段、必填、枚举与数组项结构的投影，另存 provider-schema.json；模型输入的 output_constraints 从同一契约生成长度、数量、非空、唯一性及标识符格式限制，本地仍强制执行，不假设两端支持相同 JSON Schema 扩展。

校验先检查身份、结构、权限及任务／审阅语义，再检查这些可修复格式限制。仅格式错误允许每个角色／调用类型／任务组合自动修复一次，额度写入台账且不随 resume 清零。修复启动新进程、新 dispatch，传入错误和上次证据引用，必须重新检查当前文件；worker 修复同时消耗任务及整个 run 的尝试预算。缺字段、错类型、无效枚举、身份不匹配、越权、语义矛盾或 CLI 故障均不走该路径。

worker 已报告 blocker 时，即使同时存在格式错误也保留阻断并停止，不自动重派。reviewer 格式修复必须保持原 verdict、获批知识 key 集合以及所有 finding 的数量、顺序、严重程度、说明和证据；只允许改写摘要、修正非法 finding ID 和去重批准 key。该约束与快照一起持久化，恢复不能通过重抽审阅消除原有阻断问题。无法在保留证据的前提下满足格式限制时停止。

## 为什么先串行

多模型分工不要求同时写代码。串行 DAG 能先验证模型路由、一次性实例、门禁和恢复协议；每个 run 独立工作树保证原始工作区不受普通任务编辑影响。后续并行需要进一步实现每任务工作树、依赖提交集成和冲突处理，不能只加线程池。

## 仍需明确的限制

这不是恶意代码隔离系统。原生 CLI、配置的 hooks、验证命令和 MCP 服务在用户权限下运行；只读声明、prompt 和 Git 范围检查不是完整的 OS 权限边界。强隔离应使用专用 OS 用户或容器，并在那个边界内运行本编排器。

SQLite 是长期记忆的存储真相；JSON 导出和 Markdown handoff 提供可检查产物。没有自动学习新技能、语义冲突检测、向量数据库、夜间 consolidation 或精确 token 计费。这些都可独立增加，不改变 task/dispatch/acceptance 契约。
