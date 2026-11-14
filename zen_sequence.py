"""Fixed, device-independent tilt -> safe transfer -> paired capture executor."""
from dataclasses import asdict, dataclass
from math import isfinite
from pathlib import Path
from time import monotonic
from uuid import uuid4

from zen_acquisition import acquire, wait_for_arrival
from zen_hardware import Motion
from zen_marks import MarkRecipe, MarkRecords, capture_marks
from zen_records import RunRecords, utc_now, write_json


class SequenceStopped(InterruptedError):
    pass


class SequenceRejected(ValueError):
    """Completed measurement did not pass the configured process limits."""


@dataclass(frozen=True)
class Station:
    name: str
    x_m: float
    y_m: float
    capture: MarkRecipe | None = None  # None = intermediate XY waypoint


@dataclass(frozen=True)
class SequencePlan:
    name: str
    tilt: object
    stations: tuple
    safe_z_m: float
    tilt_z_m: float
    z_min_m: float
    z_max_m: float
    max_abs_angle_urad: float
    max_rms_m: float
    z_tolerance_m: float = .5e-6
    settle_s: float = .2
    simulated_configuration: bool = False

    def validate(self, validate_tilt):
        if not self.name.strip() or not 1 <= len(self.stations) <= 1000:
            raise ValueError('请填写任务名和独立孔位/转场点表（最多 1000 行）')
        validate_tilt(self.tilt)
        numbers=(self.safe_z_m,self.tilt_z_m,self.z_min_m,self.z_max_m,
                 self.max_abs_angle_urad,self.max_rms_m,self.z_tolerance_m,self.settle_s)
        if not all(type(v) in (int,float) and isfinite(v) for v in numbers):
            raise ValueError('Sequence 参数必须是有限数字')
        if self.z_min_m >= self.z_max_m or min(numbers[4:]) <= 0:
            raise ValueError('Z 范围须下限小于上限，容差、阈值与稳定时间须为正数')
        if not all(self.z_min_m <= z <= self.z_max_m for z in (self.safe_z_m,self.tilt_z_m)):
            raise ValueError('安全 Z 与测角起始焦面须在已核对的 Z 范围内')
        names=set();holes=0
        for station in self.stations:
            if not station.name.strip() or station.name in names:
                raise ValueError('孔位/转场点编号不能为空或重复')
            names.add(station.name)
            if not all(isfinite(v) for v in (station.x_m,station.y_m)):
                raise ValueError('孔位 XY 必须为有限绝对坐标')
            if station.capture is not None:
                holes+=1;station.capture.validate()
                if station.capture.xy_mode != 'motorized':
                    raise ValueError('自动任务需要有电动 XY 的采图配方')
                for layer in (station.capture.upper,station.capture.lower):
                    if not all(self.z_min_m <= z <= self.z_max_m for z in layer.bounds):
                        raise ValueError(f'{station.name} 的焦面或 AF 范围超出任务 Z 范围')
        if not holes:
            raise ValueError('至少配置一个上下孔采图点，不能只有转场点')

    def to_dict(self):
        return {'schema_version':1,'kind':'sequence','coordinate_unit':'m',**asdict(self)}


def validate_capabilities(device):
    capabilities=getattr(device,'capabilities',None)
    if capabilities is None:return  # explicit simulator or independently tested adapter
    names=capabilities.services
    if names is None:raise ValueError('自动任务需要已读取的 Gateway 服务清单，请重新连接')
    groups=(('XY',('zen_api.hardware.v1.SimpleStageService','zen_api.lm.hardware.v2.StageService')),
            ('XY 状态',('zen_api.hardware.v1.StageService',)),
            ('实验',('zen_api.acquisition.v1beta.ExperimentService',)),
            ('图像流',('zen_api.acquisition.v1beta.ExperimentStreamingService',)),
            ('测角软件 AF',('zen_api.lm.acquisition.v1.ExperimentSwAutofocusService',
                          'zen_api.lm.acquisition.v1beta.ExperimentSwAutofocusService')))
    for title,choices in groups:
        if not any(name in names for name in choices):raise ValueError('自动任务缺少服务：'+title)


