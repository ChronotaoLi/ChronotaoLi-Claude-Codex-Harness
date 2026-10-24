# 调度与恢复协议

本文保存详细状态转换、证据要求和操作者出口；总体原则见 [设计取舍](design.md)。

当且仅当一个任务就绪、没有失败反馈且尚未尝试时，运行时可直接发送包含目标、任务、依赖结果、验收和可用命令的完整简报。选择或返工仍交给指挥。指挥是否复用会话与是否支持再规划是两个独立问题，本设计保留每次重新组装上下文。

指挥选择就绪任务时，以目标和所有就绪任务的标题检索记忆，避免只使用列表首项的任务信息。

再规划只处理任务拆分、缺少前置工作和整体验收发现的工程缺口。worker 通过 `blocker_kind` 区分 task、environment 与 operator；operator 等待明确回答，不先再规划；CLI 错误、权限、范围违例和现场不一致不会被包装成自动再规划。非法结果默认停止；明确归属且保留拒绝证据的 invalid_rejected 整体审阅可自动进入修正规划，操作者请求的有界非法结果修正另述。指挥返回 `{action, reason, new_tasks, updates}`：每个 update 仅包含 `id`、`description`、`add_depends_on`，stop 必须提供空操作数组。运行时在原计划上应用增量并重新检查 DAG，要求：

- 保留每个既有任务 ID，已接受任务完全不变。
- 未接受任务只可更新描述并添加依赖；标题、skills、验收和 allowed_paths 均从台账保留，已有依赖不得删除。
- 新任务只使用已有 allowed_paths 中的模式，或操作者显式加入 approved_scope 的模式；不通过推测 glob 包含关系放大范围。
- 原始任务继续承担最终集成义务，拆分不能删除或重命名它来避开失败。
- 每次修订保存版本与原因。校验通过后先持久化待应用修订，再归档未接受修改并恢复隔离工作树，保留已接受提交。归档阶段中断时，恢复继续应用同一修订，不重复调用指挥或增加再规划次数；新的归档尝试保留独立目录。

`resume --allow-scope PATTERN` 可重复指定，仅用于 pending/request 阶段的再规划或已停止的再规划。先校验整批模式，再追加审计事件并保存去重后的 `approved_scope`；非法或显式受保护根路径使整批失败。授权只扩大新任务可选模式，不修改原始任务、既有任务的范围与验收，也不改变预算。已返回的越界修订可在新授权下校验同一 receipt，原结果仍保留；已校验修订不可追加范围。停止状态不因授权解除，必须另用 `--retry-stopped` 才重新请求规划。

每任务尝试数、整个 run 的 worker 尝试数、有效再规划数、最终验收次数和模型派发数均持久保存且受上限约束。分别由 `max_attempts`、`max_total_attempts`、`max_replans`、`max_final_attempts`、`max_agent_dispatches` 控制。再规划修订通过完整校验后才计数；合法 stop 及下述仅原因文本格式错误的 stop 同样计数，请求故障和未知 skill 不计入有效再规划数。所有实际模型调用（含失败、格式修正）消耗独立的派发额度，默认 200 次。最终验收默认 3 次，独立于任务预算。新任务与 resume 都不清零累计预算；额度耗尽保留待再规划意图，操作者增加预算后继续修正规划。

待再规划意图在任务、run、最终和再规划预算守卫之前持久化。保留原有预算上限：最后一次任务尝试返回 task blocker 时，先停在该上限；操作者增加对应预算后直接恢复指挥再规划，不先重派 worker。结构合法、空操作且明确 stop 的结果若仅原因文本格式错误，也计入次数并持久停止，保留原文、格式诊断和返回产物，不能由普通恢复重新抽取决定。

worker 尝试与派发计数在本地上下文、hooks、MCP 配置、快照和额度预检成功后，调用后端之前同一次保存。准备失败不扣额度，调用后端失败计数；派发前失败也将对应条目标为 failed 并确认没有遗留进程。格式修正共用此入口。

经校验的指挥 stop 持久保存原触发条件、原因和证据；普通 resume 不重新派发。显式 `--retry-stopped <task_id|final>` 重新打开该规划请求，不先重跑已受阻 worker，不清零任何预算。恢复未完成的 run 时，该选项与 `--reverify` 互斥；合用在任何台账或事件写入前拒绝，原 stop 和规划请求保持不变。整体拒绝后若接受了未改变快照的修正任务，记录已处理的修正任务集合并保存新的 pending_replan；再次请求规划受原预算约束，原整体拒绝始终保留，不重新审阅同一快照。

整体验证以非零退出失败后的空 diff 修正采用相同处理：保留失败记录，直接请求新的修正规划，不额外消耗最终尝试来重跑相同代码。显式 `--reverify` 仍可用于操作者确认环境已修复的情况。

