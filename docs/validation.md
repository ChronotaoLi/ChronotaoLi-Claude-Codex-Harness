# 验证记录

最近验证日期：2026-10-22。本记录描述已执行的检查，不保证将来账号权限或 CLI 行为不变。

## 首版离线基线

`python3 -W error::ResourceWarning -m unittest discover -s tests -v`：27 项通过。

覆盖：DAG 环与非法依赖、任务文件范围、过期 dispatch 拒绝、结构化结果校验、不同任务使用不同实例、审阅拒绝后的新实例返工、验证失败不进入审阅或记忆、只读角色写入拦截、审阅后文件变化拦截、原工作区不变、重复恢复幂等、中断恢复、存活进程拒绝重复派发、配置变化拒绝恢复、超时清理进程组、MCP stdio 握手、两种客户端 MCP 配置、hooks 拒绝执行、记忆替换与历史、TTL、项目隔离、分层读取和上下文预算。

三个共用 SKILL.md 均通过 skill-creator 的 `quick_validate.py`。Python 模块语法检查通过。

## 首版真实模型联调（历史记录）

本机环境：Codex CLI 0.163.0、Claude Code 2.1.306；复用本机已有登录。

| 流程 | 运行 ID | 结果 |
| --- | --- | --- |
| `gpt-6-astra` → `claude-opus-5-5` → `gpt-6.1-sol` | `run_7f752925d60f4388` | completed |
| `claude-fable-5-1` → `claude-opus-5-5` → `gpt-6.1-sol` | `run_dacde964d01141b9` | completed |

两次均在独立临时 Git 仓库使用预写计划，实现 `greet(name)`，运行真实 unittest，再经独立 Sol 审阅通过。Opus 的供应商结果记录模型为 `claude-opus-5-5`，两次均无权限拒绝。通过后生成本地分支提交、记忆和 handoff。

这验证了实际 CLI 调用、结构化派发、工具写入、测试与审阅闭环。真实联调使用预写计划，未覆盖复杂需求的自动拆解质量；自动计划的 schema 和 DAG 调度有离线行为测试。OpenWiki 外部服务未部署，Orca 桌面编排未接管。

当时使用的命令（当前版本已改为自动生成计划并追加整体验收）：

```bash
python3 -m harness smoke
python3 -m harness smoke --commander fable
```

离线模拟入口 `python3 -m harness demo` 不调用模型，不能代替以上两次真实联调。

## 设计修订后的验证（台账 v2）

本次修订增加结构化审阅、整体目标验收、有界再规划、恢复参数白名单、可选计划批准与单步执行，以及经单独审阅的项目知识提案。首版的真实联调记录不作为这些新增流程的验证结论。

新增离线检查覆盖：整体审阅拒绝与修复、历史 findings、建议与阻断的区别、再规划保留原始义务、累计预算、任务与环境受阻分类、配置恢复、显式计划批准、知识提案来源与操作者保护、批量写入回滚、来源过时排序和旧数据库读取。

`python3 -m unittest discover -s tests -v`：105 项通过（Python 3.15.0，20.470 秒）。还覆盖了再规划中断恢复、计划生成中断后的批准边界、post_accept 失败恢复、暂存区隐藏变更、任务名与整体验收证据隔离，以及供应商 schema 投影与本地严格校验。`python3 -m compileall -q harness tests` 与 `git diff --check` 通过。

同日对修订后的代码执行了两条真实模型 smoke：

| 指挥 | 运行 ID | 结果 |
| --- | --- | --- |
| `gpt-6-astra` | `run_dcc89b3ed5284c64` | completed |
| `claude-fable-5-1` | `run_1fb92660a4d94e18` | completed |

两次均由真实指挥自动生成单任务计划，使用 `claude-opus-5-5` 实现，再由两个不同 dispatch 的 `gpt-6.1-sol` 分别执行任务审阅与整体目标审阅。各执行两轮真实 unittest，最终均 approve，原工作区保持干净。每条链路包含四次模型派发；唯一就绪任务跳过了额外的调度指挥调用。

