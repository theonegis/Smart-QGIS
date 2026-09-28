# 可靠任务模式调用说明

本文描述开发中的工作树接口，整体验收尚未完成。缺项见[技术方案的实现进度](task-contracts-and-recovery.md)。发布的 `2.0` 标签不包含这些接口。

## 启动与实验基线

```bash
uv sync --locked
uv run smart-qgis
# 仅在论文旧接口消融实验中使用：
uv run smart-qgis --execution-mode legacy
```

默认且正式使用 `reliable`。`legacy` 只作为无任务日志的旧直接工具实验基线，不承诺兼容性或恢复能力。任务状态默认放在系统用户数据目录；`SMART_QGIS_STATE_DIR` 可指定独立目录。客户端重连时必须使用相同状态目录，通过任务 ID 恢复。一个任务同一时间只允许一个进程持有锁。

服务启动时默认清理最后事件超过 10 天的整个 `COMPLETED`/`CANCELLED` 任务目录，以及超过 60 天的 `BLOCKED` 任务目录，包括 SQLite、检查点和产物。`READY`、`PREPARING`、`RUNNING`、有未决尝试、有待答结构化问题或正被进程持锁的任务不会删除。`--retention-days` 和 `--failed-retention-days` 分别设置两个保留期；任一参数设为 `0` 可关闭对应清理。清理以整个任务为单位，不裁剪表内审计链。

## Agent 调用顺序

1. `task_start`：提交原始目标、输入逻辑 ID 与绝对路径、交付物，以及用户明确提出的附加契约。输入 `kind` 可省略；服务端在建立契约前检查全部输入并确定实际类型。
2. 标准地图、地图导出或可编辑工程的 `task_start` 响应会返回准确的 `next_call`。只复制其中唯一的 opaque token；不再选择工作流工具、复制任务 ID 或重填参数。
3. `task_execute_next`：服务端执行已绑定的标准工作流并自动完成最终验收。加载、样式、布局、导出和工程保存仍分别建立契约、执行基本前后检查并写入检查点。
4. 通用数据处理先确定精确算法 ID，再调用 `prepare_algorithm`。服务端读取当前 QGIS 注册表参数，机械绑定输入与受控输出；无默认值且不能唯一确定的必选参数会成为带 ID、类型和候选值的持久化问题，再用 `task_answer` 记录真实回答。
5. `task_diagnose` 只用于重连、响应丢失或明确诊断。标准成功路径不调用状态、算法帮助或契约帮助。

默认可靠模式公开 `algorithm_info`、`task_start`、`task_execute_next`、`task_answer`、`task_record_guidance`、`task_invalidate`、`task_recover`、`task_diagnose`、`prepare_algorithm` 九个工具。`algorithm_info` 只用于必要的算法 ID 发现；MCP 进程重启后先用 `task_recover(task_id=...)` 重新绑定原任务。`prepare_algorithm` 统一替代开放参数网关，并可用 `repairs_step` 重交失败步骤。已提交结果若被证实错误或不可用，先用 `task_invalidate` 失效该生产步骤及其下游，再以 `repairs_step` 重交并复用原输出 ID；未提交的失败步骤无需回退。纠错拒绝次数达到配置上限后，必须先询问用户并通过 `task_record_guidance` 记录其真实指示，才可继续原任务。响应丢失时用相同 `task_execute_next` token 重试；服务端返回持久化的相同结果，不会重复执行 GIS 步骤。

修复时 `repairs_step` 指向旧步骤，但 `step_id` 必须是新 ID。通用处理产生的地图若在自动样式/布局/导出阶段中断，`task_recover` 会给出新的短 `task_execute_next.next_call`；继续同一任务即可，不应把状态响应里的长版本令牌误传给 `task_execute_next`。

连续参数拒绝或同一失败链的语义修复上限默认是 3；在 MCP 启动命令中加入 `--correction-limit 5` 等正整数即可自定义。达到上限后由用户提供真实指示，`task_record_guidance` 开启新的纠错窗口；历史尝试和必需验收要求保持不变。

