"""GUI-independent command catalogue and durable single-operation test session."""

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from threading import Lock
from time import monotonic
import json
import os
import traceback
from uuid import uuid4

from zen_records import pending_recovery, utc_now, write_json
from .rpc_diagnostics import diagnostic_from_error


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    numeric: bool = False
    optional: bool = False


@dataclass(frozen=True)
class Operation:
    key: str
    title: str
    group: str
    fields: tuple = ()
    writes: bool = False
    may_move: bool = False
    xy_required: bool = False
    is_stop: bool = False
    help: str = ''


EXP = Field('experiment_id','已加载实验 ID（本次连接）')
NAME = Field('experiment_name','实验名称')
OUT = Field('output_name','输出文件名（可留空，不含路径/后缀）',optional=True)
CAM = Field('camera_id','相机 ID')
OPERATIONS = {op.key:op for op in (
    Operation('connect','连接通信（不读位置）','连接',help='仅建立 TLS/通道，不加载实验、不读 XY/Z；认证是否接受须通过后续只读请求验证。'),
    Operation('disconnect','断开通信','连接',help='只关闭客户端通道，不等于设备停止。'),
    Operation('list_services','列出已注册 gRPC 服务','连接',help='只读 reflection 服务清单，用于核对本机 ZEN 注册的服务命名空间（v1/v1beta 等）。'),
    Operation('read_z','读取 Z','位置',help='Focus v2 GetPosition，返回单位 m。'),
    Operation('move_z','Z MoveTo','位置',(Field('z_um','目标绝对 Z (µm)',True),),True,True,help='只发送一次 Z MoveTo。不会自动 AF、拍照或回读；RPC 返回不证明轴已静止。'),
    Operation('stop_z','请求 Z Stop','位置',writes=True,may_move=True,is_stop=True,help='只请求 Focus.Stop，不代表 AF 或采集已结束；不能当硬件急停。'),
    Operation('read_xy','读取 XY','位置',xy_required=True),
    Operation('read_xy_motion','读取 XY 运动状态','位置',xy_required=True),
    Operation('move_xy','XY MoveTo','位置',(Field('x_um','目标绝对 X (µm)',True),Field('y_um','目标绝对 Y (µm)',True)),True,True,True,help='只发送一次 XY MoveTo，不等待到位；无 XY 模式禁止。'),
    Operation('read_optics','读取转盘/倍率位置','位置',help='仅 GetPosition，不提供转动物镜的按钮。null 表示未提供，位置读到也不代表已锁定。'),
    Operation('list_experiments','列出实验名称','实验'),
    Operation('load_experiment','加载实验 Load','实验',(NAME,),True,True,help='只调用 Load，不自动 Clone/AF/MoveTo/采集。加载实验本身可能应用硬件设置或换物镜，须先在 ZEN 确认安全。'),
    Operation('clone_experiment','克隆实验 Clone','实验',(EXP,),True,help='只调用 Clone，成功后显示新的实验 ID；不自动加载其他实验或启动动作。'),
    Operation('export_experiment','读取实验 XML 摘要','实验',(EXP,),help='只验证 Export，显示长度和 SHA-256；原始 XML 不进入共享日志。'),
    Operation('get_af','读取 AF 参数','AF',(EXP,)),
    Operation('set_af_range','设置局部 AF 范围','AF',(EXP,Field('low_um','AF 下限 Z (µm)',True),Field('high_um','AF 上限 Z (µm)',True)),True,help='先 GetAutofocusParameters 再 Set，保留模式；不自动 Clone、AF 或 Save。修改当前加载实例。'),
    Operation('af','执行一次 AF','AF',(EXP,Field('af_timeout_s','AF 服务端超时 (s)',True)),True,True,help='只调用 FindAutoFocus，沿用当前实验 AF 设置，可能运动 Z；不自动设置范围或 Clone。'),
    Operation('is_zstack','检查 Z-stack 是否启用','Z-stack',(EXP,)),
    Operation('get_zstack','读取 Z-stack 参数','Z-stack',(EXP,)),
    Operation('set_zstack','设置 Z-stack 起止/步距','Z-stack',(EXP,Field('first_um','首层绝对 Z (µm)',True),Field('last_um','末层绝对 Z (µm)',True),Field('interval_um','层间距 (µm)',True)),True,help='只修改当前实例的 Z-stack 范围，不自动启用维度、不执行扫描、不 Save。'),
    Operation('snap','执行一次 SNAP（相机）','采集',(EXP,OUT),True,True,help='只调用 RunSnap，不 Clone/AF/MoveTo/接收像素流；原始文件由 ZEN 保存。不是共聚焦采集的替代。'),
    Operation('run_experiment','运行当前实验（共焦/Z-stack）','采集',(EXP,OUT),True,True,help='只调用 RunExperiment，按 ZEN 已配置的实验运行，包括可能的 Z-stack/多通道与硬件动作，不自动修改维度。'),
    Operation('stop_experiment','请求停止指定实验','采集',(EXP,),True,True,is_stop=True,help='只对填写的实验 ID 调用 Stop，不使用空 ID 停止未知实验；返回不证明所有物理动作已结束。'),
    Operation('experiment_status','读取实验执行状态','采集',(EXP,)),
    Operation('image_output_path','读取 ZEN 图像保存目录','采集',help='只查询路径，不浏览、不删除文件；单步采集不自动转换为 TIFF。'),
    Operation('list_cameras','列出相机','相机'),
    Operation('get_camera','读取相机/曝光范围','相机',(CAM,)),
    Operation('set_exposure','设置相机曝光','相机',(CAM,Field('exposure_value','曝光值（API 原始单位）',True)),True,
              help='先 GetCamera 读取范围，再 ConfigureCamera，保留当前 binning/ROI。公开接口未标注曝光单位，不按 ms 猜换算；不等同共焦激光/扫描驻留时间。'),
)}