恢复配置允许增加上下文预算、agent/验证超时、Claude 轮数、次数上限及任务容量 `max_tasks`，不可减少。超限修订提高容量后重验同一 receipt，不重抽结果；已保存 stop 仍须显式 `--retry-stopped` 或匹配的操作者决定。可以追加精确 `worker_commands` argv，已有授权不可删除或修改；记录配置放宽事件。模型 ID、供应商、effort、验证命令和共享资产不允许在同一 run 改变。进程退出及 Git 绑定、HEAD 检查仍先执行。新运行版本为 11；未完成版本 8、9、10 通过恢复预检后升级，历史及预算保留，共享资产摘要仍必须匹配。版本 7 及更早不能直接恢复执行。已完成 run 普通恢复直接返回原结果，不升级、不比较执行配置及共享资产。

`resume RUN --environment-update FILE` 接受最多 64 KiB 的严格 JSON，每个 run 最多 32 条更新：必填 `schema_version: 1`、`task_id`、`blocker_dispatch_id` 和非空、至多 4,000 字符的 `summary`；可选 `checks` 默认 `[]`，最多 16 项。未知字段拒绝。检查只允许 `{kind: "path", path: 绝对路径, path_type: "file"|"directory"|"any"}`、`{kind: "executable", name: 可执行文件名或绝对路径}`、`{kind: "worker_command", argv: 精确命令数组}`。命令检查以是否已在当前 worker_commands 或 verification.commands 中声明判定通过；未声明时仍保留更新及失败检查报告，追加精确配置授权后普通 resume 可重检。检查不执行命令、不启动子进程、不新增授权，也不保证真实 CLI、账号或服务成功。只需表达操作者事实时可省略 checks。

接受更新前在 run 锁内核对进程退出、Git 绑定、HEAD、当前任务与范围，以及保存的环境 blocker 来源身份和原结果摘要。只接受当前未接受任务的 worker 环境阻断边界；首次后续模型调用前中断，即使状态为 interrupted 或遗留 running，仍按这些证据判断资格。后续模型调用使旧来源失效，包括已开始但失败的指挥调用；需在新的 worker 环境阻断后再提交更新。存在待处理任务／最终验证或审阅、接受及接受后待办、再规划／指挥 stop、pending_directive、commander/reviewer receipt、待格式修正或活动非法结果修正时拒绝。完成或放弃状态拒绝更新；与 reverify、retry-stopped、allow-scope、repair-invalid 互斥，step 可合用。不得通过更新清除任何 receipt、修正守卫、已保存拒绝或批准快照。

更新输入复制到私有产物并绑定摘要，原输入文件不再是恢复依赖。所有历史更新、来源及检查报告在投影前核验；缺失或篡改时停止。有效来源的重复相同更新幂等，审计事件可因中断重复但台账只有一条引用。上下文只包含相关任务的最新摘要及紧凑检查结果：worker／任务 reviewer 看当前任务，dispatch 看 ready 任务，replan 看触发任务，final reviewer 看各任务。完整历史、checks、argv 和报告保留路径与 SHA256 引用；依赖省略的细节前须读取并核验产物，失败检查先查看详情。原目标、验收、范围、实际权限及未解决 blocker 始终保留，任务环境断言不得推广到其他任务。整体容量预估采用相同投影。

更新登记后历史不可撤回；最新同任务摘要及 checks 可替代旧环境假设及检查列表，不覆盖验收或权限。旧声明及报告以可检索引用保留，不重复内联全部文字或命令。历史输入契约不收紧；合法输入的紧凑投影仍可能超出上下文预算，届时增加 `context_chars` 后恢复。

同一任务最新更新的 checks 替代先前检查列表，所有历史声明仍按序发送。选择前只预检已有的 current_task，不检查全部 ready 任务；选中任务在后续每次 worker 尝试（含格式修正）前仍重检，所有报告保留。已有当前任务的一次普通尝试可能在选择前与派发前分别记录报告，输入使用最近一份。检查失败不启动本次 worker，不消耗其尝试或派发预算。current_task 为空时，指挥选择可能已消耗派发预算；其选定指令保存到 pending_directive，后续恢复复用，修复条件后启动 worker 才消费该指令。worker 已返回并进入验证／审阅后不重跑这些前置检查；正常验证、独立审阅、快照和接受门禁仍全部执行，检查成功或操作者声明本身不是完成证据。

状态摘要的 worker_outcomes 仅分类 worker 自报完成、任务／环境／操作者阻断、运行故障与未知结果，不替代 accepted 或 completed 门禁。last_environment_blocker 是最近环境阻断的历史诊断；后续模型调用开始后标为 stale。stale=false 也不代表具备环境更新资格：更新仍需通过进程退出、来源摘要、任务范围和待处理阶段等检查。