QGIS 工具调用超时默认为 900 秒，可用启动参数 `--timeout` 自定义。超时后不会自动重放该调用；任务状态标记需要用户干预。`task_diagnose` 是只读诊断入口；`task_recover` 会核对与协调中断尝试，并非诊断工具。模型须先展示状态，收到用户关于重试、修复或停止的真实指示并用 `task_record_guidance` 或 `task_answer` 记录后，才可恢复与执行。LLM 思考超时不发生在 MCP 工具内，须由宿主客户端设置计时器；论文实验宿主默认 300 秒，同样暂停等待用户提示，不自动重启模型回合。

契约由宿主 Agent 生成，不要求用户填写。不能自动判断的必需条件返回“未验证”，不会因文件存在而自动通过。

`step_prepare` 当前覆盖加载、栅格/矢量裁剪、矢量重投影、栅格/矢量样式、标准地图布局、地图导出和工程保存。它按已检查资产推断数据类型，机械绑定输入与受控输出，并为标准布局启用图名、图例、比例尺和坐标标注。没有 recipe 时才调用 `algorithm_info(action="help", algorithm="<算法 ID>")`，按当前安装版本返回的参数名、类型、枚举值和默认值构造高级步骤，不能凭记忆猜测。

`workflow_run(workflow="standard_map_project")` 默认使用全部矢量/栅格任务输入（矢量在栅格上方），产生任务中声明的 layout、PNG/PDF 和 project 交付物。面状边界采用透明填充，避免遮住下层栅格；点线使用可见的标准符号。任一步失败即停止，已提交步骤和检查点保留；必选选择不明确时返回 `TASK_AMBIGUOUS/ask_user`，不会用猜测值继续。

算法帮助中的 `required` 和 `has_default` 是必选参数判断依据。仅当参数 `required=true`、`has_default=false`，且用户目标和已检查数据不能唯一确定其值时，宿主才直接向用户提出一个简短问题并暂停步骤准备。输入图层和受控输出引用由服务端或宿主机械绑定；相同算法、环境未变化时复用已查询帮助。

通用处理顺序为 `algorithm_info(action="list")`（仅在不知道精确 ID 时）→ LLM 根据目标选择算法 → `prepare_algorithm`。后者内部完成参数帮助查询、规范化、必选项解析、原生 QGIS preflight 和步骤契约提交；不再由 LLM 编写处理步骤契约。列表来源是当前 QGIS Processing 注册表，而非硬编码算法名清单。

`prepare_algorithm` 在持久化步骤契约之前，以当前安装版本的算法帮助为准，机械纠正无歧义的参数名大小写、数值/布尔 JSON 表示以及枚举标签；随后运行 QGIS 原生 preflight。它不会根据相似名称猜参数，也不会选择 CRS、字段、表达式、波段或科学方法。无法唯一确定的必选语义值仍保存为结构化问题，由用户回答。

可靠模式的公开工具表不包含底层修改工具或细粒度生命周期工具。`project`、`load_data`、`run_processing`、样式、布局、导出、契约提交及 `task_execute` 都是服务端内部执行目标；通用算法统一由 `prepare_algorithm` 准备。旧直接工具仅在论文消融实验的 `--execution-mode legacy` 下公开。

服务端在提交处理步骤时也会查询 Worker 的算法帮助并检查未知参数名；这不等于服务端已证明宿主阅读过帮助，也不保证所有参数组合在执行时都有效。现有算法执行前检查继续保留。

### 最低验收提示策略

服务端自动基础检查只包括：执行前核对输入版本、逻辑引用和原生 QGIS 参数；执行后核对产物存在、声明格式可打开，以及矢量/栅格 CRS 有效。制图任务还默认要求图名、图例、比例尺和已启用标注的坐标格网，格网可使用地理或投影坐标。用户明确要求去掉某项时，才把 `title`、`legend`、`scalebar` 或 `coordinates` 写入 `task_contract.map_omissions`；静默未提及不代表允许删除。

