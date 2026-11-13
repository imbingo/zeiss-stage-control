"""设置 page: paths, gateway settings, version info."""

import os
from pathlib import Path

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ..dialogs import GatewayDialog
from ..widgets import Card, hbox, label, note, vbox
from .device import _page


class SettingsPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        lay, head = _page(self, "设置", "数据位置、连接配置与版本信息")

        conn = Card("Gateway 连接", trailing=QPushButton("编辑…", clicked=lambda: GatewayDialog(app).exec()))
        overrides = app.gateway_overrides
        lines = [f"地址：{overrides.get('ZEN_GATEWAY_ADDRESS', '（默认本机）')}",
                 f"CA：{overrides.get('ZEN_GATEWAY_CA', '（默认证书）')}",
                 f"令牌：{'已设置（本次运行有效）' if overrides.get('ZEN_GATEWAY_TOKEN') else '未设置'}"]
        self.conn_info = label("\n".join(lines), wrap=True, mono=True)
        self.conn_info.setStyleSheet("font-size:11.5px")
        conn.add(self.conn_info)
        lay.addWidget(conn)

        data = Card("数据位置")
        data.add(label(f"运行记录 / 偏好 / 日志：{app.data_root}", wrap=True, mono=True))
        row = QWidget()
        rl = hbox(row, spacing=8, margins=0)
        open_dir = QPushButton("打开数据目录", clicked=self._open_data)
        rl.addWidget(open_dir)
        rl.addWidget(QPushButton("重建 index.csv", clicked=lambda: app.pages["records"].rebuild_index()))
        rl.addStretch(1)
        data.add(row)
        lay.addWidget(data)

        about = Card("版本")
        about.add(label("ZEN 表面测角工作台 v2.5 · Qt 界面\n"
                        "硬件与测量逻辑与 v2.4.x 相同；Tk 经典界面仍可通过 start_gui_classic.bat 启动。",
                        role="muted", wrap=True))
        lay.addWidget(about)
        lay.addStretch(1)

    def _open_data(self):
        self.app.data_root.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(self.app.data_root)

    def on_show(self):
        overrides = self.app.gateway_overrides
        self.conn_info.setText(
            f"地址：{overrides.get('ZEN_GATEWAY_ADDRESS', '（默认本机）')}\n"
            f"CA：{overrides.get('ZEN_GATEWAY_CA', '（默认证书）')}\n"
            f"令牌：{'已设置（本次运行有效）' if overrides.get('ZEN_GATEWAY_TOKEN') else '未设置'}")
