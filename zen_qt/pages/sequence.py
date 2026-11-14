"""Fixed automatic task editor; all hardware work belongs to Workflow's gate."""
import json
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox,QComboBox,QFileDialog,QLineEdit,QPushButton,
                              QTableWidget,QTableWidgetItem,QWidget)
from zen_marks import MarkLayer,MarkRecipe
from zen_records import write_json
from zen_sequence import SequencePlan,Station
from zen_workflow import Recipe
from ..widgets import Card,hbox,label,vbox
from .device import _page

UM=1e-6
FIELDS={
    'name':('任务名称',''), 'safe_z_um':('安全转场 Z · µm',''),
    'tilt_z_um':('测角起始焦面 Z · µm',''),
    'z_min_um':('任务 Z 下限 · µm',''), 'z_max_um':('任务 Z 上限 · µm',''),
    'max_angle_urad':('最大 |Rx|、|Ry| · µrad',''), 'max_rms_um':('最大 RMS · µm',''),
    'upper_experiment':('上孔实验',''), 'lower_experiment':('下孔实验',''),
    'upper_half_um':('上孔 AF 半范围 · µm','5'), 'lower_half_um':('下孔 AF 半范围 · µm','5'),
    'z_tolerance_um':('Z 到位容差 · µm','0.5'), 'settle_s':('稳定等待 · s','0.2'),
    'capture_timeout_s':('单层采图超时 · s','60'), 'output':('图像输出目录',''),
}