Agent 只生成用户明确附加要求对应的检查。禁止额外添加验证规则、质量目标或更严格阈值，也不以“可选检查”扩充任务。未指定的实现细节采用算法文档默认值；必要选择记入 `assumptions`，不得冒充 `user_requirement`。例如，用户只要求 DEM 裁剪时，不得自行增加最低海拔值、边缘像元规则、NoData 编码或额外精度指标。用户明确要求保留源网格或高程值时，才加入相应检查。

同一提示通过 MCP 初始化说明、`task_begin` / `contract_help` 返回内容、契约提交工具说明及内置地图提示传递；Codex、Hermes 案例脚本共享此策略，不依赖客户端扩展。上述自动基础检查由系统注入，不要求 Agent 重复编写。掩膜像元规则、网格/像元值一致、NoData、几何有效性、精度、来源追踪、图层顺序和渲染一致性等不再由算法选择自动升级为验收条件；只有用户明确要求时才使用对应验证器。

工具参数的跨字段错误返回稳定规则标识与固定修正说明，例如 `contract_exclusive`（只提交一种契约）、`repair_inheritance`（继承检查需指定修复对象）、`retry_context`（重试需版本和非空原因）。错误说明使用服务端白名单文本，不回显输入值或任意异常文本。

`output:<id>` 中的 ID 必须匹配该步骤 `outputs[].id`，不是 `binding` 字段；例如输出声明为 `id=project_qgis, binding=project`，保存路径应写 `output:project_qgis`。不匹配会在契约提交阶段返回 `UNKNOWN_OUTPUT`，不创建执行尝试、不消耗状态版本。

这是生成阶段的强制提示，不是服务端已经能判断自然语言要求是否被模型曲解的保证。现有 `required` 默认值、验收执行及已锁定契约保持原语义；历史任务不会因新提示自动降低标准。新策略对真实模型额外规则生成率的影响仍需独立验证。

## 逻辑引用与输出绑定

- 输入和已提交产物使用 `asset:<id>`；修改操作的图层引用须使用逻辑 ID，并在步骤 `inputs` 中声明。
- 文件输出参数使用 `output:<id>`，服务器为每次尝试创建独立目录，不覆盖用户输入或其他尝试。
- `load_data`、`vector_data.create`、`add_basemap` 声明一个 `binding: "layer"` 输出。
- `layout.create` 声明一个 `kind: "layout"`、`binding: "layout"` 输出。
- `run_processing` 的输出 `binding` 对应算法参数，例如 `OUTPUT`；其他文件导出使用 `binding: "path"`。
- 输出的 `filename` 可省略，默认按逻辑 ID 和类型生成；指定时只能使用文件名，不能包含目录。
- 同一键重发完全相同的已提交请求会返回原结果；修改参数后必须使用新的步骤和幂等键。

例如，地图导出步骤的 `arguments` 可为：

```json
{"layout": "asset:figure", "path": "output:map"}
```

其 `inputs` 为 `["figure"]`，`outputs` 为：

```json
[{"id": "map", "kind": "pdf", "binding": "path"}]
```

在契约提交及步骤调用后，以响应中的 `continuation_token` 为准。遇到冲突先调用 `task_diagnose`，不要构造 token 或猜测内部版本。

## 失败与恢复

`task_diagnose` 返回步骤、尝试、失败记录和检查点。服务重启后用 `task_recover` 附着任务；恢复时重新核对输入、产物指纹和运行环境。明确取消的任务需传 `resume_cancelled: true` 才能恢复。

`ENVIRONMENT_CHANGED` 列出不一致的环境组件及记录值/当前值。可以恢复原运行环境后重试；也可保留原任务，在当前环境创建新任务、重新批准契约并重新计算。当前没有原任务检查点的环境迁移接口，不应手改数据库或快照中的版本字段来绕过核对。重新计算属于新任务，不计作原任务恢复成功。

如果未提交尝试尚待核对而输入也发生变化，普通 `task_recover` 不能把旧输入的结果当作新输入结果。先检查新数据指纹，再调用 `task_revise_inputs`，显式设置 `discard_uncommitted: true`。这会隔离全部未提交尝试（包括已准备文件但未提交的尝试），保留旧目录与记录，不将它们补交为成功；接纳新输入、失效受影响步骤和更新恢复检查点在同一 SQLite 事务中完成。无关的已提交结果保留。未设置该参数时请求被拒绝且状态不变；事务失败时输入版本与未提交状态一起回滚。

