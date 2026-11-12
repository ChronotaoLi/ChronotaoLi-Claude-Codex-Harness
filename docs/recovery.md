# 恢复与操作者出口

本文写给处理停止 run 的操作者：看到哪种停止信息，该做什么，用哪个命令。它只适用于台账版本 17 的 run。保存顺序、子状态的设置与清除见[设计：状态转换](design.md#状态转换)，日常命令见 [README](../README.md)。

- [通用规则](#通用规则)
- [恢复选项与互斥](#恢复选项与互斥)
- [阻塞状态与操作者出口](#阻塞状态与操作者出口)
- [恢复步骤](#恢复步骤)
- [不能自动恢复的窗口](#不能自动恢复的窗口)

## 通用规则

- 先看 `status RUN`：它给出状态、错误首行、预算（`budgets`、`budget_explanation`、`remaining_model_calls`）、worker 结局计数 `worker_outcomes`、待答决定 `pending_decisions`、续作 `continuation`、缺失命令 `missing_command_requirements`、最近环境阻断 `last_environment_blocker`、返工提示 `convergence` 和审阅轮次 `review_rounds`。`status RUN --full` 输出完整台账。
- 每个出口都沿用同一 run 的累计预算、证据和接受条件。读得出的拒绝、停止和阻断不会被丢弃；读不出否决的无效输出不是结果，由新调用重做（见[无效输出与自动重做](#无效输出与自动重做)）。同一快照上重复 `resume` 只会得到同样的停止，或消耗新的调用。
- 出口可以是继续，也可以是保留现场后 `abandon`。`abandon` 与 `resume` 一样先经过 `load_run`：加载时核验的证据（见[证据损坏](#证据损坏)）被删改时，两者都被拒绝。
- `resume` 使用与 `run` 相同的 `--config`。恢复时只能提高 `agent_timeout_seconds`、`verify_timeout_seconds`、`context_chars`、`claude_max_turns`（及各角色实际生效的 `claude_max_turns_by_role`）、`max_attempts`、`max_agent_dispatches`，或在 `permissions.worker_commands` 追加精确 argv；其他配置、模型和共享资产必须与创建时一致。
- 恢复先确认旧进程退出、核对 Git 绑定与 HEAD，再做其他事；这些检查失败时不写入任何恢复输入。
- 外层自动化会话的权限边界见[外层 agent 操作说明](operations.md#外层-agent-操作说明)：新增权限、预算或需求选择要向用户请求。

## 恢复选项与互斥

| 选项 | 用途 |
| --- | --- |
| `--approve-plan` | 批准等待确认的计划 |
| `--step` | 最多接受一个任务后暂停 |
| `--confirm-exited DISPATCH_ID` | 记录操作者核实的退出确认，可重复 |
| `--environment-update FILE` | 登记环境事实与只读检查 |
| `--operator-decision FILE` | 回答决定请求，可重复，整批校验 |
| `--execution-guidance FILE` | 登记绑定任务与候选的执行约束 |
| `--reject-continuation FILE` | 拒绝当前续作请求，交给同一任务的新 worker 修正 |
| `--allow-scope PATTERN` | 为之后的修订追加可选范围，可重复；run 未完成、未放弃且没有已返回而未落地的指挥决定时可用 |
| `--reverify` | 本次恢复首次进入整体验收时重验失败快照；已返回的指挥决定存在时拒绝 |
| `--operation-id ID` | 指定操作记录 ID |

互斥规则（违反时在任何写入前拒绝）：

- `--reject-continuation`、`--execution-guidance` 不能与其他修改选项合用（`--confirm-exited`、`--step`、`--approve-plan` 除外）。
- `--operator-decision` 不能与 `--environment-update`、`--reverify`、`--allow-scope` 合用。
- `--environment-update` 不能与 `--reverify`、`--allow-scope` 合用。
- 已完成的 run 拒绝 `--confirm-exited`、`--allow-scope`、`--environment-update`、`--operator-decision`、`--execution-guidance`、`--reject-continuation`；普通 `resume` 返回保存结果。已放弃的 run 拒绝 `resume`。

环境更新、操作者决定、执行指导和续作拒绝只在安全边界接受：以下待处理键都为空——`pending_task`、`pending_acceptance`、`pending_post_accept`、`pending_revision`、`pending_directive`、`worker_receipts`、`review_receipts`、`commander_receipts`。

## 阻塞状态与操作者出口

“停止信息”是 `status RUN` 的 `error` 或 stderr 中的错误首行（节选）。

| 停止信息或状态 | 操作者该做什么 | 命令 |
| --- | --- | --- |
| `--plan` 文件被拒绝（此时没有创建 run） | 修正计划文件 | 重新 `run` |
| `计划等待明确批准` | 用 `status RUN --full` 审阅保存的计划 | `resume RUN --approve-plan` |
| `任务所需命令尚未授权`，或 `missing_command_requirements` 非空 | 在 `permissions.worker_commands` 追加精确 argv；`git_query` 形式表内的 Git 查询无需授权 | 普通 `resume RUN` |
| `等待续作条件：{'kind': 'commands', …}` | 授权缺少的命令，或认为方案需修正时拒绝续作 | 普通 `resume`；或 `resume RUN --reject-continuation FILE` |
| `等待续作条件：{'kind': 'budget', …}` | 把 `agent_timeout_seconds` 提高到请求下限，或拒绝续作 | 同上 |
| `worker 环境受阻：…（已截断…）。完整 worker 结果：PATH（SHA256 …）` | 读完整结果；修复环境；命令被拒时追加精确 argv | 普通 `resume`，或 `resume RUN --environment-update FILE` |
| `环境前置检查未通过` | 让检查条件成立（本次没有启动 worker、没有扣尝试） | 普通 `resume` |
| `等待操作者决定：task_id=… request_id=…` | 从 `pending_decisions` 读问题与来源，做出选择；指挥提问（种类 `commander`）也是这一行，未答或答 stop 时不调度任何模型 | `resume RUN --operator-decision FILE` |
| 提交多个决定时中断，之后出现 `immutable decision artifact conflict` | 用原来的文件、按原来的顺序重新提交 | `resume RUN --operator-decision FILE1 --operator-decision FILE2 …`（同一顺序） |
| `操作者已决定停止：request_id=…` | 要继续就对同一请求提交新的 continue | `resume RUN --operator-decision FILE` |
| `指挥修订越出已批准的文件范围：[…]` | 核对模式后授权，或不授权让指挥重新决定；两种情况下一次都是新的指挥决定，旧修订不复用 | `resume RUN --allow-scope 'PATTERN'`，或普通 `resume` |
| `… 已达尝试次数上限（runtime.max_attempts 当前值 N）` | 先看 `convergence` 与审阅轮次判断是否值得继续；必要时提高 `runtime.max_attempts` | 普通 `resume` |
| `已达 run 模型派发次数上限（已用 N，上限 M）；如果之后全部一次通过，至少还需 K 次模型调用（下界…）` | 按至少 K 次的余量提高 `max_agent_dispatches`；K 不含指挥决定、返工与重做 | 普通 `resume` |
| `任务必要上下文 N 字符超过预算 M` | 提高 `context_chars`，或让指挥修订拆小任务；看 dispatch 的 `context-report.json` | 普通 `resume` |
| worker 超时或轮数用尽（计入一次工作尝试） | 核对候选进展，再决定提高 `agent_timeout_seconds` 或 `claude_max_turns_by_role` | 普通 `resume` |
| `整体验证超时` | 修复环境或提高 `verify_timeout_seconds` | 普通 `resume`（同一快照重验） |
| `旧 dispatch … 的进程组仍存在` 或 `无法确认旧 dispatch … 已退出` | 核实该进程及其命令子进程已退出，不终止无关进程 | `resume RUN --confirm-exited ID`；终态 run 用 `cleanup-run` 的同名选项 |
| `Cannot confirm command process exited: …` | 同上；用父 dispatch ID 确认 | 同上 |
| worker 调用失败，退出已确认且原始证据判为可重试 | 修复原因 | 普通 `resume`（新的计费尝试） |
| `worker return interrupted before immutable seal; …; automatic retry forbidden` | 封存之前中断，输出能解码成对象结果（returned），或证据冲突、超过读取上限、读不出文件（unknown）；解不出对象的输出不会停在这里 | `abandon RUN` |
| worker 输出无效（结构、语义、界限、交接目标非法，或解不出对象结果） | 不停止：计一次工作尝试、回执结局 `result_rejected`，下一次尝试经指挥选择；只在同一任务达到 `max_attempts` 时以尝试上限停止 | 提高 `runtime.max_attempts` 后普通 `resume`，或 `abandon` |
| worker 的 blocked 输出无效（条目 `invalid_blocked`，反馈带 `output_error`） | 结构合法的 environment／operator 阻断按对应行处理（不给尝试豁免）；task 阻断与结构不可信的阻断交给指挥决定 | 对应出口 |
| `… 请先核对并清理越界修改，再 resume` | 核对并清理范围外改动，不自动删除 | 普通 `resume` |
| `<role> 输出连续两次无效：…；原件 PATH；普通 resume 重新请求，或 abandon` | 读原件与错误（派发条目的 `error`、`invalid_output`）；计划超出整体审阅的必要上下文同样是无效输出 | 普通 `resume`（新请求，最多再两次调用），或 `abandon RUN` |
| `无效输出原始结果缺失或无法读取`、`无效输出原始结果已改变` | 上一次无效输出的原件被删改（准备前，或本地准备与 `pre_agent` 期间），调用计费前停止 | 恢复原字节后普通 `resume`，或 `abandon RUN` |
| `worktree HEAD 与检查点不同` | 核对是否有含 `Harness-Run`／`Harness-Task` 的提交（见下一节第 1 条；读取命令见 [README](../README.md#对自己的项目执行任务)）；不手工伪造接受 | 恢复可信现场后 `resume`，或 `abandon` |
| 加载时核验的证据被删改（`worker packet artifact changed`、`worker schema artifact changed`、`evidence artifact disagrees with ledger` 等，见[证据损坏](#证据损坏)） | 恢复原字节；不改台账摘要。`abandon` 同样被拒绝 | 恢复后普通 `resume`；无法恢复时保留 run 目录原样，另开新 run |
| 路径索引、输入索引或环境更新产物损坏（`path-indexes/`、`input-indexes/`、`environment/`） | 恢复原字节 | 恢复后普通 `resume`；无法恢复时可 `abandon RUN` |
| `待派发指令的代码快照已变化`、`修订保留候选的代码或暂存区已变化`、`待派发指令与已发布计划或候选不符`、`pending continuation candidate or task changed` | 恢复原快照或保留证据 | 普通 `resume` 或 `abandon` |
| `计划的整体审阅必要上下文 N 字符超过预算 M`（供给计划） | 提高 `context_chars`；模型给出的过大计划不停在这里，按无效输出重做 | 普通 `resume` |
| `应用修订前 HEAD 与已校验计划不符`、`待应用的计划文件缺失或无法读取`、`…已改变`、`已发布的计划文件（revision-N）缺失或无法读取`、`…已改变`、`台账任务表与已发布的计划文件不符` | 恢复原 HEAD 或计划文件的原字节 | 普通 `resume`，或 `abandon RUN` |
| `共享规则、skills、MCP 或 hooks 已改变` | 恢复原版本的共享资产 | 普通 `resume` |
| `配置已改变：…` | 恢复不可放宽的配置值 | 普通 `resume` |
| `缺少已保存的 worktree Git 绑定` | 启动在绑定保存前中断；核对后手工 `git worktree remove` 并删除 `harness/<run>` 分支 | `abandon RUN` 后重新 `run` |
| `旧 run 仅保留为历史证据（台账版本 N，当前 17）` | 查看或清理，开始新 run | `status`、`dashboard`、`cleanup-run` |
| `已有进程持有运行锁` | 等另一个 `run`／`resume`／`abandon` 结束 | 稍后重试 |

## 恢复步骤

### 计划批准与分步

`run "目标" --pause-after-plan` 保存计划后进入 `awaiting_approval`。普通 `resume` 不会批准；`resume RUN --approve-plan` 批准后继续，可与 `--step` 合用。规划阶段的指挥提问先于计划批准，`--approve-plan` 不回答提问。`--step` 最多接受一个任务后进入 `paused`，最后一个任务之后仍执行整体验收。批准不授予命令权限。

### 命令授权与前置命令

任务的 `required_commands` 与 `self_checks` 是前置条件。派发前与 `verification.commands`、`permissions.worker_commands` 精确比较，参数、空参数、字面 `*` 和换行都原样比较，不接受前缀；`verification.setup_commands` 不授予 worker 能力。能被 `git_query` 解析的形式表内 argv 算作已满足（只表示能力存在，worker 要用该工具执行，不能用 Bash）。缺少授权的任务不启动 worker、不扣尝试，其他就绪任务照常继续；全部就绪任务都缺命令时停止。追加精确 argv 后普通 `resume` 即可重检，无需环境更新。

### 续作与操作指导

worker 需要新的精确命令或更长的单次时限时，返回 `blocker_kind=continuation` 和续作请求：

```json
{"kind":"commands","commands":[["python3","-m","unittest","tests.test_export","-v"]],"minimum_timeout_seconds":0,"reason":"聚焦检查需要此精确命令"}
```

时限请求用 `kind=budget`、`commands=[]` 和正的 `minimum_timeout_seconds`。请求不授权、不执行命令。普通 `resume` 在请求的命令已进入 `verification.commands`／`permissions.worker_commands`（或属于 `git_query` 形式表）、或 `agent_timeout_seconds` 已达下限时，以新派发继续原候选；条件未满足时停止并列出缺口。续作不重置任何预算。

请求本身需要先修正时，用 `resume RUN --reject-continuation FILE` 拒绝它。输入是最多 64 KiB 的严格 JSON，恰好五个字段；前四项取自 `status RUN` 的 `continuation`：

```json
{"continuation_id":"continuations_<sha256>","task_id":"task_id_from_status","source_dispatch_id":"worker_dispatch_id","source_snapshot_digest":"64_lowercase_hex_characters","reason":"旧命令绑定的脚本需要先修正；修改后重新申请精确命令。"}
```

`reason` 非空、最多 4,000 字符；其余字段最多 120 字符，摘要是 64 位小写十六进制。只用于 `blocked`、`interrupted` 或 `paused` 的当前未接受任务，要求来源结果、请求、候选、Git 绑定与 HEAD 一致，旧派发和命令都已退出，且来源之后没有模型调用。下一步直接派发同一任务的新 worker，不先调用指挥；拒绝已保存而 worker 尚未调用时，普通 `resume` 继续同一修正，相同文件可幂等重试。候选改变时停止。

`resume RUN --execution-guidance FILE` 登记一条执行约束，输入最多 64 KiB，恰好四个字段：

```json
{"task_id":"task_id_from_status","source_dispatch_id":"worker_dispatch_id","source_snapshot_digest":"64_lowercase_hex_characters","summary":"必须遵守的具体执行约束"}
```

`summary` 非空、最多 4,000 字符。来源必须是已有权威绑定的 worker 结果（续作记录或当前反馈），之后没有模型调用，当前候选仍匹配。指导按任务进入后续输入并标明是否适用当前候选；它不授予命令、不改变目标或验收、不增加预算。

### 环境更新

worker 因环境受阻后，用结构化更新说明已修复的环境事实：

```bash
python3 -m harness --config /path/to/harness.toml --repo /path/to/project resume RUN --environment-update /absolute/path/update.json --step
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

输入最多 64 KiB，每个 run 最多 32 条。`summary` 非空、最多 4,000 字符；`checks` 可省略，最多 16 项，只允许 `path`（绝对路径，`path_type` 为 `file`、`directory` 或 `any`）、`executable`（名称或绝对路径）和 `worker_command`（精确 argv：已配置或属于 `git_query` 形式表即通过）。检查不执行命令、不启动子进程、不授权。只接受当前未接受任务的 worker 环境阻断，且来源之后没有模型调用（中断导致的 `interrupted` 不使来源失效）。之后同任务的每次 worker 尝试前都重检最新一条更新；失败时不启动 worker、不扣尝试或派发预算。`status` 的 `last_environment_blocker.stale=true` 表示来源已被后续调用取代。

### 操作者决定

worker 返回 `blocker_kind=operator`、指挥提问，或已接受任务为下游任务留下 `decision_requests` 时，`status RUN` 的 `pending_decisions` 列出请求。回答：

```bash
python3 -m harness --config /path/to/harness.toml --repo /path/to/project resume RUN --operator-decision /absolute/path/decision.json --step
```

```json
{"schema_version": 1, "request_id": "request_id_from_status", "task_id": "target_task", "decision": "continue", "summary": "操作者的明确选择及理由"}
```

输入最多 64 KiB、只含上述字段，`summary` 非空、最多 4,000 字符，`decision` 为 `continue` 或 `stop`。可重复指定多个文件：整批先校验，重复 ID 或任一非法、过期答复使整批拒绝；通过后全部答复文件先写入 `decisions/`，再一次保存。每条答复的序号按参数顺序分配并写进它的不可变文件，所以预写中途被中断后，必须用原来的文件、按原来的顺序重试：已写出的文件会被复用；换了顺序，序号与已写文件不一致，恢复以 `immutable decision artifact conflict` 停止，按原顺序重交即可继续。任一 `stop` 使本次恢复保存整批后停止，不派发模型。下游请求的答复在目标任务的下一次模型调用前可修订；worker 阻断与指挥提问的答复在任何后续模型调用后过期；指挥提问的回答作为 `operator_answer` 进入下一次指挥决定。决定不改变目标、范围、命令授权、验收或预算。问题文本空白、含 NUL 或过长时，请求里是有界说明与转义摘录，必须读 `source` 绑定的原文后再答。

### 指挥提问、修订与范围

指挥的 `ask` 登记一条种类 `commander` 的决定请求并停止（有当前任务时目标是它，否则是 `final`）。这条请求未答或答 stop 时不调度任何模型，与目标无关；用 `--operator-decision` 回答 continue 后，下一次指挥决定带上 `operator_answer`。回答不授予范围、不降低验收，也不增加预算。规划、任务和整体三个阶段都可能提问。

提问的识别只要求公共前提（退出已确认、原件已绑定摘要、严格解码成对象、envelope 身份相符），不要求调用期间候选代码与 HEAD 未变：变了也照常登记，派发条目记 `candidate_changed` 并写事件 `commander_candidate_changed`，停止信息提示先核对工作树；登记不认可这些改动，之后的派发、验证与接受各自核对快照与 HEAD。其他决定（implement、revise）在候选或 HEAD 变化时不判定，停止并保留回执，恢复原状后普通 `resume`。合格的提问先运行 `post_agent`，再在一次保存里登记请求并消费回执；hook 失败时回执保留、请求尚未登记，修复 hook 后普通 `resume` 对同一原件重跑 hook（日志目录按次递增）再登记。已返回而未登记的提问（登记前中断或 hook 失败）在普通 `resume` 的 HEAD 门禁之前完成 hook 与登记，HEAD 或候选已变也一样；之后的回答登记与执行仍要先过 HEAD 门禁，HEAD 不同时须先核对并恢复。不合格的提问不运行 hook，直接登记。

修订越出已批准范围、其余全部合格时，派发条目记为无效（`out_of_scope_paths` 列出越界模式），回执消费后停止，不自动重做。`--allow-scope PATTERN` 在 run 未完成、未放弃、没有已返回而未落地的指挥决定时可用：先校验整批模式，再写审计事件并保存去重后的 `approved_scope`，然后照常继续；它只扩大之后的修订可选的范围，不修改既有任务。授权与否，之后的普通 `resume` 都是新的指挥决定，旧修订不复用。

修订只能保留既有任务 ID、追加前置任务、细化未接受任务的说明和依赖；已接受任务不变。修订先写成不可变的 `plans/revision-N.json`，与回执消费同次登记引用；发布前按摘要复核文件、读回原件重放修订、核对 HEAD。只改当前任务说明且仍选它时保留候选，交给同一任务的新 worker；其他修订先把未接受修改归档到 `recovery/revision-N`，再恢复工作树到已接受提交，然后计划版本、`tasks`、`current_task` 与下一条指令同次发布。归档与重置之间任一点被中断，普通 `resume` 都从计划文件重来，无需操作者介入。

### 验收不可达

当前范围或已答决定排除了满足某条验收所需的工作时，worker 应返回 `blocked/task` 指明验收项和边界，指挥可修订，无可行修订时提问。`continue` 不豁免验收，增加预算也不是解决不可达条件的证据。操作者可回答 stop，核对已接受提交后从 run 分支手动集成需要的部分，再 `abandon RUN`；未接受的候选不算已交付，run 不会标为 completed。

### 无效输出与自动重做

没有通过契约的模型输出不是结果。运行时只从原件读否决：退出已确认、原件字节已绑定摘要、严格解码成对象（拒绝重复键与非有限数，含溢出的数值字面量）、envelope 身份相符之后，审阅的 `request_changes`、blocking finding 或未 `resolved` 的必需项（核心结构合法时），指挥的 `ask`（`action` 为 ask 即可），以及 worker 的 `blocked`，都保持约束力。

- **指挥与审阅**：读不出否决的无效输出（含结构非法的审阅、不可解码的供应商输出）在同一次保存里记为 `status=invalid`、错误与原件摘要（`invalid_output`），回执消费，然后自动重做一次。重做是新派发，计入 `max_agent_dispatches`，条目记 `retry_of`，包里带 `previous_invalid`（上一次无效输出的引用与前 2,000 字符的错误；带上它会超出上下文预算时不带，条目记 `previous_invalid_omitted`）。重做仍无效即停止；之后的普通 `resume` 发起新请求，同样有一次重做。是否重做只看整个 run 的最后一次模型调用，所以中断、或重做在本地准备时（额度、上下文、hook）停止，恢复后的那次调用仍是重做。
- **带否决的无效输出**：审阅按拒绝返工（`invalid_rejected`），它的 blocking finding 成为下一轮必需项；指挥的 ask 照样登记提问并停止（`invalid_asked`，条目记错误）；worker 的 blocked 见上表。整体审阅的拒绝写入整体记录后同一快照不再审阅；无否决的无效整体审阅不写结论，同一快照可以重做。
- **worker**：无效输出（含解不出对象的完整输出）计一次工作尝试，回执消费，调度继续，受 `max_attempts` 约束。
- **可选产物**：worker 的 `checkpoints`、`proposed_memories` 与审阅的 `approved_memory_keys` 不合格只丢弃自身，记 `checkpoints_discarded`、`memory_proposals_discarded`，结果照常处理；之后的上下文只带它们经过校验的投影（登记后的检查点、整批合法时的提案 key，不带记忆批准），原件保留原值。worker 的 `decision_requests` 不合格则整份结果无效。
- **只因越出已批准范围而无效的修订**：记为无效并停止，不自动重做（见上节）。

### 整体验证与重验

全部任务接受后，整体验证失败时：全部失败项都是超时则停在当前尝试，修复环境或提高 `verify_timeout_seconds` 后普通 `resume` 在同一快照重验；普通非零退出交给指挥决定，同一快照重验须显式 `--reverify`。`--reverify` 只在本次恢复首次进入整体验收时生效，开始新的整体尝试，保留原失败记录；已返回而未落地的指挥决定存在时拒绝（先普通 `resume`），从未返回的决定意图在核实退出后丢弃，已扣的预算不退回。整体拒绝或验证失败之后，在新的修订落地之前每次都是指挥决定；已接受的修正任务未改变失败快照时，指挥再决定，不重抽整体审阅、不重跑相同代码。

开启 `verification.fresh_checkout` 时，整体验证在系统临时目录的全新 detached worktree 中执行；准备失败或中断后，普通 `resume` 在另一个新检出重试当前最终尝试。验证中产生未忽略的新文件时，核对后可在 `git rev-parse --git-common-dir` 指向的公共目录的 `info/exclude` 中加入精确模式，再恢复；不要用宽泛模式掩盖待交付代码。

### 进程退出确认

`--confirm-exited DISPATCH_ID` 用于 `resume`、`abandon`、`cleanup-run`，可重复。运行时在运行锁内校验全部 ID，先写审计事件（含当时的进程组探测与组长身份），再只更新指定记录；命令子进程用父 dispatch ID 确认，审计绑定子日志摘要。确认不终止进程、不放宽 HEAD、快照、Git 绑定或清理门禁，也不证明调用成功或没有结果。

### 放弃与清理

- `abandon RUN` 把未完成的 run 标为 `abandoned`，保留分支、工作树和证据，之后不能 resume。它先经过 `load_run`：台账恢复链接损坏或加载时核验的证据被删改时拒绝放弃，保留现场供核对；路径索引与输入索引不在加载时核验，它们损坏时仍可放弃。放弃不删除文件；要移除工作树，之后执行 `cleanup-run RUN --remove`。
- `cleanup-run RUN` 是唯一的清理入口，默认只读预览，加 `--remove` 才逐项删除。它不加载配置、不调用模型，支持版本 7–17 的台账，不改 run 状态，不删除分支、台账或运行证据。
  - 共同门禁：在运行锁内核对台账版本与归属（run ID、仓库、工作树路径、分支名、Git 公共目录与管理目录绑定）、命令目录、路径索引与输入索引、worker 回执链接，以及全部派发与命令子进程的退出证据。任一项不通过时整条命令报错，不处理任何资源。
  - 全新验证检出：run 处于任何状态都可清理。已跟踪修改、未跟踪或忽略文件、`assume-unchanged`／`skip-worktree` 标记阻止清理，错误列出前 10 个阻塞路径。目录已缺失而 Git 注册仍在时，只注销该精确路径，不做全仓库 prune。记录写在检出证据旁的 `cleanup.json`。
  - 分支工作树：只在 `completed` 或 `abandoned` 时清理，其他状态下按规则保留（`retained`）。要求原分支仍指向检查点，HEAD 与 Git 绑定未变，没有任何未保存文件（含忽略文件）或隐藏索引标记，已跟踪文件的字节与 HEAD 一致。记录写在 `worktree-cleanup.json`。`--remove --remove-input-copies` 另删除登记且未修改的依赖副本。
  - 每个资源先保存删除意图，再执行 Git 删除，完成后记为 `removed`。删除后记账前中断时，重跑同一命令按已存意图续做，已经删掉的资源不再执行删除。工作树的记录绑定 run、工作树路径、分支名、检查点和 Git 绑定，不绑定整份台账，所以中断后用 `--confirm-exited` 补的退出确认不妨碍续做。
  - 工作树记为 `removed` 之后，操作者删除或移动分支、或之后的 `--confirm-exited` 改写台账，都不会改变结果：再次运行仍报告 `removed`（预览为 `absent`），只有路径、Git 注册或管理目录重新出现才报 `blocked`。分支核对只在工作树仍存在、即将删除时进行。
  - 输出逐项列出每个资源的 `status`：
    - `eligible`：预览时可以删除。
    - `removed`：已删除，完成记录已写入。
    - `absent`：预览时资源已不存在（有删除记录，或检出的目录、注册和管理目录都已消失）；加 `--remove` 时补写完成记录，报告 `removed`。
    - `stale_registration`：仅验证检出，目录已缺失而 Git 注册仍在；`--remove` 只注销该精确路径。
    - `input_copies_present`：仅工作树的预览，有登记的依赖副本，需 `--remove --remove-input-copies`。
    - `retained`：仅工作树，run 还没有 `completed` 或 `abandoned`，按规则保留，`reason` 写明当前状态。`reason_kind` 为 `finish_or_abandon_first` 时，台账是当前版本，先完成或 `abandon` 该 run 再清理；为 `manual_removal` 时，台账是旧版本，当前版本既不能恢复也不能放弃它，`cleanup-run` 不删除这个工作树，需人工核对后自行移除。
    - `blocked`：资源本可清理，但门禁挡住了删除或预览；`error` 写明原因和阻塞路径，资源原样保留。
  - 某个资源受阻不影响其余资源。只有出现 `blocked` 时退出码为 1；只有 `retained` 的工作树时退出码为 0。
  - 已删除的 `abandon --remove-worktree` 在旧台账里留下的 `worktree_removed` 或 `pending_worktree_removal` 被当作已有的删除意图：工作树目录、Git 注册和管理目录都已消失时，工作树按已移除处理，其余资源照常清理。
- `prepare-run RUN --manifest FILE` 只在 `paused` 或 `awaiting_approval` 且没有待处理阶段时准备依赖副本，格式见[依赖副本清单](operations.md#依赖副本清单)。

### 证据损坏

`load_run` 在 `resume` 和 `abandon` 做任何事之前核验以下证据的路径、类型与 SHA256：worker 派发的 `packet.json` 与 `schema.json`（`packet.json` 同时是该 worker 必需问题的键对照）；`attempt_exemption` 引用的原始 worker 结果；`decisions/` 下的请求与答复；检查点 manifest 与批次文件；`continuations/`、`continuation-rejections/`、`execution-guidance/` 下的完整产物。其中任一缺失、被改或换成符号链接时，`resume` 与 `abandon` 都在写入前拒绝。出口只有两个：把原字节恢复回去后照常 `resume` 或 `abandon`；或者保留 run 目录与工作树原样，另开新 run。`status`、`watch`、`dashboard` 不经过这些核验，仍可查看。

路径索引（`path-indexes/`）、输入索引（`input-indexes/`）和环境更新产物（`environment/`）在 `resume` 预检、派发或接受前核验，不在加载时核验：它们损坏会阻止继续，但不阻止 `abandon`。

提交消息的来源在用到时核验，同样不阻止 `abandon`：构造任务审阅包前，`pending_task.worker_result` 与 worker 原件不同即停止“worker 结果台账与原件不同”，不派发审阅；接受时在 `pre_accept` 前后各核对一次，`pending_acceptance.worker_source` 不完整、与批准它的审阅记录的来源或工作尝试不符即停止“待接受的 worker 来源与批准它的审阅不符”，原件缺失或被改（包括被 `pre_accept` hook 改动）即停止，都不提交。出口是把台账或原件的原字节恢复回去后 `resume`，或者 `abandon`。

已发布的计划文件（`plans/revision-N.json`，`plan_history` 的每一行）同样不在加载时核验，只在三处核对：`resume` 的任何写入之前（含 `--confirm-exited` 的审计写入，因此也先于批准计划和重放 worker 回执）、每次推导下一步之前、标记完成之前；逐个核对它们存在、摘要与台账一致、修订号相符，并要求最新版本的任务表等于 `tasks`、`revision-0` 的任务表等于 `original_tasks`。缺失、被改或不一致即停止，不调用模型；恢复原字节后普通 `resume`，无法恢复时 `abandon`（`abandon` 与 `cleanup-run` 不需要计划文件，它们自己的 `--confirm-exited` 照常可用）。不覆盖的情形：worker 运行期间被删改的计划文件要到下一步才发现，那时该任务的验证、审阅与接受可能已经完成；`prepare-run` 与 `status` 等只读入口不核对，之后的 `resume` 会拒绝。每次核对的耗时随计划历史的总大小增长：实测每版约 40 KB 时，200 版约 19 ms，1,000 版约 102 ms。

### 终态与历史台账

`completed` run 的普通 `resume` 返回保存结果，不派发；修改选项被拒绝。

版本 16 及更早的台账不迁移、不执行：`resume`、`abandon` 和 `prepare-run` 都要求版本 17，对旧台账拒绝。以下只读与诊断入口不要求当前版本，仍可用于旧台账：`status`（含 `status RUN` 与 `--full`）、`watch`、`dashboard`、`permission-diagnostics`，以及受门禁保护的 `cleanup-run`（版本 7–17；验证检出不限状态，工作树仅限 `completed` 或 `abandoned`）。旧版本中未到终态的 run 不能 `abandon`，因此 `cleanup-run` 只能清理它的验证检出，不能清理其工作树。升级前应完成或放弃进行中的旧 run。

版本 16 及更早的 `stream_metadata_v1` 模式 run，派发目录里保留 `capture-manifest.json` 与 `stream-*` 文件，观察层按普通文件展示；`cleanup-run` 按 worker 接收意图保存的输出模式、用当时的分类规则复核，stream 超时后放弃、仍带活动回执和 unknown 判定的 run 也能清理。

## 不能自动恢复的窗口

以下中断窗口需要操作者判断；运行时停止并保留现场，不提供改写台账的捷径。

1. **Git commit 之后、接受台账保存之前被杀**：恢复因 HEAD 不符停止，不会再提交一次。用 `git -c trailer.separators=: log --format='%H %(trailers:key=Harness-Task,valueonly)'` 核对含 `Harness-Run`／`Harness-Task` 的提交；它的消息就是 `pending_acceptance.worker_source` 指向的 worker 原件里的 `commit_message`。再决定恢复现场或 `abandon`。
2. **接受时重建暂存区后、commit 之前中断**：暂存区摘要变化使旧批准失效，任务重新执行、验证、审阅并消耗一次工作尝试；预算用满时需先提高 `max_attempts`。
3. **worker 已有能解码成对象的原始输出、封存之前中断**：不能把恢复时的工作树补绑为原候选，也不自动重派；与原始输出冲突、超过读取上限或读不出文件（判为 unknown）同样阻塞，`--confirm-exited` 不能解开。保留证据后 `abandon`。对象不合契约时也停在这里，而不中断时它会作为无效输出进入下一次尝试。输出解不出对象（截断、非 JSON、重复键、不是对象）不在此列：无论是否中断，都作为无效输出进入下一次尝试。
4. **调用前台账保存失败**：可能留下已扣预算、`invoked=true` 的条目；恢复按可重试处理，多计一次尝试。
5. **派发 `starting` 后、pgid 落盘前中断**：缺 pgid 的记录不能证明未启动，需核实后 `--confirm-exited`。
6. **启动时 `worktree_binding` 保存前中断**：没有可信绑定，只能 `abandon` 后重新运行，并手工清理已创建的工作树和分支。
7. **供给计划在首次保存前中断**（`tasks` 与 `original_tasks` 都为空）：无法辨认原计划来源；`abandon` 后用原 `--plan` 重新运行。
8. **`post_accept` 或其他 hook 成功后、完成记账前中断**：hook 会再次执行；外部副作用必须可重复。
9. **`delete_path` 删除之后**：删除立即生效，接受门禁只决定候选是否被接受；需要恢复文件时从 Git 历史中取回。
10. **`prepare-run` 在保存临时文件身份之前中断**：不能凭相同字节接管文件，需人工核对。
11. **`command-staging/` 中的准备目录**：发布前失败或被杀时留下；运行时不枚举它们，也不回收，反复失败会累积磁盘占用。
12. **指挥或审阅的供应商输出不可解码、判定保存之前中断**：回执里没有原件，恢复按无返回处理，发起没有 `retry_of` 的调用；与不中断相比最多多一次调用。