这验证了当前 CLI 参数、供应商 schema 投影、自动规划和正常验收路径。复杂规划质量、真实模型再规划及故障恢复仍不由这个小函数 smoke 证明；对应的确定性边界通过上述离线行为测试验证。

## 审阅反馈修复后的验证（台账 v3）

2026-10-21 执行 `python3 -m unittest discover -s tests -v`：171 项，170 项通过、1 项跳过，耗时 39.561 秒。跳过项为实际创建非 UTF-8 文件名及符号链接目标的测试，本机文件系统拒绝此类文件名；非 UTF-8 Git 索引、文件内容、原始 diff 与归档恢复测试仍已执行并通过。`python3 -m compileall -q harness tests` 与 `git diff --check` 通过。

新增行为覆盖：格式修复使用新 dispatch 并保留精确模型与累计预算；身份、范围、blocker 和批准语义不能因格式错误绕过；审阅修复保留拒绝结论与证据，跨中断及额度耗尽的恢复也不能重新抽取批准；三个大量输出的失败验证能够在默认上下文预算内进入返工，并保留完整日志引用。

还覆盖增量再规划的不可变义务、已校验修订归档失败后的恢复、pre_accept 失败后重试同一候选及现场改变后的重新审阅、验证超时反馈、独立最终验收预算、reviewer 摘要进入任务记忆、规划时的技能与范围检查、递归 glob、删除与移动的范围门禁、字面 Git 路径提交、快照外置、hooks 提前校验、错误分类和损坏台账的状态查看。

本次没有运行真实模型 smoke。上面的 v2 联调记录属于修复前代码，不能证明 v3 增量再规划、格式修复或新增恢复分支在真实模型上的表现。离线测试使用模拟模型响应和真实本地 Git／子进程，不消耗模型账号额度。

## 阶段恢复修订后的验证（台账 v4）

2026-10-21 修订任务与整体验收阶段检查点，保留审阅原始结果及其完整校验过程，区分验证失败与模型拒绝。新增独立模型派发上限、可追加的精确命令授权、再规划预算恢复、整体上下文预检、无损任务增量，以及可搬迁的共享资产摘要。

最终执行 `python3 -m unittest discover -s tests -v`：224 项，223 项通过、1 项跳过（Python 3.15.0，82.068 秒）。跳过原因仍为本机文件系统拒绝实际创建非 UTF-8 文件名。`python3 -m compileall -q harness tests` 与 `git diff --check` 通过。

离线行为测试覆盖：reviewer CLI 故障、中断与超时只恢复审阅；验证中断保留旧日志并仅重跑检查；候选变化作废旧阶段；越界残留在派发前停止；格式修正故障保留原结论；审阅结果落盘但尚未完成登记和校验时中断仍不能重抽；错误身份、结果缺失或篡改不能跳过门禁；hook 与记忆检查故障复用已返回审阅；最终验证失败的显式重验、再规划意图持久化和预算增加；配置授权追加、资产搬迁、损坏台账/SQLite 诊断与真实 SIGHUP 子进程组清理。预计上下文超限只警告，必要上下文超限提前阻止 worker，并可增加预算恢复。

同日执行两条真实模型 smoke，本机 Codex CLI 0.163.0、Claude Code 2.1.308，保留配置中的精确模型 ID：

| 指挥 | 运行 ID | 结果 |
| --- | --- | --- |
| `gpt-6-astra` | `run_e8a0f406a4624168` | completed |
| `claude-fable-5-1` | `run_eaed58c73eb2419d` | completed |

两次均由真实指挥自动生成计划、`claude-opus-5-5` 实现，两个独立 dispatch 的 `gpt-6.1-sol` 分别完成任务审阅和整体审阅。每次 4 次模型调用、1 次 worker 尝试、1 次最终尝试、2 轮真实 unittest，最终均 approve。运行证据保留在各临时项目的 `.harness/`；本仓库 `.harness/validation/` 保存检查日志，不提交原始模型输出或生成配置。

