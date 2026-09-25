# 2.0 测试与案例

验收日期：2026-09-25。环境：macOS / QGIS **4.2.2**（Qt 6）、Python 3.12、MCP SDK 1.30.0、LangChain Core 1.6.5。精确 Python 依赖在 `uv.lock`。

## 结果

| 验证项 | 结果与范围 |
|---|---|
| 原版存档 | `1.0` tag 和分支指向原插件提交 `4b708d8` |
| 无 GUI 运行 | 独立 `QgsApplication([], False)`，offscreen，未连接 QGIS Desktop |
| 自动测试 | 7 项通过：参数契约、进程串行/退出/超时/取消、真实 MCP/QGIS 集成；详见测试代码 |
| stdio 全流程 | 加载、裁剪、样式、布局、PNG/PDF/QPT、工程保存/重开/重新渲染通过 |
| Codex 云端 | GPT-6 Astra 在 Codex CLI 中实际调用 MCP 完成完整 DEM 案例 |
| Hermes 本地 | Qwen3.8 27B MLX 经本机 Ollama，由 Hermes 调用 MCP 完成同一案例 |
| 独立数值校验 | 两个客户端结果均为 21,360 × 29,268；NoData=0；518 个有效采样点与源 DEM 一致，707 个边界外采样点为 NoData |
| 额外地形案例 | 裁剪 DEM 重投影至 EPSG:32649、300 m 分辨率、坡度（度）、山体阴影叠加、PNG/PDF 和工程保存通过 |
| 在线底图 | OSM 与 Google Roadmap 实际下载瓦片并输出可见地图，图中包含署名 |
| 地图视觉核验 | 检查中文标题、色带、边界、比例尺、经纬网；PDF 栅格化后检查；修复投影地图经纬标签交叉重叠 |

Hermes 在第一次地图导出调用中选错工具，参数校验拒绝后自行改用 `export_map`，最终成功。初期沙箱阻止 Ollama 和在线底图网络连接，允许本地/网络访问后完成重测；这些初期失败没有被算作通过。Codex 非交互初期因工具审批未配置而停止，按测试授权配置单服务器审批后重测通过。

最终代码分别重跑两套 Agent 后均成功；两张 PNG 像素完全一致，两份 PDF 均为单页。

案例原始数据、Agent 对话、配置、使用量记录与地图保留在本地 `artifacts/`，不上传。上述数值为抽样校验，不冒称逐像素全量验证。Google 地形/卫星和任意第三方 WMS/WMTS 未逐一联网验收；它们使用同一提供者接口，服务可用性、坐标偏移和凭据须按具体来源确认。

## 自动回归

```bash
uv sync --locked
uv run ruff check src scripts tests
uv run pytest -q
```

没有 QGIS 时矢量集成测试会跳过；完整验收必须在安装 QGIS 的机器上运行。测试使用临时合成数据，不依赖陕西私人数据。

## 复现论文主流程

输入目录应含 `ShannXi.shp` 及其伴随文件、`DEM.tif`。输出目录使用全新目录，避免误覆盖。

```bash
uv run python scripts/paper_case.py --data /data/ShannXi --output /output/protocol-case
```

该脚本连接真实 stdio MCP，逐步调用工具，保存本地工具轨迹。它是确定性协议测试，不替代 Agent 验收。

```bash
uv run python scripts/client_case.py codex --model YOUR_CLOUD_MODEL \
  --data /data/ShannXi --output /output/codex-case
uv run python scripts/client_case.py hermes --model YOUR_OLLAMA_MODEL \
  --data /data/ShannXi --output /output/hermes-case
```

Codex 使用已有登录；Hermes 在输出目录中生成隔离的测试配置并访问本地 Ollama，不修改常用配置。不把日志或该测试目录提交到 Git。

使用 QGIS 的 Python（含 GDAL/numpy）独立验证裁剪结果：

```bash
/path/to/qgis/python scripts/validate_case.py \
  --data /data/ShannXi --output /output/codex-case
```

追加地形案例：

```bash
uv run python scripts/terrain_case.py \
  --elevation /output/codex-case/Elevation.tif --output /output/terrain
```

在线底图渲染：

```bash
uv run python scripts/basemap_case.py --service osm --output /output/basemaps
uv run python scripts/basemap_case.py --service google --output /output/basemaps
```

除了检查文件存在，还应打开图像确认瓦片内容。部分 QGIS/GDAL 组合会在 PNG 导出时记录不支持 update access 的警告；本次 PNG 实际输出、解码和视觉核验均通过。输出文件及地图渲染错误也由工具检查。

## 兼容性边界

本次实测不代表所有 QGIS 版本、所有 Processing 算法和所有地图服务均通过。QGIS 3.44 API 文档用作设计参考，实际平台是 QGIS 4.2.2。Linux/Windows 与其他 QGIS 发行版需要相应环境的独立测试。