def validate(operation, values):
    if not isinstance(values, dict) or set(values)-{f.name for f in operation.fields}:
        raise ValueError('参数字段不匹配；不接受额外或凭据参数')
    result={}
    for field in operation.fields:
        value=values.get(field.name, '')
        if field.numeric:
            try:value=float(value)
            except (ValueError,TypeError,OverflowError):raise ValueError(field.label+' 请填写数字') from None
            if not isfinite(value):raise ValueError(field.label+' 必须是有限数字')
        else:
            if not isinstance(value,str) or len(value)>512 or any(ord(c)<32 for c in value):
                raise ValueError(field.label+' 格式无效')
            value=value.strip()
            if not value and not field.optional:raise ValueError('请填写 '+field.label)
        result[field.name]=value
    for key in ('af_timeout_s','interval_um','exposure_value'):
        if key in result and result[key]<=0:raise ValueError(key+' 必须为正数')
    if operation.key=='set_af_range' and result['low_um']>=result['high_um']:
        raise ValueError('AF 下限必须小于上限')
    if operation.key=='set_zstack' and result['first_um']==result['last_um']:
        raise ValueError('Z-stack 首层与末层不能相同')
    if result.get('output_name'):
        name=result['output_name']
        reserved={'CON','PRN','AUX','NUL','CLOCK$'}|{f'COM{i}' for i in range(1,10)}|{f'LPT{i}' for i in range(1,10)}
        if (any(c in name for c in '/\\:*?"<>|') or name in ('.','..') or name.endswith(('.',' '))
                or name.split('.')[0].upper() in reserved):
            raise ValueError('输出名为 Windows 保留名或包含非法字符')
    return result


class HardwareLibrary:
    """A plain adapter, callable without Workbench/Tk/angle/mark acquisition."""
    def __init__(self, device):self.device=device

    @property
    def connected(self):
        return bool(getattr(self.device,'connected',False))

    def execute(self, operation, values):
        if operation.key=='connect':
            self.device.connect_transport()
            return {'channel_ready':True,'authentication_verified':False,'note':'通道就绪；请用独立只读调用验证认证与服务，不代表设备业务就绪'}
        if operation.key=='disconnect':
            self.device.close();return {'channel_ready':False,'physical_stop_verified':False}
        if not self.connected:raise ValueError('请先连接通信；连接不自动调用任何位置或实验接口')
        if operation.xy_required and not getattr(self.device,'xy_enabled',True):
            raise ValueError('当前无 XY 模式，禁止此 XY 请求')
        return self.device.call_hardware_operation(operation.key,values)


def safe(value):
    if value is None or isinstance(value,(bool,int)):return value
    if isinstance(value,float):return value if isfinite(value) else '<non-finite>'
    if isinstance(value,str):return value[:32768]
    if isinstance(value,(list,tuple)):return [safe(v) for v in value[:512]]
    if isinstance(value,dict):return {str(k):safe(v) for k,v in value.items()}
    return '<'+type(value).__name__+'>'


