# 跨仓库操作、操作记录与观察网页

本文说明从 Harness 源码目录对其他项目执行任务的统一入口、外层 agent 的操作边界、自动操作记录与人工补充模板、问题报告模板、证据归属，以及本地只读观察网页的用法与限制。日常命令见 [README](../README.md)，恢复语义见[恢复协议](recovery.md#操作记录与观察事件)，设计边界见[设计取舍](design.md#运行观察与操作记录)。

- [统一入口](#统一入口)
- [外层 agent 操作说明](#外层-agent-操作说明)
- [操作记录与记录模板](#操作记录与记录模板)
- [报告模板](#报告模板)
- [证据归属](#证据归属)
- [观察网页用法与限制](#观察网页用法与限制)

## 统一入口

在 Harness 源码目录执行，所有路径使用绝对路径：

```bash
cd /abs/harness
python3 -m harness --config /abs/harness.toml doctor
python3 -m harness --config /abs/harness.toml --repo /abs/project run "目标描述"
python3 -m harness --config /abs/harness.toml --repo /abs/project resume RUN
python3 -m harness --repo /abs/project status
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
- 用 `status`、`status RUN`、`status RUN --full` 和 `dashboard` 观察，读取目标项目 `.harness/` 下的台账、事件、dispatch 产物与操作记录；
- 使用 README“状态、恢复与清理”表中明确适用于当前状态的恢复出口，例如 `--approve-plan`、追加精确命令授权后的普通 `resume`、`--environment-update`、`--operator-decision`、`--retry-stopped`。

遇到真正的新增权限（新的命令授权、范围扩展、预算或轮数提高）或需求选择（操作者决定、是否接受风险、是否放弃）时向用户请求，不自行代答。

不能做的事：

- 循环 `resume` 直到通过。同一快照上的拒绝、停止与非法结果不会因重复恢复而改变，重复恢复只消耗派发预算。
- 自动增加预算、超时、轮数或上下文上限来“解除”停止。
- 更换模型、供应商或 effort。
- 修改 `state.json`、`events.jsonl`、dispatch 产物或记忆库。
- 自行 merge／cherry-pick run 分支；最终集成由用户完成。

内部的 worker／reviewer 不调用该入口：它们在隔离工作树内完成一个任务或一次审阅，不启动新的 `run` 或 `resume`。

## 操作记录与记录模板

每次 `run`／`resume` 在加载配置之后、构造引擎之前，于 `<目标项目>/.harness/operations/<operation_id>.json` 独占创建一份记录。ID 默认自动生成（`op_` 加 16 位十六进制），也可用 `--operation-id ID` 指定：以字母或数字开头，只含 `A-Za-z0-9_.-`，最长 64 字符。同名记录已存在时拒绝并退出 1，不覆盖已有记录，也不写台账、不创建 run、不改 `.git/info/exclude`（只会确保 `operations/` 目录存在）。stderr 输出 `[harness] operation_id=<id> record=<路径>`；stdout 仍是单个 JSON，新增 `operation_id` 键。

记录不是第二套执行真相：它不取 `run.lock`、不写 run 台账，也不参与任何恢复判断。记录更新失败只在 stderr 打印 `[harness] 警告：...`，不改变运行、保存顺序或退出码。

### 自动记录的字段

| 字段 | 含义 |
| --- | --- |
| `operation_id`、`kind` | 记录 ID；`run` 或 `resume` |
| `status` | `prepared`（已创建）→ `running`（已关联 run）→ `finished`（CLI 结束） |
| `started_at`、`started_at_utc`、`finished_at`、`finished_at_utc` | epoch 秒与 ISO 8601 UTC |
| `arguments` | `run`：`goal`、`plan`（路径或 null）、`commander`、`pause_after_plan`、`step`；`resume`：`run_id`、`approve_plan`、`step`、`reverify`、`retry_stopped`、`confirm_exited`、`repair_invalid`、`environment_update`（路径或 null）、`operator_decision`（路径列表）、`allow_scope`。文件型选项只记路径 |
| `repo`、`repo_head` | 目标项目绝对路径与当时 HEAD；取不到为 null |
| `harness` | `package`、`revision`、`runtime_modified`、`python`、`executable`，与 `doctor` 回显相同 |
| `config` | `path`、`sha256`（配置文件字节摘要） |
| `asset_digest` | 共享资产摘要；失败为 null |
| `models` | 配置中 commander／fable／worker／reviewer 的 `cli`、`model`、`effort` 原样 |
| `cli` | `claude`、`codex` 的 `path`（PATH 查找结果）与 `version`（`--version` 首个非空行，5 秒超时，失败为 null） |
| `run_id`、`run_linked_at` | run 持久登记后立即写入；创建 run 前失败时为 null |
| `exit_status`、`result_status`、`error`、`error_type`、`error_omitted` | CLI 退出码（0／1／130）、最终 JSON 的 `status`、错误首行与异常类型名。本次调用含文件型输入（`--plan`、`--environment-update`、`--operator-decision`）且出错时，`error` 为 null 并写 `error_omitted: "file inputs present; see stderr"`，只保留 `error_type`，因为校验错误可能引用文件中的字段值；错误全文仍输出到 stderr。无文件输入时 `error_omitted` 为 null |

记录不保存环境变量值、凭据、`--plan`／`--environment-update`／`--operator-decision` 的文件内容或生成的 MCP 配置。

run 身份的关联：`run` 的首次台账保存随同写入可选字段 `created_by_operation`（不是额外一次保存），保存成功后追加 `run_created` 事件（带 `operation_id`）、在 stderr 输出一行 `[harness-json] {"event":"run_created","run_id":"...","operation_id":"..."}`，并立即把 `run_id` 写进记录，长时间运行或被强制终止时也能关联。`resume` 在自身预检门禁（含计划批准）通过后、进入 `execute()` 前同样追加 `run_resumed` 事件与通知。

`run_resumed` 不表示已经派发模型：缺少命令授权、等待操作者决定等执行阶段门禁仍可使它紧接一条 `run_stopped`，并沿用原有停止记账和 on_error hook。恢复预检拒绝时不写 `run_resumed`、不输出该通知，但之前保存的退出确认审计、台账升级、配置放宽、命令授权检查等记录仍保留。已完成 run 的普通 resume 不写运行事件，台账与事件文件保持不变。完整边界见[恢复协议](recovery.md#操作记录与观察事件)。

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

run 列表 → run 详情 → 文件查看。详情包含：目标、台账状态、分支、工作树（仅显示路径）、指挥、当前任务、已接受任务、台账版本、创建该 run 的操作记录、首／末事件时间；预算；等待操作者决定、环境阻塞、缺失命令授权、worker 结果计数；任务表；费用与调用分类；dispatch 表（含产物文件链接）；关联本 run 的操作记录；事件尾部；文件索引。文件查看显示文本尾部，默认 64 KiB，最大 1 MiB，可调整。页面有刷新按钮和每 10 秒自动刷新选项，界面为中文；`status`（无 run ID）的列表与网页读同一份投影，stdout 字段不变。

脚本可直接读 JSON 接口：`/api/runs`、`/api/runs/<run_id>`、`/api/runs/<run_id>/events?tail=`、`/api/runs/<run_id>/files`、`/api/runs/<run_id>/files/<相对路径>?tail=&format=json`。无效 ID 或参数 400，拒绝的路径与 Host 不匹配 403，不存在的 run 或文件 404，GET／HEAD 以外的方法 405；台账可读但摘要失败的 run 仍返回 200 并带 `summary_error`。

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

### 费用

费用按供应商分列，以台账 dispatch ID 为唯一累计单位；事件中的用量不再相加，重复的 dispatch ID 只计一次并列出。

- Claude 显示的是 CLI 报告的估算费用，不是账户账单。
- Codex 不报告美元费用，显示“不可用”。
- 没有任何合法费用字段时显示“不可用”而不是 0；不给全 run 合计。
- token 只在 dispatch 行逐条显示原字段，不跨 dispatch 或供应商相加。
- 按组：协调开销（role 为 commander 的 plan／dispatch／replan，按角色识别）、各任务（worker／reviewer 按 `task_id`）、验证。失败或超时的调用只要有合法报告费用也计入。

### 文件访问

只能通过固定根读取 run 目录内的普通文件：顶层 `state.json`、`events.jsonl`、`handoff.json`、`handoff.md`、`ignored-baseline.json`、`ignored-paths.json`；目录 `plans/`、`dispatches/<uuid>/`（含 `commands/<32 位十六进制>/`）、`verification/`、`hooks/`、`recovery/`、`decisions/`、`environment/`、`checkpoints/`、`path-indexes/`、`indexes/`、`batches/`。`mcp.json`、`run.lock`、`*.tmp` 与 `memory.sqlite3*` 被排除；符号链接与非普通文件一律拒绝，中间目录在检查后被替换为符号链接也无法逃出根目录；路径深度不超过 8。台账中的绝对路径只显示不打开。文件内容永远以 `text/plain`（或 `format=json`）返回，日志不会作为 HTML 执行。

### 限制

- 只绑定 127.0.0.1，不开放远端访问；只接受 GET／HEAD；同时连接最多 8 个，超出返回 503；慢客户端约 10 秒后断开。
- 服务在启动时固定持有 `.harness` 目录的描述符；目录被删除重建后需重启服务。目标项目还没有 `.harness` 目录时 `dashboard` 直接报错退出 1，而 `status` 列表为空。`.harness/runs` 本身是符号链接或不可读时，`status` 列表输出包含目录路径的诊断并退出 1；`status RUN` 保留既有的直接台账读取方式，不使用列表的符号链接拒绝规则，但仍须能读取该文件。
- 首／末事件时间只在 `events.jsonl` 头／尾 64 KiB 内查找；dispatch 结束时间只取尾部 64 KiB 中的事件，较早调用可能显示不可用；`state.json` 超过 64 MiB 不解析；操作记录最多扫描 1000 个文件、每个 256 KiB；文件索引最多 2000 项。
- 损坏的台账在列表中跳过并给诊断；详情仍尽量显示并标出 `ledger_error`／`summary_error`；旧 `schema_version` 没有的区块标“不可用”。
- 不汇总多个项目，不按公开单价估算账单，观察结果不改变任何预算。页面尚未做过浏览器人工验收，当前验证只覆盖 HTTP 接口。