对长时间中断后 pgid 被复用的情况，采用显式确认而非根据组长身份自动推断整个进程组已退出。`resume` 与 `abandon` 的 `--confirm-exited <dispatch_id>` 在运行锁内校验全部 ID，先追加操作事件，再只更新指定记录的退出确认。事件记录当时的组存活探测及组长 PID、PPID、PGID、启动时间和命令名（或探测错误）；组长缺失不构成自动退出证明，不发送终止信号。确认不放宽其他恢复或工作树移除检查。

运行与验证清理共用进程退出判定。派发在准备阶段保存 `process_state=prepared`，真正调用后端前与预算一起保存 `starting`，启动回调记录 pgid 后转为 `running`；受控结束记录退出。只有明确的准备状态可以证明尚未尝试启动，`starting` 或旧台账中无 pgid 且未确认退出的条目均停止恢复与清理，覆盖子进程已启动但 pgid 尚未落盘的中断窗口。`cleanup-verification --confirm-exited ID` 提供无需运行配置的同一审计确认入口，兼容 v7/v8/v9/v10/v11，也支持 completed/abandoned，不恢复执行或改写终态。

worker 完成且结果通过检查后，持久化 `pending_task` 的 verifying 阶段及完整快照摘要；验证成功后持久化 reviewing 阶段、diff、验证产物路径、记忆提案和历轮审阅。恢复时先检查范围、HEAD 和快照；一致则继续未完成阶段，不再派发 worker 或消耗 worker 额度。验证重跑使用新的日志目录。候选变化使后续证据失效，回到新的工作尝试；越界残留先停止并提示人工核对清理，不自动删文件，不派发模型或消耗 worker 额度。

已知窗口：worker 结果落盘、dispatch 返回之后，到 `pending_task` 或 blocker 反馈持久化之前，还会进行结果核对、忽略文件记录及候选快照。如果这时中断，普通恢复会重新调度并启动新的 worker，保留现场编辑但再次消耗尝试和派发预算。此窗口不属于 `dispatch_once` 的异常处理，可能没有 `worker_recovery` 来源；已返回的普通 worker 结果不按 reviewer／commander receipt 复用。当前不提供直接消费该旧结果的恢复协议。

候选变化时，先将已返回的审阅及格式修正原始依据保存到历史，保留 dispatch、原快照、产物路径、摘要和校验错误，再清除待审阅状态。结构与身份可确认的 findings 传给后续实例复核，其余产物只作为带诊断的原始证据引用。已绑定产物缺失或篡改仍阻止恢复；失效批准不构成新候选的接受依据。

审阅派发在调用模型之前保存 `review_receipts` 接收意图，绑定原 dispatch ID、候选快照和结果文件位置。返回结果原子落盘后即使还没完成检查就中断，恢复也读取该结果，完成原契约身份、结构、语义、格式、文件一致性和 post_agent hook 检查。结果绑定哈希后，文件缺失或改变必须停止；hook 重试使用独立日志并具有至少一次执行语义。没有返回结果的调用可在人工 resume 后重新派发，仍消耗派发预算。协议错误不能通过重抽结果消失。

首次审阅的身份、结构和快照正确，但 finding ID 重复、blocking 缺少证据／属于偏好或记忆批准越界时，若 verdict 为 request_changes 或包含任意 blocking finding，则保留为 `invalid_rejected`。首次拒绝若语义合法、但 finding 数量、位置、说明或证据等不可变字段违反格式限制，也使用该路径。仅可变字段格式错误时，该角色／调用类型／任务的自动额度未用则走现有一次自动格式修正；额度已用的首次拒绝直接保存为 `invalid_rejected`，不追加格式修正派发。该状态保存完整原文、产物摘要和校验诊断，不修造一份合法结果，也不批准记忆；post_agent hook 仍须通过，hook 中断后恢复继续核验同一原始结果。任务级保存历史后返工，整体级保存完成的失败记录与 pending_replan，再按原预算请求修正。相同整体快照包括空 diff 修正均不可重抽审阅；不含拒绝的非法批准默认停止，仅下述显式元数据修正可继续。身份／结构错误及自动或显式修正中的非法结果继续停止。无效语义结果可能同时超过格式大小限制；历史通过原始产物引用和有界摘录传递，必要元数据仍超预算时明确停止。

