"""System-level preferences, clearly separate from product process recipes."""

import os
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget,
    QListWidget,
    QStackedWidget,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QFormLayout,
)
from ..widgets import Card, hbox, vbox, label
from ..dialogs import GatewayDialog
from .device import _page


class SettingsPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        lay, head = _page(self, "系统设置", "存储、设备连接、坐标约定与软件信息")
        body = QWidget()
        bl = hbox(body, margins=0)
        lay.addWidget(body, 1)
        self.categories = QListWidget()
        self.categories.addItems(
            ["存储与命名", "设备配置", "坐标与标定", "操作模式", "软件信息"]
        )
        self.categories.setMaximumWidth(210)
        self.categories.setStyleSheet("QListWidget::item {padding:12px 14px;}")
        bl.addWidget(self.categories)
        self.stack = QStackedWidget()
        bl.addWidget(self.stack, 1)
        self.categories.currentRowChanged.connect(self.stack.setCurrentIndex)
        storage = Card("存储与命名")
        storage.add(label("图像默认输出目录", role="muted"))
        self.image_path = QLineEdit(
            str(
                app.preferences.data.get(
                    "image_output", app.data_root.parent / "measurement-images"
                )
            )
        )
        storage.add(self.image_path)
        storage.add(QPushButton("选择目录…", clicked=self.choose))
        storage.add(label("运行记录与配方库：" + str(app.data_root), wrap=True))
        storage.add(
            label(
                "文件组织：sequence_<运行编号>/hole_<序号>/upper.tif、lower.tif\n所有图片与配方快照通过运行编号关联；文件名不覆盖历史结果。",
                wrap=True,
            )
        )
        storage.add(QPushButton("打开数据目录", clicked=self._open_data))
        storage.add(
            label(
                "配方修改需显式保存草稿；动作确认不会自动记忆。",
                wrap=True,
                role="muted",
            )
        )
        self.stack.addWidget(storage)
        device = Card("Gateway 连接配置")
        self.conn_info = label("", wrap=True)
        device.add(self.conn_info)
        device.add(
            QPushButton(
                "编辑连接与兼容选项…", clicked=lambda: GatewayDialog(app).exec()
            )
        )
        device.add(
            label(
                "API 能力由连接后的服务发现和实测确定，不能仅由 ZEN 版本号推断。",
                wrap=True,
                role="muted",
            )
        )
        self.stack.addWidget(device)
        coord = Card("坐标与标定")
        coord.add(
            label(
                "坐标：设备绝对坐标\nXY/Z：µm\nRx/Ry：µrad\n配方与设备通信内部统一使用 m。",
                wrap=True,
            )
        )
        coord.add(
            label(
                "样品装夹基准：现场人工核对\n自动样品对准与坐标变换：未启用\n物镜与倍率读回：以设备实际能力为准，未读到不代表已锁定。",
                wrap=True,
            )
        )
        coord.add(
            label("产品点位、焦面及运动范围在配方管理中设置。", wrap=True, role="muted")
        )
        self.stack.addWidget(coord)
        roles = Card("操作模式")
        roles.add(
            label(
                "正式运行：只接受已放行配方。\n工程试运行：允许经检查和人工确认后的草稿执行。\n离线模拟：只调用模拟设备，不能形成现场验证证据。",
                wrap=True,
            )
        )
        roles.add(
            label(
                "操作员姓名记入执行记录；本版不提供身份认证或安全权限隔离。",
                wrap=True,
                role="muted",
            )
        )
        self.stack.addWidget(roles)
        about = Card("软件信息")
        about.add(
            label(
                "精密测量工作站 · 2.7.0 联调预览\n统一配方、工程试运行与任务追溯。",
                wrap=True,
            )
        )
        about.add(QPushButton("导出诊断包…", clicked=app.export_diagnostic_bundle))
        about.add(
            QPushButton(
                "重建结果索引", clicked=lambda: app.pages["records"].rebuild_index()
            )
        )
        self.stack.addWidget(about)
        row = QWidget()
        rl = hbox(row, margins=0)
        self.message = label("")
        rl.addWidget(self.message, 1)
        rl.addWidget(QPushButton("取消修改", clicked=self.on_show))
        save = QPushButton("保存设置", clicked=self.save)
        save.setObjectName("primary")
        rl.addWidget(save)
        lay.addWidget(row)
        for i in range(self.stack.count()):
            self.stack.widget(i)._lay.addStretch(1)
        self.categories.setCurrentRow(0)

    def choose(self):
        p = QFileDialog.getExistingDirectory(self, "选择图像目录")
        if p:
            self.image_path.setText(p)

    def save(self):
        p = Path(self.image_path.text()).expanduser()
        if not self.image_path.text().strip() or p.resolve().is_relative_to(
            self.app.data_root.resolve()
        ):
            self.message.setText("图像目录必须在运行记录目录之外")
            return
        if self.app.preferences.update(image_output=str(p)):
            if not self.app.workflow or not self.app.workflow.busy:
                self.app.pages["sequence"].output.setText(str(p))
            self.message.setText("设置已保存")
        else:
            self.message.setText("设置保存失败，请检查偏好文件")

    def _open_data(self):
        self.app.data_root.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(self.app.data_root)

    def on_show(self):
        self.image_path.setText(
            str(
                self.app.preferences.data.get(
                    "image_output", self.app.data_root.parent / "measurement-images"
                )
            )
        )
        o = self.app.gateway_overrides
        self.conn_info.setText(
            "地址："
            + o.get("ZEN_GATEWAY_ADDRESS", "localhost:5002")
            + "\nCA："
            + o.get("ZEN_GATEWAY_CA", "默认证书")
            + "\n控制令牌："
            + ("本次已设置" if o.get("ZEN_GATEWAY_TOKEN") else "从现场配置读取")
        )