被隔离但不依赖变化输入的步骤标为 FAILED，故障码 INPUT_REVISION_INTERRUPTED；其契约仍为当前版本时，可通过 `task_recover(retry_step=..., continuation_token=..., reason=...)` 显式重新授权，并直接调用返回的 `next_call`。仍适用每步骤最多两次技术重试授权。依赖变化输入的步骤为 INVALIDATED，必须提交修正步骤，不能使用此入口重新授权旧契约。

QGIS Processing 参数预检失败返回 INVALID_PARAMETERS；Worker 正常返回的其他执行异常为 OPERATION_FAILED。这两类需要检查参数或方法，不能按 WORKER_UNAVAILABLE 重新授权原参数。真正的 Worker 退出、超时或通信中断仍属于技术恢复。旧失败记录的原错误码保持不变，不追溯改写历史。

Worker 非超时性丢失响应时，受控步骤至多重放一次；输出使用新的尝试子目录。超时不自动重放，等待用户干预。已准备但未提交的产物在重启后重新验证，通过后补齐提交，不直接把文件存在视为成功。

语义失败时由 Agent 提交新步骤，通过 `repairs_step` 指向失败步骤。已有用户要求和必需检查不能被静默放宽。恢复不保存或重新创建宿主对话，也不会唤醒已关闭的客户端。

`task_invalidate` 用于回退当前已提交的错误结果。如果步骤已经 FAILED 或 INVALIDATED，不必重复回退；用 `contract_get` 查看契约后直接提交包含 `repairs_step` 的修正步骤。错误响应的 evidence.steps 会列出被拒绝步骤的状态和下一步建议。拒绝重复回退不会新增执行尝试或改变工程与状态版本。

修正步骤必须把原必需检查原样保留在原来的 preconditions 或 postconditions 中，不能通过更换阶段绕过执行前检查。`contract_get` 返回的 `system_` 检查来自服务端，重新提交时省略，由服务端按新操作重新生成；不能省略其他必需检查。CONTRACT_WEAKENING 会列出不匹配检查的 ID、阶段及原步骤，便于读取后修正。

为避免重复抄写，可在 `step_contract_submit` 顶层显式传 `inherit_required_checks: true`，并在 `contract.repairs_step` 指定失败或失效步骤。服务端仅补入遗漏的必需非系统检查，保持全部字段和原阶段；已经提交的同 ID 检查不会被覆盖，改弱或换阶段仍拒绝。继承后的完整契约会重新校验并持久保存，可用 `contract_get` 查看。

系统基础检查注入后会再次校验完整步骤。用户附加检查若与执行参数明显矛盾，应修正执行方案，不得删除已锁定的用户要求。算法参数本身不会自动派生 NoData、像元值或掩膜边缘验收规则。

当前可靠模式只开放具有受控输出适配的 Processing 算法，未适配算法返回 `UNSUPPORTED_SIDE_EFFECT`。兼容模式仍可使用完整已安装算法注册表，但不具有恢复保证。

## 可选追踪

默认不向 LangSmith 发送事件。显式设置 `SMART_QGIS_LANGSMITH_TRACING=true` 后，按 LangSmith SDK 的凭据配置发送白名单事件；可用 `SMART_QGIS_LANGSMITH_PROJECT` 指定项目。

事件仅包含固定操作名、生成的任务 UUID、成功标记及耗时，不上传原始目标、工具参数、坐标、文件路径或产物。网络失败不影响任务提交。MCP 入口禁止环境变量隐式启用 LangChain 全量参数追踪；LangSmith 不作为恢复数据源。

## 本地模型的按需契约帮助