class HardwareTestSession:
    """Serialises commands and records BEGIN/RESULT even for failed validation."""
    def __init__(self, root):
        self.root=Path(root)
        self.session_id=uuid4().hex
        self.directory=self.root/'hardware_logs'/self.session_id
        self.directory.mkdir(parents=True)
        self.log_path=self.directory/'session.log'
        self.jsonl_path=self.directory/'session.jsonl'
        self._gate=Lock();self._log_gate=Lock()
        self.library=None;self.simulated=False;self.running_id=None

    def bind(self, device, *, simulated):
        if not self._gate.acquire(blocking=False):raise RuntimeError('已有单步调用')
        try:self.library=HardwareLibrary(device);self.simulated=simulated
        finally:self._gate.release()

    def append(self, event):
        payload=json.dumps(safe(event),ensure_ascii=False,allow_nan=False)
        with self._log_gate:
            for path,content in ((self.jsonl_path,payload+'\n'),(self.log_path,json.dumps(safe(event),ensure_ascii=False,indent=2,allow_nan=False)+'\n\n')):
                with path.open('a',encoding='utf-8') as stream:
                    stream.write(content);stream.flush();os.fsync(stream.fileno())

    def note_stop_request(self):
        self.append({'event':'stop_requested','at':utc_now(),'operation_id':self.running_id,
                     'note':'仅请求停止等待/后续动作；同步 RPC 可能仍在执行，不代表设备停止'})

    def execute(self, key, parameters=None, *, confirmed=False, timeout_s=60, stop=None):
        if not self._gate.acquire(blocking=False):raise RuntimeError('已有单步调用，禁止并发')
        identity=uuid4().hex;self.running_id=identity;started=monotonic()
        operation=OPERATIONS.get(key)
        allowed={} if operation is None else {f.name:(parameters or {}).get(f.name,'') for f in operation.fields} if isinstance(parameters,dict) else {}
        report={'schema_version':1,'software_version':'2.4.0','session_id':self.session_id,
                'operation_id':identity,'operation':key if operation else 'unknown','title':operation.title if operation else '未知操作',
                'started_at':utc_now(),'simulated':self.simulated,'parameters':safe(allowed),'timeout_s':safe(timeout_s),
                'parameter_labels':{f.name:f.label for f in operation.fields} if operation else {},
                'writes':bool(operation and operation.writes),'may_move':bool(operation and operation.may_move),
                'recovery_required':False,'rpc_trace':[]}
        run=None;old_timeout=None;had_timeout=False;device=None
        try:
            self.append({**report,'event':'begin'})  # No hardware request before durable logging.
            if operation is None:raise ValueError('未知硬件操作')
            p=validate(operation,parameters or {})
            timeout=float(timeout_s)
            if not isfinite(timeout) or timeout<=0:raise ValueError('RPC 超时必须为有限正数（秒）')
            if self.library is None:raise ValueError('请先创建连接或载入离线模拟')
            device=self.library.device
            had_timeout=hasattr(device,'_timeout');old_timeout=getattr(device,'_timeout',None)
            if hasattr(device,'clear_rpc_trace'):device.clear_rpc_trace()
            if operation.writes and not operation.is_stop and confirmed is not True:
                raise ValueError('本次写入/动作尚未人工确认；未发送设备请求')
            if operation.writes and not operation.is_stop and pending_recovery(self.root,simulated=self.simulated):
                raise RuntimeError('存在待确认动作；可继续只读检查或请求 Stop，确认设备结束后再执行写入')
            if stop and stop.is_set():raise InterruptedError('请求已取消，未发送设备请求')
            if operation.may_move:
                run=self.root/('demo' if self.simulated else 'field')/identity
                run.mkdir(parents=True)
                write_json(run/'status.json',{'kind':'hardware_test','state':'running','recovery_required':True,
                                              'result_valid':False,'started_at':report['started_at'],'failure_stage':operation.title})
                report['recovery_required']=True
            device._timeout=timeout
            report['result']=safe(self.library.execute(operation,p))
            report['status']='success'
            report['note']='RPC/逻辑操作已返回，不等于物理动作完成；不会自动执行下一步'
        except Exception as error:
            report['status']='failed'
            diagnostic=diagnostic_from_error(error)
            report['error']=diagnostic or {'code':type(error).__name__,
                'message':str(error) or '调用或日志失败，请检查接口与日志目录'}
            report['client_stack']=[f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in traceback.extract_tb(error.__traceback__)]
        finally:
            if device is not None:
                if had_timeout:device._timeout=old_timeout
                elif hasattr(device,'_timeout'):del device._timeout
                if hasattr(device,'rpc_trace'):report['rpc_trace']=device.rpc_trace()
            report['finished_at']=utc_now();report['duration_ms']=round((monotonic()-started)*1000,3)
            report['stop_requested']=bool(stop and stop.is_set())
            try:
                if run is not None:
                    write_json(run/'hardware_operation.json',report)
                    write_json(run/'status.json',{'kind':'hardware_test','state':'complete' if report['status']=='success' else 'failed',
                        'recovery_required':True,'result_valid':False,'started_at':report['started_at'],
                        'failure_stage':report['title'],'operation_id':identity})
                self.append({**report,'event':'result'})
            except (OSError,ValueError):
                report['log_error']='日志写入失败；若设备调用已发生，请现场确认状态并保留界面结果'
                report['status']='failed'
            self.running_id=None;self._gate.release()
        return report