整体验收在 `final_history` 中持久化 verifying/reviewing 阶段及证据。中断且快照相同则复用同一次最终尝试；未完成记录不作为先前审阅结论注入。验证失败使用独立的 verification_failed 状态：所有失败项均超时时停止，人工修复环境或增加超时后 resume 可在同一快照上重验；普通非零退出进入修正规划，同一快照重验须显式 `--reverify`。该授权在本次恢复首次进入整体验收时消费，首次处理新快照、未完成阶段或超时重验也不保留授权；同次恢复后续产生的失败及空 diff 修正仍按普通恢复规则处理。重验消耗新的最终尝试且保留原失败记录。该选项仅在整体验收预算允许重验时取消尚未返回指挥结果的验证失败再规划请求；预算耗尽时，被拒绝的 `--reverify` 不取消待再规划意图，也不记录取消或显式重验事件，仍记录本次停止。已返回结果必须完成原校验，已校验修订必须继续应用，模型拒绝仍要求正常修正任务。最终拒绝结果与待再规划意图一起保存，避免中断丢失修正路径。

旧版本若已在预算拒绝前取消待再规划意图，本修订不会自动重建该请求。保留原失败证据，增加整体验收预算后可显式 `--reverify`；预算仍不足时不记录显式重验事件。

普通运行错误记录类型、证据并进入 blocked；角色调用故障单独保存为 `last_runtime_error`，不污染其他角色的任务反馈。用户中断、SIGTERM 和 SIGHUP 进入 interrupted，并清理所拥有的进程组。验证超时记录为 `timed_out`、退出码 124，任务级进入正常返工反馈，最终级按上述恢复策略处理。缺少 CLI、损坏供应商 JSON 等明确阻断，不作通用网络重试，也不切换模型。默认配置属于 Harness 自身，`--repo` 不自动选择项目配置；真实项目应显式指定 `--config`，启动时显示所用配置和验证 argv。

worker 调用失败或中断时，另按 task ID 保存 `worker_recovery`，记录故障类型、dispatch ID、证据目录和变更文件；同任务的调度、后续 worker 与 reviewer 通过 `recovery` 收到其来源。格式失败也可能伴随实际编辑，因此格式修正成功不提前清除这项来源。保留可用的范围内进度，不自动复位；越界残留仍先阻止派发。其他任务没有此项反馈，接受或应用再规划后清除该来源。

配置采用封闭键集合，拒绝未知顶层节和各节、角色字段，错误给出完整键路径。`status`、`memory` 与 `cleanup-verification` 不加载运行配置；源码外安装执行任务需显式提供配置和共享资源。运行事件、预检提示与 demo 横幅写 stderr，结果 JSON 保持 stdout 可独立解析。

计划确认和单步执行为显式选项：默认连续执行；选择暂停确认后必须显式批准。任何无回复或重复恢复都不能代替批准。step 以一个已接受任务为边界；最后一个任务之后还需要整体检查。

指挥 plan、replan 和 dispatch 同样采用派发前 receipt：保存原 schema、输入包及摘要、dispatch、候选摘要、返回位置；结果原子落盘后继续原身份、结构、任务语义和格式校验。replan 另绑定持久请求 ID，合法新修订不能误用前次结果。结果摘要确定后缺失或篡改停止。post_agent 使用独立日志，保留至少一次语义；receipt 与计划、停止或已校验修订在同一次台账保存中消费。模型生成的任务选择先保存为 pending_directive，worker 本地准备失败保留它，调用后端前与尝试预算一起消费。已返回的指挥结果不再由普通恢复重新抽取；未返回结果的故障仍可显式恢复，格式修正保持原有一次额度。

`resume --repair-invalid DISPATCH_ID` 提供独立、显式且有界的输出修正入口。仅匹配当前未消费的 receipt，先验证原字节摘要、派发身份、结构、当前快照，以及指挥请求摘要和 replan 请求归属。语义修正适用对象为非法 plan/revise，或 verdict 为 approve、没有 blocking finding，但重复 finding ID／批准未知记忆 key 的审阅。自动格式修正额度耗尽后，仅可变字段格式非法的指挥任务选择、revise 和无阻断 approve 也可使用同一显式额度；不得在自动修正尚未耗尽时取得额外派发。

格式耗尽的显式修正保留最初结果和最新失败修正结果的路径、派发、快照与内容摘要。review 以最初 review_corrections 守卫为约束，不能用后一次返回的内容替代最初结论；指挥的自动和显式格式修正固定最初 selected_task 或 action/new_tasks/updates，仅修正 instruction 或 reason。旧 v8 指挥格式修正没有最初结果的已绑定摘要时，普通恢复与显式修正均停止：既不派发未返回的修正，也不接受已有修正结果，不从现有文件补造可信依据；操作者可保留证据后 abandon。旧 reviewer 守卫已有原始摘要，可按同一条件恢复。合法结果及其 hook 失败、stop、拒绝或 blocking 审阅、身份／结构错误、改变原结论的 failed 守卫均不适用；非阻断批准的 finding 数量、位置、内容或证据等不可变字段超限仍属于 unrepairable，不派发无解修正。符合前述条件的首次拒绝按 invalid_rejected 路径处理；旧运行中已保存的拒绝型 exhausted 或 unrepairable 守卫不自动转换，同一快照仍停止。任务候选真实变化时仅按证据归档与完整重做路径恢复。