调用 `contract_help` 获取验证器目录；通过 `contract_help(kind="raster_mask")` 等请求取得选定检查类型的完整参数。任务有显式附加要求时才读取 `structure="task"`；仅当没有 workflow/recipe 时读取 `structure="step"`。`workflow_run` 和 `step_prepare` 均进入同一套类型、引用、前检、基本后检和恢复流程，不绕过契约门槛。

状态响应默认返回检查点摘要。诊断时用 `task_diagnose(include_details=true)` 获取完整指纹和故障证据；服务端存储的完整记录不因响应精简而改变。

## 契约修订后的结果复用

最终验收拒绝已提交产物时，调用 `task_invalidate(task_id=..., continuation_token=..., steps=["producer_step"], reason=...)`。它使所选生产步骤及依赖闭包失效，回退工程到受影响操作之前的已验证状态，并保留不受影响的持久数据。旧文件和尝试记录保留；新生产步骤通过 `repairs_step` 指向原步骤，可重新使用原交付物逻辑 ID。

回退点按受影响的工程操作选择：文件计算分支若没有修改工程，不回退独立的图层或布局；有效的独立文件结果即使晚于失败步骤产生，也保留复用。较早开发版本留下的依赖记录若缺少工程操作关系，返回 `REPAIR_SCOPE_INCOMPLETE` 和需显式纳入范围的步骤，不静默丢弃仍标为有效的工程状态。

修订任务契约会使旧步骤失效。对具有已提交执行记录和持久输出的步骤，可调用 `task_validate(revalidate_steps=["step_id"], continuation_token=...)`，按依赖顺序重新验证旧结果。服务端检查输入及产物指纹、上游状态、原步骤后置条件、输出有效性和当前任务契约中针对该输出的条件；全部通过才重新开放资产。

没有输出文件的纯状态步骤也可使用该接口，但其原工程文件、补充状态及指纹必须仍是当前检查点；重新检查步骤后置条件和当前契约中针对其输入资产的条件后才恢复有效状态。如果后续操作已替换检查点，返回 RESULT_NOT_REVALIDATABLE，要求新步骤建立并验证当前需要的状态，不能仅凭历史成功记录推断状态仍然成立。

某步骤失败时已通过的上游仍可复用；失败结果继续不可用。没有持久输出的纯工程状态操作需提交新步骤，不能仅凭历史成功记录自动恢复有效性。最终任务仍必须通过完整验收。此接口不接受外部输入的新版本；输入变化后的版本接纳和局部重算仍待补齐。


### 故障阶段与恢复上下文

结构化故障包含 `code`、`phase`、`retryable`、`evidence` 和 `next_action`。关联当前任务时，额外返回最新 `state_version` 与检查点摘要；摘要只表示已提交的检查点引用，恢复使用前仍须重新核对文件指纹与环境。执行阶段可区分 `preconditions`、`execution`、`checkpoint`、`validation`、`persistence` 与 `commit`，失败尝试持久保存该阶段。旧记录缺少阶段时，状态摘要显示 `unknown`，不反推历史阶段。

Processing 步骤提交时即按当前 QGIS 注册表核对参数名称。未知键返回 INVALID_PARAMETERS，并给出 unknown_parameters 与 allowed_parameters；不保存该步骤、不启动执行，也不自动修正拼写。参数值的 QGIS 检查仍可能在执行前预检阶段拒绝，不能把名称有效等同于全部参数语义有效。

文件系统或 Worker 不可用也返回结构化错误。`retryable=false` 表示宿主不应无条件重复同一调用：先处理外部条件、核对任务，再决定恢复或提交修订契约。验证失败不会自动放宽契约。检查点摘要不包含完整文件指纹清单，避免本地模型反复接收冗长恢复元数据。


### 可选追踪关联

只有显式设置 `SMART_QGIS_LANGSMITH_TRACING=true` 才启用远端追踪；单独设置 LangChain 的全局追踪变量不会启用。阶段事件关联服务端生成的任务 UUID、尝试 UUID，以及根据任务 UUID 和步骤名派生的 `step_key`。原始步骤名、工具参数、目标文本、路径、产物和验证证据不进入远端字段；`step_key` 用于同任务内关联，并非对可能被猜测的步骤名提供密码学保密保证。

