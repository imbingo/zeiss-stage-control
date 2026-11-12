import json
import tempfile
import unittest
from pathlib import Path
from zen_hardware.library import HardwareTestSession
from zen_hardware.mark_simulator import SimulatedMarkDevice
from zen_records import pending_recovery, confirm_recovery


class HardwareLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.device=SimulatedMarkDevice(xy_enabled=False)
        self.session=HardwareTestSession(self.root);self.session.bind(self.device,simulated=True)
        self.assertEqual(self.run_op('connect')['status'],'success')

    def run_op(self,key,p=None,**kw):return self.session.execute(key,p,**kw)

    def test_connect_does_not_read_or_load(self):
        self.assertEqual(self.device.lab_calls,[])
        self.assertIsNone(self.device._experiment_id)
        self.assertFalse(self.run_op('connect')['result']['authentication_verified'])

    def test_no_xy_never_calls_device(self):
        r=self.run_op('read_xy');self.assertEqual(r['status'],'failed');self.assertEqual(self.device.lab_calls,[])

    def test_move_confirmation_and_recovery(self):
        self.assertEqual(self.run_op('move_z',{'z_um':'100'})['status'],'failed')
        self.assertFalse(self.device.lab_calls)
        r=self.run_op('move_z',{'z_um':'101'},confirmed=True)
        self.assertEqual(r['status'],'success');self.assertAlmostEqual(self.device.z,101e-6)
        self.assertTrue(pending_recovery(self.root,simulated=True))
        self.assertEqual(self.run_op('move_z',{'z_um':102},confirmed=True)['status'],'failed')
        self.assertEqual(self.run_op('read_z')['status'],'success')
        self.assertEqual(self.run_op('stop_z')['status'],'success')

    def test_parameter_operation_no_acquisition(self):
        r=self.run_op('set_zstack',{'experiment_id':'test','first_um':100,'last_um':110,'interval_um':1},confirmed=True)
        self.assertEqual(r['status'],'success');self.assertEqual(self.device.capture_count,0)
        self.assertEqual(self.device.z_moves,[])
        self.assertFalse(pending_recovery(self.root,simulated=True))

    def test_logs_include_failed_validation_and_omit_unknown_secrets(self):
        self.run_op('read_z',{'token':'SECRET'})
        lines=[json.loads(x) for x in self.session.jsonl_path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(lines[-2]['event'],'begin');self.assertEqual(lines[-1]['event'],'result')
        self.assertEqual(lines[-1]['status'],'failed')
        self.assertIn('duration_ms',lines[-1]);self.assertIn('client_stack',lines[-1])
        self.assertNotIn('SECRET',self.session.log_path.read_text(encoding='utf-8'))

    def test_nonfinite_input_rejected(self):
        r=self.run_op('move_z',{'z_um':'nan'},confirmed=True)
        self.assertEqual(r['status'],'failed');self.assertFalse(self.device.lab_calls)

    def test_snap_has_no_hidden_actions(self):
        r=self.run_op('snap',{'experiment_id':'test','output_name':'mark'},confirmed=True)
        self.assertEqual(r['status'],'success');self.assertEqual(self.device.capture_count,1)
        self.assertEqual(self.device.z_moves,[]);self.assertEqual(self.device.prepared,[])
        self.assertEqual(len(self.device.lab_calls),1)

if __name__=='__main__':unittest.main()