真实 smoke 验证正常规划、CLI、测试和接受链路。随后调整了最终尝试计数与快照写入的持久化顺序，并增加中断、I/O 故障和损坏快照测试；该调整由最终离线回归覆盖，未重复真实模型调用。故障注入、复杂再规划和预算边界由离线行为测试覆盖，不能据此宣称真实服务发生故障时的所有行为已验证。

## 接受后恢复与停止语义修订（台账 v5）

2026-10-22 最终执行 `python3 -m unittest discover -s tests -v`：277 项，276 项通过、1 项跳过（Python 3.15.0，88.827 秒）。跳过原因仍为本机文件系统拒绝实际创建非 UTF-8 文件名。`python3 -m compileall -q harness tests`、运行时代码的 Python 3.11 语法解析与 `git diff --check` 通过；语法解析不等同于在 Python 3.11 运行完整测试。

新增行为覆盖：派发准备失败不扣 worker／派发额度、失败 dispatch 的退出状态、格式修正独立状态和摘要绑定；契约上限审阅的任务记忆、SQLite 写入故障与事务完成后中断、批准提案的批量回滚、接受事件去重及 step 恢复；指挥 stop 的持久停止、显式重试保存的规划请求、空 diff 修正后的有界再规划与预算恢复；结构非法的拒绝仍不能重新抽取。

还覆盖工作树隐藏 `.harness` 的 worker／整体审阅／hook 门禁、submodule 提前拒绝、恢复 mtime 与隐藏索引标记下的内容快照、验证生成文件的本地排除恢复、配置和 MCP 诊断、状态摘要与完整台账、放弃运行与清理。清理检查独立核对实际文件内容、拒绝隐藏索引标记和忽略文件；测试验证删除后的记账中断可恢复，以及 resume 不会覆盖并发放弃操作。

本次未运行真实模型 smoke。测试使用模拟模型响应、真实 Git 和本地子进程，不能证明当前真实模型调用、复杂规划或供应商故障恢复可用。完整测试日志保存在 `.harness/validation/review-fixes-2026-10-22-final-unittest.log`，不提交运行日志。设计与使用说明已同步，原临时审阅文档已删除。

## 后续设计评审修复（2026-10-22，台账 v5）

执行 `python3 -m unittest discover -s tests -v`：312 项，311 项通过、1 项跳过（Python 3.15.0，103.958 秒）。跳过项仍为本机文件系统无法创建非 UTF-8 文件名。`python3 -m compileall -q harness tests`、运行时代码的 Python 3.11 语法解析与 `git diff --check` 通过；Python 3.11 语法解析不等于在该版本执行完整测试。

新增行为覆盖：接近契约上限且含转义字符的大型审阅经过有界摘录后，在默认上下文预算内完成任务返工、最终修正规划和格式修正；摘录之外的证据改变仍被完整结果门禁拒绝。还覆盖三种任务再规划预算耗尽后的直接恢复、格式错误 stop 的持久停止、不可修复及修正额度耗尽后的零重派，以及终止守卫写入中断后保留原审阅 receipt。

同任务 worker 故障来源与跨任务隔离、post_accept/on_error 独立日志、整体验证失败后空 diff 修正的再规划、未知配置键、无配置的状态和记忆操作均有回归测试。显式退出确认覆盖精确 dispatch、未知 ID 整批拒绝、审计事件、PGID 复用后的恢复和放弃、保留 HEAD 与忽略文件的删除门禁。单任务一次通过流程最多 15 次全量快照，验证 hook 修改仍阻止进入审阅；空白调度指令必须经新 dispatch 修正才能传给 worker。

`python3 -m harness demo` 完成，stdout 可直接解析为单个 JSON；模型调用标记为 false。本次未运行真实模型 smoke，以上离线结果不证明真实账号或供应商调用可用。日志位于 `.harness/review-fixes-final-tests.log`，demo 结果及日志位于 `.harness/review-demo.json` 与 `.harness/review-demo.log`，均不提交。使用说明与设计约束已同步，临时评审文件已删除。

## 恢复与权限边界修订（2026-10-22，台账 v6）

