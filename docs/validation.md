# 首版验证记录

验证日期：2026-10-21。本记录描述已执行的检查，不保证将来账号权限或 CLI 行为不变。

## 离线行为测试

`python3 -W error::ResourceWarning -m unittest discover -s tests -v`：27 项通过。

覆盖：DAG 环与非法依赖、任务文件范围、过期 dispatch 拒绝、结构化结果校验、不同任务使用不同实例、审阅拒绝后的新实例返工、验证失败不进入审阅或记忆、只读角色写入拦截、审阅后文件变化拦截、原工作区不变、重复恢复幂等、中断恢复、存活进程拒绝重复派发、配置变化拒绝恢复、超时清理进程组、MCP stdio 握手、两种客户端 MCP 配置、hooks 拒绝执行、记忆替换与历史、TTL、项目隔离、分层读取和上下文预算。

三个共用 SKILL.md 均通过 skill-creator 的 `quick_validate.py`。Python 模块语法检查通过。

## 真实模型联调

本机环境：Codex CLI 0.163.0、Claude Code 2.1.306；复用本机已有登录。

| 流程 | 运行 ID | 结果 |
| --- | --- | --- |
| `gpt-6-astra` → `claude-opus-5-5` → `gpt-6.1-sol` | `run_7f752925d60f4388` | completed |
| `claude-fable-5-1` → `claude-opus-5-5` → `gpt-6.1-sol` | `run_dacde964d01141b9` | completed |

两次均在独立临时 Git 仓库使用预写计划，实现 `greet(name)`，运行真实 unittest，再经独立 Sol 审阅通过。Opus 的供应商结果记录模型为 `claude-opus-5-5`，两次均无权限拒绝。通过后生成本地分支提交、记忆和 handoff。

这验证了实际 CLI 调用、结构化派发、工具写入、测试与审阅闭环。真实联调使用预写计划，未覆盖复杂需求的自动拆解质量；自动计划的 schema 和 DAG 调度有离线行为测试。OpenWiki 外部服务未部署，Orca 桌面编排未接管。

可复现命令：

```bash
python3 -m harness smoke
python3 -m harness smoke --commander fable
```

离线模拟入口 `python3 -m harness demo` 不调用模型，不能代替以上两次真实联调。
