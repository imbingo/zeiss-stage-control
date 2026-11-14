"""Fixed, device-independent tilt -> safe transfer -> paired capture executor."""
from dataclasses import asdict, dataclass
from math import isfinite
from pathlib import Path
from uuid import uuid4

from zen_acquisition import acquire
from zen_hardware.interlock import MotionInterlock, SequenceStopped, enforces_safe_z
from zen_marks import MarkRecipe, MarkRecords, capture_marks
from zen_records import (RunRecords, canonical_sha256, software_version,
                         utc_now, write_json, write_manifest)


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


class SequenceDevice(MotionInterlock):
    """Sequence policy on top of the shared interlock.

    All safety behaviour lives in MotionInterlock (zen_hardware/interlock.py),
    so a second executor cannot accidentally get a device without safe Z.
    """

    def __init__(self, device, plan, stop):
        super().__init__(device,
                         safe_z_m=plan.safe_z_m, z_min_m=plan.z_min_m,
                         z_max_m=plan.z_max_m, z_tolerance_m=plan.z_tolerance_m,
                         xy_tolerance_m=plan.tilt.tolerance_m,
                         arrival_timeout_s=plan.tilt.arrival_timeout,
                         settle_s=plan.settle_s, stop=stop,
                         tilt_z_m=plan.tilt_z_m)
        self.plan = plan


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
        self.status('running', recovery=True, stage='准备孔位输出目录')
        self.output.mkdir(parents=True)
        write_json(self.path/'recipe.json',self.document)
        self.sync()


def build_snapshot(plan, confirmed_start, session, preflight, attestations):
    """Freeze a complete copy, not a reference to editable UI state."""
    document = {'kind':'sequence_snapshot','model_version':'1.1',
                'software_version':software_version(),'frozen_at':utc_now(),
                'plan':plan.to_dict(),'confirmed_start_xy_m':list(confirmed_start),
                'session':dict(session or {}),'preflight':dict(preflight or {}),
                'operator_attestations':list(attestations or [])}
    # The hash covers process content only; timestamps/metadata are excluded.
    document['snapshot_sha256'] = canonical_sha256({
        k:v for k,v in document.items()
        if k not in ('frozen_at','software_version')})
    return document


def execute_sequence(workflow,plan,output,confirmed_start,session,*,
                     preflight=None,attestations=None):
    """Runs under Workflow._gate for the entire task, including all child captures."""
    parent=None;child=None;step=None;steps=[];actions_possible=False;device=None
    workflow.sequence_run_path=None
    workflow.sequence_report=None
    workflow.result=None;workflow.result_path=None
    output=Path(output).expanduser().resolve()
    snapshot=build_snapshot(plan,confirmed_start,session,preflight,attestations)
    snapshot_id=snapshot['snapshot_sha256']
    report={'kind':'sequence','model_version':'1.1',
            'software_version':software_version(),'snapshot_sha256':snapshot_id,
            'simulated':workflow.simulated,'plan':plan.to_dict(),
            'steps':steps,'captures':[],'state':'running',
            'disposition':'PENDING','result':None}
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
        write_json(parent.path/'snapshot.json',snapshot)
        if preflight:write_json(parent.path/'preflight.json',preflight)
        if attestations:write_json(parent.path/'attestations.json',{'items':list(attestations)})
        write_json(parent.path/'session.json',dict(session or {},
                   snapshot_sha256=snapshot_id,
                   operator_attestations=list(attestations or [])))
        write_json(parent.path/'recipe.json',dict(plan.to_dict(),recipe_name=plan.name,
                   point_order=plan.tilt.targets,experiment_name=plan.tilt.experiment,
                   simulated=workflow.simulated,snapshot_sha256=snapshot_id,
                   frozen_at=snapshot['frozen_at'],software_version=software_version()))
        image_root=output/f'{"demo_sequence" if workflow.simulated else "sequence"}_{parent.path.name}'
        report['output_directory']=str(image_root)
        image_root.mkdir(parents=True)
        begin('设备与起点检查')
        device=SequenceDevice(workflow.device,plan,workflow._stop)
        if not enforces_safe_z(device):
            raise ValueError('执行器未经过安全互锁层，拒绝移动')
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
        report['limits_used']={'max_abs_angle_urad':plan.max_abs_angle_urad,
                               'max_rms_m':plan.max_rms_m}
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
        report.update(state='complete',finished_at=utc_now(),recovery_required=False,
                      disposition='AWAITING_EXTERNAL_REVIEW')
        persist()
        # Freeze a hash list before writing the summary into the image directory.
        try:
            produced=[p for p in image_root.rglob('*') if p.is_file()]
            manifest=write_manifest(parent.path,run_id=parent.path.name,
                                    files=produced,base=image_root)
            report['manifest_sha256']=manifest['manifest_sha256']
            report['artifact_count']=manifest['artifact_count']
        except OSError:
            report['manifest_sha256']=None
        persist()
        write_json(image_root/'sequence.json',report)
        parent.finish('complete',recovery_required=False,result=result)
        workflow.result=result;workflow.result_path=parent.path
        workflow.events.put(('result',result,parent.path));event()
        workflow._state('完成',detail='Sequence 完成；停留在最后孔位 XY、安全 Z')
    except Exception as error:
        from zen_hardware.rpc_diagnostics import diagnostic_from_error
        stopped=workflow._stop.is_set() or isinstance(error,InterruptedError)
        rejected=isinstance(error,SequenceRejected)
        recovery=actions_possible and not rejected
        from zen_acquisition import AcquisitionFailed
        detail=(error.stage+' · '+workflow._mark_hint(error.__cause__)
                if isinstance(error,AcquisitionFailed) and error.__cause__ else workflow._mark_hint(error))
        report.update(state='stopped' if stopped else 'failed',finished_at=utc_now(),
                      recovery_required=recovery,
                      disposition='MEASUREMENT_REJECTED' if rejected else 'EQUIPMENT_FAULT',
                      error=diagnostic_from_error(error) or
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
            # Keep the fail-closed lock AND make the external report agree with it.
            # Previously only the local variable changed, so image_root's copy
            # could claim recovery_required=false while the centre stayed locked.
            recovery=True
            report['recovery_required']=True
            report['disposition']='EQUIPMENT_FAULT'
            try:persist()
            except OSError:pass
        event()
        workflow._state('设备状态待确认' if recovery else '已中止',detail=detail)
    finally:
        if device is not None and parent:
            try:
                write_json(parent.path/'command_log.json',
                           {'kind':'command_log','run_id':parent.path.name,
                            'snapshot_sha256':snapshot_id,
                            'commands':device.command_log_snapshot()})
            except OSError:pass
        if hasattr(workflow.device,'rpc_trace'):
            report['rpc_trace']=workflow.device.rpc_trace()
            try:persist()
            except OSError:pass
        if 'image_root' in locals() and image_root.is_dir():
            try:write_json(image_root/'sequence.json',report)
            except OSError:pass
        workflow._save_capabilities()
