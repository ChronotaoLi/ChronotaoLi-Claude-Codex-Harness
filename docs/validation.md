# 验证记录

本文只记录当前版本（台账 18）已经完成的验证。更早版本的记录在 git 历史里；它们验证的是已经改掉的代码，证明不了现在的行为。

## 验证对象

- 分支 `ledger18-2026-11-13`，台账版本 18，提交 `7440249`（运行时、测试与使用文档）；起点 `1361f95`（台账 17）。验证日期 2026-11-17。
- 台账 18 的九步在 `b0ba960` 完成，2026-11-16 在它上面跑过同样的三种离线套件（分片、单进程参考、带绊线的分片；各 1,712 项，通过 1,711、跳过 1）；`7f7fda6` 只改 `docs/recovery.md`；`7440249` 修复真实账号联调发现的缺陷与两处遗留（见[真实账号联调](#真实账号联调2026-11-17)），下面的离线结果是在它上面重新跑的。
- 其后的 `08766eb` 落实代码审阅的两条建议与复测报的两处小问题：改的是各子命令帮助页的文字、`_exited_unreaped()` 在缺少 `waitid` 常量的平台上的判断、注释、探测与帮助的测试、两份使用文档。改后重跑了带绊线的分片套件（收集 1,725 = 执行 1,725，通过 1,724、跳过 1，768.4 秒，真实 CLI 调用 0 次，没有遗留），并在去掉了 `os.waitid` 的进程里跑了三个额度模块（`test_quota_probes` 30 项、`test_quota_sampling` 33 项、`test_quota_wait` 51 项，全部通过，其中 3 项只在能观察到领导进程退出的平台上成立、按设计跳过）。单进程参考跑法没有在 `08766eb` 上重跑。其余提交只改本记录。
- `602da5f` 之上的 `3412536` 落实了第 29 轮缺陷审查的 A–F 六项修复：运行时、测试、使用文档与 worker 技能共 28 个文件（不含本记录）。验证是在提交之前、对同一份内容做的，以树对象 `8274ee356e2358115958aa269d1e579c328b5fe3` 标识，`git rev-parse '3412536^{tree}'` 等于它。它的验证见[第 29 轮审查的修复](#第-29-轮审查的修复2026-11-17)。
- `6047f8d` 之上的 `1c2f749` 是“跨项目使用的适配”：运行时、测试、使用文档（含 `AGENTS.md`）、根目录 `harness.toml`、`examples/project.toml` 与 `operator/harness-operator/SKILL.md`，相对 `6047f8d` 共 29 个文件（+1,730／−184，不含本记录）。验证是在提交之前、对同一份内容做的，以树对象 `b442b114aa9b08116338d449b763cc263f07954c` 标识，`git rev-parse '1c2f749^{tree}'` 等于它；带绊线的分片全量跑在它的父树 `321272c1b15a3816b3bd2570c7d4156e62820f27` 上，两者只差 `docs/operations.md` 里的一句话。它的验证见[跨项目使用的适配](#跨项目使用的适配2026-11-17)。
- macOS（Darwin 25.6.0），Python 3.15.2。
- 下面的离线结果都来自模拟模型响应（`DemoBackend`、各测试的脚本化后端，以及打印合成信封的本地子进程）加真实 Git、真实验证子进程与真实信号。没有调用任何真实模型，也没有运行真实的 `claude`、`codex`：凡用到 `CLIBackend` 或额度探测的用例，两个可执行文件都换成记录调用的桩。离线结果不证明任何真实模型调用或真实 CLI 接口可用；真实账号上做过的见下面的“真实账号联调”一节，以及跨项目使用的适配一节里的[真实账号上的检查](#真实账号上的检查)；仍然没有运行的见[没有运行的](#没有运行的)。

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
| 联调修复 | 额度探测：答复即完成查询、之后有界地等 CLI 自行结束、查询时限按实测放宽；观察入口的固定根与实际布局对齐并加守卫；帮助与提示文字 | `7440249`、补充 `08766eb` | 等 CLI 自行结束期间的 SIGINT（约 0.1 秒内传播）、第二条结果行、不是 JSON 的行、输出超限；等满之后由收尾结束；经额度等待走一遍“晚关输出的 CLI → 失败样本可用 → 进入等待” | 23 条，全部杀死（其中 2 条第一轮存活，补了桩的时序模式与用例后杀死）；新回归在 `779ecbf` 的代码上失败；补充另 8 条（含 4 条帮助页），全部杀死；审阅者另验 11 条 |

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
| 联调修复 | 无（任务书即规格） | 可以继续：0 条阻断、2 条建议（没有 `os.waitid` 的平台上一个新用例的期望不对，运行时行为正确；一个时序用例在慢机器上余量偏小）。审阅者用真实子进程核对了只看不回收的 `waitid` 之后领导进程仍未被回收、`Popen.wait()` 取得真实退出码，读取与等待中的 SIGINT 约 0.03–0.09 秒到检查点，观察入口的路径规则没有放松 | 建议由 `08766eb` 落实；没有阻断，未再做确认审阅 |

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
- **复测另报的两处小问题**：各子命令自己的帮助页没有描述段、若干参数没有说明；使用文档与注释把整个进程组的退出时间写成了 `app-server` 的。两处由 `08766eb` 修正；修正之后没有再对真实 CLI 复测。

## 第 29 轮审查的修复（2026-11-17）

对象是提交 `3412536` 的内容，即树 `8274ee356e2358115958aa269d1e579c328b5fe3`（见[验证对象](#验证对象)）。下面全部是离线验证：模拟模型响应加真实 Git、真实文件系统与真实子进程，没有调用真实模型，也没有运行真实的 `claude`、`codex`。

### 修复了什么

| 问题 | 现在的行为 |
| --- | --- |
| A | `stops.make()` 接受它自己产出的停止值：本行登记的列表字段的 `<字段>_omitted` 是非负整数时保留，并加上新截短的条数，否则与其他不合格的值一样记入 `dropped_fields`；`make()`、`_fit()`、`validate()` 对“哪些字段可带 `_omitted`”统一为本行登记的列表字段。于是额度用尽、满窗口多于 16 个、等待判断决定不等时，停止仍是 `provider_quota/usage_limit`（退出码 3、带 `wait_reason` 与失败样本字段），不会变成 `unclassified/unexpected_exception` 和 CLI 回溯。 |
| B | 恢复时、进入执行循环前重放已封存而未交接的 worker 结果，其验证与任务审阅遇到 Claude 额度用尽：照旧不等待，停止值带 `wait_reason=not_running`、`waits=0`；下一次 `resume` 在执行循环里照常判断并等待。 |
| C | worker 调用期间 run 工作树的 HEAD 被移动：`WorkerReceipts.seal()` 先算候选快照、再取 HEAD 与 `expected_head` 比较，不同就不封存；停止是 `evidence/unsealed_return`（出口只有 `abandon RUN`，原因在派发条目的 `error`），恢复 HEAD 后的 `resume` 仍是这个停止，不会落到没有出口的未分类停止。 |
| D | 受保护根（`.git`、`.harness`、`.codex`、`.claude`）按折叠后的名称判定（`harness.util.fold_name`：去掉格式字符、NFKC、不分大小写、忽略尾部的点与空格）。规划（范围模式、`--scope` 边界、修订追加的范围、证据要求路径）、接受（候选检查）、删除工具，以及修复声明、候选证据、检查点来源路径的判定共用 `git.protected_name()` 一个入口；纯名称判定，在所有平台执行。`.CLAUDE/**` 这类别名在创建 run 或发布修订前被拒；宽范围之下写进别名目录的候选以 `scope_violation/protected_path` 被拒，不产生接受提交。 |
| E | 一份计划里的任务 ID 在 `fold_name` 之下必须互不相同（任务的验证与预检证据目录以 ID 命名）：供给计划在创建 run 前被拒（`invalid_input/input_schema`）；模型的首版计划与修订是无效输出，不发布计划或修订；加载既有台账时同样核对，折叠冲突与完全重复一样以 `evidence/load_integrity` 停止，台账不变。 |
| F | 同一批检查点的 ID 在 `fold_name` 之下必须互不相同：冲突按重复声明处理，在写出任何检查点文件前整批丢弃（`checkpoints_discarded` 事件带两个原始 ID），任务照常验证、审阅、接受；已发布或已登记的检查点产物被改动仍然停止。同一任务里后一次 dispatch 的同名检查点取代前一次的，同名也按折叠后的 ID 判断。worker 技能里补了一句说明。 |

### 离线套件

在主工作树上运行，对象就是上面那棵树；`claude`／`codex` 两个绊线脚本放在 `PATH` 最前（被调用就记日志并以 97 退出，同[真实 CLI 绊线](#真实-cli-绊线)）：

| 跑法 | 收集 | 执行 | 通过 | 失败 | 跳过 | 用时 |
| --- | --- | --- | --- | --- | --- | --- |
| 分片 `python3 tests/run_shards.py --shards 4`，带绊线 | 1,748 | 1,748 | 1,747 | 0 | 1 | 884.5 秒 |

运行期间机器上没有别的测试进程。收集集合与执行集合逐项相等（缺失、重复、多出都是 0），错误 0。跳过的一项仍是本机文件系统拒绝非 UTF-8 文件名的那一项。相对 `602da5f` 的 1,725 项多 23 项，都是这六项修复的回归。绊线被调用 **0** 次（日志文件没有产生）；运行结束后没有遗留的测试进程。`python3 -m compileall -q harness tests` 与 `git diff --check` 通过。单进程参考跑法（`python3 -m unittest discover -s tests`）没有在这棵树上重跑。

每项修复的新回归都由实现者在撤掉该项运行时改动的代码上跑过并确认失败，失败原因是缺陷本身；审阅者在父树上独立复核了同一点（见下节）。E 里一条证据完整性用例是守卫：它用明显不同的 ID 证明后一任务执行与恢复之后，前一任务已登记的四份证据字节不变，在修复前的代码上也通过。

### 独立审阅

审阅者是 `gpt-6.1-sol`，每个问题一次、每次新进程（`codex exec`），只在导出的树副本里工作，主工作树每次都没有被改动；审阅对象是各问题修复后的树对象（未提交的快照）。共 8 次：6 次代码审阅、2 次窄确认。审阅原文在被忽略的 `.harness/reviews/2026-11-17-v29-fixes/reviews/`。

| 问题 | 代码审阅 | 修复与确认 |
| --- | --- | --- |
| A | 可以继续：0 条阻断、1 条建议（性质测试借用了生产代码的展平与构造函数）。审阅者核对了全部 11 行、25 个列表字段的重建与计数累计；1,280 份修复前生成的合法停止值在修复后仍能验证并原样重建；父树的 `stops.py` 上三条新回归都失败；4 种变异全部检出 | 建议已落实（测试侧独立表达“项”与省略计数）；没有阻断，未再做确认审阅 |
| B | 可以继续：0 条阻断、0 条建议。审阅者核对了进入执行循环前除首版规划与重放外没有别的模型调用；非额度错误经过这层包装后与父树逐项一致；4 种变异全部检出（外层统一写法使既有的 14 个对照子用例失败） | — |
| C | 可以继续：0 条阻断、1 条建议（把“提交恰好落在算候选快照期间”固化为回归）。审阅者核对了正常路径的封存文件与父树逐字节相同；3 种变异检出 | 建议已落实；没有阻断，未再做确认审阅 |
| D | 可以继续：0 条阻断、2 条建议（补一道能发现“删除工具又有了自己的一份规则”的回归；实现者一处排查说明不准确，不涉及代码）。审阅者在不区分大小写的文件系统上实测了显式别名、`--scope`、修订追加、五种宽范围下的候选及尾点、尾空格、兼容字符、零宽字符变体，全部被拒；合法路径不被误拒；删除语法 3,615 种路径组合与修复前一致 | 建议 1 已落实（把共用判定临时多拒一个名字，七道门全部跟着拒绝；8 种“改回本地规则”的变异各自被检出）；没有阻断，未再做确认审阅 |
| E | 需先修改：1 条阻断（加载既有台账时的任务 ID 唯一性检查仍按原始字符串比较，带 `greet`／`GREET` 的既有台账恢复后仍会覆盖前一任务的证据）、2 条建议（补强三处回归断言；检查点 ID 的同类碰撞，即问题 F） | 加载处改用同一规则并补恢复入口的回归；确认：可以继续（审阅者用修复前的校验器独立造出冲突台账复核） |
| F | 可以继续：0 条阻断、2 条建议（跨 dispatch 的替代键也按折叠后的 ID；worker 技能补一句说明）。审阅者核对了首次交接与封存后重放都在发布前整批丢弃；未登记产物被改、JSON 损坏、证据副本被改三种情形仍然停止；4 种变异全部检出 | 两条建议已落实；确认：可以继续 |

## 跨项目使用的适配（2026-11-17）

对象是提交 `1c2f749` 的内容，即树 `b442b114aa9b08116338d449b763cc263f07954c`（见[验证对象](#验证对象)）。规格、执行记录与审阅原文在被忽略的 `.harness/design-proposals/2026-11-17-cross-project-execution/`。“离线套件”与“独立审阅”是离线验证：模拟模型响应加真实 Git、真实文件系统与真实子进程，没有调用真实模型，也没有运行真实的 `claude`、`codex`。“安装入口”只运行了不调用模型的 `harness doctor`。调用了真实模型的只有[真实账号上的检查](#真实账号上的检查)。

### 改了什么

| 项目 | 现在的行为 |
| --- | --- |
| 两份配置 | 操作者配置（`--config`，默认仍是源码目录的 `harness.toml`）只允许 `roles`、`runtime`、`memory`、`shared`；项目配置固定在目标仓库 Git 根目录的 `.harness/project.toml`，只允许 `verification`、`permissions`、`runtime`，不向上级目录搜索、没有回退，也没有改它位置的参数。有效配置 = 内置默认值 ← 操作者配置 ← 项目配置：`runtime` 逐键覆盖，每个值整体替换（`claude_max_turns_by_role` 这类表与数组都不合并）；`verification`、`permissions` 只来自项目配置；合并结果再经原有的全部校验（`Config.validate`，规则不变）。写错层的节（错误指出它该写在哪份文件里）、不是表的节与未知键（给出完整键路径）都被拒绝，错误写明是哪一份文件；UTF-8 与 TOML 的解析错误照旧不带文件名。`shared` 的路径仍相对操作者配置所在目录解析，模型 ID 原样保留。根目录 `harness.toml` 去掉了 `[verification]` 与 `[permissions]`，成为纯操作者配置；新增带注释的 `examples/project.toml`。 |
| 各命令读哪份文件 | `run`、`resume`、`permission-diagnostics` 读两份，项目配置必须存在。`abandon`、`doctor`、`demo`、`smoke`、`sync` 与 `python3 -m harness.permission_smoke`、`python3 -m harness.tool_smoke` 的有效配置只来自操作者配置（`doctor` 另读项目配置，只为报告，见“来源的呈现”）。`status`、`watch`、`dashboard`、`memory`、`prepare-run`、`cleanup-run`、`supervise` 照旧不读配置。`demo`／`smoke` 与两个独立 smoke 自己设置验证命令和 worker 命令，目标仓库里有项目配置也不读取、不执行其中的命令。 |
| 拒绝 | 项目配置不存在时，`run`、`resume`、`permission-diagnostics` 以退出码 2、`invalid_input/cli_arguments`、`data.option` = `--repo` 拒绝，错误给出期望的绝对路径并指向 `examples/project.toml`。拒绝发生在任何写入之前：不创建操作记录，`.git/info/exclude`、私有目录的锁文件与忽略文件都不变，不创建 run、分支与工作树，不调用模型。项目配置存在但读不出、不是合法 TOML 或内容不合格时，按操作者配置出同样错误时的方式报错（停止类型与退出码相同，不新增停止类型），同样在任何写入之前。`abandon` 不读项目配置，项目配置缺失或损坏时照常可用；不读配置的命令在两份文件都损坏时照常可用。`run` 仍要求有效配置的 `verification.commands` 非空。 |
| 恢复、额度等待与台账 | `resume` 与 `run` 用同一个加载方式，把有效配置与台账当前的 `config_snapshot` 比较，允许与拒绝的差异不变（采纳的放宽替换这份快照，之后以它为基准）。没有按文件路径或文件摘要的门禁：操作者配置换了路径而内容等价、任一文件只改注释，恢复照常。额度等待醒来后不重读任何一份配置文件。台账结构不变、不升版本：`config_path` 是操作者配置的路径，`config_snapshot` 是有效配置；观察层与观察网页没有改。 |
| 来源的呈现 | 操作记录与 `doctor` 的 `config` 字段是 `{"path", "sha256", "project"}`：`path`／`sha256` 是操作者配置；`project` 在这次加载读了项目配置时是 `{"path", "sha256"}`，否则是 `null`。摘要是加载时那一次读取、实际解析的字节的摘要，记录时不再读文件；不是由加载得到的 `Config`（测试里直接构造）摘要为 `null`。`doctor` 另有顶层字段 `project`：`{"path", "exists", "sha256", "error"}`，`--repo` 不在 Git 仓库里时整个字段为 `null`，`path` 总是期望的绝对路径。文件不存在（含悬空链接）时 `exists=false`，`sha256`、`error` 为 `null`；与已加载的操作者配置合在一起校验通过时有摘要、`error` 为 `null`；读不出时 `sha256=null`、`error` 是错误首行；读得出但不是合法 TOML 或内容不合格时有摘要、`error` 是错误首行。摘要与错误来自同一次读取，诊断不重读操作者配置；嵌套过深、解析时递归超限的文件也只记为 `error`，`doctor` 不崩溃。`doctor` 不因项目配置缺失或无效而拒绝，退出码仍只取决于 CLI 登录。`run` 启动时 stderr 回显三条：`[harness] config=…`、`[harness] project=…`、`[harness] verification=…`（这一条的 JSON 可跨多行）。项目配置的路径总是显示固定位置：是符号链接时读取跟随链接，显示的不是链接目标。提示操作者追加命令授权或配置验证命令的文字（规划时发现未授权的任务命令、`missing_commands` 停止、worker 环境受阻、空验证命令）指明是项目配置文件。 |
| 外层 skill 与角色隔离 | 新增 `operator/harness-operator/SKILL.md`（frontmatter 的 `name` 是 `harness-operator`，正文英文，`description` 带中文触发词），供在别的项目里替所有者调用 Harness 的外层会话使用；它不放在会枚举给内部角色、计入共用资源摘要的 `shared/skills/` 下。内容包括入口命令与两份配置；首次使用先 `doctor`、看 `project.exists`；项目配置由所有者确认内容后才写入；启动前与所有者确认可改动的范围并用 `--scope` 提交；按退出码分流；外层工具没有可靠的后台任务或沙箱受限时，`run` 与需要继续执行的 `resume` 交给所有者在普通终端运行；先请示与不能做的事。分发方式是文档里的两条 `ln -s`，没有安装子命令。内部角色的启动参数：Claude 担任的每个角色带 `--disable-slash-commands`，Codex 担任的每个角色带 `-c skills.config=[{"name" = "harness-operator", "enabled" = false}]`；被禁用的名字与 skill 的 `name` 由测试保证相等。不保证：不关闭 Codex 角色对其他用户级 skill、系统 skill 与插件 skill 的发现；不阻止角色用文件工具读到这份 skill 的正文。 |
| 文档 | `README.md`（安装入口、两份配置）、`docs/operations.md`（各命令读哪份文件、`doctor` 的 `project`、外层操作说明与 skill 的链接方式）、`docs/recovery.md`（恢复的比较基准）、`docs/design.md`（不变量 40 与限制）、`AGENTS.md` 按上面的行为改写。 |

### 离线套件

在主工作树上运行，对象是父树 `321272c1…`（与 `b442b114…` 只差 `docs/operations.md` 的一句话）；本仓库自己的项目配置 `.harness/project.toml`（本地、被忽略）已存在；`claude`／`codex` 两个绊线脚本放在 `PATH` 最前（同[真实 CLI 绊线](#真实-cli-绊线)）：

| 跑法 | 收集 | 执行 | 通过 | 失败 | 错误 | 跳过 | 用时 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 分片 `python3 tests/run_shards.py --shards 4`，带绊线 | 1,786 | 1,786 | 1,785 | 0 | 0 | 1 | 896.9 秒 |

收集集合与执行集合逐项相等（缺失、重复、多出都是 0）。跳过的一项仍是本机文件系统拒绝非 UTF-8 文件名的那一项。相对基线 `6047f8d` 的 1,748 项多 38 项。绊线被调用 **0** 次（日志文件没有产生）；运行结束后没有遗留的测试进程。`compileall` 与 `git diff --check` 在 `b442b114…` 上通过。单进程参考跑法（`python3 -m unittest discover -s tests`）没有在这棵树上重跑。

各实施任务结束时，T1a、T5、T1b、T2、T2b 也各自在自己的树上跑过带绊线的分片全量（收集分别为 1,759、1,750、1,770、1,779、1,783 项，每次跳过 1 项、其余全部通过，绊线 0 次）。

改运行时的每个任务，实现者都在撤掉本任务运行时改动的代码上跑过自己的新回归并确认失败，失败原因是缺少新行为：T1a 的 `test_config_layers` 11 项全部失败；T5 失败 33 个（20 个 Claude 启动参数缺 `--disable-slash-commands`、12 个 Codex 缺 `skills.config`、名字相等的接缝用例 1 个）；T1b 的缺失拒绝 2 项（9 个子用例，退出码是 1 而不是 2）；T2 有 33 个失败条目（记录与 `doctor` 缺 `project`、摘要是改写后的、路径显示的是链接目标等）；T2b 的读取次数、两个方向的竞态与两层等价性 3 个用例；T2c 的新用例两个子情况都以 `RecursionError` 出错。T1b 的另外 9 项新测试在撤掉后仍通过，是纯回归：`abandon` 在项目配置缺失或损坏时可用、损坏时与操作者配置同样报错、两份文件都损坏时不读配置的命令可用、`demo`／`smoke` 与两个独立 smoke 不读项目配置、恢复时两份文件里的放宽与拒绝及等价文件、额度等待醒来不重读。只改测试的 T1a2 与 T1b2 用变异确认新断言有效：把读错误改报成操作者配置缺失、把仓库根改成主检出、醒来后重读并丢弃、删掉空验证命令的启动门禁，都被对应用例抓住。

### 独立审阅

审阅者是 `gpt-6.1-sol`，每次一个新进程（`codex exec --ephemeral --ignore-user-config`），只在导出的树副本里运行命令；审阅驱动记下的主工作树状态在每次审阅前后都相同。审阅对象是各任务完成时的树对象（未提交的快照）。共 11 次：规格审阅 1 次（`spec-1`），代码或文档审阅 6 次（`code-T4`、`code-T1a`、`code-T5`、`code-T1b`、`code-T2`、`docs-T3`），确认审阅 4 次（`confirm-T4`、`confirm-T2`、`confirm-T2c`、`confirm-T3`）。实施由 Opus 完成，每个任务（含修复与跟进）一个全新的代理。审阅原文在被忽略的 `.harness/design-proposals/2026-11-17-cross-project-execution/reviews/`。

| 任务 | 审阅 | 处理与确认 |
| --- | --- | --- |
| 规格 | `spec-1`：需先修改规格，2 条阻断（3.3 的测试矩阵含被禁止的 Codex worker；外层会话权限不足时交给终端的规则漏了 `resume`）、5 条建议（补齐拒绝、放弃、等价文件、摘要在改写后不变等测试；写清 `doctor` 的失败字段；测试改造范围；受影响模块与归属；把 T1 拆成两个任务）。只读，抽查了规格引用的行号，没有运行测试 | 全部并入规格；T1 拆成 T1a、T1b，命令接线放在 T1a（与建议的切法不同：不接线 T1a 结束时全套测试过不了）；没有再做规格确认审阅 |
| T4 外层 skill | `code-T4`（树 `56885f82`）：需先修改，1 条阻断（“读结果”一段说本次停止同时在 `status RUN` 与 stderr，漏了操作级拒绝：恢复预检被拒时本次停止只在 stderr 与操作记录里，台账保留旧停止）、2 条建议（`doctor` 只检查、不修复登录；普通 `resume` 可能直接继续执行）。只读，没有运行测试 | T4b 处理阻断与两条建议；确认 `confirm-T4`（树 `b9512cc2`）：可以继续，0 条阻断、1 条措辞建议（操作记录不是每次都有，例如重复操作 ID、项目配置缺失时不创建），由 T4c 改掉一个分句，只改措辞，未再送审 |
| T1a 两层加载与接线 | `code-T1a`（树 `6f6c819c`）：可以继续，0 条阻断、2 条建议（`read_layer` 的注释说每种错误都带文件名，TOML／UTF-8 错误其实不带；把探针里的工作树、符号链接、不可读文件场景固化为回归）。审阅者的离线探针覆盖子目录、嵌套仓库与 linked worktree 的定位，以及符号链接、目录、不可读文件与悬空链接；与父树比较，7 份有效配置与 49 组 `resume_changes` 结果完全一致；项目配置损坏、并禁止读取 `verification`／`permissions` 时 `abandon` 仍成功；七个既有测试模块的 104 个测试方法都保留。跑了 184 项：182 通过，2 项环境错误（导出副本没有 `.git`、沙箱禁止 HTTP 监听），父树同一用例同样出错 | T1a2 收窄注释并补 3 个回归（Git 工作树定位、符号链接可加载、目录／不可读／悬空链接不被误报为操作者配置缺失）；只有测试与注释，未再送审 |
| T5 角色隔离 | `code-T5`（树 `d6243d57`）：可以继续，0 条阻断、0 条建议。审阅者的探针：16 种角色配置中 8 种合法、8 种被拒，生成 32 组参数，恰好覆盖 5 种“角色 × CLI”；只改运行时常量或只改 skill 的 `name`，接缝用例都失败；Claude 开关的位置不会吞掉相邻参数，Codex 覆盖项能被 `tomllib` 解析成单项数组、`enabled` 为布尔 `false`。跑了 106 项，加 2 项额度探测的供应商快照测试，全部通过 | — |
| T1b 拒绝保证与回归 | `code-T1b`（树 `e18d6df0`）：可以继续，0 条阻断、2 条建议（额度等待用例抓不住“醒来后重读再丢弃”；空验证命令的启动门禁没有测试，删掉门禁后新测试仍全部通过）。审阅者用全仓库探针比较目标目录的全部内容（含 `.git`、文件字节、目录项、符号链接、权限与 mtime／ctime）：项目配置缺失 12 次、损坏及真实 `EACCES` 42 次，变化的路径都是 0，Git 调用只有 `rev-parse`；4 种变异（`abandon` 读项目配置、恢复接受验证命令的变化、醒来采用重载的配置、`permission_smoke` 读项目配置）各自被对应测试抓住；换回父树的 CLI 后两个缺失配置用例失败、其余 9 项通过。跑了 124 项：123 通过，1 项环境错误（导出副本没有 `.git`，父树同样） | T1b2 只改测试：给醒来后的配置读取加监视、补空验证命令的启动拒绝用例，两种变异都被抓住；未再送审 |
| T2 来源与观察 | `code-T2`（树 `a84e3ba4`）：可以继续，0 条阻断、1 条建议（`doctor` 的 `project` 先算摘要再重读校验，探针复现了摘要来自第一次读取、错误来自第二次读取）。审阅者核对：加载后把两份源文件改坏，记录与 `doctor` 里仍是加载时的摘要；3 个受保护命令共 15 次缺失拒绝都是退出码 2、`data.option=--repo`，私有目录、exclude、锁、refs、工作树与状态都不变。跑了 164 项，全部通过 | 按根因处理：T2b 把加载拆成读文件、解析并检查一层、合成有效配置并校验三步（`read_layer`、`parse_layer`、`Config.effective`），`doctor` 对同一份字节算摘要并校验、不重读操作者配置。确认 `confirm-T2`（树 `bf4a4553`）：可以继续，0 条阻断；父树与任务树 49 组 `Config.load` 结果完全一致，1,400 组等价性与隔离检查通过，临时换回父树的诊断函数后三个读取分支与两个竞态方向都失败，跑了 124 项全部通过。它另报一个既有问题（项目配置约 2,000 层嵌套会让 `doctor` 崩溃），由 T2c 在 `project_report()` 里捕获 `RecursionError` 并补回归。确认 `confirm-T2c`（树 `748ad2ab`）：可以继续，0 条阻断；新用例在 Python 3.11.17 与 3.15.2 上都通过，在父树上以 `RecursionError` 失败；2 条建议中，测试注释里的“always”由 T2d 收窄（只改注释，未再送审），CLI 顶层不捕获 `RecursionError` 没有处理（见[没有运行的](#没有运行的)） |
| T3 文档 | `docs-T3`（树 `e9088fd9`）：需先修改，2 条阻断（`docs/recovery.md` 把恢复的比较基准写成创建时的快照，实际是台账当前的 `config_snapshot`，探针复现了 300 → 1000 → 600 时第二次恢复被拒；`docs/operations.md` 写重复操作 ID 退出 1，实际是 `invalid_input/cli_arguments`、退出 2）、4 条建议（不变量 40 补全“只读一次”；“三行”回显改为三条、JSON 可跨多行；首次使用先建 `.harness/` 与 skill 目录；skill 对 `--config` 的要求比代码更严）。文档代理按代码写的三处判断都被确认；135 个站内链接有效，停止表 88 个行标识与基线一致。跑了 158 项加 51 项，全部通过 | T3b 处理两条阻断与四条建议（`README.md`、三份文档与 skill）。确认 `confirm-T3`（树 `321272c1`）：可以继续，0 条阻断，两条阻断都已解决，隔离验收的措辞没有超出事实，停止表 88 个行标识不变，跑了 80 项加 12 项与 1 个探针，全部通过；1 条建议（`docs/operations.md` 一句容易混淆恢复的阶段）由 T3c 按代码改写，只改这一句，未再送审，得到树 `b442b114` |

### 安装入口

在临时虚拟环境里 `uv pip install -e`（uv 选用 Python 3.13.18），以及用 `UV_TOOL_DIR`、`UV_TOOL_BIN_DIR` 把工具目录指到临时位置的 `uv tool install --editable`，都装出了可执行的 `harness`。前者在别的目录运行 `harness doctor` 退出 0，`harness.package`、默认的 `config.path` 与 `shared` 都解析到源码目录；后者装出的 `harness` 在别的目录能运行。两次安装的源码都是导出到临时目录的基线 `6047f8d`，不是适配后的树；适配的 29 个文件里没有 `pyproject.toml`。安装在源码目录留下 `personal_agent_harness.egg-info/`，仓库的 `.gitignore` 忽略 `*.egg-info/`。没有往真实的全局工具目录安装。

### 真实账号上的检查

这是跨项目使用的适配里唯一调用了真实模型的部分：隔离验收 5 次、端到端 `smoke` 4 次。证据在被忽略的 `.harness/validation/cross-project-real-20261117/`（`README.md` 是结论，`probe.py` 是探针，`smoke/summary.json` 由 smoke 各派发的 `command.json` 整理）。

**隔离验收**：对象是树 `e9088fd9…` 的 `harness/adapters.py`（这个文件在 `b442b114…` 里没有再改），codex-cli 0.169.0、Claude Code 2.1.326。把 `operator/harness-operator` 临时软链接到 `~/.claude/skills/` 与 `~/.codex/skills/`（验收结束后已删除），用 `CLIBackend.command()` 实际生成的启动参数启动角色，问它本会话可用哪些 skill；每个模型用例只改动用例名所说的那一处，并且像 `permission_smoke` 那样去掉输出 schema，Claude 改用 `stream-json` 以取得启动时的 `init` 事件，外部 MCP 置空。共 5 次真实模型调用（Codex 2 次、Claude 3 次），另有 1 项不调用模型。

| 用例 | 启动参数 | 结果 |
| --- | --- | --- |
| `codex-list`（不调用模型） | `codex app-server`，不加／加上适配器生成的 `skills.config` 覆盖 | 发现 8 个 skill；`harness-operator`（用户级）不加时 `enabled=true`，加上后 `enabled=false` |
| `codex-exact` | 审阅角色的原样参数（`gpt-6.1-sol`） | 角色列出 5 个别的 skill，没有 `harness-operator`；事件流里也没有这个名字 |
| `codex-control` | 同上，去掉 `skills.config` 覆盖 | 多列出 `harness-operator`（正对照成立） |
| `claude-exact` | 指挥角色的原样参数（`claude-fable-5-2`） | `init` 事件里 skill 0 个、斜杠命令 0 个 |
| `claude-control` | 同上，去掉 `--disable-slash-commands` | skill 19 个、斜杠命令 55 个，其中没有 `harness-operator` |
| `claude-validity` | 再去掉 `--setting-sources ""` | skill 37 个、斜杠命令 73 个，其中有 `harness-operator`（探针看得到它） |

- Codex：按名称禁用的覆盖项在 `codex exec` 的最终上下文里生效，正对照成立。同时证实了既有情况：Codex 角色仍看得到其他用户级 skill 与系统 skill，与“不保证”一致。
- Claude：用户级的 `harness-operator` 在原有的 `--setting-sources ""` 下就已经不可见，新开关 `--disable-slash-commands` 是第二层，它把其余内置与插件 skill、斜杠命令也清空（19／55 → 0／0）。三次调用里模型自己的回答都是 NONE（角色没有 `Skill` 工具），所以 Claude 一侧的依据是 `init` 事件，不是模型自述。
- 局限：单机，只在 codex-cli 0.169.0 与 Claude Code 2.1.326 上；每种 CLI 各一个角色（Codex 审阅、Claude 指挥；各角色的隔离参数由同一处生成，离线测试覆盖全部允许的组合）；Codex 的依据是模型自述加正对照，以及不调用模型的发现列表。Claude 证据里含账号额度信息的 `rate_limit_event` 行已从保存的输出里删除。

**端到端 `smoke`**：在主工作树（树 `b442b114…`）上运行 `python3 -m harness smoke`（默认指挥），补上隔离开关与输出 schema 参数同时使用的真实调用。退出码 0，`completed`，158 秒；4 次真实模型调用全部完成，每次都同时带隔离开关与 schema 参数：指挥 `decide`（Codex，`gpt-6-astra`，`skills.config` 覆盖与 `--output-schema`）、worker `implement`（Claude，`claude-opus-5-5`，`--disable-slash-commands` 与 `--json-schema`）、任务审阅 `review` 与整体审阅 `final_review`（Codex，`gpt-6.1-sol`，同指挥）；另有两次验证派发（真实 `unittest` 与 Git，不调用模型）。run 目录在系统临时目录里，没有复制进证据（其中有额度样本）。`smoke --commander fable` 没有跑。

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

跨项目使用的适配（真实账号上只做了[隔离验收与一次 `smoke`](#真实账号上的检查)）：

12. 没有用真实的外层会话走过一遍：没有在别的项目里由 Claude Code 或 Codex 会话加载 `harness-operator` 去驱动一次 run；skill 是否按描述被触发、外层会话是否照其中的规则行事都没有验证。外层 Codex 会话的沙箱下能否启动 `run` 没有实测。
13. 隔离只在 codex-cli 0.169.0 与 Claude Code 2.1.326、每种 CLI 各一个角色上做过真实检查；启动参数改变之后，`smoke --commander fable`、`python3 -m harness.permission_smoke`、`python3 -m harness.tool_smoke` 没有重跑。
14. 没有往真实的全局工具目录执行 `uv tool install --editable`，安装检查用的是基线 `6047f8d` 的导出副本；把 skill 软链接到用户级 skill 目录只在隔离验收期间临时做过。

离线验证本身没有覆盖的：

- Linux 原生通知（inotify）只做了模拟通知与记账测试，没有在 Linux 上运行；观察网页没有做浏览器人工验收。
- 完整套件只在 Python 3.15.2 上运行，没有在 Python 3.11 上运行；没有 `os.waitid` 的平台只用去掉该函数的进程模拟过（三个额度模块），没有在那样的 Python 上运行。
- 第 29 轮审查的修复之后，单进程参考跑法没有重跑；真实账号联调没有重跑（修复不涉及供应商接口，但 worker 技能的文本改了一句，共享资产摘要随之改变），`smoke` 只在包含这些修复的跨项目适配的树上跑过一次（默认指挥），见[真实账号上的检查](#真实账号上的检查)。
- 跨项目使用的适配之后，单进程参考跑法没有在这棵树上重跑；完整套件仍只在 Python 3.15.2 上运行（审阅者在 Python 3.11.17 上只跑过 T2c 的一条新用例和一个探针）。
- 大小写别名在文件系统上的实际行为（`.CLAUDE/settings.json` 与 `.claude/settings.json` 是同一个文件）只在本机不区分大小写的 APFS 上验证过；在区分大小写的文件系统上那一条用例按设计跳过，其余纯名称判定的用例没有在那样的文件系统上运行过。
- 审阅中记下、没有在第 29 轮审查的修复里处理的既有缺口（修复前后行为相同，与大小写别名无关）：普通名字的符号链接指向受保护目录时，链接本身可以被接受；受保护名只在路径首段判定，`docs/.claude/x` 这类非首段路径按现有策略允许。
- 审阅与实施中记下、跨项目使用的适配没有处理的（修复前后行为相同或属于范围外）：
  - Codex 担任的角色仍能发现其他用户级 skill、系统 skill 与插件 skill（codex-cli 0.169.0 没有稳定的全部关闭开关）；角色仍可用文件工具读到外层 skill 的正文。
  - `run`、`resume` 等命令加载嵌套过深的配置文件时，CLI 顶层不捕获 `RecursionError`，会打印回溯退出；操作者配置在基线上就是这样（`doctor` 加载嵌套过深的操作者配置也一样）。
  - 项目配置合法但 `verification.commands` 为空时，`run` 的拒绝是未分类的错误（退出码 1），并且发生在创建操作记录与初始化私有状态之后。
  - 观察网页里“追加精确授权后普通 resume”的提示没有指明是项目配置文件。
