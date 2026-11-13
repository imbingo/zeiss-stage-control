"""Manual hardware tests; widgets never call vendor RPCs directly."""
import json
import os
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from zen_hardware.library import OPERATIONS, HardwareTestSession
from zen_hardware.gateway import GatewayDevice, secure_channel
from zen_hardware.local_config import gateway_settings
from zen_hardware.mark_simulator import SimulatedMarkDevice
from zen_fit import validate_recipe, fit_field


def _wrap(label, pad=4):
    label.bind("<Configure>", lambda event: label.configure(
        wraplength=max(1, event.width - pad)))
    return label


class HardwareTestWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app=app
        self.title('硬件库 · 通信与单步测试')
        self.geometry('1080x700')
        self.session=HardwareTestSession(app.data_root)
        saved=app.preferences.data.get("hardware_test",{})
        self.values={k:tk.StringVar(self,value=v) for k,v in saved.items() if k!="rpc_timeout_s"}
        self.latest=None
        self.op=None
        self.approved=tk.BooleanVar(value=False)
        self.timeout=tk.StringVar(value=saved.get('rpc_timeout_s','60'))
        self.info=tk.StringVar(value='请选择操作。打开窗口不会连接或发送设备指令。')
        self.mode=tk.StringVar(value='未绑定设备')
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.transient(app.root)
        self.grab_set()
        top=ttk.Frame(self,padding=10);top.pack(fill='x')
        ttk.Label(top,textvariable=self.mode).pack(side='left')
        ttk.Button(top,text='绑定现场设备',command=lambda:self.bind(False)).pack(side='left',padx=8)
        ttk.Button(top,text='绑定离线模拟器',command=lambda:self.bind(True)).pack(side='left')
        _wrap(ttk.Label(self,text='无物镜转盘运动按钮。加载/运行实验仍可能换物镜或运动：请先在 ZEN 核对实验。读到位置不代表转盘已锁定。')).pack(fill='x',padx=10)
        body=ttk.Panedwindow(self,orient='horizontal');body.pack(fill='both',expand=True,padx=10,pady=8)
        left=ttk.Frame(body);right=ttk.Frame(body);body.add(left,weight=1);body.add(right,weight=4)
        self.tree=ttk.Treeview(left,show='tree',selectmode='browse',height=18)
        scroll=ttk.Scrollbar(left,orient='vertical',command=self.tree.yview);self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        for op in OPERATIONS.values():
            if not self.tree.exists(op.group):self.tree.insert('', 'end',iid=op.group,text=op.group,open=True)
            self.tree.insert(op.group,'end',iid=op.key,text=('⚠ ' if op.writes else '')+op.title)
        self.tree.bind('<<TreeviewSelect>>',self.select)
        self.description=_wrap(ttk.Label(right));self.description.pack(fill='x')
        self.form=ttk.Frame(right);self.form.pack(fill='x',pady=5)
        timeout=ttk.Frame(right);timeout.pack(fill='x')
        ttk.Label(timeout,text='客户端 RPC 超时 (s)').pack(side='left');ttk.Entry(timeout,textvariable=self.timeout,width=10).pack(side='left')
        ttk.Checkbutton(right,text='本次写入/动作已在设备端确认安全（每次执行后重置）',variable=self.approved).pack(anchor='w')
        actions=ttk.Frame(right);actions.pack(fill='x',pady=5)
        self.run_button=ttk.Button(actions,text='执行选中的单步',command=self.run);self.run_button.pack(side='left')
        ttk.Button(actions,text='请求停止后续任务',command=self.request_stop).pack(side='left',padx=4)
        ttk.Button(actions,text='确认设备恢复',command=self.recover).pack(side='left')
        _wrap(ttk.Label(right,textvariable=self.info)).pack(fill='x')
        log_frame=ttk.Frame(right);log_frame.pack(fill='both',expand=True)
        self.log=tk.Text(log_frame,wrap='word',height=16,state='disabled')
        scrollbar=ttk.Scrollbar(log_frame,command=self.log.yview);self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side='left',fill='both',expand=True);scrollbar.pack(side='right',fill='y')
        tools=ttk.Frame(self,padding=10);tools.pack(fill='x')
        ttk.Button(tools,text='复制本次结果/报错',command=self.copy_latest).pack(side='left')
        ttk.Button(tools,text='复制全部日志',command=self.copy_all).pack(side='left',padx=8)
        ttk.Button(tools,text='导出全部日志',command=self.export).pack(side='left')
        _wrap(ttk.Label(self,text='自动日志：'+str(self.session.directory))).pack(fill='x',padx=10,pady=4)
        if app.workflow and hasattr(app.workflow.device,"call_hardware_operation"):self.attach(app.workflow)
        self.tree.selection_set('read_z');self.select()

    def attach(self,workflow):
        self.session.bind(workflow.device,simulated=workflow.simulated)
        self.mode.set(('离线模拟 · 无真实设备' if workflow.simulated else '现场设备')+' · '+('有 XY' if workflow.xy_enabled else '无 XY'))

    def bind(self,simulated):
        if self.app.workflow and self.app.workflow.busy:return
        try:
            if simulated:device=SimulatedMarkDevice(xy_enabled=self.app.xy_mode.get()!='no_xy')
            else:
                env={**os.environ,**self.app.gateway_overrides}
                address,pem,token=gateway_settings(env)
                allow_v1=env.get("ZEN_ALLOW_FOCUS_V1","").strip().lower() in ("1","true","yes")
                allow_unverified_af=env.get("ZEN_ALLOW_UNVERIFIED_AF","").strip().lower() in ("1","true","yes")
                allow_definite=env.get("ZEN_ALLOW_DEFINITE_FOCUS","").strip().lower() in ("1","true","yes")
                device=GatewayDevice(lambda:secure_channel(address,pem.read_bytes(),token),xy_enabled=self.app.xy_mode.get()!='no_xy',allow_focus_v1=allow_v1,allow_unverified_af=allow_unverified_af,allow_definite_focus=allow_definite)
            self.app._replace_workflow(device,simulated=simulated,validate=validate_recipe,fit=fit_field)
            self.values.pop('experiment_id',None)
            self.attach(self.app.workflow)
            self.info.set('已绑定，尚未连接。请选择“连接通信”。')
            self.select()
        except Exception as exc:
            report={'operation':'bind','title':'绑定设备','status':'failed','simulated':simulated,
                    'error':{'code':type(exc).__name__,'message':str(exc) if isinstance(exc,(ValueError,OSError)) else '绑定失败，请检查本地配置'}}
            try:self.session.append({**report,'event':'binding_error'})
            except OSError:pass
            self.show_result(report)

    def select(self,event=None):
        selected=self.tree.selection()
        if not selected or selected[0] not in OPERATIONS:
            self.op=None
            self.description.configure(text='请在左侧选择一个具体操作（分组条目不可执行）。')
            return
        self.op=OPERATIONS[selected[0]]
        self.approved.set(False)
        for child in self.form.winfo_children():child.destroy()
        self.description.configure(text=self.op.title+'\n'+self.op.help)
        for row,field in enumerate(self.op.fields):
            var=self.values.setdefault(field.name,tk.StringVar(self,value='10' if field.name=='af_timeout_s' else ''))
            ttk.Label(self.form,text=field.label).grid(row=row,column=0,sticky='w',pady=3)
            ttk.Entry(self.form,textvariable=var,width=40).grid(row=row,column=1,sticky='ew')
        self.form.columnconfigure(1,weight=1)

    def run(self):
        workflow=self.app.workflow
        if not workflow or workflow.busy or self.session.library is None:
            self.info.set('请先绑定设备，或等待当前任务结束。');return
        if self.op is None:
            self.info.set('请先在左侧选择一个具体操作。');return
        try:
            timeout=float(self.timeout.get())
            if not 0<timeout<=600:raise ValueError
        except (ValueError,TypeError):
            self.info.set('RPC 超时须为 0–600 秒的数字。');return
        self.save_preferences()
        params={f.name:self.values[f.name].get() for f in self.op.fields}
        confirmed=self.approved.get();self.approved.set(False)
        self.info.set('正在执行 '+self.op.title+'；同步 RPC 可能持续至超时。')
        self.run_button.state(['disabled'])
        try:
            workflow.run_hardware_test(self.session,self.op.key,params,confirmed=confirmed,timeout_s=timeout)
        except Exception as exc:
            self.run_button.state(['!disabled'])
            self.info.set('未能提交单步：'+str(exc))

    def show_result(self,report):
        self.latest=report
        self.run_button.state(['!disabled'])
        self.info.set(report['title']+' · '+report['status']+' · '+('设备状态须人工确认' if report.get('recovery_required') else '详见日志'))
        self.log.configure(state='normal');self.log.insert('end',json.dumps(report,ensure_ascii=False,indent=2)+'\n\n');self.log.see('end');self.log.configure(state='disabled')
        if report['operation'] in ('connect','disconnect') and 'experiment_id' in self.values:
            self.values['experiment_id'].set('')
        result=report.get('result',{})
        if report['status']=='success' and report['operation'] in ('load_experiment','clone_experiment') and result.get('experiment_id'):
            self.values.setdefault('experiment_id',tk.StringVar(self)).set(result['experiment_id'])
        if report.get('writes'):
            self.app.field_verified=False;self.app.clear_result()

    def request_stop(self):
        if self.app.workflow:self.app.workflow.stop()
        self.session.note_stop_request()
        self.info.set('已请求停止后续步骤；不能取消正在等待的同步 RPC。请在设备端确认停止，必要时使用硬件急停。')

    def recover(self):
        if self.app.workflow and self.app.workflow.busy:return
        from zen_records import pending_recovery, confirm_recovery
        if self.session.simulated:
            runs=pending_recovery(self.app.data_root,simulated=True)
            if not runs:return
            if not messagebox.askyesno('演示恢复','模拟确认 Z、AF、采集及 XY（如有）均已结束？',parent=self):return
            try:
                if self.app.workflow and self.app.workflow.simulated:
                    self.app.workflow.device.confirm_simulated_recovery()
                for run in runs:confirm_recovery(run,xy_confirmed=True,af_z_confirmed=True)
                self.app.recovery_runs=pending_recovery(self.app.data_root,simulated=True)
            except OSError:
                self.info.set('恢复记录保存失败，请检查日志目录。');return
        else:
            self.app.field_recovery_runs=pending_recovery(self.app.data_root,simulated=False)
            self.app.recover_field(parent=self)
        self.info.set('已处理恢复确认；实验 ID 沿用当前连接，业务采集前须重新校验。')

    def copy_latest(self):
        if self.latest:self.clipboard_clear();self.clipboard_append(json.dumps(self.latest,ensure_ascii=False,indent=2))

    def copy_all(self):
        if self.session.log_path.exists():self.clipboard_clear();self.clipboard_append(self.session.log_path.read_text(encoding='utf-8'))

    def export(self):
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.log',initialfile='hardware-test.log')
        if path:
            try:
                from pathlib import Path
                Path(path).write_text(self.session.log_path.read_text(encoding='utf-8') if self.session.log_path.exists() else '尚无操作记录\n',encoding='utf-8')
            except OSError as exc:messagebox.showerror('导出失败',str(exc),parent=self)

    def save_preferences(self):
        self.app.preferences.update(hardware_test={**{k:v.get() for k,v in self.values.items() if k!="experiment_id"},"rpc_timeout_s":self.timeout.get()})

    def close(self):
        if self.app.workflow and self.app.workflow.busy:
            self.info.set('当前调用尚未结束，请等待返回并确认设备状态。');return
        self.save_preferences()
        self.destroy()