授权与原 receipt 一起持久保存在 `invalid_repairs`，保留原结果文件、内容摘要、快照、诊断和指挥请求。每个角色／调用类型／任务整个 run 只可授权一次；新派发使用新进程、新 dispatch，并消耗累计派发预算。指挥修正仍受原目标、范围、DAG、验收与预算约束。审阅修正必须保留原 verdict、全部 finding 的数量、顺序、严重程度、类别、位置、说明和证据，以及原先获批且确属提案的 key；只允许修改摘要、重复或非法 ID，并去重批准 key、删除不存在的 key。原本合法且唯一的 finding ID 不变，不允许新增批准。

指挥任务选择的固定任务身份为 `dispatching`，再规划为 `replanning`，对应 `commander:dispatch:dispatching` 和 `commander:replan:replanning`。两种调用类型各自在整个 run 内共享一次自动格式修正额度和一次显式修正额度；选择不同工程任务、增加 worker 尝试、进入新一轮再规划或 resume 均不重置。已保存的 receipt 和修正依据仍按原身份恢复。

语义修正与格式耗尽修正共用这一次显式额度；格式修正不允许改变已批准 key 集合。显式修正再次格式非法也不取得自动修正额度，普通恢复继续校验同一个已返回结果。

原始依据在准备、派发和接收修正时继续核验。已返回的修正结果由新 receipt 完成同一校验与 hook，不因再次非法、格式错误或中断自动增加派发；未返回结果的 CLI 故障可普通 resume 继续，保留授权并计入预算。完成、失败或待处理的修正记录均不清零，任务候选变化仍走原审阅证据归档路径。`--repair-invalid` 与 `--reverify`、`--retry-stopped`、`--allow-scope` 互斥，不能组合成丢弃结果或扩大权限的捷径。

结构与语义正确但格式超限的 blocked/task worker 结果先保存完整证据和 pending_replan，再停止；resume 直接请求有界再规划。环境 blocker 仍停止，不将权限或服务故障变成工程再规划。恢复中校验 receipt 的错误同步更新 dispatch 及 last_runtime_error，避免仅 run 状态反映故障。

### 恢复子状态的维护约束

加载台账时先检查下列可选对象的类型、任务阶段与再规划互斥，以及 receipt、修正守卫和 worker 故障来源引用的 dispatch 是否存在并匹配角色／调用类型／任务；重复 dispatch ID 同样停止。`pending_task.task_id`、`pending_acceptance.task_id`、`pending_post_accept.payload.task_id` 和非 `None` 的 `current_task` 必须是指向现有任务的字符串；检查引用前先核对任务条目、嵌套 payload 和 ID 的类型。检查失败给出 `HarnessError`，不重写台账、不派发、不写入记忆，也不先归档候选。此检查不替代消费证据时的内容摘要、候选快照、身份和 hook 门禁，不自动修复损坏状态；版本 8、9、10、11 均执行这些检查；环境更新、检查点及决定另校验各自归属和证据引用。

恢复链接校验失败同样阻止 `abandon`，应保留台账与证据供核对。`status` 和 `cleanup-verification` 不经过此链接检查；后者仍须通过独立的台账、进程退出和 Git 绑定检查，不保证损坏台账可以清理。

