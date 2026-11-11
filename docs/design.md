# 设计：架构、不变量与状态转换

模型是可替换的决策与执行部件；任务状态、权限边界、证据和接受条件留在确定性的运行时。本文描述台账版本 17 的系统。用法见 [README](../README.md)，停止后的出口见[恢复与操作者出口](recovery.md)，工具接口与观察见[操作与观察](operations.md)，历史改动见 git log 与[验证记录](validation.md)。

- [架构](#架构)
- [不变量](#不变量)
- [状态转换](#状态转换)
- [参考项目](#参考项目)
- [厂商接口依据](#厂商接口依据)
- [仍需明确的限制](#仍需明确的限制)

## 架构

一个 **run** 对应一个目标、一条分支 `harness/<run_id>`、一个隔离工作树 `.harness/worktrees/<run_id>` 和一份台账 `.harness/runs/<run_id>/state.json`。run 内的 **task** 构成串行执行的 DAG，各带验收条件和文件范围 `allowed_paths`。一次 **dispatch** 是一次模型调用（新进程、新 UUID）。三种角色：**指挥**（commander，调用类型 `plan`、`dispatch`、`replan`）拆分、选择和再规划，连续性只来自台账（唯一就绪、没有反馈且未尝试过的任务直接派发，不调用指挥）；**执行者**（worker，`implement`）完成一个任务的一次尝试，是唯一有写工具和 `harness_commands` 的角色；**审阅者**（reviewer，`review`、`final_review`）只读地审阅任务候选或整条分支。

| 模块 | 职责 |
| --- | --- |
| `__init__.py`、`__main__.py`、`cli.py` | 命令行入口；`status` 摘要、`doctor`、`demo`／`smoke`、`sync`、`permission-diagnostics` |
| `runtime.py`、`services.py`、`run_context.py` | 生命周期 API `Runtime`；组装命名服务；当前 run 的台账、事件、hooks、快照 |
| `run_store.py` | 台账唯一提交入口（不变量 27） |
| `run_lifecycle.py` | 新建 run、`load_run` 校验、恢复预检与选项互斥、`abandon` |
| `scheduler.py`、`workflow.py` | 唯一调度循环；停止记账、交接目标、决定请求同步、worker 失败来源 |
| `planning.py`、`requirements.py` | 计划登记、再规划与候选保留、范围授权；任务命令前置条件 |
| `dispatch.py`、`packets.py`、`context.py`、`input_projection.py`、`path_evidence.py` | 派发、预算、`invoked`、无效输出的重做；角色输入；上下文与容量报告；输入和路径索引 |
| `receipts.py`、`worker_receipts.py`、`worker_returns.py`、`recovery.py` | 指挥与审阅回执与判定；worker 接收意图、封存、判定、交接；无结果判定；恢复忙碌键 |
| `task_lifecycle.py`、`finalization.py`、`verification.py`、`verification_plan.py`、`review_progress.py` | 任务验证、审阅、接受；整体验收；验证命令；候选预检；必需问题与审阅来源 |
| `budgets.py`、`diagnostics.py`、`convergence.py` | 工作尝试与豁免；预算解释与剩余调用下界；返工提示与审阅轮次 |
| `continuation.py`、`environment.py`、`decisions.py`、`progress.py` | 续作、续作拒绝、执行指导；环境更新与前置检查；操作者决定；检查点 |
| `adapters.py`、`process_runtime.py`、`util.py` | CLI 参数与模拟后端；进程组、截止时间与退出事实 |
| `command_server.py`、`command_entry.py` | worker 专属 `harness_commands` MCP：`list_commands`、`run_command`、`delete_path` |
| `mcp_server.py`、`mcp_entry.py`、`file_tools.py`、`git_query.py` | 三种角色共用的只读 `harness` MCP：记忆、技能、运行状态、文件工具、`git_query` |
| `git.py`、`memory.py` | Git 绑定、快照、范围、接受提交与归档；SQLite 项目记忆 |
| `shared.py`、`contracts.py`、`config.py`、`versions.py` | 共享资产摘要、MCP 校验、hooks；输出契约；配置；台账版本 |
| `private_state.py`、`workspace_inputs.py` | `.harness` 私有根保护；依赖副本清单 |
| `cleanup.py` | `cleanup-run`：共同门禁之后分别处理全新验证检出与分支工作树 |
| `observe.py`、`dashboard.py`、`dashboard_page.py`、`watch.py`、`watch_cli.py` | 只读观察：列表与详情、本地网页、原生文件通知 |
| `operations.py`、`supervisor.py` | 操作记录；`supervise` |
| `permission_diagnostics.py`、`permission_smoke.py`、`tool_smoke.py` | 权限诊断；真实账号联调入口 |

每次调用的输入由三部分组成：`shared/RULES.md` 与该角色的 skill（协议说明，包括各类摘录与索引的读法），以及包（`packet`）。包放数据、索引和现场事实：环境更新、操作者决定和检查点在包里是有界摘录加 SHA256 绑定的索引，与本次任务无关时只带索引。环境、决定、检查点、执行指导、权限、审阅、路径索引、命令目录、问题进度和计划增量的说明不在包里。仍在包里的静态说明及其去向：指挥 plan／dispatch／replan 的 `instruction`、默认 worker 指令和续作拒绝后写入拒绝记录的 worker 指令（随指挥调用合并改写，B4）。必需问题各行的读法（键、来源审阅、原件中的序号、可重复的标签）在 RULES 里，包里只有 `required_findings` 与可选的 `required_finding_sources` 数据。另有几个分类标签（`instruction_file_changes.trust`、`ignored_paths.coverage`、环境检查结果的 `coverage`）、`verification_plan.policy` 的布尔标记、操作者决定里替代非法问题的有界说明，以及提示词中可选记忆前的分隔标题，它们描述具体数据，不是协议说明。worker 包的 `instruction` 是指挥写的指令，属于数据；指挥包另带 `workspace: {head, clean}`，在构造包时同时读取 `expected_head` 和工作树 `git status` 是否干净。

`dispatches/<id>/` 保存一次调用的输入（worker 的 `packet.json`、`schema.json`；`packet.json` 的 `required_findings` 就是该 worker 的键到问题的对照）、输出、`returned-result.json`、`worker-seal.<SHA256>.json`、命令清单 `commands.json` 与 `commands/<call>/` 下的记录。

## 不变量

**派发与进程**

1. 每次 worker、reviewer 和指挥调用都是新进程、新 `dispatch_id`，无效输出的重做也是新调用；Claude 用 `--no-session-persistence`，Codex 用 `--ephemeral`，原生子 agent 关闭，不恢复供应商会话。模型 ID、供应商和 effort 按配置原样使用，同一 run 内不变，出错不换模型。
2. 调用身份与预算先于进程启动保存：本地准备（上下文、`pre_agent`、MCP、快照、额度检查）成功后，`attempts`、`agent_dispatches`、接收意图和 `invoked=true` 同一次保存，然后才调用后端。本地准备失败不扣额度，后端失败照常计数。
3. 退出未确认就不继续：恢复、放弃和清理要求父 dispatch 及其每个命令子进程都有退出证据；`starting` 或缺 pgid 且未确认的记录阻塞。`--confirm-exited` 是审计过的操作者断言，不补造退出码、输出或成功。
4. 领导进程正常退出后，同组后代最多观察 2 秒，仍未退出就停止消费结果，不向身份不明的进程组发信号。
5. 台账的 `invoked` 是调用事实的权威。事件 `dispatch_prepared`（预算检查前）、`dispatch_started`（`invoked=true` 保存后）和带 `invoked` 的 `dispatch_finished` 只是尽力而为的观察：追加失败不阻止调用，崩溃可能缺事件且不补发；按 `dispatch_id` 去重，费用只按台账统计。

**结论与证据**

6. 已返回的结果不重抽：调用前的接收意图绑定原请求、候选快照和返回位置；恢复时先处理已返回的原件，缺失或被改即停止。有效的指挥与审阅结论，以及从原件可靠读出的否决事实，不因重做或 resume 消失。否决事实包括审阅的 `request_changes`、blocking finding、批准中未 `resolved` 的必需项，指挥再规划的 `stop`，以及 worker 的 `blocked`；可靠读出的公共前提是退出已确认、原件字节已绑定摘要、严格解码成对象（拒绝重复键与非有限数，包括溢出成无穷大的数值字面量）、envelope 身份相符。运行时只写严格解码能读回的 JSON：要写出非有限数时停止，而不是写出自己读不回的原件。审阅的否决另要求核心结构合法（拒绝要带着可编号的 findings 返工），指挥的 `stop` 另要求整份结果结构合法；不满足时按读不出否决处理。没有通过契约、也读不出否决的输出不是结果：原件保留，故障记在它的派发条目上，由同一角色的新进程重做（不变量 11）。
7. worker 结果先封存再交接：确认 worker 与命令子进程退出后，把结果、来源文件摘要、返回后候选、HEAD、Git 绑定和进程事实写成内容寻址的 `worker-seal.<SHA256>.json`，再做契约、范围、`post_agent` 与交接。未封存的原始输出不补绑恢复时的工作树。
8. 无结果判定绑定原始证据：确认退出后，按 Claude 的 JSON 输出（`process.stdout`）和派发目录中的结果文件把来源判为 retryable、invalid、returned 或 unknown。retryable（没有输出，或完整的显式错误）与 invalid（输出完整，但严格解码不出对象结果）都允许新的计费尝试：可能中断的步骤（分类、为恢复记录读取 Git）先完成，然后判定、判定证据、故障来源和回执消费同次保存，结局分别是 `provider_failure` 与 `result_rejected`；在此之前中断，条目与回执都保持判定前的样子。适配器当场遇到解不出的输出时，走与恢复相同的判定代码（适配器与判定共用一个解码函数），所以是否在判定保存之前中断不改变结局。returned（已有能解码成对象的结果但没有封存）或 unknown（与原始输出冲突、超过读取上限、读文件失败）时，判定与停止原因同次保存，回执保留，`--confirm-exited` 不能解开。判定记录所依据文件的 SHA256，证据删改后不能改判。
9. 不可变证据先发布，再登记引用：packet、契约、封存、续作类记录、环境更新、决定和检查点先写成内容寻址或带 SHA256 的文件，再在台账存路径与摘要。文件写入失败时不登记引用；worker 输入写入失败时也不扣调用预算、不保存接收意图或 `invoked=true`，但此前保存的准备条目与随后的失败状态、诊断仍会落盘。未引用的遗留文件被忽略，同内容重试时复用。
10. 台账对 worker 输入和续作类记录只存摘要与引用：接收意图存 `packet.json`、`schema.json` 的路径与 SHA256；续作、执行指导、续作拒绝行只存 `snapshot_digest`，整棵快照只在不可变文件里。引用文件缺失、被改或换成符号链接时，恢复停止并保留现场。
11. 无效输出的重做有界。指挥或审阅的一次请求返回读不出否决的无效输出时，在同一次保存里记录错误与原件摘要（`status=invalid`、`invalid_output`）并消费回执，然后自动重做一次；是否重做由整个 run 最后一次模型调用推导（它是同一调用的无效输出且本身不是重做），重做条目记 `retry_of`，输入附上一份无效输出的引用与前 2,000 字符的错误（带上它超出上下文预算时去掉）；这份原件在本地准备（含 `pre_agent`）之后、调用计费之前再按摘要核对一次，缺失或被改即按证据损坏停止。重做仍无效则停止；此后的普通 resume 发起新请求，规则相同。worker 的无效输出记为一次工作尝试，回执消费，调度继续，受 `max_attempts` 约束；blocked 的输出保持约束力：核心结构与语义都合法、只有界限不合格的 environment／operator／task 阻断按原路由，原结果进入 feedback；结构或语义不合法的 blocked 输出按 task 阻断进入再规划，feedback 不带原结果；都不给尝试豁免。所有重做都计入 `max_agent_dispatches`；证据损坏或退出未确认时不重做。worker 的 `checkpoints`、`proposed_memories` 与审阅的 `approved_memory_keys` 不参与判定，不合格只丢弃自身并记事件；后续上下文只用它们经过校验、有界的投影（检查点只以登记后的 `task_checkpoints` 出现，记忆提案只投影整批合法时的 key，记忆批准只在接受时起作用、不进入后续上下文），被丢弃的部分不投影，原件保留原值；`decision_requests` 不合格即为无效输出。越出已批准范围的修订不是无效输出：保留回执等待 `--allow-scope`。

**接受**

12. 任务接受同时要求：合法结果声明完成且无 blocker；任务的证据要求在候选中都是普通文件，由运行时检查并记录摘要，不取 worker 的声明；Git 绑定与 HEAD 未变，改动（含暂存区）在 `allowed_paths` 内；全部验证命令成功；新审阅实例批准、无 blocking finding，并且按固定键给出的必需问题处置全部为 resolved；接受前快照等于审阅时快照。必需问题由审阅历史在当前候选上推导：处置只在它的审阅快照等于该候选、且该审阅可用时关闭问题。
13. `pre_accept` 前后都重新哈希完整快照（含暂存区），不用 mtime、size 或 Git 的未变判断；快照变化时旧批准持久标为失效，任务重新执行、验证和审阅。
14. 提交与接受记录共用一道屏障：commit 后，`accepted`、`expected_head`、`pending_post_accept` 同次保存，同时清除 `pending_acceptance`、`current_task` 和反馈。commit 已落盘而台账未保存时，恢复因 HEAD 不符停止，不自动修复。
15. 全部任务接受后重新验证，再由新审阅者审阅 base 到 HEAD 的完整 diff；最终快照不变才 `completed`。同一快照的整体拒绝不能靠 resume 重抽，只能由通过正常门禁的修正任务改变候选。
16. Git 绑定在创建工作树后按实际解析结果保存（`.git`、gitdir 反向链接、公共目录、预期分支）；每个工作树 Git 命令显式指定 git-dir 与 work-tree 并剔除 `GIT_*` 环境变量，操作前后核验绑定。接受、归档、恢复、移除和记忆来源核验都使用它。
17. `allowed_paths` 从仓库根匹配，`*`、`?`、字符集只作用于单段，独立 `**` 段跨层级；`.git`、`.harness`、`.codex`、`.claude` 在规划和接受时都被拒绝，工作树内的 `.harness` 一律拒绝；索引含 gitlink 的仓库在创建工作树前拒绝。
18. 接受记录和任务记忆用审阅者的摘要；记忆提案须审阅者逐项批准才写入 `knowledge:`；记忆都是参考（`reference_only`），不授予权限或替代决定。

**预算**

19. 预算只增不减：`attempts`、`agent_dispatches`、`replan_attempts`、`final_attempts` 累计保存，resume 不清零。恢复只允许提高超时、上下文、轮数、各项上限与 `max_tasks`，以及追加精确 `worker_commands`。
20. `max_attempts` 按每个任务的工作尝试计数：worker 调用数减去已完整校验、与反馈同次保存的 environment／operator 阻断（`attempt_exemption`），豁免在使用前复核原始结果。没有全 run 的 worker 尝试上限；再规划新增的任务各有自己的 `max_attempts`，全局由计入每次模型调用的 `max_agent_dispatches` 封顶。`max_tasks` 限制计划任务数。
21. 观察投影不改变预算：`status` 的 `budget_explanation`、`remaining_model_calls`（下界）、`convergence`、`review_rounds` 只读已保存事实。`convergence`、`review_rounds` 跨轮比较的是 finding 标签（审阅者写的 id），只作观察。

**权限与工具**

22. worker 应优先用 broker 的 `run_command` 执行命令；broker 只接受 `verification.commands` 与 `permissions.worker_commands` 中的完整 argv，不能附加参数、改 cwd 或 env，不经 shell。Claude worker 另有 CLI 层的 Bash 许可：`config.py` 的只读前缀（参数只能接在 `--` 之后），以及已配置命令中不含 `*`、换行的精确 argv；这些调用不经 broker、不留命令记录。任务的 `required_commands`、`self_checks` 是前置条件不是授权；缺授权时不启动 worker、不扣尝试。
23. `git_query` 只执行形式表内的 argv，表外 argv 不执行即返回工具错误。同一解析函数决定执行与“已覆盖”：计划前置命令、续作命令请求、环境检查和权限诊断把表内形式算作已满足——只表示能力存在，不表示已执行或成功，也不是 Bash 或 broker 授权。
24. 命令与删除先发布 journal：每次调用先在 `command-staging/` 写好完整 `process.json`，再一次原子改名进 `commands/`；发布前不启动子进程、不删除文件。保护：退出门禁不会遇到缺 journal 的目录。
25. `delete_path` 保证：路径从工作树根目录描述符逐级以 no-follow 打开；不跟随符号链接；最后一次核对时，最终项是 HEAD 中已跟踪、在任务范围内的普通文件；每次被准入的调用都留下记录。不保证：在最后一次核对与 `unlinkat` 之间，能在工作树内改名的并发进程可以让被删除的文件位于别处，包括工作树外；事后的并发变化检测是尽力而为，可能漏报（例如目录被移回）。删除立即生效，接受门禁只决定候选能否被接受，不能撤销删除。
26. 角色能力不是 OS 隔离：Codex 只读角色用 `--sandbox read-only`，Claude 只读角色只有读工具与只读 Git 命令，Claude worker 的 Edit／Write 限于工作树并拒绝 `.git`，Codex 不能作为 worker。候选中的 `AGENTS.md`、`CLAUDE.md` 等指令文件是待审阅数据，不授予权力；两种 CLI 都关闭项目指令自动注入。

**台账、版本与观察**

27. 台账只经 `RunStore` 提交：在 `store.lock` 下比较已加载字节的摘要，拒绝旧写入，原子写入并递增 `store_revision`；外部动作前也核对当前权威。`run.lock` 保护生命周期。
28. 旧版本台账不迁移、不执行：`resume`、`abandon`、`prepare-run` 只接受版本 17；版本 16 及更早仍可用只读与诊断入口（`status`、`watch`、`dashboard`、`permission-diagnostics`）和受门禁保护的 `cleanup-run`（版本 7–17，按各自形状核对；版本 15、16 的 worker 回执按其意图保存的输出模式、用当时的分类规则原样复核）；不补写旧默认值。resume 另核对共享资产摘要（不含 hook 脚本的依赖）和完整配置快照。
29. hooks 至少一次：`post_agent` 只作用于通过契约校验的结果和 `invalid_rejected` 审阅（读不出否决的无效输出、指挥的无效 stop 与 worker 的无效阻断不运行它；worker 交接请求的目标在 `post_agent` 与检查点登记之后才检查，不合格时这份结果在 hook 之后才判为无效）；`pre_accept`、`post_accept`、`on_error` 执行前递增计数并用独立日志目录，中断后可能重复。配置出现 `context_compacted` 时报未知事件并提示改用 `context_prepared`。
30. 必要输入超过 `context_chars` 时不调用模型、不扣额度，报告仍写入 `context-report.json`；可选记忆按整条加入剩余空间。成功派发的报告保留根与顶层字段尺寸及最大的 30 个嵌套字段并给出省略数，超限报告保留全部明细。
31. 观察层（`status`、`watch`、`dashboard`、操作记录、`supervise` 回执）不写台账、不取 `run.lock`、不参与恢复。观察网页仍保留已删除事件（`output_format_repair`、`invalid_output_repair_requested`、`worker_result_rejected`）的显示名，只用于显示旧台账里的事件；运行时不再写这些事件。

**身份**

32. 模型不回填运行时拥有的身份。必需问题的处置和 worker 的修复声明使用运行时按当次必需项生成的固定键 `f1…fN`：键全部必填、不许多余键；没有必需项时不出现该字段；审阅有必需项时必须给出 `finding_dispositions`。审阅的键到来源的对照在派发前保存，审阅的重放与重做都用同一份：任务审阅在 `pending_task`，整体审阅在该次整体记录；worker 的结果只按它自己那次派发已发布的 `packet.json` 映射。审阅记录带一份对照副本；处置只经副本映射，副本必须逐行等于在同一历史前缀和同一候选上重算的清单。任务证据由运行时按计划检查。finding 的内部身份是（来源审阅 dispatch，原件中的序号），模型写的 id 只是标签，可以重复。envelope 的 `task_id`、`dispatch_id` 是单值枚举，照旧回填。

## 状态转换

### run 状态

| 状态 | 进入 | 离开 |
| --- | --- | --- |
| `planning` | 首次保存 | 计划登记后进入 `running` 或 `awaiting_approval` |
| `awaiting_approval` | `--pause-after-plan` 且计划未批准 | `resume --approve-plan` |
| `running` | 每次进入执行循环，同时清除 `error`、`error_type` | 停止、暂停或完成 |
| `paused` | `--step` 下本次已接受一个任务 | 普通 `resume` |
| `blocked` | 执行中出错：先保存状态与错误，再写 `run_stopped`、运行 `on_error` | 对应恢复出口或 `abandon` |
| `interrupted` | Ctrl-C、SIGTERM、SIGHUP | 普通 `resume` 或 `abandon` |
| `completed` | 整体批准后保存 | 终态；普通 `resume` 返回保存结果 |
| `abandoned` | `abandon` | 终态；不能 resume |

### 新建台账的键

`run_lifecycle.py` 新建台账的全部顶层键（`store_revision` 由首次提交加入）；“恢复”列是中断后 `resume` 的处理。

| 键 | 设置与更新 | 清除 | 持久化顺序 | 恢复 |
| --- | --- | --- | --- | --- |
| `schema_version`、`id`、`repo`、`goal`、`commander`、`branch`、`worktree`、`base_commit`、`config_path`、`asset_digest`、`created_by_operation` | 首次保存 | 不变 | 早于创建工作树 | 要求版本 17、ID 与仓库一致；比较共享资产摘要 |
| `config_snapshot` | 首次保存；resume 接受合法放宽后替换 | 不清除 | 先写 `resume_config_relaxed` 再保存 | 与当前配置比较，只许放宽 |
| `status` | 见上表 | — | 先保存再运行 hook | 终态拒绝修改选项 |
| `worktree_binding`、`ignored_baseline` | 首次保存为空；`git worktree add` 后第二次保存填入 | 不清除 | 第一次保存在创建工作树前 | 绑定为空时 resume 拒绝，只能 `abandon` |
| `original_tasks` | 供给计划在首次保存；模型计划与首个计划版本同次 | 不变 | 早于 `tasks` | `tasks` 为空且无未消费规划回执时，重新校验并登记同一计划 |
| `tasks`、`plan_history` | 计划登记、再规划应用 | 不清除 | 先写 `plans/revision-N.json` | 按计划版本继续 |
| `plan_approval_required`、`plan_approved` | 创建时；`--approve-plan` | 不变 | 先写 `plan_approved` 再保存 | 普通 resume 不批准 |
| `expected_head`、`accepted` | 任务接受 | 不清除 | 与 `pending_post_accept` 同次 | 工作树 HEAD 必须等于 `expected_head` |
| `attempts`、`agent_dispatches` | 调用后端前 | 只增 | 与接收意图、`invoked` 同次 | 已记账的调用不退回 |
| `replan_attempts` | 修订通过校验或保存 stop | 只增 | 与修订进入 validated、消费回执同次 | 失败的请求不计数 |
| `final_attempts`、`final_history` | 新的整体尝试追加 `running` 记录，随后更新阶段与结论；进入 reviewing 时与阶段同次写入该次审阅的对照 `required_findings`（在验证开始前算出） | 不清除 | 尝试记录先于快照产物；失败或拒绝与 `feedback`、`pending_replan` 同次 | 同一快照：未完成则复用该尝试，已批准则完成，已拒绝则要求修正任务 |
| `dispatches` | 准备完成追加（`prepared`、`invoked=false`）；调用前 `starting`、`invoked=true`，重做另记 `retry_of`（去掉了提示时记 `previous_invalid_omitted`）；启动记 pgid；结束记进程事实与用量；读不出否决的无效输出记 `status=invalid`、错误与 `invalid_output`，否决类记 `invalid_rejected`、`invalid_stopped` 或 `invalid_blocked` | 不清除 | 准备条目先于 `pre_agent`；`retry_of` 与预算同次；`invalid` 与回执消费同次 | 退出门禁读进程事实；`worker_receipt_consumed` 记 worker 回执结局；`retry_of` 必须指向它之前最后一次模型调用 |
| `feedback` | worker 阻断（含无效阻断，带 `output_error`；结构不可信时没有 `worker`）、审阅拒绝、验证或预检失败、候选变化、`pre_accept` 失败、整体失败、修订应用 | 接受、整体批准时置 null | 与引起它的转换同次 | 下一次指挥与 worker 的输入 |
| `review_history` | 任务审阅结束；候选变化前把已返回审阅存为 invalidated；每条记录带派发时的对照副本 `required_findings` | 不清除 | 与审阅后的阶段同次 | 返工输入、`review_rounds` 来源；处置只经副本映射，副本与重算清单不符即停止 |
| `worker_receipts` | 调用前存意图（`invoking`、packet／契约引用；worker 的必需问题对照在 `packet.json`）；封存后 `sealed`；`post_agent` 后记 `post_hook_digest` | 交接时消费 | 封存文件先于 `sealed`；消费与后续阶段同次 | 未封存：retryable 或 invalid（不可解码的输出，结局 `result_rejected`）则消费并新开尝试，否则停止；封存文件已写而 `sealed` 未保存：找到并核验该文件后继续；已封存：复核后继续原门禁 |
| `review_receipts` | 调用前存意图；返回后绑定 `artifact_digest` | 审阅结束；候选变化（先存原文）；无返回时重派前；无效输出判定时同次移除 | 结果文件先于摘要 | 有结果则校验同一结果；无结果才新派发并计费 |
| `commander_receipts` | 调用前存意图（含原请求与摘要；再规划绑 `request_id`） | 首版计划、任务选择、修订或 stop 登记时同次移除；无效输出判定时同次移除；越出范围的修订保留至授权 | 同上 | 校验同一结果，不重抽 |
| `approved_scope` | `--allow-scope` | 不清除 | 先写 `scope_extension_approved` 再保存 | 只扩大新任务可选范围 |
| `environment_updates` | `--environment-update` | 不清除 | 先写 `environment/<id>.json` 与事件 | 同任务最新一条替代旧检查列表 |
| `environment_checks` | 选中任务每次 worker 尝试前 | 不清除 | 报告文件先于追加 | 检查失败不启动 worker、不扣尝试 |
| `decision_requests` | operator 阻断、指挥 stop、来源任务接受且 `post_accept` 完成 | 不清除 | 先写 `decisions/<id>.json` 与事件 | 未答或答 stop 时阻止目标任务派发 |
| `operator_decisions` | `--operator-decision` 整批 | 不清除 | 全部答复文件先写，再一次保存 | 只解除匹配请求的等待 |
| `task_checkpoints`、`checkpoint_batches` | worker 结果交接时逐项核对 | 不清除 | 证据副本、manifest、批次文件先写 | 批次文件已写未登记时登记同一结果 |
| `continuation_history` | worker 返回 `blocker_kind=continuation` | 不清除 | 先写 `continuations/<id>.json`；与 `pending_continuation` 同次 | 条件满足后新派发继续 |
| `execution_guidance` | `--execution-guidance` | 不清除 | 先写 `execution-guidance/<id>.json` | 进入该任务后续输入 |
| `workspace_inputs` | 首次为 null；`prepare-run` 写入清单摘要 | 不清除 | 例外：绑定先保存，再写冻结清单和副本回执；缺失时失败关闭 | resume 与每次派发前核对副本 |
| `store_revision` | 每次提交 | 只增 | 由 `RunStore` 写入 | 修订不符的写入被拒绝 |

### 运行中出现的键

| 键 | 设置 | 清除 | 持久化顺序与恢复 |
| --- | --- | --- | --- |
| `current_task` | 选择任务；选择转再规划；保留候选的修订 | 接受、需归档的修订应用 | 有当前任务时只调度它 |
| `pending_directive` | 指挥选择 `implement`（与回执消费同次）；续作拒绝 | worker 调用前与预算同次消费 | 本地准备失败时保留；恢复先核对快照 |
| `pending_task` | worker 交接（`verifying`，与回执消费同次）；预检通过后与验证轮次同次保存审阅对照 `required_findings`；验证后 `reviewing` | 审阅结束（与 `review_history` 及后续阶段同次）；候选变化、预检或验证失败 | 同一快照复用已完成阶段；reviewing 阶段的审阅派发、重放和重做都只用保存的对照，缺少即停止 |
| `pending_acceptance` | 审阅批准 | 接受登记或候选变化 | `pre_accept` 前递增 `hook_attempts`；快照未变只重试 hook |
| `pending_post_accept` | 与接受同次 | 任务记忆、知识提案、`task_accepted`、`post_accept` 依次完成 | 每步完成即保存，记忆用稳定操作 ID 去重；完成前不派发后续任务 |
| `pending_replan` | task 阻断（含 worker 的无效阻断按 task 路由）、整体失败、指挥 `action=replan`、stop 被重开、空修正被接受 | 修订应用、保存 stop、允许的 `--reverify` 取消 | `pending` → `request` → `validated`；早于预算检查保存，提高预算后继续同一请求 |
| `replan_stopped` | 指挥 stop，含结构合法但其余不合格的 stop（带 `output_error`）；同次消费回执、移除 `pending_replan` | 匹配请求的 continue 或 `--retry-stopped` | 普通 resume 不重新派发 |
| `retained_candidate` | 只改当前任务说明的修订应用 | 该任务下一次 worker 调用前与预算同次 | 绑定原修订结果与完整快照，变化即停止 |
| `pending_continuation` | 续作登记 | 条件满足或被拒绝时置 null | 条件未满足时停止并列出缺口 |
| `continuation_rejections` | `--reject-continuation` | 不清除；由 worker dispatch 的 `continuation_rejection_id` 消费 | 拒绝文件先写；与 `pending_directive` 同次 |
| `worker_recovery` | 已调用的 worker 失败、结果或交接被拒 | 同 dispatch 正常交接、接受、需归档的修订 | 后续指挥与 worker 的 `recovery` 输入 |
| `last_runtime_error` | 调用或回执处理失败 | 不清除 | 诊断 |
| `error`、`error_type`、`error_hook_attempts` | 停止时 | 前两项在重新 `running` 时清除 | 先保存再运行 `on_error` |
| `command_requirements` | 计划登记、恢复、调度时重算，变化才保存 | 不清除 | 只是诊断，派发前总是重算 |
| `context_capacity` | 整体审阅容量预估，变化才保存 | 不清除 | 必要下界超限时提前停止 |
| `ignored_diagnostics` | 开始、worker 返回、验证结束 | 不清除 | 只是路径清单摘要 |

`recovery.py` 的 `RECOVERY_BUSY_KEYS` 列出共用的八个待处理键，环境更新、续作拒绝、执行指导和操作者决定在其中任一有值时拒绝（见[恢复选项](recovery.md#恢复选项与互斥)）。`load_run` 在任何副作用前检查这些对象的类型、所引 dispatch 的角色与任务、`pending_task` 与 `pending_replan` 互斥，失败时不改写台账。

清理状态不在台账里：`cleanup-run` 的删除意图与完成记录写在检出证据旁的 `cleanup.json` 和 run 目录的 `worktree-cleanup.json`，清理不改写台账（`--confirm-exited` 的退出确认除外）。已删除的 `abandon --remove-worktree` 曾写入 `pending_worktree_removal`、`worktree_removed`；新 run 不再写这两个键，`cleanup-run` 读旧台账时把它们当作已有的删除意图：工作树目录、Git 注册和管理目录都已消失时按已移除处理。

## 参考项目

参考时间：2026-10-21。

| 来源 | 采用的部分 |
| --- | --- |
| [stablyai/orca orchestration](https://github.com/stablyai/orca/blob/main/skill-guides/orchestration.md)、[CLI orchestration docs](https://github.com/stablyai/orca/blob/main/docs/site/content/docs/cli/orchestration.mdx) | run／task／dispatch 分层；每次尝试独立 ID；结构化结果校验；不根据空闲推断成功；退出未确认不重复派发 |
| [ECC](https://github.com/affaan-m/ECC/tree/90606c8315ee09cafc303515c5f33024db6b9882)、[ECC memory vault](https://github.com/affaan-m/ECC/blob/90606c8315ee09cafc303515c5f33024db6b9882/docs/design/ecc-memory-vault.md) | 规则、技能、hooks 一份源文件，CLI 只做适配；记忆带来源、按项目隔离、可导出 |
| [OpenWiki](https://github.com/Neuroant/OpenWiki#second-brain--agent-memory-path-b-opt-in) | 必要状态与可检索知识分离；旧版本保留；摘要引用证据；本地词法检索 |

Orca 按本机 CLI 1.4.237 参考，不运行它；“一任务一实例”比 Orca 可复用 worker 的策略更严格。OpenWiki 指 Neuroant/OpenWiki，不是 OpenViking。

## 厂商接口依据

- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)、[Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference)、[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)；[Claude Code CLI](https://code.claude.com/docs/en/cli-reference)、[Claude Code MCP](https://code.claude.com/docs/en/mcp)：非交互调用、JSON 输出与 schema、会话持久化、工具许可、MCP 配置。
- [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)、[MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)：逐行 JSON-RPC，stdout 只用于协议。
- [OpenAI 严格结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs#all-fields-must-be-required)：Codex 的 `provider-schema.json` 把每个对象的全部属性列为必填，计划任务因此必须带 `required_commands`（可为空数组）；本地 `schema.json` 仍是校验依据。
- [OpenAI models](https://developers.openai.com/api/docs/models)、[Claude models](https://platform.claude.com/docs/en/models/overview)：模型 ID 按配置保留，可用性由实际调用确认。实现对照了本机 Codex CLI 0.163.0 与 Claude Code 2.1.306 的 `--help`。

## 仍需明确的限制

- 不是恶意代码隔离系统：CLI、hooks、验证命令和 MCP 服务以用户权限运行；强隔离需要专用 OS 用户或容器。`delete_path` 不隔离并发进程（不变量 25）。
- 未发布的 `command-staging/` 准备目录不参与枚举，也不自动回收；反复失败会累积磁盘占用，不影响恢复。
- 调用前的台账保存失败时，`finally` 的保存可能留下已扣预算、`invoked=true` 的条目；恢复按可重试处理，保守地多计一次尝试。
- 接受时重建暂存区改变了已审阅的暂存区摘要、在 commit 前中断，恢复作废旧批准并消耗新的工作尝试。
- 进程组监督不是完整的 OS 进程树隔离；`--confirm-exited` 依赖操作者核实。
- 快照每次全量哈希，成本随仓库增长；忽略路径清单不校验内容；全新检出验证不保证 hermetic 构建。
- 记忆检索是本地词法排序，没有 embedding 或语义冲突检测；来源 `matching` 不证明事实正确。
- 串行 DAG；并行需要每任务工作树、依赖集成和冲突处理。
- 轮数上限与墙钟超时只限制单次调用，不是任务累计工具预算；到限不自动扩容、续接会话或豁免预算，也没有到限前的交接协议。
- 供应商用量来自 CLI 输出，缺失不当作零；Claude 的美元数是 CLI 估算，Codex 不报告美元。
