# 设计：架构、不变量与状态转换

模型是可替换的决策与执行部件；任务状态、权限边界、证据和接受条件留在确定性的运行时。本文描述台账版本 18 的系统。用法见 [README](../README.md)，停止后的出口见[恢复与操作者出口](recovery.md)，工具接口与观察见[操作与观察](operations.md)，历史改动见 git log 与[验证记录](validation.md)。

- [架构](#架构)
- [不变量](#不变量)
- [状态转换](#状态转换)
- [参考项目](#参考项目)
- [厂商接口依据](#厂商接口依据)
- [仍需明确的限制](#仍需明确的限制)

## 架构

一个 **run** 对应一个目标、一条分支 `harness/<run_id>`、一个隔离工作树 `.harness/worktrees/<run_id>` 和一份台账 `.harness/runs/<run_id>/state.json`。run 内的 **task** 构成串行执行的 DAG，各带验收条件和文件范围 `allowed_paths`。一次 **dispatch** 是一次模型调用（新进程、新 UUID）。三种角色：**指挥**（commander，只有一种调用 `decide`，结果是 implement、revise 或 ask）规划、选择、修订与向操作者提问，连续性只来自台账（是否需要决策由台账推导；唯一就绪、没有反馈且未尝试过的任务直接派发，不调用指挥）；**执行者**（worker，`implement`）完成一个任务的一次尝试，是唯一有写工具和 `harness_commands` 的角色；**审阅者**（reviewer，`review`、`final_review`）只读地审阅任务候选或整条分支。

| 模块 | 职责 |
| --- | --- |
| `__init__.py`、`__main__.py`、`cli.py` | 命令行入口；`status` 摘要、`doctor`、`demo`／`smoke`、`sync`、`permission-diagnostics` |
| `runtime.py`、`services.py`、`run_context.py` | 生命周期 API `Runtime`；组装命名服务；当前 run 的台账、事件、快照 |
| `run_store.py` | 台账唯一提交入口（不变量 27） |
| `run_lifecycle.py` | 新建 run、`load_run` 校验、恢复预检与选项互斥、`abandon` |
| `stops.py` | 停止值、登记表与投影 |
| `scheduler.py`、`workflow.py` | 唯一调度循环与下一步的推导（`next_step`）；停止记账、交接目标、决定请求同步、指挥提问门禁、worker 失败来源 |
| `planning.py`、`requirements.py` | 计划登记、`decide` 的组包与三种落地、修订的复核、归档与发布、待派发指令的复核、判定的分类、提案的持有与拒绝；任务命令前置条件 |
| `scope.py` | 范围边界：操作者条目、包含判断、建议条目、范围摘要与持有提案的即时视图 |
| `dispatch.py`、`packets.py`、`context.py`、`input_projection.py`、`path_evidence.py` | 派发、预算、`invoked`、无效输出的重做；角色输入；上下文与容量报告；输入和路径索引 |
| `receipts.py`、`worker_receipts.py`、`worker_returns.py`、`recovery.py` | 指挥与审阅回执与判定；worker 接收意图、封存、判定、交接；无结果判定与供应商分类；恢复忙碌键 |
| `provider_failures.py` | 失败调用的进程事实与 CLI 终止信封分类（纯函数）；四类豁免 |
| `task_lifecycle.py`、`finalization.py`、`verification.py`、`verification_plan.py`、`review_progress.py` | 任务验证、审阅、接受；整体验收；验证命令；候选预检；必需问题与审阅来源 |
| `budgets.py`、`diagnostics.py`、`convergence.py` | 工作尝试与豁免；预算解释与剩余调用下界；返工提示与审阅轮次 |
| `continuation.py`、`environment.py`、`decisions.py`、`progress.py` | 续作、续作拒绝、执行指导；环境更新与前置检查；操作者决定；检查点 |
| `adapters.py`、`process_runtime.py`、`util.py` | CLI 参数与模拟后端；进程组、截止时间与退出事实 |
| `command_server.py`、`command_entry.py` | worker 专属 `harness_commands` MCP：`list_commands`、`run_command`、`delete_path` |
| `mcp_server.py`、`mcp_entry.py`、`file_tools.py`、`git_query.py` | 三种角色共用的只读 `harness` MCP：记忆、技能、运行状态、文件工具、`git_query` |
| `git.py`、`memory.py` | Git 绑定、快照、范围、接受提交与归档；SQLite 项目记忆 |
| `shared.py`、`contracts.py`、`config.py`、`versions.py` | 共享资产摘要、MCP 校验；输出契约；配置；台账版本 |
| `private_state.py`、`workspace_inputs.py` | `.harness` 私有根保护；依赖副本清单 |
| `cleanup.py` | `cleanup-run`（唯一的显式清理入口）：共同门禁之后分别处理全新验证检出与分支工作树；检出删除步骤（含归属核验）也供整体验收在结论保存后尝试回收验证通过的检出 |
| `quota.py` | 额度探测、归一化、样本与观测变化（只是观察） |
| `quota_wait.py` | Claude 角色额度失败后的有界等待：判断、选窗口、睡眠、醒来后的前提核对与观察记录 `quota_wait` |
| `observe.py`、`dashboard.py`、`dashboard_page.py`、`watch.py`、`watch_cli.py` | 只读观察：列表与详情、本地网页、原生文件通知 |
| `operations.py`、`supervisor.py` | 操作记录；`supervise` |
| `permission_diagnostics.py`、`permission_smoke.py`、`tool_smoke.py` | 权限诊断；真实账号联调入口 |

每次调用的输入由三部分组成：`shared/RULES.md` 与该角色的 skill（协议说明，包括各类摘录与索引的读法），以及包（`packet`）。包放数据、索引和现场事实：环境更新、操作者决定和检查点在包里是有界摘录加 SHA256 绑定的索引，与本次任务无关时只带索引。`decide` 的相关范围按触发：task 触发取 `trigger.ready` 中的任务；plan 与 final 触发时环境更新与操作者决定带全部摘录，检查点只带索引——规划阶段还没有检查点，整体阶段全部任务已接受、检查点不再指导执行，与整体审阅取同一个只含索引的视图，也不占整体阶段的上下文容量。环境、决定、检查点、执行指导、权限、审阅、路径索引、命令目录、问题进度和计划增量的说明不在包里。包里没有剩下的静态协议说明：指挥包不带 `instruction`；不经指挥直接派发时 worker 包的 `instruction` 为 null，续作拒绝写入的指令只含操作者的理由，默认做法与修正被拒续作的做法写在 worker 技能里。必需问题各行的读法（键、来源审阅、原件中的序号、可重复的标签）在 RULES 里，包里只有 `required_findings` 与可选的 `required_finding_sources` 数据。另有几个分类标签（`instruction_file_changes.trust`、`ignored_paths.coverage`、环境检查结果的 `coverage`）、`verification_plan.policy` 的布尔标记、操作者决定里替代非法问题的有界说明，以及提示词中可选记忆前的分隔标题，它们描述具体数据，不是协议说明。worker 包的 `instruction` 是指挥写的指令，任务审阅包的 `commit_message` 是从 worker 原件读出的消息，都属于数据；指挥包另带 `workspace: {head, clean}`，在构造包时同时读取 `expected_head` 和工作树 `git status` 是否干净。

`dispatches/<id>/` 保存一次调用的输入（worker 的 `packet.json`、`schema.json`；`packet.json` 的 `required_findings` 就是该 worker 的键到问题的对照）、输出、`returned-result.json`、`worker-seal.<SHA256>.json`、命令清单 `commands.json` 与 `commands/<call>/` 下的记录。

## 不变量

**派发与进程**

1. 每次 worker、reviewer 和指挥调用都是新进程、新 `dispatch_id`，无效输出的重做也是新调用；Claude 用 `--no-session-persistence`，Codex 用 `--ephemeral`，原生子 agent 关闭，不恢复供应商会话。模型 ID、供应商和 effort 按配置原样使用，同一 run 内不变，出错不换模型。
2. 调用身份与预算先于进程启动保存：本地准备（上下文、输入与路径索引、MCP、快照、待派发指令与上一份无效原件的核对、额度检查）成功后，`attempts`、`agent_dispatches`、接收意图和 `invoked=true` 同一次保存，然后才调用后端。本地准备失败不扣额度。后端失败照常计入 `attempts` 与 `agent_dispatches`；其中由 CLI 终止信封判为额度、限速、过载或认证的 worker 失败不计工作尝试（不变量 8、20、36），其余失败（超时、轮数用尽、启动失败、其他明确错误、读不出）照常计数。
3. 退出未确认就不继续：恢复、放弃和清理要求父 dispatch 及其每个命令子进程都有退出证据；`starting` 或缺 pgid 且未确认的记录阻塞。`--confirm-exited` 是审计过的操作者断言，不补造退出码、输出或成功。额度探测进程不登记进 `dispatches`，不受这道门禁约束：它在空临时目录里运行、不执行工具，结果只在本进程内读一次；每次探测有 5 秒查询时限与至多 3 秒计时的收尾；整个探测没有硬上限，启动、文件系统操作与关闭描述符不在计时内，任何退出路径都在回收领导进程之前对整个进程组 SIGTERM、SIGKILL，回收之后只观察；收尾后进程组仍未确认退出时，本进程不再启动该供应商的探测。从创建临时目录之前到收尾完成，探测处在同一个信号边界里：目录与进程在取得的同一步记下归属，收尾从第一行起不会被信号打断；其间收到的第一次用户中断只被记下：读写时由检查点转为中断（读取等待每轮至多 0.1 秒，所以检查点约 0.1 秒内响应），其余时刻在收尾完成后照常传播；之后的收尾仍按上面的计时，传播不是立即的。Harness 被强杀时可能留下一个不被追踪的只读探测进程。
4. 领导进程正常退出后，同组后代最多观察 2 秒，仍未退出就停止消费结果，不向身份不明的进程组发信号。只有进程组查询报告“不存在”才算后代已退出；组内只剩等待回收的僵尸进程时（macOS 对此返回 EPERM）算作仍存在、继续观察，窗口结束时的查询仍被拒绝才记为清理错误。
5. 台账的 `invoked` 是调用事实的权威。事件 `dispatch_prepared`（预算检查前）、`dispatch_started`（`invoked=true` 保存后）和带 `invoked` 的 `dispatch_finished` 只是尽力而为的观察：追加失败不阻止调用，崩溃可能缺事件且不补发；按 `dispatch_id` 去重；用量（token 与 CLI 报告的名义费用）只按台账逐条显示，不汇总。验证检出的回收事件（`verification_checkout_reclaimed`、`verification_checkout_reclaim_failed`）同样经 `RunContext.observe()` 追加，追加失败不影响执行。

**结论与证据**

6. 已返回的结果不重抽：调用前的接收意图绑定原请求、候选快照和返回位置；恢复时先处理已返回的原件，缺失或被改即停止。有效的指挥与审阅结论，以及从原件可靠读出的否决事实，不因重做或 resume 消失。否决事实包括审阅的 `request_changes`、blocking finding、批准中未 `resolved` 的必需项，指挥的 `ask`，以及 worker 的 `blocked`；可靠读出的公共前提是退出已确认、原件字节已绑定摘要、严格解码成对象（拒绝重复键与非有限数，包括溢出成无穷大的数值字面量）、envelope 身份相符。运行时只写严格解码能读回的 JSON：要写出非有限数时停止，而不是写出自己读不回的原件。审阅的否决另要求核心结构合法（拒绝要带着可编号的 findings 返工），不满足时按读不出否决处理；指挥的 `ask` 只要求公共前提（envelope 相符且 `action` 为 ask），其余字段不合格时仍登记提问，派发条目记 `invalid_asked`；调用期间候选或 HEAD 改变也不妨碍登记（条目记 `candidate_changed`）：已返回而未登记的提问（登记前中断）在 `resume` 的 HEAD 门禁之前完成登记；登记不认可这些改动，其他指挥决定在这种情况下不判定，执行、回答登记之后的每一步仍受 HEAD 与快照门禁约束。没有通过契约、也读不出否决的输出不是结果：原件保留，故障记在它的派发条目上，由同一角色的新进程重做（不变量 11）。
7. worker 结果先封存再交接：确认 worker 与命令子进程退出后，把结果、来源文件摘要、返回后候选、HEAD、Git 绑定和进程事实写成内容寻址的 `worker-seal.<SHA256>.json`，再做契约、范围检查与交接。交接只认封存记录里的候选摘要：封存之后候选（含暂存区与 HEAD）的任何变化都在交接前被拒绝，不重新绑定。未封存的原始输出不补绑恢复时的工作树。
8. 无结果判定绑定原始证据：确认 worker 与命令子进程退出后，按派发目录里的结果文件、Claude 的 JSON 输出（`process.stdout`）与进程记录（`process.outcome.json`）把来源判为 retryable、invalid、returned 或 unknown。worker 调用失败时当场判定（`Dispatcher.judge_worker_failure()`），主进程在判定保存之前死掉或被中断时由下一次 `resume` 判定，两者调用同一个 `WorkerReceipts.pending()`、读同样的字节，所以结局相同。retryable（没有输出，或完整的显式错误信封）另记供应商分类（判定记录版本 2 的 `provider: {code, http_status}`）：先看进程记录——超时、启动失败按进程事实，进程没有正常结束或记录不完整时不读信封——再按信封的 `subtype`、`api_error`、`api_error_status` 分为额度、限速、过载、认证、轮数用尽或其他错误；信封里的文字与 stderr 不参与。retryable 与 invalid（输出完整，但严格解码不出对象结果）都允许新的计费尝试：可能中断的步骤（分类、为恢复记录读取 Git）先完成，然后判定、判定证据、额度／限速／过载／认证的工作尝试豁免、恢复记录和回执消费同次保存，结局分别是 `provider_failure` 与 `result_rejected`；在此之前中断，条目与回执都保持判定前的样子。当场判定为 retryable 时按分类停下，为 invalid 时调度继续（与退出码无关）。returned（已有能解码成对象的结果但没有封存，包括封存之前的本地检查失败）或 unknown（与原始输出冲突、超过读取上限、解码失败）时，判定与停止原因保存，回执保留，`--confirm-exited` 不能解开。读文件时的系统错误不是字节的性质，不形成判定：原样上抛，回执保留，修好后重判。已封存的结果从不进入这个判定。判定记录所依据五个文件的 SHA256 与分类一起保存，加载时按字节重新推导核对，证据删改后不能改判。
9. 不可变证据先发布，再登记引用：packet、契约、封存、续作类记录、环境更新、决定、检查点和计划先写成内容寻址或带 SHA256 的文件，再在台账存路径与摘要。计划写成 `plans/revision-N.json`（同字节复用，不同即停止），`plan_history` 与 `pending_revision` 只存修订号、路径与 SHA256。已发布的计划文件是执行字段的权威，在三处核对：`resume` 的任何写入之前（含显式退出确认，因此也先于批准计划与重放 worker 回执）、每次推导下一步之前、标记完成之前；全部版本都按摘要核对，最新版本的任务表必须等于 `tasks`，`revision-0` 的等于 `original_tasks`，缺失或不符即停止。只此三处：worker 运行期间被删改的计划要到下一步才发现，那时该任务的验证、审阅与接受可能已经完成；`prepare-run` 与只读入口不核对（之后的 `resume` 会拒绝）。每次核对的耗时随计划历史的总大小增长（实测每版约 40 KB 时，200 版约 19 ms、1,000 版约 102 ms）。文件写入失败时不登记引用；worker 输入写入失败时也不扣调用预算、不保存接收意图或 `invoked=true`，但此前保存的准备条目与随后的失败状态、诊断仍会落盘。未引用的遗留文件被忽略，同内容重试时复用。
10. 台账对 worker 输入和续作类记录只存摘要与引用：接收意图存 `packet.json`、`schema.json` 的路径与 SHA256；续作、执行指导、续作拒绝行只存 `snapshot_digest`，整棵快照只在不可变文件里。引用文件缺失、被改或换成符号链接时，恢复停止并保留现场。
11. 无效输出的重做有界。指挥或审阅的一次请求返回读不出否决的无效输出时，在同一次保存里记录错误与原件摘要（`status=invalid`、`invalid_output`）并消费回执，然后自动重做一次；是否重做由整个 run 最后一次模型调用推导（它是同一调用的无效输出且本身不是重做），重做条目记 `retry_of`，输入附上一份无效输出的引用与前 2,000 字符的错误（带上它超出上下文预算时去掉）；这份原件在本地准备之后、调用计费之前按摘要核对一次，缺失或被改即按证据损坏停止，留下一个未调用（`invoked=false`）的失败准备条目。重做仍无效则停止；此后的普通 resume 发起新请求，规则相同。worker 的无效输出记为一次工作尝试，回执消费，调度继续，受 `max_attempts` 约束；blocked 的输出保持约束力：核心结构与语义都合法、只有界限不合格的 environment／operator／task 阻断按原路由，原结果进入 feedback；结构或语义不合法的 blocked 输出按 task 阻断交给指挥决定，feedback 不带原结果；都不给尝试豁免。所有重做都计入 `max_agent_dispatches`；证据损坏或退出未确认时不重做。blocked 结果不产生提交，它的 `commit_message` 在每个校验入口（判定与历史复核）都被去掉，不影响判定、路由与豁免，也不进入后续上下文，原件保留原值。worker 的 `checkpoints`、`proposed_memories` 与审阅的 `approved_memory_keys` 不参与判定，不合格只丢弃自身并记事件；后续上下文只用它们经过校验、有界的投影（检查点只以登记后的 `task_checkpoints` 出现，记忆提案只投影整批合法时的 key，记忆批准只在接受时起作用、不进入后续上下文），被丢弃的部分不投影，原件保留原值；`decision_requests` 不合格即为无效输出。例外：一份已返回的指挥原件是否无效，只由原件字节与它的修订基线决定：只缺范围的合格提案被持有（不变量 38），已判定合格的原件在重放时不会因为运行条件（容量）的变化被判为无效；证据读取的失败从来不是无效输出。

**接受**

12. 任务接受同时要求：合法结果声明完成且无 blocker；任务的证据要求在候选中都是普通文件，由运行时检查并记录摘要，不取 worker 的声明；Git 绑定与 HEAD 未变，改动（含暂存区）在 `allowed_paths` 内；全部验证命令成功；新审阅实例批准、无 blocking finding，并且按固定键给出的必需问题处置全部为 resolved；接受前快照等于审阅时快照。必需问题由审阅历史在当前候选上推导：处置只在它的审阅快照等于该候选、且该审阅可用时关闭问题。
13. 提交前重新哈希一次完整快照（含暂存区与 HEAD），不用 mtime、size 或 Git 的未变判断；与审阅时的快照不同时旧批准持久标为失效，任务重新执行、验证和审阅。这次核对在任务记忆记录预检与 worker 原件读取（都不读工作树）之后；它之后到 commit 之前，运行时只做索引、范围与审阅来源的只读核验。
14. 提交与接受记录共用一道屏障：commit 后，`accepted`、`expected_head`、`pending_post_accept` 同次保存，同时清除 `pending_acceptance`、`current_task` 和反馈。提交消息取自 worker 原件里的 `commit_message`，台账不另存副本：原件由 `pending_acceptance.worker_source` 指定，这个来源必须与批准该候选的审阅记录中的来源、工作尝试和快照一致；提交前按摘要读一次，来源不完整、缺失、被改或不一致即停止、不提交。运行时只追加 `Harness-Run`、`Harness-Task` 两行 trailer，以固定作者与提交者、UTF-8 编码、不签名、不运行仓库 hooks、`--cleanup=verbatim` 原样提交。没有改动时不提交。commit 已落盘而台账未保存时，恢复因 HEAD 不符停止，不自动修复。
15. 全部任务接受后重新验证，再由新审阅者审阅 base 到 HEAD 的完整 diff；最终快照不变才 `completed`。同一快照的整体拒绝不能靠 resume 重抽，只能由通过正常门禁的修正任务改变候选；同一快照的拒绝或非超时验证失败在新的修订落地之前持续触发 `decide`，空修正被接受后不重抽整体审阅。例外只有一个：同一快照的整体**验证**以普通非零退出失败（不是超时）时，本次恢复显式 `--reverify` 在首次进入整体验收时开始新的整体尝试，而不派生决定；它可以与回答指挥提问的 `--operator-decision` 同用，答复先整批登记，再进入整体验收。`--reverify` 不绕过整体**审阅**的拒绝：同一快照被拒绝后，无论是否带 `--reverify`、是否同时答复，下一步都是派生的指挥决定，不重抽整体审阅。
16. Git 绑定在创建工作树后按实际解析结果保存（`.git`、gitdir 反向链接、公共目录、预期分支）；每个工作树 Git 命令显式指定 git-dir 与 work-tree 并剔除 `GIT_*` 环境变量，操作前后核验绑定。接受、归档、恢复、移除和记忆来源核验都使用它。
17. `allowed_paths` 从仓库根匹配，`*`、`?`、字符集只作用于单段，独立 `**` 段跨层级；`.git`、`.harness`、`.codex`、`.claude` 在规划和接受时都被拒绝，工作树内的 `.harness` 一律拒绝；索引含 gitlink 的仓库在创建工作树前拒绝。run 的范围边界（不变量 37）只约束计划准入；接受时的范围检查只用任务自己的 `allowed_paths`，边界不解除任何受保护根检查。
18. 接受记录和任务记忆用审阅者的摘要；提交消息由 worker 撰写、审阅者审查，不进接受记录和任务记忆。任务审阅包里的消息在每次构造包时从 `pending_task` 绑定的 worker 原件按摘要读出，台账结果与原件不同即停止、不派发审阅。记忆提案须审阅者逐项批准才写入 `knowledge:`；记忆都是参考（`reference_only`），不授予权限或替代决定。

**预算**

19. 预算只增不减：`attempts`、`agent_dispatches` 累计保存，resume 不清零。修订号与整体尝试序号由 `plan_history`、`final_history` 的长度推导。恢复只允许提高超时、上下文、轮数、`max_attempts`、`max_agent_dispatches`，以及追加精确 `worker_commands`；额度等待上限 `quota_wait_max_seconds` 是等待策略而不是预算，恢复时可以调高、调低或设为 0。
20. `max_attempts`（默认 5）按每个任务的工作尝试计数：worker 调用数减去两种已完整校验的豁免（`attempt_exemption`）——与反馈同次保存的 environment／operator 阻断，以及与判定同次保存、绑定 `process.stdout` 摘要的额度、限速、过载、认证失败（`basis: provider_error`）。豁免在加载与每次派发前复核原件，不依赖额度快照或账号的当前状态。计划任务数、修订次数和整体尝试次数都没有单独的上限：每次模型调用都计入 `max_agent_dispatches`，它是总调用数唯一可靠的上界；计划大小由整体审阅的必要上下文约束，模型给出的计划在首次判定时超出即为无效输出；已判定合格的原件在重放时超出（配置放宽或新的记录可能加长必要输入）则停止并保留原件（`budget/proposal_capacity`）；供给计划超出则停止。
21. 观察投影不改变预算：`status` 的 `budget_explanation`、`remaining_model_calls`（下界）、`convergence`、`review_rounds` 只读已保存事实。`convergence`、`review_rounds` 跨轮比较的是 finding 标签（审阅者写的 id），只作观察。
40. 额度等待是同一进程里的有界等待，不是恢复。只有 Claude 担任的角色、只在执行循环里（`running`）、只对已经分类、且失败调用的事实已经保存的 `provider_quota/usage_limit` 失败才考虑（worker 的判定、豁免与回执消费在抛出之前已保存；指挥与审阅的调用事实与错误已保存，额度分类只由异常携带）：内存里没有未提交的改动（`RunStore.matches_commit()`）、配置 `runtime.quota_wait_max_seconds` 不为 0、`agent_dispatches` 仍小于 `max_agent_dispatches`、失败供应商的额度快照可用；只看对失败模型适用的窗口（`session`、`weekly_all`、显示名能唯一对应失败模型 ID 的 `weekly_scoped`：同一家族，且显示名不带版本或版本完整相等），到 100% 的取最晚的重置时刻，`0 < 重置时刻 + 60 秒 − 当前时间 ≤ 上限` 才等；任何到 100% 的窗口对应不明（不能证明是另一个家族或另一个完整版本）、阻塞窗口缺重置时刻、将要写入的等待记录不满足它的加载契约、加载校验此刻不通过，都不等。失败原因只来自失败原件，快照只提供时刻，从不读失败文字。等待期间持有 `run.lock`、不持有 `store.lock`，状态保持 `running`，台账的 `quota_wait` 只是观察：与其他改动分开、单独一次保存写入，醒来时单独一次保存删除，中断时随中断的停止一并删除，进入执行、放弃或任何停止时删除，恢复从不读它；进程死亡就不会继续等。醒来后先重跑 `resume` 预检里的只读前提核对（盘上台账仍是本进程刚提交的那份、加载校验、共享资产摘要、环境产物、计划权威、Git 绑定与派发索引、HEAD、输入与回执链接；与 `resume()` 调用同一组函数），不符就按 `resume` 遇到同样情况时的类型停止，不重跑带写入的预检步骤（退出确认、登记已返回的提问、补摘要、恢复输入），也不重读配置文件；再回到调度循环开头，重跑全部调度门禁，下一步照常推导（worker 失败之后是指挥决定），每次模型调用仍是新进程、新 dispatch。每次等待之前都有一次计入派发预算的失败调用，所以等待次数受 `max_agent_dispatches` 约束，累计时长不受单次上限约束。不等时，停止值的 `data.wait_reason` 写明原因；进入执行循环之前（首版规划）的额度失败不等（`not_running`）。

**权限与工具**

22. worker 应优先用 broker 的 `run_command` 执行命令；broker 只接受 `verification.commands` 与 `permissions.worker_commands` 中的完整 argv，不能附加参数、改 cwd 或 env，不经 shell。Claude worker 另有 CLI 层的 Bash 许可：`config.py` 的只读前缀（参数只能接在 `--` 之后），以及已配置命令中不含 `*`、换行的精确 argv；这些调用不经 broker、不留命令记录。任务的 `required_commands`、`self_checks` 是前置条件不是授权；缺授权时不启动 worker、不扣尝试。
23. `git_query` 只执行形式表内的 argv，表外 argv 不执行即返回工具错误。同一解析函数决定执行与“已覆盖”：计划前置命令、续作命令请求、环境检查和权限诊断把表内形式算作已满足——只表示能力存在，不表示已执行或成功，也不是 Bash 或 broker 授权。
24. 命令与删除先发布 journal：每次调用先在 `command-staging/` 写好完整 `process.json`，再一次原子改名进 `commands/`；发布前不启动子进程、不删除文件。保护：退出门禁不会遇到缺 journal 的目录。
25. `delete_path` 保证：路径从工作树根目录描述符逐级以 no-follow 打开；不跟随符号链接；最后一次核对时，最终项是 HEAD 中已跟踪、在任务范围内的普通文件；每次被准入的调用都留下记录。不保证：在最后一次核对与 `unlinkat` 之间，能在工作树内改名的并发进程可以让被删除的文件位于别处，包括工作树外；事后的并发变化检测是尽力而为，可能漏报（例如目录被移回）。删除立即生效，接受门禁只决定候选能否被接受，不能撤销删除。
26. 角色能力不是 OS 隔离：Codex 只读角色用 `--sandbox read-only`，Claude 只读角色只有读工具与只读 Git 命令，Claude worker 的 Edit／Write 限于工作树并拒绝 `.git`，Codex 不能作为 worker。候选中的 `AGENTS.md`、`CLAUDE.md` 等指令文件是待审阅数据，不授予权力；两种 CLI 都关闭项目指令自动注入。

**台账、版本与观察**

27. 台账只经 `RunStore` 提交：在 `store.lock` 下比较已加载字节的摘要，拒绝旧写入，原子写入并递增 `store_revision`；外部动作前也核对当前权威。`run.lock` 保护生命周期。
28. 只支持当前台账版本 18，只解释一种台账形状和一种配置形状：其他版本（含缺失或非整数的版本）不迁移、不补写默认值、不解释字段；`resume`、`abandon`、`prepare-run`、`cleanup-run`、`status RUN`（含 `--full`）、`watch`、`permission-diagnostics`、观察网页的 run 详情、命令 broker（建 manifest、执行与删除）和只读 MCP 都在读完版本、读任何其他字段之前拒绝，错误写明该 run 的台账版本与当前版本；非对象的台账同样按“不支持”处理；任务记忆的来源台账不是当前版本时来源状态为 `unsupported`，其任务历史不再召回；运行列表（不带 run 的 `status`、观察网页的列表）对其他版本只给出路径、版本和“不支持”，一个旧版本或损坏的目录不影响其他 run 的列出。配置的未知键（含已删除的键）一律报“未知配置键”，不给迁移提示。resume 另核对共享资产摘要和完整配置快照。
29. 必要输入超过 `context_chars` 时不调用模型、不扣额度，报告仍写入 `context-report.json`；可选记忆按整条加入剩余空间。成功派发的报告保留根与顶层字段尺寸及最大的 30 个嵌套字段并给出省略数，超限报告保留全部明细。
30. 观察层（`status`、`watch`、`dashboard`、操作记录与 `supervise` 回执的读取）不写台账、不取 `run.lock`、不参与恢复；额度只读 run 目录里已保存的样本，观察入口从不启动额度探测或任何供应商 CLI。（`run`／`resume` 创建操作记录时读取 CLI 的 `--version`，见 [操作与观察](operations.md)，那是执行入口的行为，不属于观察入口。）它们显示的停止信息都是 `stop` 的投影，不解析文字。额度等待记录 `quota_wait` 也只是投影：台账不证明进程仍在等待。
39. 额度快照只是观察。只有真实的 `CLIBackend` 采样：每次 `run`／`resume` 里首次用到某供应商之前，额度失败停止之前，以及停止（中断除外）、暂停、完成、等待批准**确已保存**之后对本进程用过的供应商各一次，至多 5 次；额度等待（不变量 40）另取每次失败的失败样本与每次醒来的醒来样本，次数受派发预算约束；每个采样点启动探测之前都要求退出门禁通过（没有未确认退出的模型进程或命令子进程）。每次写一个排他、原子发布的样本文件 `observations/quota/…`，文件名按（台账修订、相位、进程启动时刻、序号）排序；不进 `state.json`，不参与预算、回执、接受、恢复与 `load_run()`，缺失或损坏只是观察缺口。探测失败、超时、输出超限、形状不符只记“不可用＋原因”，从不改变调度、预算、回执与接受；用户中断照常传播。Claude 探测必须读到零调用的结果行（`num_turns`、`total_cost_usd`、`duration_api_ms` 为 0，`local_command` 为 `usage`），否则本进程内停用。统计口径是“运行期间账号额度的观测变化”：同一额度桶、同一重置周期内的首尾差（百分点）；周期以锚点重置时刻划分，观测时刻越过重置时刻即为新周期，报告的重置时刻已经过期的观测不参与差值；未知不当零；账号级，包含本 run 以外的使用，不相加、不合成总数；credits 用独立的 64 位十进制上下文精确计算余额净变化。

**身份**

31. 模型不回填运行时拥有的身份。必需问题的处置和 worker 的修复声明使用运行时按当次必需项生成的固定键 `f1…fN`：键全部必填、不许多余键；没有必需项时不出现该字段；审阅有必需项时必须给出 `finding_dispositions`。审阅的键到来源的对照在派发前保存，审阅的重放与重做都用同一份：任务审阅在 `pending_task`，整体审阅在该次整体记录；worker 的结果只按它自己那次派发已发布的 `packet.json` 映射。审阅记录带一份对照副本；处置只经副本映射，副本必须逐行等于在同一历史前缀和同一候选上重算的清单。任务证据由运行时按计划检查。finding 的内部身份是（来源审阅 dispatch，原件中的序号），模型写的 id 只是标签，可以重复。envelope 的 `task_id`、`dispatch_id` 是单值枚举，照旧回填。

**指挥**

32. 指挥只有一种调用 `decide`，结果是 implement、revise 或 ask。是否需要决策由台账与工作树事实推导（`Scheduler.next_step`），不设持久标记；需要决策的条件在决策落地之前持续成立，已返回的回执受保护，所以中断、提高预算或确认退出之后，不会重跑已报告阻断的 worker，也不会漏掉一次决策（允许的外部变化可能让调度停下，或按明确规则改变下一步）。每次落地是一次保存，并在其中消费回执：implement 写待派发指令；revise 只在全部校验（含整体审阅必要容量与范围边界）通过后，先写不可变的计划文件（执行字段的唯一权威），再保存只含引用与摘要的 `pending_revision`；发布前按摘要复核文件、读回原件重放修订；需要时归档、重置之后，计划版本、`tasks`、`current_task` 与下一条指令同次发布，指令的快照取保留的候选或重置后的干净快照。非初始规划的 revise 必须带指令；初始规划的 revise 不带指令。修订可以给未接受的既有任务追加 `allowed_paths` 与 `required_commands`（只增、去重、保持顺序）；已接受任务不变。只改当前任务的说明、范围或命令且仍选它时保留候选。修订发布的指令在 worker 调用前、本地准备之后与计划文件、任务表和快照核对一次；所有待派发指令都要求快照等于保存时的快照。ask 登记操作者请求（种类 `commander`）并停止；未答或答 stop 时不调度任何模型，回答在一次指挥决定真正落地之前一直作为 `decide` 的输入（持有、容量保留与被拒都不算落地），不授予范围，也不降低验收。

37. run 的范围边界 `scope` 是操作者用 `run --scope`、`resume --scope` 声明的条目列表：具体路径、字面子树 `p1/…/pk/**` 或独立的 `**`；只增不减。空列表表示没有声明：首版计划不受约束，修订只能复用已发布计划的模式；运行时不把首版的模式写回 `scope`。模型提出的每个新模式——首版落地那一刻 `scope` 非空时首版的全部模式，修订新增任务的模式与 `add_allowed_paths`——必须由三条规则证明在“修订前已发布计划的全部模式 ∪ `scope`”之内：字符串相等；具体路径被其中某条匹配；按路径段位于某条字面子树的前缀之下。待判定的提案自身不进入这个集合。判断可靠、不完备：证明不了的模式按“无法证明包含”处理。供给计划在启动时与在恢复时发布前都按当时的 `scope` 检查；启动时越界不创建 run，恢复时越界停止、不发布。边界不授予命令，不改变验收与审阅，不追认已判定的结果或已有候选。
38. 一份已返回的指挥原件按四类检查判定：证据（失败即按证据或现场类型停止，回执保留，不重做）、原件契约（只依赖原件与修订基线；首次判定失败是无效输出）、范围包含（只缺范围即持有）、运行条件（整体审阅容量；首次判定失败是无效输出，已判定原件的重放失败则停止并保留）。条目 `completed` 且回执仍在，表示原件已判定合格而未落地；重放的失败不降级这个状态。持有与容量保留都不消费回执、不记无效、不写计划文件、不进 `pending_revision`，台账没有对应字段，普通 resume 重放同一原件得到同一结论，不调用模型。`resume --scope` 与答复、配置放宽在一次保存里生效后重放同一原件；全部在边界内且容量通过时照常落地（首版：先复核边界，计划文件先写，`original_tasks`、`tasks`、`plan_history` 与回执消费同次；修订：计划文件先写，`pending_revision` 与回执消费同次，发布前按当前 `scope` 复核）。`resume --reject-request` 拒绝已判定合格而未落地的提案：先写内容寻址的拒绝文件，归档这份提案未登记的预写计划文件（由原件与当前基线重算出的字节相同才算），再一次保存回执消费与来源条目的 `proposal_rejection`；被拒原件不复活，之后的指挥决定带 `rejected_revision`，直到有指挥决定落地。

**资源**

33. 运行时拥有的全新验证检出随用途结束：检出记录 `checkout.json` 创建时写明所属的 run、整体尝试与验证轮次（`owner`），整体验证结论（含 `phase=reviewing`）与该记录的摘要（轮次的 `checkout_digest`）在同一次保存里登记；结论保存之后、整体审阅派发之前，运行时在这一个位置尝试回收该尝试最近一轮、结论已绑定的检出，首次进入与恢复走同一个调用点。回收与 `cleanup-run` 共用同一段删除代码与门禁（证据位置、归属、摘要、路径形状、Git 绑定、HEAD、可删除性），先写 `cleanup.json` 的 `pending` 再 `git worktree remove`、确认目录与注册都消失后写 `removed`；不写台账；删除失败只记尽力而为的事件，不改变结论、不重跑验证或审阅；权威核对或退出证据失败时 run 照常停止。失败或中断轮次的检出保留到 `cleanup-run --remove`，连同其中的忽略文件删除。任务工作树的忽略文件只在本次命令带 `--remove-ignored` 时随工作树删除。两种资源上，未忽略的未跟踪文件、已跟踪改动、暂存区与 HEAD 不符、隐藏索引标志都阻止删除。

**停止**

34. 停止是一个有限的值 `stop = {kind, reason, data, source}`，只在 `status` 为 `awaiting_approval`、`blocked`、`interrupted` 时存在，与 `status` 同一次保存，进入 `running` 或 `abandoned` 时同一次删除。`(kind, reason)` 必须是登记表（`stops.REGISTRY`）的一行；`data` 只含该行登记的有界字段与诊断摘录 `detail`（最多 2,000 字符），`source` 只含身份（任务、派发、请求、续作、产物路径与摘要）。文案、建议出口和退出码由登记表从这个值投影；`status`、`run_stopped`、操作记录、stderr 用同一个投影。运行时不读 `stop` 做调度、权限或恢复判断，恢复仍重跑全部门禁。未标注类型的异常记为 `unclassified`。加载成功之后发出的每个停止或拒绝（含进入计划待批准与它的重报），在落盘、上抛或重报之前重跑加载校验，中断除外（中断不复核）；校验失败时，最终的停止就是这次加载失败（证据不符是 `evidence/load_integrity`，本地 I/O 是 `runtime_failure/io`，两者都没有命令出口），原停止只记在 `superseded` 里，同一个最终值写回上抛的异常；所以中断以外已落盘、已上抛或已重报的停止列出的 `resume`／`abandon` 在那一刻不被加载核验挡住（之后证据再变化不在保证内），`evidence/runtime_integrity` 表示证据有问题而加载核验通过。读不出但路径仍在（权限、句柄、设备、磁盘满）是本地 I/O 故障，不是证据故障；证据故障从不被当作模型输出无效。一个停止列出的每条出口所需的身份都在值里，缺了就整体记为 `unclassified`。整个值按写入台账的同一种序列化（`ensure_ascii`，补充平面字符各占 12 字节）不超过 32 KiB：加入诊断摘录之后按完整结果收缩——先截短出口不读的列表字段（后登记的先截，保留开头并记 `<字段>_omitted`），再截短诊断摘录与其他出口不读的文字，最后才截短出口读取或作为出口条件的列表（至少留一项，出口仍列出、仍能执行）；所以合法的停止不会因为字段过大而丢失类型或出口。恢复预检、`abandon` 的拒绝只进 stderr 与操作记录，不改 run 的 `status` 与 `stop`；每个输入文件每次恢复只读一次。

35. 退出码由停止值的 `(kind, reason)` 查登记表得出：0 完成或操作者要求的暂停（`completed`、`paused`、计划待批准，含未带 `--approve-plan` 的普通 `resume`）；3 run 自洽、有文档化的操作者动作或外部条件可解开；2 本次调用的输入不被接受；1 证据或现场与台账不符、Harness 自身失败或未分类；130 中断。`cleanup-run` 例外，仍按逐资源结果返回 0／1。

36. 豁免只减工作尝试：被豁免的调用照常计入 `attempts` 与 `max_agent_dispatches`。一个派发条目最多一个豁免；只有四类供应商失败（额度、限速、过载、认证）与 environment／operator 阻断能得到豁免，其他失败与无效输出没有。

## 状态转换

### run 状态

| 状态 | 进入 | 离开 |
| --- | --- | --- |
| `planning` | 首次保存 | 计划登记后进入 `running` 或 `awaiting_approval` |
| `awaiting_approval` | `--pause-after-plan` 且计划未批准；与 `stop`（`plan_approval/initial_plan`）同次保存 | `resume --approve-plan` |
| `running` | 每次进入执行循环，同时删除 `stop` 与遗留的 `quota_wait`；额度等待期间保持 `running` | 停止、暂停或完成 |
| `paused` | `--step` 下本次已接受一个任务 | 普通 `resume` |
| `blocked` | 执行中出错：先在一次保存里写状态与 `stop`，再写 `run_stopped` | 对应恢复出口或 `abandon` |
| `interrupted` | Ctrl-C、SIGTERM、SIGHUP；与 `stop`（`interrupted/signal`）同次保存 | 普通 `resume` 或 `abandon` |
| `completed` | 整体批准后保存 | 终态；普通 `resume` 返回保存结果 |
| `abandoned` | `abandon`，同时删除 `stop` | 终态；不能 resume |

### 新建台账的键

`run_lifecycle.py` 新建台账的全部顶层键（`store_revision` 由首次提交加入）；“恢复”列是中断后 `resume` 的处理。

| 键 | 设置与更新 | 清除 | 持久化顺序 | 恢复 |
| --- | --- | --- | --- | --- |
| `schema_version`、`id`、`repo`、`goal`、`commander`、`branch`、`worktree`、`base_commit`、`config_path`、`asset_digest`、`created_by_operation` | 首次保存 | 不变 | 早于创建工作树 | 要求版本 18、ID 与仓库一致；比较共享资产摘要 |
| `config_snapshot` | 首次保存；resume 接受合法放宽或额度等待上限的调整后替换 | 不清除 | 与答复、`scope` 一次保存，之后写 `resume_config_changed` | 与当前配置比较，只许放宽；`quota_wait_max_seconds` 可任意调整 |
| `status` | 见上表 | — | 先保存再写 `run_stopped` | 终态拒绝修改选项 |
| `worktree_binding` | 首次保存为空；`git worktree add` 后第二次保存填入 | 不清除 | 第一次保存在创建工作树前 | 绑定为空时 resume 拒绝，只能 `abandon` |
| `original_tasks` | 供给计划在首次保存；模型计划与首版计划同次 | 不变 | 供给计划的副本早于 `tasks` 登记；模型写的首版计划与 `tasks`、`plan_history` 同次保存 | `tasks` 为空时：有供给计划则重新校验并登记同一计划，否则推导出规划的 `decide`（回执已返回则重放同一原件） |
| `tasks`、`plan_history` | 首版计划登记；修订发布 | 不清除 | 首版发布前按当时的 `scope` 复核（模型首版越界则持有，供给首版越界则停止）；先写 `plans/revision-N.json`；`plan_history` 每行只存修订号、路径与 SHA256；修订发布与 `current_task`、`pending_directive`、`feedback` 同次 | 按计划版本继续；修订的执行字段只在计划文件里 |
| `plan_approval_required`、`plan_approved` | 创建时；`--approve-plan` | 不变 | 先写 `plan_approved` 再保存 | 普通 resume 不批准；`--approve-plan` 只批准已发布的首版，首版未发布时拒绝该参数 |
| `expected_head`、`accepted` | 任务接受 | 不清除 | 与 `pending_post_accept` 同次 | 工作树 HEAD 必须等于 `expected_head` |
| `attempts`、`agent_dispatches` | 调用后端前 | 只增 | 与接收意图、`invoked` 同次 | 已记账的调用不退回；豁免只作用于工作尝试的投影，计数本身不退回 |
| `final_history` | 新的整体尝试追加 `running` 记录（尝试号是 `len(final_history)+1`），随后更新阶段与结论；进入 reviewing 时与阶段同次写入该次审阅的对照 `required_findings`（在验证开始前算出）；开启 `fresh_checkout` 时，验证结论那次保存同时写入该轮的 `checkout_digest`（`checkout.json` 字节的 SHA256） | 不清除 | 尝试记录先于快照产物；失败或拒绝与 `feedback` 同次；进入 reviewing 之后、整体审阅之前尝试回收最近一轮的检出（不写台账） | 同一快照：未完成则复用该尝试，已批准则完成，已拒绝或非超时验证失败则推导出整体阶段的 `decide` |
| `dispatches` | 准备完成追加（`status=running`、`process_state=prepared`、`invoked=false`）；调用前 `process_state=starting`、`invoked=true`，重做另记 `retry_of`（去掉了提示时记 `previous_invalid_omitted`）；启动记 pgid；结束记进程事实与用量；读不出否决的无效输出记 `status=invalid`、错误与 `invalid_output`，否决类记 `invalid_rejected`、`invalid_asked` 或 `invalid_blocked`；已判定合格的指挥条目记 `completed`，重放失败只写 `error`、不降级；被拒的提案另记 `proposal_rejection`，与回执消费同次；调用期间候选或 HEAD 已变的提问另记 `candidate_changed`；worker 调用失败的判定记录 `worker_return_evidence`（版本 2，带 `provider` 分类）与 `attempt_exemption`（阻断或供应商两种形状） | 不清除 | 准备条目先于本地准备门禁（输入、索引、MCP、快照、待派发指令与上一份无效原件的核对、额度），这些门禁失败时条目以 `invoked=false`、`status=failed` 收尾；`retry_of` 与预算同次；`invalid` 与回执消费同次；`candidate_changed`（与 `invalid_asked`）和提问登记、回执消费同次 | 退出门禁读进程事实；`worker_receipt_consumed` 记 worker 回执结局；`retry_of` 必须指向它之前最后一次模型调用 |
| `feedback` | worker 阻断（含无效阻断，带 `output_error`；结构不可信时没有 `worker`）、审阅拒绝、验证或预检失败、候选变化、整体失败、修订发布 | 接受、整体批准时置 null | 与引起它的转换同次 | 下一次指挥与 worker 的输入 |
| `review_history` | 任务审阅结束；候选变化前把已返回审阅存为 invalidated；每条记录带派发时的对照副本 `required_findings` | 不清除 | 与审阅后的阶段同次 | 返工输入、`review_rounds` 来源；处置只经副本映射，副本与重算清单不符即停止 |
| `worker_receipts` | 调用前存意图（`invoking`、packet／契约引用；worker 的必需问题对照在 `packet.json`）；封存后 `sealed`，交接按封存记录里的候选摘要核对 | 交接时消费 | 封存文件先于 `sealed`；消费与后续阶段同次 | 未封存：调用失败时当场判定（主进程死掉则恢复时由同一代码判定）；retryable 或 invalid（不可解码的输出，结局 `result_rejected`）则消费，retryable 按供应商分类停止并可能豁免工作尝试，invalid 调度继续；否则停止；判定、豁免、恢复记录与消费同次保存；封存文件已写而 `sealed` 未保存：找到并核验该文件后继续；已封存：复核后继续原门禁 |
| `review_receipts` | 调用前存意图；返回后绑定 `artifact_digest` | 审阅结束；候选变化（先存原文）；无返回时重派前；无效输出判定时同次移除 | 结果文件先于摘要 | 有结果则校验同一结果；无结果才新派发并计费 |
| `commander_receipts` | 调用前存意图（含原请求与摘要；键 `commander:decide:run`） | `decide` 落地（待派发指令、`pending_revision`、首版计划或决定请求）时同次移除；首次判定为无效时同次移除；持有与容量保留时保留；被拒时与条目的 `proposal_rejection` 同次移除；无返回的意图在重新派发前或 `--reverify` 时丢弃 | 同上 | 校验同一结果，不重抽 |
| `scope` | 创建 run 的首次保存；`resume --scope` | 不清除、不缩小 | 先写 `scope_extended` 事件，再与答复、配置放宽同一次保存 | 按已保存集合判断首版与修订的准入 |
| `environment_updates` | `--environment-update` | 不清除 | 先写 `environment/<id>.json` 与事件 | 同任务最新一条替代旧检查列表 |
| `environment_checks` | 选中任务每次 worker 尝试前 | 不清除 | 报告文件先于追加 | 检查失败不启动 worker、不扣尝试 |
| `decision_requests` | operator 阻断、指挥提问（种类 `commander`，与回执消费同次）、来源任务接受且 `pending_post_accept` 完成 | 不清除 | 先写 `decisions/<id>.json` 与事件 | worker 与 handoff 请求未答或答 stop 时阻止目标任务派发；指挥提问未答或答 stop 时阻止一切调度 |
| `operator_decisions` | `--operator-decision` 整批 | 不清除 | 全部答复文件先写，再与 `scope`、本次采用的配置放宽一次保存 | 只解除匹配请求的等待 |
| `task_checkpoints`、`checkpoint_batches` | worker 结果交接时逐项核对 | 不清除 | 证据副本、manifest、批次文件先写 | 批次文件已写未登记时登记同一结果 |
| `continuation_history` | worker 返回 `blocker_kind=continuation` | 不清除 | 先写 `continuations/<id>.json`；与 `pending_continuation` 同次 | 条件满足后新派发继续 |
| `execution_guidance` | `--execution-guidance` | 不清除 | 先写 `execution-guidance/<id>.json` | 进入该任务后续输入 |
| `workspace_inputs` | 首次为 null；`prepare-run` 写入清单摘要 | 不清除 | 例外：绑定先保存，再写冻结清单和副本回执；缺失时失败关闭 | resume 与每次派发前核对副本 |
| `store_revision` | 每次提交 | 只增 | 由 `RunStore` 写入 | 修订不符的写入被拒绝 |

### 运行中出现的键

| 键 | 设置 | 清除 | 持久化顺序与恢复 |
| --- | --- | --- | --- |
| `current_task` | `decide` 选择任务；修订发布（选中的任务）；直接派发 worker 时（本地准备之前） | 接受 | 有当前任务时只调度它；待派发指令的任务必须等于它 |
| `pending_directive` | `decide` 的 implement（与回执消费同次）；修订发布（另带 `revision`）；续作拒绝 | worker 调用前与预算同次消费 | 本地准备失败时保留；调用前、本地准备之后核对一次任务与快照（全部指令），修订来源的再与计划文件核对 |
| `pending_task` | worker 交接（`verifying`，与回执消费同次）；预检通过后与验证轮次同次保存审阅对照 `required_findings`；验证后 `reviewing` | 审阅结束（与 `review_history` 及后续阶段同次）；候选变化、预检或验证失败 | 同一快照复用已完成阶段；reviewing 阶段的审阅派发、重放和重做都只用保存的对照，缺少即停止；每次构造审阅包都按摘要读 worker 原件，`worker_result` 与原件不同即停止 |
| `pending_acceptance` | 审阅批准（`worker_source` 与审阅记录的来源同次写入，不存提交消息） | 接受登记或候选变化 | 提交前依次：任务记忆记录预检、按摘要读 `worker_source` 原件取提交消息（来源须与批准它的审阅记录一致）、快照核对一次（变了则旧批准失效并清除本键）、索引、范围、审阅来源；快照以外的核验失败保留本键 |
| `pending_post_accept` | 与接受同次 | 任务记忆、知识提案、`task_accepted` 依次完成 | 每步完成即保存，记忆用稳定操作 ID 去重；完成前不派发后续任务 |
| `pending_revision` | `decide` 的 revise 落地：计划文件先写，再与回执消费同次保存修订号、路径与 SHA256 | 修订发布 | 发布前复核文件与原件、重放修订；需要时归档、重置，再发布；其间任一点中断，普通 resume 从计划文件重来（见 recovery.md 的指挥提问、修订与范围） |
| `pending_continuation` | 续作登记 | 条件满足或被拒绝时置 null | 条件未满足时停止并列出缺口 |
| `continuation_rejections` | `--reject-request`（续作） | 不清除；由 worker dispatch 的 `continuation_rejection_id` 消费 | 拒绝文件先写；与 `pending_directive` 同次 |
| `worker_recovery` | 已调用的 worker 被判定（无结果、无效、越界、无效阻断），与判定同次 | 同 dispatch 正常交接、接受、需归档的修订发布 | 后续指挥与 worker 的 `recovery` 输入；记录不含错误文字，组包时从来源派发条目读取错误摘录与失败类型（`source.failure`） |
| `stop` | `Workflow.stop()`；进入 `awaiting_approval` | 进入 `running`；`abandon` | 与 `status` 同次保存，之后写 `run_stopped`；`load_run` 校验存在条件、登记与形状 |
| `quota_wait` | 额度等待开始，单独一次保存；只发布满足加载契约（`quota_wait.valid_record()`）的记录：来源 dispatch、角色、窗口、重置时刻、目标时刻、记录时刻、上限、失败样本名 | 醒来（单独一次保存）；中断与任何停止（与停止同次）；进入 `running`；`abandon` | 只在 `running` 时存在，`load_run` 用同一个函数校验形状；恢复不读它，强杀后遗留不证明进程存活（普通 resume 进入执行时删除） |
| `command_requirements` | 计划登记、恢复、调度时重算，变化才保存 | 不清除 | 只是诊断，派发前总是重算 |
| `context_capacity` | 整体审阅容量预估，变化才保存 | 不清除 | 必要下界超限时提前停止 |

`recovery.py` 的 `RECOVERY_BUSY_KEYS` 列出共用的八个待处理键，环境更新、续作拒绝、执行指导和操作者决定在其中任一有值时拒绝（见[恢复选项](recovery.md#恢复选项与互斥)）。`load_run` 在任何副作用前检查这些对象的类型、所引 dispatch 的角色与任务、`plan_history` 与 `pending_revision` 的引用形状、`pending_revision` 不与任务阶段或待派发指令并存，失败时不改写台账。

额度样本不在台账里：`runs/<id>/observations/quota/r<修订>-p<相位>-<进程启动时刻>-s<序号>-<供应商>-<触发>.json`，文件名按（台账修订、相位、进程启动时刻、序号）排序，排他发布、从不覆盖；只存归一化字段，不存 CLI 原始输出；缺失或损坏只是观察缺口（不变量 39）。

清理状态不在台账里：检出的删除意图与完成记录写在检出证据旁的 `cleanup.json`（`by` 是这份记录的写入者——运行时回收或 `cleanup-run`；补写完成记录的一方可能不是执行删除的一方），任务工作树的写在 run 目录的 `worktree-cleanup.json`（含本次是否允许删除忽略文件 `remove_ignored` 与删除前重新列出的有界清单）；运行时回收与 `cleanup-run` 都不改写台账（`--confirm-exited` 的退出确认除外）。台账里与检出有关的只有整体验证结论那次保存的 `checkout_digest`，它是结论的证据绑定，不是清理状态。

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

- 不是恶意代码隔离系统：CLI、验证命令和 MCP 服务以用户权限运行；强隔离需要专用 OS 用户或容器。`delete_path` 不隔离并发进程（不变量 25）。
- 未发布的 `command-staging/` 准备目录不参与枚举，也不自动回收；反复失败会累积磁盘占用，不影响恢复。
- 调用前的台账保存失败时，`finally` 的保存可能留下已扣预算、`invoked=true` 的条目；恢复按可重试处理，保守地多计一次尝试。
- 接受时重建暂存区改变了已审阅的暂存区摘要、在 commit 前中断，恢复作废旧批准并消耗新的工作尝试。
- 接受前只核对一次快照（不变量 13）：这次核对之后、commit 之前，并发的外部进程对候选的修改仍可能进入提交，运行时发现不了。
- 待接受的候选已变、而 worker 原件同时缺失或被改时，先因原件错误停止；恢复原件之后，下一次恢复才作废旧批准并重新执行、验证和审阅。
- 进程组监督不是完整的 OS 进程树隔离；`--confirm-exited` 依赖操作者核实。模型调用与验证命令的监督器在回收领导进程之后不再向进程组发信号：领导先退出、或领导被 SIGTERM 结束时，忽略 SIGTERM 的后代会留下来，退出门禁把它报成退出未确认（不是静默泄漏）。额度探测用自己的收尾（不变量 3），监督器的这一点留待后续修改。
- 额度等待（不变量 40）每次最多 `quota_wait_max_seconds` 秒，不等于整个 run 最多等这么久：累计时长只受派发预算约束（默认 200 次 × 6 小时）。
- 显示名与模型 ID 的对应是对显示文字的词法判断，不是供应商的映射，两个方向都可能误判：供应商用不带版本的家族名指某一个版本时，无关窗口可能被当成适用（多等，或因超过上限而不等）；供应商用另一个家族名作显示名时，适用窗口可能被当成无关（只按 `session`／`weekly_all` 等、提前醒来、再失败一次，受派发预算封顶）。没有真实样本验证。
- Claude Code 自带的 `CLAUDE_CODE_RETRY_WATCHDOG` 从每个 Claude／Codex 子进程的环境里删除；它的默认值没有查到，若某个 CLI 版本默认开启这种调用内等待，删除变量关不掉它（那时调用会等到 `agent_timeout_seconds` 被杀，结局 `budget/agent_timeout`，不与 Harness 的等待叠加）。
- 额度的两个接口（`claude -p /usage` 的 `usage_report`、`codex app-server` 的 `account/rateLimits/read`）都标 experimental，没有做过真实联调；变了就是“不可用＋原因”。Codex 探测不消耗额度只靠协议语义（只发三条消息、不开线程），没有零调用证据；`--max-turns 1` 只限制 Claude 误调用的规模，不能证明第一次误调用不会发生。
- Harness 被强杀时，正在运行的额度探测进程（新会话）与它的空临时目录可能遗留；台账没有它的记录，恢复不知道它存在。`codex app-server` 在 stdin 关闭后是否自行退出没有核实。
- 用 `setsid`／`setpgid` 离开探测进程组的后代不在收尾的组信号范围内；SIGKILL 之后卡在不可中断状态的进程使 `exit_confirmed=false`。
- 快照每次全量哈希，成本随仓库增长；审阅包的忽略清单只列路径（目录折叠、最多 100 条与 8 KiB），不显示被忽略目录里的内容，不校验内容；全新检出验证不保证 hermetic 构建。
- 结论未保存的验证轮次（准备失败、中断）没有台账摘要，`cleanup-run` 只凭检出记录里的归属与身份核对处理它们；能改写本 run 证据目录的人可以伪造归属。
- 记忆检索是本地词法排序，没有 embedding 或语义冲突检测；来源 `matching` 不证明事实正确。
- 串行 DAG；并行需要每任务工作树、依赖集成和冲突处理。
- 轮数上限与墙钟超时只限制单次调用，不是任务累计工具预算；到限不自动扩容、续接会话或豁免预算，也没有到限前的交接协议。
- 供应商用量来自 CLI 输出，缺失不当作零；Claude 的 `reported_cost_usd` 是 CLI 的名义估算，Codex 不报告美元，两者都不汇总。
- 额度统计的三个容差取值（120 秒上限、窗口长度的 1/60、窗口长度未知时 60 秒）与未激活窗口的重置时刻是否漂移，待真实联调复核；漂移超过容差会被分成多段。