| 子状态 | 设置点 | 清除点与共存约束 |
| --- | --- | --- |
| `pending_task` | worker 结果检查完成 | 验证失败、候选变化或任务审阅结束时清除；与 `pending_replan` 互斥 |
| `pending_acceptance` | 任务审阅通过 | 候选变化或接受台账保存时清除 |
| `pending_post_accept` | 接受提交及台账保存 | 记忆、事件和 post_accept 均完成后清除 |
| `pending_replan` | task blocker、整体失败或显式重试 stop | 修订登记、stop 或允许的 reverify 取消时清除 |
| `pending_directive` | 已校验的指挥任务选择 | worker 调用前与预算一起消费；本地准备失败保留 |
| `review_receipts` / `commander_receipts` | 调用前保存接收意图 | 结果被阶段状态消费、转入修正或允许的候选失效时清除 |
| `pending_format_corrections` | 格式错误后准备修正 | 成功、额度耗尽或候选失效时清除；worker 返回即清除。尚未启动时允许没有新 dispatch ID |
| `review_corrections` | 首次需要格式修正的审阅 | 审阅被消费或候选失效时清除；可与 receipt、待修正状态并存 |
| `invalid_repairs` | 操作者授权显式修正 | 完成、失败或失效后仍保留来源与额度记录 |
| `format_repairs` | 修正调用前 | 累计额度不因新候选或 resume 清零 |
| `worker_recovery` | worker 调用内失败或中断 | 任务接受或应用再规划后清除；可与待验证、待审阅阶段共存 |
| `replan_stopped` | 校验并保存指挥 stop | 显式 retry-stopped 或匹配该来源的 operator-decision continue 转回 pending_replan 时清除 |
| `environment_updates` | 操作者环境更新审计并登记 | 版本 9 起的有序持久记录；不清除旧声明，最新同任务 checks 替代旧检查列表 |
| `environment_checks` | 每次前置检查保存报告后 | 追加保存全部报告引用；上下文携带相关任务最新更新的最近检查结果及完整报告引用 |
| `task_checkpoints` | 合法 worker 结果返回后，证据复制与摘要绑定完成 | 保留历次引用；同任务同检查 ID 采用最新记录，输入变化或过期标为 stale，不改变任务接受状态 |
| `decision_requests` | 当前 operator 阻断、指挥 stop，或来源任务接受且 post_accept 完成 | 请求与原始结果绑定并保留；后续任务请求可从 accepted 中恢复登记，未答复或 stop 时禁止目标任务派发 |
| `operator_decisions` | 严格校验显式答复后，审计先于台账 | 仅解除匹配请求的等待；后续调用开始后拒绝重交旧答复，不清除已有结果或累计预算 |
| `accepted[].worker_source / decision_requests` | 任务接受台账保存时 | 保存接受的精确 worker 来源及后续决策请求，不能用其他尝试的结果替代 |
| `dispatches[].invoked` | 本地准备时为 false，后端调用前同预算保存为 true | 后续真实调用使旧环境 blocker 来源过期；旧记录缺该字段时保守按已调用处理，不替代进程退出检查 |

新增子状态或改变保存顺序时，应同步更新此表并验证中断后的合法组合；不能将上述状态一概视为互斥。

### 阻塞状态与操作者出口

以下命令均沿用同一 run 的预算、证据和接受条件。出口可以是继续执行，也可以是保留现场后放弃；不要求每个非法结果都存在可继续的修正方式。表中 `abandon` 仍要求台账恢复链接有效、进程退出已确认。

| 状态或原因 | 操作者出口 | 仍须满足的条件 |
| --- | --- | --- |
| 提供的计划非法 | 修正 `--plan` 文件后重新 `run` | 此时尚未创建 run 现场 |
| 等待计划批准 | `resume --approve-plan` | 不以普通 resume 代替批准 |
| 次数、上下文或超时预算不足 | 按允许范围增加配置后 `resume` | 累计计数不清零，已有结果不重抽 |
| 旧进程存活或退出未知 | 核实后 `resume --confirm-exited ID`；终态使用 `cleanup-verification RUN --confirm-exited ID` | 确认精确 dispatch，先审计后写入，不终止无关进程 |
| worker 或未返回结果的模型调用故障 | 修复环境后 `resume` | 新进程、新 dispatch，保留故障来源并消耗对应预算 |
| worker 已报告环境阻断，需要更新环境事实 | `resume --environment-update FILE`；已保存检查仍失败时修复条件后普通 `resume` | 来源有效且无待处理阶段；检查不执行命令，更新不扩大权限或降低验收，失败检查不消耗本次 worker 尝试或派发预算 |
| 验证、审阅或接受 hook 中断 | 修复原因后 `resume` | 同一快照复用已有阶段；hook 不被绕过 |
| 自动格式修正耗尽，且满足显式修正条件 | `resume --repair-invalid ID` | 最初结论、已绑定原文、失败修正证据与快照不变；整个角色／类型／任务仅一次，指挥任务选择和再规划各按固定身份在整个 run 共用 |
| 首次拒绝仅可变字段格式非法，且自动额度已用 | 保存为 `invalid_rejected` 后进入任务返工或整体修正规划；中断后普通 `resume` | 原文、摘要、快照及 hook 仍须核验；不追加格式修正派发，不适用于修正结果或旧拒绝型 exhausted 守卫 |
| 支持的语义非法 plan/revise 或批准元数据 | `resume --repair-invalid ID` | 原有语义修正约束及同一显式额度 |
| 修正改变原结论、批准的不可变字段超限、已有 unrepairable 或拒绝型 exhausted 守卫、显式修正再次非法，或缺可信原始依据 | 保留证据后 `abandon` | 不重新抽取同一候选的结论；任务候选确有变化时仅适用既有归档与完整重做路径 |
| 指挥明确 stop | `resume --retry-stopped TASK_ID` 或 `final` | 保留原 stop，仍受累计再规划和派发预算约束 |
| 新任务需要额外文件范围 | 待再规划时 `resume --allow-scope PATTERN` | 只增加新任务可选模式；已停止时另需 `--retry-stopped` |
| 最终验证超时／非零退出 | 超时修复后 `resume`；非零退出走修正规划，确认环境修复后可显式 `--reverify` | 已返回的指挥结果和模型拒绝不可由重验丢弃；预算耗尽时 `--reverify` 不取消待再规划意图 |
| HEAD、Git 绑定、原始证据或候选不一致 | 核对并恢复可信现场，或 `abandon` 保留证据 | 不手工伪造接受依据；任务候选变化须重新执行验证与审阅 |
| 移除工作树被未保存文件阻止 | 核对诊断路径、归档或清理后重试移除 | 忽略文件也不得未经核对删除 |

