# 2.0 开发说明

本文描述开发中的 2.0 任务接口，以及仅供论文消融实验使用的 `legacy` 旧直接工具基线。默认接口已接入任务契约、执行门槛与检查点协调器；完整验收状态见[任务契约与故障恢复技术方案](task-contracts-and-recovery.md)，调用方法见[可靠任务使用说明](reliable-usage.md)。

候选产物检出实验可运行 `uv run python scripts/validation_benchmark.py --output artifacts/validation-run`（必须使用新目录）。它自动调用 QGIS Python，生成合成正反例，并报告全量/抽样的 TP、FN、FP、TN 及未验证数。报告只支持所列夹具；重复种子不是新增独立候选，详见论文素材。

## 架构

```text
Codex / Hermes / 其他 MCP 客户端
                │ stdio MCP
          server.py（官方 MCP SDK）
                │ 校验参数并调用
          tools.py（LangChain StructuredTool + Pydantic）
                │ 异步串行调用
          bridge.py（子进程生命周期、超时、错误）
                │ 私有 JSON-lines 管道
          worker.py / extra_ops.py（QGIS 自带 Python）
                │ 主线程、QT_QPA_PLATFORM=offscreen
       QgsApplication + QgsProject + Processing + Layout
```

所有 QGIS 对象只在 worker 主线程操作。MCP 与 QGIS 使用不同的 Python 环境，不把 Qt/GDAL 二进制依赖混进 uv 环境。Qt 作为 QGIS 的底层依赖仍然存在，但没有桌面窗口、聊天面板、`iface`、QGIS 插件注册或 TCP Socket 转发。

LangChain 用于工具定义、参数验证和执行接口，不创建模型、不发起推理、不读取 Agent 凭据。客户端 harness 是唯一的推理编排层。

## 模块与工具

| 模块/工具 | 职责 |
|---|---|
| `runtime.py` | 定位 QGIS Python、PROJ/GDAL 和 Processing 插件路径 |
| `server.py` | MCP 工具列表、调用、资源、提示模板 |
| `bridge.py` | 每服务一个 worker；串行请求；取消/超时终止进程组 |
| `project_manage` | info/create/open/save；保存工程和布局 |
| `load_data`, `add_basemap` | 本地/提供者数据源；XYZ/WMS/WMTS |
| `layer_manage`, `feature_info` | 显隐、排序、名称、删除、属性和几何抽样 |
| `style_vector`, `style_vector_graduated` | 单一、分类、分级符号；标签 |
| `style_raster`, `render_raster` | 伪彩色、灰度、RGB、山体阴影 |
| `qml_style_manage` | 加载/保存 QML |
| `vector_data_manage` | 选择、统计、内存数据、矢量导出与 CRS 转换 |
| `algorithm_info`, `processing_execute` | 查询完整算法注册表与参数，执行处理并加载结果 |
| `layout_manage`, `export_map` | 布局、QPT、地图图片/PDF |

使用 `algorithm_info` 的分页、关键字搜索来发现算法；提供者由安装的 QGIS 决定，不能假设每台机器都包含 GRASS/PDAL。不是只支持示例中列出的几个算法。未识别的算法 ID/参数会被拒绝。没有开放任意 Python `eval/exec`；1.0 中的任意代码执行由通用 Processing 和明确工具接口替代。

算法帮助对 QGIS 明确声明为整数索引的枚举增加 `choices`（value/label）与 `value_format`，值从当前注册表动态生成。Agent 应提交 value，不把标签当成整数，也不硬编码某台机器上的索引。其他参数继续保留原始 definition；服务端不猜测枚举含义或自动替换提交值。

## 运行环境

macOS 自动发现 `/Applications/QGIS.app/Contents/MacOS/python`。自定义安装可使用：

- `SMART_QGIS_APP`：macOS QGIS.app 路径。
- `SMART_QGIS_PYTHON`：能够 `import qgis.core` 的 Python 可执行文件。
- `QGIS_PREFIX_PATH`：QGIS 安装前缀。
- `SMART_QGIS_PLUGIN_PATH`：包含 `processing` 的 QGIS Python plugins 目录。
- `PROJ_DATA` / `GDAL_DATA`：必要时指定 QGIS 自带资源目录。

