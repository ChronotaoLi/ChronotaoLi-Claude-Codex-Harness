# 跨仓库操作、操作记录与观察网页

本文说明从 Harness 源码目录对其他项目执行任务的统一入口、外层 agent 的操作边界、自动操作记录与人工补充模板、问题报告模板、证据归属、本地只读观察网页、角色工具接口、依赖副本、单次监督和真实账号联调。日常命令见 [README](../README.md)，停止后的出口见[恢复与操作者出口](recovery.md)，观察层边界见[设计：不变量](design.md#不变量)。

- [统一入口](#统一入口)
- [外层 agent 操作说明](#外层-agent-操作说明)
- [操作记录与记录模板](#操作记录与记录模板)
- [报告模板](#报告模板)
- [证据归属](#证据归属)
- [观察网页用法与限制](#观察网页用法与限制)
- [角色工具参考](#角色工具参考)
- [worker 结果与审阅问题](#worker-结果与审阅问题)
- [依赖副本清单](#依赖副本清单)
- [单次监督](#单次监督)
- [真实账号联调](#真实账号联调)

## 统一入口

在 Harness 源码目录执行，所有路径使用绝对路径：

```bash
cd /abs/harness
python3 -m harness --config /abs/harness.toml doctor
python3 -m harness --config /abs/harness.toml --repo /abs/project run "目标描述"
python3 -m harness --config /abs/harness.toml --repo /abs/project resume RUN
python3 -m harness --repo /abs/project status
python3 -m harness --repo /abs/project watch RUN
python3 -m harness --repo /abs/project dashboard --port 8765
```

`pip install -e .` 后，`harness` 命令等价于 `python3 -m harness`（`pyproject.toml` 的 `[project.scripts]`）。无论哪种方式都固定使用同一个解释器：`doctor` 与操作记录的 `harness.python`／`harness.executable` 记录实际解释器，换解释器可能加载另一份安装的 `harness` 包。

- `--config`、`--repo` 是全局参数，必须写在子命令之前；写在子命令之后会被 argparse 当作未识别参数拒绝。
- 未传 `--config` 时使用 Harness 源码目录的默认 `harness.toml`，不会按 `--repo` 自动选择目标项目的配置。`run` 启动时在 stderr 回显 `[harness] config=...` 与 `[harness] verification=...`，后续 `resume` 应使用同一份配置。
- 共享资源（rules、skills、mcp、hooks）相对配置文件所在目录解析；复制配置到其他位置时同步修正这些路径。`doctor` 的 `shared` 字段显示解析后的绝对路径。
- 启动前用 `doctor` 核对实际加载的包与配置：`harness.package`（包目录）、`harness.revision`（包目录上级是 Git 检出根时的 HEAD，否则 null）、`harness.runtime_modified`（`harness/` 下有已修改或未跟踪文件时为 true）、`config.path`／`config.sha256`、`asset_digest`（共享资产摘要；失败为 null 并另给 `asset_error`）。同名模块、site-packages 中的另一份安装或改过的工作副本都会在这里显现。`doctor` 的退出码仍只取决于配置引用的 CLI 是否已登录。
- 启动前核对目标仓库：已有提交，已跟踪文件与暂存区没有未提交修改，`verification.commands` 与依赖在工作树中可用，生成产物已忽略。这些要求见 [README](../README.md#对自己的项目执行任务)。
- 每次 `run`／`resume` 自动写一份操作记录，可用 `--operation-id ID` 指定记录 ID；见[操作记录与记录模板](#操作记录与记录模板)。

## 外层 agent 操作说明

外层 agent 指替操作者调用上述入口的自动化会话（例如另一个 Claude Code 或 Codex 会话）。它在已有授权内可以：

- 启动 `run`，并为每次调用指定可追溯的 `--operation-id`；
- 用 `status`、`status RUN`、`status RUN --full`、`watch RUN` 和 `dashboard` 观察，读取目标项目 `.harness/` 下的台账、事件、dispatch 产物与操作记录；
- 使用[阻塞状态与操作者出口](recovery.md#阻塞状态与操作者出口)中明确适用于当前状态的恢复出口，例如 `--approve-plan`、追加精确命令授权后的普通 `resume`、`--environment-update`、`--operator-decision`、`--retry-stopped`。

遇到真正的新增权限（新的命令授权、范围扩展、预算或轮数提高）或需求选择（操作者决定、是否接受风险、是否放弃）时向用户请求，不自行代答。

不能做的事：

- 循环 `resume` 直到通过。同一快照上的拒绝与停止不会因重复恢复而改变；读不出否决的无效输出由新调用重做（每次请求最多重做一次）；重复恢复只消耗派发预算。
- 自动增加预算、超时、轮数或上下文上限来“解除”停止。
- 更换模型、供应商或 effort。
- 修改 `state.json`、`events.jsonl`、dispatch 产物或记忆库。
- 自行 merge／cherry-pick run 分支；最终集成由用户完成。

内部的 worker／reviewer 不调用该入口：它们在隔离工作树内完成一个任务或一次审阅，不启动新的 `run` 或 `resume`。

## 操作记录与记录模板

每次 `run`／`resume` 在加载配置之后、构造引擎之前，于 `<目标项目>/.harness/operations/<operation_id>.json` 独占创建一份记录。ID 默认自动生成（`op_` 加 16 位十六进制），也可用 `--operation-id ID` 指定：以字母或数字开头，只含 `A-Za-z0-9_.-`，最长 64 字符。同名记录已存在时拒绝并退出 1，不覆盖已有记录，也不写台账、不创建 run、不改 `.git/info/exclude`；重复 ID 检查在公共 Git 锁内、保护规则变更前完成。stderr 输出 `[harness] operation_id=<id> record=<路径>`；stdout 仍是单个 JSON，新增 `operation_id` 键。

记录不是第二套执行真相：它不取 `run.lock`、不写 run 台账，也不参与任何恢复判断。记录更新失败只在 stderr 打印 `[harness] 警告：...`，不改变运行、保存顺序或退出码。

### 自动记录的字段

| 字段 | 含义 |
| --- | --- |
| `operation_id`、`kind` | 记录 ID；`run` 或 `resume` |
| `status` | `prepared`（已创建）→ `running`（已关联 run）→ `finished`（CLI 结束） |
| `started_at`、`started_at_utc`、`finished_at`、`finished_at_utc` | epoch 秒与 ISO 8601 UTC |
| `arguments` | `run`：`goal`、`plan`（路径或 null）、`commander`、`pause_after_plan`、`step`；`resume`：`run_id`、`approve_plan`、`step`、`reverify`、`retry_stopped`、`confirm_exited`、`environment_update`、`execution_guidance`、`reject_continuation`（后三项为路径或 null）、`operator_decision`（路径列表）、`allow_scope`。文件型选项只记路径 |
| `repo`、`repo_head` | 目标项目绝对路径与当时 HEAD；取不到为 null |
| `harness` | `package`、`revision`、`runtime_modified`、`python`、`executable`，与 `doctor` 回显相同 |
| `config` | `path`、`sha256`（配置文件字节摘要） |
| `asset_digest` | 共享资产摘要；失败为 null |
| `models` | 配置中 commander／fable／worker／reviewer 的 `cli`、`model`、`effort` 原样 |
| `cli` | `claude`、`codex` 的 `path`（PATH 查找结果）与 `version`（`--version` 首个非空行，5 秒超时，失败为 null） |
| `run_id`、`run_linked_at` | run 持久登记后立即写入；创建 run 前失败时为 null |
| `exit_status`、`result_status`、`error`、`error_type`、`error_omitted` | CLI 退出码（0／1／130）、最终 JSON 的 `status`、错误首行与异常类型名。本次调用含文件型输入（计划或恢复 JSON，包括执行指导与续作拒绝）且出错时，`error` 为 null 并写 `error_omitted: "file inputs present; see stderr"`，只保留 `error_type`，因为校验错误可能引用文件中的字段值；错误全文仍输出到 stderr。无文件输入时 `error_omitted` 为 null |

记录不保存环境变量值、凭据、计划或恢复 JSON 的文件内容或生成的 MCP 配置。

run 身份的关联：`run` 的首次台账保存随同写入可选字段 `created_by_operation`（不是额外一次保存），保存成功后追加 `run_created` 事件（带 `operation_id`）、在 stderr 输出一行 `[harness-json] {"event":"run_created","run_id":"...","operation_id":"..."}`，并立即把 `run_id` 写进记录，长时间运行或被强制终止时也能关联。`resume` 在自身预检门禁（含计划批准）通过后、进入 `execute()` 前同样追加 `run_resumed` 事件与通知。

`run_resumed` 不表示已经派发模型：缺少命令授权、等待操作者决定等执行阶段门禁仍可使它紧接一条 `run_stopped`，并沿用原有停止记账和 on_error hook。恢复预检拒绝时不写 `run_resumed`、不输出该通知，但之前保存的退出确认审计、配置放宽、命令授权检查等记录仍保留。已完成 run 的普通 resume 不写运行事件，台账与事件文件保持不变。操作记录缺失、损坏或与台账不一致都不影响 resume、abandon 或清理。

上述拒绝或已完成返回只要对应台账文件存在，操作记录结束时仍关联参数中的 run ID。加载配置失败时没有记录（记录在 `Config.load` 之后才创建）；创建 run 之前失败（仓库基线不满足、计划非法等）时记录保留但没有 `run_id`；resume 在预检阶段被强制终止时记录也可能没有 `run_id`。

限制：非 Git 检出或包目录上级不是检出根时 `revision`／`runtime_modified` 为 null；`runtime_modified` 也会因 `harness/` 下的未跟踪文件为 true；`cli.version` 是启动时 PATH 中的 CLI，不证明派发实际使用了同一个二进制。

### 人工补充（人工核实）

自动记录只覆盖调用事实。做实测或排障时，另写一份以 `operation_id` 命名的说明（放在目标项目 `.harness/` 或汇总索引里，不写进记录 JSON）。以下字段都标注“人工核实”：

| 字段 | 内容 |
| --- | --- |
| 任务目的 | 这次操作要验证或完成什么；不是 `goal` 的复述 |
| 人工干预 | 每次手工动作：时间、命令或编辑、依据哪条恢复出口；没有则写“无” |
| 结论 | 任务层面（accepted／未接受／放弃）与 Harness 层面（是否暴露缺陷、缺陷 ID）分开写 |
| CLI 版本核对 | 记录中的 `cli.*.version` 与实际派发使用的 CLI 是否一致（不同 shell 的 PATH 可能不同）；不一致时写明实际版本与依据 |

项目专用的环境处理（例如某个项目需要清除某个代理变量）只记录具体条件、变量名与证据路径，不保存变量值或秘密；也不把它推广为“删除全部 `CODEX_*`／`CLAUDE_*`”之类的通用措施。

## 报告模板

实测或排障报告中的每个问题使用一个稳定 ID（例如 `P-01`），后续轮次沿用，不重新编号。

| 字段 | 内容 |
| --- | --- |
| ID | 稳定 ID |
| 现象 | 实际观察到什么，引用原文或日志行 |
| 预期 | 依据哪条文档或规则应当发生什么 |
| 首次位置 | 首次出现的文件与行、事件或页面区块 |
| run／task／dispatch | 相关 run ID、任务 ID、dispatch ID；缺失写“未知” |
| 证据 | 目标项目 `.harness/` 下的路径（台账、事件、dispatch 日志、操作记录）或有界摘录 |
| 人工干预 | 为复现或绕过做过什么；没有则写“无” |
| 恢复结果 | 采用了哪个恢复出口、结果如何；未尝试写“未尝试” |
| 可信度 | 高（证据直接支持）／中（部分推断）／低（仅猜测） |
| 类别 | 见下表，只填一项；无法归类用“尚未定位” |

| 类别 | 含义 |
| --- | --- |
| Harness 运行时 | 编排器自身的错误、门禁误判、台账或恢复问题 |
| 角色协议／模型表现 | 模型未遵守输出契约或角色规则、工作质量不足 |
| 供应商 CLI | Claude Code／Codex CLI 的行为、版本或登录问题 |
| 配置／环境 | 配置、共享资源、PATH、依赖或权限 |
| 目标项目缺陷 | 被操作项目自身的 bug 或测试问题 |
| 尚未定位 | 证据不足以归类 |

任务状态（accepted／blocked／abandoned）与 Harness 缺陷状态（已确认／已修复／不复现）分开记录：任务失败不等于 Harness 缺陷，Harness 缺陷也可能出现在任务成功的 run 中。

## 证据归属

- 原始证据留在目标项目的 `.harness/`（台账、`events.jsonl`、dispatch 产物、操作记录），不复制进 Harness 仓库。
- Harness 仓库的 `.harness/field-tests/`（被 Git 忽略）只放汇总索引与引用：每条注明来源路径、来源是否仍存在、是否保全了副本及副本位置。
- 旧记录缺失的字段写“未知”，不补推。
- `docs/validation.md` 只记录已完成的 Harness 验证结果，不记录测试进度或外部项目的任务状态。

## 观察网页用法与限制

```bash
python3 -m harness --repo /abs/project dashboard            # 默认端口 8765
python3 -m harness --repo /abs/project dashboard --port 0   # 由系统分配端口
python3 -m harness.dashboard --state /abs/project/.harness --port 8765
```

与 `status` 一样不加载配置；`--repo` 必须是 Git 仓库。stdout 只打印一行 `http://127.0.0.1:<port>/`，必须用这个地址打开：Host 必须恰为 `127.0.0.1:<port>`，用 `localhost` 或其他主机名得到 403。Ctrl-C 或 SIGTERM 停止，退出码 0；其他信息走 stderr。

它是只读、本机、单项目多 run 的视图：

- 不启动、恢复或修改 run，不取 `run.lock`，不写任何文件，不启动子进程，不探测进程；启动前只解析一次 `--repo` 的仓库根，服务开始后不调用 Git。
- 台账记为 `running` 不等于进程仍在工作；页面只显示 `process_state`、`exit_confirmed`、`pgid` 与最后事件时间。没有新日志不等于卡死；worker `completed` 不等于任务 `accepted`。
- 各文件按请求逐个读取，显示采样时间而不是原子快照；运行中的 `events.jsonl` 最后一行可能未写完，页面标为未完成，不算错误。
- Claude 的结构化输出可能在调用结束才出现：运行中的 dispatch 还没有返回结果或用量是正常的。

### 页面

run 列表 → run 详情 → 文件查看。列表按最近事件排序，以卡片显示台账状态、目标摘要、分支与最近事件时间，并汇总需要关注／进行中／已结束的数量。详情首屏只放判断所需的信息：目标摘要与状态标记；“需要处理”区（运行错误、摘要或台账读取失败、等待操作者决定、当前环境阻断、缺失命令授权、续作请求、再规划停止、返工提示、当前任务最近一次调用的错误，并附常用出口），没有问题时不显示该区；台账状态、已接受／总任务进度、当前任务、最近观测与待处理数量；任务列表与最近活动。长目标、决定问题和事件字段默认只显示有界摘要，“查看完整原文”展开后显示未截断的原文。预算与费用、调用记录（逐行展开查看进程、用量与产物文件链接）、事件日志、操作记录、运行信息（工作树、台账版本、操作记录、worker 结果计数、历史环境阻断等）和文件索引（可按路径筛选）收在标签页中，可用方向键、Home、End 切换；“如何理解这些状态”展开后给出台账与进程、完成与接受的区别。同一 run 的事件刷新和手动刷新保留当前标签、已展开项、文件筛选、页面与原文框／标签栏／表格的滚动位置以及键盘焦点（隐藏标签或收起项中的位置在再次显示时恢复；同一文件的刷新保留阅读位置）；切换到其他 run 或文件时恢复默认。文件查看显示文本尾部，默认 64 KiB，最大 1 MiB，可调整。详情默认开启“跟随运行变化”，收到原生文件通知后刷新；列表与文件查看使用刷新按钮，不定时轮询。连接断开会显示提示，手动刷新可重新订阅。界面为中文，跟随系统浅色／深色并适配窄屏；`status`（无 run ID）的列表与网页读同一份投影，stdout 字段不变。

返工提示来自已有台账：未接受任务的有效工作尝试达到 3 次，或最近两次连续拒绝审阅出现相同的 blocking finding 标签（审阅者写的 id）时提示；整体验收累计拒绝至少 2 次且最新结果尚未批准时也提示。`status RUN`、`watch RUN` 与详情中的 `convergence` 都是建议，不自动改变预算、范围、派发或接受条件；completed／abandoned 不显示返工警告。

审阅轮次投影 `review_rounds` 按任务列出 `review_history` 的每一轮（按记录顺序从 1 编号，含失效或结构非法的记录）：`attempt`、来源审阅 `review_dispatch_id`、候选 `snapshot_digest`、`verdict`，以及本轮首次出现的 blocking finding 标签 `new_blocking`（按审阅者写的 id 字符串判断；标签不是身份，同名不一定是同一个问题）。它只读、不调用模型，`cause` 固定为 `unknown`，不推断问题为何在后续轮次才出现。详情页在“运行信息”标签显示该表。

预算区显示 `attempts_total`（各任务 `attempts` 之和；版本 16 起没有全 run worker 上限）、模型派发、再规划与整体验收的已用／上限，以及未终止 run 的 `remaining_model_calls`：“如果之后全部一次通过，至少还需 N 次模型调用”。N 按记录的阶段计算：已在待接受或任务审阅已开始的任务计 0，已有候选的计 1，其余未接受任务计 2，整体审阅计 1（全部已接受且同一 HEAD 已批准或整体审阅已开始时计 0）。N 是下界，不含指挥调用、返工、无效输出的重做与再规划；`max_agent_dispatches` 用尽时停止信息里也给出它。版本 15 及更早的台账另显示保存的 `total_attempts` 与 `attempt_budget_policy`。

脚本可直接读 JSON 接口：`/api/runs`、`/api/runs/<run_id>`、`/api/runs/<run_id>/events?tail=`、`/api/runs/<run_id>/files`、`/api/runs/<run_id>/files/<相对路径>?tail=&format=json`。无效 ID 或参数 400，拒绝的路径与 Host 不匹配 403，不存在的 run 或文件 404，GET／HEAD 以外的方法 405；台账可读但摘要失败的 run 仍返回 200 并带 `summary_error`。

`GET /api/runs/<run_id>/stream` 提供 SSE，HEAD 返回 405。`ready` 与 `change` 只发送 run ID、原生后端和采样时间，页面随后读取当前投影；`unavailable` 表示订阅不可用并关闭连接。每 15 秒的注释心跳仅用于连接维护，不读取台账，也不证明模型进程存活。默认最多 2 个订阅，占用总计 8 个连接额度；断连、写入超时和服务关闭都会释放订阅。

命令行 `watch RUN` 先订阅，再输出简短 JSONL 状态；后续仅在投影变化时输出。到达 completed、abandoned、blocked、interrupted、paused 或 awaiting_approval 后退出；`--follow` 可跨越这些状态继续等待，Ctrl-C 结束。`watch RUN --state /abs/archive/.harness` 可观察归档状态，不要求 Git 仓库或配置。macOS 使用 kqueue，Linux 使用 inotify 与 `/proc/self/fd`；平台不支持或订阅失败时明确报错，不回退到轮询。输出的 `process_observation=ledger_only` 表示只读台账，未探测模型进程。

### 调用阶段分类

每个模型 dispatch 先做矛盾检测再分类：

| 类别 | 含义 |
| --- | --- |
| `usage_recorded` | 已进入后端调用且台账有 `provider_usage` |
| `invoked_no_usage` | 已进入后端调用但 CLI 未留下用量（超时、CLI 失败、输出损坏等）；已消耗本地派发预算，不断言供应商是否计费 |
| `known_not_invoked` | 已知未进入调用：准备中的记录，或本地准备门禁（hook、MCP、快照、预算）失败后的收尾状态 |
| `unknown` | `invoked` 字段缺失、不是布尔值，或与 `pgid`／`process_state` 矛盾；页面悬停显示原因 |
| `verification` | 验证进程，不计入模型费用 |

`invoked=true` 在进程启动前写入，不证明 CLI 已启动或供应商已收到请求；`process_state=starting` 另标“启动未确认”，不覆盖上述分类。

事件日志里的派发事件：`dispatch_prepared` 在本地准备保存后、预算检查前写入；`dispatch_started` 在台账保存 `invoked=true` 之后写入；`dispatch_finished` 带 `invoked`、`status` 和 `provider_usage`。正常完成的 run 中 `dispatch_started` 条数等于 `agent_dispatches`。事件是尽力而为的观察：写入失败不影响调用，崩溃可能缺一条，恢复不补发。计数与费用以台账为准。版本 15 及更早的 run 中，不带 `invoked` 字段的 `dispatch_started` 写于预算检查前，不表示已调用。

### 费用

费用按供应商分列，以台账 dispatch ID 为唯一累计单位；事件中的用量不再相加，重复的 dispatch ID 只计一次并列出。

- Claude 显示的是 CLI 报告的估算费用，不是账户账单。
- Codex 不报告美元费用，显示“不可用”。
- 没有任何合法费用字段时显示“不可用”而不是 0；不给全 run 合计。
- token 只在 dispatch 行逐条显示原字段，不跨 dispatch 或供应商相加。
- 按组：协调开销（role 为 commander 的 plan／dispatch／replan，按角色识别）、各任务（worker／reviewer 按 `task_id`）、验证。失败或超时的调用只要有合法报告费用也计入。

### 文件访问

只能通过固定根读取 run 目录内的普通文件：顶层 `state.json`、`events.jsonl`、`handoff.json`、`handoff.md`（只在旧 run 目录中存在）、`ignored-baseline.json`、`ignored-paths.json`、`worktree-cleanup.json`；目录 `plans/`、`dispatches/<uuid>/`（含 `commands/<32 位十六进制>/`）、`verification/`、`hooks/`、`recovery/`、`decisions/`、`environment/`、`checkpoints/`、`path-indexes/`、`indexes/`、`batches/`。`mcp.json`、`run.lock`、`*.tmp` 与 `memory.sqlite3*` 被排除；符号链接与非普通文件一律拒绝，中间目录在检查后被替换为符号链接也无法逃出根目录；路径深度不超过 8。台账中的绝对路径只显示不打开。文件内容永远以 `text/plain`（或 `format=json`）返回，日志不会作为 HTML 执行。

### 限制

- 只绑定 127.0.0.1，不开放远端访问；只接受 GET／HEAD（事件流仅 GET）；同时连接最多 8 个，超出返回 503；写入阻塞的慢客户端约 10 秒后断开。
- 服务在启动时固定持有 `.harness` 目录的描述符；目录被删除重建后需重启服务。目标项目还没有 `.harness` 目录时 `dashboard` 直接报错退出 1，而 `status` 列表为空。`.harness/runs` 本身是符号链接或不可读时，`status` 列表输出包含目录路径的诊断并退出 1；`status RUN` 保留既有的直接台账读取方式，不使用列表的符号链接拒绝规则，但仍须能读取该文件。
- 首／末事件时间只在 `events.jsonl` 头／尾 64 KiB 内查找；dispatch 结束时间只取尾部 64 KiB 中的事件，较早调用可能显示不可用；`state.json` 超过 64 MiB 不解析；操作记录最多扫描 1000 个文件、每个 256 KiB；文件索引最多 2000 项。
- 损坏的台账在列表中跳过并给诊断；详情仍尽量显示并标出 `ledger_error`／`summary_error`；旧 `schema_version` 没有的区块标“不可用”。
- 不汇总多个项目，不按公开单价估算账单，观察结果不改变任何预算。自动验证覆盖 HTTP、macOS 原生通知和页面脚本行为；Linux 原生通知及浏览器人工验收尚未执行，验证范围见[验证记录](validation.md)。

## worker 结果与审阅问题

### 检查点

WORK 结果可以带可选的 `checkpoints`，每项为 `{id, summary, inputs, evidence, freshness, max_age_seconds}`，`inputs`、`evidence` 是 `{path, sha256}` 列表。运行时把 evidence 原文私有复制到 `checkpoints/<dispatch_id>/`，之后按声明的输入和有效期在角色输入中标为 `reusable` 或 `stale`。

| 限制 | 值 |
| --- | --- |
| 每个结果的检查点 | 最多 16 项，ID 在本结果内唯一 |
| 每项 `inputs`／`evidence` | 各最多 8 个，`evidence` 至少 1 个 |
| 单个 evidence 文件 | 1 MiB |
| 一次结果的 evidence 副本合计 | 16 MiB |
| `summary` | 非空，最多 4,000 字符 |
| `max_age_seconds` | 0–86,400 |

- `freshness="content"` 必须绑定至少一个 input，且 `max_age_seconds=0`；任一输入的实际 SHA256 变化即为 stale。
- `freshness="volatile"` 的有效期从来源 worker 调用开始计时（dispatch 的 `invoked_at`），worker 自己的执行时间也算在内，结果落盘或恢复都不重新计时；`max_age_seconds=0` 表示每次都要重新检查；缺少有效调用时间的记录一律 stale。
- 路径不得含 `..` 或 `.git`、`.harness`、`.codex`、`.claude` 段，不能是符号链接。唯一例外：evidence 可以引用当前 worker dispatch `commands/` 下的普通文件（broker 返回的 stdout／stderr 路径）。新的 worker 尝试是新的 dispatch，不能重新登记原 dispatch 的 broker 输出；旧证据也不能靠重新登记刷新有效期，需要时重跑可安全重复的检查。
- `reusable` 只表示声明的输入未变且未过期，不证明依赖完整、结论正确或任务已接受；检查点不替代验证、审阅或接受门禁。

声明被丢弃与证据损坏是两回事。检查点是可选产物，不参与 worker 结果的契约判定：单项的声明核对失败（字段缺失或超长、空 evidence、SHA256 格式非法、content 缺输入或有效期非零、路径不允许、实际摘要不符、超出大小）只丢弃该项，超过 16 项或重复 ID 丢弃整批；结果、blocker 和其余有效项照常登记，写 `checkpoints_discarded` 事件，blocked 反馈带前 2,000 字符原因。无效的 worker 结果（见[无效输出与自动重做](recovery.md#无效输出与自动重做)）不登记检查点；交接请求非法的 completed 结果例外，它在检查点登记之后才被判为无效，合格的检查点保留给下一次 worker。原始结果、台账行、manifest、`checkpoints/batches/` 下的批次文件或私有副本被删改，以及私有写入失败，都直接停止并保留现场，不按丢弃处理。

### 决定请求与审阅问题

- 已满足本任务验收的 completed 结果可以附带最多 16 条 `decision_requests: [{id, task_id, question}]`，目标只能是传递依赖于本任务、尚未接受的任务；目标或问题不合法时本次尝试被拒绝，原因经 `worker_recovery` 交给后续调用。blocked 结果附带的 `decision_requests` 不登记，只写 `decision_requests_discarded` 事件和原结果摘要；该结果的 blocker、有效检查点和尝试豁免照常登记。
- finding 的内部身份是（来源审阅 dispatch，该审阅原件 `findings` 数组中从 0 起的序号 `finding_index`）；审阅者写的 id 只是标签 `label`，可以重复，也不做别名。每次派发收到的 `required_findings` 每行是 `{key, review_dispatch_id, finding_index, label, artifact}`，`key` 为运行时生成的 `f1…fN`；`artifact` 在更省字符时提到 `required_finding_sources[review_dispatch_id]`。审阅者的 `finding_dispositions` 与 worker 的 `repair_claims` 是以这些键为属性的固定对象，键全部必填、不许多余键，没有必需项时字段不出现。审阅包里的 `repair_claims` 已按（来源，序号）换成本次审阅的键，只含两边清单都有的问题。历史审阅的键只在它那次派发内有效，按该记录自带的 `required_findings` 副本解释。无效拒绝中的处置不能关闭旧问题；任务证据由运行时按 `evidence_requirements` 直接检查候选文件，worker 不再声明，预检失败时 `evidence_invalid.reason` 为 `missing`、`symlink`、`not_regular_file`、`outside_candidate`、`unsafe_path` 或 `unreadable`。

## 依赖副本清单

`prepare-run RUN --manifest FILE` 在 run 锁内预检主检出源文件、当前 HEAD、worktree Git 绑定、所有已登记派发和命令进程。仅接受当前台账版本（17）的 `paused` / `awaiting_approval` run，没有待消费候选、审阅、再规划等阶段；历史版本的 run 只读。已接受任务和预算不改变。

清单示例（HEAD 和 SHA256 必须替换为实际值，mode 是十进制 POSIX 权限）：

```json
{"schema_version":1,"run_id":"run_0123456789abcdef","head":"<current expected HEAD>","source_root":".","max_files":1,"max_bytes":1048576,"files":[{"source":"deps/input.dat","destination":"vendor/input.dat","sha256":"<64 lowercase hex>","mode":420,"type":"file","ownership":"input_copy"}]}
```

源为主检出中的普通独立文件；目标必须在隔离 worktree 中、被 Git 忽略且未跟踪。拒绝符号链接、硬链接、保留路径、隐式凭据路径、源目标重叠和未知已有目标。复制前冻结清单摘要与台账绑定，再记录意图、独占临时文件身份、发布及回执；源和副本不共享 inode。中断后仅用登记的文件身份恢复，不能凭内容相同接管文件。resume 与派发前均核验登记副本；前置核验失败不新增 worker 次数。

清理默认只读预览。`cleanup-run RUN --remove --remove-input-copies` 在终态与进程退出、HEAD/Git 绑定门禁通过后，整批预检源和副本未改变，再逐项记录删除意图和结果。未知或改动的文件保留；其他遗留文件仍会阻止整个 worktree 删除。中断可能留下已删/未删回执，重复执行仅恢复同一意图。此机制不认领构建产物、不运行任意 setup，也不将忽略的可编辑源码导入任务。

## 单次监督

`supervise -- run ...` 或 `supervise -- resume RUN ...` 启动一次 Harness CLI（固定为当前解释器的 `-m harness`），独占一个 operation ID（可用 `supervise --operation-id ID` 指定），继承 stdio，并把真实的启动失败或 `wait()` 退出码写入 `.harness/supervision/<supervision_id>.json`；回执另输出到 stderr。`supervise` 的退出码等于子进程退出码（被信号终止时为 128 加信号值）。它不重启、不自动恢复；监督进程本身被强杀时回执可能没有更新；父进程退出不证明 broker 子进程已退出（回执的 `broker_processes` 固定为 `unknown`）。写回执失败时，若已有异常正在传播，失败只作为附注写到 stderr，原异常照常传播。

## 角色工具参考

### 只读 harness MCP

三种角色都能使用内置只读 `harness` MCP：`memory_search`（`query` 与 `key` 二选一）、`memory_read`、`skill_list`、`skill_read`、`run_status`，以及下面的文件工具和 `git_query`。它不写文件，没有记忆写入工具。

- `file_sha256({path})`：普通文件的 SHA256 与字节数，上限 1 GiB。
- `json_check({path})`：严格解析 UTF-8 JSON（上限 8 MiB），拒绝重复 key 与非有限数，返回 `valid`、根类型、值数量、摘要和可用的错误位置；非法内容是 `valid=false` 的证据。超出 Python 可表示范围的浮点数返回 `valid=false`；嵌套深度等解析能力限制返回工具错误。
- `file_diff({left,right})`：用 Python difflib 比较两个普通文件（各上限 8 MiB），返回两边摘要、差异与二进制标志，文本 diff 上限 24,000 字符并报告截断。

相对路径锚定 run 工作树，绝对路径可读工作树外的证据；解析符号链接后只读普通文件，不启动子进程、不运行 Git 过滤器。

### git_query

`git_query({argv})` 在 run 工作树上执行一条固定形式的只读 Git 查询，不经 shell、pager、网络，不写对象或索引。argv 必须整串匹配下表某一行，选项按所列顺序：

| 形式 | 实现 |
| --- | --- |
| `git ls-files [-z] [--deleted] -- <path>...` | `git ls-files` |
| `git ls-tree -r <rev> -- <path>...` | `git ls-tree` |
| `git rev-parse [--verify] <rev>` | `git rev-parse` |
| `git log -1 --format=%H -- <path>` | `git log`，附加 `--no-show-signature --no-ext-diff --no-textconv` |
| `git show <rev>:<path>` | `git cat-file blob`，返回原始 blob，不经 textconv |
| `git hash-object --no-filters -- <path>` | 不运行 Git 的 `hash-object`：先用 `git rev-parse --show-object-format` 确定对象格式，再从工作树根逐级 no-follow 打开文件，在 Python 中计算 blob 摘要，不写对象；最终项必须是普通文件，最大 1 GiB；拒绝链接、FIFO、设备和目录 |
| `git cat-file -e\|-t\|-s <rev>[:<path>]` | `git cat-file` |

参数规则：

- `<rev>` 整串匹配 7–64 位十六进制、`HEAD`、`HEAD~N` 或 `HEAD^N`（N 为 1–4 位十进制）；分支名、`@{…}`、`..`／`...` 范围、`HEAD~1junk` 都拒绝。
- 路径按字面解释（Git 字面 pathspec，`*`、`?`、`[`、`]` 不作通配，`:(…)` 魔法拒绝），必须是仓库相对路径，最多 128 个；拒绝空路径、空段、`.`／`..` 段、绝对路径、以 `-` 或 `:` 开头的路径、NUL 和看起来像 shell 运算符或重定向的参数。
- 含 `--` 的形式（`ls-files`、`ls-tree`、`log`、`hash-object`）中，`--` 必须恰好出现一次，且在规定位置，之后不能再有选项；`rev-parse`、`show`、`cat-file` 不接受 `--`。argv 最多 134 项，每项最多 4,096 字节。
- 执行时显式指定 git-dir／work-tree，剔除 `GIT_*` 环境变量，固定 `--no-pager --literal-pathspecs`，关闭 fsmonitor、签名显示、颜色和协议传输。

输出有两个上限。**保留上限 1 MiB**：`stdout` 最多返回前 1 MiB（截断时按完整 UTF-8 字符，无法解码时用 base64 并设 `stdout_encoding`），超出时 `stdout_truncated=true`。**观测上限 64 MiB**：超过保留上限后继续读取并计入 `stdout_bytes` 与 `stdout_sha256`，直到输出结束、任一管道读满 64 MiB 或 30 秒超时。输出正常结束时 `observed_complete=true`，`stdout_bytes` 与 `stdout_sha256` 覆盖完整输出；读满观测上限（恰好 64 MiB 也算）或超时时终止整个进程组，`terminated` 为 `output_limit` 或 `timeout`，`exit_code=null`，`observed_complete=false`，字节数与摘要只覆盖已观测的前缀。其他字段：`form`、`implementation`（`git` 或 `python_blob_hash`）、`git_args`、`stdout_returned_bytes`、`retain_limit_bytes`、`observe_limit_bytes`、`stderr`（保留前 64 KiB）与 `stderr_truncated`；`hash-object` 另有 `object_format`、`file_bytes`、`file_sha256`。

表外的 argv 不执行，直接返回工具错误。**已覆盖**指以下四处把能被该函数解析的 argv 算作已满足：计划的 `required_commands` 与 `self_checks`、续作命令请求、环境更新的 `worker_command` 检查（报告记 `coverage: "builtin_git_query"`）、`permission-diagnostics` 的 `builtin_coverage`（`covered`、`form`，`meaning` 为 `capability_available_not_executed`）。已覆盖只表示能力存在，不表示查询已执行或成功，也不是 Bash 或命令 broker 授权；管道、重定向和表外选项都不算已覆盖。`local_exact_argv_match` 与 `configured_sources` 仍只表示配置里的精确 argv。

### worker 命令与删除

只有 worker 有 `harness_commands` MCP，worker 应优先用它执行命令。Claude worker 另有 CLI 层的 Bash 许可：只读前缀（参数只能接在 `--` 之后）以及已配置命令中不含 `*`、换行的精确 argv；这些 Bash 调用不经 broker、不留命令记录，也不是 OS 隔离。`list_commands` 列出已授权的命令 ID 与 argv；`run_command({command_id})` 执行 `verification.commands` 和 `permissions.worker_commands` 中与 ID 对应的完整 argv，每次调用都是新的执行，不能附加参数或覆盖 cwd、env，不经 shell。字面 `*` 和换行原样传递；管道或重定向需要显式授权完整的 `["/bin/sh", "-c", "固定脚本"]`。每条命令的超时取 `runtime.verify_timeout_seconds`，且不超过父派发截止时间减 2 秒收尾余量。

`delete_path({path})` 只在 worker 派发有当前任务范围时出现，删除一个文件：`path` 是字面的仓库相对路径，必须精确等于 HEAD 中已跟踪的普通文件（模式 `100644` 或 `100755`），并匹配任务的 `allowed_paths`。路径最多 4,096 字符；拒绝绝对路径、空段、`.`／`..` 段、NUL、首段为 `.git`、`.harness`、`.codex`、`.claude` 或任一段为 `.git` 的路径（不分大小写，并按 Unicode 规范化比较），以及未跟踪文件、目录、符号链接和子模块；不删除目录或空的父目录，也不移动文件。

保证与不保证：

- 保证：路径从工作树根目录描述符逐级以 no-follow 打开；不跟随符号链接；最后一次核对时，最终项是 HEAD 中已跟踪、在任务范围内的普通文件；每次被准入的调用都留下记录。
- 不保证：在最后一次核对与 `unlinkat` 之间，能在工作树内改名的并发进程可以让被删除的文件位于别处，包括工作树外；事后的并发变化检测是尽力而为，可能漏报（例如目录被移回）。
- 删除立即生效。审阅和接受门禁只决定候选能否被接受，不能撤销删除。

每次调用（命令或删除）都先在 dispatch 的 `command-staging/` 中准备目录并写好完整的 `process.json`，再一次原子改名进 `commands/<32 位十六进制>/`；发布之前不启动子进程、不删除任何文件。发布失败或在发布前被杀时，`commands/` 里不会出现残缺目录，恢复不受影响；残留的准备目录不参与枚举，也不自动回收。命令记录包括 `output.stdout`、`output.stderr` 与 `receipt.json`；删除记录的 journal 命令 ID 为 `delete_path`，在最后一次核对前写入 `delete-intent.json`（路径、HEAD、原文件 SHA256 与字节数、时间），结束后写入 `receipt.json`，其 `outcome` 为 `deleted`、`refused`、`failed` 或 `concurrent_change`，并带 `deleted`、`sha256`、`bytes`、`concurrent_change`。只有 `deleted` 不是工具错误。每个派发的命令清单 `commands.json` 是 manifest 版本 2，含任务删除范围 `delete_scope`；版本 15 及更早 run 的清单是版本 1，只读路径不读清单，只核对 `commands/` 下的记录。所有恢复和清理都核对这些子进程记录，父 dispatch 退出不能证明命令已退出。

## 真实账号联调

以下入口使用配置中的真实模型与账号额度，与离线测试分开记录；离线测试不能证明这些调用已执行或通过。

```bash
python3 -m harness smoke
python3 -m harness smoke --commander fable
python3 -m harness.permission_smoke --config /path/to/harness.toml --repo /path/to/project
python3 -m harness.tool_smoke --config /path/to/harness.toml --repo /path/to/project
```

- `smoke` 在临时 Git 仓库由真实指挥生成计划，再调用执行者、任务审阅者和整体审阅者，运行真实测试；不改动本仓库。
- `permission_smoke` 在 `--repo` 的 `.harness/permission-smoke/` 建立专用现场，检查工作树内读写、精确命令、额外参数、工作树外读写和 `.git` 修改；通过要求实际工具调用、工具结果、对应的 `permission_denials` 和文件内容一致，模型自称“已拒绝”不算证据。专用命令替换项目命令，外部 MCP 关闭。默认超时 180 秒、最多 16 轮、预算参数 1 美元，可用 `--timeout`、`--max-turns`、`--max-budget-usd` 调整。
- `tool_smoke` 让配置的 Claude worker 调用 broker、文件工具、`git_query`（`ls-tree`、`hash-object` 与一条被拒绝的 argv）和 `delete_path`（范围内删除与范围外拒绝），核对工具结果与文件；默认最多 20 轮。

结果只覆盖这组现场和实际 CLI 版本，不证明完整命令白名单或 OS 隔离。已执行的联调记录在[验证记录](validation.md)。