执行 `python3 -m unittest discover -s tests -v`：344 项，343 项通过、1 项跳过，耗时 114.048 秒。跳过项仍为本机文件系统无法创建非 UTF-8 文件名。完整日志位于 `.harness/validation-2026-10-22-final.log`，不提交。运行时代码通过 Python 3.11 语法解析；这不等同于在 Python 3.11 执行完整测试。

新增行为覆盖：格式修正返回失败后结束 pending 状态，连续恢复改为普通尝试并携带最新故障来源；未返回结果的 CLI 故障保留修正；结果原子落盘与清理之间的中断不重放。语义非法且含拒绝或 blocking 的审阅保存为 invalid_rejected，保留原文与摘要，在 hook 通过后进入返工或有界再规划；approve 携带非法 blocking 也不能接受。恢复、空 diff 修正、预算耗尽和 --reverify 均不能对同一整体快照重抽；无拒绝的非法批准仍停止。候选变化保留旧审阅与修正依据，篡改证据仍阻止恢复；旧 v5 台账不能由新恢复语义直接执行。

还覆盖审阅语义约束与本地校验的一致性、工作树写权限的真实路径及模式字符转义、8 个独立进程同时初始化新数据库和迁移旧数据库、只读记忆查询不建库或修改 Git 排除项、已删除暂存新文件与真实删除的接受、全部就绪任务的记忆检索，以及已完成 run 返回原台账而不比较执行配置。

本次未运行真实模型 smoke。权限测试检查生成参数并使用 Git 验证文档规定的路径模式，不证明真实 Claude CLI 已拒绝越界写入；恢复测试使用模拟模型响应和真实 Git、子进程。模型 ID、新进程与 dispatch、原有接受门禁和人工集成边界保持不变。


## Git 绑定、指挥恢复与可选新检出验证（2026-10-22，台账 v7）

最终执行 `python3 -m unittest discover -s tests -v`：402 项，401 项通过、1 项跳过，耗时 304.243 秒（Python 3.15.0）。跳过项仍为本机文件系统无法创建非 UTF-8 文件名。完整日志位于 `.harness/validation/design-implementation-2026-10-22-final.log`，不提交。`python3 -m compileall -q harness tests`、运行时代码的 Python 3.11 语法解析与 `git diff --check` 通过；语法解析不等于在 Python 3.11 运行测试。

新增行为覆盖：工作树 `.git` 改向、符号链接、实际管理目录重名、反向链接和公共目录变化、嵌套符号分支、环境 Git 参数重定向，以及 detached 提交绑定；接受、恢复、移除和记忆来源核验拒绝错误绑定，主分支和主索引保持不变。文件与目录互换可快照、接受和归档恢复，新增暂存的 gitlink 仍被拒绝。

指挥 plan、replan 和任务选择的结果落盘中断、hook 失败、结果篡改、格式修正、停止与显式重试均有覆盖；普通恢复复用原结果，返回 stop 后的 --reverify 不能丢弃该结论，登记 stop 中断不重复计数。格式超限的 task blocker 恢复进入再规划，审阅 receipt 校验故障更新 dispatch 状态。大型 worker 反馈通过有界摘录进入再规划与返工，全文及记忆提案不裁剪，引用缺失或篡改停止。MCP 查询不创建或迁移数据库，测试 SQLite 连接显式关闭。

可选 fresh_checkout 覆盖忽略文件使任务通过但新检出整体验证失败、显式依赖准备成功、准备修改受检文件被拒绝、准备首步失败不执行后续步骤、恢复使用新检出且不重复 worker、审阅恢复复用已有验证、验证 Git 绑定变化及源候选被修改。忽略路径诊断传入审阅；初始化中断或基线产物写入失败后，恢复仍使用台账中的原始基线。

`python3 -m harness demo` 完成，stdout 为单个 JSON，明确记录 `simulation: true`、`models_called: false`。结果和日志位于 `.harness/validation/design-implementation-demo.json` 与 `.harness/validation/design-implementation-demo.log`。本次没有真实模型调用；以上测试使用模拟模型响应、真实 Git 和本地子进程，不证明当前账号、Claude 原生权限判定或真实供应商故障恢复可用。使用说明和设计约束已同步，临时审阅文档 `docs/review-2026-10-22.md` 已删除。

