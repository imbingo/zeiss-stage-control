"""Whole-task motion ordering, fail-closed recovery and independent hole paths."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest

from zen_fit import DEMO_CONVENTION,fit_demo,validate_recipe
from zen_hardware.mark_simulator import SimulatedSequenceDevice
from zen_marks import MarkLayer,MarkRecipe
from zen_records import pending_recovery
from zen_sequence import SequencePlan,Station
from zen_workflow import Recipe,Workflow


def plan():
    tilt=Recipe('A','Demo AF',((0.,0.),(.0005,0.),(0.,.0005)),1e-6,2,.5,DEMO_CONVENTION)
    capture=MarkRecipe(MarkLayer('Demo upper',100e-6,5e-6),MarkLayer('Demo lower',200e-6,5e-6),
                       settle_s=.001,arrival_timeout_s=.5,use_autofocus=False,capture_kind='confocal')
    return SequencePlan('A角度+B孔位',tilt,(Station('via',.001,.001),Station('H1',.002,.0015,capture),
                        Station('H2',.0025,.0015,capture)),20e-6,100e-6,0.,300e-6,10000.,10e-6,
                        settle_s=.001,simulated_configuration=True)


class TracedDevice(SimulatedSequenceDevice):
    def __init__(self,**kw):super().__init__(**kw);self.trace=[]
    def move_xy(self,x,y):self.trace.append(('xy',x,y,self.z));return super().move_xy(x,y)
    def move_z(self,z):self.trace.append(('z',z));return super().move_z(z)
    def capture_frame(self,*a,**kw):
        self.trace.append(('capture',self.xy,self.z));return super().capture_frame(*a,**kw)


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'data';self.device=TracedDevice()
        self.device.connect()
        self.workflow=Workflow(self.device,self.root,simulated=True,validate=validate_recipe,fit=fit_demo)
        self.workflow.state='就绪'

    def wait(self):
        deadline=time.monotonic()+10
        while self.workflow.busy and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.workflow.busy)

    def start(self,p=None):
        self.workflow.start_sequence(p or plan(),self.base/'images',confirmed=True,
                                     experiment_loading_confirmed=True,confirmed_start=(0.,0.),session={'sample':'S1'})
        self.wait()
        return self.workflow.sequence_report

    def test_complete_independent_regions_all_xy_at_safe_z_and_final_safe(self):
        report=self.start()
        self.assertEqual(report['state'],'complete',report.get('error'))
        self.assertEqual(self.device.capture_count,4)
        self.assertEqual(self.device.xy,(.0025,.0015));self.assertAlmostEqual(self.device.z,20e-6)
        for item in self.device.trace:
            if item[0]=='xy':self.assertAlmostEqual(item[3],20e-6)
        self.assertEqual(self.device.moves[:3],list(plan().tilt.targets))
        self.assertEqual(self.device.moves[3:],[(s.x_m,s.y_m) for s in plan().stations])
        for capture in report['captures']:
            out=Path(capture['output_directory'])
            self.assertTrue((out/'upper.tif').exists());self.assertTrue((out/'lower.tif').exists())
            data=json.loads((out/'capture.json').read_text())
            self.assertTrue(data['status']['pair_complete'])
            self.assertEqual(data['parent_run_id'],self.workflow.sequence_run_path.name)
        self.assertFalse(pending_recovery(self.root,simulated=True))
        from zen_qt.pages.records import scan_runs
        runs=scan_runs(self.root)
        self.assertEqual(len(runs),1);self.assertEqual(runs[0]['kind'],'sequence');self.assertEqual(runs[0]['n_images'],4)

    def test_angle_gate_blocks_holes_without_unnecessary_recovery(self):
        report=self.start(replace(plan(),max_abs_angle_urad=1.))
        self.assertEqual(report['state'],'failed')
        self.assertEqual(self.device.capture_count,0)
        self.assertEqual(len(self.device.moves),3)
        self.assertFalse(pending_recovery(self.root,simulated=True))
        self.assertTrue((self.workflow.sequence_run_path/'angle_result.json').exists())

    def test_lower_failure_preserves_upper_and_locks_without_return(self):
        self.device.mark_fault='lower_capture'
        report=self.start()
        self.assertEqual(report['state'],'failed');self.assertEqual(self.workflow.state,'设备状态待确认')
        self.assertEqual(self.device.xy,(.002,.0015));self.assertAlmostEqual(self.device.z,200e-6)
        out=Path(report['captures'][0]['output_directory'])
        self.assertTrue((out/'upper.tif').exists());self.assertFalse((out/'lower.tif').exists())
        self.assertTrue(pending_recovery(self.root,simulated=True))
        with self.assertRaises(RuntimeError):self.start()

    def test_stop_after_first_xy_prevents_af_capture_and_return(self):
        original=self.device.move_xy
        def stop(x,y):original(x,y);self.workflow.stop()
        self.device.move_xy=stop
        report=self.start()
        self.assertEqual(report['state'],'stopped');self.assertEqual(self.device.af_attempts,0)
        self.assertEqual(len(self.device.moves),1);self.assertEqual(self.device.capture_count,0)
        self.assertTrue(pending_recovery(self.root,simulated=True))

    def test_mutual_exclusion_for_entire_sequence_and_restart_pending_lock(self):
        from threading import Event
        entered,release=Event(),Event();original=self.device.move_xy
        def hold(x,y):entered.set();release.wait(3);return original(x,y)
        self.device.move_xy=hold
        self.workflow.start_sequence(plan(),self.base/'images',confirmed=True,experiment_loading_confirmed=True,confirmed_start=(0.,0.))
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(RuntimeError):self.workflow.read_only('position',lambda d:d.read_xy())
            self.assertTrue(pending_recovery(self.root,simulated=True))
        finally:self.workflow.stop();release.set();self.wait()

    def test_z_timeout_never_starts_xy(self):
        self.device.move_z=lambda z:None
        report=self.start(replace(plan(),tilt=replace(plan().tilt,arrival_timeout=.06)))
        self.assertEqual(report['state'],'failed');self.assertEqual(self.device.moves,[])
        self.assertTrue(pending_recovery(self.root,simulated=True))

    def test_experiment_restores_position_aborts_before_any_xy(self):
        original=self.device.load_experiment
        def load(name):original(name);self.device.xy=(.005,.005)
        self.device.load_experiment=load
        report=self.start();self.assertEqual(report['state'],'failed')
        self.assertEqual(self.device.moves,[]);self.assertEqual(self.device.capture_count,0)

    def test_invalid_bounds_no_xy_approvals_demo_source_and_output_rejected(self):
        for p in (replace(plan(),safe_z_m=500e-6),replace(plan(),stations=(plan().stations[0],)),
                  replace(plan(),max_rms_m=float('nan'))):
            with self.assertRaises(ValueError):self.start(p)
        with self.assertRaises(ValueError):self.workflow.start_sequence(plan(),self.base/'images')
        with self.assertRaises(ValueError):self.workflow.start_sequence(plan(),self.root/'images',confirmed=True,experiment_loading_confirmed=True,confirmed_start=(0.,0.))
        self.workflow.simulated=False
        with self.assertRaises(ValueError):self.start()
        self.workflow.simulated=True;self.device.xy_enabled=False
        with self.assertRaises(ValueError):self.start()
        self.assertEqual(self.device.moves,[])
