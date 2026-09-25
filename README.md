# Smart-QGIS 2.0

**让现有 AI Agent 在后台使用 QGIS，无需打开 QGIS 桌面。**

Smart-QGIS 是本地 stdio MCP 服务。Codex、Hermes 等客户端负责对话、规划、模型调用和权限管理；本项目使用 LangChain `StructuredTool` 组织工具，使用独立的 PyQGIS 进程执行空间分析与制图，不内置另一个 Agent harness，也不依赖某家模型服务。

## 从 1.0 迁移

1.0 是 QGIS 插件，使用 Qt 聊天面板和 Socket 转发。原版本保存在 **`1.0` 分支与 `1.0` tag**。2.0 在 `codex/2.0` 分支重新组织代码，移除了插件界面和 Socket 服务。不要把 2.0 复制到 QGIS 插件目录；改为在 Agent 中注册 MCP。原来的 `.qgs/.qgz` 工程仍可打开，前提是数据源可访问。

## 功能

- **工程与数据**：创建、打开、保存工程；加载矢量、栅格、OSM、Google/自定义 XYZ、WMS/WMTS。
- **图层与可视化**：排序、显隐、重命名；分类/分级符号、标签、透明度、色带、灰度、RGB、山体阴影、QML 样式。
- **空间处理**：查询并执行本机 QGIS/GDAL 注册的算法，包括裁剪、缓冲区、叠加、重投影、栅格计算、坡度等；可查询算法参数和结果。
- **要素操作**：筛选、选择、字段统计、GeoJSON 内存图层、导出 GPKG/GeoJSON/SHP。
- **制图**：可编辑打印布局、标题、图例、比例尺、经纬网、QPT 模板、PNG/JPEG/TIFF/PDF 导出。

## 安装与启动

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/) 和含 Python 绑定的 QGIS。macOS 默认发现 `/Applications/QGIS.app`；其他安装位置见[运行环境说明](docs/development.md)。

```bash
uv sync --locked
uv run smart-qgis
```

最后一条启动 stdio 服务，等待 MCP 客户端输入，**不会出现 GUI 或网页**。不要向标准输入手工键入聊天内容。

在客户端配置中，将下列路径换成本机项目的绝对路径：

```json
{
  "mcpServers": {
    "smart_qgis": {
      "command": "/path/to/Smart-QGIS/.venv/bin/python",
      "args": ["-m", "smart_qgis.server"]
    }
  }
}
```

Codex 和 Hermes 使用各自的配置格式，见[接入示例](docs/clients.md)。MCP 本身不需要 LLM API Key，模型和登录由客户端配置。建议工具超时设为 900 秒。

## 使用示例

连接后向 Agent 提出：

> 加载 `/data/ShannXi/ShannXi.shp` 和 `/data/ShannXi/DEM.tif`，用边界裁剪 DEM，NoData 设为 0，保存到 `/output/Elevation.tif`。用 Viridis 色带显示高程，边界透明填充、黑色描边。创建带标题、图例、比例尺和经纬网的地图，导出 PNG、PDF，并保存 QGIS 工程。

服务器还提供 `dem-map` 提示模板和 `qgis://project` 状态资源。

一个 MCP 进程维护一个工程；客户端断开后未保存状态会消失。内存图层应先导出并重新加载，再保存工程。处理算法可覆盖输出文件，请明确指定输出目录。底图需要网络；Google 提供道路、地形、卫星底图预设，也可提供自己的 XYZ URL；使用时遵循提供者的访问及署名要求，不内置凭据。

[工具与开发说明](docs/development.md) · [测试与案例](docs/testing.md)