## 受控恢复与维护修订（2026-10-22，台账 v8）

完整执行 `python3 -m unittest discover -s tests -v`，分别使用以下两个解释器：

| Python | 测试结果 | 耗时 | 私有日志 |
| --- | --- | --- | --- |
| 3.15.0 | 447 项，446 通过、1 跳过 | 435.810 秒 | `.harness/validation/design-v8-python-current.log` |
| 3.11.17 | 447 项，446 通过、1 跳过 | 440.123 秒 | `.harness/validation/design-v8-python311.log` |

两次跳过均为本机文件系统拒绝实际创建非 UTF-8 文件名。Python 3.11.17 安装在本仓库 `.harness/python/`，本次已实际运行完整测试，不再仅以语法解析作为该版本的验证。新增 GitHub Actions 配置在 Python 3.11 和 3.x 执行同一离线测试命令；本记录不声称远程 CI 已运行。`python3 -m compileall -q harness tests` 与 `git diff --check` 通过。

新增离线行为覆盖：显式修正非法计划／修订及无拒绝的非法批准元数据，保留原始 receipt、摘要、findings、有效记忆批准和累计预算；合法结果的 hook 故障、停止、拒绝、身份／结构错误均不能通过修正选项丢弃。派发前 hook 改变候选、模型或 hook 改写原始依据、结果落盘后中断、畸形 JSON 的先绑定后解析、修正服务故障及预算耗尽都有恢复检查。已返回的非法修正不增加自动重派，`--reverify` 不能取消已授权且保留原结果的再规划修正。

范围授权测试验证整批校验、审计、原任务范围和验收不变，以及已返回的越界修订获授权后继续校验同一结果。清理测试覆盖预览、运行锁、进程退出、路径与 Git 绑定、修改及忽略文件拒绝、精确注销缺失检出、部分清理、中断恢复和 Git 管理目录名称复用；原运行和验证证据保留。还覆盖缺失审阅产物更新 dispatch 诊断、旧审阅原始摘要的检索去重，以及指令文件新增／修改／删除／重命名进入任务和整体审阅输入。

`python3 -m harness demo` 完成，stdout 可解析为单个 JSON，记录 `simulation: true`、`models_called: false`；结果和日志位于 `.harness/validation/design-v8-demo.json` 与 `.harness/validation/design-v8-demo.log`。

本次另执行一次真实 `python3 -m harness.permission_smoke`，使用 Claude Code 2.1.308 和精确 worker 模型 `claude-opus-5-5`，dispatch 为 `e388fab9-e345-44f9-9678-088bdc6c5e4a`。11 次实际工具调用中，工作树内 Read/Edit/Write、`.git` 读取、精确 Bash 命令及工作树外 Read 共 6 项允许控制成功；追加未授权参数、未列命令、工作树外 Edit/Write 和 `.git` Edit 共 5 项均被拒绝。每项拒绝都有匹配工具 ID 与输入的供应商 `permission_denials` 及错误工具结果，同时核对边界文件字节，未使用模型文字作为通过依据。供应商结果记录 12 轮、成本字段 0.1453248 美元。

真实权限报告和原始证据保留在 `.harness/permission-smoke/7e3e021b1d7d4efda4bc79ecef33fce9/`，不提交。该探针使用专用精确命令并关闭外部 MCP，验证本次 CLI 对这组允许／拒绝操作的处理，不证明任意项目授权或 OS 隔离。本轮未重新执行完整 Astra／Fable 指挥、worker、独立 reviewer 的真实工作流；新增受控恢复和故障分支仍由离线行为测试验证。

台账升级为版本 8，旧执行器不能忽略新增修正守卫；版本 7 及更早运行须由对应旧版本继续，独立清理命令支持版本 7、8。使用说明与设计文档已同步，临时审阅文档 `docs/review-2026-10-22-v7.md` 已删除。

## v8 审阅后的恢复与诊断修订（2026-10-22）

