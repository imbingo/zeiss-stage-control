# ZEN 3.12 / 3.14 / 3.15 API 全功能兼容性核查

核查日期：2026-11-14。软件基线：[V2.5.1 的准确提交 4867009](https://github.com/imbingo/zeiss-stage-control/tree/4867009141e76b070c8279d233f2502d65d4b8a7)。本次只添加核查报告和复现工具，没有改变设备控制代码或发布新安装包。

**结论：目前不能宣称“所有功能同时兼容 3.12 和 3.14 及以上”。3.14 与 3.15 的相关接口契约匹配；3.12 仍有一处确定的 XY 适配缺口，以及官方接口中未提供的选配功能。** 现场已反馈仅上 mark 的 TIFF 与完成记录；成功轮次未提供完整配方，不能据此确定是否启用 AF 或采图模式，更不等于全部功能实机通过。

## 官方资料与版本基线

本次重新查询了 [蔡司官方 OAD GitHub](https://github.com/zeiss-microscopy/OAD)。查询时 master 为 `781ce14954946366524b96347e7767cefb11a2c8`，与软件构建引用的官方提交一致。

| ZEN 产品版本 | 本次使用的官方证据 | 证据含义 |
| --- | --- | --- |
| ZEN 3.12 | [历史文档：ZEN Release 3.12 - Spring 2025 (2025-05-09)](https://github.com/zeiss-microscopy/OAD/blob/c94fb25f693900c4ad23b4e4fbe3f7f3f7b57fbc/ZEN-API/documentation/ZEN_API_Documentation_20250509.md)；[同一提交的官方生成客户端](https://github.com/zeiss-microscopy/OAD/tree/c94fb25f693900c4ad23b4e4fbe3f7f3f7b57fbc/ZEN-API/python_examples/public/zen_api) | 明确标注 3.12，不能拿 2025.10.1（官方对应 3.13）冒充 3.12 |
| ZEN 3.14 | [proto_2026.5.1](https://github.com/zeiss-microscopy/OAD/tree/781ce14954946366524b96347e7767cefb11a2c8/ZEN-API/protofiles/proto_2026.5.1)；[官方在线文档](https://zeiss-microscopy.github.io/OAD/zenapi/2026.5.1/) | 官方版本表对应 ZEN 3.14 |
| ZEN core 3.14 | [proto_2026.7.1](https://github.com/zeiss-microscopy/OAD/tree/781ce14954946366524b96347e7767cefb11a2c8/ZEN-API/protofiles/proto_2026.7.1)；[官方在线文档](https://zeiss-microscopy.github.io/OAD/zenapi/2026.7.1/) | 属于 ZEN core，不能混同于所有 ZEN blue 配置 |
| ZEN 3.15 | [proto_2026.11.1](https://github.com/zeiss-microscopy/OAD/tree/781ce14954946366524b96347e7767cefb11a2c8/ZEN-API/protofiles/proto_2026.11.1)；[官方在线文档](https://zeiss-microscopy.github.io/OAD/zenapi/2026.11.1/) | 当前软件随包生成存根的来源 |

版本映射来自 [官方 README 的 Supported ZEN and ZEN core Versions](https://github.com/zeiss-microscopy/OAD/blob/781ce14954946366524b96347e7767cefb11a2c8/ZEN-API/README.md#supported-zen-and-zen-core-versions)。官方同时写明 **“Not all API methods will work for every system or software version”**。下面的“契约匹配”表示路径、字段和单位方面的证据，不保证任意型号、许可证、实验配置下都能执行。3.15 以后的未知版本不在本次可确认范围内。

## 检查覆盖范围

覆盖硬件库全部 **28 个操作**，以及角度扫描、上下 mark 流程中的补充接口，共 **31 个蔡司 RPC 方法**；另外检查标准 gRPC reflection 的服务列表与描述符查询。包含显式启用的 Focus v1、Definite Focus 兼容路径。

机器可读的 [逐方法清单 CSV](evidence/methods.csv) 和 [契约比较 JSON](evidence/contracts.json) 记录每个方法的历史路径、当前路由是否实现、请求/响应比较结果。31 个方法不是 31 项实机成功，也不意味着每个方法默认启用。

| 契约检查结果 | 3.12 历史定义 | 3.14 | core 3.14 | 3.15 |
| --- | ---: | ---: | ---: | ---: |
| 官方可找到对应方法（包含改名前的方法） | 25/31 | 31/31 | 31/31 | 31/31 |
| 当前代码存在对应调用路径（包含可选路径） | 23/31 | 31/31 | 31/31 | 31/31 |
| 已找到方法的请求/响应字段比较存在差异 | 0 | 0 | 0 | 基准 |

3.12 的差额：XY 两个方法有官方旧路径但代码没有适配；物镜位置、Optovar 位置、相机三个方法和 Definite Focus 一个方法未出现在该版官方定义中。未出现在公开定义中不等于能断言某台设备绝无私有扩展或后续补丁。

## 全部硬件库操作检查

表中“匹配”均指公开接口契约；“待实测”指尚无当前设备的完成证据。缩写：E = `zen_api.acquisition.v1beta.ExperimentService`；AF = `ExperimentSwAutofocusService`；ZS = `ZStackService`。

| 硬件库操作 | 实际依赖 | ZEN 3.12 与当前代码 | ZEN 3.14 / 3.15 |
| --- | --- | --- | --- |
| 连接通信 `connect` | TLS、control-token、通道就绪 | 官方连接方式匹配；通道就绪本身不证明授权 | 官方连接方式匹配 |
| 断开 `disconnect` | 本地关闭通道 | 版本无关；不等于物理停止 | 同左 |
| 列服务 `list_services` | 标准 ServerReflectionInfo | 官方 Gateway 提供聚合 reflection；服务清单仍须现场读 | 同左 |
| 读 Z `read_z` | Focus v2.GetPosition | 米；契约匹配，现场已成功 | 米；契约匹配 |
| 移动 Z `move_z` | Focus v2.MoveTo | 米；契约匹配，动作/到位须现场验证 | 米；契约匹配 |
| 停 Z `stop_z` | Focus v2.Stop | 契约匹配；不是 AF、采集或硬件急停的通用接口 | 同左 |
| 读 XY `read_xy` | SimpleStageService.GetPosition | **不完整：应适配旧 lm.hardware.v2.StageService.GetPosition** | 当前新路径匹配 |
| 读 XY 运动状态 `read_xy_motion` | hardware.v1.StageService.GetStageMotionState | 原路径、枚举契约匹配；无 XY 设备禁止调用 | 同左 |
| 移动 XY `move_xy` | SimpleStageService.MoveTo | **不完整：应适配旧 lm.hardware.v2.StageService.MoveTo** | 当前新路径匹配 |
| 读镜头/倍率 `read_optics` | ObjectiveChanger v2 / Optovar v2.GetPosition | 两项不在 3.12 公开定义；现场均 UNIMPLEMENTED；代码保留 null/未校验 | 有公开方法；仍要求硬件及现场服务支持 |
| 列实验 `list_experiments` | E.GetAvailableExperiments | 契约匹配，现场仍需方法级验证 | 匹配 |
| 加载实验 `load_experiment` | E.Load | 匹配；现场 OK 并返回独立加载 ID | 匹配 |
| 克隆实验 `clone_experiment` | E.Clone | 匹配；现场 OK | 匹配 |
| 读 XML 摘要 `export_experiment` | E.Export | 匹配；使用 Load 返回 ID 后现场 OK | 匹配 |
| 读 AF `get_af` | AF.GetAutofocusParameters | 旧 v1beta 路径；当前反射适配已实现，协议模拟通过；旧日志只证明 v1 路径不支持 | v1 路径匹配 |
| 设置 AF 范围 `set_af_range` | AF.Get/SetAutofocusParameters | 同上；范围与 offset 单位均为米；须现场复验读回 | v1 路径、字段匹配 |
| 执行 AF `af` | AF.FindAutoFocus | v1beta 适配已实现；timeout 秒，focus_position 米；须现场实测 | v1 路径匹配 |
| 检查 Z-stack `is_zstack` | ZS.IsZStackExperiment | v1beta 适配已实现，协议模拟通过 | v1 路径匹配 |
| 读取 Z-stack `get_zstack` | ZS.GetZStackInfo | 同上；first/last/range/interval 为米 | v1 路径匹配 |
| 设置 Z-stack `set_zstack` | ZS.ModifyZStackFirstLast | 同上；不会自动启用维度或执行扫描 | v1 路径匹配 |
| SNAP `snap` | E.RunSnap | 匹配；现场 OK；单步测试不接像素流、不生成本软件 TIFF | 匹配 |
| 运行共焦/Z-stack `run_experiment` | E.RunExperiment | 契约匹配；现场曾超时且状态返回 Z 索引 8/21，不能标为成功完成 | 匹配；实验耗时、维度须现场确认 |
| 停实验 `stop_experiment` | E.Stop | 契约匹配；必须指定当前有效实验 ID；结束状态待验证 | 匹配 |
| 读实验状态 `experiment_status` | E.GetStatus | 匹配；现场 OK；running=false 不保证计划帧全部采齐 | 匹配 |
| 读保存路径 `image_output_path` | E.GetImageOutputPath | 契约匹配，待现场实测 | 匹配 |
| 列相机 `list_cameras` | Camera v2.GetCameras | 不在 3.12 公开定义；现场 UNIMPLEMENTED | 有公开方法；硬件依赖 |
| 读相机 `get_camera` | Camera v2.GetCamera | 不在 3.12 公开定义，不能认定可用 | 有公开方法；硬件依赖 |
| 设曝光 `set_exposure` | Camera v2.GetCamera / ConfigureCamera | 同上；不能用作 3.12 的通用曝光控制 | 有公开方法；曝光单位公开注释未明确，本软件传 API 原始值并保留 binning/ROI |

## 高层工作流与补充接口

| 功能 | 检查结论 |
| --- | --- |
| 角度扫描 | 依赖 XY 读取/移动/状态、实验 Load、软件 AF 返回焦点。当前 3.12 的 XY 路径缺口会阻断流程，即使 AF 的 v1beta 适配可用，也不能宣称完整兼容。3.14/3.15 契约匹配，仍需安装 XY 与实机验证。 |
| 无 XY 单点/上下 mark | 不调用 XY；Focus v2、E.Load/Clone、RunSnap 或 RunExperiment、MonitorExperiment 的契约都有 3.12 证据。现场仅上 mark 已反馈 TIFF，但成功轮次的模式与 AF 开关字段未完整提供；双层/自动 AF 尚须现场验收。 |
| 采图前收敛 Z-stack | 除 IsZStackExperiment、GetZStackInfo、ModifyZStackFirstLast 外，还使用 ModifyZStackCenterRange；3.12 为 v1beta，3.14/3.15 为 v1。当前均有反射路径，模拟测试验证调用到 v1beta。是否接受零范围/单层需真实 ZEN 验证。 |
| 图像流与 TIFF | `acquisition.v1beta.ExperimentStreamingService.MonitorExperiment` 在三个版本中存在，所用帧/像素/位置/标定字段匹配。`enable_raw_data=false` 是让 Gateway 把 LSM 分行数据拼成完整帧，仍读取 pixel_data.raw_data；不是 CZI 转 TIFF，也不是要求实验输出 TIFF。 |
| 像素格式 | 软件只接受 GRAY8、GRAY16、BGR24、BGR48；逐像素格式、尺寸、字节数、标定检查后写 TIFF。契约有该格式不等于所有可能图像格式都被软件支持。单通道/单层限制仍然存在。 |
| Focus v1 显式回退 | 3.12/3.14/3.15 均有定义；v1 为 µm，v2 为 m。当前 read/move 显式转换且默认不开启；Stop 当前仍是 v2，没有 v1 Stop 回退。 |
| Definite Focus 显式回退 | 3.12 公开资料未列出此服务，不能将它当作 3.12 的通用替代 AF。3.14/3.15 有 FindSurface，但依赖特定硬件；其返回 zposition 注释没有明确单位，当前代码按米使用，需厂家或 Focus 回读确认后才能验收此可选路径。 |
| 镜头旋转约束 | 软件没有直接调用 ObjectiveChanger.MoveTo 或 Optovar.MoveTo。但是 Load/Run 的实验设置可能影响光路，且 3.12 无转盘读回时 null 与 null 相等不能证明位置没变；操作员确认不能解释为 API 硬锁。 |
| 路径编辑、参数保存、恢复确认、归档、Rx/Ry µrad | 这些是本地功能，不依赖 ZEN 新旧 API。角度计算用米坐标、结果输出 µrad；输入偏好迁移与真实配方/API 单位分开。恢复确认不能自动证明设备结束。 |

## 本次确认的问题与后续修改范围

1. **P1：补齐 3.12 的 XY 服务路由。** 当前 [gateway.py 的 read_xy / move_xy](https://github.com/imbingo/zeiss-stage-control/blob/4867009141e76b070c8279d233f2502d65d4b8a7/zen_hardware/gateway.py#L146) 固定使用 SimpleStageService。官方 [3.12 LM Stage 示例](https://github.com/zeiss-microscopy/OAD/blob/c94fb25f693900c4ad23b4e4fbe3f7f3f7b57fbc/ZEN-API/python_examples/zenapi_stage_LM.py) 明确调用 lm.hardware.v2.StageService。通用回退按短服务名匹配，SimpleStageService 与 StageService 名字不同，不会自动命中。应显式选择这两条等价的米制 XY 路径；不要退到 µm 版 Stage v1，亦不要混用另一套多轴 StageService 请求。
2. **P2：开机检查须按能力组合评估。** [reflection.py](https://github.com/imbingo/zeiss-stage-control/blob/4867009141e76b070c8279d233f2502d65d4b8a7/zen_hardware/reflection.py) 把新 XY 路径列为唯一必需服务；[preflight.py](https://github.com/imbingo/zeiss-stage-control/blob/4867009141e76b070c8279d233f2502d65d4b8a7/zen_hardware/preflight.py#L26) 逐项列可选路径缺失，可能在 v1 与 v1beta 二选一已满足时仍报缺失，并把“服务已注册”写成“已自动降级、不影响对焦”。应区分已注册、方法支持、单步验证、完整流程验收，选配相机/转盘应明确不适用或未校验。
3. **P2：3.12 缺失的功能必须降级展示。** 相机参数、转盘和 Optovar 读回、Definite Focus 不能承诺 3.12 通用支持；无相机参数 API 不妨碍使用已验证实验拍图。3.12 曝光宜先由 ZEN 实验设置，软件不应猜测旧 API 名称。
4. **P1：Z-stack 写入回退的错误条件过宽。** [_collapse_zstack](https://github.com/imbingo/zeiss-stage-control/blob/4867009141e76b070c8279d233f2502d65d4b8a7/zen_hardware/mark_gateway.py#L497) 在 ModifyZStackFirstLast 任意 gRPC 错误后尝试 ModifyZStackCenterRange，包含超时和权限错误。跨版本适配不应在动作结果不确定时再发送另一写入。需要把“方法不支持”的回退与“超时/通信/授权失败”的停止恢复分开；是否接受 first=last/range=0 也需现场读回。
5. **待证实：Definite Focus 返回单位。** 三套新 proto 对 zposition 都只写“The Z position”，不支持用文档断言一定为米。可选路径应以厂家确认或硬件 Focus v2 回读验证为准，不能把软件 mock 中的米制数值当作官方单位证据。

## 这次实际做了哪些验证

- 下载并固定官方历史与当前文件，保留 [来源 URL 和 SHA-256 清单](evidence/source-manifest.json)。历史生成客户端只通过 Python AST 读取，不执行下载代码。
- 编译 2026.5.1、2026.7.1、2026.11.1 的 proto 描述符，比较相关方法的请求/响应、递归嵌套字段、字段号、类型、wrapper、重复字段和相关枚举；对 3.12 比较历史生成客户端中的相同信息。相关已找到方法未发现字段差异；单位另对照注释人工核查。
- 三套官方存根分别运行当前 58 项核心及 wire 测试，均通过、无跳过：[结果](evidence/test-results.json)。这是三次软件回归，不是三台不同 ZEN 的实测，也不代表每个相机/Stop 方法都经过真实服务执行。
- 按 3.12 官方旧服务路径限制本地 gRPC 模拟器，运行 6 项路由检查：[结果及实际调用清单](evidence/312-route-mock.json)。确认 AF v1beta 读写与执行、ZStack v1beta 收敛、共焦完整帧、无 AF SNAP；确认相机/转盘保持不可用；确认当前电动 XY 连接按预期暴露兼容缺口。模拟器复用现有 wire fixture，字段先经官方档案比较，不能模拟真实 AF 质量、实验接受条件或硬件运动。
- 现场证据仍以 [2026-11-13 的 ZEN 3.12 测试报告](../../field-tests/2026-11-13-ZEN3.12-现场测试结果.md) 为准。本次没有访问显微镜，未改变该报告中的成功/失败结论。

### 复现方式

在仓库根目录使用安装了 requirements-gateway.txt 的 Python：

```bash
python docs/api-compatibility/2026-11-14/fetch_sources.py --output /tmp/zeiss-api-audit
python docs/api-compatibility/2026-11-14/audit_contracts.py --sources /tmp/zeiss-api-audit --output /tmp/contracts.json
python generate_gateway_stubs.py /tmp/zeiss-api-audit/781ce14954946366524b96347e7767cefb11a2c8/ZEN-API/protofiles/proto_2026.11.1 --output /tmp/zeiss-audit-stubs
PYTHONPATH=.:/tmp/zeiss-audit-stubs python docs/api-compatibility/2026-11-14/verify_312_routes.py --contracts /tmp/contracts.json --output /tmp/312-routes.json
```

路径示例适用于 Linux；Windows 的 PYTHONPATH 分隔符需使用分号。缓存目录按官方完整提交 SHA 分开，脚本不向设备发送请求。

## 现场验收建议

先补齐 XY 路由和错误回退，再分别在真实 3.12 与 3.14/3.15 上保存同一套单步结果：服务/描述符、Z Get/Move/Stop、XY Get/Move/状态（有电动 XY 时）、Load/Clone/Export、AF Get/Set/读回/Find、ZStack Get/Set/读回、单平面 RunSnap/RunExperiment/完整图像/结束状态、Stop 与恢复。相机和转盘只在实际注册且有硬件时测试。双层 mark 最后验证像素标定、Z 位置与两张文件，再交给 SOMA。未通过的选配项不应使已通过的基础采图被误判为全部不可用。