## 可复用检查点与操作者决定（版本 10）

WORK 可选 `checkpoints`，最多 16 项，每项为 `{id, summary, inputs: [{path, sha256}], evidence: [{path, sha256}], freshness, max_age_seconds}`。ID 在本次结果中唯一；summary 非空、最多 4,000 字符；inputs/evidence 各最多 8 个文件，evidence 至少一个。文件必须为允许的普通文件，SHA256 为实际字节摘要；运行时将原始 evidence 私有保存。`freshness="content"` 必须绑定至少一个 input 且 `max_age_seconds=0`；`volatile` 的有效期为 0–86,400 秒，0 表示每次都需重新检查。有效期保守地从来源 worker 的调用开始时间计算，worker 内部执行时间也会消耗有效期；缺少有效调用时间的时效检查点一律视为 stale，不从结果落盘时间重新计时。输入及证据文件拒绝符号链接；使用解析后的实际路径（例如 macOS 的 /private/tmp）。单份证据最多 1 MiB，一次结果的副本总计最多 16 MiB。投影根据输入字节、证据摘要及有效期标记 reusable/stale，并保留历史引用。reusable 仅证明声明的依赖未变且未过期，不证明依赖完整、推理正确、命令成功或任务已接受；时效事实、未声明依赖和未解决 blocker 仍需核实。检查点不能绕过验证、独立审阅或接受门禁。

允许的检查点源为不含 `.git`、`.harness`、`.codex`、`.claude` 保留路径段或 `..` 的普通文件；工作树内路径按相对工作树的部分检查。仅 evidence 额外允许当前 worker dispatch 的 `commands/` 中的普通文件，可直接引用 broker receipt 的绝对 stdout/stderr 路径及 SHA256；其他 dispatch、run、私有文件及 inputs 均不获得该例外。符号链接和文件大小门禁仍生效。

格式修正也是新 dispatch，不能重新申报原 dispatch 的 broker 输出作为检查点证据。可以省略可选检查点，或仅重跑已授权且可安全重复的检查；不要为重新取得证据重复有副作用的操作。其他允许的普通文件证据仍可使用。原命令日志保留；completed 修正结果的检查点声明错误只记录丢弃事件，blocked 结果另携带有界反馈。不继承旧 broker 证据，也不刷新旧 volatile 证据的时间起点。

必要上下文只展开相关任务的最新检查点，`checkpoint_sources` 按 dispatch ID 共享来源。历史不重复最新行，超过 4,000 字符时省略内联历史并报告数量；已接受任务也不在整体审阅中展开。`checkpoint_index` 指向 SHA256 绑定的不可变索引，包含全部 manifest 路径和摘要，可检索被省略的历史及已接受任务的证据。完整声明、记录时间和输入／证据细节从 manifest 读取。投影前仍核验全部保存证据，索引损坏不会被覆盖。默认预算测试覆盖三次尝试各 16 个同 ID 检查点，以及四任务各四项的最终审阅；不同 ID 累积、较长路径或其他必要内容仍可能超限，此时增加 `context_chars` 后恢复，不能靠删除证据或降低接受门禁继续。

当前任务需要用户决定时，worker 返回 `status="blocked", blocker_kind="operator"` 和具体问题；运行停止等待回答，不先再规划。已满足本任务验收的 worker 可以在 completed 结果中附带 `decision_requests: [{id, task_id, question}]`，将明确风险选择留给传递依赖于本任务的后续任务。本任务仍正常验证、审阅、接受；该请求不表示后续操作被批准，目标任务在得到回答前停止。禁止用后续阶段的操作／测试证明作为前一分析任务的完成条件；负面分析结论可以完成，但必须满足该分析任务自己的验收。

worker 通过身份、结构与语义门禁后，本次检查点声明的哈希、路径、源文件或大小校验失败会丢弃该批检查点并记录 `checkpoints_discarded`，不丢失原有 operator 阻断，也不跳过验证和审阅。blocked feedback 还携带任务、dispatch 及前 2,000 字符原因，供后续指挥与 worker 读取；完整原因保留在事件中，completed 结果仅记录事件。原始结果绑定、已有台账、私有副本完整性和私有存储错误仍停止。新返回的 completed handoff 目标或问题不合法时，在接受前拒绝本次尝试，拒绝原因和 dispatch 来源经 `worker_recovery` 交给后续指挥与 worker，不静默过滤请求。