最终执行 `python3 -m unittest discover -s tests -v`：481 项，480 项通过、1 项跳过，耗时 449.788 秒（Python 3.15.0）。跳过原因仍为本机文件系统拒绝实际创建非 UTF-8 文件名。完整日志保存在 `.harness/validation/review-v8-implementation-verified.log`，不提交。

新增检查覆盖自动格式修正耗尽后的显式一次修正、最初与失败修正结果的双重摘要绑定、原审阅结论和指挥决定固定、累计预算、返回结果落盘中断及候选失效归档。旧 v8 指挥修正缺少原始摘要时，普通恢复、显式修正和候选变化均不能重新派发或接受无法核验的决定；已有摘要的 reviewer 守卫可继续按受控条件恢复。

生命周期测试使用真实本地子进程，覆盖 Popen 成功但 pgid 尚未保存时 controller 强制退出，以及 Popen 返回前的 KeyboardInterrupt；未知启动同时阻止恢复与清理。已退出进程的中断恢复夹具也先记录实际 pgid，再中断并清理，检查新 dispatch、阶段复用与预算累计。清理确认覆盖 v7/v8、completed/abandoned、整批 ID 校验、运行锁、先审计后记账、故障保留及文件门禁。

还覆盖全部 hook 的运行定位字段、全新验证目录与源工作树区分、错误与恢复事件、非法显式计划不创建运行现场、全仓库范围提示中断后保留原验收，以及未跟踪／忽略文件诊断不删除任何内容。README 与设计文档同步更新，并增加阻塞状态与操作者出口表。

另使用仓库私有 Python 3.11.17 执行 `test_process_exit_recovery.py`：4 项全部通过，耗时 4.580 秒；日志位于 `.harness/validation/review-v8-process-python311.log`。本轮未在 Python 3.11 重跑完整测试集。运行时代码的 Python 3.11 语法解析、`python3 -m compileall -q harness tests` 和 `git diff --check` 通过。

本次未调用真实模型或执行 smoke，离线模型响应与本地进程测试不证明真实账号或供应商调用可用。临时审阅文档 `docs/review-2026-10-22-v8.md` 已按要求删除。

## v9 审阅后的使用与观测修订（2026-10-22，台账仍为 v8）

本轮提前校验启用的 MCP 配置，在 run 创建前及派发计数前报告服务和字段错误；允许主检出保留未跟踪文件并提示其不进入工作树；记录供应商用量并将进程心跳间隔调为 60 秒。规划说明补充所需测试、夹具与前置工作的归属，详细恢复语义移到 [恢复协议](recovery.md)。任务预算、现有恢复出口和默认 32,000 字符预算保持原有语义，没有新增台账状态或版本。

完整执行 `python3 -m unittest discover -s tests -v`：**499 项，498 通过、1 跳过，452.501 秒**（Python 3.15.0）。跳过项仍为本机文件系统拒绝实际创建非 UTF-8 文件名。新增的 MCP、启动隔离及用量观测三组测试另在 Python 3.11.17 执行，**18 项全部通过，12.862 秒**；本轮未在 Python 3.11 重跑完整测试集。`python3 -m compileall -q harness tests` 与 `git diff --check` 通过。离线日志分别为 `.harness/validation/review-v9-implementation-unittest.log` 与 `.harness/validation/review-v9-python311.log`。

新增行为覆盖：缺失连接字段及非法配置类型在创建 run/分支前被拒绝；pre_agent 后的 MCP 配置错误不消耗任务或派发次数，恢复原配置后可继续；主检出同名未跟踪文件与笔记在完整任务运行后保留，暂存和已跟踪修改仍阻止启动；诊断路径转义并限制显示长度。用量测试以真实本地子进程模拟 CLI 输出，覆盖成功、CLI/协议失败、缺失及非法指标、模型自报费用不作计量、receipt 恢复不重复记录，以及观测故障不改变接受结果。它们不证明真实模型调用可用。

另使用真实 CLI 完成一次多任务联调：Claude Code 2.1.308、codex-cli 0.163.0，run 为 `run_8e93e74bc5b045e4`，最终 `completed`。保留精确模型 ID：

