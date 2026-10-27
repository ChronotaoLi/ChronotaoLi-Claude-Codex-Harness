# 调度与恢复协议

本文保存详细状态转换、证据要求、兼容规则和操作者出口；日常命令见 [README](../README.md)，总体原则见 [设计取舍](design.md)。

- [环境更新](#环境更新)
- [进程退出与阶段恢复](#进程退出与阶段恢复)
- [指挥结果与受控修正](#指挥结果与受控修正)
- [终态与版本兼容](#终态与版本兼容)
- [阻塞状态与操作者出口](#阻塞状态与操作者出口)
- [可复用检查点与操作者决定](#可复用检查点与操作者决定版本-10)
- [计划范围与命令前置条件](#计划范围与命令前置条件)
- [Git 现场与验证检出清理](#git-现场与验证检出清理)
- [Hooks 与共享资产兼容](#hooks-与共享资产兼容)
- [记忆作用域与来源兼容](#记忆作用域与来源兼容)
- [上下文与诊断产物](#上下文与诊断产物)
- [独立真实工具检查](#独立真实工具检查)

Claude 使用 `--no-session-persistence`，Codex 使用 `--ephemeral`；没有 resume/continue，也不启用原生子 agent。每次任务尝试与审阅都使用新进程和新 dispatch，返回的 `dispatch_id` 与 `task_id` 必须匹配。

当且仅当一个任务就绪、没有失败反馈且尚未尝试时，运行时可直接发送包含目标、任务、依赖结果、验收和可用命令的完整简报。选择或返工仍交给指挥。指挥是否复用会话与是否支持再规划是两个独立问题，本设计保留每次重新组装上下文。

指挥选择就绪任务时，以目标和所有就绪任务的标题检索记忆，避免只使用列表首项的任务信息。

选择结果为 `selected_task`、`instruction` 和 `action=implement|replan`，旧结果省略 action 按 implement 处理。replan 可用于审阅反馈暴露的缺少前置条件或验收不可达问题，直接进入既有再规划，不先消耗一次 worker 尝试。运行时同一次保存消费选择 receipt、登记 pending_replan，并清除同任务已被取代的待执行 worker 格式修正，绑定选择原文摘要及候选快照；原结果、故障证据与累计修正额度保留。再规划预算或调用失败后恢复继续该路线，原选择被篡改或候选变化则停止。格式修正只能修正文案，不能将 replan 改成 implement。

旧 run 若在上述转换后仍残留同任务 worker 修正，`resume` 在配置、进程退出、Git 绑定及 HEAD 预检后兼容清理。只处理带 `coordination` 来源的 pending/request 再规划或匹配的停止状态：核验原选择的身份、路径、摘要和当前候选快照，确认任务仍未接受、修正来源早于该选择，且此后没有同任务 worker 调用，再审计并保存清理。其他任务、角色的修正和阶段门禁均保留；已校验修订继续原归档恢复。清理不自动重开 stop、不改变预算或接受条件；操作者答复仍须通过原门禁，拒绝时列出活动门禁名称。

再规划处理任务拆分、缺少前置工作、验收不可达判断和整体验收发现的工程缺口。worker 通过 `blocker_kind` 区分 task、environment 与 operator；operator 等待明确回答，不先再规划；CLI 错误、权限、范围违例和现场不一致不会被包装成自动再规划。非法结果默认停止；明确归属且保留拒绝证据的 invalid_rejected 整体审阅可自动进入修正规划，操作者请求的有界非法结果修正另述。指挥返回 `{action, reason, new_tasks, updates}`：每个 update 仅包含 `id`、`description`、`add_depends_on`，stop 必须提供空操作数组。运行时在原计划上应用增量并重新检查 DAG，要求：

- 保留每个既有任务 ID，已接受任务完全不变。
- 未接受任务只可更新描述并添加依赖；标题、skills、required_commands、验收和 allowed_paths 均从台账保留，已有依赖不得删除。
- 新任务只使用已有 allowed_paths 中的模式，或操作者显式加入 approved_scope 的模式；不通过推测 glob 包含关系放大范围。
- 原始任务继续承担最终集成义务，拆分不能删除或重命名它来避开失败。
- 每次修订保存版本与原因。校验通过后先持久化待应用修订，再归档未接受修改并恢复隔离工作树，保留已接受提交。归档阶段中断时，恢复继续应用同一修订，不重复调用指挥或增加再规划次数；新的归档尝试保留独立目录。

`resume --allow-scope PATTERN` 可重复指定，仅用于 pending/request 阶段的再规划或已停止的再规划。先校验整批模式，再追加审计事件并保存去重后的 `approved_scope`；非法或显式受保护根路径使整批失败。授权只扩大新任务可选模式，不修改原始任务、既有任务的范围与验收，也不改变预算。已返回的越界修订可在新授权下校验同一 receipt，原结果仍保留；已校验修订不可追加范围。停止状态不因授权解除，必须另用 `--retry-stopped` 才重新请求规划。

每任务尝试数、整个 run 的 worker 尝试数、有效再规划数、最终验收次数和模型派发数均持久保存且受上限约束。分别由 `max_attempts`、`max_total_attempts`、`max_replans`、`max_final_attempts`、`max_agent_dispatches` 控制。再规划修订通过完整校验后才计数；合法 stop 及下述仅原因文本格式错误的 stop 同样计数，请求故障和未知 skill 不计入有效再规划数。所有实际模型调用（含失败、格式修正）消耗独立的派发额度，默认 200 次。最终验收默认 3 次，独立于任务预算。新任务与 resume 都不清零累计预算；额度耗尽保留待再规划意图，操作者增加预算后继续修正规划。

新 run 保存 `attempt_budget_policy="work"`：`attempts` 和 `total_attempts` 保持所有 worker 调用总数；`max_attempts` 按 `work_attempts` 判断，从任务调用总数扣除已经完整校验并与反馈一起保存的 environment/operator 阻断。豁免保存在对应 dispatch 的 `attempt_exemption`，绑定 blocker 类型、原始结果路径及 SHA256。缺少该策略字段的旧 run 使用 all_invocations，不重算历史预算。格式非法、调用失败或未知结果仍计入任务额度；总 worker 及所有模型派发上限仍包含每次等待调用。

豁免落盘前中断保守计费；落盘后恢复，以及派发前 hook 完成后、扣预算之前，均核验原始结果身份、语义和摘要。不因改变候选、再次恢复或重复回答增加豁免；证据缺失／变化停止，操作者决定仍必须通过原决策门禁。status 同时展示原始调用数与任务预算使用数，不把豁免当作已完成工作。

待再规划意图在任务、run、最终和再规划预算守卫之前持久化。保留原有预算上限：最后一次任务尝试返回 task blocker 时，先停在该上限；操作者增加对应预算后直接恢复指挥再规划，不先重派 worker。结构合法、空操作且明确 stop 的结果若仅原因文本格式错误，也计入次数并持久停止，保留原文、格式诊断和返回产物，不能由普通恢复重新抽取决定。

worker 尝试与派发计数在本地上下文、hooks、MCP 配置、快照和额度预检成功后，调用后端之前同一次保存。准备失败不扣额度，调用后端失败计数；派发前失败也将对应条目标为 failed 并确认没有遗留进程。格式修正共用此入口。

经校验的指挥 stop 持久保存原触发条件、原因和证据；普通 resume 不重新派发。显式 `--retry-stopped <task_id|final>` 重新打开该规划请求，不先重跑已受阻 worker，不清零任何预算。恢复未完成的 run 时，该选项与 `--reverify` 互斥；合用在任何台账或事件写入前拒绝，原 stop 和规划请求保持不变。整体拒绝后若接受了未改变快照的修正任务，记录已处理的修正任务集合并保存新的 pending_replan；再次请求规划受原预算约束，原整体拒绝始终保留，不重新审阅同一快照。

整体验证以非零退出失败后的空 diff 修正采用相同处理：保留失败记录，直接请求新的修正规划，不额外消耗最终尝试来重跑相同代码。显式 `--reverify` 仍可用于操作者确认环境已修复的情况。

恢复配置允许增加上下文预算、agent/验证超时、Claude 轮数、次数上限及任务容量 `max_tasks`，不可减少。超限修订提高容量后重验同一 receipt，不重抽结果；已保存 stop 仍须显式 `--retry-stopped` 或匹配的操作者决定。可以追加精确 `worker_commands` argv，已有授权不可删除或修改；记录配置放宽事件。模型 ID、供应商、effort、验证命令和共享资产不允许在同一 run 改变。进程退出及 Git 绑定、HEAD 检查仍先执行。新运行版本为 11；未完成版本 8、9、10 通过恢复预检后升级，历史及预算保留，共享资产摘要仍必须匹配。版本 7 及更早不能直接恢复执行。已完成 run 普通恢复直接返回原结果，不升级、不比较执行配置及共享资产。

## 验收不可达

若当前范围和操作者决定排除了满足某条验收所需的工作，应保留该项阻断，停止重复提交相同候选。worker 返回 `blocked/task`，指明验收项、证据和无法继续的边界；指挥也可在返工选择中直接返回 `action=replan`，随后在无可行修订时 stop。已回答的风险选择不应被反复询问，`continue` 不豁免验收，完成失败报告也不等于原任务完成。尝试或再规划预算耗尽仍会停止，增加预算不是解决不可达条件的证据。

操作者可以回答 stop，保留现场；核对已接受提交后从 run 分支手动集成需要的部分，再用 `abandon RUN` 收尾。未接受的候选另行检查，不视为已交付；run 不标为 completed。只有前置条件或可行范围确有变化时，才按既有显式恢复出口继续，原验收和总预算保持约束。

## 环境更新

worker 因环境受阻后，可用结构化更新说明已修复的环境事实：

```bash
python3 -m harness --config /path/to/harness.toml --repo /path/to/project resume run_实际ID --environment-update /absolute/path/update.json --step
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

`resume RUN --environment-update FILE` 接受最多 64 KiB 的严格 JSON，每个 run 最多 32 条更新：必填 `schema_version: 1`、`task_id`、`blocker_dispatch_id` 和非空、至多 4,000 字符的 `summary`；可选 `checks` 默认 `[]`，最多 16 项。未知字段拒绝。检查只允许 `{kind: "path", path: 绝对路径, path_type: "file"|"directory"|"any"}`、`{kind: "executable", name: 可执行文件名或绝对路径}`、`{kind: "worker_command", argv: 精确命令数组}`。命令检查以是否已在当前 worker_commands 或 verification.commands 中声明判定通过；未声明时仍保留更新及失败检查报告，追加精确配置授权后普通 resume 可重检。检查不执行命令、不启动子进程、不新增授权，也不保证真实 CLI、账号或服务成功。只需表达操作者事实时可省略 checks。

接受更新前在 run 锁内核对进程退出、Git 绑定、HEAD、当前任务与范围，以及保存的环境 blocker 来源身份和原结果摘要。只接受当前未接受任务的 worker 环境阻断边界；首次后续模型调用前中断，即使状态为 interrupted 或遗留 running，仍按这些证据判断资格。后续模型调用使旧来源失效，包括已开始但失败的指挥调用；需在新的 worker 环境阻断后再提交更新。存在待处理任务／最终验证或审阅、接受及接受后待办、再规划／指挥 stop、pending_directive、commander/reviewer receipt、待格式修正或活动非法结果修正时拒绝。完成或放弃状态拒绝更新；与 reverify、retry-stopped、allow-scope、repair-invalid 互斥，step 可合用。不得通过更新清除任何 receipt、修正守卫、已保存拒绝或批准快照。

更新输入复制到私有产物并绑定摘要，原输入文件不再是恢复依赖。所有历史更新、来源及检查报告在恢复及投影前核验；缺失或篡改时停止。有效来源的重复相同更新幂等，审计事件可因中断重复但台账只有一条引用。上下文只包含相关任务的最新摘要及紧凑检查结果：worker／任务 reviewer 看当前任务，dispatch 看 ready 任务，replan 看触发任务，final reviewer 看各任务。完整历史、checks、argv 和报告保留路径与 SHA256 引用；依赖省略的细节前须读取并核验产物，失败检查先查看详情。原目标、验收、范围、实际权限及未解决 blocker 始终保留，任务环境断言不得推广到其他任务。整体容量预估采用相同投影。

更新登记后历史不可撤回；最新同任务摘要及 checks 可替代旧环境假设及检查列表，不覆盖验收或权限。旧声明及报告以可检索引用保留，不重复内联全部文字或命令。历史输入契约不收紧；合法输入的紧凑投影仍可能超出上下文预算，届时增加 `context_chars` 后恢复。

同一任务最新更新的 checks 替代先前检查列表；`environment_history` 仅内联相关任务的更新引用，`environment_check_history` 仅保留每个相关 update 的最新报告引用。`environment_index` 提供内容寻址的不可变 JSON 路径、SHA256、update_count 与 check_count，索引的 updates/checks 保存完整台账引用。旧报告和其他任务历史按索引检索；已有索引损坏时拒绝覆盖，新增观测不会改变旧索引。选择前只预检已有的 current_task，不检查全部 ready 任务；选中任务在后续每次 worker 尝试（含格式修正）前仍重检，所有报告保留。已有当前任务的一次普通尝试可能在选择前与派发前分别记录报告，输入使用最近一份。检查失败不启动本次 worker，不消耗其尝试或派发预算。current_task 为空时，指挥选择可能已消耗派发预算；其选定指令保存到 pending_directive，后续恢复复用，修复条件后启动 worker 才消费该指令。worker 已返回并进入验证／审阅后不重跑这些前置检查；正常验证、独立审阅、快照和接受门禁仍全部执行，检查成功或操作者声明本身不是完成证据。

状态摘要的 worker_outcomes 仅分类 worker 自报完成、任务／环境／操作者阻断、运行故障与未知结果，不替代 accepted 或 completed 门禁。last_environment_blocker 是最近环境阻断的历史诊断；后续模型调用开始后标为 stale。stale=false 也不代表具备环境更新资格：更新仍需通过进程退出、来源摘要、任务范围和待处理阶段等检查。

## 进程退出与阶段恢复

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

worker 的格式修正一旦返回结果，就结束该待修正状态，即使返回结果仍然非法；结果落盘后中断也不会重放旧修正。指挥修正返回的结果由 receipt 保留并完成原校验；非法结果不能通过普通恢复重抽。worker 的后续 `resume` 按新的普通尝试处理，携带最近故障来源并继续消耗累计预算。只有修正尚未派发或 CLI 没有返回结果时才保留待修正状态。

worker 的失败反馈共享 9,000 字符的序列化摘录额度，保留状态、blocker 类型、各数组数量、记忆提案 key、完整结果路径及 SHA-256；后续使用前核验原始结果，缺失或改变则停止。完整执行证据和待审阅记忆提案不裁剪；必要标识和引用仍超过输入总预算时停止。身份和语义合法、但存在格式错误的 `blocked/task` 结果仍保存再规划意图，停止后 resume 直接进入有界再规划，不先重复 worker；环境 blocker 仍由操作者处理。

配置采用封闭键集合，拒绝未知顶层节和各节、角色字段，错误给出完整键路径。`status`、`memory` 与 `cleanup-verification` 不加载运行配置；源码外安装执行任务需显式提供配置和共享资源。运行事件、预检提示与 demo 横幅写 stderr，结果 JSON 保持 stdout 可独立解析。

计划确认和单步执行为显式选项：默认连续执行；选择暂停确认后必须显式批准。任何无回复或重复恢复都不能代替批准。step 以一个已接受任务为边界；最后一个任务之后还需要整体检查。

## 指挥结果与受控修正

指挥 plan、replan 和 dispatch 同样采用派发前 receipt：保存原 schema、输入包及摘要、dispatch、候选摘要、返回位置；结果原子落盘后继续原身份、结构、任务语义和格式校验。replan 另绑定持久请求 ID，合法新修订不能误用前次结果。结果摘要确定后缺失或篡改停止。post_agent 使用独立日志，保留至少一次语义；receipt 与计划、停止或已校验修订在同一次台账保存中消费。模型生成的 implement 任务选择先保存为 pending_directive，worker 本地准备失败保留它，调用后端前与尝试预算一起消费；replan 选择直接登记 pending_replan。已返回的指挥结果不再由普通恢复重新抽取；未返回结果的故障仍可显式恢复，格式修正保持原有一次额度。

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

下表同时列出持久证据引用，其内容完整性检查与上述加载时的结构／链接检查不同。`dispatches[].path_evidence` 在恢复执行、派发及接受门禁核验索引文件；`abandon`、`status` 和 `cleanup-verification` 不核验这些索引。仅索引损坏时，台账恢复链接有效且进程退出已确认，仍可 `abandon` 保留现场；移除工作树另须满足清理条件。

| 子状态 | 设置点 | 清除点与共存约束 |
| --- | --- | --- |
| `original_tasks` / 初始 `tasks` | 供给计划完整校验后，首次台账保存即保留任务副本；`tasks` 与首条计划历史随后共同登记 | 登记前允许 `original_tasks` 非空且 `tasks` 为空；恢复重新校验并登记供给计划。未消费的模型规划 receipt 仍先走原证据门禁，不以任务副本代替 |
| `pending_task` | worker 结果检查完成 | 验证失败、候选变化或任务审阅结束时清除；与 `pending_replan` 互斥 |
| `pending_acceptance` | 任务审阅通过 | 候选变化或接受台账保存时清除 |
| `pending_post_accept` | 接受提交及台账保存 | 记忆、事件和 post_accept 均完成后清除 |
| `pending_replan` | task blocker、整体失败、指挥任务选择 `action=replan`（同一次保存消费选择 receipt），或显式重开 stop | 修订登记、stop 或允许的 reverify 取消时清除 |
| `pending_directive` | 已校验的 implement 任务选择；replan 选择不进入 | worker 调用前与预算一起消费；本地准备失败保留 |
| `review_receipts` / `commander_receipts` | 调用前保存接收意图 | 结果被阶段状态消费、转入修正或允许的候选失效时清除 |
| `pending_format_corrections` | 格式错误后准备修正 | 成功、额度耗尽或候选失效时清除；worker 返回或同任务指挥选择 replan 时清除。旧再规划残留只经来源校验后兼容清理；尚未启动时允许没有新 dispatch ID |
| `review_corrections` | 首次需要格式修正的审阅 | 审阅被消费或候选失效时清除；可与 receipt、待修正状态并存 |
| `invalid_repairs` | 操作者授权显式修正 | 完成、失败或失效后仍保留来源与额度记录 |
| `format_repairs` | 修正调用前 | 累计额度不因新候选或 resume 清零 |
| `worker_recovery` | worker 调用内失败或中断 | 任务接受或应用再规划后清除；可与待验证、待审阅阶段共存 |
| `replan_stopped` | 校验并保存指挥 stop | 显式 retry-stopped 或匹配该来源的 operator-decision continue 转回 pending_replan 时清除 |
| `environment_updates` | 操作者环境更新审计并登记 | 版本 9 起的有序持久记录；不清除旧声明，最新同任务 checks 替代旧检查列表 |
| `environment_checks` | 每次前置检查保存报告后 | 追加保存全部报告引用；上下文携带相关任务最新更新的最近检查结果及完整报告引用 |
| `task_checkpoints` | 合法 worker 结果返回后，证据复制与摘要绑定完成 | 保留历次引用；同任务同检查 ID 采用最新记录，输入变化或过期标为 stale，不改变任务接受状态 |
| `checkpoint_batches` | 当前 dispatch 各项校验及 resolution 写入完成后，与有效检查点一起保存 | 绑定原始结果、有效 manifest 集合及丢弃原因；恢复不能把部分登记当作完成。旧批次没有此字段仍按旧协议核验 |
| `dispatches[].attempt_exemption` | 完整合法的 environment/operator blocker 与反馈一起保存 | 仅新 work 预算策略排除该任务额度，保留所有调用计数；消费前核验绑定来源，未知／非法结果不豁免 |
| `decision_requests` | 当前 operator 阻断、指挥 stop，或来源任务接受且 post_accept 完成 | 请求与原始结果绑定并保留；后续任务请求可从 accepted 中恢复登记，未答复或 stop 时禁止目标任务派发 |
| `operator_decisions` | 严格校验显式答复后，审计先于台账 | 仅解除匹配请求的等待；后续调用开始后拒绝重交旧答复，不清除已有结果或累计预算 |
| `accepted[].worker_source / decision_requests` | 任务接受台账保存时 | 保存接受的精确 worker 来源及后续决策请求，不能用其他尝试的结果替代 |
| `dispatches[].invoked` | 本地准备时为 false，后端调用前同预算保存为 true | 后续真实调用使旧环境 blocker 来源过期；旧记录缺该字段时保守按已调用处理，不替代进程退出检查 |
| `dispatches[].path_evidence` | 本地准备时随 dispatch 条目保存，早于调用预算；记录输入引用的路径索引路径、摘要与数量 | 不清除；未完成 run 的 resume 预检、派发进出及接受前核验索引。损坏、缺失或符号链接阻止继续，恢复原字节后沿用原结果；旧 dispatch 无此字段时兼容。abandon、status、cleanup-verification 不核验索引 |

新增子状态或改变保存顺序时，应同步更新此表并验证中断后的合法组合；不能将上述状态一概视为互斥。

### 阻塞状态与操作者出口

以下命令均沿用同一 run 的预算、证据和接受条件。出口可以是继续执行，也可以是保留现场后放弃；不要求每个非法结果都存在可继续的修正方式。表中 `abandon` 仍要求台账恢复链接有效、进程退出已确认。

| 状态或原因 | 操作者出口 | 仍须满足的条件 |
| --- | --- | --- |
| 提供的计划非法 | 修正 `--plan` 文件后重新 `run` | 此时尚未创建 run 现场 |
| 任务声明的精确命令未授权 | 追加 `permissions.worker_commands` 后普通 `resume` | 预检不消耗 worker 额度；`--approve-plan` 不授予命令权限，无需环境 blocker 更新 |
| 等待计划批准 | `resume --approve-plan` | 不以普通 resume 代替批准 |
| 次数、上下文或超时预算不足 | 按允许范围增加配置后 `resume` | 累计计数不清零，已有结果不重抽 |
| 旧进程存活或退出未知 | 核实后 `resume --confirm-exited ID`；终态使用 `cleanup-verification RUN --confirm-exited ID` | 确认精确 dispatch，先审计后写入，不终止无关进程 |
| worker 或未返回结果的模型调用故障 | 修复环境后 `resume` | 新进程、新 dispatch，保留故障来源并消耗对应预算 |
| worker 已报告环境阻断，需要更新环境事实 | `resume --environment-update FILE`；已保存检查仍失败时修复条件后普通 `resume` | 来源有效且无待处理阶段；检查不执行命令，更新不扩大权限或降低验收，失败检查不消耗本次 worker 尝试或派发预算 |
| 验证、审阅或接受 hook 中断 | 修复原因后 `resume` | 同一快照复用已有阶段；hook 不被绕过 |
| 自动格式修正耗尽，且满足显式修正条件 | `resume --repair-invalid ID` | 最初结论、已绑定原文、失败修正证据与快照不变；整个角色／类型／任务仅一次，指挥任务选择和再规划各按固定身份在整个 run 共用 |
| 首次拒绝仅可变字段格式非法，且自动额度已用 | 保存为 `invalid_rejected` 后进入任务返工或整体修正规划；中断后普通 `resume` | 原文、摘要、快照及 hook 仍须核验；不追加格式修正派发，不适用于修正结果或旧拒绝型 exhausted 守卫 |
| 支持的语义非法 plan/revise（含 required_commands 重复或超过 32 条）或批准元数据 | `resume --repair-invalid ID` | 原有语义修正约束及同一显式额度 |
| 修正改变原结论、批准的不可变字段超限、已有 unrepairable 或拒绝型 exhausted 守卫、显式修正再次非法，或缺可信原始依据 | 保留证据后 `abandon` | 不重新抽取同一候选的结论；任务候选确有变化时仅适用既有归档与完整重做路径 |
| 指挥明确 stop | `resume --retry-stopped TASK_ID` 或 `final` | 保留原 stop，仍受累计再规划和派发预算约束 |
| 新任务需要额外文件范围 | 待再规划时 `resume --allow-scope PATTERN` | 只增加新任务可选模式；已停止时另需 `--retry-stopped` |
| 最终验证超时／非零退出 | 超时修复后 `resume`；非零退出走修正规划，确认环境修复后可显式 `--reverify` | 已返回的指挥结果和模型拒绝不可由重验丢弃；预算耗尽时 `--reverify` 不取消待再规划意图 |
| HEAD、Git 绑定、原始证据或候选不一致 | 核对并恢复可信现场，或 `abandon` 保留证据 | 不手工伪造接受依据；任务候选变化须重新执行验证与审阅 |
| 启动中断且 `worktree_binding` 仍为空 | `abandon RUN` 保留证据后重新 `run` | 没有已保存的可信绑定，不能 resume 或自动推断；若工作树或 `harness/<run>` 分支已创建，核对后手工执行 `git worktree remove` 并删除该分支 |
| 移除工作树被未保存文件阻止 | 核对诊断路径、归档或清理后重试移除 | 忽略文件也不得未经核对删除 |

环境更新与操作者决定共用九个待处理阶段键：`pending_task`、`pending_acceptance`、`pending_post_accept`、`pending_replan`、`pending_directive`、`review_receipts`、`commander_receipts`、`pending_format_corrections`、`review_corrections`。环境更新另将 `replan_stopped` 视为忙碌；决定答复则单独核对停止请求的目标、原因及来源。两入口保留各自的 invalid repair 判断：pending 修正阻断，completed 修正的匹配 receipt 未消费时仍阻断，消费后不会仅因历史修正完成而永久阻断。准备门禁不写入更新或答复，也不消耗模型预算；原台账版本与证据保存顺序不变。

来源过期判断仍有意保留入口差异：环境更新只计来源之后 worker／commander／reviewer 的后续模型调用；worker／replan_stop 决定登记计来源之后的全部 dispatch；决定答复或修订从保存的 `dispatch_count` 起算，handoff 只计目标任务。后两处以 `.get("invoked", True)` 判断，因此没有 invoked 字段的 verification 也会被表达式计入；目前未发现该差异在正常可达流程中造成错误过期，不将状态注入测试视为实际生命周期缺陷证明。status 对三种模型角色按 `invoked is not False` 保守诊断原始坏台账。

供给计划随首次台账保存，Git 绑定已保存但计划尚未登记时，普通恢复沿用原任务、验收、范围、依赖与命令前置条件，不追加模型规划派发；`--pause-after-plan` 仍等待显式批准。该保证不补造缺失的 Git 绑定，也不迁移旧 run 中已经遗失的计划。修复前若供给计划尚未保存、`tasks` 和 `original_tasks` 均为空，台账无法辨认原计划来源；应保留证据后 `abandon`，再用原 `--plan` 重新运行。

旧版本若已为重复或超限的 `required_commands` 派发自动格式修正并保存修正结果，本次校验调整不会迁移该现场。原始证据、receipt 和修正守卫继续生效，普通 `resume` 或 `--repair-invalid` 不能绕过已经形成的阻塞；需保留现场后 `abandon`，另行发起新 run。上述语义修正出口适用于尚未进入该错误自动修正流程的结果，不提供台账重写或预算重置。

## 可复用检查点与操作者决定（版本 10）

WORK 可选 `checkpoints`，最多 16 项，每项为 `{id, summary, inputs: [{path, sha256}], evidence: [{path, sha256}], freshness, max_age_seconds}`。ID 在本次结果中唯一；summary 非空、最多 4,000 字符；inputs/evidence 各最多 8 个文件，evidence 至少一个。文件必须为允许的普通文件，SHA256 为实际字节摘要；运行时将原始 evidence 私有保存。`freshness="content"` 必须绑定至少一个 input 且 `max_age_seconds=0`；`volatile` 的有效期为 0–86,400 秒，0 表示每次都需重新检查。有效期保守地从来源 worker 的调用开始时间计算，worker 内部执行时间也会消耗有效期；缺少有效调用时间的时效检查点一律视为 stale，不从结果落盘时间重新计时。输入及证据文件拒绝符号链接；使用解析后的实际路径（例如 macOS 的 /private/tmp）。单份证据最多 1 MiB，一次结果的副本总计最多 16 MiB。投影根据输入字节、证据摘要及有效期标记 reusable/stale，并保留历史引用。reusable 仅证明声明的依赖未变且未过期，不证明依赖完整、推理正确、命令成功或任务已接受；时效事实、未声明依赖和未解决 blocker 仍需核实。检查点不能绕过验证、独立审阅或接受门禁。

允许的检查点源为不含 `.git`、`.harness`、`.codex`、`.claude` 保留路径段或 `..` 的普通文件；工作树内路径按相对工作树的部分检查。仅 evidence 额外允许当前 worker dispatch 的 `commands/` 中的普通文件，可直接引用 broker receipt 的绝对 stdout/stderr 路径及 SHA256；其他 dispatch、run、私有文件及 inputs 均不获得该例外。符号链接和文件大小门禁仍生效。

格式修正也是新 dispatch，不能重新申报原 dispatch 的 broker 输出作为检查点证据。可以省略可选检查点，或仅重跑已授权且可安全重复的检查；不要为重新取得证据重复有副作用的操作。其他允许的普通文件证据仍可使用。原命令日志保留；通过输出契约后，completed 修正结果的检查点核对失败记录丢弃事件，blocked 结果另携带有界反馈。不继承旧 broker 证据，也不刷新旧 volatile 证据的时间起点。

必要上下文只展开相关任务的最新检查点，`checkpoint_sources` 按 dispatch ID 共享来源。历史不重复最新行，超过 4,000 字符时省略内联历史并报告数量；已接受任务也不在整体审阅中展开。`checkpoint_index` 指向 SHA256 绑定的不可变索引，包含全部 manifest 路径和摘要，可检索被省略的历史及已接受任务的证据。完整声明、记录时间和输入／证据细节从 manifest 读取。投影前仍核验全部保存证据，索引损坏不会被覆盖。默认预算测试覆盖三次尝试各 16 个同 ID 检查点，以及四任务各四项的最终审阅；不同 ID 累积、较长路径或其他必要内容仍可能超限，此时增加 `context_chars` 后恢复，不能靠删除证据或降低接受门禁继续。

当前任务需要用户决定时，worker 返回 `status="blocked", blocker_kind="operator"` 和具体问题；运行停止等待回答，不先再规划。已满足本任务验收的 worker 可以在 completed 结果中附带 `decision_requests: [{id, task_id, question}]`，将明确风险选择留给传递依赖于本任务的后续任务。本任务仍正常验证、审阅、接受；该请求不表示后续操作被批准，目标任务在得到回答前停止。禁止用后续阶段的操作／测试证明作为前一分析任务的完成条件；负面分析结论可以完成，但必须满足该分析任务自己的验收。

worker 通过身份、结构、完成／停止语义及格式门禁后，本次可选检查点的 content 输入／TTL 规则、实际哈希、路径、源文件或大小核对失败只丢弃对应项并记录 `checkpoints_discarded`，同批有效项保留；重复 ID 丢弃整批检查点并记录事件，保留 worker 结果和原始 blocker。合法 environment/operator 等待仍登记反馈及预算豁免，operator 等待须显式答复，environment 等待可提交环境更新。进入逐项处理的结果先写入 `checkpoints/batches/<dispatch_id>.json`，绑定原始结果、有效 manifest 行及按声明索引记录的丢弃原因；其摘要与有效行一并登记，恢复核验集合完全一致。resolution 已写但台账未保存时继续登记同一结果，不重新计算有效期；私有副本或 resolution 损坏不覆盖。

上述丢弃不涵盖输出契约错误。缺字段、错类型、非法枚举仍按结构错误停止；超过 16 项、空 evidence、SHA256 格式非法或字段长度超限等按格式错误处理。身份、结构与完成／停止语义合法的 completed 结果，在自动修正额度和预算允许时新派发一次格式修正；blocked 结果保存 blocker（operator 登记决定请求，task 保存再规划意图）并停止，不自动重派，不获任务预算豁免。

blocked feedback 携带任务、dispatch 及前 2,000 字符原因；完整原因保留在事件中，completed 结果保留事件。原始结果绑定、已有台账、私有副本完整性和私有写入错误仍停止。已保存检查点可跨 dispatch 读取，不能重新登记旧 broker 输出来刷新时效。

结构、格式及主状态合法的 blocked 结果若附带 `decision_requests`，不登记这些后续请求，仅记录 `decision_requests_discarded` 事件及原结果摘要。原始结果和反馈保持不变，阻断、合法检查点、environment/operator 尝试豁免继续登记；恢复和来源校验采用同一语义。字段类型、格式或主状态非法仍拒绝，不因此绕过接受门禁。

worker packet 的 `handoff_targets` 列出未接受直接／传递下游的 ID 与标题，输出约束和运行时校验使用同一集合。新返回的 completed handoff 目标或问题不合法时，在验证及接受前拒绝本次尝试，拒绝原因和 dispatch 来源经 `worker_recovery` 交给后续指挥与 worker，不静默过滤请求。独立校验成功的检查点在此门禁前保存，供后续读取未接受证据；这不生成合法决定或推进任务阶段。无合法下游时，必要的选择应作为当前任务的 operator blocker 报告。

operator 问题空白、含 NUL 或超过登记长度时，运行时保留原始结果并登记有界说明及转义摘录。操作者须读取 request 的 `source` 所绑定的原始问题，再用正常 `--operator-decision` 回答；普通 resume 只保留等待，不消耗新派发预算。此处理也恢复旧版本因问题文本而登记失败的 operator 请求和已接受 handoff。旧版本已经保存的 `pending_task`／`pending_acceptance` 继续其验证、审阅及接受阶段，随后登记请求，不能提前派发依赖任务；原始产物、接受记录、预算及合法请求身份保持不变。指挥 stop 原因含 NUL 时不登记问题，普通恢复仍提示原有 `--retry-stopped` 出口。

从 `status RUN` 取得运行时 request ID 后提交：

```bash
python3 -m harness --config /path/to/harness.toml --repo /path/to/project resume RUN --operator-decision /absolute/path/decision.json --step
```

```json
{"schema_version": 1, "request_id": "request_id_from_status", "task_id": "target_task", "decision": "continue", "summary": "操作者对该问题的明确选择及理由"}
```

输入是最多 64 KiB 的严格 JSON，只接受上述字段，summary 非空且不超过 4,000 字符；`decision` 为 `continue` 或 `stop`。回答绑定任务、问题和原始来源，私有保存并绑定摘要。continue 允许针对该问题使用全新 worker 尝试或继续原再规划；指挥 stop 的 continue 重新打开原规划请求并保留旧结论和证据。stop 保持等待。handoff 的答复可在目标任务的后续模型调用前显式修订，无关任务的执行不关闭窗口；worker 和指挥 stop 的答复仍只能在后续模型调用前修订，来源过期后拒绝旧回答。回答不改变目标、scope、命令授权、验收或累计预算，也不产生完成证据。仍可用原 `--retry-stopped` 在不附说明时显式重试指挥 stop，但不能覆盖已记录的操作者 stop。

`--operator-decision` 可重复指定多个文件。整批先验证，重复 request ID 或任一非法／过期答复使整批拒绝且不登记答复；通过后预写不可变证据并一次保存全部答复，再进入调度。任一 stop 使本次恢复保存整批后停止，不派发模型。预写中断后按原文件和原顺序重试，已落盘但未登记的产物会复用；改变顺序可能因 sequence 冲突而停止。每个任务仍需全部相关问题答复才可派发，无关任务沿用原调度规则。

`--operator-decision` 与 `--environment-update`、`--retry-stopped`、`--reverify`、`--repair-invalid`、`--allow-scope` 互斥，可与 `--confirm-exited`、`--step` 合用。操作者已回答 stop 时，必须向同一请求提交新的 continue；单独 `--retry-stopped` 不能覆盖该决定。完成或放弃的 run 拒绝回答。操作者决定是任务范围内的选择，不是其他任务的环境事实、记忆指令或权限授予。

工作上下文按任务筛选决定，整体审阅和再规划包含全部请求的紧凑投影。问题与答复摘录共享 4,000 字符额度，每段最多 500 字符；ID、来源和最新 continue/stop 保留，未答复为 response=null。`operator_decision_index` 的路径与 SHA256 绑定完整请求、当前答复及全部历史答复，含原产物路径和摘要；依赖截断信息前须读取全文。被省略的来源仍须通过完整性校验，原文和台账不被截断或替换。

已有操作者答复的恢复在继续工作前重做整体容量预检。可修订且随条数缩减的决定投影仅计入预计容量，超限给出增大 `context_chars` 的预警；确定的必要下界超限才提前停止。最终审阅和预检只携带检查点索引，避免把当前未接受任务的临时展开内容当成最终下界。实际派发仍核对完整输入；若最终审阅因此超限，增大预算后普通 resume 继续同一次最终尝试，复用已完成验证，不删除决定或重跑已接受任务。

## 命令子进程证据（版本 11）

worker 独有的 `harness_commands` broker 在 dispatch 私有 `commands/` 中保存每次精确 argv 命令的 stdout/stderr 原文、摘要 receipt 和子进程 journal。恢复、abandon 与验证检出清理都核对这些子进程的退出证据；父 dispatch 已退出或缺少 pgid 都不能证明命令未启动或已经退出。证据缺失、损坏或退出未知时保留现场并停止。

每条 broker 命令的超时取 `runtime.verify_timeout_seconds`，可在恢复时调大；适配器配置的 MCP 超时另加 10 秒，整个 worker 仍受 `agent_timeout_seconds` 限制。

操作者核实后仍以父 dispatch ID 提交 `--confirm-exited`，审计记录绑定命令子日志摘要，再保存该精确 dispatch 的确认；不终止无关进程，也不跳过 HEAD、快照、Git 绑定或文件清理检查。新运行写版本 11；未完成版本 8、9、10 经预检后升级且保留历史与预算，版本 7 仍不能恢复执行。`cleanup-verification` 可检查版本 7 至 11，包括终态 run。

## 终态与版本兼容

新运行台账为版本 11，保存环境更新、可复用检查点、操作者决定和命令子进程证据。未完成的版本 8、9、10 在恢复预检通过后升级，原预算、证据和历史保留；共享资产摘要仍须匹配，更新本仓库规则不构成继续旧 run 的授权。版本 7 及更早台账可通过 `status` 查看，不能直接恢复执行。列出状态时，损坏或缺失的台账会产生诊断，不阻止查看其他运行。已完成 run 的普通 `resume` 直接返回保存结果，不比较执行配置或共享资产，不升级台账、不派发或记账；CLI 仍需能加载当前配置与 hooks。已完成 run 拒绝 `--confirm-exited`、`--repair-invalid`、`--allow-scope`、`--environment-update` 和 `--operator-decision`；`--reverify`、`--retry-stopped`、`--approve-plan`、`--step` 不生效。

## 计划范围与命令前置条件

可用 `--plan /path/to/plan.json` 提供已有计划，格式见 [examples/plan.json](../examples/plan.json)。计划会检查任务 ID、依赖环、非空验收条件、文件范围及引用的 skill 是否存在。`allowed_paths` 从仓库根目录匹配：`src/*` 只匹配直接子项，`src/**` 匹配所有层级，`**/*.py` 也可匹配根目录 Python 文件；只有独立的 `**` 路径段跨越 `/`。显式指向 `.git`、`.harness`、`.codex`、`.claude` 的范围在规划时拒绝，实际变更仍会独立检查这些受保护路径。唯一就绪任务在首次尝试且没有反馈时直接使用完整任务简报派发；存在选择或返工反馈时才调用指挥。

用户提供的计划在创建 run 目录、分支和 worktree 前完成校验，输入错误可直接修正后重试。任务范围应覆盖实现所需的具体测试与夹具，前置工作应有明确归属与依赖顺序。`**`、`**/*` 等常见全仓库模式仍允许使用，但登记计划时会向 stderr 提示并记录 `broad_scope_warning`；这类范围不限制普通项目文件，受保护路径门禁仍生效。

任务可声明 `required_commands`，例如 `[["node", "scripts/lan-share/mode-ui-test.mjs"]]`，最多 32 条不同的完整 argv。这是执行前置条件，不是授权。登记计划和恢复时提前列出所有未授权项；对应 worker 派发前重新与 `verification.commands`、`permissions.worker_commands` 精确比较，不执行命令。参数、空参数、字面 `*` 和换行均原样比较，不接受命令前缀，`verification.setup_commands` 不授予 worker 能力。没有声明的旧计划保持原行为，已知命令应由指挥显式列出，未知参数仍由后续发现阶段报告。

缺少声明命令的任务不会启动 worker 或消耗其尝试；其他就绪且满足权限与决定条件的任务可以继续。已有任务选择和恢复现场保持绑定。没有可执行任务时停止，`status RUN` 显示 `missing_command_requirements`；在配置中追加精确 `permissions.worker_commands` 后普通 `resume` 即可重检，无需 `--environment-update`。计划批准不增加权限；此检查不证明命令存在、执行成功或验收通过，也不新增验证命令。已返回的 worker 结果仍继续原验证和审阅门禁。

角色 skill 自动注入一次，指挥看到的可选任务 skill 目录不包含三个内置角色 skill；不需要额外技能时使用 `skills: []`。旧计划中的角色 skill 引用仍合法，同名 worker 角色内容不会重复注入，其他任务 skill 正常保留。

## Git 现场与验证检出清理

创建工作树时记录实际 Git 管理目录、公共目录、分支及双向链接文件的摘要。运行时的工作树 Git 命令显式固定管理目录和工作目录，排除环境中的 Git 重定向参数，并在操作前后检查绑定；改写 `.git`、切换分支或改变管理目录链接会停止运行。恢复、接受提交、再规划归档、记忆来源核验和移除工作树都遵守该绑定。Claude 内置编辑权限另拒绝工作树 `.git`，但脚本仍依靠运行时门禁检查。

开启 `verification.fresh_checkout` 后，整体验证在目标仓库之外的全新 detached worktree 中执行，先按精确 argv 顺序准备依赖，再运行验证。准备命令仅由运行时执行，不自动授予 worker 命令权限；不得修改已跟踪文件或留下未忽略的新文件。任务级验证仍在原 run 工作树中执行，因此它所需的依赖仍应提前可用。默认 `fresh_checkout = false`，`setup_commands = []`；同一 run 恢复时这两项不可改变。

准备失败或中断保留日志，普通 resume 在另一个全新检出重试当前最终尝试，不重复 worker；已经完成验证而仅审阅失败时复用原验证证据。新检出位于系统临时目录的私有 `harness-verify-*/.harness/checkout`，路径、绑定和准备结果记入验证目录的 `checkout.json`，保留供检查，不由 `abandon --remove-worktree` 自动清理。临时目录若位于目标仓库内则拒绝使用。此模式隔离工作树遗留文件及主仓库父目录依赖，仍使用当前进程环境、Git 配置和操作者声明的准备命令，不承诺容器级可复现性。

`cleanup-verification RUN` 默认只返回各检出的可清理状态；`--remove` 在运行锁内确认旧进程退出、路径归属、Git 绑定及原验证提交后逐项移除，保留运行台账、diff、日志和 `checkout.json`，另写清理记录。已跟踪修改、未跟踪或忽略文件均阻止清理，需要先人工核对。目录已缺失时，仅在原绑定仍可核验的情况下注销该精确路径，不执行全仓库 prune。该命令兼容版本 7、8、9、10、11 的台账，不加载配置或调用模型。

所有恢复和清理入口都核对父 dispatch 及 broker 命令子进程；父进程退出不能证明子进程已退出。旧 dispatch 没有可确认的退出记录时，清理与恢复均停止；缺少 pgid 不能证明进程未启动。核实退出后可执行 `cleanup-verification RUN --confirm-exited DISPATCH_ID`，可重复指定 ID，也可与 `--remove` 合用。此确认入口支持已完成或已放弃的 run，先保存审计事件，再更新指定 dispatch 的确认，不改变 run 状态或放松文件清理门禁。未指定确认时，预览不改运行台账。

移除前还独立核对已跟踪文件的字节与 HEAD，并拒绝 `assume-unchanged`、`skip-worktree` 标记。换行转换或 clean/smudge 过滤导致工作树字节不同，也会保守拒绝；此时可只放弃运行并保留工作树。

未跟踪文件（包括忽略文件）导致清理失败时，错误列出前 10 个阻塞路径及剩余数量，长路径作有界显示；这些文件仍需人工核对，不自动删除。

运行开始、worker 返回及验证结束还保存被忽略路径的清单和相对初始状态的增减，变化写入事件；审阅输入带清单产物引用。目录折叠显示，此诊断不哈希忽略文件内容，也不能发现既有依赖目录内部的修改，不能替代全新检出验证。

隔离工作树内部不允许创建 `.harness` 文件、目录或符号链接，即使 Git 忽略该路径，任务范围和运行时快照检查仍会拒绝它。目标项目根目录的真实 `.harness/` 状态目录不受此限制。

若验证中途产生未忽略的新文件，可在核对内容后，通过 `git rev-parse --git-common-dir` 定位公共 Git 目录，在其 `info/exclude` 中加入精确生成产物模式，再恢复。linked worktree 共用该文件；这会改变后续快照可见范围，不豁免已跟踪文件，不能用宽泛模式掩盖待交付代码。

`abandon RUN` 保留分支、工作树和证据并禁止恢复；`--remove-worktree` 只移除该 run 的干净工作树。旧进程未确认退出、HEAD 不符或有任何未保存文件（包括忽略文件）时拒绝移除，不使用强制删除；全新验证检出由 `cleanup-verification RUN --remove` 独立清理。

## Hooks 与共享资产兼容

脚本工作目录是配置所在目录，从 stdin 读取 JSON 事件，非零退出码或超时阻止继续。全部事件、argv 和正有限超时值在执行任何脚本前校验。通过事件中的路径定位目标项目。默认空数组表示没有额外策略。`context_prepared` 表示已组装有界输入；旧名 `context_compacted` 仍可单独配置作为别名，不能与新名同时配置非空脚本。它们与厂商原生的 `PreToolUse`、`PostToolUse` 不是同一层；运行时不截获所有原生工具调用。生成的调用会关闭个人原生 hooks/自动记忆，避免重复采集；企业管理配置可能仍有更高优先级。

所有 hook 事件都有 `run_id`、`repo`（目标仓库）、`worktree`（该事件的工作树）和 `run_dir`（运行证据目录），路径为绝对路径。全新检出的 `pre_verify`／`post_verify` 使用实际验证目录作为 `worktree`，另以 `source_worktree` 指向原 run 工作树；其他事件默认使用 run 工作树。

`post_agent` 接收已通过结果校验的调用，以及明确保留为 `invalid_rejected` 的审阅。其他非法结果在调用该 hook 前停止。“至少一次”指已进入 hook 门禁后的重试语义，不要求每个非法模型输出也触发 `post_agent`。

`pre_accept` 前保存已通过审阅的快照摘要和待接受状态。hook 失败会保存错误及日志引用；恢复时若快照不变，只重试该 hook，不重派该任务的 worker/reviewer。若文件或暂存区摘要已改变，旧审阅失效，必须重新执行任务、验证和独立审阅。每次 `pre_accept` 尝试使用独立日志目录。

`post_accept` 失败时保留已接受提交及待完成的 hook；恢复会先重试该 hook，成功前不能执行下一个任务或完成 run。hook 在成功后、检查点写入前中断时可能再次执行，需要将外部副作用写成可重复执行的操作。

`post_accept` 与 `on_error` 的每次执行也使用独立日志目录，恢复成功不会覆盖此前的失败输出。

任务记忆、获批知识提案及接受事件也有持久待办，在 `post_accept` 之前完成。SQLite 故障后恢复补写；稳定操作 ID 与内容摘要防止写入成功后中断造成重复版本，接受事件使用稳定 ID 去重。任务记忆只保存有界摘要和完整审阅产物引用。恢复补完一项待接受后续工作也计入 `--step` 的一个任务边界。

共享资产摘要按规则、skill 名称、MCP 和 hooks 的逻辑身份及内容计算，字节一致的 Harness 安装可以搬迁；目标项目与运行状态路径仍须保持一致。摘要不会递归追踪任意 hook 命令引用的脚本或依赖。操作者仍需保证这些外部内容在恢复时保持一致。

创建 run 前校验所有角色可见且已启用的 MCP 服务配置，缺少 `transport`、stdio 的 `command` 或 HTTP 的 `url` 等错误会指出具体服务与字段。派发前再次校验当前角色的配置，本地配置错误不消耗 worker 或模型派发次数；关闭的示例服务可以保留未填写的连接字段。此预检不连接服务，也不证明凭据或服务可用。

`python3 -m harness sync --role worker` 生成 `.harness/generated/worker/claude.mcp.json` 和 `codex.mcp.toml`，供检查或手动使用；每次正式 dispatch 也会生成并传入自己的 MCP 配置。不会覆盖 `~/.claude` 或 `~/.codex` 的配置。Codex 调用使用 `--ignore-user-config` 保留登录凭据并减少个人配置干扰；如果你的登录依赖自定义 provider/base URL，当前适配器需要先显式扩展后才能复用该配置。

Claude worker 通过锚定在工作树真实路径的 `Edit(//.../**)` 规则允许内置 Edit、Write 工具写入，路径中的模式字符按字面转义；同时允许读取工具、部分读取命令和配置的验证命令。该规则不约束验证脚本或其他子进程自行读写文件。Claude 指挥与审阅角色也可使用受限的 Git 查询命令；输入包列出可用命令示例。额外的 worker 命令通过 `permissions.worker_commands` 声明为完整 argv，示例见 [README](../README.md#共用-skills--mcp--hooks)。

精确 argv 可包含字面的 `*` 或换行，由命令 broker 原样传递；旧 Bash 授权仍过滤这些 argv，以免 CLI 将其解释为权限模式。需要管道或重定向时，必须显式授权完整的 `["/bin/sh", "-c", "固定脚本"]`，broker 不自行添加 shell。不会自动组合命令或授予 `gh`、`ssh`、`curl` 等命令前缀。需要删除或移动文件时，可显式配置包含具体路径的 `rm` / `mv` argv；默认不授予这些命令。不得使用后台运行或权限绕过来替代 broker。不使用跳过所有权限检查的参数；CLI 自身仍可能允许内建只读命令，这些授权不是操作系统沙箱。必要命令被拒绝时，worker 应报告环境 blocker。当前 Codex 适配器只提供只读模式，因此配置 Codex worker 会提前报错。Codex 只读角色设置 `project_doc_max_bytes=0`，关闭项目指令文件的自动注入；Claude 使用 `--setting-sources ""`，当前适配版本关闭项目／本地来源的指令加载。已提交的项目指令也需由角色按需读取。其他 CLI 内建上下文及工具读取内容仍可能影响模型，100,000 字符预算仅覆盖编排器生成的输入包。

两种只读角色的调用方式不同：Claude 通过工具许可限制可用写入工具和命令，Codex 调用显式传入 `--sandbox read-only`。这些配置不承诺工作树之外的读取隔离；需要更强隔离时使用专用 OS 用户或容器。

任务与整体审阅包会单独列出根级或嵌套的 `AGENTS.md`、`AGENTS.override.md`、`CLAUDE.md`、`CLAUDE.local.md` 变更，包含新增、修改、删除及重命名涉及的路径。候选指令文件作为待审阅数据，不授予改变原目标、验收或工具权限的权力。审阅者需按 `instruction_file_changes` 读取这些文件作为证据，检查其对后续派发的影响；合法范围内的指令维护仍可通过正常门禁。

## 记忆作用域与来源兼容

SQLite 存在目标项目 `.harness/memory.sqlite3`，项目之间独立。每条记忆保存来源、时间、类型和版本。相同 `key` 在相同有效作用域内的新记录替代旧记录的默认检索资格，旧记录仍可按 ID 查询。key 在项目内全局唯一；项目与 run、不同 run 之间的同名写入在事务内拒绝，原记录保持有效，需要使用不同 key。旧 unscoped 人工记录按项目作用域处理版本替换，读取时仍保留原标记。支持 TTL。系统不会自动猜测两条不同 key 的事实是否矛盾。

同一项目的不同 run 使用独立分支和工作树，共享此项目的记忆库。任务历史保存结构化的 run、task 和 commit 来源；召回时核对来源台账及任务接受记录，来源已放弃、缺失或无效时不自动召回，仍可通过 `read/history/export` 审计。其他 run 的有效已接受记录标为 historical，项目知识继续按文件哈希和提交核验后参与检索。所有记忆明确标为 reference_only，不授予当前 run 的命令权限或操作者决定。数据库初始化使用短时等待锁，覆盖 WAL 设置、建表和迁移，不将整个 run 串行化。`memory search/read/history/export` 使用只读连接，不建库、不迁移，也不改 Git `info/exclude`；SQLite 仍可能管理 WAL 辅助文件。

任务历史仍使用 `task:<run_id>:<task_id>`，保留每次已接受结果。接受台账和任务记忆的摘要来自 reviewer 对代码与证据的独立审阅；worker summary 只保留为原始执行证据。worker 可另行返回 `proposed_memories`，每条包含稳定 key、摘要、事实内容和仓库来源文件；审阅者通过 `approved_memory_keys` 单独批准。运行时将通过检查的提案写入 `knowledge:` 命名空间，并保护操作者维护的记录。代码通过不代表所有记忆提案通过，agent 没有直接写记忆的 MCP 工具。内置 MCP 也使用 SQLite 只读连接，不建库或迁移；库不存在时搜索为空。

自动任务记忆与项目知识默认 TTL 均为 180 天，可用 `[memory]` 的 `task_ttl_days`、`knowledge_ttl_days` 配置，设为 0 表示不过期。操作者手写事实默认不过期。项目知识保存提交和来源文件哈希；检索时来源变化会标记 `possibly_stale` 并降权，保留原记录供查证。类型权重帮助项目知识和手写事实优先于同等相关的任务流水，但不能替代对当前代码的核对。

`memory add` 默认保存项目事实；临时运行说明可用 `--run RUN_ID` 绑定已有 run，仅向该 run 的模型检索公开，不能替代同名项目事实或其他 run 的说明。CLI 查询这些说明也需 `memory search ... --run RUN_ID`；MCP 的 search/read 都以当前 run 计算 applicable/historical，CLI read 保持无 run 视角的历史查询。新规则不自动恢复此前已被替代的版本，可用 read/export 核对后重新写入适当的新 key。真正的继续／停止选择应使用 `resume --operator-decision`，不能用记忆替代。旧人工记录保留为 unscoped/reference_only，缺少结构化来源时标记 missing_provenance；旧任务记录可从原 source/key 识别来源 run，但不会因此补造来源完整性证明。来源台账只在单次检索内缓存，后续放弃操作会在下一次检索生效。

检索是支持英文词项和中文二元词的本地词法排序，含时间衰减；没有把它称为 embedding / GraphRAG。`memory export` 输出包含历史版本的 JSON，便于备份或迁移。

`memory search QUERY` 与 `memory search --key KEY` 必须且只能选一种，MCP `memory_search` 的 query/key 同样互斥。显式空 query 沿用全候选检索，空 key 拒绝；缺库也先检查参数，合法缺库查询返回空列表。key 按原字符串等值比较，大小写、首尾空格和 Unicode 规范形式不同均不相等。精确查询仍执行默认 active、有效期和作用域筛选，level 0/1/2 不变。普通查询另索引完整 key、knowledge/task 命名空间正文和合法来源路径；这些匹配也会影响派发自动召回，不把 `--source` 说明当作路径。

`memory history KEY` 按创建顺序列出该 key 的全部版本，保留替代链和当前来源核验状态；无 run 上下文，使用与 CLI read 相同的审计视角。未知 key 或缺库返回空列表。该入口不会激活旧版、刷新 TTL、写入或迁移旧库，也不会把历史结果接入自动召回；没有 MCP history 工具。

文件绑定只由 `memory add --source-file PATH` 触发，重复路径在词法规范化后去重。绝对路径、父目录跳转、反斜杠、NUL、保留目录及越界解析拒绝；大小写、空格和 Unicode 保留。固定一次 HEAD，按其中的精确路径读取普通 blob 并比较工作区原始字节，最多十个文件、每个 2 MiB；任一失败不替代旧 active 版本。符号链接不支持，换行转换、clean/smudge、LFS 等可能导致 Git clean 但原始字节不同，错误不会据此推断具体原因，也不运行用户过滤器。

应在拥有项目 `.harness` 的主检出操作；`--run` 只限定作用域，不选择读取位置。把 `--repo` 指向 run 工作树当前可能新建独立库，带 `--run` 通常找不到台账，不受支持；运行时没有自动归属拒绝或主库路由，也没有禁止普通 linked worktree。run 内检索使用自己的工作树核验，主检出后来的文件可能在旧 run 中 changed/unavailable，不自动更新分支或提交文件。

`source_status` 读取摘要按以下优先级给出，完整 stale_reasons 保留：

| 条件 | 状态 |
| --- | --- |
| 现有核验报告 invalid_provenance | invalid |
| 无文件哈希和 commit 绑定 | unbound |
| 任一 source_changed | changed |
| 任一 source_unavailable 或 commit_unavailable | unavailable |
| 未完成核验或未知原因 | not_checked |
| 已声明绑定全部匹配 | matching |

旧空人工来源仍为 unbound，保留 missing_provenance 和原 0.7 折减；无 repo 的未绑定记录不因 source_not_checked 变成有绑定。matching 不证明内容正确、来源依赖完整或提交已合入，也不消除 historical。排序先 preferred、后 needs_review，组内保留旧分数；变化、不可用、非法或未知原因进入后组，只有未绑定／未核验原因的记录不额外降组。所有状态与历史字段均为投影，不迁移 SQLite 或修改默认资格。

## 上下文与诊断产物

上下文压缩采用**结构化检查点 + 有界检索 + 原始证据引用**：在任务边界写 `handoff.json` / `handoff.md`，下一次指挥重新构建输入。不会将越来越长的对话摘要反复摘要。可选记忆按整条移除，必要约束完整保留。单个 CLI 内部的长任务压缩仍由 CLI 管理，编排器不操作模型私有 KV cache。

输入包每条验证命令的 stdout/stderr 摘录合计最多 500 字符，始终保留 argv、退出状态及完整日志路径。相同审阅问题去重，变化后的严重程度或证据仍保留；较早的说明可从审阅产物读取。当前任务计划与原始计划完全相同时，以 `tasks_ref: "original_tasks"` 引用同一份任务；再规划后以 `tasks_base_ref` 和 `tasks_delta` 传入新增任务、描述更新和新增依赖，保留完整原始义务。接受摘要引用各任务审阅产物。最终文件快照单独落盘，台账保留摘要和路径，接受门禁仍计算完整内容哈希。

大型审阅在派发包中使用有界摘录，审阅历史的文本共享 9,000 字符额度，所有问题 ID、严重程度、类别及全文路径仍保留。摘录明确要求采取修复或认定问题解决前读取完整产物；没有全文引用时不截断。旧审阅在删除重复 finding 或摘要之前保存完整内容摘要，即使裁剪后未超预算也保留，用于反馈去重并区分摘录相同但证据不同的审阅。接受与格式修正门禁始终检查完整结果。必要标识、历史元数据或任务义务仍可能超出总预算，此时继续明确停止。

`changed_files`（包括恢复记录内的同名字段）与 `instruction_file_changes.paths` 共享 4,000 字符内联额度。较大的清单改为 `{ "path_index": "SHA256" }`，通过包内的 `path_evidence_indexes` 定位完整 JSON 清单及其 `artifact_digest`、`count`。索引位于 run 的 `path-indexes/`，保留所有路径、顺序及非 UTF-8 文件名的 JSON 转义；相同清单复用产物，父级的来源 dispatch、错误、diff 和指令信任标记不变。模型可按需要分段读取和搜索，不必把整个索引再次复制到上下文。

派发台账中的 `path_evidence` 绑定这些索引。已有索引被改写、删除或替换为符号链接时会停止；保留现场并恢复原证据后再正常恢复，不手改摘要，也不丢弃已返回的审阅结果。首次索引写入失败尚未消耗模型派发；如果已保存 worker 和验证阶段，恢复继续审阅。目录中的同名路径不证明内容未变，接受门禁仍按完整文件快照核验。该机制不改变原目标、范围、权限、拒绝结论或累计预算。

首次执行、应用再规划前，以及已有环境更新或操作者答复的恢复中，预检整体审阅容量。确定的必要内容下界超限时保存计划并停止，允许增加 `context_chars` 后恢复；预计摘要和验证输出可能超限时提前警告。决定摘录会随答复修订或条数变化而缩减，因此仅计入预警估计；检查点按最终全部任务已接受的场景只提供索引，不把当前未接受任务的展开内容计入最终容量。预计容量不是保证，后续历史和实际输出仍可能使输入超限，所有正式派发继续执行完整输入的实际预算检查。

所有输入包与预算报告落在 dispatch 目录。默认 `context_chars = 100000` 是编排输入的字符上限，不是 token 数或固定输入长度。`context-report.json` 分别记录必要输入 `required_chars`、已加入的可选记忆及其分隔文本 `memory_chars`，两者之和为 `used_chars`；必要输入超限才触发硬门禁，可选记忆按剩余预算整条加入。

报告的 `memory_retrieval.counts` 区分 active_candidates、matched、scope_excluded、applicable、returned、limit_excluded、included、capacity_excluded。八条适用、六条返回、三条装入时，数量上限排除为二、容量排除为三；排第一的大记录可能被跳过而后续小记录进入，明细按实际 ID 标记。查询摘录最多 1,024 字符，附完整 SHA256；最多四十条明细、每条最多十个命中词、长文本最多 256 字符并标注截断，阶段计数仍完整。未执行的来源核验或评分为 null，不为报告重扫来源。报告随 context_prepared（或既有 context_compacted 别名）hook 传递，不放入提示词，也不解释 SQL 已排除的每条历史记录。

默认 `agent_timeout_seconds = 3600`，全局 `claude_max_turns = 50`。可选 `runtime.claude_max_turns_by_role = { worker = 200 }` 按 commander、worker、reviewer 设置正整数覆盖；Fable 指挥使用 commander，Codex 不使用此限制。仓库示例显式设置 worker 200，旧配置省略覆盖仍沿用原全局值。恢复允许添加、提高或移除覆盖，但各角色实际生效值不得减少；例如原 worker 200、全局 50 时，直接删除 worker 覆盖会拒绝恢复，将全局提高到至少 200 后才可删除。全局值自身仍不可降低，合法变更沿用配置变更审计，不清零累计预算。

Claude 的轮数上限针对单次派发的工具使用循环，一轮可以包含多个工具调用，不是任务累计工具次数；供应商报告的 `num_turns` 仅用于观测，不能直接与该上限比较。墙钟时限独立生效，持续输出或工具运行不会自动延长。轮数耗尽或超时 worker 均计入工作尝试，文件改动和原始错误保留；增加限额后正常恢复使用新进程和新 dispatch，继续核验原目标、范围、验证及独立审阅。当前不会自动延长、续接供应商会话或豁免预算。200 只是长任务起始值，需按到限率、最终验收和恢复重复工作校准；提高容量不保证任意任务完成。

Claude 非零退出时，诊断还会从有界的 CLI JSON 结果读取错误类型和最多三条短错误说明，例如 `error_max_turns`，同时保留退出码及原始日志。只读取 CLI 的 `subtype`／`errors`，不把模型正文当诊断；读取失败时仍保留原退出错误。诊断不增加重试、不换模型、不豁免预算，也不根据 `num_turns` 推断成功或失败。

模型调用结束时，`dispatch_finished` 事件和台账对应 dispatch 的 `provider_usage` 记录 CLI 实际返回的用量，可通过 `status RUN --full` 查看。Claude 包括其报告的美元成本、轮数及 token 字段；Codex 包括完成轮数及报告的 token 字段，不推算美元成本。来源是供应商输出，模型自行声明的费用不作计量；缺失、损坏、过大的输出或中断可能没有记录，不将未知用量当作零。报告成本不等于账号实际账单，观测不改变预算或接受条件，恢复已有审阅结果不重复累计。长时间进程每 60 秒输出一次仍在运行提示，配置的实际超时仍单独生效。

接受提交前运行时会重建暂存区并加入审阅过的路径；若这些步骤改变了暂存区摘要，并在 commit 前中断，恢复按候选变化作废旧审阅，即使文件内容和 HEAD 未变。会重新执行任务、验证和审阅，增加一次工作尝试；预算已用满时需增加后 resume。这一限制不通过自动 reset 或忽略暂存区核验消除。

若在 Git commit 与检查点写入之间被强制杀死，恢复因 HEAD 不一致拒绝自动重放；先核对包含 `Harness-Run` / `Harness-Task` 的提交。运行时不提供自动改写状态文件的恢复捷径。

## 独立真实工具检查

`python3 -m harness.permission_smoke --config FILE --repo PROJECT` 与 `python3 -m harness.tool_smoke --config FILE --repo PROJECT` 都使用配置中的真实 Claude worker 模型和账号额度；离线测试不能证明这些调用已执行或通过。

`permission_smoke` 在 `.harness/permission-smoke/` 创建专用测试仓库与工作树，保留报告和供应商原始输出；`--repo` 仅指定私有现场的存放位置。检查包含工作树内读写、精确命令、额外参数、工作树外读写及 `.git` 修改。通过标准要求实际工具调用、工具结果、对应的 `permission_denials` 和文件内容一致，模型自称“已拒绝”不算证据。工作树外读取按当前适配器授权应当成功。检查使用专用命令并关闭外部 MCP，不运行项目验证命令；默认超时 180 秒、最多 16 轮、预算参数为 1 美元，可用 `--timeout`、`--max-turns`、`--max-budget-usd` 调整。结果只覆盖这组测试与实际 CLI 版本，不证明完整的命令白名单或操作系统隔离。

只读文件工具与 broker 的工具接口、大小限制和 JSON 数值兼容规则见[设计说明](design.md#精确命令-broker-与只读文件证据版本-11)。