operator 问题空白、含 NUL 或超过登记长度时，运行时保留原始结果并登记有界说明及转义摘录。操作者须读取 request 的 `source` 所绑定的原始问题，再用正常 `--operator-decision` 回答；普通 resume 只保留等待，不消耗新派发预算。此处理也恢复旧版本因问题文本而登记失败的 operator 请求和已接受 handoff。旧版本已经保存的 `pending_task`／`pending_acceptance` 继续其验证、审阅及接受阶段，随后登记请求，不能提前派发依赖任务；原始产物、接受记录、预算及合法请求身份保持不变。指挥 stop 原因含 NUL 时不登记问题，普通恢复仍提示原有 `--retry-stopped` 出口。

从 `status RUN` 取得运行时 request ID 后提交：

```bash
python3 -m harness --config /path/to/harness.toml --repo /path/to/project resume RUN --operator-decision /absolute/path/decision.json --step
```

```json
{"schema_version": 1, "request_id": "request_id_from_status", "task_id": "target_task", "decision": "continue", "summary": "操作者对该问题的明确选择及理由"}
```

输入是最多 64 KiB 的严格 JSON，只接受上述字段，summary 非空且不超过 4,000 字符；`decision` 为 `continue` 或 `stop`。回答绑定任务、问题和原始来源，私有保存并绑定摘要。continue 允许针对该问题使用全新 worker 尝试或继续原再规划；指挥 stop 的 continue 重新打开原规划请求并保留旧结论和证据。stop 保持等待。handoff 的答复可在目标任务的后续模型调用前显式修订，无关任务的执行不关闭窗口；worker 和指挥 stop 的答复仍只能在后续模型调用前修订，来源过期后拒绝旧回答。回答不改变目标、scope、命令授权、验收或累计预算，也不产生完成证据。仍可用原 `--retry-stopped` 在不附说明时显式重试指挥 stop，但不能覆盖已记录的操作者 stop。

`--operator-decision` 与 `--environment-update`、`--retry-stopped`、`--reverify`、`--repair-invalid`、`--allow-scope` 互斥，可与 `--confirm-exited`、`--step` 合用。操作者已回答 stop 时，必须向同一请求提交新的 continue；单独 `--retry-stopped` 不能覆盖该决定。完成或放弃的 run 拒绝回答。操作者决定是任务范围内的选择，不是其他任务的环境事实、记忆指令或权限授予。

工作上下文按任务筛选决定，整体审阅和再规划包含全部请求的紧凑投影。问题与答复摘录共享 4,000 字符额度，每段最多 500 字符；ID、来源和最新 continue/stop 保留，未答复为 response=null。`operator_decision_index` 的路径与 SHA256 绑定完整请求、当前答复及全部历史答复，含原产物路径和摘要；依赖截断信息前须读取全文。被省略的来源仍须通过完整性校验，原文和台账不被截断或替换。

已有操作者答复的恢复在继续工作前重做整体容量预检。可修订且随条数缩减的决定投影仅计入预计容量，超限给出增大 `context_chars` 的预警；确定的必要下界超限才提前停止。最终审阅和预检只携带检查点索引，避免把当前未接受任务的临时展开内容当成最终下界。实际派发仍核对完整输入；若最终审阅因此超限，增大预算后普通 resume 继续同一次最终尝试，复用已完成验证，不删除决定或重跑已接受任务。


## 命令子进程证据（版本 11）

worker 独有的 `harness_commands` broker 在 dispatch 私有 `commands/` 中保存每次精确 argv 命令的 stdout/stderr 原文、摘要 receipt 和子进程 journal。恢复、abandon 与验证检出清理都核对这些子进程的退出证据；父 dispatch 已退出或缺少 pgid 都不能证明命令未启动或已经退出。证据缺失、损坏或退出未知时保留现场并停止。

每条 broker 命令的超时取 `runtime.verify_timeout_seconds`，可在恢复时调大；适配器配置的 MCP 超时另加 10 秒，整个 worker 仍受 `agent_timeout_seconds` 限制。

操作者核实后仍以父 dispatch ID 提交 `--confirm-exited`，审计记录绑定命令子日志摘要，再保存该精确 dispatch 的确认；不终止无关进程，也不跳过 HEAD、快照、Git 绑定或文件清理检查。新运行写版本 11；未完成版本 8、9、10 经预检后升级且保留历史与预算，版本 7 仍不能恢复执行。`cleanup-verification` 可检查版本 7 至 11，包括终态 run。