| 模型与角色 | 实际派发数 |
| --- | --- |
| `claude-fable-5-1` 指挥 | 4 |
| `claude-opus-5-5` worker | 5 |
| `gpt-6.1-sol` 独立审阅 | 5 |

临时仓库实现标签规范化、整数金额合计和发票文本组合。通过 `--plan` 提供三个任务，初始有两个同时就绪的任务以触发指挥选择；计划刻意遗漏共享金额校验模块的前置任务，该文件范围已包含在原集成任务的授权中。固定验收测试对尚未实现的模块暂时跳过，整体验收要求全部模块存在且不再跳过。

真实执行先通过 `--step` 接受 labels 后暂停，再由独立 CLI 命令 resume。totals worker 确认前置模块缺失并返回 task blocker，指挥添加 amount_rules 任务和依赖；该前置任务通过验证及独立审阅后，新的 totals worker 继续，随后完成 invoice 集成。原三个任务的 ID、标题、范围、验收和 skills 不变。共 5 次 worker 尝试、1 次有效再规划、1 次最终尝试；报告 blocker 的尝试仍计入预算。四个接受任务和整体目标分别通过五次独立审阅，最终 unittest **25 项全部通过，无跳过**。

核对原主分支 HEAD、固定验收测试字节和未跟踪 `operator-notes.txt` 均未改变；笔记没有进入任务工作树。14 次模型调用的 dispatch ID 与实际进程组 ID 均不同且确认退出，调用命令使用上述模型，不含会话 resume。14 个 dispatch 均有供应商用量记录，与完成事件对应；9 次 Claude 调用累计报告约 **2.557116 美元**，Codex 仅记录 token，不据此推算费用，这不是完整账号账单。实际派发包最大为 **26,836 字符**，未调整默认上下文预算；此样本不证明更大计划也能容纳。

本次覆盖真实任务选择、step/resume、任务 blocker、受限再规划、新增前置任务、跨任务集成、任务与整体审阅，以及供应商用量提取。没有触发任务/整体审阅拒绝、格式修正、invalid_rejected、`--repair-invalid` 或供应商故障恢复；没有以本次 Fable 联调替代 Astra 链路验证。遗漏前置任务是明确设计的联调条件，不用于估计真实任务的失败频率。

夹具、配置、启动/恢复输出、完整核验脚本和汇总报告保存在 `.harness/validation/v9-live/`；原始 dispatch、验证、提交和记忆证据保留在该目录 `fixture.json` 指向的临时项目 `.harness/`。这些私有产物不提交。临时审阅文档 `docs/review-2026-10-22-v9.md` 已按要求删除。


## 第十轮审阅修订验证（台账 v8）

2026-10-22 执行 `python3 -m unittest discover -s tests -v`：514 项，513 项通过、1 项跳过（Python 3.15.0，482.060 秒）。跳过原因仍为本机文件系统拒绝实际创建非 UTF-8 文件名。日志保存在本仓库私有 `.harness/validation/review-v10-offline.log`，不提交。

本轮修订覆盖：文件 stdin 在延迟读取大输入时完整传递 UTF-8 字节；首次拒绝的不可变 finding 格式错误保留为 invalid_rejected，维持原始产物摘要和 post_agent 门禁；Codex 只读调用关闭项目指令自动注入；恢复时拒绝类型、互斥或派发引用不一致的台账；显式增加 max_tasks 后复用已返回的超限修订，并保留 stop 的显式重试要求。worker 返回到阶段持久化之间的重复派发窗口仅补充文档，未新增旧结果消费协议。

Python 3.11.17 上另行通过新增 stdin 测试 3 项、拒绝恢复测试 4 项、状态校验及任务容量恢复测试 7 项，以及既有超时／启动／进程退出测试 7 项。stdin 测试使用超过 256 KiB 的多字节输入、延迟读取和多个短心跳窗口，核对实际字节数与 SHA-256，并检查空输入 EOF、退出码和中断清理；同一延迟读取场景在旧 run_process 的 Python 3.11 实现上复现超时。