Linux 例如使用 `/usr/bin/python3`、前缀 `/usr`、plugins 路径 `/usr/share/qgis/python/plugins`。先通过系统包管理器安装 QGIS Python 绑定。macOS 4.2.2 是本次实测环境；Linux 与 QGIS 3.44 需在各自机器验证，不宣称已跨平台测试。当前进程组管理面向 POSIX，Windows 尚未验收。

MCP SDK 明确约束 `>=1.28,<2`，采用官方 v1 维护线的 stdio 接口，`uv.lock` 固定实际依赖版本，避免客户端生态升级时隐式迁移到 SDK v2。Smart-QGIS **产品版本 2.0** 与 MCP SDK 主版本无关。

## 数据与状态语义

- `project.create/open` 替换当前状态；先保存需要保留的工程。
- 工程保存不复制输入文件；相对路径由 QGIS 管理，迁移工程时须一并迁移数据。
- 内存图层不具有跨进程持久性，保存前要求导出为文件并重新加载。
- `TEMPORARY_OUTPUT` 仅在本服务会话存活期间有效，需永久保存时指定明确输出路径。
- 图层引用优先用 ID；重复名称会报错。排序和布局图层列表从上到下。
- 布局采用指定 CRS，对数据范围做坐标转换；自动范围排除全球底图，可用 `extent_layer` 或显式 extent。
- DEM 色带使用有效像素统计，NoData 保持透明。坡度/山体阴影需要处理水平与垂直单位；示例先投影到米制 CRS 再算坡度。
- 操作串行但不是事务。处理工具可能留下部分文件；超时/取消会终止 worker，之后要求重启 MCP 并重开已保存工程，避免悄悄丢失状态后继续执行。
- 通过 stdout 的原生库输出重定向到 stderr，防止污染 MCP 和内部管道。
- 工作进程使用临时配置；GDAL PAM 禁用，避免向输入目录写 `.aux.xml`。

## 扩展与验证

在 `tools.py` 添加 Pydantic 参数模型与 `SPECS`，在 worker 中添加对应操作。所有 MCP 入口经同一 LangChain 参数模型校验。返回 JSON 可序列化结果，失败抛出异常，由 MCP SDK 转换为错误响应。

```bash
uv sync --locked
uv run ruff check src scripts tests
uv run pytest -q
```

单元测试验证参数契约与独立工具分发；集成测试实际启动 QGIS/MCP，验证矢量选择/导出、分类样式、缓冲区、栅格渲染、坡度、布局持久化和导出。案例脚本及真实客户端验收见 [testing.md](testing.md)。

## 参考资料

