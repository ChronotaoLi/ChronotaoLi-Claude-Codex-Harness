# 恢复与操作者出口

本文写给处理停止 run 的操作者：看到哪种停止信息，该做什么，用哪个命令。它只适用于台账版本 20 的 run。保存顺序、子状态的设置与清除见[设计：状态转换](design.md#状态转换)，日常命令见 [README](../README.md)。

- [通用规则](#通用规则)
- [恢复选项与互斥](#恢复选项与互斥)
- [阻塞状态与操作者出口](#阻塞状态与操作者出口)
- [恢复步骤](#恢复步骤)
- [不能自动恢复的窗口](#不能自动恢复的窗口)

## 通用规则

- 先看 `status RUN`：它给出状态、停止投影 `stop`（类型、标题、出口命令与诊断摘录）、预算（`budgets`、`budget_explanation`、`remaining_model_calls`）、worker 结局计数 `worker_outcomes`、待答决定 `pending_decisions`、续作 `continuation`、缺失命令 `missing_command_requirements`、范围边界 `scope`（有已判定合格而未落地的提案时含即时重算的 `scope.held`）、最近环境阻断 `last_environment_blocker`、返工提示 `convergence`、审阅轮次 `review_rounds`、台账最后记录的步骤 `ledger_stage`（不证明进程仍在执行，见[页面](operations.md#页面)）、额度等待记录 `quota_wait`（等待期间）和运行期间账号额度的观测变化 `quota`（见[额度](operations.md#额度)）。`status RUN --full` 输出完整台账。run ID 格式有效时，`status RUN` 与 `status RUN --full` 都先在 stderr 第一行写出读取的检出与状态目录，这一行不进 stdout。
- 每个出口都沿用同一 run 的累计预算、证据和接受条件。读得出的拒绝、停止和阻断不会被丢弃；读不出否决的无效输出不是结果，由新调用重做（见[无效输出与自动重做](#无效输出与自动重做)）。同一快照上重复 `resume` 只会得到同样的停止，或消耗新的调用。
- 出口可以是继续，也可以是保留现场后 `abandon`。`abandon` 与 `resume` 一样先经过 `load_run`：加载时核验的证据（见[证据损坏](#证据损坏)）被删改时，两者都被拒绝。
- `resume` 与 `run` 一样读操作者配置与项目配置（项目配置缺失时同样拒绝，见[统一入口](operations.md#统一入口)），用两者合并后的有效配置与台账当前的 `config_snapshot` 比较。恢复时只能提高 `agent_timeout_seconds`、`verify_timeout_seconds`、`context_chars`、`claude_max_turns`（及各角色实际生效的 `claude_max_turns_by_role`）、`max_attempts`、`max_agent_dispatches`（写在哪一份的 `runtime` 里都可以），在项目配置的 `permissions.worker_commands` 加入精确命令（目录与 argv；已有条目不能删除或修改，可以重排，改一条的目录等于删除一条再加一条，会被拒绝），或任意调整额度等待上限 `quota_wait_max_seconds`（调高、调低、设 0 关闭；事件 `resume_config_changed`）。合法放宽被采纳后更新这份快照，后续恢复以更新后的值为基准：例如 `agent_timeout_seconds` 创建时是 300、一次恢复提高到 1000 之后，再恢复时设为 600 会被拒绝（`config_mismatch/immutable_config`），即使它仍高于创建时的值。其他配置与模型不能改；共享资产按创建 run 时记下的摘要比较，必须一致。比较的是有效配置的内容，不是文件路径或文件摘要：操作者配置换了路径而内容等价、任一份文件只改了注释，恢复照常；台账的 `config_path` 始终是创建时操作者配置的路径，只作显示。
- 恢复先核对共享资产与计划文件，记下 `--confirm-exited` 的确认，再确认旧进程退出、核对 Git 绑定、索引与 HEAD，之后才登记本次的其他恢复输入；这些检查失败时，答复、范围、配置放宽、环境更新、执行指导与拒绝都不写入，但同一条命令里已经保存的退出确认与审计照常保留。例外：已返回而未登记的指挥提问在 HEAD 门禁之前完成登记（见[指挥提问、修订与范围](#指挥提问修订与范围)），之后的回答与执行仍先过 HEAD 门禁。
- 外层自动化会话的权限边界见[外层 agent 操作说明](operations.md#外层-agent-操作说明)：新增权限、预算或需求选择要向用户请求。

## 恢复选项与互斥

| 选项 | 用途 |
| --- | --- |
| `--approve-plan` | 批准已发布、等待确认的首版；首版未发布时拒绝（`plan-unpublished`），不改写 run |
| `--step` | 最多接受一个任务后暂停 |
| `--confirm-exited DISPATCH_ID` | 记录操作者核实的退出确认，可重复 |
| `--environment-update FILE` | 登记环境事实与只读检查 |
| `--operator-decision FILE` | 回答决定请求，可重复，整批校验 |
| `--execution-guidance FILE` | 登记绑定任务与候选的执行约束 |
| `--reject-request FILE` | 拒绝已判定合格而未落地的指挥提案（修订或首版），或当前续作请求；文件 `{request_id, reason}` |
| `--scope PATTERN` | 扩大 run 的范围边界，可重复（也可写成 `--scope=PATTERN`）；只接受具体路径、字面子树 `p/…/**` 或 `**`；与答复、本次采用的配置放宽在一次保存里生效 |
| `--reverify` | 本次恢复首次进入整体验收时重验失败快照；可与 `--operator-decision` 同用（答复先整批登记，再重验）；已返回的指挥决定存在时拒绝；不绕过整体审阅的拒绝 |
| `--operation-id ID` | 指定操作记录 ID |

互斥规则（违反时在任何写入前拒绝）：

- `--reject-request`、`--execution-guidance` 不能与其他修改选项合用（`--confirm-exited`、`--step`、`--approve-plan` 除外）。`--reject-request` 不与 `--scope` 同用：同一次调用里先扩大再拒绝与先拒绝再扩大结果不同。
- `--operator-decision` 不能与 `--environment-update` 合用；答复含 stop 时不能带 `--reverify`（stop 答复使本次恢复立即停止，重验不会发生）。`--operator-decision` 可以与 `--scope` 同用（一次保存）。
- `--environment-update` 不能与 `--reverify`、`--scope` 合用。
- `--approve-plan` 在首版尚未发布时拒绝（包括首版持有时与 `--scope` 同用：先扩大，发布后再批准）。
- 已完成的 run 拒绝 `--confirm-exited`、`--scope`、`--environment-update`、`--operator-decision`、`--execution-guidance`、`--reject-request`；普通 `resume` 返回保存结果。已放弃的 run 拒绝 `resume`。

环境更新、操作者决定、执行指导和续作拒绝只在安全边界接受（持有的指挥提案也有回执，所以持有期间它们都被拒绝；`--scope` 与提案拒绝不受这条限制）：以下待处理键都为空——`pending_task`、`pending_acceptance`、`pending_post_accept`、`pending_revision`、`pending_directive`、`worker_receipts`、`review_receipts`、`commander_receipts`。

## 阻塞状态与操作者出口

“停止信息”是 `status RUN` 的 `stop.detail` 或 stderr 里 `harness: ` 开头的错误全文（节选；不含 `status` 写在第一行的位置行 `harness: 检出 …`）；紧跟其后的 `[harness-json]` 行是同一停止的投影。每个停止都有类型 `stop.kind/stop.reason`；外层按类型选择下表的行，`stop.exits` 给出可直接执行的出口命令，`data`、`source` 给出出口所需的身份（请求号、任务、派发、原件路径与摘要）。行标识是稳定的名字，`tests/test_stop_exits.py` 用它把行与走完出口的用例绑在一起；同一类型因情形不同（例如环境阻断的两条路由）分成几行。调用失败的停止（`provider_quota/*`、`provider_failure/*`、`budget/agent_timeout`、`budget/agent_turns`）在 `data` 里另给出只用于显示的事实，每项有自己的出现条件。取自原件的事实只在两份原件（`process.outcome.json`、`process.stdout`）都重新读到、且都与分类时的摘要相符时给出：`exit_confirmed` 在进程记录里是布尔值时给出，进程记录不完整时也给出；`api_error`、`terminal_reason`（符合标识符形状才给出）、`api_error_type`（CLI 的标准类型才给出）、存在而不能给出的标签字段名 `unrecognized_labels` 与 `http_status_state`（`missing`、`null`、`valid`、`invalid`）只在原件满足读信封的前提（见下一段）时给出；超时与启动失败只有 `exit_confirmed`。数值的 `http_status`（分类得到有效的 HTTP 状态时）与 worker 的 `attempt_charged`（这次调用是否计了工作尝试，取自台账）不取自重新读取，原件读不到或不符时仍保留。

`agent-turns` 与 `provider-quota` 到 `provider-failed` 这几行按 Claude 的错误信封分类，共同前提是：Claude 的调用正常结束，进程记录完整（退出已确认、有整数退出码、`evidence_complete` 为真、`cleanup_errors` 与 `errors` 为空），`process.stdout` 是显式的错误信封（能严格解码的 `type=result` 对象，`is_error=true` 或 `subtype` 以 `error_` 开头）。不满足这个前提时，这几行都不适用。满足前提时轮数用尽优先于标签分类：`subtype=error_max_turns` 是 `agent-turns`，不看标签字段（带 `api_error=connection_failed` 也一样）；其余信封才按各行的标签条件分类。

表的读法：类型后标“（操作）”的行是本次调用被拒或失败，停止值只在 stderr 与操作记录里，不写入台账（同一条命令里此前已保存的退出确认与审计照常保留）；其余行写入台账（`status RUN` 可见）。这个标记说的是该行的情形，同一类型在别的情形可能落在另一个边界（例如 `command-exit`、`head-changed`）。“出口命令”一列就是登记表给出的 `stop.exits` 模板：`RUN` 换成 run ID，`{dispatch_id}` 取自 `source.dispatch_id`，`FILE`、`PATTERN` 由操作者填写；带〔`flag`〕的只在 `data.flag` 为真时列出，〔`!flag`〕只在为假时列出。“无命令出口”表示不给可执行的命令，按“操作者该做什么”处理；“非停止”的行不停止 run。

停止在落盘、上抛或重报之前会“定稿”：加载核验重跑一次，若此刻台账或加载核验的证据已不符，最终的停止就是那次加载失败（`evidence/load_integrity` 或 `runtime_failure/io`，`data.superseded` 记下原来的停止）。进入计划待批准的保存与普通 `resume` 对它的重报同样定稿：失败时本次调用以那次加载失败结束（退出 1），不是待批准、不是 0。

所以已落盘、已上抛或已重报的停止，在那一刻列出的 `resume`／`abandon` 不被加载核验挡住；加载核验失败时，`resume`、`abandon` 报告的是加载失败自己的类型，不列命令出口。出口不保证走到“操作者该做什么”预期的结果：别的门禁仍然生效，也可能得到新的停止（例如确认退出之后转到只能放弃的停止）；中断不做定稿。保证与不保证见设计的不变量 38 与[停止与出口](design.md#停止与出口)。

### 退出码

`run`、`resume`、`abandon`、`status`、`watch`、`prepare-run`、`permission-diagnostics` 的退出码由停止类型查登记表得出，不解析文字：0 完成或操作者要求的暂停（`completed`、`paused`、计划待批准——包括未带 `--approve-plan` 的普通 `resume`，它返回 0 并重报同一停止）；3 run 自洽，有文档化的操作者动作或外部条件可解开；2 本次调用的输入不被接受（`invalid_input/*`、`config_mismatch/*`、`unsupported_ledger`，以及参数解析错误）；1 证据或现场与台账不符、Harness 自身失败、未分类；130 中断。`cleanup-run` 仍按逐资源结果返回 0／1。0 不代表 `completed`，外层仍要读结果 JSON 的 `status`。

| 行标识 | 类型（`kind/reason`） | 退出码 | 停止信息或状态 | 操作者该做什么 | 出口命令（`stop.exits`） |
| --- | --- | --- | --- | --- | --- |
| `plan-file` | `invalid_input/input_schema`（操作） | 2 | `--plan` 文件被拒绝（此时没有创建 run） | 修正计划文件后重新 `run` | 无命令出口 |
| `plan-approval` | `plan_approval/initial_plan` | 0 | `计划等待明确批准` | 用 `status RUN --full` 审阅保存的计划；普通 `resume` 只重报同一停止（退出 0），不批准 | `status RUN --full`；`resume RUN --approve-plan` |
| `plan-unpublished` | `invalid_input/plan_unpublished`（操作） | 2 | `首版计划尚未发布，--approve-plan 无可批准的计划` | `--approve-plan` 只批准已发布的首版；首版持有或尚未登记时先处理它（扩大范围、拒绝或普通 `resume`），进入计划待批准后再批准。被拒的调用不改写 run | `status RUN` |
| `plan-outside-scope` | `invalid_input/plan_outside_scope`（操作） | 2 | `--plan` 的模式无法证明在 `--scope` 声明的边界内（`data.gaps`、`data.suggested_scope`；此时没有创建 run、台账或工作树） | 修正计划，或调整 `--scope` 后重新 `run` | 无命令出口 |
| `scope-pattern` | `invalid_input/scope_pattern`（操作） | 2 | `--scope` 的条目不是具体路径、字面子树 `p/…/**` 或 `**`，或以受保护根开头（`.CLAUDE` 这类别名写法同样算；`data.patterns` 列出被拒的条目；整批拒绝，不创建或改写 run） | 改成合法形式后重试；一般 glob 的缺口用停止信息给出的建议条目 | 无命令出口 |
| `plan-approval-after-capacity` | `budget/context_capacity` | 3 | 带 `--pause-after-plan` 启动后，计划的整体审阅必要上下文超过预算（计划已登记、尚未批准） | 提高 `context_chars`；之后普通 `resume` 重新预检，通过即进入计划待批准（退出 0），未提高时仍是同一停止 | `resume RUN`；`abandon RUN` |
| `commands-all-ready` | `missing_commands/all_ready_tasks` | 3 | `任务所需命令尚未授权`（全部就绪任务都缺），或 `missing_command_requirements` 非空 | 在项目配置的 `permissions.worker_commands` 加入精确命令（目录与 argv 都按 `data.missing` 里的原样）；`cwd` 为 `.` 的 `git_query` 形式表内的 Git 查询无需授权；停止期间不扣尝试与派发预算 | `resume RUN` |
| `commands-selected` | `missing_commands/selected_task` | 3 | 已选中的任务（待派发指令保留）在派发前缺少命令授权 | 同上；授权后同一指令派发，不重新决定 | `resume RUN` |
| `continuation-commands` | `continuation/commands_required` | 3 | `等待续作条件：{'kind': 'commands', …}` | 授权缺少的命令（`data.missing_commands`，每条带目录），或认为方案需修正时拒绝续作（交给同一任务的新 worker；`request_id` 取 `status RUN` 的 `continuation.request_id`） | `resume RUN`；`resume RUN --reject-request FILE` |
| `continuation-timeout` | `continuation/timeout_required` | 3 | `等待续作条件：{'kind': 'budget', …}` | 把 `agent_timeout_seconds` 提高到请求下限，或拒绝续作 | `resume RUN`；`resume RUN --reject-request FILE` |
| `environment-blocked` | `environment/worker_blocked`（`invalid_output=false`） | 3 | `worker 环境受阻：…（已截断…）。完整 worker 结果：PATH（SHA256 …）` | 读完整结果；修复环境；命令被拒时加入精确命令（目录与 argv）。合法的环境阻断不扣工作尝试 | `resume RUN`；`resume RUN --environment-update FILE` |
| `environment-checks` | `environment/checks_failed` | 3 | `环境前置检查未通过` | 让检查条件成立（本次没有启动 worker、没有扣尝试） | `resume RUN` |
| `operator-answer` | `operator_decision/answer_required` | 3 | `等待操作者决定：task_id=… request_id=…` | 从 `pending_decisions` 读问题与来源，做出选择；指挥提问（种类 `commander`）也是这一行，未答或答 stop 时不调度任何模型 | `resume RUN --operator-decision FILE`；〔`reverify_available`〕`resume RUN --operator-decision FILE --reverify` |
| `operator-answer-reverify` | `operator_decision/answer_required`（`reverify_available=true`） | 3 | 整体验证普通失败（不是超时）后指挥就此提问 | 修复环境后一次完成答复与同一快照重验；整体审阅的拒绝不给这条出口 | `resume RUN --operator-decision FILE`；〔`reverify_available`〕`resume RUN --operator-decision FILE --reverify` |
| `ask-head-changed` | `workspace_mismatch/head_changed`（`question_registered=true`） | 1 | 指挥提问期间 HEAD 被改：提问已登记，但恢复门禁先拒绝改动的 HEAD | 先恢复 HEAD（未恢复时答复被 HEAD 门禁拒绝），再答复 | `resume RUN`；`abandon RUN`；〔`question_registered`〕`resume RUN --operator-decision FILE` |
| `decision-conflict` | `invalid_input/conflicting_retry`（操作） | 2 | 提交多个决定时中断，之后换序重交出现 `immutable decision artifact conflict` | 用原来的文件、按原来的顺序重新提交（被拒的重交不写任何答复文件，也不登记答复） | 无命令出口 |
| `operator-stopped` | `operator_decision/operator_stopped` | 3 | `操作者已决定停止：request_id=…` | 要继续就对同一请求提交新的 continue | `resume RUN --operator-decision FILE` |
| `scope-revision` | `scope_extension/revision` | 3 | `指挥修订有模式无法证明在范围边界内（当前规则无法证明包含）：{…}；提案已持有，未调用模型` | 提案已判定合格、只缺范围：回执保留，普通 `resume` 只重报同一停止（同一 `request_id`，不调用模型）。核对 `data.gaps` 与 `data.suggested_scope`（建议条目可能比缺口宽，`data.widened` 标出；完整缺口见 `status RUN` 的 `scope.held`）后执行扩大命令，同一原件随后落地；或拒绝它（文件 `{request_id, reason}`，`request_id` 取 `source.request_id`），下一次指挥决定带 `rejected_revision` | 〔`suggested_scope`〕`resume RUN {suggested_scope:scope_args}`；`resume RUN --reject-request FILE`；`status RUN` |
| `scope-initial-plan` | `scope_extension/initial_plan` | 3 | `指挥首版计划有模式无法证明在范围边界内…；提案已持有，未调用模型` | 同上；首版尚未发布，`--approve-plan` 被拒（`plan-unpublished`）。扩大后首版发布（带 `--pause-after-plan` 时进入计划待批准）；拒绝时这份首版预写过的计划文件归档到 `plans/rejected/` | 〔`suggested_scope`〕`resume RUN {suggested_scope:scope_args}`；`resume RUN --reject-request FILE`；`status RUN` |
| `scope-supplied-plan` | `scope_extension/supplied_plan` | 3 | `供给的首版计划越出范围边界…；未发布、未调用模型`（`run --plan` 在首版登记之前中断，之后用 `resume --scope` 第一次声明或扩大了边界） | 供给计划没有回执可拒绝：扩大边界使计划在内，或放弃后用修正的计划重新 `run` | 〔`suggested_scope`〕`resume RUN {suggested_scope:scope_args}`；`abandon RUN` |
| `task-attempts` | `budget/task_attempts` | 3 | `… 已达尝试次数上限（runtime.max_attempts 当前值 N）` | 先看 `convergence` 与审阅轮次判断是否值得继续；必要时提高 `runtime.max_attempts` | `resume RUN`；`abandon RUN` |
| `dispatch-limit` | `budget/agent_dispatches` | 3 | `已达 run 模型派发次数上限（已用 N，上限 M）；如果之后全部一次通过，至少还需 K 次模型调用（下界…）` | 按至少 K 次（`data.minimum_remaining_calls`）的余量提高 `max_agent_dispatches`；K 不逐批计入同一候选或整体快照的后续审阅，也不含指挥决定、返工与重做 | `resume RUN`；`abandon RUN` |
| `context-dispatch` | `budget/context_capacity` | 3 | `任务必要上下文 N 字符超过预算 M` | 提高 `context_chars`；看 `source.artifact` 指向的 `context-report.json`。超限发生在本地准备阶段，不产生 worker 尝试或反馈；唯一就绪、尚未尝试的任务在普通 `resume` 时仍直接派发 worker，不经过指挥，因此不能指望原 run 的指挥修订拆小它；无法提高时保留现场后放弃，用拆小后的计划另开新 run | `resume RUN`；`abandon RUN` |
| `agent-timeout` | `budget/agent_timeout` | 3 | 调用超时（进程记录的 `termination_reason=timeout`；worker 的这次调用已当场判定并计一次工作尝试） | 核对候选进展，再决定是否提高 `agent_timeout_seconds` | `resume RUN` |
| `agent-turns` | `budget/agent_turns` | 3 | Claude 轮数用尽（信封 `subtype=error_max_turns`，worker 计一次工作尝试） | 核对候选进展，再决定是否提高 `claude_max_turns_by_role` | `resume RUN` |
| `final-timeout` | `verification/final_timeout` | 3 | `整体验证超时` | 修复环境或提高 `verify_timeout_seconds`；普通 `resume` 在同一快照重验 | `resume RUN` |
| `dispatch-exit` | `process_unconfirmed/dispatch_exit` | 3 | `旧 dispatch … 的进程组仍存在` 或 `无法确认旧 dispatch … 已退出` | 核实该进程及其命令子进程已退出，不终止无关进程；终态 run 用 `cleanup-run` 的同名选项。`data.returned_unsealed` 为真时，这是一个已返回对象结果、尚未封存的 worker：未封存的结果事后不补认，确认之后只能放弃，所以不列 `resume`；为假时，确认之后 worker 的这次调用按原件判定（可重试、无效输出或未封存结果都可能） | 〔`!returned_unsealed`〕`resume RUN --confirm-exited {dispatch_id}`；`abandon RUN --confirm-exited {dispatch_id}` |
| `command-exit` | `process_unconfirmed/command_exit`（操作） | 3 | `Cannot confirm command process exited: …`。本行是恢复预检的拒绝；运行中发现时（例如 worker 返回、封存之前）同一类型作为 run 停止写入台账 | 同上；用父 dispatch ID（`source.dispatch_id`）确认 | 〔`!returned_unsealed`〕`resume RUN --confirm-exited {dispatch_id}`；`abandon RUN --confirm-exited {dispatch_id}` |
| `provider-quota` | `provider_quota/usage_limit` | 3 | 供应商额度用尽（Claude 信封 `api_error=usage_limit_reached`，`api_error_type` 缺失、为 null，或为 `rate_limit_error` 而 HTTP 状态是 429、缺失或 null；`source.artifact` 是失败原件 `process.stdout` 与摘要），而且没有在进程内等过去（见[额度等待](#额度等待)）：`data.wait_reason` 写明为什么没等，`data.waits` 是本进程已完成的等待次数，判断走到哪一步就带到哪一步（`window` 选中的窗口、`resets_at` 它的重置时刻、`wait_seconds` 需要的秒数、`wait_limit_seconds` 配置的上限、`uncertain_windows` 对应不明的满窗口）。真实 CLI 的 run 另带额度样本：`data.quota_snapshot`（`available`、`unavailable`、`not_published`）、样本文件名 `quota_sample`、不可用原因 `quota_reason`，以及快照里已满（≥100%）的窗口与重置时刻 `full_windows`——账号观察，不是失败原因 | 等额度恢复后普通 `resume`；worker 的这次调用已当场判定，不计工作尝试，仍计入 `max_agent_dispatches`；额度未恢复时会再失败一次（那次失败照常先判断能否等待）。`exceeds_limit`：可调高 `quota_wait_max_seconds` 后普通 `resume`，不会“补等”这一次；`dispatch_budget`：先提高 `max_agent_dispatches`；`window_uncertain`、`reset_unknown`、`no_blocking_window`：快照证明不了该等到何时，自行判断时刻后 `resume`；`record_invalid`：快照里的时刻超出等待记录的范围；`not_running`：进入执行循环之前的失败不等（首版规划，以及恢复时重放已封存而未交接的 worker 结果所做的验证与任务审阅） | `resume RUN` |
| `provider-rate-limit` | `provider_quota/rate_limit` | 3 | 供应商限速（`api_error` 与 `api_error_type` 中一个是 `rate_limit_error`、另一个缺失、为 null 或也是它，或两个都缺失或为 null 而 `api_error_status=429`；`api_error=usage_limit_reached` 与 `api_error_type=rate_limit_error` 并存时不在这一行：HTTP 状态是 429、缺失或 null 时是 `provider-quota`，否则是 `provider-failed`）；额度样本字段同上 | 稍后普通 `resume`；worker 不计工作尝试 | `resume RUN` |
| `provider-overloaded` | `provider_failure/overloaded` | 3 | 供应商过载（`api_error` 或 `api_error_type` 为 `overloaded_error`，另一个缺失、为 null 或也是它） | 稍后普通 `resume`；worker 不计工作尝试 | `resume RUN` |
| `provider-authentication` | `provider_failure/authentication` | 3 | 账号认证或权限失败（`api_error` 或 `api_error_type` 为 `authentication_error` 或 `permission_error`，另一个缺失、为 null 或也是这两者之一） | 用 `doctor` 核对登录，修复后普通 `resume`；worker 不计工作尝试 | `doctor`；`resume RUN` |
| `provider-transport` | `provider_failure/transport` | 3 | 与供应商的连接失败：Claude 信封 `api_error=connection_failed`（按 CLI 的内部说明，连不上 API 主机或连接中途断开），没有响应体类型，HTTP 状态缺失或为 null，`terminal_reason=api_error` | 网络或代理恢复后普通 `resume`；worker 的这次调用已当场判定，不计工作尝试，仍计入 `max_agent_dispatches`，中途留下的改动经 `worker_recovery` 交给下一次 worker；外层操作者可以不问所有者直接恢复，连续至多 3 次（计数边界见 harness-operator 技能） | `resume RUN` |
| `provider-failed` | `provider_failure/invocation_failed` | 3 | 模型调用失败，CLI 信封报告了其他错误，包括两个标签字段指向不同类别、不认识的标签值、`stream_idle_timeout` 等未收入的种类，以及不满足 `provider-transport` 条件的 `connection_failed`（worker 计一次工作尝试） | 修复原因；worker 的这次调用已当场判定为可重试 | `resume RUN` |
| `provider-unknown` | `provider_failure/unknown` | 3 | 模型调用失败，原因未识别：没有进程记录或记录不完整、没有错误信封，或 Codex 角色的调用失败（Codex 不读信封；超时与启动失败仍按进程事实分别是 `agent-timeout`、`provider-start`）；worker 计一次工作尝试 | 修复原因 | `resume RUN` |
| `provider-start` | `provider_failure/local_start` | 3 | 模型 CLI 未能启动 | 恢复 CLI 后普通 `resume`（新的计费尝试） | `resume RUN` |
| `unsealed-return` | `evidence/unsealed_return` | 1 | `worker return interrupted before immutable seal; …; automatic retry forbidden`：输出能解码成对象结果，但没有封存——封存之前中断、CLI 非零退出、或封存前的本地检查失败（例如 worker 写了受保护的 `.harness`、改了工作树的 Git 绑定、或移动了 HEAD；原因在派发条目的 `error`，恢复预检另行报告绑定或 HEAD 的变化）；当场判定与恢复时都停在这里 | `--confirm-exited` 解不开；保留证据后放弃 | `abandon RUN` |
| `unsealed-unknown` | `evidence/unsealed_unknown` | 1 | 同上：返回证据互相冲突、超过读取上限或字节无法严格解码（判为 unknown）；解不出对象结果的输出不会停在这里，读文件时的系统错误也不会（见 `runtime-io`） | 同上 | `abandon RUN` |
| `invalid-worker-output` | 非停止 | — | worker 输出无效（结构、语义、界限、交接目标非法，或解不出对象结果） | 不停止：计一次工作尝试、回执结局 `result_rejected`，下一次尝试经指挥选择；只在同一任务达到 `max_attempts` 时以 `task-attempts` 停止 | — |
| `invalid-blocker-environment` | `environment/worker_blocked`（`invalid_output=true`） | 3 | worker 的 environment 阻断只有界限检查失败（条目 `invalid_blocked`，反馈带 `output_error`） | 原结果进入反馈的 `worker`，按 `environment-blocked` 处理；不给尝试豁免 | `resume RUN`；`resume RUN --environment-update FILE` |
| `invalid-blocker-operator` | `operator_decision/answer_required` | 3 | worker 的 operator 阻断只有界限检查失败 | 按 `operator-answer` 答复；不给尝试豁免 | `resume RUN --operator-decision FILE`；〔`reverify_available`〕`resume RUN --operator-decision FILE --reverify` |
| `invalid-blocker-task` | 非停止 | — | worker 的 blocked 输出结构或语义不合法（例如 `blocker_kind=operator` 而 `blockers` 为空），或 task 阻断只有界限失败 | 不停止：按 task 阻断交给指挥决定，反馈带 `output_error`，结构或语义不合法时不带原结果、不登记决定请求；不给尝试豁免 | — |
| `scope-violation` | `scope_violation/candidate_outside_scope` | 1 | `… 请先核对并清理越界修改，再 resume`（`data.path`、`source.task_id`） | 核对并清理范围外改动，不自动删除 | `resume RUN` |
| `protected-path` | `scope_violation/protected_path` | 1 | `任务修改了受保护路径：…`（`.git`、`.harness`、`.codex`、`.claude` 下，按折叠后的名称判定，`.CLAUDE` 这类别名写法同样算；`data.path` 是原始拼写） | 同上 | `resume RUN` |
| `special-file` | `scope_violation/special_file` | 1 | `候选含 Git 不能记录的特殊文件：…`（管道、套接字或设备；`data.path`、`source.task_id`） | 删除它或换回普通文件，不自动删除 | `resume RUN` |
| `redo-exhausted` | `invalid_output/redo_exhausted` | 3 | `<role> 输出连续两次无效：…；原件 PATH；普通 resume 重新请求，或 abandon` | 读原件与错误（派发条目的 `error`、`invalid_output`）；首次判定时计划超出整体审阅的必要上下文同样是无效输出；普通 `resume` 是新请求，最多再两次调用 | `resume RUN`；`abandon RUN` |
| `invalid-original-changed` | `evidence/runtime_integrity` | 1 | `无效输出原始结果缺失或无法读取`、`无效输出原始结果已改变` | 上一次无效输出的原件被删改，调用计费前的核对停止（这次核对在本地准备之后，留下一个 `invoked=false` 的失败准备条目）；恢复原字节后普通 `resume`，或放弃 | `resume RUN`；`abandon RUN` |
| `head-changed` | `workspace_mismatch/head_changed`（操作） | 1 | `worktree HEAD 与检查点不同` 或 `接受分支与意图不符` | 核对检查点和 `pending_acceptance.intent`（如有），恢复可信现场后 `resume`，或放弃 | `resume RUN`；`abandon RUN`；〔`question_registered`〕`resume RUN --operator-decision FILE` |
| `load-integrity` | `evidence/load_integrity`（操作） | 1 | 加载时核验的证据被删改（`worker packet artifact changed`、`worker schema artifact changed`、`evidence artifact disagrees with ledger` 等，见[证据损坏](#证据损坏)），或台账里保存的命令形状不合格（`运行台账 tasks[i] 的命令无效` 等）；运行中定稿得出时写入台账，`data.superseded` 记原停止 | 恢复原字节；不改台账摘要。`abandon` 同样被拒绝；无法恢复时保留 run 目录原样，另开新 run | 无命令出口 |
| `index-damaged` | `evidence/runtime_integrity` | 1 | 路径索引、输入索引或环境更新产物损坏（`path-indexes/`、`input-indexes/`、`environment/`） | 恢复原字节后普通 `resume`；无法恢复时可放弃 | `resume RUN`；`abandon RUN` |
| `candidate-changed` | `workspace_mismatch/candidate_changed` | 1 | `待派发指令的代码快照已变化`、`修订保留候选的代码或暂存区已变化`、`真实暂存区与接受意图不符` | 恢复原快照或意图允许的暂存区状态后普通 `resume`，或保留证据后放弃 | `resume RUN`；`abandon RUN` |
| `directive-mismatch` | `workspace_mismatch/directive_mismatch` | 1 | `待派发指令与已发布计划或候选不符` | 恢复台账与计划文件的原值后普通 `resume`，或放弃 | `resume RUN`；`abandon RUN` |
| `continuation-candidate` | `workspace_mismatch/candidate_changed` | 1 | `pending continuation candidate or task changed` | 恢复续作等待的候选后普通 `resume`（新 worker 继续原候选） | `resume RUN`；`abandon RUN` |
| `context-final` | `budget/context_capacity` | 3 | `计划的整体审阅必要上下文 N 字符超过预算 M`（供给计划） | 提高 `context_chars`；模型给出的过大计划首次判定时按无效输出重做，不停在这里 | `resume RUN`；`abandon RUN` |
| `proposal-capacity` | `budget/proposal_capacity` | 3 | `计划的整体审阅必要上下文 N 字符超过预算 M；…；已判定合格的提案保留` | 已判定合格的提案在重放时因运行条件（例如加入的命令授权加长了整体审阅的必要输入）超出容量：回执保留，不记无效、不调用模型。提高 `runtime.context_chars` 后普通 `resume` 落地同一原件，或拒绝它 | `resume RUN`；`resume RUN --reject-request FILE` |
| `plan-file-damaged` | `evidence/runtime_integrity`（操作） | 1 | `待应用的计划文件缺失或无法读取`、`…已改变`、`已发布的计划文件（revision-N）缺失或无法读取`、`…已改变`、`台账任务表与已发布的计划文件不符`、`已发布的计划文件（revision-N）的命令无效` | 恢复计划文件的原字节后普通 `resume`；`abandon` 不需要计划文件 | `resume RUN`；`abandon RUN` |
| `revision-head` | `workspace_mismatch/head_changed` | 1 | `应用修订前 HEAD 与已校验计划不符` | 恢复原 HEAD 后普通 `resume`（修订照常发布），或放弃 | `resume RUN`；`abandon RUN`；〔`question_registered`〕`resume RUN --operator-decision FILE` |
| `shared-assets` | `config_mismatch/shared_assets`（操作） | 2 | `共享规则、skills 或 MCP 已改变` | 恢复原版本的共享资产 | `resume RUN` |
| `wake-shared-assets` | `config_mismatch/shared_assets` | 2 | 同上，额度等待醒来后的前提核对发现的（等待期间改了共享资产）；之后没有调用任何模型 | 恢复原版本的共享资产后普通 `resume`；醒来后的核对与手工 `resume` 是同一组函数，HEAD、Git 绑定、计划文件、索引、加载核验的不符也按各自的行处理 | `resume RUN` |
| `config-changed` | `config_mismatch/immutable_config`（操作） | 2 | `配置已改变：…` | 把操作者配置或项目配置改回[通用规则](#通用规则)允许的范围：不可放宽的值恢复原样，可放宽的值不低于台账当前 `config_snapshot` 里的值 | `resume RUN` |
| `binding-missing` | `workspace_mismatch/binding_missing`（操作） | 1 | `缺少已保存的 worktree Git 绑定` | 启动在绑定保存前中断；放弃后重新 `run`，核对后手工 `git worktree remove` 并删除 `harness/<run>` 分支 | `abandon RUN` |
| `unsupported` | `unsupported_ledger/ledger_version`（操作） | 2 | `台账版本 N 不支持（当前版本 20）` | 当前版本不恢复、不放弃、不准备、不清理、不解释这份台账（见[终态与其他版本的台账](#终态与其他版本的台账)）；运行列表仍显示路径与版本；开始新 run，原始 JSON 用文件工具查看 | 无命令出口 |
| `busy` | `busy/run_lock`（操作） | 3 | `已有进程持有运行锁`。额度等待期间 `resume`／`abandon` 也会遇到：锁外读到的台账有 `quota_wait` 时，停止值带 `quota_wait_until`、`quota_wait_recorded_at` 与固定说明 `quota_wait_note`（记录不证明持锁的进程仍在等待），并列出 `status RUN` | 等另一个 `run`／`resume`／`abandon` 结束后重试原命令；看 `status RUN` 的 `quota_wait` 判断是否在等额度；要取消等待就中断那个进程（Ctrl-C 或 SIGTERM），再按 `interrupted` 处理。`cleanup-run` 遇到锁被占按逐资源结果返回 1 | 〔`quota_wait_recorded`〕`status RUN` |
| `setup-failed` | `verification/setup_failed` | 3 | `整体验证依赖准备失败` | 修复环境；普通 `resume` 用另一全新检出，同一尝试。准备命令的 `cwd` 不在 HEAD 中、也不由前面的准备命令创建时，每次 `resume` 都在同一 HEAD 的新检出里同样失败：`verification.*` 恢复时不能改，只能 `abandon` 后用改正的配置开新 run | `resume RUN` |
| `checks-modified` | `verification/checks_modified_files` | 3 | 验证或依赖准备修改了受检文件 | 让验证只读；验证生成的文件在 `info/exclude` 加精确模式 | `resume RUN` |
| `dirty-final` | `workspace_mismatch/dirty_final_workspace` | 1 | 真实暂存区条目或 Git 从工作树构建的树与已接受提交不符，或构建失败；消息给出差异路径或 Git 错误（例如过滤器失败） | 恢复已接受提交对应的暂存区和工作树，或修好 Git 构建错误后普通 `resume`；也可放弃 | `resume RUN`；`abandon RUN` |
| `binding-changed` | `workspace_mismatch/binding_changed` | 1 | worktree 的 Git 绑定核对不通过，例如 `.git` 的指向被改、执行命令时工作树根不是绑定的目录；核对用的 Git 调用失败（例如超时）也报这一类，这时绑定未必改变 | 保留现场核对 | `abandon RUN` |
| `process-incomplete` | `evidence/process_incomplete` | 1 | 指挥、审阅或验证进程结束了，但退出观察留下清理或持久化故障 | 核实进程已退出。列出的只有放弃：确认退出修不好已记下的故障。按实际情形，`resume`（退出未确认时带 `--confirm-exited ID`）的结果不同：指挥或审阅进程正常结束、结果已返回而 outcome 未完整落盘时，`resume` 仍被拒；指挥或审阅调用没有返回结果（例如超时且清理出错）时，`resume` 丢弃无返回的意图并重新派发；验证命令进程（例如超时且清理出错）时，`resume` 重新执行验证 | 〔`!exit_unconfirmed`〕`abandon RUN`；〔`exit_unconfirmed`〕`abandon RUN --confirm-exited {dispatch_id}` |
| `runtime-io` | `runtime_failure/io` | 1 | 本地 I/O 失败（权限、句柄、设备、磁盘满）；逐级打开命令 `cwd` 遇权限拒绝时记 `start_failed`，不在此列 | 排查本地环境；证据未必损坏，不要“恢复原字节”；修好后重试原命令。worker 调用失败的判定读不出原件（持续的 `EIO`、权限）时也停在这里：系统读取错误不形成判定，回执保留，每次 `resume` 都重新判定，故障不消失就每次停在这里；故障只涉及这次尚未判定的原件、加载核验通过时，`abandon` 仍可用（它不判定），加载本身也读失败时 `resume` 与 `abandon` 都要先修复读取条件 | 无命令出口 |
| `runtime-database` | `runtime_failure/database`（操作） | 1 | 记忆数据库错误 | 排查数据库；修好后重试原命令 | 无命令出口 |
| `runtime-invariant` | `runtime_failure/invariant`（操作） | 1 | 运行时一致性检查失败（例如另一写入者改了台账） | 保留现场报告 | 无命令出口 |
| `unclassified-harness-error` | `unclassified/untyped_harness_error` | 1 | 未分类的停止 | 不推测恢复动作，保留现场报告 | 无命令出口 |
| `unclassified-exception` | `unclassified/unexpected_exception` | 1 | 未预期的异常 | 同上 | 无命令出口 |
| `interrupted` | `interrupted/signal` | 130 | Ctrl-C、SIGTERM、SIGHUP | 确认中断原因；普通 `resume` 报出需要确认退出的确切派发 | `resume RUN`；〔`exits_confirmed`〕`abandon RUN` |
| `input-arguments` | `invalid_input/cli_arguments`（操作） | 2 | 命令参数无效（例如 run ID 形式无效、run 不存在、目标为空、`--operation-id` 形式不合格或同名操作记录已存在）；或 `run`、`resume`、`permission-diagnostics` 找不到项目配置（`data.option` 为 `--repo`，错误给出期望的路径；拒绝在任何写入之前） | 修正参数后重试；缺项目配置时以 `examples/project.toml` 为起点在给出的路径创建它（见[两份配置](../README.md#两份配置)），再重试原命令 | 无命令出口 |
| `input-file` | `invalid_input/input_schema`（操作） | 2 | 输入文件不合格（`--operator-decision`、`--environment-update`、`--execution-guidance`、`--reject-request`、`prepare-run --manifest`），含文件不存在、读不出、不是 UTF-8 或不是 JSON | 修正文件后重试。`--operator-decision`、`--execution-guidance`、`--reject-request` 与 `prepare-run --manifest` 的读取与解析在任何写入之前，这些拒绝不改写台账；`--environment-update` 的文件在退出确认之后才读取，同一条命令里先前保存的 `--confirm-exited` 确认与审计会保留，其他恢复输入都不写入（内容合法而之后的状态检查拒绝的，见各选项） | 无命令出口 |
| `options` | `invalid_input/incompatible_options`（操作） | 2 | 选项不能同用（见[恢复选项与互斥](#恢复选项与互斥)） | 拆成合法的组合后重试 | 无命令出口 |
| `stale-request` | `invalid_input/stale_request`（操作） | 2 | 输入针对的请求或来源已失效（未知请求、之后已有模型调用、已被新答复取代；`--reject-request` 的 `request_id` 不是已判定合格而未落地的提案或当前续作） | 用 `status RUN` 读当前请求后重答 | `status RUN` |
| `run-state` | `invalid_input/run_state`（操作） | 2 | 该命令或选项不适用于 run 的当前状态（已放弃、已完成、有待处理阶段） | 用 `status RUN` 核对状态 | `status RUN` |
| `config-file` | `invalid_input/config_file`（操作） | 2 | 配置文件不合格：不是 UTF-8、不是合法 TOML、嵌套过深、写错文件的节、未知键、取值或命令形状不合格；`run` 的 `verification.commands` 为空（`data.config_key`）。`data.file` 指出是操作者配置还是项目配置，`source.artifact` 是文件路径；合并之后的校验不指出是哪一份，两份都要核对。拒绝在任何写入之前 | 按 stderr 的诊断修正那份文件后重试原命令 | 无命令出口 |

## 恢复步骤

### 计划批准与分步

`run "目标" --pause-after-plan` 保存计划后进入 `awaiting_approval`。启动时可以用 `--scope PATTERN`（可重复）声明范围边界，首版计划受它约束（见[指挥提问、修订与范围](#指挥提问修订与范围)）；启动后才第一次声明时，尚未发布的首版同样受约束。`status RUN` 在等待首版批准时给出 `scope_summary`：每个任务的具体路径与通配模式计数、按顶层目录分组的模式（分组只做导航）及各自是否在边界内（未声明时为 `null`）；“在边界内”是确定性检查，“足以满足验收”仍要人判断。普通 `resume` 不会批准，它返回 0 并重报同一个 `plan_approval/initial_plan` 停止；未批准但处于其他状态（例如计划登记后、待批准保存前被中断）的 run，普通 `resume` 重新经过预检后进入待批准；`resume RUN --approve-plan` 批准已发布的首版后继续，可与 `--step` 合用；首版尚未发布时（首版持有、规划提问未答，或首版登记之前中断）它被拒绝，不改写 run。规划阶段的指挥提问先于计划批准，`--approve-plan` 不回答提问。`--step` 最多接受一个任务后进入 `paused`，最后一个任务之后仍执行整体验收。批准不授予命令权限。

### 命令授权与前置命令

任务的 `required_commands` 与 `self_checks` 是前置条件，每条是一个命令 `{cwd, argv}`。派发前按（目录，argv）与 `verification.commands`、`permissions.worker_commands` 精确比较：目录与参数、空参数、字面 `*` 和换行都按原样字符串比较，不做大小写或 Unicode 折叠，不接受前缀；同一 argv 在另一个目录的授权不算；`verification.setup_commands` 不授予 worker 能力。`cwd` 为 `.`、argv 能被 `git_query` 解析的形式表内命令算作已满足（只表示能力存在，worker 要用该工具执行，不能用 Bash）；`git_query` 只在工作树根执行，同一 argv 带其他目录不算已满足，需要显式授权。缺少授权的任务不启动 worker、不扣尝试，其他就绪任务照常继续；全部就绪任务都缺命令时停止，停止数据与 `status RUN` 的 `missing_command_requirements` 列出的命令都带目录。在项目配置加入精确命令后普通 `resume` 即可重检，无需环境更新。

### 续作与操作指导

worker 需要新的精确命令或更长的单次时限时，返回 `blocker_kind=continuation` 和续作请求：

```json
{"kind":"commands","commands":[{"cwd":".","argv":["python3","-m","unittest","tests.test_export","-v"]}],"minimum_timeout_seconds":0,"reason":"聚焦检查需要此精确命令"}
```

`commands` 的每条是命令 `{cwd, argv}`，目录是授权的一部分。时限请求用 `kind=budget`、`commands=[]` 和正的 `minimum_timeout_seconds`。请求不授权、不执行命令。普通 `resume` 在请求的命令已按（目录，argv）进入 `verification.commands`／`permissions.worker_commands`（或 `cwd` 为 `.` 且属于 `git_query` 形式表）、或 `agent_timeout_seconds` 已达下限时，以新派发继续原候选；条件未满足时停止并列出缺口。续作不重置任何预算。

请求本身需要先修正时，用 `resume RUN --reject-request FILE` 拒绝它。输入是最多 64 KiB 的严格 JSON，恰好两个字段；`request_id` 取自 `status RUN` 的 `continuation.request_id`：

```json
{"request_id":"continuations_<sha256>","reason":"旧命令绑定的脚本需要先修正；修改后重新申请精确命令。"}
```

`reason` 非空、最多 4,000 字符、不含 NUL；`request_id` 是 `continuations_` 或 `proposal_` 加 64 位小写十六进制（后者拒绝指挥提案，见下文）。任务、来源结果与候选快照取自那条续作记录。续作拒绝只用于 `blocked`、`interrupted` 或 `paused` 的当前未接受任务，要求来源结果、请求、候选、Git 绑定与 HEAD 一致，旧派发和命令都已退出，且来源之后没有模型调用。下一步直接派发同一任务的新 worker，不先调用指挥；拒绝已保存而 worker 尚未调用时，普通 `resume` 继续同一修正，相同文件可幂等重试。候选改变时停止。

`resume RUN --execution-guidance FILE` 登记一条执行约束，输入最多 64 KiB，恰好四个字段：

```json
{"task_id":"task_id_from_status","source_dispatch_id":"worker_dispatch_id","source_snapshot_digest":"64_lowercase_hex_characters","summary":"必须遵守的具体执行约束"}
```

`summary` 非空、最多 4,000 字符。来源必须是已有权威绑定的 worker 结果（续作记录或当前反馈），之后没有模型调用，当前候选仍匹配。指导按任务进入后续输入并标明是否适用当前候选；它不授予命令、不改变目标或验收、不增加预算。

### 环境更新

worker 因环境受阻后，用结构化更新说明已修复的环境事实：

```bash
harness resume RUN --environment-update /absolute/path/update.json --step
```

```json
{
  "schema_version": 1,
  "task_id": "task_id_from_status",
  "blocker_dispatch_id": "worker_dispatch_id_from_status",
  "summary": "依赖已在当前运行工作树中准备好；请重新检查后继续原任务。",
  "checks": [
    {"kind": "path", "path": "/absolute/path/to/dependencies", "path_type": "directory"},
    {"kind": "executable", "name": "python3"}
  ]
}
```

输入最多 64 KiB，每个 run 最多 32 条。`summary` 非空、最多 4,000 字符；`checks` 可省略，最多 16 项，只允许 `path`（绝对路径，`path_type` 为 `file`、`directory` 或 `any`）、`executable`（名称或绝对路径）和 `worker_command`（`{"kind": "worker_command", "command": {"cwd": "app", "argv": ["flutter", "test"]}}`：`command` 是一条精确命令，argv 最多 128 项、每项最多 4,096 字符；按（目录，argv）已配置，或 `cwd` 为 `.` 且属于 `git_query` 形式表即通过）。检查不执行命令、不启动子进程、不授权。只接受当前未接受任务的 worker 环境阻断，且来源之后没有模型调用（中断导致的 `interrupted` 不使来源失效）。之后同任务的每次 worker 尝试前都重检最新一条更新；失败时不启动 worker、不扣尝试或派发预算。`status` 的 `last_environment_blocker.stale=true` 表示来源已被后续调用取代。

### 操作者决定

worker 返回 `blocker_kind=operator`、指挥提问，或已接受任务为下游任务留下 `decision_requests` 时，`status RUN` 的 `pending_decisions` 列出请求。回答：

```bash
harness resume RUN --operator-decision /absolute/path/decision.json --step
```

```json
{"schema_version": 1, "request_id": "request_id_from_status", "task_id": "target_task", "decision": "continue", "summary": "操作者的明确选择及理由"}
```

输入最多 64 KiB、只含上述字段，`summary` 非空、最多 4,000 字符，`decision` 为 `continue` 或 `stop`。可重复指定多个文件：整批先校验，重复 ID 或任一非法、过期答复使整批拒绝；通过后全部答复文件先写入 `decisions/`，再一次保存。每条答复的序号按参数顺序分配并写进它的不可变文件，所以预写中途被中断后，必须用原来的文件、按原来的顺序重试：已写出的文件会被复用；换了顺序，序号与已写文件不一致，恢复以 `immutable decision artifact conflict` 停止，按原顺序重交即可继续。任一 `stop` 使本次恢复保存整批后停止，不派发模型。下游请求的答复在目标任务的下一次模型调用前可修订；worker 阻断的答复在任何后续模型调用后过期；指挥提问的回答作为 `operator_answer` 进入之后的指挥决定，直到一次指挥决定真正落地（持有、容量保留或被拒都不算落地）。决定不改变目标、范围、命令授权、验收或预算。问题文本空白、含 NUL 或过长时，请求里是有界说明与转义摘录，必须读 `source` 绑定的原文后再答。

### 指挥提问、修订与范围

指挥的 `ask` 登记一条种类 `commander` 的决定请求并停止（有当前任务时目标是它，否则是 `final`）。这条请求未答或答 stop 时不调度任何模型，与目标无关；用 `--operator-decision` 回答 continue 后，下一次指挥决定带上 `operator_answer`。回答不授予范围、不降低验收，也不增加预算。规划、任务和整体三个阶段都可能提问。

提问的识别只要求公共前提（退出已确认、原件已绑定摘要、严格解码成对象、envelope 身份相符），不要求调用期间候选代码与 HEAD 未变：变了也照常登记，派发条目记 `candidate_changed` 并写事件 `commander_candidate_changed`，停止信息提示先核对工作树；登记不认可这些改动，之后的派发、验证与接受各自核对快照与 HEAD。其他决定（implement、revise）在候选或 HEAD 变化时不判定，停止并保留回执，恢复原状后普通 `resume`。合格的提问在一次保存里登记请求并消费回执。已返回而未登记的提问（登记前中断）在普通 `resume` 的 HEAD 门禁之前完成登记，HEAD 或候选已变也一样；之后的回答登记与执行仍要先过 HEAD 门禁，HEAD 不同时须先核对并恢复。不合格的提问直接登记（条目记 `invalid_asked`）。

**范围边界。** run 的范围边界 `scope` 是操作者用 `run --scope PATTERN`、`resume RUN --scope PATTERN` 声明的条目列表（`status RUN` 的 `scope.entries`），只增不减。条目只有三种形式：具体路径（不含 `*`、`?`、`[`）、字面子树 `p1/…/pk/**`（前缀各段都是字面量）和独立的 `**`；都要先通过普通的范围校验（不得以 `.git`、`.harness`、`.codex`、`.claude` 或它们的别名写法开头，如 `.CLAUDE`）。空列表表示没有声明：首版计划不受边界约束，之后的修订只能复用已发布计划里的模式（`status RUN` 的 `scope.note` 写明这一点）；运行时不会把首版的模式写回边界。

模型提出的每个新模式——首版落地那一刻边界非空时首版的全部模式、修订新增任务的模式与 `add_allowed_paths`——都要由三条规则证明在“修订前已发布计划的全部模式 ∪ 边界”之内：与其中一条相同；是具体路径且被其中一条匹配；按路径段位于某条字面子树的前缀之下（`app2/x.py` 不在 `app/**` 里）。待判定的提案自己的模式不进入这个集合，同一份提案里的模式不能互相证明。判断可靠、不完备：`app/[ab]/**` 对 `app/a/**`、`app/b/**` 这类情形证明不了，停止信息写“当前规则无法证明包含”，不写“存在越界文件”。边界只约束计划准入：接受时的范围检查仍只用任务自己的 `allowed_paths`，受保护根照旧禁止，越界的候选在任何指挥调用之前就停止，扩大边界不追认它；边界不授予命令，也不改变验收与审阅。

**判定与持有。** 一份已返回的指挥原件（implement 或 revise）按四类检查判定：证据（失败即按证据或现场类型停止，回执保留，不重做）；原件契约（只依赖原件与修订基线，首次判定不合格是无效输出）；范围包含（只缺范围即持有）；运行条件（整体审阅的必要上下文：首次判定超出是无效输出；已判定合格的原件在重放时超出则停止并保留原件，`budget/proposal_capacity`）。派发条目记 `completed` 且回执仍在，表示原件已判定合格而未落地；之后重放失败只记 `error`，不降级。持有与容量保留都不消费回执、不记无效、不写计划文件，普通 `resume` 重放同一原件得到同一停止（同一 `request_id`），不调用模型。持有期间环境更新、操作者决定与执行指导被拒绝（回执仍在）；首版持有时 `--approve-plan` 被拒。

**扩大。** 停止信息与 `status RUN` 给出两组字段：`gaps` 是提案里证明不了的原模式（按任务），`suggested_scope` 是由它们推导的合法条目——缺口本身是具体路径或字面子树时原样，否则取第一个通配段之前的字面前缀加 `/**`（`tests/*.py` → `tests/**`，`**/*.py` → `**`），`widened` 标出比缺口宽的条目。核对后执行停止给出的命令（`resume RUN --scope=…`，每条都写成 `--scope=VALUE`，以 `-` 开头或含空格的值也不会被误读）；建议可能比缺口宽，也可以改写成更窄的具体路径。边界是持续、跨任务的授权：扩大后当前与之后的提案都可复用这些条目，旧终端里的同一条命令也会放行之后尚未读过的提案。只覆盖一部分缺口时继续持有，停止里只剩其余缺口。停止值里的列表有上限，截断时 `data.*_omitted` 记被截去的条数；完整缺口在 `status RUN` 的 `scope.held`，每次读取时按当前台账与原件即时重算，不写任何文件。`--scope` 与答复、本次采用的配置放宽（例如为新任务加入 `permissions.worker_commands`）在一次保存里生效，然后重放同一原件：全部在边界内且容量通过时照常落地（首版：先按当前边界复核，计划文件先写，`original_tasks`、`tasks`、`plan_history` 与回执消费同次；修订：计划文件先写，`pending_revision` 与回执消费同次，发布前按当前边界复核）。追加的命令授权可能加长整体审阅的必要输入，此时重放停在 `budget/proposal_capacity`，原件仍保留：提高 `runtime.context_chars` 后普通 `resume` 落地同一原件，或拒绝它。

**拒绝。** `resume RUN --reject-request FILE` 拒绝一份已判定合格而未落地的提案（持有、容量保留，或边界已扩大而尚未重放的都可以）：文件是 `{"request_id": "proposal_<sha256>", "reason": "…"}`，`request_id` 取停止的 `source.request_id` 或 `status RUN` 的 `scope.held.request_id`，`reason` 非空、最多 4,000 字符。从未判定过的原件（返回后在判定之前中断）不能拒绝，先普通 `resume`；请求 ID 不符是 `stale_request`；提案绑定的候选或 HEAD 已变时拒绝被挡住（`candidate_changed`）。拒绝先写内容寻址的 `proposal-rejections/<sha256>.json`，再把这份提案在中断前预写过、尚未登记的计划文件（只有与由原件和当前基线重算出的字节完全相同的文件才算）改名归档到 `plans/rejected/`，最后一次保存消费回执并在来源派发条目记 `proposal_rejection`。被拒原件此后不会在任何恢复里落地；之后的指挥决定带 `rejected_revision`（请求 ID、原件引用与操作者理由），直到有一次指挥决定真正落地。保存之前中断时可以重交（换了理由也可以）。

**追加字段。** 修订的每个 update 都带 `id`、`description`、`add_depends_on`、`add_allowed_paths`、`add_required_commands`，三个 `add_` 字段只追加（去重、保持顺序），不用时是空数组；只能指向未接受的既有任务，对已接受任务的追加是无效输出。`add_required_commands` 的条目是命令 `{cwd, argv}`，去重按（目录，argv）整条比较。新增的命令只是前置条件：派发前缺授权照常以 `missing_commands/*` 停止，持有的停止信息提前列出提案里未接受任务的缺失命令（`data.missing_commands`），可以与 `--scope` 在同一次 `resume` 里一并加入授权。

修订只能保留既有任务 ID、追加前置任务、细化未接受任务的说明、依赖、范围与前置命令；已接受任务不变。新任务的 ID 与任何既有任务（含已接受任务）折叠后（不区分大小写等）相同时，与 ID 完全重复一样是无效输出，计划不变。修订先写成不可变的 `plans/revision-N.json`，与回执消费同次登记引用；发布前按摘要复核文件、读回原件按当前边界重放修订、核对 HEAD。只改当前任务的说明、范围或前置命令且仍选它时保留候选，交给同一任务的新 worker；其他修订先把未接受修改归档到 `recovery/revision-N`，再恢复工作树到已接受提交，然后计划版本、`tasks`、`current_task` 与下一条指令同次发布。归档与重置之间任一点被中断，普通 `resume` 都从计划文件重来，无需操作者介入。

### 验收不可达

当前范围或已答决定排除了满足某条验收所需的工作时，worker 应返回 `blocked/task` 指明验收项和边界，指挥可修订，无可行修订时提问。`continue` 不豁免验收，增加预算也不是解决不可达条件的证据。操作者可回答 stop，核对已接受提交后从 run 分支手动集成需要的部分，再 `abandon RUN`；未接受的候选不算已交付，run 不会标为 completed。

### 无效输出与自动重做

没有通过契约的模型输出不是结果。运行时只从原件读否决：退出已确认、原件字节已绑定摘要、严格解码成对象（拒绝重复键与非有限数，含溢出的数值字面量）、envelope 身份相符之后，审阅的 `request_changes`、blocking finding 或未 `resolved` 的必需项（核心结构合法时），指挥的 `ask`（`action` 为 ask 即可），以及 worker 的 `blocked`，都保持约束力。

- **指挥与审阅**：读不出否决的无效输出（含结构非法的审阅、不可解码的供应商输出）在同一次保存里记为 `status=invalid`、错误与原件摘要（`invalid_output`），回执消费，然后自动重做一次。重做是新派发，计入 `max_agent_dispatches`，条目记 `retry_of`，包里带 `previous_invalid`（上一次无效输出的引用与前 2,000 字符的错误；带上它会超出上下文预算时不带，条目记 `previous_invalid_omitted`）。重做仍无效即停止；之后的普通 `resume` 发起新请求，同样有一次重做。是否重做只看整个 run 的最后一次模型调用（审阅另要求那次调用审阅的候选快照与本次相同，否则既不是重做也不带 `previous_invalid`），所以重做调用之前的中断、或重做在本地准备时（额度、上下文、原件核对）停止，恢复后的那次调用仍是重做；重做已经调用、结果绑定之前被中断时，它按无返回处理，恢复后的调用是不带 `previous_invalid` 的新请求，这个新请求仍有自己的一次重做，与不中断相比最多多两次调用。
- **带否决的无效输出**：审阅按拒绝返工（`invalid_rejected`），它的 blocking finding 与有效拒绝的一样计入历史阻断问题（必需问题怎样从历史推导见设计的不变量 12）；指挥的 ask 照样登记提问并停止（`invalid_asked`，条目记错误）；worker 的 blocked 见上表。整体审阅的拒绝写入整体记录后同一快照不再审阅；进入整体审阅步骤时，若 `final_history` 末尾 `running` 的尝试属于另一个快照，它已返回而未登记的审阅在使用当前快照的历史之前，按该尝试保存的对照判定后存为它的结论：否决记 `invalid_rejected`（同样是对那个快照的拒绝，回到它时是指挥决定，不重抽整体审阅），其余记 `invalidated`（这份审阅不关闭任何问题，回到那个快照时照常进入整体审阅）；回执绑定的候选不是这次尝试的时以 `evidence/runtime_integrity` 停止并保留回执；回执是终端捕获时不离开这次尝试，停止并保留回执（设计的不变量 15）；按停止的出口处理，例如 `workspace_mismatch/candidate_changed` 时恢复原快照后普通 `resume` 消费它。无否决的无效整体审阅不写结论，同一快照可以重做。
- **worker**：无效输出（含解不出对象的完整输出）计一次工作尝试，回执消费，调度继续，受 `max_attempts` 约束。worker 调用失败而输出解不出对象时（例如 CLI 非零退出、输出截断）同样当场判为无效输出并继续，与退出码无关。
- **可选产物**：worker 的 `checkpoints`、`proposed_memories` 与审阅的 `approved_memory_keys` 不合格只丢弃自身，记 `checkpoints_discarded`、`memory_proposals_discarded`，结果照常处理；之后的上下文只带它们经过校验的投影（登记后的检查点、整批合法时的提案 key，不带记忆批准），原件保留原值。worker 的 `decision_requests` 不合格则整份结果无效。
- **只缺范围的合格提案不是无效输出**：持有（回执保留，见上节）；已判定合格的原件在重放时容量不够也不是无效输出（`budget/proposal_capacity`，原件保留）；另有契约缺陷时首次判定照常记无效并重做。

### 整体验证与重验

全部任务接受后，整体验证失败时：全部失败项都是超时则停在当前尝试，修复环境或提高 `verify_timeout_seconds` 后普通 `resume` 在同一快照重验；普通非零退出交给指挥决定，同一快照重验须显式 `--reverify`；若指挥已就此提问，修复环境后用 `resume RUN --operator-decision FILE --reverify` 一次完成答复与同一快照重验（答复先整批登记，再进入整体验收；停止信息里 `reverify_available=true` 时投影给出这条组合命令）。`--reverify` 只在本次恢复首次进入整体验收时生效，开始新的整体尝试，保留原失败记录；已返回而未落地的指挥决定存在时拒绝（先普通 `resume`），从未返回的决定意图在核实退出后丢弃，已扣的预算不退回。整体拒绝或非超时验证失败之后，整体阶段的下一步按当前快照上最近的整体记录推导（设计的不变量 15）：它仍是这次拒绝时，无论是否带 `--reverify`、是否同时答复，都是派生的指挥决定；仍是这次非超时验证失败时，没有 `--reverify` 就是指挥决定；这两种情况（例如已接受的修正任务没有改变快照）都不重抽整体审阅、不重跑相同代码。只改已跟踪普通文件的读写执行权限位（HEAD 与 Git 清洁状态都不变）也得到另一个快照；没有记录的快照进入整体审阅。

开启 `verification.fresh_checkout` 时，整体验证在系统临时目录的全新 detached worktree 中执行；准备失败或中断后，普通 `resume` 在另一个新检出重试当前最终尝试。准备命令与验证命令的 `cwd` 相对新检出的根；新检出只含 HEAD 的已提交内容，所以目录必须在 HEAD 中存在，或由排在前面、已经成功的准备命令创建。准备命令的目录不存在时依赖准备失败（`verification/setup_failed`，见出口表的 `setup-failed`）；验证命令的目录不存在时它是一条失败的检查（`termination_reason` 为 `start_failed`），按普通非零退出处理。验证通过、结论已保存且有创建登记的检出，由运行时在整体审阅派发之前尝试回收（首次进入与恢复同一处）；删除失败只记事件 `verification_checkout_reclaim_failed`，不改变结论、不重跑验证或审阅，检出留给 `cleanup-run`。准备失败、验证失败或中断的检出保留供诊断；清理依据见[放弃与清理](#放弃与清理)。验证中产生未忽略的新文件时，核对后可在 `git rev-parse --git-common-dir` 指向的公共目录的 `info/exclude` 中加入精确模式，再恢复；不要用宽泛模式掩盖待交付代码。

### 额度等待

Claude 担任的角色（worker、`--commander fable` 的指挥、配置成 Claude 的审阅者）在执行循环里遇到已判定的 `provider_quota/usage_limit` 时，默认先在同一进程里等到快照给出的重置时刻再继续，不停止、不退出。条件、选窗口的规则与不等时的原因见设计的不变量 22；配置键 `runtime.quota_wait_max_seconds`（默认 21,600 秒，0 关闭），单次最多这么久，余量 60 秒；没有命令行参数。Codex 的失败、进入执行循环之前的失败（首版规划，以及恢复时重放已封存而未交接的 worker 结果所做的验证与任务审阅）、限速与其他失败从不等待。

- **等待期间**：状态保持 `running`，台账另有只供观察的 `quota_wait`（来源调用、窗口、重置时刻、等到何时、记录时刻、上限、失败样本），`status RUN` 的 `quota_wait` 与观察网页详情首屏的额度等待记录显示它（不在“需要处理”区），`watch` 的快照带 `quota_wait`，退出条件不变。进程持有 `run.lock`：另一个 `resume`／`abandon` 得到 `busy/run_lock`（提示里有等待记录的时刻，记录不证明持锁的进程仍在等待）。事件 `quota_wait_started`、`quota_wait_finished`；失败时与醒来时各取一份额度样本（触发 `quota_failure`、`wake`）。
- **醒来之后**：先删除 `quota_wait`（单独一次保存），再重跑 `resume` 预检里只读的前提核对，与手工 `resume` 调用同一组函数：盘上台账仍是本进程刚提交的那份、加载核验、共享资产摘要、环境产物、计划文件、Git 绑定、路径与输入索引、HEAD、依赖副本、回执链接。任何一项不符就停止，类型与 `resume` 遇到同样情况时相同（例如 `head-changed` 的 `workspace_mismatch/head_changed`、`wake-shared-assets`、`load-integrity`），之后不调用任何模型。不重跑带写入的预检步骤（退出确认、登记已返回的提问、补终端摘要、恢复输入），也不重读任何一份配置文件：等待期间改了操作者配置或项目配置，本进程照旧用启动时加载的有效配置，改动由下一次 `resume` 按通用规则接受或拒绝。核对通过后回到调度循环开头，重跑全部调度门禁，下一步照常推导：worker 失败之后是一次新的指挥决定，指挥或审阅失败之后是同一种调用的新请求（新进程、新 dispatch，不带 `retry_of`），不是重试同一个调用。
- **取消**：中断这个进程（Ctrl-C 或 SIGTERM），台账记 `interrupted/signal`（退出码 130），`quota_wait` 与中断同次删除；之后普通 `resume` 或 `abandon`。等待中改小或关闭上限只能这样：中断后带新配置 `resume`，新值只影响之后的额度失败。
- **主进程被强杀**：台账留着 `running` 与 `quota_wait`，读法与被强杀的 `running` 相同——记录不证明进程存活，不会自动继续。普通 `resume` 进入执行时删除它、照常推导下一步，不续等；`abandon` 同样删除它。
- **有界**：每次等待之前都有一次计入 `max_agent_dispatches` 的失败调用，并且要求 `agent_dispatches` 仍小于上限；所以等待次数受派发预算约束，但累计等待时长不受单次上限约束（默认 200 次 × 6 小时）。

保证与不保证见设计的不变量 22 与[额度探测、统计与等待](design.md#额度探测统计与等待)。

### 进程退出确认

`--confirm-exited DISPATCH_ID` 用于 `resume`、`abandon`、`cleanup-run`，可重复。运行时在运行锁内校验全部 ID，先写审计事件（含当时的进程组探测与组长身份），再只更新指定记录；命令子进程用父 dispatch ID 确认。单项确认绑定子命令 ID 与 `process.json` 摘要；`process.json` 缺失或因文件类型、读取期间被替换等原因被拒时，显式保存 `journal_digest: null`，它只匹配同一子命令当前仍无摘要的记录。本地 I/O 错误直接停止。子命令仍无退出证据而确认缺失或摘要发生变化时，须重新核实。确认不终止进程、不放宽 HEAD、快照、Git 绑定或清理门禁，也不证明调用成功或没有结果。

### 放弃与清理

- `abandon RUN` 把未完成的 run 标为 `abandoned`，保留分支、工作树和证据，之后不能 resume。它先经过 `load_run`：台账恢复链接损坏或加载时核验的证据被删改时拒绝放弃，保留现场供核对；路径索引与输入索引不在加载时核验，它们损坏时仍可放弃。放弃不删除文件；要移除工作树，之后执行 `cleanup-run RUN --remove`。
- `cleanup-run RUN` 是唯一的显式清理入口（验证通过的检出另由运行时在整体审阅前尝试回收，见[整体验证与重验](#整体验证与重验)），默认只读预览，加 `--remove` 才逐项删除。它不加载配置、不调用模型，只支持当前台账版本，不改 run 状态，不删除分支、台账或运行证据。
  - 共同门禁：在运行锁内核对台账版本与归属（run ID、仓库、工作树路径、分支名、Git 公共目录与管理目录绑定）、命令目录、路径索引与输入索引、worker 回执链接，以及全部派发与命令子进程的退出证据。任一项不通过时整条命令报错，不处理任何资源。
  - 全新验证检出：run 处于任何状态都可清理。删除以台账 `verification_checkouts` 中对应尝试和轮次的创建登记为准，核对路径、三处目录身份、Git 绑定及已有清理记录，已发布的验证还核对派发记录里的检出路径。`checkout.json` 供报告和诊断；只有该文件而没有创建登记时报告 `blocked`，被引用的路径不动。已跟踪修改、未被忽略的未跟踪文件、`assume-unchanged`／`skip-worktree` 标记阻止清理，错误列出前 10 个阻塞路径；被 Git 忽略的文件（含被忽略目录里的嵌套仓库）随检出删除，不需要选项——需要保留诊断产物时先另存。目录已缺失而 Git 注册仍在时，只注销该精确路径，不做全仓库 prune。记录写在检出证据旁的 `cleanup.json`，`by` 是这份记录的写入者（`runtime` 或 `cleanup-run`）。
  - 分支工作树：只在 `completed` 或 `abandoned` 时清理，其他状态下按规则保留（`retained`）。要求原分支仍指向检查点，HEAD 与 Git 绑定未变，没有未被忽略的未跟踪文件、`.harness` 目录或隐藏索引标记，已跟踪文件的字节与 HEAD 一致。记录写在 `worktree-cleanup.json`。`--remove --remove-input-copies` 另删除登记且未修改的依赖副本；登记的副本仍在时，`--remove` 报 `blocked`，`--remove-ignored` 不能代替 `--remove-input-copies`。
  - 工作树里的忽略文件：不带 `--remove-ignored` 时，有任何被 Git 忽略的条目即 `blocked`，`error` 列出目录折叠后的前 10 条并提示选项。`cleanup-run RUN --remove-ignored` 预览：工作树条目带 `ignored_paths`（`{coverage, entries, entry_count, omitted_count}`，目录折叠、不计空目录，最多 100 条与 8 KiB，其余只计数；被忽略目录里的内容包括嵌套仓库不显示）。`--remove --remove-ignored` 在写意图之前重新列出清单，意图与完成记录写明 `remove_ignored` 与这份清单，然后由 `git worktree remove` 连同忽略文件删除。只有本次命令的选项能授权：意图里存的允许不延续到之后不带选项的命令（工作树还在时照样 `blocked`）；工作树已被删、只差完成记录时，补写的记录保留原意图的 `remove_ignored` 与清单。预览与删除之间清单可能变化。
  - “忽略”按 Harness 调用的 Git 判定：工作树里的 `.gitignore`、公共 Git 目录的 `info/exclude`（含运行时加入的 `/.harness/`）以及 `core.excludesFile`（未设置时为 `$XDG_CONFIG_HOME/git/ignore` 或 `~/.config/git/ignore`）。Harness 调用 Git 时去掉全部 `GIT_*` 环境变量（含 `GIT_CONFIG_GLOBAL`），以 `--remove-ignored` 预览列出的清单为准，不以自己 shell 里的 `git status` 为准。
  - 每个资源先保存删除意图，再执行 Git 删除，完成后记为 `removed`。删除后记账前中断时，重跑同一命令按已存意图续做，已经删掉的资源不再执行删除。工作树的记录绑定 run、工作树路径、分支名、检查点和 Git 绑定，不绑定整份台账，所以中断后用 `--confirm-exited` 补的退出确认不妨碍续做。
  - 工作树记为 `removed` 之后，操作者删除或移动分支、或之后的 `--confirm-exited` 改写台账，都不会改变结果：再次运行仍报告 `removed`（预览为 `absent`），只有路径、Git 注册或管理目录重新出现才报 `blocked`。分支核对只在工作树仍存在、即将删除时进行。
  - 输出逐项列出每个资源的 `status`：
    - `eligible`：预览时可以删除。
    - `removed`：已删除，完成记录已写入。
    - `absent`：预览时资源已不存在（有删除记录，或检出的目录、注册和管理目录都已消失）；加 `--remove` 时补写完成记录，报告 `removed`。此前已删除的检出带 `removed_by`（完成记录的写入者，`runtime` 表示运行时回收）。
    - `stale_registration`：仅验证检出，目录已缺失而 Git 注册仍在；`--remove` 只注销该精确路径。
    - `input_copies_present`：仅工作树的预览，有登记的依赖副本，需 `--remove --remove-input-copies`。
    - `retained`：仅工作树，run 还没有 `completed` 或 `abandoned`，按规则保留，`reason` 写明当前状态。`reason_kind` 为 `finish_or_abandon_first`：先完成或 `abandon` 该 run 再清理。
    - `blocked`：资源本可清理，但门禁挡住了删除或预览；`error` 写明原因和阻塞路径，资源原样保留。被挡住的检出条目没有 `worktree`，改给 `recorded_worktree`：来自创建登记；无登记时取 `checkout.json` 的报告路径（读不出时为 null）。此字段只供诊断。
  - 某个资源受阻不影响其余资源。只有出现 `blocked` 时退出码为 1；只有 `retained` 的工作树时退出码为 0。
- `prepare-run RUN --manifest FILE` 只在 `paused` 或 `awaiting_approval` 且没有待处理阶段时准备依赖副本，格式见[依赖副本清单](operations.md#依赖副本清单)。

### 证据损坏

`load_run` 在 `resume` 和 `abandon` 做任何事之前核验以下证据的路径、类型与 SHA256：worker 派发的 `packet.json` 与 `schema.json`（`packet.json` 同时是该 worker 必需问题的键对照）；`attempt_exemption` 引用的原始 worker 结果或失败原件（供应商豁免引用 `process.stdout`）；worker 判定记录 `worker_return_evidence`（版本 3）绑定的五个来源（存在时记录整份 SHA256，缺失时为 null；重新推导判定与供应商分类）；`decisions/` 下的请求与答复；检查点 manifest 与批次文件；`continuations/`、`continuation-rejections/`、`execution-guidance/` 下的完整产物。其中任一缺失、被改或换成符号链接时，`resume` 与 `abandon` 都在写入前拒绝。`load_run` 另按命令的形状重新校验台账里保存的命令（`config_snapshot` 的 `verification.commands`、`verification.setup_commands`、`permissions.worker_commands`，`tasks` 与 `original_tasks` 各任务的 `required_commands`、`self_checks`），不合格时同样拒绝（`evidence/load_integrity`）。出口只有两个：把原字节恢复回去后照常 `resume` 或 `abandon`；或者保留 run 目录与工作树原样，另开新 run。`status`、`watch`、`dashboard` 不经过这些核验，仍可查看。

路径索引（`path-indexes/`）、输入索引（`input-indexes/`）和环境更新产物（`environment/`）在 `resume` 预检、派发或接受前核验，不在加载时核验：它们损坏会阻止继续，但不阻止 `abandon`。

提交消息的来源在用到时核验：构造任务审阅包前，`pending_task.worker_result` 与 worker 原件不同即停止“worker 结果台账与原件不同”，不派发审阅。首次构建或重建接受意图时，`pending_acceptance.worker_source` 必须与批准审阅绑定的来源、工作尝试和快照一致；按摘要读取原件的 `commit_message` 之前或途中发现来源错误即停止，保留 `pending_acceptance`。修复原始证据后普通 `resume`，或在加载门禁通过时 `abandon`。续做有效的已保存意图使用绑定的提交对象。

已发布的计划文件（`plans/revision-N.json`，`plan_history` 的每一行）同样不在加载时核验。对全部版本的复核（`Planner.verify_plan_authority`）在四处：`resume` 的任何写入之前（含 `--confirm-exited` 的审计写入，因此也先于批准计划和重放 worker 回执）；额度等待醒来之后、回到调度循环之前（醒来后的前提核对与 `resume` 调用同一个函数，见[额度等待](#额度等待)）；每次推导下一步之前；标记完成之前。每一处都逐个核对全部版本存在、摘要与台账一致、修订号相符、`tasks` 是对象数组且各任务的命令形状合格，并要求最新版本的任务表等于 `tasks`、`revision-0` 的任务表等于 `original_tasks`。另外，待派发指令来自修订时，worker 派发在本地准备之后、扣预算之前由 `Planner.check_directive` 单独复核指令引用的那一个修订：文件按摘要读出，其中的指令与任务表要等于台账里的；文件缺失或被改时停在 `evidence/runtime_integrity`，内容不符时停在 `workspace_mismatch/directive_mismatch`，都不启动 worker、不扣预算。台账副本（`tasks`、`original_tasks`）里的命令不合格时加载先拒绝（`evidence/load_integrity`，`abandon` 同样被拒）；只在计划文件里不合格时（例如台账没有副本的中间版本）停在 `evidence/runtime_integrity`，`abandon` 仍可用。缺失、被改或不一致即停止，不调用模型；恢复原字节后普通 `resume`，无法恢复时 `abandon`（`abandon` 与 `cleanup-run` 不需要计划文件，它们自己的 `--confirm-exited` 照常可用）。不覆盖的情形：两次复核之间被删改的计划文件要到之后的复核才发现。离线实测的例子：单任务计划在 worker 调用期间被改了 `revision-0` 的字节，这个任务照常完成验证、审阅与接受，接受之后的下一次推导下一步才停在 `evidence/runtime_integrity`；`prepare-run` 与 `status` 等只读入口不核对；run 未到终态时，之后的 `resume` 在预检里遇到文件仍然缺失或不符即拒绝。每次核对的耗时随计划历史的总大小增长：实测每版约 40 KB 时，200 版约 19 ms，1,000 版约 102 ms。

### 终态与其他版本的台账

`completed` run 的普通 `resume` 返回保存结果，不派发；修改选项被拒绝。

只支持台账版本 20。其他版本（含缺失或非整数的版本）的台账不迁移、不补默认值、不解释字段：`resume`、`abandon`、`prepare-run`、`cleanup-run`、`status RUN`（含 `--full`）、`watch`、`permission-diagnostics` 与观察网页的 run 详情都拒绝，worker 的命令 broker 与只读 MCP 读到这样的台账也拒绝，错误写明该 run 的台账版本与当前版本（`台账版本 N 不支持（当前版本 20）`；不是对象或版本缺失、无效时为“台账版本缺失或无效，不支持（当前版本 20）”），退出码 2（`cleanup-run` 仍为 1），台账、工作树与检出都不改动。运行列表（不带 run 的 `status`、观察网页的列表）对这些目录只给出路径、版本和“不支持”；损坏或旧版本的目录不影响其他 run 的列出。原始 JSON 用文件工具查看。

旧版本 run 的工作树与验证检出不由当前版本清理：用对应版本的 Harness 清理，或人工核对后执行 `git worktree remove`。升级前应完成或放弃进行中的旧 run。

## 接受过程中断

`pending_acceptance.intent` 保存后，分支仍在原父提交、已移动到意图提交但真实暂存区尚未同步、或两者都已更新，均可普通 `resume`。通过预检后，运行时只补缺少的分支更新、暂存区同步和台账登记，这几个步骤沿用原批准。分支仍在父提交、未发布的树或提交对象缺失时，重新核对原件、候选和批准，通过后重建意图。构建或重建时发现候选快照变化，旧批准失效，任务需重新执行、验证和审阅。

分支偏离意图的两个提交、真实暂存区不符同步前后任一摘要、或 Git 对象与意图不符时，恢复保留现场并停止。可以用 `git -c trailer.separators=: log --format='%H %(trailers:key=Harness-Task,valueonly)'` 核对提交，并用 `git show --no-patch <commit>` 查阅 trailer。原生 Git 若留下引用或暂存区的锁文件，运行时不会移除它们。

## 不能自动恢复的窗口

以下中断窗口需要操作者判断；运行时停止并保留现场，不提供改写台账的捷径。

1. **worker 已有能解码成对象的原始输出、封存之前中断**：不能把恢复时的工作树补绑为原候选，也不自动重派；返回证据互相冲突、超过读取上限或字节无法严格解码（判为 unknown）同样阻塞，`--confirm-exited` 不能解开（读文件时的系统错误不形成判定，见出口表的 `runtime-io`）。保留证据后 `abandon`。对象不合契约时也停在这里，而不中断时它会作为无效输出进入下一次尝试。输出解不出对象（截断、非 JSON、重复键、不是对象）不在此列：无论是否中断，都作为无效输出进入下一次尝试。
2. **调用前台账保存失败**：可能留下已扣预算、`invoked=true` 的条目；恢复按原件判定（没有输出时可重试），多计一次尝试。
3. **worker 调用失败、判定保存之前原件被改**：判定之前没有可比对的记录，恢复按改后的字节判定（包括额度类豁免）；判定保存之后再改，加载核验拒绝 `resume` 与 `abandon`。能写运行目录的人也能直接提高 `max_attempts`。
4. **派发 `starting` 后、pgid 落盘前中断**：缺 pgid 的记录不能证明未启动，需核实后 `--confirm-exited`。
5. **启动时 `worktree_binding` 保存前中断**：没有可信绑定，只能 `abandon` 后重新运行，并手工清理已创建的工作树和分支。
6. **`delete_path` 删除之后**：删除立即生效，接受门禁只决定候选是否被接受；需要恢复文件时从 Git 历史中取回。
7. **`prepare-run` 在保存临时文件身份之前中断**：不能凭相同字节接管文件，需人工核对。
8. **`command-staging/` 中的准备目录**：发布前失败或被杀时留下；运行时不枚举它们，也不回收，反复失败会累积磁盘占用。
9. **指挥或审阅的供应商输出不可解码、判定保存之前中断**：回执里没有原件，恢复按无返回处理，发起没有 `retry_of` 的调用；与不中断相比最多多一次调用。
10. **额度等待中主进程被杀**：不会自动继续；台账留着 `running` 与 `quota_wait`（不证明进程存活），普通 `resume` 进入执行时删除记录并照常推导下一步，不续等。