追踪覆盖执行尝试的前置检查、执行、检查点、验证、持久化、提交和失败后的恢复耗时，工具级事件补充契约提交与重连等操作。远端队列有容量上限，失败会丢弃追踪；SQLite 才是审计和恢复的权威记录。合成工程测试已验证追踪持续连接失败时仍能创建任务、提交产物、关闭并恢复 Worker、导出 PDF 和完成任务。尚未使用真实远端账户做连通性验收。


### VRT 与复合输入

可靠模式对本地 VRT 的 `SourceFilename`、`SourceDataset` 和 `SrcDataSource` 引用递归计算完整文件指纹，包括所引用 TIFF 的外部掩膜等辅助文件。相对路径必须明确声明 `relativeToVRT=1`；循环、超过 32 层的引用、网络/VSI 引用和嵌入 Python 像元代码不会进入可靠执行通道。未适配的连接字符串不能被当作已完整记录的本地源；需要先转换为受支持的持久本地文件。此限制不改变 legacy 模式。

仅变更引用文件、保留 VRT 本文及文件时间不变，仍会改变输入版本；恢复要求核对一致。新增或删除辅助文件也属于版本变化。检测到变化后会阻止继续；可按下述显式输入修订流程接纳新版本。


### 最终检查引用中间产物

任务契约可以通过 `intermediates` 声明最终验收依赖的中间逻辑产物，例如 `{"map_layout": "layout"}`。这允许最终 PDF/PNG 的契约引用布局来验证图层关联和图例，无需把布局额外列为用户交付文件。声明仅确定逻辑 ID 与类型，不预先确定算法路径；具体生成操作仍由后续步骤契约增量决定。

这些 ID 不得覆盖输入或交付物，后续步骤的输出类型必须一致，契约修订不得删除或改变已有声明。声明不会生成产物，也不会使检查自动通过；必需检查的中间产物缺失时仍阻止任务完成。未声明资产继续返回 UNKNOWN_ASSET，并提示补充声明或纠正引用。


### 跨坐标系的边界模型