class SequencePage(QWidget):
    def __init__(self,app):
        super().__init__();self.app=app
        self._restoring=False;self._checked=None;self._teach_target=None
        self._source_demo=False
        lay,head=_page(self,'自动任务 / Sequence','测角 → 结果放行 → 安全 Z 转场 → 独立孔位上下层采图 → 安全停留')
        for text,fn in [('新建现场任务',self.new_field),('保存任务…',self.save_file),
                        ('加载任务…',self.load_file),('离线演示',lambda:app.connect_demo('sequence'))]:
            button=QPushButton(text,clicked=fn);head.addWidget(button)
        self.editor=QWidget();el=vbox(self.editor,margins=0)
        config=Card('1 · 任务配置')
        self.recipe_label=label('',wrap=True);config.add(self.recipe_label)
        self.source_label=label('',role='muted',wrap=True);config.add(self.source_label)
        config.add(label('孔位表独立于测角路径；所有 XY 移动前先到安全 Z。安全方向与间隙须现场示教。',wrap=True))
        self.fields={}
        items=list(FIELDS.items())
        for offset in range(0,len(items),3):
            row=QWidget();rl=hbox(row,margins=0)
            for key,(title,default) in items[offset:offset+3]:
                cell=QWidget();cl=vbox(cell,spacing=3,margins=0)
                cl.addWidget(label(title,role='muted'))
                edit=QLineEdit(default);edit.setPlaceholderText(title)
                edit.textChanged.connect(self.invalidate)
                cl.addWidget(edit);rl.addWidget(cell,1);self.fields[key]=edit
            config.add(row)
        line=QWidget();ll=hbox(line,margins=0)
        self.use_af=QCheckBox('上下孔启用局部 AF（首次建议关闭）')
        self.use_af.toggled.connect(self.invalidate);ll.addWidget(self.use_af)
        self.capture_kind=QComboBox();self.capture_kind.addItem('共聚焦 · RunExperiment','confocal')
        self.capture_kind.addItem('相机 · RunSnap','camera_snap')
        self.capture_kind.currentIndexChanged.connect(self.invalidate);ll.addWidget(self.capture_kind)
        ll.addWidget(QPushButton('选择图像目录…',clicked=self.choose_output));config.add(line)
        config.add(label('Z 范围仅检查指令与回读，不是硬件限位；不能限制 ZEN 内部 AF。实验须已验证不换物镜、不恢复位置。',wrap=True,role='muted'))
        el.addWidget(config)
        points=Card('2 · 独立孔位 / 中间转场点（按表格顺序执行）')
        self.stations=QTableWidget(0,6)
        self.stations.setHorizontalHeaderLabels(['编号','类型','绝对 X · µm','绝对 Y · µm','上孔 Z · µm','下孔 Z · µm'])
        self.stations.horizontalHeader().setStretchLastSection(True)
        self.stations.setSelectionBehavior(QTableWidget.SelectRows)
        self.stations.setMinimumHeight(210);self.stations.itemChanged.connect(self.invalidate)
        points.add(self.stations)
        line=QWidget();ll=hbox(line,margins=0)
        for text,fn in [('＋孔位',lambda:self.add_station()),('＋转场点',lambda:self.add_station('转场')),
                        ('删除',self.delete_station),('上移',lambda:self.reorder(-1)),('下移',lambda:self.reorder(1))]:
            ll.addWidget(QPushButton(text,clicked=fn))
        points.add(line)
        line=QWidget();ll=hbox(line,margins=0)
        for text,part in [('读取当前 XY 到所选行','xy'),('读取上孔 Z','upper'),('读取下孔 Z','lower')]:
            ll.addWidget(QPushButton(text,clicked=lambda _=False,p=part:self.teach(p)))
        points.add(line)
        points.add(label('转场行仅填写 XY；孔位行填写上下层 Z。同一对上下孔之间不移动 XY。',role='muted',wrap=True))
        el.addWidget(points);lay.addWidget(self.editor)
        control=Card('3 · 检查与执行')
        self.path_confirm=QCheckBox('已在现场核对测角及全部转场路线、安全 Z、各孔位焦面与任务 Z 范围')
        self.load_confirm=QCheckBox('已在 ZEN 验证所有实验加载/运行不换物镜、不恢复 XY/Z，采图为单平面')
        for check in (self.path_confirm,self.load_confirm):
            check.toggled.connect(self.refresh_enabled);control.add(check)
        self.status=label('请先配置测角配方、任务参数与独立孔位表。',wrap=True);control.add(self.status)
        row=QWidget();rl=hbox(row,margins=0)
        self.check_button=QPushButton('执行前只读检查',clicked=self.preflight)
        self.start_button=QPushButton('启动 Sequence',clicked=self.start);self.start_button.setObjectName('primary')
        self.stop_button=QPushButton('停止后续步骤',clicked=app.stop)
        for button in (self.check_button,self.start_button,self.stop_button):rl.addWidget(button)
        control.add(row);lay.addWidget(control)
        progress=Card('4 · 执行进度')
        self.steps=QTableWidget(0,3);self.steps.setHorizontalHeaderLabels(['步骤','状态','时间 / 错误'])
        self.steps.horizontalHeader().setStretchLastSection(True);self.steps.setMinimumHeight(220)
        self.steps.setEditTriggers(QTableWidget.NoEditTriggers);progress.add(self.steps)
        self.result_label=label('任务结果与逐孔 TIFF 将以同一运行编号关联保存。',wrap=True);progress.add(self.result_label)
        lay.addWidget(progress)
        self.restore(app.preferences.data.get('sequence',{}))

    def draft(self):
        return {'entry_unit':'um','fields':{k:e.text() for k,e in self.fields.items()},
                'rows':self.rows(),'use_autofocus':self.use_af.isChecked(),
                'capture_kind':self.capture_kind.currentData(),
                'simulated_configuration':self._source_demo,
                'tilt_coordinate_unit':'m','tilt':asdict(self.app.field_recipe) if self.app.field_recipe else None}

    def rows(self):
        return [[self.stations.item(r,c).text() if self.stations.item(r,c) else ''
                 for c in range(6)] for r in range(self.stations.rowCount())]

    def restore(self,draft):
        self._restoring=True
        for k,e in self.fields.items():e.setText(str(draft.get('fields',{}).get(k,FIELDS[k][1])))
        self.stations.setRowCount(0)
        for row in draft.get('rows',[]):self.add_station(values=row)
        self.use_af.setChecked(draft.get('use_autofocus',False))
        self.capture_kind.setCurrentIndex(max(0,self.capture_kind.findData(draft.get('capture_kind','confocal'))))
        self._source_demo=draft.get('simulated_configuration',False)
        if draft.get('tilt'):
            self.app.pages['tilt'].apply_recipe(Recipe(**draft['tilt']))
        self._restoring=False;self.invalidate()

    def persist(self):
        if not self._restoring:
            saved=self.app.preferences.data.get('sequence',{})
            field_content=bool(saved.get('rows') or saved.get('tilt') or any(saved.get('fields',{}).get(k)
                for k,(_,default) in FIELDS.items() if default==''))
            if self._source_demo and field_content and not saved.get('simulated_configuration',False):return
            self.app.preferences.update(sequence=self.draft())

    def invalidate(self,*_):
        if self._restoring:return
        self._checked=None
        if self.app.workflow and self.app.workflow.simulated:self._source_demo=True
        self.path_confirm.setChecked(False);self.load_confirm.setChecked(False)
        self.status.setText('参数已修改；请重新执行只读检查并确认路线与实验。')
        self.steps.setRowCount(0)
        self.result_label.setText('本配置尚未执行；历史任务请到记录与分析查看。')
        self.persist();self.refresh_enabled()

    def add_station(self,kind='孔位',values=None):
        r=self.stations.rowCount();self.stations.insertRow(r)
        data=values or [f'P{r+1:03d}',kind,'','','','']
        for c,val in enumerate(data[:6]):self.stations.setItem(r,c,QTableWidgetItem(str(val)))
        self.stations.selectRow(r)
        self.invalidate()

    def delete_station(self):
        row=self.stations.currentRow()
        if row>=0:self.stations.removeRow(row);self.invalidate()

    def reorder(self,delta):
        row=self.stations.currentRow();other=row+delta
        if not 0<=other<self.stations.rowCount() or row<0:return
        rows=self.rows();rows[row],rows[other]=rows[other],rows[row]
        draft=self.draft();draft['rows']=rows;self.restore(draft);self.stations.selectRow(other)

    def new_field(self):
        if self.app.workflow and self.app.workflow.busy:return
        self.restore({});self._source_demo=False;self.persist()
        self.status.setText('新的现场任务：请重新示教参数与孔位；测角配方在表面测角页配置。')

    def load_demo_defaults(self):
        fields={k:v[1] for k,v in FIELDS.items()}
        fields.update(name='演示 Sequence',safe_z_um='20',tilt_z_um='100',z_min_um='0',z_max_um='300',
                      max_angle_urad='10000',max_rms_um='10',upper_experiment='Demo upper',
                      lower_experiment='Demo lower',output=str(self.app.data_root.parent/'sequence-images'))
        self.restore({'fields':fields,'rows':[['V001','转场','1000','1000','',''],
                     ['H001','孔位','2000','1500','100','200'],['H002','孔位','2500','1500','102','202']],
                      'simulated_configuration':True})

    def choose_output(self):
        path=QFileDialog.getExistingDirectory(self,'选择图像输出目录')
        if path:self.fields['output'].setText(path)

    def build_plan(self):
        recipe=self.app.field_recipe
        if recipe is None:raise ValueError('请先在表面测角页加载或编辑测角配方')
        def number(key):
            try:return float(self.fields[key].text())
            except ValueError:raise ValueError(FIELDS[key][0]+' 请填写数字') from None
        values={k:number(k) for k in FIELDS if k not in ('name','output','upper_experiment','lower_experiment')}
        stations=[]
        for row in self.rows():
            name,kind,x,y,upper,lower=row
            if kind not in ('孔位','转场'):raise ValueError('点类型只能填写“孔位”或“转场”')
            try:
                mark=None
                if kind=='孔位':
                    mark=MarkRecipe(MarkLayer(self.fields['upper_experiment'].text().strip(),float(upper)*UM,values['upper_half_um']*UM),
                                    MarkLayer(self.fields['lower_experiment'].text().strip(),float(lower)*UM,values['lower_half_um']*UM),
                                    xy_tolerance_m=recipe.tolerance_m,z_tolerance_m=values['z_tolerance_um']*UM,
                                    af_timeout_s=recipe.af_timeout,arrival_timeout_s=recipe.arrival_timeout,
                                    capture_timeout_s=values['capture_timeout_s'],settle_s=values['settle_s'],
                                    capture_kind=self.capture_kind.currentData(),use_autofocus=self.use_af.isChecked())
                stations.append(Station(name.strip(),float(x)*UM,float(y)*UM,mark))
            except ValueError as error:raise ValueError(f'{name or "未编号点"} 的坐标/采图参数无效：{error}') from error
        plan=SequencePlan(self.fields['name'].text().strip(),recipe,tuple(stations),
                          values['safe_z_um']*UM,values['tilt_z_um']*UM,
                          values['z_min_um']*UM,values['z_max_um']*UM,values['max_angle_urad'],
                          values['max_rms_um']*UM,values['z_tolerance_um']*UM,values['settle_s'],self._source_demo)
        from zen_fit import validate_recipe
        plan.validate(validate_recipe)
        if not self.fields['output'].text().strip():raise ValueError('请选择图像输出目录')
        if Path(self.fields['output'].text()).expanduser().resolve().is_relative_to(self.app.data_root.resolve()):
            raise ValueError('图像输出目录必须位于运行记录目录之外')
        return plan

    def fingerprint(self):
        recipe=asdict(self.app.field_recipe) if self.app.field_recipe else None
        return json.dumps([self.draft(),recipe],ensure_ascii=False,sort_keys=True)

    def preflight(self):
        workflow=self.app.workflow
        try:
            plan=self.build_plan()
            if not workflow or not self.app.device_ready() or workflow.busy:raise ValueError('请先连接并解除待恢复状态')
            if not workflow.xy_enabled:raise ValueError('自动任务需要电动 XY')
            if not workflow.simulated:
                from zen_fit import FIELD_CONVENTION
                if plan.simulated_configuration or plan.tilt.sign_convention!=FIELD_CONVENTION:
                    raise ValueError('演示任务/配方不能用于现场；请新建现场任务')
                if not self.app.field_verified:raise ValueError('请先在表面测角页完成当前点 AF 校验')
            from zen_sequence import validate_capabilities
            validate_capabilities(workflow.device)
            token=self.fingerprint()
            def query(device):
                from zen_hardware import Motion
                if device.motion_state() is not Motion.IDLE:raise ValueError('XY 未空闲')
                xy=device.read_xy();z=device.read_z();optics=device.read_optics()
                import math
                if not all(math.isfinite(v) for v in (*xy,z)):raise ValueError('位置回读无效')
                if not plan.z_min_m<=z<=plan.z_max_m:raise ValueError('当前 Z 不在任务核对范围内')
                if not workflow.simulated:
                    from math import hypot
                    start=self.app.verified_xy
                    if start is None or hypot(xy[0]-start[0],xy[1]-start[1])>plan.tilt.tolerance_m:
                        raise ValueError('当前位置已偏离单点 AF 校验起点，请重新校验')
                return {'token':token,'device':id(device),'xy':xy,'optics':optics}
            workflow.read_only('sequence_preflight',query)
        except (ValueError,RuntimeError,AttributeError) as error:self.status.setText(str(error))

    def start(self):
        workflow=self.app.workflow;checked=self._checked
        try:
            if not checked or checked['token']!=self.fingerprint() or checked['device']!=id(workflow.device):
                raise ValueError('请重新执行只读检查')
            workflow.start_sequence(self.build_plan(),self.fields['output'].text(),
                confirmed=self.path_confirm.isChecked(),experiment_loading_confirmed=self.load_confirm.isChecked(),
                confirmed_start=checked['xy'],session={'sample':self.app.sample_edit.text(),'operator':self.app.operator_edit.text()})
            self._checked=None;self.path_confirm.setChecked(False);self.load_confirm.setChecked(False)
            self.steps.setRowCount(0);self.status.setText('Sequence 已提交；全部设备调用共用任务互斥门。')
            self.app.field_verified=False;self.app.verified_xy=None
            self.app.page_containers['sequence'].ensureWidgetVisible(self.steps)
        except (ValueError,RuntimeError,AttributeError) as error:self.status.setText(str(error))

    def teach(self,part):
        workflow=self.app.workflow;row=self.stations.currentRow()
        if row<0 or not workflow or workflow.busy or not self.app.device_ready():return
        self._teach_target=(row,part,id(workflow.device))
        def query(device):
            from zen_hardware import Motion
            if device.motion_state() is not Motion.IDLE:raise ValueError('XY 未空闲')
            return {'xy':device.read_xy(),'z':device.read_z(),'target':self._teach_target}
        try:workflow.read_only('sequence_teach',query)
        except RuntimeError as error:self.status.setText(str(error))

    def on_rpc_result(self,tag,value):
        if tag=='sequence_preflight':
            if value['token']==self.fingerprint() and self.app.workflow and value['device']==id(self.app.workflow.device):
                self._checked=value
                self.status.setText('只读检查通过；未测试 AF/曝光、未锁定物镜。请核对全部路线并勾选两项确认。'
                                    + (' 光学位置未完全读回，须人工核对。' if None in value['optics'] else ''))
        elif tag=='sequence_teach':
            row,part,device=value['target']
            if not self.app.workflow or device!=id(self.app.workflow.device) or row>=self.stations.rowCount():return
            if part=='xy':
                for c,v in zip((2,3),value['xy']):self.stations.setItem(row,c,QTableWidgetItem(f'{v/UM:.6f}'))
            else:self.stations.setItem(row,4 if part=='upper' else 5,QTableWidgetItem(f'{value["z"]/UM:.6f}'))
        self.refresh_enabled()

    def on_rpc_error(self,tag,error):
        if tag.startswith('sequence_'):self._checked=None;self.status.setText(str(error))

    def on_device_changed(self,workflow):
        if workflow and not workflow.simulated and self._source_demo:
            saved=self.app.preferences.data.get('sequence',{})
            if saved and not saved.get('simulated_configuration',False):self.restore(saved)
        self._checked=None;self._teach_target=None
        self.path_confirm.setChecked(False);self.load_confirm.setChecked(False)

    def on_workflow_event(self,kind,data,workflow):
        if kind!='sequence_progress':return
        report=data[0];self.steps.setRowCount(len(report['steps']))
        names={'running':'执行中','complete':'完成','failed':'失败','stopped':'中止'}
        for r,step in enumerate(report['steps']):
            for c,text in enumerate((step['title'],names.get(step['state'],step['state']),step.get('error') or step.get('finished_at') or step['started_at'])):
                self.steps.setItem(r,c,QTableWidgetItem(text))
        self.steps.resizeColumnToContents(0);self.steps.resizeColumnToContents(1)
        self.steps.scrollToBottom()
        if report['state']!='running':
            self.result_label.setText(f'任务 {names.get(report["state"],report["state"])} · 记录：{workflow.sequence_run_path}\n图像：{report.get("output_directory","")}')
            self.status.setText(report.get('error',{}).get('message','全部完成；停留在最后孔位 XY、安全 Z。'))

    def refresh_enabled(self):
        workflow=self.app.workflow;busy=bool(workflow and workflow.busy)
        ready=bool(workflow and self.app.device_ready() and workflow.xy_enabled and not busy)
        self.editor.setEnabled(not busy)
        self.path_confirm.setEnabled(ready);self.load_confirm.setEnabled(ready)
        self.check_button.setEnabled(ready)
        valid=bool(self._checked and self._checked['token']==self.fingerprint())
        self.start_button.setEnabled(ready and valid and self.path_confirm.isChecked() and self.load_confirm.isChecked())
        self.stop_button.setEnabled(busy)
        recipe=self.app.field_recipe
        self.recipe_label.setText('测角配方：'+(f'{recipe.name} · {recipe.experiment} · {len(recipe.targets)} 点（在表面测角页编辑）' if recipe else '尚未配置，请到表面测角页配置'))
        self.source_label.setText('配置来源：演示数据，禁止用于真实设备' if self._source_demo else '配置来源：现场输入 / 历史输入，执行前须重新核对')

    def save_file(self):
        if self.app.workflow and self.app.workflow.busy:return
        path,_=QFileDialog.getSaveFileName(self,'保存 Sequence 配置','','JSON (*.json)')
        if not path:return
        try:write_json(Path(path),{'schema_version':1,'kind':'sequence_ui','draft':self.draft()})
        except (OSError,ValueError) as error:self.status.setText(str(error))

    def load_file(self):
        if self.app.workflow and self.app.workflow.busy:return
        path,_=QFileDialog.getOpenFileName(self,'加载 Sequence 配置','','JSON (*.json)')
        if not path:return
        try:
            from zen_preferences import sanitize
            file=Path(path)
            if file.stat().st_size>1024*1024:raise ValueError('配置文件过大')
            data=json.loads(file.read_text(encoding='utf-8'))
            if not isinstance(data,dict) or data.get('schema_version')!=1 or data.get('kind')!='sequence_ui' or not isinstance(data.get('draft'),dict) or data['draft'].get('entry_unit')!='um':
                raise ValueError('Sequence 配置版本或单位无效')
            clean=sanitize({'sequence':data['draft']})['sequence']
            self.restore(clean)
            self.invalidate()
        except (OSError,ValueError,KeyError,TypeError) as error:self.status.setText('配置读取失败：'+str(error))
