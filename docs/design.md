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

`planning → running → completed`，异常进入 `blocked` 或 `interrupted`。这些是本地运行状态，与任何外部产品的自动化目标状态无关。

一个任务只有在以下条件同时满足时才进入 accepted：

1. 当前 dispatch 返回匹配 ID 的合法结果，声明完成且无 blocker。
2. Git HEAD 未被 agent 改动，文件变更落在任务的 allowed_paths。
3. 所有配置验证命令成功，且没有修改待审阅文件。
4. 新 Sol 实例审阅通过，没有未处理 findings。
5. 接受前文件快照等于审阅时快照。

分支提交、结果记忆和台账都带来源引用。review 失败或测试失败只保留诊断，不提升为长期成功经验。权限错误、非法输出和范围违例停止运行，避免把运行时故障误作普通返工。

## 为什么先串行

多模型分工不要求同时写代码。串行 DAG 能先验证模型路由、一次性实例、门禁和恢复协议；每个 run 独立工作树保证原始工作区不受普通任务编辑影响。后续并行需要进一步实现每任务工作树、依赖提交集成和冲突处理，不能只加线程池。

## 仍需明确的限制

这不是恶意代码隔离系统。原生 CLI、配置的 hooks、验证命令和 MCP 服务在用户权限下运行；只读声明、prompt 和 Git 范围检查不是完整的 OS 权限边界。强隔离应使用专用 OS 用户或容器，并在那个边界内运行本编排器。

SQLite 是长期记忆的存储真相；JSON 导出和 Markdown handoff 提供可检查产物。没有自动学习新技能、语义冲突检测、向量数据库、夜间 consolidation 或精确 token 计费。这些都可独立增加，不改变 task/dispatch/acceptance 契约。
