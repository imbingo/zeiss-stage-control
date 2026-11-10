# ZEN AF 表面测角工作台

当前为 V2.0 联调预览。可在 Windows 上复核逐点采集、结果和恢复流程。现场接口已经过本机 gRPC 回环验证，实机联调与 V1.1 兼容性仍待确认。

## 启动

Windows x64 便携包已包含运行时和 Gateway 存根。解压后双击 `start_gui.bat`，现场操作见[首次上机指南](首次上机.md)。下面的 Python 安装和存根生成步骤适用于源码包。

安装含 Tcl/Tk 的 Windows Python 3.12。演示只用标准库。双击 `start_gui.bat`，或执行 `python zen_gui.py`。也可通过环境变量 `ZEN_PYTHON` 指定 Python 可执行文件的完整路径。

选择「正常演示」运行四点采集。完成后可导出 JSON。选择「第 3 点 AF 超时演示」复核部分采集记录和启动锁定。使用「确认演示恢复」记录模拟确认，再启动下一轮。

数据保存在 `%LOCALAPPDATA%\ZENSurfaceTilt\demo` 下，每次运行单独建目录。运行目录含配方快照、逐点 CSV、日志和最终状态。有效运行另有结果 JSON。演示坐标用 m 存储，界面显示 µm。演示角度采用 Rx=atan(dZ/dY)、Ry=−atan(dZ/dX)，旧版约定待原工程核对。

## 资料与联调

现场入口依赖 `grpcio`、`protobuf` 和由蔡司 2026.11.1 proto 生成的 `zen_api` 存根。使用同一个 Python 执行以下命令。将路径替换为已取得的 [官方 2026.11.1 proto 目录](https://github.com/zeiss-microscopy/OAD/tree/master/ZEN-API/protofiles/proto_2026.11.1)。

```powershell
python -m pip install -r requirements-gateway.txt
python generate_gateway_stubs.py C:\path\to\proto_2026.11.1
$env:PYTHONPATH = (Resolve-Path .\gateway_stubs).Path
python zen_gui.py
```

同机默认配置可直接点击「Gateway 只读 XY」。工作台尝试 `127.0.0.1:5000`，读取 `%PROGRAMDATA%\Carl Zeiss\ZEN APIGateway` 下的默认 CA 与令牌。自定义配置可通过进程环境覆盖 `ZEN_GATEWAY_ADDRESS`、`ZEN_GATEWAY_CA`（PEM 证书路径）和 `ZEN_GATEWAY_TOKEN`。远程连接须显式配置对应的 CA 和令牌。参见[首次上机指南](首次上机.md)。

按联调清单先执行「Gateway 只读 XY」。加载按 `recipe.example.json` 编辑的现场配方，然后示教或导入明确标有坐标单位的 CSV。示例点位仅展示文件格式。单点 AF 验证通过后，在设备端核对安全路径和间隙，再打开批量确认。批量配方需要明确采用临时联调约定 `联调约定：Rx=atan(dZ/dY)，Ry=-atan(dZ/dX)，角度单位 °`。正式约定待核对 V1.1。

运行回归测试使用 `python -B -m unittest -v test_zen_workflow`。

## 停止、退出与重启

「停止后续采集」会阻止开始后续点位，并等待当前调用返回。现场设备可能仍在运动或调焦。遇到紧急情况，按设备规程使用设备自身的急停与互锁。

任务运行期间关窗会请求停止后续采集。待状态栏显示本轮已结束或设备状态待确认后，再次关窗即可退出。强制退出或断电后，重新启动会扫描未完成的运行目录，现场待确认记录会锁住示教、单点 AF 和批量启动。

现场断线后可以重新连接读取 XY。解除待确认锁定前，请在设备端分别确认载物台 XY 与软件 AF/Z 已结束，再点击「确认现场恢复」，随后重新连接并做单点 AF 验证。XY 空闲只代表载物台状态。AF 超时后的 Z 行为仍待实机验证。

- [公开接口核查](ZEN-2026.11.1-接口核查.md)
- [实机联调清单](实机联调清单.md)
- 待获取 `ZEN_AF_Surface_Tilt_GUI_V1_1.zip` 和 `ZEN_API_2026_11_1_接口功能手册.docx`，用于核对旧配置、点位、CLI、符号与现场服务绑定。