class SequenceDevice:
    """Apply readback guards and safe-Z transfers around the existing adapter.

    The configured limits check commands/readbacks; they are not a hardware
    travel limit and cannot constrain ZEN's own autofocus motion.
    """
    def __init__(self, device, plan, stop):
        self.device,self.plan,self.stop=device,plan,stop
        self.angle_mode=False
        self.optics=device.read_optics()

    def __getattr__(self,name):return getattr(self.device,name)

    def checkpoint(self):
        if self.stop.is_set():raise SequenceStopped('已停止后续任务；不自动移动或返回')

    def guard(self):
        self.checkpoint()
        if self.device.read_optics()!=self.optics:
            raise ValueError('物镜/倍率读回变化，已停止 Sequence')
        z=self.device.read_z()
        if not isfinite(z) or not self.plan.z_min_m <= z <= self.plan.z_max_m:
            raise ValueError('实际 Z 超出任务核对范围，已停止后续动作')
        if self.device.motion_state() is not Motion.IDLE:
            raise ValueError('XY 未空闲，已停止后续动作')
        return self.device.read_xy(),z

    def same_xy(self,expected):
        from math import hypot
        xy=self.device.read_xy()
        if not all(isfinite(v) for v in xy) or hypot(xy[0]-expected[0],xy[1]-expected[1])>self.plan.tilt.tolerance_m:
            raise ValueError('XY 与已确认位置不一致，已停止 Sequence')

    def move_z(self,z):
        xy,_=self.guard()
        if not isfinite(z) or not self.plan.z_min_m <= z <= self.plan.z_max_m:
            raise ValueError('目标 Z 超出任务范围')
        self.checkpoint();self.device.move_z(z)
        deadline=monotonic()+self.plan.tilt.arrival_timeout
        stable=0
        while True:
            self.checkpoint();self.same_xy(xy)
            if self.device.motion_state() is not Motion.IDLE or self.device.read_optics()!=self.optics:
                raise ValueError('Z 转场期间 XY 或光学状态变化')
            actual=self.device.read_z()
            stable=stable+1 if isfinite(actual) and abs(actual-z)<=self.plan.z_tolerance_m else 0
            if stable>=3:break
            if monotonic()>=deadline:raise TimeoutError('Sequence Z 到位确认超时')
            self.stop.wait(.02)
        if self.stop.wait(self.plan.settle_s):self.checkpoint()
        xy_after,z_after=self.guard();self.same_xy(xy)
        if abs(z_after-z)>self.plan.z_tolerance_m:raise ValueError('Z 稳定等待后偏离目标')

    def move_xy(self,x,y):
        self.move_z(self.plan.safe_z_m)
        self.checkpoint();self.device.move_xy(x,y)
        wait_for_arrival(self.device,(x,y),self.plan.tilt.tolerance_m,
                         self.plan.tilt.arrival_timeout,self.stop)
        _,z=self.guard()
        if abs(z-self.plan.safe_z_m)>self.plan.z_tolerance_m:
            raise ValueError('XY 转场期间 Z 偏离安全位置')

    def _settings(self,action):
        xy,z=self.guard()
        result=action()
        _,after=self.guard();self.same_xy(xy)
        if abs(after-z)>self.plan.z_tolerance_m:
            raise ValueError('实验加载/准备改变了 Z，停止后续任务，请检查实验设置')
        return result

    def load_experiment(self,name):
        return self._settings(lambda:self.device.load_experiment(name))

    def prepare_mark_experiment(self,*a,**kw):
        return self._settings(lambda:self.device.prepare_mark_experiment(*a,**kw))

    def autofocus(self,timeout):
        if self.angle_mode:self.move_z(self.plan.tilt_z_m)
        xy,_=self.guard();self.checkpoint()
        value=self.device.autofocus(timeout)
        _,z=self.guard();self.same_xy(xy)
        if not isfinite(value) or abs(z-value)>self.plan.z_tolerance_m:
            raise ValueError('AF 返回焦面与 Z 回读不一致')
        return value