Codex 0.163.0 的本地 `debug prompt-input` 检查确认：未提交的 AGENTS.md 标记默认出现在输入中，设置 `project_doc_max_bytes=0` 后消失。该检查只渲染输入，不证明真实 exec 模型调用成功；Claude 的空 setting-sources 参数保持原行为，本轮没有新增真实模型或权限联调。没有运行 `python3 -m harness smoke`。

## 第十一轮审阅修订验证（台账 v8）

2026-10-22 执行 `python3 -m unittest discover -s tests -v`：520 项，519 项通过、1 项跳过（Python 3.15.0，531.499 秒）。跳过原因仍为本机文件系统拒绝实际创建非 UTF-8 文件名。完整日志保存在本仓库私有 `.harness/validation/review-v11-offline.log`，不提交。

本轮新增 6 项行为测试，并调整已有整体审阅用例。自动格式额度已用后，新候选首次拒绝若仅摘要或 finding ID 格式非法，保存为 `invalid_rejected` 并进入任务返工或整体修正规划；测试核对原始产物、内容摘要、派发次数和累计额度，以及 post_agent 失败后复用同一结果、独立重试 hook 的恢复行为。已保存的拒绝型 exhausted 守卫仍阻断同一快照，批准及修正结果继续遵守原有门禁。

恢复加载另检查任务条目、待验证／审阅和待接受任务引用、接受后待办的嵌套 payload，以及非空闲的 current_task。未知任务、缺失字段、错误类型或重复任务 ID 在执行前以 HarnessError 停止；测试确认台账字节、事件、记忆、Git HEAD 和模型派发均不改变。指挥任务选择与再规划的固定身份及额度保持原实现，仅在文档明确各调用类型在整个 run 共用一次自动和一次显式修正额度。

上述审阅恢复测试使用模拟模型响应及真实本地 Git、验证子进程，不证明真实模型调用行为。本轮未运行 `python3 -m harness smoke` 或新增真实模型联调。README、设计与恢复协议已同步，临时审阅文档 `docs/review-2026-10-22-v11.md` 已按要求删除。

## 第十二轮审阅修订验证（2026-10-23，台账 v8）

执行 `python3 -m unittest discover -s tests -v`：524 项，523 项通过、1 项跳过（Python 3.15.0，511.501 秒）。跳过原因仍为本机文件系统拒绝实际创建非 UTF-8 文件名。完整日志保存在本仓库私有 `.harness/validation/review-v12-offline.log`，不提交。

本轮新增 4 项行为测试。`--reverify` 的授权在本次恢复首次进入整体验收时消费：已返回修订产生新快照、续跑未完成验证及超时恢复三种路径中，后续非零失败和空 diff 修正均保留失败证据并请求再规划，不额外消耗最终尝试，也不误记显式重验事件。测试核对最终尝试数、验证派发数、接受提交及 `unchanged_corrections` 触发内容。

恢复未完成的 run 时，`--reverify` 与 `--retry-stopped` 对 final 和任务目标均互斥。测试核对拒绝组合后台账及事件字节不变、原 stop 保留、没有新派发，即使同时提供有效的 `--confirm-exited` 也不会先写入退出确认；随后单独重试停止规划仍能确认退出并重新调用指挥。仅在内存中恢复基线提交的原始 `resume` 与 `final_review` 方法时，四项新增测试均检出旧行为；负向验证日志为 `.harness/validation/review-v12-regression-baseline.log`。

另使用 Python 3.11.17 执行全部 4 项新增测试，均通过，耗时 12.173 秒，日志为 `.harness/validation/review-v12-python311.log`；本轮未在 Python 3.11 重跑完整套件。`python3 -m compileall -q harness tests`、CLI 恢复帮助输出与 `git diff --check` 检查通过。

台账保持版本 8，没有新增恢复子状态。上述测试使用模拟模型响应及真实本地 Git、验证子进程，不证明真实模型调用行为；本轮未运行 `python3 -m harness smoke`。README、设计、恢复协议及 CLI 帮助已同步，临时审阅文档 `docs/review-2026-10-22-v12.md` 已按要求删除。
