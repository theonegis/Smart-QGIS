# 可靠模式简明用法

Smart-QGIS MCP 默认以 `reliable` 模式运行；`--execution-mode legacy` 是没有任务日志与恢复保证的直接工具模式。客户端负责模型推理、用户对话和审批，服务端负责 GIS 执行与持久化任务状态。

## 调用流程

1. 用 `task_start` 创建任务，提供原始目标、绝对路径输入和所需交付物。输入类型可以由服务端检查；`contract` 默认留空，只写用户明确附加的验收要求。
2. 标准地图/工程或已给定的处理计划：按响应调用 **无参数** 的 `task_execute_next`。不复制或构造内部 token，不重复提交已批准的 GIS 参数。
3. 通用 Processing：先确认一个当前安装的精确算法 ID；必要时用 `algorithm_info` 搜索或查看语义，再调用 `prepare_algorithm`。输入图层、输出文件和已知数值/波段/枚举/CRS/表达式分别按公开 schema 提交。服务端读取该算法的实时帮助并做基本参数检查；缺少无默认值的必需语义参数时，返回结构化问题。
4. 只有收到用户真实回答后才用 `task_answer`。准备完成后继续 `task_execute_next`，直到 `COMPLETED`、需要用户指导或出现明确阻塞。

公开可靠工具包括 `algorithm_info`、`task_start`、`task_execute_next`、`prepare_algorithm`、`task_answer`、`task_record_guidance`、`task_invalidate`、`task_recover` 和 `task_diagnose`。以运行中的 MCP 工具列表与 schema 为准，不猜测或搜索未暴露的工具名。多步任务按当前步骤逐步发现、准备、执行，不预先猜完所有算法与参数。

## 失败与恢复

`task_diagnose` 只读，不会重试；进程重启后使用原 task ID 调用 `task_recover`。未提交的失败处理步骤可用 `prepare_algorithm(repairs_step=...)` 重交；已提交但经证实错误的结果，先用 `task_invalidate` 失效生产者及下游，再修复并复用原逻辑输出 ID。所有尝试和检查点保留在任务日志中。

连续纠错达到上限，或客户端思考/工具调用超时后，停止盲试，展示具体问题并等待用户真实指示。用 `task_record_guidance` 记录后继续原任务，不新建任务掩盖失败。服务端默认纠错上限 3 次、单次 worker 操作超时 900 秒；客户端需自行设置合适的模型思考超时，它不是 MCP 启动参数。

## 最低检查与地图

执行前只检查输入、逻辑引用及当前工具/算法参数；执行后检查输出格式、可打开性、CRS 和用户明确附加的要求。不要把某案例的科学阈值或评价答案加入通用契约。未知且无文档默认值的必选选择应询问用户，不自行猜 CRS、字段、枚举或科学方法。

地图默认含图名、图例、比例尺与地理或投影坐标标注；只有用户明确要求才删去某项。地图范围以所展示数据的有效足迹为主，避免大片无意义留白和元素互相压盖。图例/比例尺优先利用不遮挡数据的图框内空白，放不下时在图框外比较侧边与下方布局。图例只展示对读者有意义的分类、色带和单位，不把 `Band 1` 这类内部标签当成主题信息。更完整的通用制图指导在[项目 Skill](../skills/smart-qgis-mcp/SKILL.md)。

## 启动配置与数据保留

```bash
uv run smart-qgis
uv run smart-qgis --correction-limit 5 --timeout 1200 --retention-days 10 --failed-retention-days 60
```

完成/取消任务目录默认保留 10 天，阻塞任务默认保留 60 天；清理仅在服务启动时执行，并跳过仍在运行或等待回答的任务。对应保留天数设为 `0` 可关闭该类自动清理。状态目录可由 `SMART_QGIS_STATE_DIR` 指定；重连时要使用相同目录。

本模式不是多租户隔离沙箱。输入和输出路径应由用户确认，多个客户端不能并发修改同一任务或同一交付文件。