- [PyQGIS 3.44 独立脚本](https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/intro.html)
- [QGIS API](https://api.qgis.org/api/)
- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) 与 [v1 文档](https://py.sdk.modelcontextprotocol.io/v1/)
- [LangChain 工具](https://docs.langchain.com/oss/python/langchain/tools)

本地论文用于确定“加载—裁剪—可视化—制图”验收过程，不随代码分发。案例数据、地图、原始日志和临时 Agent 配置保留在被忽略的 `artifacts/`。


独立 DEM 全量参考脚本为 `scripts/validate_full_reference.py`，在包含 GDAL、Shapely 2、PyProj 与 NumPy 的 QGIS Python 中运行，参数为 `--summary <客户端 summary.json> --report <本地报告.json>`。它逐源像元检查原始边界 CRS 内的包含关系，报告边界/覆盖/数值差异；返回非零代表该参考标准不通过。其具体边缘规则及与运行时方法的差异必须随实验报告，不将任何单一检查器当作唯一科学真值。


固定步骤的真实数据恢复流程使用 `scripts/reliable_fault_case.py --data <数据目录> --output <新的本地实验目录>`。它在六个阶段终止 Worker，记录全部失败前输出及重放结果，不能代表自主规划能力。随后可在 QGIS Python 中运行 `scripts/compare_fault_replays.py --report <report.json> --output <比较报告.json>` 比较解码后的像元；严格图片不一致会返回非零，不能忽略该状态或把 PDF 字节差异当成地图错误。数据、工程和原始报告仍保留在忽略的实验目录。


服务端生成的伪彩色栅格样式将采样色阶颜色规范为 QGZ 可保存的 8 位 RGBA。这样可避免浮点 QColor 在项目序列化后变化，导致连续图例色带出现一灰度级差异；栅格值和色阶数值不变。对应回归覆盖真实 Worker 替换前后的 PNG 精确像素比较。

### 独立坡度验证

使用 QGIS Python 运行 `scripts/validate_slope_reference.py --source /path/to/projected.tif --slope /path/to/slope.tif --output /path/to/report.json`。脚本用 NumPy 独立实现 Horn 差分，逐像元核对坡度与 NoData；仅支持米制投影北向网格、单高程波段、尺度 1、角度输出且不插补边缘的案例。默认绝对容差为 10⁻⁵ 度，报告同时记录实际最大误差。源高程单位及垂直基准须另外确认，不能由此脚本推断。公开测试使用解析平面和人为错误，真实案例报告仍保存在忽略的 artifacts 目录。

### 恢复策略执行基线

`reliable_fault_case.py` 增加实验专用 `--recovery-policy none|fixed_retry|checkpoint` 和 `--fault-stage all|load|elevation|projected|slope|style|png`。生产 MCP 不暴露降级策略。三组保留同一任务契约、输入、步骤、验证和持久化，只在操作执行遇到 WorkerError 后分别停止、固定重试最多两次（坏 Worker 重建但不加载检查点）、或使用生产检查点恢复。失败后协调器仍执行安全回退，不能把回退误算为成功重试。

单独选择 style 故障可检验已有图层状态丢失后的恢复。各组输出独立目录，失败也记录注入位置、失败步骤耗时、最终状态及重试事件。该测试是固定步骤执行消融，既不是模型规划实验，也不等于四组研究实验全部完成；AI 修复必须由真实宿主生成并另外记录。同机存在其他计算负载时，耗时不得用于隔离条件下的性能结论。

### 合成语义故障执行夹具

`scripts/semantic_recovery_case.py --output artifacts/example-semantic-case` 创建三个 WGS84 观测点和锁定的 EPSG:3857 交付目标，故意提交一个目标 CRS 仍为 EPSG:4326 的有效结果。脚本验证最终检查失败、两次相同请求仅返回原幂等结果、恢复检查点仍不能消除语义错误，随后保留任务供真实宿主修复。这是同一任务上的顺序机制对照，不是独立随机化四组实验。

客户端运行器支持 `--prompt-file /path/to/private-prompt.txt`，用于不同于陕西目标任务的执行实验；最终提示文本摘要记录在 manifest 中。结合 `--resume-task`、`--state-dir` 继续夹具任务，宿主自行读取契约和诊断，不提供修复步骤。任务契约通过后仍须用独立坐标及属性参考验收，防止单纯重新标注 CRS 被误认为正确变换。所有生成数据、提示词和状态保留在忽略的实验目录中。

### 独立四组语义恢复试验

`semantic_ablation.py --output artifacts/example-ablation --client codex --model MODEL --repetitions 3 --seed 2026` 为每组每次建立独立任务，随机化组别执行顺序；无恢复、两次相同请求重试、检查点恢复、检查点加真实宿主修复共四组。各组源数据字节与初始契约摘要必须一致，报告保留软件摘要、任务状态、尝试数、独立坐标及属性检查和源文件未变检查。

这是单类合成语义故障的小型执行试验。固定重试保留生产幂等规则，故不会重复产生已提交副作用；不代表移除全部可靠架构的基线。host 组的端到端时间包含模型开销，同机负载没有隔离时不得将这些耗时当作恢复框架性能。当前不将三次试验外推成泛化成功率或统计显著性结论。外部客户端失败、超时或独立参考失败均应保留，不通过挑选成功运行计算结果。