可靠模式的 `gdal:cliprasterbymasklayer` 支持通过精确参数 `EXTRA="-wo CUTLINE_ALL_TOUCHED=TRUE"` 选择相交像元规则，或 `EXTRA="-wo CUTLINE_ALL_TOUCHED=FALSE"` 显式选择像元中心规则；省略时为像元中心规则。若用户明确要求掩膜边缘规则，可在契约中添加同一规则的 `raster_mask` 检查；服务端不会仅因选择裁剪算法就自动增加该严格验收。仅这两个完整字符串经过适配，不能拼接其他选项、文件路径或命令；其他算法仍拒绝非空 EXTRA。已锁定契约要求 all_touched 时，应修正执行参数，不能静默改弱契约。GDAL 对该选项的定义见[官方 WarpOptions 文档](https://gdal.org/en/stable/doxygen/structGDALWarpOptions.html)。

`raster_mask.geometry_model` 明确边界表达：

- `transformed_vertices`：将边界顶点转换至栅格 CRS 后按 GDAL 规则栅格化，支持 `pixel_center` 和 `all_touched`。为兼容既有契约，这是默认值；它不是“原始边界 CRS 中精确包含”的同义词。
- `original_crs`：将像元中心转换至原始边界 CRS 后做严格内部包含判断。仅支持 `pixel_center`，恰在边界上的点视为外部；QGIS Python 必须包含 Shapely 2 和 PyProj，缺少依赖会明确验证失败，不回退至另一模型。

Agent 应根据用户要求和算法语义选择并记录模型、来源及方法依据。必需检查锁定后不得切换模型来让失败结果通过。验证报告记录模型及相关库版本；数值容差和边缘约定仍独立规定，不使用通用半像元容差。

检查点同时记录 Worker 的 NumPy、Shapely、PyProj 版本（无法导入时为空），与 QGIS、GDAL、Python、Qt 和处理提供者一起核对。版本变化或旧检查点缺少依赖记录时拒绝自动恢复，返回 ENVIRONMENT_CHANGED；不静默升级旧检查点。可使用原服务端快照和原环境恢复，或在当前环境创建新任务、批准新契约并重新计算。新任务重算不能统计为原任务恢复成功。

独立全量参考脚本支持 `--geometry-model original_crs`（默认）和 `--geometry-model transformed_vertices`。选择必须与要检验的方法假设一致；并列比较两种模型是敏感性分析，不可删除未通过报告或声称两种语义都通过。

对显式相交像元契约，独立参考还支持 `--geometry-model transformed_vertices --boundary-rule all_touched`。此模式使用 Shapely 计算像元与边界的正面积相交，不复用服务端 GDAL 栅格化；仅支持北向网格，零面积边/点接触视为外部，报告明确该约定可能与 GDAL 退化边缘行为不同。默认仍为严格像元中心内部规则，不根据失败结果自动切换。全量正确性计数与抽样辅助距离诊断分开报告。


### 基础设施恢复后重试原步骤

文件系统不可用、Worker 不可用、未提交进程中断或明确取消造成失败时，可在处理原因后调用 `task_recover`，提供 `retry_step`、最新 `continuation_token` 和非空 `reason`。取消任务仍须同时明确 `resume_cancelled=true`。服务端核对输入、环境与检查点后返回新的 `next_call`。

旧尝试及旧幂等键不改写为成功。参数/验证失败不能走此入口，契约已过期也会拒绝。每个步骤最多授权两次显式基础设施重试，次数记录在 SQLite 中；这与一次自动 Worker 重放、两次临时网络重试、三次语义修复预算分别计算，不因重连清零。说明外部条件已经改变由宿主负责，服务端不根据一句说明保证剩余磁盘容量足以完成整个算法。


### 同任务接纳更新后的本地输入

1. `inspect_data(source="asset:<输入ID>", include_fingerprint=true)` 检查当前文件及其内容指纹。
2. 读取最新 `task_diagnose`，调用 `task_revise_inputs`，提交 `task_id`、`continuation_token`、`expected_digests`（输入 ID 到新 digest 的映射）及修订原因。
3. 服务端要求明确确认全部发生变化的输入，拒绝过时指纹、未知 ID 或未变化的虚假修订；此接口不改变路径、类型或用户目标。
4. 恢复不依赖旧输入的安全工程检查点，保留独立持久结果；将新输入版本、受影响步骤失效状态和新检查点一起事务提交，保留旧版本指纹及事件。
5. 为被失效的生产步骤提交新步骤契约，使用 `repairs_step` 保留原必需检查；输入版本已改变时允许原算法参数重新执行。依赖闭包中的下游需重新生成，独立结果不自动重算。

输入修订仍要求先核对未决执行尝试，不支持修改过程中并发写入源数据。它不降低任务验收标准，也不将旧输入产生的结果重新标为有效。语义修复预算按相关输入修订后的阶段计算，已有约束继续保留。

## 布局内容的显式结构检查

制图步骤会自动注入基础 `layout_content` 检查，要求图名、图例、比例尺和坐标标注；明确列入 `map_omissions` 的元素除外。`layout.grid_crs` 可指定地理或投影 CRS，省略时使用 EPSG:4326。用户要求精确标题文本或指定格网 CRS 时，Agent 可再加入显式 `layout_content` 检查。例如：

```json
{
  "id": "map_content",
  "kind": "layout_content",
  "target": "map_layout",
  "source": "user_requirement",
  "basis": "User requested this title, a scale bar and WGS84 coordinate annotations",
  "evidence": ["goal"],
  "texts": ["Example elevation map"],
  "require_scalebar": true,
  "grid_crs": "EPSG:4326"
}
```

`map_item` 默认 `main-map`，可指定工程中的其他地图对象 ID。检查可导出的图名与可见标签、关联主地图的图例和比例尺，以及启用标注、间隔为正的坐标格网。图名、图例、比例尺、格网或精确文本至少提供一类，不能提交空检查。

这是布局对象结构检查，不证明字体渲染、文字遮挡、比例尺实际校准或地图美观。视觉与科学解释仍需独立检查；必需外部审查未完成时仍不能宣称全部验收通过。服务端会生成非空默认图名和地理坐标标注，但不猜测用户未提供的精确标题文本或特定投影 CRS。

## 不依赖旧对话的契约读取

重新连接后先调用 `task_recover(task_id=...)`，再用只读 `contract_get(task_id=...)` 读取当前锁定任务契约。需要原步骤参数时传入 `step_id`，一次只读取一个步骤，避免向小上下文模型重复发送全部历史。

返回包含 `contract`、步骤持久状态和当前 `continuation_token`。内部状态版本与契约版本不再暴露给模型；失效步骤的读取也不代表重新授权。不存在的步骤返回 UNKNOWN_STEP，未提交任务契约返回 CONTRACT_REQUIRED。

## 执行已批准步骤，避免重复参数

`step_contract_submit` 成功后，直接调用响应中的：

```json
{
  "task_id": "TASK_ID",
  "step_id": "APPROVED_STEP_ID",
  "continuation_token": "OPAQUE_TOKEN"
}
```

该接口从持久契约读取原操作和全部参数，不接收或猜测新算法参数；需要改参数时先提交修复契约。token 同时承载下一动作授权和服务端内部幂等身份；原调用重试可取回已提交结果，过时或用于错误步骤的 token 会被拒绝。

这是所有 MCP 客户端都可使用的可选入口，不要求客户端增加功能或改用特定模型。减少参数重述是接口性质；模型错误率和端到端耗时收益仍需客户端实测。

GDAL 裁剪若确实需要 NaN 空值编码，可在算法参数中传入 JSON 字符串 `"nan"`，不要发送非法 JSON 数值 NaN。当前 QGIS 的掩膜裁剪合成测试已验证此表示。NaN、有限哨兵值或 Alpha 应由数据和任务约束决定，不能把具体编码选择无依据地标成用户要求。

### 完成后的显式重开与依赖恢复

独立审查在 task_finish 后发现问题时，COMPLETED 任务也可通过带当前版本和原因的 task_invalidate 显式重开。它不会自动放宽契约；交付重新验证通过前不能再次完成。失效闭包中的每个步骤都可以通过 repairs_step 指向自己的旧步骤重建，不限于初始回退根。成功后失效的操作可以保留原参数重算；真实失败后无变化重试仍被拒绝。所有修复继续继承必需检查并共享原修复预算。

## 前检、后检与停止纠错

正常处理分为两道检查：执行前核对实际输入、算法帮助和参数；执行后核对基本可用性及用户明确要求。`run_processing` 的步骤审批会调用 QGIS 原生参数检查，参数不合法时不创建执行尝试。输出路径在预检中使用临时目标占位，因此预检通过不保证后续磁盘、网络或外部输入始终可用。

任务或步骤的 `unresolved_questions` 是服务端安全兜底：非空契约不会获批。宿主发现实质未决项时应直接向用户提出具体问题，并在得到答复前不调用契约提交工具，避免用一次必然拒绝来“登记问题”并浪费纠错预算。可从数据和文档确定的参数不反复询问；可选参数、有文档默认值的参数以及 JSON 字段名等工具使用问题不得推给用户解决。

任务创建、契约提交以及修改工具的无效参数/授权/修复调用共用纠错预算；连续三次被拒绝后，修改调用停止并返回 `CLARIFICATION_REQUIRED`。宿主解释阻塞原因，收到用户实际答复后调用 `task_record_guidance(question=..., user_response=..., task_id=..., continuation_token=...)`；任务尚未建立时省略任务 ID 和 token。不得由模型自行编造用户答复。

已建立任务的拒绝计数随 SQLite 保存，重连不清零；创建任务前的计数限于当前服务进程。服务端能拒绝修改，不能强制终止客户端的内部推理或只读查询，也不能认证宿主传来的答复是否确由用户发出。

成功提交契约或完成新的执行后重开纠错窗口；读取幂等缓存、查询帮助或查看状态不重开窗口。网络及 Worker 故障继续按技术恢复预算处理，不混入参数纠错次数。
