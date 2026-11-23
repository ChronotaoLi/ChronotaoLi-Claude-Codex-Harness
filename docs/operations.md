# 跨仓库操作、操作记录与观察网页

本文说明对其他项目执行任务的统一入口（各命令读哪份配置）、外层 agent 的操作边界与它可加载的 skill、自动操作记录与人工补充模板、问题报告模板、证据归属、run 目录的文件布局、本地只读观察网页、角色工具接口、依赖副本、单次监督和真实账号联调。日常命令见 [README](../README.md)，停止后的出口见[恢复与操作者出口](recovery.md)，观察层边界见[设计：不变量](design.md#不变量)。

- [统一入口](#统一入口)
- [外层 agent 操作说明](#外层-agent-操作说明)
- [操作记录与记录模板](#操作记录与记录模板)
- [报告模板](#报告模板)
- [文件布局](#文件布局)
- [证据归属](#证据归属)
- [观察网页用法与限制](#观察网页用法与限制)
- [角色工具参考](#角色工具参考)
- [worker 结果与审阅问题](#worker-结果与审阅问题)
- [依赖副本清单](#依赖副本清单)
- [单次监督](#单次监督)
- [真实账号联调](#真实账号联调)

## 统一入口

editable 安装（见 [README](../README.md#安装入口)）之后在目标项目里直接调用 `harness`；在 Harness 源码目录里调用时用 `python3 -m harness` 并写 `--repo`，两者等价：

```bash
cd /abs/project
harness doctor
harness run "目标描述" --scope 'src/**' --scope 'tests/**'
harness resume RUN
harness status
harness watch RUN
harness dashboard --port 8765

cd <Harness 源码目录>
python3 -m harness --repo /abs/project run "目标描述" --scope 'src/**'
```

无论哪种方式都固定使用同一个解释器：`doctor` 与操作记录的 `harness.python`／`harness.executable` 记录实际解释器，换解释器可能加载另一份安装的 `harness` 包。

- `--config`、`--repo` 是全局参数，必须写在子命令之前；写在子命令之后会被 argparse 当作未识别参数拒绝。`--repo` 默认是当前目录，指向仓库里的任何目录都按它的 Git 根目录处理。
- `--config` 只选操作者配置，不传时是 Harness 源码目录的 `harness.toml`；项目配置固定在 `<目标仓库 Git 根目录>/.harness/project.toml`，没有参数能改。两份配置的内容与合并规则见 [README](../README.md#两份配置)。各命令读取的文件：

| 命令 | 操作者配置 | 项目配置 |
| --- | --- | --- |
| `run`、`resume`、`permission-diagnostics` | 读 | 读，必须存在 |
| `abandon` | 读 | 不读 |
| `doctor` | 读 | 读取并校验，只为报告顶层的 `project` 字段，不进入 `doctor` 的有效配置 |
| `demo`、`smoke`、`sync`，以及 `python3 -m harness.permission_smoke`、`python3 -m harness.tool_smoke` | 读 | 不读 |
| `status`、`watch`、`dashboard`、`memory`、`prepare-run`、`cleanup-run`、`supervise` | 不读 | 不读 |

- 项目配置不存在时，`run`、`resume`、`permission-diagnostics` 拒绝：退出码 2，停止类型 `invalid_input/cli_arguments`，`data.option` 为 `--repo`，错误信息给出期望的绝对路径并指向 `examples/project.toml`。这个拒绝在任何写入之前：没有操作记录，不安装 `.harness/` 的忽略保护，不创建 run、分支或工作树，不调用模型。项目配置存在但读不出、不是合法 TOML 或内容不合格时，与操作者配置无效时一样报错（退出码 1），同样在任何写入之前。
- `abandon` 不读项目配置，项目配置缺失或损坏时照常可用。`demo`、`smoke` 与两个独立联调入口自己设置验证命令和 worker 命令，目标仓库里即使有项目配置也不读取、不执行其中的命令。`supervise` 把 `--config` 原样传给它启动的子进程。
- `run` 启动时在 stderr 依次回显三条：`[harness] config=<操作者配置路径>`、`[harness] project=<项目配置路径>`、`[harness] verification=<验证命令>`。`verification` 这一条的值是缩进排版的 JSON，可以跨多行（续行没有 `[harness]` 前缀）。项目配置显示的总是固定位置：它是符号链接时也不显示链接目标。
- 共享资源（rules、skills、mcp）相对操作者配置所在目录解析；复制操作者配置到其他位置时同步修正这些路径。`doctor` 的 `shared` 字段显示解析后的绝对路径。
- 启动前用 `doctor` 核对实际加载的包与配置：`harness.package`（包目录）、`harness.revision`（包目录上级是 Git 检出根时的 HEAD，否则 null）、`harness.runtime_modified`（`harness/` 下有已修改或未跟踪文件时为 true）、`config`、`project`（见下）、`asset_digest`（共享资产摘要；失败为 null 并另给 `asset_error`）。同名模块、site-packages 中的另一份安装或改过的工作副本都会在这里显现。`doctor` 的退出码仍只取决于配置引用的 CLI 是否已登录，项目配置缺失或无效不会让它失败。
  - `config` 是 `{path, sha256, project}`，字段含义与[操作记录](#自动记录的字段)相同；`doctor` 不把项目配置当作有效配置，所以这里的 `config.project` 总是 null。
  - `project` 报告 `--repo`（默认当前目录）对应的项目配置：`{path, exists, sha256, error}`，`path` 总是期望的绝对路径；`--repo` 不在 Git 仓库里时整个字段为 null。文件不存在：`exists=false`，`sha256` 与 `error` 为 null。存在且能与当前操作者配置一起加载：`exists=true`，有摘要，`error=null`。存在但读不出：`exists=true`，`sha256=null`，`error` 是错误首行。读得出但不是合法 TOML 或内容不合格：`exists=true`，有摘要，`error` 是错误首行。摘要与错误来自同一次读取，描述的是同一份字节。
- 启动前核对目标仓库：已有提交，已跟踪文件与暂存区没有未提交修改，`verification.commands` 与依赖在工作树中可用，每条验证命令的 `cwd` 在 HEAD 中存在、每条准备命令的 `cwd` 在 HEAD 中存在或由前面的准备命令创建（命令带目录 `{cwd, argv}`，目录不存在不是配置错误，执行时这条命令不启动），生成产物已忽略。这些要求见 [README](../README.md#执行)。
- 每次 `run`／`resume` 自动写一份操作记录，可用 `--operation-id ID` 指定记录 ID；见[操作记录与记录模板](#操作记录与记录模板)。

## 外层 agent 操作说明

外层 agent 指替所有者调用上述入口的自动化会话（例如在目标项目里打开的另一个 Claude Code 或 Codex 会话）。本节的规则另有可加载的形式，即[外层 skill](#外层-skill)，两者内容一致；停止后的出口以[恢复与操作者出口](recovery.md)为准。

**首次在一个项目里使用。** `harness` 不在 `PATH` 上时，外层会话不自己安装，把 `uv tool install --editable <Harness 源码目录>` 交给所有者。先运行 `doctor`（不调用模型），核对 `harness.package`、`harness.revision` 是预期的 Harness，`checks.<cli>.logged_in` 为 true，再看 `project`。`project.exists` 为 false 时，外层会话以 `examples/project.toml` 为起点起草完整的项目配置，把完整内容和绝对路径（`project.path`）给所有者看，所有者确认后才写入（Git 根目录下还没有 `.harness/` 时先建这个目录）：其中的验证命令与 worker 命令授权就是授予 worker 的执行能力。所有者已经明确给出内容时不必再问。`project.error` 非空时，把错误给所有者看，经同意修好之后再 `run`。不要 `git add` 或提交 `.harness/`（见 [README](../README.md#两份配置)）。

它在已有授权内可以：

- 启动 `run`，并为每次 `run`／`resume` 指定可追溯的 `--operation-id`；启动前先与所有者确认允许改动的区域（目录级）、必要的测试与夹具、生成文件的位置，再用 `run --scope` 提交为范围边界（没有声明时 `status RUN` 的 `scope.note` 会写明首版不受约束）；启动后核对 stderr 回显的两份配置与验证命令；
- 用 `status`、`status RUN`、`status RUN --full`、`watch RUN` 和 `dashboard` 观察，读取目标项目 `.harness/` 下的台账、事件、dispatch 产物与操作记录；`status` 的三种形式把检出与状态目录写在 stderr 第一行（条件与格式见[页面](#页面)），这一行不进 stdout，需要知道一份结果来自哪个检出时保留这份 stderr；
- 使用[阻塞状态与操作者出口](recovery.md#阻塞状态与操作者出口)中明确适用于当前状态的恢复出口，例如 `--approve-plan`、加入精确命令授权后的普通 `resume`、`--environment-update`、带着所有者答复的 `--operator-decision`；
- `provider_failure/transport`（与供应商的连接失败）之后不问所有者执行 `stop.exits` 里的普通 `resume`，同一 run 连续至多 3 次：只依据本会话自己运行的调用返回的停止（stderr 里 `event` 为 `stop` 的那一行，或它的操作记录），不依据 `status RUN` 显示的台账停止；每次用新的、唯一的 `--operation-id`，按下面“长任务与受限沙箱”的规则启动，交给所有者在终端运行的那次 `resume` 不算外层自己的；第一次这样恢复的传输停止算第 1 次，本会话运行的调用返回同一 run 的其他停止、或所有者作出决定时清零，`busy/run_lock` 不清零，也从不作为再次恢复的理由（另一个进程可能仍持有该 run）；计数只在本会话里，新会话从零开始；第 4 次连续的传输停止不再恢复，报告所有者检查网络或代理。

**长任务与受限沙箱。** `run` 和需要继续执行的 `resume` 可能运行很久：用外层工具自己的后台任务启动，再用 `status RUN`、`watch RUN` 观察。`&`、`nohup` 不保证进程存活，不要依赖它们。外层工具没有可靠的后台任务，或者它的沙箱不允许联网、写 `.git`、启动 `claude`／`codex` 子进程时，这两类命令都交给所有者在普通终端里运行：外层会话给出完整命令，自己负责观察、准备输入文件（操作者决定、环境更新、请求拒绝）和提出下一条命令。普通 `resume RUN` 在退出证据齐全时会直接继续执行，所以中断（130）之后的它也按这条规则启动。`dashboard` 在启动时打开一个状态目录，直到停止都不换，并在打印地址之前把它与检出写在 stderr，页眉显示同样的检出与状态目录：启动时用 `--repo` 指向 run 所在的检出，把地址交给所有者时说明它显示的是哪个检出；默认端口可能已被另一个 `dashboard` 占用时用 `--port 0`，端口被占用是启动错误，不会改用别的端口。

读停止时用退出码与 `stop.kind` 分流，不解析文字：3 按 `stop.exits` 中适用于当前状态的出口处理，涉及新增权限或需求选择时请示所有者；2 修正本次调用的输入（例如补上缺失的项目配置）；1 停下报告（证据或现场不符、Harness 自身失败、未分类）；0 读结果 JSON 的 `status`（`completed`、`paused` 或待批准）；130 是中断。本次调用失败时先读它自己的停止，即 stderr 里以 `[harness-json] ` 开头、`event` 为 `stop` 的那一行；`status RUN` 显示的是台账里的停止，标“（操作）”的拒绝不改台账，所以那里可能还是更早的停止，没有创建 run 的拒绝则根本没有台账。供应商额度类停止（`provider_quota/*`）与 `provider_failure/{overloaded,authentication}` 都是 3：等额度恢复，或由所有者修好认证与权限（`doctor` 只检查、不修复）之后，按 `stop.exits` 普通 `resume`，worker 的这次调用不计工作尝试；不要用正则匹配错误文字判断额度。Claude 角色的额度用尽默认由 Harness 在进程内等待（`runtime.quota_wait_max_seconds`，见[额度等待](recovery.md#额度等待)）：外层不要自己睡眠后循环 `resume`；停在 `provider_quota/usage_limit` 时先读 `stop.data.wait_reason`。`resume`／`abandon` 返回 `busy/run_lock` 可能是那个进程正在等额度：看 `status RUN` 的 `quota_wait`（等到何时、哪个窗口），记录不证明进程仍在；要取消就中断那个进程。

先请示所有者，不自行代答：

- 新增权限：项目配置 `permissions.worker_commands` 里的命令授权（每条是 `{cwd, argv}`；恢复时只能加入，已有条目可以重排，不能删除或改目录）、范围扩展、提高预算、超时、轮数或上下文上限；
- 需求选择：答复 `pending_decisions`、是否接受风险、是否放弃；
- 指挥提案被持有（`scope_extension/*`）时，`resume --scope` 扩大边界与 `--reject-request` 拒绝提案：边界是持续、跨任务的授权，停止给出的建议条目可能比缺口宽（`data.widened`），完整缺口在 `status RUN` 的 `scope.held`；
- 创建或修改 `.harness/project.toml`，以及启动前的范围；两次调用之间对任一份配置的修改只能是[通用规则](recovery.md#通用规则)允许的那些。

不能做的事：

- 循环 `resume` 直到通过。同一快照上的拒绝与停止不会因重复恢复而改变；读不出否决的无效输出由新调用重做（每次请求最多重做一次）；重复恢复只消耗派发预算。唯一的例外是上面 `provider_failure/transport` 之后有次数上限的恢复。
- 自动增加预算、超时、轮数或上下文上限来“解除”停止。
- 更换模型、供应商或 effort。
- 修改 `state.json`、`events.jsonl`、dispatch 产物或记忆库。
- 自行 merge／cherry-pick run 分支；最终集成由所有者完成。
- 代所有者回答操作者问题。

内部的 worker／reviewer 不调用该入口，也不使用外层 skill：它们在隔离工作树内完成一个任务或一次审阅，不启动新的 `run` 或 `resume`。

### 外层 skill

`operator/harness-operator/SKILL.md`（frontmatter 的 `name` 是 `harness-operator`）是上面这些规则的可加载形式，供 Claude Code 或 Codex 的外层会话使用：正文用英文（与 `shared/skills/` 下的角色 skill 相同），`description` 带中文触发词。分发方式是把这个目录链接进两家的用户级 skill 目录，没有安装子命令：

```bash
mkdir -p ~/.claude/skills ~/.codex/skills
ln -s <Harness 源码目录>/operator/harness-operator ~/.claude/skills/harness-operator
ln -s <Harness 源码目录>/operator/harness-operator ~/.codex/skills/harness-operator
```

它不放在 `shared/skills/` 下：那个目录会作为技能目录提供给内部角色（`skill_list`、`skill_read`），也计入共享资产摘要。它不替代本文与[恢复与操作者出口](recovery.md)。Harness 启动的角色在启动参数里关闭这份 skill：Claude 担任的角色带 `--disable-slash-commands`，Codex 担任的角色带 `-c 'skills.config=[{"name" = "harness-operator", "enabled" = false}]'`；保证与不保证见[设计的不变量 27](design.md#不变量)，不保证的部分与真实账号下的检查见[隔离与进程](design.md#隔离与进程)。

## 操作记录与记录模板

每次 `run`／`resume` 在加载两份配置之后、构造引擎之前，于 `<目标项目>/.harness/operations/<operation_id>.json` 独占创建一份记录。ID 默认自动生成（`op_` 加 16 位十六进制），也可用 `--operation-id ID` 指定：以字母或数字开头，只含 `A-Za-z0-9_.-`，最长 64 字符。ID 形式不合格或同名记录已存在时以 `invalid_input/cli_arguments`（`data.option` 为 `--operation-id`）拒绝，退出码 2，不覆盖已有记录，也不写台账、不创建 run、不改 `.git/info/exclude`；重复 ID 检查在公共 Git 锁内、保护规则变更前完成。stderr 输出 `[harness] operation_id=<id> record=<路径>`；stdout 仍是单个 JSON，新增 `operation_id` 键。

记录不是第二套执行真相：它不取 `run.lock`、不写 run 台账，也不参与任何恢复判断。记录更新失败只在 stderr 打印 `[harness] 警告：...`，不改变运行、保存顺序或退出码。

### 自动记录的字段

| 字段 | 含义 |
| --- | --- |
| `operation_id`、`kind` | 记录 ID；`run` 或 `resume` |
| `status` | `prepared`（已创建）→ `running`（已关联 run）→ `finished`（CLI 结束） |
| `started_at`、`started_at_utc`、`finished_at`、`finished_at_utc` | epoch 秒与 ISO 8601 UTC |
| `arguments` | `run`：`goal`、`plan`（路径或 null）、`commander`、`pause_after_plan`、`step`、`scope`（列表）；`resume`：`run_id`、`approve_plan`、`step`、`reverify`、`confirm_exited`、`environment_update`、`execution_guidance`、`reject_request`（后三项为路径或 null）、`operator_decision`（路径列表）、`scope`（列表）。文件型选项只记路径 |
| `repo`、`repo_head` | 目标项目绝对路径与当时 HEAD；取不到为 null |
| `harness` | `package`、`revision`、`runtime_modified`、`python`、`executable`，与 `doctor` 回显相同 |
| `config` | `path`、`sha256`：操作者配置的路径与字节摘要；`project`：本次加载读了项目配置时是 `{path, sha256}`（路径是固定位置），否则 null（`run`／`resume` 总会读）。摘要都来自加载时解析的那一份字节，记录时不再读文件，所以加载之后文件再被改写，记录里仍是当时的摘要 |
| `asset_digest` | 共享资产摘要；失败为 null |
| `models` | 配置中 commander／fable／worker／reviewer 的 `cli`、`model`、`effort` 原样 |
| `cli` | `claude`、`codex` 的 `path`（PATH 查找结果）与 `version`（`--version` 首个非空行，5 秒超时，失败为 null）；这是创建记录时的读取，与额度探测无关 |
| `run_id`、`run_linked_at` | run 持久登记后立即写入；创建 run 前失败时为 null |
| `exit_status`、`result_status`、`error`、`stop`、`error_omitted` | CLI 退出码（0／2／3／1／130，见[退出码](recovery.md#退出码)）、最终 JSON 的 `status`、错误首行与停止投影（`kind`、`reason`、标题、退出码、出口命令、`data`、`source`、诊断摘录 `detail`；结束时 run 停在待批准上时也有）。本次调用含文件型输入（计划或恢复 JSON，包括执行指导与请求拒绝 `--reject-request`）且出错时，`error` 为 null 并写 `error_omitted: "file inputs present; see stderr"`，`stop` 去掉 `detail`、保留其余字段，因为校验错误可能引用文件中的字段值；错误全文仍输出到 stderr。无文件输入时 `error_omitted` 为 null |

记录不保存环境变量值、凭据、计划或恢复 JSON 的文件内容或生成的 MCP 配置。

run 身份的关联：`run` 的首次台账保存随同写入可选字段 `created_by_operation`（不是额外一次保存），保存成功后追加 `run_created` 事件（带 `operation_id`）、在 stderr 输出一行 `[harness-json] {"event":"run_created","run_id":"...","operation_id":"..."}`，并立即把 `run_id` 写进记录，长时间运行或被强制终止时也能关联。`resume` 在自身预检门禁（含计划批准）通过后、进入 `execute()` 前同样追加 `run_resumed` 事件与通知。

`run_resumed` 不表示已经派发模型：缺少命令授权、等待操作者决定等执行阶段门禁仍可使它紧接一条 `run_stopped`（携带停止投影 `stop`）。命令失败时，stderr 里 `harness: ` 加错误全文之后紧跟一行 `[harness-json] {"event":"stop","run_id":…,"operation_id":…,"stop":…}`（与 `status RUN` 的 `stop` 同一投影，不含 `detail`）；写这一行失败不改变退出码与操作记录。这两部分之前有 `[harness] config=…`、`[harness] project=…`、`[harness] operation_id=…`、运行事件与 `run_created` 通知等行，之后还可能有输出（例如 `supervise` 的监督回执），所以外层脚本应找以 `[harness-json] ` 开头、`event` 为 `stop` 的那一行，不按行号读。恢复未进入执行时不发布 `run_resumed`。已保存的退出确认审计仍保留；前置门禁失败不会采纳本次配置放宽。前置门禁通过后若仍等待计划批准，已采纳的配置放宽和命令授权检查记录也保留。已完成 run 的普通 resume 不写运行事件，台账与事件文件保持不变。操作记录缺失、损坏或与台账不一致都不影响 resume、abandon 或清理。

上述拒绝或已完成返回只要对应台账文件存在，操作记录结束时仍关联参数中的 run ID。加载配置失败（含项目配置缺失或无效）时没有记录（记录在 `Config.load` 之后才创建）；创建 run 之前失败（仓库基线不满足、计划非法等）时记录保留但没有 `run_id`；resume 在预检阶段被强制终止时记录也可能没有 `run_id`。

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

## 文件布局

```text
<project>/.harness/
  project.toml             # 项目配置（操作者维护，运行时只读）
  memory.sqlite3           # 项目记忆
  generated/               # sync 生成的 MCP 客户端配置
  operations/<op_id>.json  # 每次 run/resume 的自动操作记录
  supervision/<id>.json    # supervise 回执
  runs/<run_id>/
    state.json             # 台账：状态、预算、进程、阶段与证据引用
    events.jsonl           # 追加事件
    handoff.json           # 任务边界交接检查点
    worktree-cleanup.json  # cleanup-run 对分支工作树的删除意图与完成记录
    dispatches/<uuid>/     # 一次调用：输入（worker 的 packet.json、schema.json）、CLI 输出与进程记录、结果、封存、commands/
    verification/          # 验证命令、日志、diff、快照；全新检出的 checkout.json 与 cleanup.json
    preflight/             # 候选预检结果
    plans/                 # 计划版本 revision-N.json；plans/rejected/ 归档被拒提案预写的计划文件
    proposal-rejections/   # 指挥提案的拒绝记录
    recovery/              # 修订归档的未接受修改
    continuations/ continuation-rejections/ execution-guidance/  # 续作类不可变记录
    environment/           # 环境更新与只读前置检查
    decisions/             # 来源绑定的请求与答复
    checkpoints/           # 检查点、批次与原始证据副本
    path-indexes/ input-indexes/ finding-progress/  # 路径、输入与问题进度的摘要索引
    observations/quota/    # 额度样本（见“额度”）
    workspace-inputs*.json # prepare-run 的依赖副本清单与回执
  worktrees/<run_id>/      # 隔离分支中的代码
```

`operations/` 与 `supervision/` 只是观察信息，不参与恢复判断；台账的 `created_by_operation` 与 `run_created`／`run_resumed` 事件把 run 关联到操作记录。离线开发验证的产物放在 Harness 仓库的 `.harness/validation/<批次>/`，不进版本库。

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

启动成功后、打印地址之前，stderr 先写一行位置 `harness dashboard: 检出 <检出>；状态目录 <状态目录>`。状态目录是给定路径解析（`Path.resolve()`，符号链接已解析）后的路径，服务在启动时打开它，直到停止都不换。`--repo` 入口的检出是 `--repo` 所在仓库的 Git 根（`git rev-parse --show-toplevel` 解析后的路径）。`python3 -m harness.dashboard --state` 不调用 Git：解析后的目录名是 `.harness` 时写 `检出（按状态目录路径推定，未核实）<上级目录>`，否则写 `检出 未知`。路径中的不可打印字符（含换行）与反斜杠按 Python `repr()` 的写法转义、不加引号，所以位置总是一行；JSON 里的 `observed` 保留原路径。状态目录不存在、不能以读权限打开（包括只有搜索权限）或路径无法解析时（符号链接环与含 NUL 字节的路径都在其中）拒绝启动：退出 1，stdout 为空，不写位置行。

它是只读、本机、单项目多 run 的视图：

- 不启动、恢复或修改 run，不取 `run.lock`，不写任何文件，不启动子进程，不探测进程；启动前只解析一次 `--repo` 的仓库根，服务开始后不调用 Git。额度只读 run 目录里已保存的样本，刷新不启动任何 CLI。
- 台账记为 `running` 不等于进程仍在工作；页面只显示 `process_state`、`exit_confirmed`、`pgid` 与最后事件时间。没有新日志不等于卡死；worker `completed` 不等于任务 `accepted`。额度等待期间状态仍是 `running`，详情首屏状态标记那一行之下多一块“额度等待记录：<窗口>”，写明等到何时、重置时刻、来源调用、记录时刻与取消方法；它是台账记录，不证明进程仍在等待（被强杀时遗留，下一次进入执行时清除），不在“需要处理”区，也不计入待处理数量。
- 各文件按请求逐个读取，显示采样时间而不是原子快照；运行中的 `events.jsonl` 最后一行可能未写完，页面标为未完成，不算错误。
- Claude 的结构化输出可能在调用结束才出现：运行中的 dispatch 还没有返回结果或用量是正常的。

### 页面

run 列表 → run 详情 → 文件查看。列表按最近事件排序，以卡片显示台账状态、目标摘要、分支与最近事件时间，并汇总需要关注／进行中／已结束的数量。详情首屏只放判断所需的信息：目标摘要、状态标记与台账阶段（见下文 `ledger_stage`），额度等待期间另有额度等待记录；“需要处理”区（摘要或台账读取失败；停止的类型、诊断摘录、调用失败的结构化事实（停止的 `data` 里有这些事实时才显示）与出口命令，台账为 blocked 而没有停止投影时的阻断提示；运行中断或暂停；计划等待批准；等待操作者决定；当前环境阻断；缺失命令授权；续作请求；并附常用出口），没有这些项时不显示该区；有返工提示时，其后是单独的返工提示区（见下文）；台账状态、已接受／总任务进度、当前任务、最近观测与待处理数量（只数“需要处理”区的项）；任务列表与最近活动。当前任务最近一次调用（不含验证命令）的记录带错误时，任务列表里这个任务的“最近调用”之下显示错误的首行与 dispatch ID；不论 run 状态，它都不单列为“需要处理”的项。长目标、决定问题和事件字段默认只显示有界摘要，“查看完整原文”展开后显示未截断的原文。

预算与额度、调用记录（逐行展开查看进程、用量与产物文件链接）、事件日志、操作记录、运行信息（工作树、台账版本、操作记录、worker 结果计数、历史环境阻断等）和文件索引（可按路径筛选）收在标签页中，可用方向键、Home、End 切换；“如何理解这些状态”展开后给出台账与进程、完成与接受的区别。文件查看显示文本尾部，默认 64 KiB，最大 1 MiB，可调整。

同一 run 的事件刷新和手动刷新保留当前标签、已展开项、文件筛选、页面与原文框／标签栏／表格的滚动位置以及键盘焦点（隐藏标签或收起项中的位置在再次显示时恢复；同一文件的刷新保留阅读位置）；切换到其他 run 或文件时恢复默认。详情默认开启“跟随运行变化”，收到原生文件通知后刷新；列表与文件查看使用刷新按钮，不定时轮询。连接断开会显示提示，手动刷新可重新订阅。

界面为中文，跟随系统浅色／深色并适配窄屏；`status`（无 run ID）的列表与网页读同一份投影，stdout 字段不变。不是当前台账版本（20）的 run 目录不进入列表：列表页的台账诊断与 `status` 的 stderr 只给出它的路径、版本和“不支持”，不读其他字段；打开它的详情返回 409，页面显示同一句拒绝。

页眉写出本页读取的检出与状态目录：`检出 <路径>`（检出是按路径推定的写 `检出（按路径推定）<路径>`，未知的写 `检出 未知`）加 ` · 状态目录 <路径>`；浏览器标题是 `<name> · Harness 观察台`（`name` 见下面的 `observed`）。当前视图（列表、详情或文件查看）最新主请求返回带 `observed` 的 JSON（含错误体）时，页眉与标题换成它的 `observed`；纯文本错误、网络失败等没有 `observed` 的失败保留已显示的值，详情另取文件索引的请求也不改它们；收到首份带 `observed` 的响应之前，标题是 `Harness 观察台`，页眉为空。详情或文件查看得到 404 时，错误带 `observed` 就提示这个 run（或文件）不在页首所示的状态目录里（详情另说明它可能属于另一个检出），不带时只说没有找到。详情的文件索引由另一个请求取得：它的 `observed.state` 与详情的相同才显示并缓存；不同时（例如端口背后的服务换成了另一个状态目录）不显示，文件面板写出两边的状态目录并提示刷新，文件数显示“…”；缓存的索引与本次详情的状态目录不同时，在新索引到达之前也不显示。`status`、`status RUN`、`status RUN --full` 在 run ID 格式检查之后、读台账或列表之前，把位置写在 stderr 第一行（`harness: ` 加与 `dashboard` 相同格式的位置，检出总是 `--repo` 的 Git 根；状态目录没能打开时末尾加 `（无法打开）`），之后才是诊断或停止行；位置行不进 stdout。

返工提示 `convergence` 只读台账，以工作尝试为单位。对每个未接受任务，`review_history` 里的对象记录按顺序从 1 编号（与 `review_rounds` 的轮次相同），按 `attempt` 分组（`attempt` 不是整数的记录各成一组），一组是一次尝试，它的结论这样求：`pending_task` 的 `task_id` 是这个任务、`attempt` 是这一组的尝试号时，这次尝试还在它的阶段里，没有结论，组里已登记的记录（例如同一候选上已批准的中间批）不计数、不比较；否则取组里编号最大的记录：`status` 为 `invalidated` 是失效；没有审阅对象，或 `status` 为 `invalid_rejected`、带 `validation_error` 或 `disposition_errors`，或 `verdict` 不是 approve，或 `findings` 里有 blocking 项，是否决（所以原始 `verdict` 为 approve 的记录也可能是否决）；其余是批准，但只有 `pending_acceptance` 的 `task_id`、`attempt` 与这一组相同、`review_source` 与这条记录的相同（两者都是对象）时才算结论，否则这次尝试没有结论（例如阶段在最后一批之前结束）。

有效工作尝试（`work_attempts`）达到 3 次，或最近两个结论都是否决、两条记录的 `findings` 有共同的 blocking 标签时提示。任务提示的字段：`work_attempts`；`review_rejections`（结论为否决的尝试数）；`repeated_labels`（最近两个结论都是否决时两条记录 blocking 标签的交集，排序后至多 20 个，`repeated_labels_truncated` 标出截断）；`reasons`（成立的触发条件，依次是 `work_attempts`、`repeated_labels`）；`compared_reviews`（`repeated_labels` 非空时是这两个结论，先早后晚，否则为空列表）；`latest_review`（最近一个结论，没有时为 null）；`latest_attempt`（台账 `attempts` 里这个任务的值，即最新的尝试编号，不是整数时为 null）；`attempts_since_latest_review`（尝试编号之差：`latest_attempt` 减去最近结论的尝试号；没有结论时等于 `latest_attempt`；任一方不是整数或差为负时为 null）。被豁免的 worker 调用也占一个尝试编号而不计入 `work_attempts`，所以这个差可以大于其间新增的工作尝试。结论写成 `{round, attempt, review_dispatch_id, snapshot_digest, verdict, outcome}`：前五个与 `review_rounds` 的行同取法（`verdict` 是审阅原写的值），`outcome` 是 `rejected`、`approved` 或 `invalidated`。只因工作尝试次数触发时 `compared_reviews` 为空，`latest_review` 不受影响。标签是审阅者写的文字，不是问题的身份；以原键 `open` 指出的旧问题写在 `finding_dispositions` 里，不参与比较，审阅只以 `open` 指出旧问题、不再新报时，它不进入 `repeated_labels`。提示不说明进程是否在执行；台账最后记录的步骤见下文 `ledger_stage`。整体验收在 `final_history` 里 `status` 为 `rejected` 或 `invalid_rejected` 的记录至少 2 条、最后一条记录不是 `approved` 时提示：`review_rejections` 是这些记录的条数，`latest_rejection` 是其中最后一条的 `{attempt, review_dispatch_id, snapshot_digest}`（dispatch 取 `review_source.dispatch_id`），`latest_final_attempt` 是 `final_history` 最后一条的 `{attempt, status, phase}`；这两项照录台账的值，`attempt` 不是整数、其余不是字符串时记为 null。

`status RUN`、`watch RUN` 与详情中的 `convergence` 都是建议，不自动改变预算、范围、派发或接受条件；completed／abandoned 不显示返工警告。详情页把提示放在“需要处理”区（没有该区时是首屏卡片）之后、概览之前的单独区域，不计入待处理数量：每个任务一项，写工作尝试与否决次数、比较依据（两轮各自的轮次、尝试号、审阅 dispatch 的短 ID 与共同的标签；只因次数触发时写明没有可比较的标签）、最近一个结论属于第几次尝试与第几轮、是什么结论，以及最新尝试编号与非零的编号差（没有结论或编号不可用时只写这一点）；整体验收一项写拒绝次数、最近一次拒绝的整体尝试号与审阅 dispatch、最近一次整体尝试的尝试号与台账记录的 `status`、`phase`。

审阅轮次投影 `review_rounds` 按任务列出 `review_history` 的每一轮（按记录顺序从 1 编号，含失效或结构非法的记录）：`attempt`、来源审阅 `review_dispatch_id`、候选 `snapshot_digest`、`verdict`，以及本轮首次出现的 blocking finding 标签 `new_blocking`（按审阅者写的 id 字符串判断；标签不是身份，同名不一定是同一个问题）。它只读、不调用模型，`cause` 固定为 `unknown`，不推断问题为何在后续轮次才出现。每一批审阅是一轮：同一验证轮次的各批是相邻的几轮，`snapshot_digest` 相同，除最后一轮外 `verdict` 都是 approve。详情页在“运行信息”标签显示该表。

预算区显示 `attempts_total`（各任务 `attempts` 之和；没有全 run worker 上限）、模型派发的已用／上限，以及未终止 run 的 `remaining_model_calls`：“如果之后全部一次通过，至少还需 N 次模型调用”。N 按记录的阶段计算：已在待接受或任务审阅已开始的任务计 0，已有候选的计 1，其余未接受任务计 2，整体审阅计 1（全部已接受且同一 HEAD 已批准或整体审阅已开始时计 0）。N 是下界，不逐批计入同一候选或整体快照的后续审阅，也不含指挥调用、返工、无效输出的重做与再规划；`max_agent_dispatches` 用尽时停止信息里也给出它。

`status RUN` 与详情 `summary` 里的 `ledger_stage` 是台账最后记录的步骤，只读台账，不读事件、文件或进程。run 的 `status` 不是 `planning`、`running` 时为 null；否则是七个键的对象 `{step, task_id, attempt, role, kind, dispatch_id, process_state}`，用不到的键为 null，按下面四步依次写，后一步成立时覆盖前面写过的键：

1. `step` 为 `between_steps`。
2. 取第一个成立的：`pending_task` 的 `stage` 为 `verifying`／`reviewing` 时为 `task_verification`／`task_review`；有 `pending_acceptance` 或 `pending_post_accept` 时为 `task_acceptance`；`final_history` 最后一条的 `status` 为 `running`、`phase` 为 `verifying`／`reviewing` 时为 `final_verification`／`final_review`，`task_id` 为 `final`。`task_id`、`attempt` 取自对应的记录（`pending_post_accept` 只给 `task_id`，取自它的 `payload`）。
3. `dispatches` 里最后一个 `status` 为 `running` 的条目：`step` 为 `dispatch`，`role`、`kind`、`task_id`、`process_state` 与 `dispatch_id`（条目的 `id`）取自条目；`attempt` 在 worker 条目上取 `worker_receipt.attempt`，其他角色的条目只在它的 `task_id` 与第 2 步的相同时沿用第 2 步的 `attempt`，否则为 null。验证命令的条目（`role` 为 `verification`，没有 `kind`）也在这一步。
4. 有 `quota_wait` 时 `step` 为 `quota_wait`，`role`、`dispatch_id` 取自它，`kind`、`process_state` 为 null，`task_id`、`attempt` 保留前面的值。

字符串键只取字符串，`attempt` 只取整数，其余记为 null。派发条目在本地准备时（`process_state` 为 `prepared`）就是 `running`，验证条目在进程启动之前也是，进程被杀后台账可能一直停在这些值上：`ledger_stage` 只说台账最后记录了什么，不证明进程仍在执行。详情首屏状态标记那一行（run ID 旁）显示它：`dispatch` 写“最后记录的派发”、角色、`kind`、进程记录（`prepared` 写“已登记，未启动”，`starting` 写“启动未确认”，`running` 写“已记录启动”，`exited` 写“已记录退出”，没有时写“不可用”）与短 dispatch ID；第 2 步的五种阶段写“记录的阶段：…”；`quota_wait` 写额度等待记录的窗口与等到何时（有 dispatch ID 时另加短 ID）；`between_steps` 写“台账未记录更具体的阶段”；之后有值时加任务 ID 与“第 N 次尝试”；值为 null 时不显示。角色、进程记录与步骤按固定的文字表显示，表里没有的值原样显示。

脚本可直接读 JSON 接口：`/api/runs`、`/api/runs/<run_id>`、`/api/runs/<run_id>/events?tail=`、`/api/runs/<run_id>/files`、`/api/runs/<run_id>/files/<相对路径>?tail=&format=json`。无效 ID 或参数 400，拒绝的路径与 Host 不匹配 403，不存在的 run 或文件 404，GET／HEAD 以外的方法 405；台账可读但摘要失败的 run 仍返回 200 并带 `summary_error`。这五个接口的 JSON 响应（文件内容在 `format=json` 时）与 JSON 格式的错误体 `{error, status, observed}`（400／403／404／409）都带同一份 `observed`，它在服务启动时算出，之后不变：`state` 是状态目录（解析后的路径），`checkout` 是检出（未知时为 null），`checkout_basis` 是 `git`（`--repo` 的 Git 根）、`path`（按状态目录路径推定，未核实）或 null，`name` 是检出（未知时取状态目录）去掉根之后的最后两段（一段也没有时是完整路径）。不是 JSON 的响应没有它：文件正文（不带 `format=json`）、页面与脚本，以及 Host 不匹配的 403、405、414、500、503 等纯文本错误；SSE 帧也没有。

`GET /api/runs/<run_id>/stream` 提供 SSE，HEAD 返回 405。`ready` 与 `change` 只发送 run ID、原生后端和采样时间，页面随后读取当前投影；`unavailable` 表示订阅不可用并关闭连接。每 15 秒的注释心跳仅用于连接维护，不读取台账，也不证明模型进程存活。默认最多 2 个订阅，占用总计 8 个连接额度；断连、写入超时和服务关闭都会释放订阅。

命令行 `watch RUN` 先订阅，再输出简短 JSONL 状态；之后每次读取台账，快照与上一次读到的不同时输出一行，两次读取之间的几次变化可能合并成一行，也可能不留下输出。快照带 `quota_wait`（额度等待记录的 `window`、`until`、`recorded_at`，没有时为 null）；快照的 `convergence` 与 `status RUN` 的是同一投影（字段见上面的返工提示），快照不带 `observed` 与 `ledger_stage`；等待期间状态仍是 `running`，`watch` 不退出。读到 completed、abandoned、blocked、interrupted、paused 或 awaiting_approval 时退出；`--follow` 可跨越这些状态继续等待，Ctrl-C 结束。`watch RUN --state /abs/archive/.harness` 可观察归档状态，不要求 Git 仓库或配置。macOS 使用 kqueue，Linux 使用 inotify 与 `/proc/self/fd`；平台不支持或订阅失败时明确报错，不回退到轮询。输出的 `process_observation=ledger_only` 表示进程信息取自台账而非探测。

### 调用阶段分类

每个模型 dispatch 先做矛盾检测再分类：

| 类别 | 含义 |
| --- | --- |
| `usage_recorded` | 已进入后端调用且台账有 `provider_usage` |
| `invoked_no_usage` | 已进入后端调用但 CLI 未留下用量（超时、CLI 失败、输出损坏等）；已消耗本地派发预算，不断言供应商是否计费 |
| `known_not_invoked` | 已知未进入调用：准备中的记录，或本地准备门禁（输入、MCP、快照、待派发指令与原件核对、预算）失败后的收尾状态 |
| `unknown` | `invoked` 字段缺失、不是布尔值，或与 `pgid`／`process_state` 矛盾；页面悬停显示原因 |
| `verification` | 验证进程，不是模型调用 |

`invoked=true` 在进程启动前写入，不证明 CLI 已启动或供应商已收到请求；`process_state=starting` 另标“启动未确认”，不覆盖上述分类。

事件日志里的派发事件：`dispatch_prepared` 在本地准备保存后、预算检查前写入；`dispatch_started` 在台账保存 `invoked=true` 之后写入；`dispatch_finished` 带 `invoked`、`status` 和 `provider_usage`。事件追加没有丢失时，`dispatch_started` 条数等于 `agent_dispatches`；追加失败会被吞掉，run 照常完成而少一条事件。事件是尽力而为的观察：写入失败不影响调用，崩溃可能缺一条，恢复不补发。计数以台账为准。`quota_sampled`（页面显示“额度样本”）在每次额度采样之后追加，带 `provider`、`trigger`、`available`、`reason`、样本文件名 `sample`（写入失败时为 null 并带 `write_error`）与 `dispatch_id`。额度等待写 `quota_wait_started`（“开始等待额度”：`dispatch_id`、`role`、`window`、`resets_at`、`until`、`wait_seconds`、`limit_seconds`、`sample`）与 `quota_wait_finished`（“额度等待结束”：`dispatch_id`、`waits`、`woke_at`），都是尽力而为，等待被中断时没有 `finished`。恢复时调整了配置写 `resume_config_changed`（“恢复时调整配置”，字段 `changes`）。

### 额度

**样本。** 真实 `CLIBackend` 的 `run`／`resume` 在这些时刻各取一份账号额度快照（不调用模型）：本进程首次用到某供应商之前（`before_use`），额度失败之后、判断是否等待或停止之前（`quota_failure`），额度等待醒来之后（`wake`），以及停止、暂停、完成、等待批准确已保存之后，对本进程用过的供应商各一份（`stop`、`paused`、`completed`、`awaiting_approval`）；不计额度等待时每次至多 5 份，额度等待每等一次另加一份失败样本与一份醒来样本（受派发预算约束）。`demo` 与离线后端不采样。探测：Claude 是 `claude -p /usage --output-format stream-json --verbose --no-session-persistence --setting-sources "" --strict-mcp-config --max-turns 1`，必须读到零调用的结果行（`num_turns`、`total_cost_usd`、`duration_api_ms` 为 0，`local_command` 为 `usage`），读不到就在本进程停用 Claude 探测；Codex 是 `codex app-server` 的三条 JSON-RPC 消息（`initialize`、`initialized`、`account/rateLimits/read`）。两者都在空临时目录、用与模型调用相同的环境运行。查询时限 20 秒，从启动算到读到 Claude 的结果行或 Codex 的答复，读到即完成查询（依据 2026-11-17 的联调实测：结果行在启动后约 1.7–4.2 秒到达，Codex 的答复约 3.0–4.6 秒，冷启动超过 5 秒）。之后至多等 5 秒让 CLI 自行结束，再发任何信号：Claude 什么也不做，Codex 先关闭输入（实测 Claude Code 在结果行之后 1.0–2.0 秒自行退出；`codex app-server` 本身在输入关闭后约 2–18 毫秒退出，连同它派生的 `git` 在内的整个进程组 0.03–2.2 秒）；其间照旧读输出，受同一输出上限与检查点约束，Claude 这时写出的行照旧校验（第二条结果行、不是 JSON 的行、超出上限都与查询中相同），Codex 的只读掉；能不回收地看到领导进程已退出的平台以此为准提前结束，看不到的平台（没有 `os.waitid`，或调用失败）以输出关闭为准；只等领导进程，它派生的、到收尾时仍在运行的子进程由收尾的进程组信号结束；等满不算失败，样本照常可用。超时、输出超限、启动失败与中断不等，直接收尾。收尾时整个进程组 SIGTERM 后 SIGKILL；进程事实的 `exited_before_signal` 记收尾发出第一个信号时领导进程是否已经自己退出（null：未启动或无法观察；只描述领导进程，不说明它派生的子进程），为 false 时 `exit_code` 是 CLI 对收尾信号的结果，不表示它自己正常退出。一次探测的计时部分至多约 28 秒（查询 20 秒＋等待自行结束 5 秒＋收尾 3 秒）：不计额度等待时一次 `run`／`resume` 的探测合计最坏约 140 秒（至多 5 次），每次额度等待另加的失败与醒来两份最坏再加约 56 秒；Claude 探测超时一次就在本进程停用，所以 Claude 一侧每个进程至多一次超时。探测期间按 Ctrl-C，读写中的探测在约 0.1 秒内由检查点转为中断，其余时刻在收尾完成之后传播（收尾计时至多 3 秒），临时目录与进程组照样清理。

样本写在 `runs/<id>/observations/quota/`，文件名 `r<修订>-p<相位>-<进程启动时刻>-s<序号>-<供应商>-<触发>.json` 就是它们的顺序，排他发布、从不覆盖，只存归一化字段（窗口的已用百分比与重置时刻、Codex 的 credits 余额原样字符串、`extra_usage`、进程事实），不存 CLI 原始输出或错误文字。探测失败只记“不可用＋原因”：`not_installed`、`timeout`、`output_limit`、`exited`、`provider_error`、`shape_mismatch`、`not_zero_call`、`probe_disabled`、`internal_error`；从不改变调度、预算、回执与接受，用户中断照常传播。

**口径：运行期间账号额度的观测变化。** `status RUN` 的 `quota` 块与网页“预算与额度”标签里的额度区块是同一份投影，只读已保存的样本（最新 400 份），刷新从不启动任何 CLI：

- 这是账号级观测：包含本 run 以外的使用（含驱动 Harness 的会话），不是本 run 的消耗；样本里没有账号标识，换号认不出来。
- 每个额度桶分开：Claude 按 `kind` 与 scope（`weekly_all` 与各个 `weekly_scoped（模型显示名）` 各是一个桶，无关模型的周窗口到 100% 也只是它自己那一行）；Codex 按 limit、槽位与样本报告的窗口长度（不把 `primary` 当成固定的 5 小时窗口）。桶与桶不相加，两家不合成总数，输出里没有任何合计。单位是百分点。
- 每个桶写最近观测（不是“当前”）、观测时间与下次重置时刻，再按重置周期分段，只在同一周期内给首尾差：一段以首个观测的有效重置时刻为锚点；之后的观测时刻达到或越过 min(锚点, 上一份的重置时刻) 就是新周期，重置时刻与锚点相差超过容差也是新周期（与锚点比，连续的小漂移也会累积分段）。容差为 min(120 秒, 窗口长度的 1/60)，窗口长度未知（Claude）时 60 秒；三个取值是设计值，只在约 21 分钟的真实样本上核对过（同一周期内的抖动 Claude 是亚秒级、Codex 为 0，都在容差之内）。
- 一份观测报告的重置时刻只有晚于它自己的观测时刻才算有效：已经过期的值不作锚点、不参与同周期判断与差值，该段的变化为未知，标“报告的重置时刻已过期”。
- 不跨段相减，不假定旧周期用到 100%。有效重置时刻未知、有多个观测的段，变化写“变化未知”；只有一个观测的段写“只有一个观测”；两者都不当作 0。段内下降照算并标“下降（修正或异常）”；因重置时刻偏离锚点而分出的新段标“重置时刻在周期内改变”。
- 不完整的段写明原因：首个可用观测（之前的样本不可用或没有这个桶，不称“run 开始值”）、较早样本未读取（读取端截掉了更早的样本）、重置之后、重置之前、之后的样本不可用或未报告。不可用的样本不打断分段，只计入每家的不可用原因计数。
- Codex 的 credits 放在卡片第一行：最近观测的余额（原样十进制字符串），以及运行期间的净变化——连续的、有余额且不是 unlimited 的观测为一段，用独立的 64 位十进制上下文精确相减（对 Inexact、Rounded 等设陷阱，结果是字符串；陷阱触发时为 null，显示“变化未知”；一段只有一个余额观测时也为 null，显示“只有一个余额观测”）；净变化含充值、赠送、过期，不是消费账单，不加货币符号。unlimited 时不计算差值，余额未报告时写“余额未报告”（可用样本都没有报告 credits 时也这样写；`status` 的 JSON 里此时 `credits` 是空对象，不补造条目）；某份样本没有报告 credits 不断开一段。
- 样本时刻倒退（时钟回拨）时标出，先后仍按台账修订排序；损坏、超大或名实不符的样本计入 `damaged_count` 并跳过；截掉的较早样本计入 `omitted`，第一段的开头标为不完整。`--full` 只打印台账，不带 `quota`。

两个接口都是 experimental，2026-11-17 对真实账号运行过（Claude Code 2.1.325、codex-cli 0.169.0，见[验证记录](validation.md)）；自动等待仍然没有在真实的额度失败上运行过。这一口径不保证的事（每次调用的消耗、跨重置的缺口、换号、显示的新鲜度、容差取值）见[仍需明确的限制](design.md#额度探测统计与等待)。

**用量与名义费用。** 调用分类按台账 dispatch ID 计数，重复的 ID 只计一次并列出；事件里的用量不再相加。token 与 CLI 报告的 `reported_cost_usd`（Claude 的名义估算，不是账户账单，也不是订阅额度；Codex 不报告）只在调用记录里逐条显示原字段，不跨 dispatch 或供应商汇总。

### 文件访问

只能通过固定根读取 run 目录内的普通文件：顶层 `state.json`、`events.jsonl`、`handoff.json`、`worktree-cleanup.json`、`workspace-inputs.json`、`workspace-inputs-manifest.json`；目录 `plans/`、`dispatches/<uuid>/`（含 `commands/<32 位十六进制>/`）、`verification/`、`preflight/`、`recovery/`、`decisions/`、`environment/`、`checkpoints/`、`proposal-rejections/`、`continuations/`、`continuation-rejections/`、`execution-guidance/`、`path-indexes/`、`input-indexes/`、`finding-progress/`、`observations/`（额度样本），即[文件布局](#文件布局)里 run 目录下的全部证据。生成的 MCP 配置 `mcp.json`、两个空锁文件 `run.lock` 与 `store.lock`、`*.tmp` 与 `memory.sqlite3*` 按名排除；顶层其他条目在文件索引里计为“不可寻址”。离线套件在每个仓库用例结束时核对它留下的 run 目录：顶层的每个条目要么在上面的固定根里、要么按名排除，运行时新增顶层文件或目录而忘了登记会让用例失败。符号链接与非普通文件一律拒绝，中间目录在检查后被替换为符号链接也无法逃出根目录；路径深度不超过 8。文件接口不打开台账中的绝对路径；详情摘要与 `watch` 可能按台账记录的绝对路径读取指挥原件（`scope.held_view`），观察复制的 `.harness` 时也读原位置那一份。文件内容永远以 `text/plain`（或 `format=json`）返回，日志不会作为 HTML 执行。

### 限制

- 只绑定 127.0.0.1，不开放远端访问；只接受 GET／HEAD（事件流仅 GET）；同时连接最多 8 个，超出返回 503；写入阻塞的慢客户端约 10 秒后断开。
- 服务在启动时固定持有 `.harness` 目录的描述符；目录被删除重建后需重启服务。目标项目还没有 `.harness` 目录、`.harness` 是指向自身的符号链接或只有搜索权限时，`dashboard` 报错退出 1，而 `status` 列表输出 `[]`、退出 0（前两种情况位置行末尾带 `（无法打开）`）。`.harness/runs` 本身是符号链接或不可读时，`status` 列表输出包含目录路径的诊断并退出 1。`status RUN` 的台账不用列表的符号链接拒绝规则：它从位置行所报、已经打开的状态目录出发按相对路径读取，跟随符号链接，状态目录只有搜索权限也能读；额度样本也从这个目录读取。`state.json` 是 FIFO、设备这类非普通文件时不读，以 `runtime_failure/io` 停止。
- `python3 -m harness.dashboard --state` 指向复制或归档的 `.harness` 时，检出只是按路径推定的上级目录，那里未必是 Git 检出；解析后的目录名不是 `.harness` 时检出未知。
- 端口被占用时启动失败、退出 1、不写位置行；不自动换端口，也不探测其他端口或检出。同一仓库的几个检出各开一个 `dashboard` 时用 `--port 0`。
- 首／末事件时间只在 `events.jsonl` 头／尾 64 KiB 内查找；dispatch 结束时间只取尾部 64 KiB 中的事件，较早调用可能显示不可用；`state.json` 超过 64 MiB 不解析；操作记录最多扫描 1000 个文件、每个 256 KiB；文件索引最多 2000 项。
- 无法解析的台账在列表中跳过并给诊断，详情仍尽量显示并标出 `ledger_error`；摘要失败标出 `summary_error`。解析成功但不是当前版本对象的台账（其他版本、缺版本、数组、`null`）只列路径、版本和“不支持”，详情返回 409。
- 不汇总多个项目，不按公开单价估算账单，观察结果不改变任何预算。自动验证覆盖 HTTP、macOS 原生通知和页面脚本行为；Linux 原生通知与浏览器人工验收没有执行（见[验证记录](validation.md)）。

## worker 结果与审阅问题

### 检查点

WORK 结果可以带可选的 `checkpoints`，每项为 `{id, summary, inputs, evidence, freshness, max_age_seconds}`，`inputs`、`evidence` 是 `{path, sha256}` 列表。运行时把 evidence 原文私有复制到 `checkpoints/<dispatch_id>/`，之后按声明的输入和有效期在角色输入中标为 `reusable` 或 `stale`；同一任务里，后一次 dispatch 的同名检查点（按折叠后的 ID 判断，`Build` 与 `build` 同名）取代前一次的成为当前项，前一次的转入 `checkpoint_history`（历史合计超过 4,000 字符时列表留空，只在 `checkpoint_history_omitted` 记条数）。

| 限制 | 值 |
| --- | --- |
| 每个结果的检查点 | 最多 16 项，ID 在本结果内唯一，折叠后（不区分大小写等）相同也算重复 |
| 每项 `inputs`／`evidence` | 各最多 8 个，`evidence` 至少 1 个 |
| 单个 evidence 文件 | 1 MiB |
| 一次结果的 evidence 副本合计 | 16 MiB |
| `summary` | 非空，最多 4,000 字符 |
| `max_age_seconds` | 0–86,400 |

- `freshness="content"` 必须绑定至少一个 input，且 `max_age_seconds=0`；任一输入的实际 SHA256 变化即为 stale。
- `freshness="volatile"` 的有效期从来源 worker 调用开始计时（dispatch 的 `invoked_at`），worker 自己的执行时间也算在内，结果落盘或恢复都不重新计时；`max_age_seconds=0` 表示每次都要重新检查；缺少有效调用时间的记录一律 stale。
- 路径不得含 `..` 或 `.git`、`.harness`、`.codex`、`.claude` 段（按折叠后的名称比较，`.CLAUDE` 这类别名写法同样算），不能是符号链接。唯一例外：evidence 可以引用当前 worker dispatch `commands/` 下的普通文件（broker 返回的 stdout／stderr 路径）。新的 worker 尝试是新的 dispatch，不能重新登记原 dispatch 的 broker 输出；旧证据也不能靠重新登记刷新有效期，需要时重跑可安全重复的检查。
- `reusable` 只表示声明的输入未变且未过期，不证明依赖完整、结论正确或任务已接受；检查点不替代验证、审阅或接受门禁。

声明被丢弃与证据损坏是两回事。检查点是可选产物，不参与 worker 结果的契约判定：单项的声明核对失败（字段缺失或超长、空 evidence、SHA256 格式非法、content 缺输入或有效期非零、路径不允许、实际摘要不符、超出大小）只丢弃该项，超过 16 项或重复 ID 丢弃整批；ID 是 `checkpoints/<dispatch_id>/` 下的文件名，所以按 `harness.util.fold_name` 比较，`Build` 与 `build` 这类折叠后相同的 ID 与完全相同的 ID 一样，在写出任何文件之前整批丢弃；结果、blocker 和其余有效项照常登记，写 `checkpoints_discarded` 事件，blocked 反馈带前 2,000 字符原因。无效的 worker 结果（见[无效输出与自动重做](recovery.md#无效输出与自动重做)）不登记检查点；交接请求非法的 completed 结果例外，它在检查点登记之后才被判为无效，合格的检查点保留给下一次 worker。原始结果、台账行、manifest、`checkpoints/batches/` 下的批次文件或私有副本被删改，以及私有写入失败，都直接停止并保留现场，不按丢弃处理。

### 决定请求与审阅问题

- 已满足本任务验收的 completed 结果可以附带最多 16 条 `decision_requests: [{id, task_id, question}]`，目标只能是传递依赖于本任务、尚未接受的任务；目标或问题不合法时本次尝试被拒绝，原因经 `worker_recovery` 交给后续调用。blocked 结果附带的 `decision_requests` 不登记，只写 `decision_requests_discarded` 事件和原结果摘要；该结果的 blocker、有效检查点和尝试豁免照常登记。
- finding 的内部身份是（来源审阅 dispatch，该审阅原件 `findings` 数组中从 0 起的序号 `finding_index`）；审阅者写的 id 只是标签 `label`，可以重复，也不做别名。每次派发收到的 `required_findings` 每行是 `{key, review_dispatch_id, finding_index, label, artifact}`，`key` 为运行时生成的 `f1…fN`；`artifact` 在更省字符时提到 `required_finding_sources[review_dispatch_id]`。worker 收到的是待修状态为未解决的问题（新报告的问题为未解决；之后可用审阅的 resolved 设为已解决，任何审阅、包括不可用审阅的 open 设回未解决，not_checked 不改变）按来源顺序的前 60 条，与候选无关；审阅收到的是一批：当前候选上还没有可用 resolved 的问题，超过 60 条时先选待修状态为未解决的、再按来源顺序补满 60 条（规则见设计的不变量 12）。同一候选的各批都是完整审阅：任何一批不批准即返工；一批批准而算上这一批之后这个候选上仍有缺可用 resolved 的问题时，登记这一批并换成下一批的对照，候选不变时下一步是下一批的派发，照常经过派发的各道门禁（例如派发预算用尽时停在 `budget/agent_dispatches`，不调用模型）；算上批准的这一批之后这个候选上不再缺时才进入待接受。审阅者技能要求：仍有问题的行在原键上给 open 并附证据，不作为新 finding 重报；请求修改时没核验的行可以留为 not_checked；运行时不判断是否重报。审阅者的 `finding_dispositions` 与 worker 的 `repair_claims` 是以这些键为属性的固定对象，键全部必填、不许多余键，没有必需项时字段不出现。审阅包里的 `repair_claims` 已按（来源，序号）换成本次审阅的键，只含两边清单都有的问题。历史审阅的键只在它那次派发内有效，按该记录自带的 `required_findings` 副本解释。包的 `prior_reviews` 只有最后一条记录带这份副本与 `finding_dispositions`；同一范围里其余带处置审阅的副本与处置在 `prior_review_keys_index` 指向的输入索引里，每条记录是 `{review_dispatch_id, snapshot_digest, artifact, artifact_digest, required_findings, finding_dispositions}`（`artifact` 是该审阅的原件），按历史顺序排列（范围与发布条件见设计的不变量 33）。截短的审阅摘录（`details_truncated`）的完整审阅在所在记录的 `artifact`；与紧随其后的记录 `diff_artifact` 相同的较早记录（例如同一验证轮次的前几批）带 `same_candidate_as`（那条记录的 `review_source.dispatch_id`），去掉与那条记录相同的 `diff_artifact`、`verification`、`worker_source`。不可用审阅（定义见设计的不变量 12，含无效与失效的审阅）的 resolved 不关闭旧问题，它的 open 仍把问题设回未解决；任务证据由运行时按 `evidence_requirements` 直接检查候选文件，worker 不再声明，预检失败时 `evidence_invalid.reason` 为 `missing`、`symlink`、`not_regular_file`、`outside_candidate`、`unsafe_path` 或 `unreadable`。

## 依赖副本清单

`prepare-run RUN --manifest FILE` 在 run 锁内预检主检出源文件、当前 HEAD、worktree Git 绑定、所有已登记派发和命令进程。仅接受当前台账版本（20）的 `paused` / `awaiting_approval` run，没有待消费候选、审阅、待发布的修订等阶段；其他版本的台账被拒绝。已接受任务和预算不改变。

清单示例（HEAD 和 SHA256 必须替换为实际值，mode 是十进制 POSIX 权限）：

```json
{"schema_version":1,"run_id":"run_0123456789abcdef","head":"<current expected HEAD>","source_root":".","max_files":1,"max_bytes":1048576,"files":[{"source":"deps/input.dat","destination":"vendor/input.dat","sha256":"<64 lowercase hex>","mode":420,"type":"file","ownership":"input_copy"}]}
```

源为主检出中的普通独立文件；目标必须在隔离 worktree 中、被 Git 忽略且未跟踪。拒绝符号链接、硬链接、保留路径、隐式凭据路径、源目标重叠和未知已有目标。复制前冻结清单摘要与台账绑定，再记录意图、独占临时文件身份、发布及回执；源和副本不共享 inode。中断后仅用登记的文件身份恢复，不能凭内容相同接管文件。resume 与派发前均核验登记副本；前置核验失败不新增 worker 次数。

清理默认只读预览。`cleanup-run RUN --remove --remove-input-copies` 在终态与进程退出、HEAD/Git 绑定门禁通过后，整批预检源和副本未改变，再逐项记录删除意图和结果。未知或改动的文件保留；未被忽略的遗留文件仍会阻止整个 worktree 删除，其他被 Git 忽略的文件只在同时加 `--remove-ignored` 时随工作树删除（`--remove-ignored` 不能代替 `--remove-input-copies`：登记的副本还在时照样阻止）。中断可能留下已删/未删回执，重复执行仅恢复同一意图。此机制不认领构建产物、不运行任意 setup，也不将忽略的可编辑源码导入任务。

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

表外的 argv 不执行，直接返回工具错误。**已覆盖**指以下四处把 `cwd` 为 `.`、argv 能被该函数解析的命令算作已满足：计划的 `required_commands` 与 `self_checks`、续作命令请求、环境更新的 `worker_command` 检查（只靠内置覆盖通过时，报告的这一项带 `coverage: "builtin_git_query"`；同一条命令已有显式授权时没有这个字段）、`permission-diagnostics` 的 `builtin_coverage`（`covered`、`form`，`meaning` 为 `capability_available_not_executed`）。`git_query` 只在工作树根执行，同一 argv 带其他目录不算已覆盖：`builtin_coverage` 此时是 `{"tool": "harness.git_query", "covered": false, "reason": "git_query runs only at the worktree root"}`，这样的命令需要显式授权，授权后经 broker 在那个目录执行。已覆盖只表示能力存在，不表示查询已执行或成功，也不是 Bash 或命令 broker 授权；管道、重定向和表外选项都不算已覆盖。`local_exact_command_match` 与 `configured_sources` 只表示配置里有目录与 argv 都相同的命令。`permission-diagnostics` 的 `--command-json` 一出现就比较，值必须是命令对象 `{"cwd": …, "argv": […]}`；`null`、空串与数组都被拒绝，不当作没给。

### worker 命令与删除

只有 worker 有 `harness_commands` MCP。配置条目只给 broker 授权，不生成 Bash 规则：Harness 为 Claude worker 生成的 `--settings` 里，以 `Bash(` 开头的放行规则恰好是只读前缀各自的 `Bash(<前缀> *)`（参数只能接在 `--` 之后），与配置里有哪些命令无关。broker 回执只记录经 broker 的执行，不记录 worker 经 Bash 执行的命令。Claude CLI 按这些规则实际放行或拒绝哪些 Bash 调用是联调的预期，这一版没有用真实账号核对。预期是：argv 落在只读前缀里的配置命令（例如 `{"cwd": "app", "argv": ["ls", "--", "src"]}`）仍可经 Bash 执行，那是只读前缀独立的能力，与这条配置无关，在 Bash 会话当时的目录里执行（不一定是 `cwd`），不留 broker 的 journal 与回执，也不是 OS 隔离；配置里不落在只读前缀的命令经 Bash 被拒绝，只能经 broker 执行。

`list_commands` 列出每条已授权命令的 `command_id` 与 `command`（`{cwd, argv}`）；命令 ID 是（目录，argv）规范 JSON 的 SHA256，同一 argv 在两个目录有两个 ID。`run_command({command_id})` 在命令的目录里执行 `verification.commands` 和 `permissions.worker_commands` 中与 ID 对应的完整 argv，每次调用都是新的执行，不能附加参数或改目录、env，不经 shell。下面的结果都以单一故障为前提，见[设计：隔离与进程](design.md#隔离与进程)。准入时 broker 以 no-follow 打开工作树根，在这个已打开的根上核对 Git 绑定（根下的 `.git` 经同一个描述符读取），并要求它是建清单时记下的那个目录（清单的 `root_identity`）；任一项不符是工具错误，不写 journal 与回执，命令不启动。然后从这个根逐级以 no-follow 打开命令目录：某一段不存在、不是目录（例如普通文件）、是符号链接、没有权限（`EACCES`、`EPERM`）或名称过长，或者子进程进入已打开目录的 `fchdir` 失败（例如没有搜索权限）时，命令不启动，回执记 `exit_code: null`、`termination_reason: "start_failed"`、`exit_confirmed: true`、`evidence_complete: true`，原因写在 `error` 与 stderr 捕获里；这不是工具错误，同一派发里之后的调用照常准入。逐级打开时遇到其他本地 I/O 错误（例如 `EIO`）时命令同样不启动，但回执记 `termination_reason: "error"`、`evidence_complete: false`，同一派发里之后的调用在准入时因命令证据不完整被拒（工具错误）。回执的 `command` 等于清单里该 ID 的命令，不相等时之后的调用按命令证据不完整被拒。字面 `*` 和换行原样传递；管道或重定向需要显式授权完整的 `{"cwd": …, "argv": ["/bin/sh", "-c", "固定脚本"]}`，子目录写进 `cwd`，不要在脚本里 `cd`。每条命令的超时取 `runtime.verify_timeout_seconds`，且不超过父派发截止时间减 2 秒收尾余量。

`delete_path({path})` 只在 worker 派发有当前任务范围时出现，删除一个文件：`path` 是字面的仓库相对路径，必须精确等于 HEAD 中已跟踪的普通文件（模式 `100644` 或 `100755`），并匹配任务的 `allowed_paths`。路径最多 4,096 字符；拒绝绝对路径、空段、`.`／`..` 段、NUL、首段为 `.git`、`.harness`、`.codex`、`.claude` 或任一段为 `.git` 的路径（按折叠后的名称比较：不分大小写，并按 Unicode 规范化；与规划和接受时的受保护根判定是同一条规则），以及未跟踪文件、目录、符号链接和子模块；不删除目录或空的父目录，也不移动文件。

删除立即生效，审阅和接受门禁只决定候选能否被接受，不能撤销删除。保证见[设计的不变量 26](design.md#不变量)，不保证（并发改名）见[仍需明确的限制](design.md#隔离与进程)。

每次调用（命令或删除）都先在 dispatch 的 `command-staging/` 中准备目录并写好完整的 `process.json`，再一次原子改名进 `commands/<32 位十六进制>/`；发布之前不启动子进程、不删除任何文件。发布失败或在发布前被杀时，`commands/` 里不会出现残缺目录，恢复不受影响；残留的准备目录不参与枚举，也不自动回收。命令记录包括 `output.stdout`、`output.stderr` 与 `receipt.json`；删除记录的 journal 命令 ID 为 `delete_path`，在最后一次核对前写入 `delete-intent.json`（路径、HEAD、原文件 SHA256 与字节数、时间），结束后写入 `receipt.json`，其 `outcome` 为 `deleted`、`refused`、`failed` 或 `concurrent_change`，并带 `deleted`、`sha256`、`bytes`、`concurrent_change`。只有 `deleted` 不是工具错误。每个派发的命令清单 `commands.json` 是 manifest 版本 3，含工作树根 `worktree`、建清单时根目录的身份 `root_identity`（`[st_dev, st_ino]`，`run_command` 与 `delete_path` 共用）、命令条目 `{command_id, command}` 与任务删除范围 `delete_scope`；broker 拒绝其他版本与不合格的命令条目。所有恢复和清理都核对这些子进程记录，父 dispatch 退出不能证明命令已退出。

## 真实账号联调

以下入口使用配置中的真实模型与账号额度，与离线测试分开记录；离线测试不能证明这些调用已执行或通过。

```bash
python3 -m harness smoke
python3 -m harness smoke --commander fable
python3 -m harness.permission_smoke --repo /abs/project
python3 -m harness.tool_smoke --repo /abs/project
```

这四个入口都只读操作者配置（`--config`，默认是源码目录的 `harness.toml`），不读项目配置，验证命令与 worker 命令由各自设置。

- `smoke` 在临时 Git 仓库由真实指挥生成计划，再调用执行者、任务审阅者和整体审阅者，运行真实测试；不改动本仓库。它用真实的 `CLIBackend`，所以也运行两家的额度探测（见[额度](#额度)）；离线测试只用桩可执行文件，不证明探测在真实 CLI 上可用。
- `permission_smoke` 在 `--repo` 的 `.harness/permission-smoke/` 建立专用现场，检查工作树内读写、已配置命令经 Bash 被拒（配置命令不是 Bash 规则）、额外参数、只读前缀与配置命令的交集（同样被配置的 `ls -- .` 经只读前缀允许）、工作树外读写和 `.git` 修改。每个用例该放行还是拒绝，是现场（`prepare_fixture`）记录的预期；这一版的规则还没有用真实账号跑过这组用例。通过要求实际工具调用、工具结果、对应的 `permission_denials` 和文件内容一致，模型自称“已拒绝”不算证据。专用命令替换项目命令，外部 MCP 关闭。默认超时 180 秒、最多 16 轮、预算参数 1 美元，可用 `--timeout`、`--max-turns`、`--max-budget-usd` 调整。
- `tool_smoke` 让配置的 Claude worker 调用 broker（根目录命令、显式管道、子目录 `sub` 里的命令，以及目录不存在、得到未启动回执的命令）、文件工具、`git_query`（`ls-tree`、`hash-object` 与一条被拒绝的 argv）和 `delete_path`（范围内删除与范围外拒绝），核对工具结果与文件；默认最多 20 轮。

结果只覆盖这组现场和实际 CLI 版本，不证明完整命令白名单或 OS 隔离。已执行的联调记录在[验证记录](validation.md)。在 Claude Code 会话里运行这些入口（或 `run`／`resume`）时，会话的环境变量照常传给子进程（见[限制](design.md#额度探测统计与等待)），建议在普通终端里运行。
