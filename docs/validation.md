# 验证记录

本文只记录当前版本（台账 18）已经完成的验证。更早版本的记录在 git 历史里；它们验证的是已经改掉的代码，证明不了现在的行为。

## 验证对象

- 分支 `ledger18-2026-11-13`，台账版本 18，提交 `7440249`（运行时、测试与使用文档）；起点 `1361f95`（台账 17）。验证日期 2026-11-17。
- 台账 18 的九步在 `b0ba960` 完成，2026-11-16 在它上面跑过同样的三种离线套件（分片、单进程参考、带绊线的分片；各 1,712 项，通过 1,711、跳过 1）；`7f7fda6` 只改 `docs/recovery.md`；`7440249` 修复真实账号联调发现的缺陷与两处遗留（见[真实账号联调](#真实账号联调2026-11-17)），下面的离线结果是在它上面重新跑的。`7440249` 之后的提交只改本记录。
- macOS（Darwin 25.6.0），Python 3.15.2。
- 下面的离线结果都来自模拟模型响应（`DemoBackend`、各测试的脚本化后端，以及打印合成信封的本地子进程）加真实 Git、真实验证子进程与真实信号。没有调用任何真实模型，也没有运行真实的 `claude`、`codex`：凡用到 `CLIBackend` 或额度探测的用例，两个可执行文件都换成记录调用的桩。离线结果不证明任何真实模型调用或真实 CLI 接口可用；真实账号上做过的见下面的“真实账号联调”一节，仍然没有运行的见[没有运行的](#没有运行的)。

## 离线套件

三次运行都在提交 `7440249` 的运行时与测试代码上，依次进行、互不重叠：

| 跑法 | 收集 | 执行 | 通过 | 失败 | 跳过 | 用时 |
| --- | --- | --- | --- | --- | --- | --- |
| 分片 `python3 tests/run_shards.py --shards 4` | 1,723 | 1,723 | 1,722 | 0 | 1 | 805.6 秒 |
| 分片，`claude`／`codex` 绊线在 `PATH` 最前（见下节） | 1,723 | 1,723 | 1,722 | 0 | 1 | 775.3 秒 |
| 单进程参考 `python3 -m unittest discover -s tests`，同样带绊线 | 1,723 | 1,723 | 1,722 | 0 | 1 | 2,534.5 秒 |

前两次运行时机器上没有别的测试进程；单进程那次运行期间，同一台机器上在做[修复后的复测](#修复后的复测)（真实 CLI 的探测与一次 `smoke`，不是测试进程）。分片跑法核对了收集集合与执行集合逐项相等（没有缺失、重复或多出的用例），单进程跑法执行 1,723 项，两种跑法的用例数相等。跳过的一项是 `test_git_repairs.GitBoundaryRepairsTests.test_non_utf8_paths_and_link_targets_survive_snapshot_and_acceptance`：本机文件系统拒绝非 UTF-8 文件名，用例自己跳过。停止守卫 G1（恢复预检的拒绝不改 run 的状态与停止；除同一条命令里经审计的 `--confirm-exited` 确认之外，台账逐字节不变）、G7（运行时发出的停止没有 `unclassified`）、G8（出口表、登记表与行用例三方一致）、G10（已发布停止列出的出口不被加载核验挡住）在三次运行里都没有失败。`python3 -m compileall -q harness tests` 与 `git diff --check` 通过。

## 真实 CLI 绊线

第二、三次运行前把两个绊线脚本 `claude`、`codex` 放在 `PATH` 最前：被调用就把参数追加到日志并以 97 退出。用例里需要 CLI 的地方各自装了记录调用的桩，并在用例结束时断言没有意外调用；绊线兜住桩以外的任何查找。结果：两次运行里真实 CLI 调用都是 **0** 次（日志为空）；运行结束后没有遗留的桩进程，临时目录里没有遗留的 `harness-quota-*` 探测目录。

## 各步的中断注入与变异

台账 18 分步实现，每步一次主提交，必要时加修复提交。中断注入用仓库的崩溃辅助（`crash_point`、`crash_after_save`、`after_save`）或真实信号，在规格列出的落地窗口上逐个入库，按默认预算运行；“变异”指在导出的副本上改动运行时、确认入库用例失败（被杀死），变异不进版本库。

| 步 | 内容 | 提交 | 中断注入 | 变异 |
| --- | --- | --- | --- | --- |
| 1 | 只支持台账 18，删除历史兼容与旧键提示 | `636fee3`、修复 `1012ed8` | 没有新的落地窗口 | 修复提交 20 条全部被杀死；确认审阅另验 4 条 |
| 2 | 删除全部 hooks | `5ba72e3`、补充 `b961932` | 规格的 28 行中断窗口（接受链、worker 封存到交接、指挥与审阅回执落地；`Path.replace`、Git 子进程、记忆写入处中断）逐行有入库用例 | 13 条按预期；审阅者独立复跑 9 条，都被行为或磁盘状态断言杀死 |
| 3 | 资源生命周期：验证通过的检出回收、`cleanup-run --remove-ignored` | `b4bd146`、修复 `2c1453c` | 回收与 `--remove-ignored` 意图、续做的五个窗口 | 13 条；修复另 2 条，全部杀死 |
| 4a | 停止值、投影与字段去重 | `7036523`、修复 `73918e9` | — | 11 条；修复后 21 条，全部杀死 |
| 4b | 出口表逐行标注、退出码 0／2／3、`--reverify` | `f435e7b`、修复 `8d0c9ca` | 4 个保存窗口 × 3 种恢复命令的 12 格；17 类恢复预检拒绝不改 run 的状态与停止（带 `--confirm-exited` 时只多出那次经审计的确认）；出口表每行各走一次列出的出口（现在 78 行，守卫 G8 核对文档、登记表与行用例三方一致） | 27 条；补测 42 条；修复 11 条，全部杀死；审阅者另验 11 类、确认审阅 4 条 |
| 5 | 供应商分类、当场判定、四类豁免 | `1498490` | 进程退出后、判定保存前；判定保存后、停止保存前；退出未确认；判定前后改动原件；判定读文件失败；第 9 步补 `WorkerReceipts.reject()` 构造恢复记录（读 Git）时的进程死亡与 Ctrl-C | 13 条（含绕过全部退出检查的 M6）；审阅者另验 15 条；第 9 步另 4 条，全部杀死 |
| 6 | 范围边界：`run`／`resume --scope`、持有、`--reject-request`、追加范围与命令 | `0a7e15d`、修复 `e7dd285` | 方案步骤表第 6 行的八个注入点，加组合提交三项已保存、尚未调度时中断 | 24 条；修复另 3 条，全部杀死 |
| 7a | 额度快照适配层与样本文件 | `7ede811`、修复 `23116ea` | 探测进行中的 SIGINT（读取中的中断约 0.1 秒内传播的延迟断言随 `a14e40f` 入库）、超时、输出超限、形状不符、零调用核验失败、样本写入失败；确认审阅另用真实 SIGINT 测了启动前后、宽限等待、删除临时目录与样本发布 | 34 条；修复后 36 条，全部杀死；审阅者另验 16 条 |
| 7b | 额度统计与展示，替换美元面板 | `f6c9ac5`、修复 `a14e40f` | 没有新的台账窗口；分段边界表 20 行逐行有用例 | 21 条；修复 11 条，全部杀死；确认审阅另做 60 个页面变异，全部被入库用例杀死 |
| 8 | 额度用尽后的进程内等待 | `0a06b55`、补充 `4b81219` | 等待中 SIGINT；强杀后遗留 `quota_wait`；派发预算用尽；快照不可用；窗口对应不明；超过上限；恢复时改小或关闭上限；醒来后路径索引、输入索引与依赖副本被真实损坏 | 45 条；补充 2 条，全部杀死；审阅者另验 18 条，另做 8 个离线探针（含 6 次等待记录发布后的真实 SIGKILL） |
| 联调修复 | 额度探测：答复即完成查询、之后有界地等 CLI 自行结束、查询时限按实测放宽；观察入口的固定根与实际布局对齐并加守卫；帮助与提示文字 | `7440249` | 等 CLI 自行结束期间的 SIGINT（约 0.1 秒内传播）、第二条结果行、不是 JSON 的行、输出超限；等满之后由收尾结束；经额度等待走一遍“晚关输出的 CLI → 失败样本可用 → 进入等待” | 23 条，全部杀死（其中 2 条第一轮存活，补了桩的时序模式与用例后杀死）；新回归在 `779ecbf` 的代码上失败 |

## 独立审阅

审阅者是 `gpt-6.1-sol`，每轮新进程，只读导出的提交，不改仓库。结论“需先修改”表示有阻断意见；阻断全部由修复提交解决并经确认。审阅原文在被忽略的 `.harness/design-proposals/2026-11-13-execution/reviews/`。

| 步 | 规格审阅 | 代码审阅 | 修复与确认 |
| --- | --- | --- | --- |
| 1 | 无（方案即规格） | 需先修改：4 条阻断（命令 broker 与只读 MCP 缺版本门禁；`load_run()` 的门禁晚于挂接；任务记忆仍解释旧台账；非对象台账绕过网页详情的 409）、3 条建议 | `1012ed8`；确认：可以继续 |
| 2 | 1 轮：3 条阻断、4 条建议，修订后实现 | 可以继续：0 条阻断、2 条建议 | 建议由 `b961932` 落实 |
| 3 | 1 轮：2 条阻断，修订后实现 | 需先修改：1 条阻断（回收的快速检查遇到非对象回执时抛异常，挡住整体审阅）、1 条建议 | `2c1453c`；确认（并入第 7 步规格复审）：已解决 |
| 4a、4b | 3 轮：10、4、3 条阻断；第三轮遗留的 3 条改成统一的“停止定稿”规则，交 4b 代码审阅核对 | 4a 需先修改：2 条阻断（恢复包重复带错误摘录；操作记录丢失停止类别）、2 条建议。4b 需先修改：4 条阻断（检查点来源不符未归为证据故障；进入待批准绕过定稿；验证超时绕过进程事实分类；`prepare-run --manifest` 的坏输入退出码）、3 条建议；第三轮遗留 3 条全部已解决 | `73918e9`（确认并入 4b 代码审阅：已解决）；`8d0c9ca`，确认：可以继续 |
| 5 | 1 轮：可以继续，0 条阻断、4 条建议（实现前落实） | 可以继续：0 条阻断、2 条建议 | 建议由第 9 步落实（使用文档的限制与 I/O 说明、两个入库用例） |
| 6 | 2 轮：6 条阻断；复审可以继续 | 需先修改：1 条阻断（停止值按字符数估算大小，补充平面字符使合法的持有退化成 `unclassified`）、1 条建议 | `e7dd285`；确认（并入 7a 确认审阅）：已解决 |
| 7a、7b | 2 轮：7 条阻断；复审留下 2 条窄阻断，改后交代码审阅核对 | 7a 需先修改：2 条阻断（探测建临时目录与收尾开始处的信号窗口）、2 条建议。7b 需先修改：1 条阻断（验收测试杀不死“余额转成浮点数”的变异，运行时本身正确）、2 条建议；复审遗留的 2 条都已解决 | `23116ea`，确认：可以继续；`a14e40f`，确认：可以继续 |
| 8 | 1 轮：3 条阻断、4 条建议，修订后交代码审阅核对；规格另加一条要求：醒来后重跑 `resume` 的只读前提核对 | 可以继续：0 条阻断、3 条建议；规格审阅遗留的各条全部已解决 | 建议由 `4b81219` 落实；确认：可以继续 |
| 9 | 无（任务书即规格） | 文档对代码的只读审阅，需先修改：1 条阻断（`docs/recovery.md` 写 `--environment-update` 的输入拒绝不改写台账，而 `resume` 先保存经审计的退出确认、之后才读这个文件）、1 条建议 | `7f7fda6`（只改文档）；确认：可以继续 |

## 真实账号联调（2026-11-17）

- 对象：提交 `779ecbf`（修复之前的代码，运行时与测试和 `b0ba960` 相同），主工作树干净，直接运行主工作树的代码。Claude Code 2.1.325、codex-cli 0.169.0、Python 3.15.2，macOS（Darwin 25.6.0）。
- 配置：`harness.toml` 的副本，只改 `runtime.quota_wait_max_seconds = 900` 与 `[shared]` 的三个路径（改为指向同一份 `shared/` 的绝对路径）；模型 ID 原样：指挥 `gpt-6-astra`（`--commander fable` 时 `claude-fable-5-2`）、worker `claude-opus-5-5`、审阅 `gpt-6.1-sol`。
- 全部流程在系统临时目录下新建的 Git 仓库里运行；驱动 Harness 的是一个 Claude Code 会话，除注明“去掉会话变量”的探测外，都带着会话变量运行。
- 真实模型调用共 38 次（另有两次 worker 返回由脚本给出，不计）。没有遇到额度用尽。每个流程结束后按台账记录的进程组核对没有遗留进程，系统临时目录里没有遗留的 `harness-quota-*`（强杀探测的实验除外，见下）。证据在被忽略的 `.harness/validation/ledger18-real-20261117/`。

### 基线

| 项 | 结果 | 调用 |
| --- | --- | --- |
| `doctor` | 退出 0；两个 CLI 已安装、已登录；回显的包、提交与配置摘要正确 | 0 |
| `smoke`（`gpt-6-astra`，会话内） | `completed`，退出 0；指挥、worker、任务审阅、整体审阅各 1 次 | 4 |
| `smoke --commander fable`（会话内） | `completed`，退出 0；同上 | 4 |
| `python3 -m harness.tool_smoke` | 通过，26 项检查全部通过（broker、文件工具、`git_query` 三种、`delete_path` 范围内删除与范围外拒绝） | 1 |
| `python3 -m harness.permission_smoke` | 通过，全部检查通过 | 1 |

两次 `smoke` 的额度样本各 4 份（每家一份 `before_use`、一份 `completed`），都有 `quota_sampled` 事件，`status RUN` 的 `quota` 块成形；但 Claude 的 `before_use` 两次都是 `timeout`，随后的 `completed` 是 `probe_disabled`，astra 那次 Codex 的 `completed` 也是 `timeout`（缺陷 1）。没有未分类的停止。

### 范围边界与修订

- **边界内规划**：`run --scope hello.py --scope 'tests/**'`，目标完全在边界内。`gpt-6-astra` 与 `claude-fable-5-2` 都在边界内规划，都 `completed`（各 4 次调用）。
- **规划阶段提问**：`--scope 'src/**'`、目标要求“边界不够时先问”，astra 在规划阶段提问（`operator_decision/answer_required`，退出 3，请求种类 `commander`）。
- **直接提交越界首版**：目标说明“边界可能不够，照样规划”时，astra 直接给出含边界外路径的首版（被持有）；不加引导的对照（目标只说要改边界外的文件）里 astra 与 fable 都没有提问，直接给出越界首版（被持有）。
- **扩展原任务而不拆任务**：修订时 astra 与 fable 都用原任务的 `add_allowed_paths`（astra 另加 `add_required_commands`）扩展原任务，没有新增任务，修订被持有。
- **契约接受**：带 `add_allowed_paths`、`add_required_commands` 的 `TASK_UPDATE`，Codex 严格模式与 Claude `--json-schema` 都接受，回值与要求逐字相符（2 次调用）。
- **范围兜底的往返**：
  - 首版越界：`scope_extension/initial_plan`，退出 3（经 `supervise` 包住，回执 `exit_code` 3）；`status RUN` 的 `stop` 与 stderr 的 `[harness-json]` 是同一投影，出口为 `resume RUN --scope=hello.py`、`--reject-request FILE`、`status RUN`；普通 `resume` 得到同一停止、同一 `request_id`、零调用；`--approve-plan` 被拒（`invalid_input/plan_unpublished`，退出 2）；`resume RUN --scope=hello.py` 后同一原件发布，不再调用指挥，跑到 `completed`。
  - 修订越界：`scope_extension/revision`，缺口两个文件；普通 `resume` 零调用；部分扩大后仍持有，停止里只剩另一个文件，出口随之变化；全部扩大后同一原件落地（`plan_revised`，不调用指挥），保留候选，跑到 `completed`。
  - 拒绝：同一持有用 `--reject-request` 拒绝（写入 `proposal-rejections/`）；指挥的下一次决定是提问，没有重交同一修订；之后普通 `resume` 零调用同一停止；`abandon` 退出 0。
  - `--scope` 与 `--operator-decision` 同一次提交：对规划阶段的提问一次回答并扩大边界，下一次指挥决定带 `operator_answer`，在扩大后的边界内规划，跑到 `completed`。

### 额度探测

全部经 Harness 自己的探测代码（`harness.quota.snapshot`）运行，只加了只观察的插桩（逐行记录形状、轮询进程树）；探测本身都是零模型调用。

- **Claude `/usage`**：结果行照常出现、零调用核验通过（`num_turns`、`total_cost_usd`、`duration_api_ms` 为 0，`local_command` 为 `usage`），`usage_report` 的形状被归一化接受（`session`、`weekly_all` 与按模型的 `weekly_scoped` 三个窗口；按模型窗口的显示名是不带版本的家族名 `Fable`）。会话内 6 次、去掉 12 个会话变量 4 次，结果行都一样。`--strict-mcp-config` 下没有启动任何 MCP 服务器；`--setting-sources ""` 下用户启用的插件没有加载，但 CLI 内置的 4 个插件照常加载，会话内另多一个来自 `CLAUDE_CODE_PLUGIN_DIRS` 的插件；进程树里没有看到 hooks 的 shell 进程（50 毫秒轮询，可能漏掉极短的进程）。三行在启动后约 1.7–4.2 秒到达，之后 CLI 还要约 1–2 秒才关闭输出（缺陷 1）。全部 32 份 Claude 记录（探测实验与各 run 的样本）：可用 26、超时 4、因超时停用 2。
- **Codex `app-server`**：`account/rateLimits/read` 可用；`initialize` 答复有 `userAgent`，能取出版本；不带 `jsonrpc` 字段的消息被接受；三条消息之间只有两条通知（`remoteControl/status/changed`、`account/updated`），没有需要回应的服务端请求；`rateLimitsByLimitId` 是以 `codex` 为键、与主快照同一 `limitId` 的映射（归一化跳过它，不重复计桶），`limitName` 为 null；答复另有 `normalModelSlug`、`individualLimit`、`rateLimitResetCredits`、`accountId` 等字段，不读不存。答复在启动后约 3.0–4.6 秒到达；31 份记录可用 29、超时 2。stdin 关闭后整个进程组约 0.03–2.2 秒内退出；探测期间派生 `git` 与 `git-remote-https`（会联网），没有看到 MCP 服务器进程，收尾的进程组信号把它们一并结束。
- **强杀**：探测进行中 SIGKILL 探测脚本，遗留的 Claude 探测进程约 7.3 秒后自行退出，Codex 探测进程（连同 `git` 子进程）约 3.2 秒后自行退出，各留下一个只含空 `cwd/` 的 `harness-quota-*` 目录（与文档的“不保证”一致）。0.5 秒的 SIGTERM 宽限：多数探测在宽限内退出（退出码 0），在启动中途被收尾时需要 SIGKILL。
- **`extra_usage`**：见到的是未启用的形状（`is_enabled` 为布尔，其余四个为 null），与归一化一致。
- **容差**：约 21 分钟内 26 份可用 Claude 观测与 29 份可用 Codex 观测里，同一周期内 `resets_at` 的抖动 Claude 是亚秒级（最大约 0.5 秒）、Codex 为 0，都在容差（60 秒、120 秒）之内；`is_active=false` 的窗口同样只有亚秒级抖动。
- **显示名的对应**：真实显示名 `Fable` 按 `quota_wait.relation()` 对 `claude-fable-5-2` 是“适用”、对 `claude-opus-5-5` 是“无关”（纯函数在真实显示名上的结果，不是真实的额度失败）。

### 停止、退出码与监督

- 遇到的停止：`scope_extension/initial_plan`、`scope_extension/revision`、`operator_decision/answer_required`（退出 3）、`invalid_input/plan_unpublished`、`invalid_input/run_state`（退出 2）、`busy/run_lock`（退出 3，在真实 run 执行期间另起 `resume` 得到，没有额度等待时出口为空）。每个停止的退出码、`status RUN` 的 `stop` 投影与 stderr 的 `[harness-json]` 行都与出口表一致，列出的出口照着执行都可行。`[harness-json]` 的停止行总是紧跟在 `harness:` 行之后，但前面还有 `[harness]` 开头的行（缺陷 2）。
- `supervise -- run …` 包住一次以退出码 3 停下的持有：监督回执 `process_observation=exited`、`exit_code` 3、`broker_processes=unknown`，外层退出码 3。
- `watch RUN`：持有时立即输出一行 `blocked` 快照后退出 0；执行期间输出 24 行快照，到 `completed` 退出 0。
- `cleanup-run`：验证留下的 `__pycache__/` 使 `--remove` 报 `blocked`（退出 1，与文档一致），`--remove --remove-ignored` 删除成功；`completed` 与 `abandoned` 的 run 都清理成功。之后对已放弃并清理过的 run 执行 `resume`，提示仍说保留工作树（缺陷 3）。

### 联调发现的缺陷

三个缺陷都是联调发现、已在随后的提交 `7440249` 里修复：

1. Claude 额度探测读到输出结束才算完成、查询时限 5 秒，而真实 CLI 在结果行之后还要 1–2 秒才关闭输出，冷启动更慢：会话内有探测超时，超时使本进程停用 Claude 探测，之后的额度失败就取不到快照、额度等待不会等（这一后果由代码推断，没有在真实额度失败上发生；修复附带的离线回归在修复前的代码上复现了它）。修复：把“查询已答复”与“进程已结束”分开——读到结果行（Codex 是答复）即完成查询，两家的查询时限按实测改为 20 秒；之后至多等 5 秒让 CLI 自行结束（Codex 先关闭输入），等满不算失败、由收尾结束它；进程事实记下收尾信号之前领导进程是否已经自己退出；离线桩加了结果行之后迟迟不关闭输出、慢答复与答复后不退出的时序。修复后的真实复测见[下一节](#修复后的复测)。
2. 文档写 `[harness-json]` 在 stderr 第二行，实际紧跟在 `harness:` 行之后、前面还有别的行。修复：文档改为按前缀与 `event` 找这一行。
3. 对已放弃并清理过的 run 执行 `resume`，提示仍说保留工作树。修复：提示改为在清理前后都成立的说法。

### 修复后的复测

对象：提交 `7440249`，同一份配置副本、同一台机器、同样的 CLI 版本；只测不改。复测期间后台另有一次离线套件在运行（不调用真实 CLI），机器负载约 3–4，探测耗时可能因此略偏大。真实模型调用 4 次（一次 `smoke`），没有遇到额度用尽，没有遗留进程与目录。证据在同一证据目录的 `retest/`。

- **额度探测**：经 `harness.quota.snapshot` 共 25 次（Claude 会话内 10 次、去掉会话变量 5 次，Codex 10 次），全部可用，没有超时；Claude 全部通过零调用核验。每次都是 CLI 自己结束：`exited_before_signal` 为 true、退出码 0、`exit_confirmed` 为 true，5 秒的等待没有一次用满。启动到答复：Claude 1.68–2.90 秒、Codex 2.51–3.81 秒（查询时限 20 秒）；答复到 CLI 自行结束：Claude 1.02–2.01 秒（绝大多数约 2.01 秒）、Codex 0.002–0.018 秒。每次探测之后按记录的领导进程 PID 与临时目录名核对，进程组与临时目录都已不在。探测前后 `~/.claude.json` 都能解析，`~/.claude/`、`~/.codex/` 下 `*.lock`／`*.tmp` 的文件名单没有变化。
- **Codex 的子进程**：另做 3 次 Codex 探测观察收尾时的进程组，其中 1 次 `app-server` 派生的 `git` 子进程在收尾时仍在退出途中，由收尾的进程组信号结束。`exited_before_signal` 只描述领导进程；上面“stdin 关闭后约 0.03–2.2 秒”是整个进程组退出的时间，`app-server` 自己在约 2–18 毫秒内退出。
- **`smoke`（默认指挥，会话内）**：`completed`，4 次调用。4 份样本（每家 `before_use`、`completed` 各一份）全部可用，Claude 两份零调用已核验，四次都是 CLI 自行结束；本进程没有停用探测；`status RUN` 的 `quota` 里 Claude 为 `available`、三个桶各一段。
- **观察入口**：这次 `smoke` 的 run 与联调证据里的三条 run（含 `proposal-rejections/`、`preflight/`、`checkpoints/`、`decisions/`、`worktree-cleanup.json`）复制到临时状态目录后用 `Observer.file_index()` 查看：`skipped.unaddressable` 都是 0，索引里的文件全部能用 `read_file()` 读出，`dispatches/*/mcp.json` 与 `store.lock` 全部被拒。真实 run 里没有出现 `input-indexes/`、`continuations/`、`continuation-rejections/`、`execution-guidance/`、`finding-progress/`，这几个根只有离线用例核对过。
- **帮助与提示**：`python3 -m harness --help` 里 `resume`、`status`、`memory` 都有说明，`memory --help` 列出的五个子命令也有说明。对一条已放弃并清理过的 run 执行 `resume`：退出 2，`invalid_input/run_state`，提示为“该 run 已放弃，不能继续派发；分支与运行证据保留，工作树保留到 cleanup-run 移除为止”。stderr 里停止 JSON 紧跟在 `harness: ` 行之后，前面另有 `[harness] operation_id=…` 行，与文档一致。

## 没有运行的

**以下各项离线结果证明不了，真实账号联调也没有覆盖。**

额度失败与等待（联调期间没有遇到额度用尽，下面这些都无法按需触发）：

1. 真实额度失败那一刻的快照：`session`／`weekly_*` 是否到 100、`resets_at` 是否给出；自动等待、醒来后“先指挥再 worker”的流程；重置时刻加 60 秒之后额度是否确实可用；额度用尽时 `/usage` 是否仍可用、仍是零调用。按模型窗口的显示名只见过一种写法（不带版本的家族名），别名、带日期的 ID 与带版本的显示名没有见过。
2. `CLAUDE_CODE_RETRY_WATCHDOG`：`-p` 下是否生效、默认值与取值格式、等待是否计入墙钟、Harness 能否观察到；默认开启的版本下删除变量是否足够。
3. macOS 休眠期间与醒来后的等待（单调时钟不走、墙钟前跳）；离线只用伪时钟验证过逻辑。
4. 额度等待期间长时间持有运行锁时的 `busy/run_lock` 与 `supervise`、外层脚本的配合。

额度探测：

5. `extra_usage` 启用后各字段的取值类型；Claude 未登录或登录过期时 `/usage` 的结果行，Codex 未登录时的错误码；Codex `initialize` 或 `account/rateLimits/read` 出错时的答复形状。
6. 更长时间（数小时以上）里 `resets_at` 的漂移；容差只在约 21 分钟的样本上核对过。

供应商失败与其他：

7. Codex 的真实额度失败样本；限速、过载、认证三类的真实失败信封（只有额度类有真实样本，来自更早的实测）。
8. 在 Claude Code 会话外运行 `python3 -m harness smoke`（这次只在会话内运行；探测在会话内外各做了）。
9. 旧版本台账下只读 MCP 以退出码 1 拒绝启动时，Claude 与 Codex 客户端各自的表现。

并行（方案设计八）**没有实现**；下面两项属于并行，没有可运行的对象：

10. 两个 worker 真的同时存活、各改各的分道；命令锁生效；worker 运行中主进程被强杀后收回已写好的 seal；并行下的额度消耗与等待；指挥是否把共用文件集中到收口任务；与可比串行 run 的墙钟对照。
11. 取消 Claude worker 的 Bash 直接执行已授权命令之后，worker 是否稳定改用 broker。

离线验证本身没有覆盖的：

- Linux 原生通知（inotify）只做了模拟通知与记账测试，没有在 Linux 上运行；观察网页没有做浏览器人工验收。
- 完整套件只在 Python 3.15.2 上运行，没有在 Python 3.11 上运行。