class SequenceCaptureRecords(MarkRecords):
    """Child capture uses its owning sequence's durable recovery lock."""
    def __init__(self,parent,output,station,index,simulated):
        identity=uuid4().hex
        self.path=parent.path/'captures'/f'{index:04d}'
        self.path.mkdir(parents=True)
        self.output=output/f'hole_{index:04d}'
        self.started=utc_now()
        self.document={'schema_version':1,'kind':'mark_capture','simulated':simulated,
                       'started_at':self.started,'run_id':identity,'parent_run_id':parent.path.name,
                       'station_name':station.name,'recipe':station.capture.to_dict(),
                       'requested_layers':['upper','lower'],'images':{}}
        self.status('running',recovery=True,stage='准备孔位输出目录')
        self.output.mkdir(parents=True)
        write_json(self.path/'recipe.json',self.document)
        self.sync()


def execute_sequence(workflow,plan,output,confirmed_start,session):
    """Runs under Workflow._gate for the entire task, including all child captures."""
    parent=None;child=None;step=None;steps=[];actions_possible=False
    workflow.sequence_run_path=None
    workflow.sequence_report=None
    workflow.result=None;workflow.result_path=None
    output=Path(output).expanduser().resolve()
    report={'kind':'sequence','simulated':workflow.simulated,'plan':plan.to_dict(),
            'steps':steps,'captures':[],'state':'running','result':None}
    def persist():
        workflow.sequence_report=report
        if parent:write_json(parent.path/'sequence.json',report)
    def event():
        workflow.events.put(('sequence_progress',dict(report,steps=[dict(s) for s in steps])))
    def checkpoint():
        if workflow._stop.is_set():raise SequenceStopped('已停止后续任务，不自动返回')
    def begin(title):
        nonlocal step
        checkpoint()
        step={'title':title,'state':'running','started_at':utc_now()};steps.append(step)
        parent.log(title);persist();event();workflow._state('自动任务',detail=title)
    def done():
        step['readback']={'xy_m':workflow.device.read_xy(),'z_m':workflow.device.read_z()}
        step.update(state='complete',finished_at=utc_now());persist();event()
    try:
        parent=RunRecords(workflow.root,name=plan.name,experiment=plan.tilt.experiment,
                          targets=plan.tilt.targets,tolerance_m=plan.tilt.tolerance_m,
                          af_timeout=plan.tilt.af_timeout,arrival_timeout=plan.tilt.arrival_timeout,
                          sign_convention=plan.tilt.sign_convention,simulated=workflow.simulated,
                          confirmed_start=confirmed_start)
        workflow.sequence_run_path=parent.path
        write_json(parent.path/'session.json',session)
        write_json(parent.path/'recipe.json',dict(plan.to_dict(),recipe_name=plan.name,
                   point_order=plan.tilt.targets,experiment_name=plan.tilt.experiment,
                   simulated=workflow.simulated))
        image_root=output/f'{"demo_sequence" if workflow.simulated else "sequence"}_{parent.path.name}'
        report['output_directory']=str(image_root)
        image_root.mkdir(parents=True)
        begin('设备与起点检查')
        device=SequenceDevice(workflow.device,plan,workflow._stop)
        xy,_=device.guard();device.same_xy(confirmed_start)
        done()
        begin('测角路径采集')
        rows=[]
        def row(value):parent.append(value);rows.append(value);workflow.events.put(('row',value))
        def progress(stage,index):
            checkpoint();parent.log(stage,index);workflow._state('自动任务',index,'测角 · '+stage)
        actions_possible=True
        device.angle_mode=True
        acquire(device,plan.tilt.targets,plan.tilt.experiment,plan.tilt.tolerance_m,
                plan.tilt.af_timeout,plan.tilt.arrival_timeout,workflow._stop,row,progress,
                confirmed_start=confirmed_start)
        device.angle_mode=False;done()
        begin('测角结果放行')
        result=workflow.fit(tuple(rows))
        if not all(isfinite(result[k]) for k in ('rx_urad','ry_urad','rms_m')):
            raise ValueError('测角计算返回无效结果')
        report['result']=result
        write_json(parent.path/'angle_result.json',result)
        if (max(abs(result['rx_urad']),abs(result['ry_urad']))>plan.max_abs_angle_urad
                or result['rms_m']>plan.max_rms_m):
            raise SequenceRejected('测角结果超出放行阈值，未执行孔位转场')
        done()
        for index,station in enumerate(plan.stations,1):
            child=None
            begin(f'{station.name} · 安全 Z 转场与 XY 到位')
            device.move_xy(station.x_m,station.y_m);done()
            if station.capture is None:continue
            begin(f'{station.name} · 上下孔采图')
            child=SequenceCaptureRecords(parent,image_root,station,index,workflow.simulated)
            report['captures'].append({'station':station.name,'record_path':str(child.path),
                                       'output_directory':str(child.output),'state':'running'})
            persist()
            def mark_progress(detail):
                checkpoint();parent.log(station.name+' · '+detail)
                workflow._state('自动任务',index,station.name+' · '+detail)
            capture_marks(device,station.capture,('upper','lower'),workflow._stop,child,mark_progress)
            child.document['operator_experiment_loading_confirmed']=True
            child.document['rpc_trace']=workflow.device.rpc_trace() if hasattr(workflow.device,'rpc_trace') else []
            child.finish('complete',recovery=False)
            report['captures'][-1]['state']='complete';done();child=None
        begin('返回安全 Z')
        device.move_z(plan.safe_z_m);done()
        report.update(state='complete',finished_at=utc_now(),recovery_required=False)
        persist()
        write_json(image_root/'sequence.json',report)
        parent.finish('complete',recovery_required=False,result=result)
        workflow.result=result;workflow.result_path=parent.path
        workflow.events.put(('result',result,parent.path));event()
        workflow._state('完成',detail='Sequence 完成；停留在最后孔位 XY、安全 Z')
    except Exception as error:
        from zen_hardware.rpc_diagnostics import diagnostic_from_error
        stopped=workflow._stop.is_set() or isinstance(error,InterruptedError)
        recovery=actions_possible and not isinstance(error,SequenceRejected)
        from zen_acquisition import AcquisitionFailed
        detail=(error.stage+' · '+workflow._mark_hint(error.__cause__)
                if isinstance(error,AcquisitionFailed) and error.__cause__ else workflow._mark_hint(error))
        report.update(state='stopped' if stopped else 'failed',finished_at=utc_now(),
                      recovery_required=recovery,error=diagnostic_from_error(error) or
                      {'code':type(error).__name__,'message':detail})
        if step and step['state']=='running':step.update(state=report['state'],error=detail)
        if child:
            try:
                child.finish(report['state'],recovery=recovery,stage=detail)
                report['captures'][-1]['state']=report['state']
            except OSError:pass
        try:
            persist()
            if parent:parent.finish(report['state'],recovery_required=recovery,stage=detail)
        except OSError:
            recovery=True  # initial running status remains a durable lock
        event()
        workflow._state('设备状态待确认' if recovery else '已中止',detail=detail)
    finally:
        if hasattr(workflow.device,'rpc_trace'):
            report['rpc_trace']=workflow.device.rpc_trace()
            try:persist()
            except OSError:pass
        if 'image_root' in locals() and image_root.is_dir():
            try:write_json(image_root/'sequence.json',report)
            except OSError:pass
        workflow._save_capabilities()
