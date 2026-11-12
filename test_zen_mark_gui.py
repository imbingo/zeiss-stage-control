"""Windows UI acceptance for current-position paired mark capture."""

import json
import gc
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from zen_gui import Workbench
from zen_mark_gui import MarkCaptureDialog


class MarkGuiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.cleanup)
        self.base = Path(self.temp.name)
        self.app = Workbench(self.root, self.base / 'records')
        self.app.open_mark_capture()
        self.dialog = self.app.mark_dialog
        self.dialog.output.set(str(self.base / 'images'))

    def cleanup(self):
        if self.app.workflow and self.app.workflow.busy:
            self.app.stop()
            deadline=time.monotonic()+3
            while self.app.workflow.busy and time.monotonic()<deadline:time.sleep(.01)
        if self.dialog.winfo_exists():
            self.dialog.destroy()
        for task in self.root.tk.call('after', 'info'):
            self.root.after_cancel(task)
        self.root.destroy()
        self.root = None
        self.dialog = None
        self.app = None
        gc.collect()

    def wait(self, condition):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.root.update()
            if condition():return
            time.sleep(.02)
        self.fail('UI did not reach expected state: '+self.dialog.message.get())

    def demo(self):
        self.dialog.load_demo()
        self.wait(lambda:not self.app.workflow.busy and self.app.workflow.state=='就绪')

    def test_offline_demo_reads_z_and_saves_pair(self):
        self.assertEqual(self.dialog.fields['upper']['z_m'].get(), '')
        self.dialog.capture(('upper', 'lower'))
        self.assertIn('请先连接', self.dialog.message.get())
        self.demo()
        self.app.workflow.device.z = 101e-6
        self.dialog.read_z('upper')
        self.wait(lambda:self.dialog.fields['upper']['z_m'].get()=='101.000000')
        self.dialog.approved.set(True)
        self.dialog.loading_approved.set(True)
        self.dialog.capture(('upper', 'lower'))
        self.wait(lambda:not self.app.workflow.busy and self.app.workflow.mark_output_path
                  and (self.app.workflow.mark_output_path/'lower.tif').exists())
        self.assertEqual(self.app.workflow.state, '就绪')
        document=json.loads((self.app.workflow.mark_output_path/'capture.json').read_text(encoding='utf-8'))
        self.assertTrue(document['simulated'])
        self.assertTrue(document['status']['pair_complete'])
        self.assertFalse(self.app.field_verified)
        self.assertEqual(self.app.workflow.device.moves, [])

    def test_parameter_changes_require_new_confirmation_and_overlap_is_rejected(self):
        self.demo()
        self.dialog.approved.set(True)
        self.dialog.fields['lower']['z_m'].set('102')
        self.assertFalse(self.dialog.approved.get())
        self.dialog.approved.set(True)
        self.dialog.loading_approved.set(True)
        self.dialog.capture(('upper', 'lower'))
        self.assertIn('不能重叠', self.dialog.message.get())
        self.assertEqual(self.app.workflow.device.z_moves, [])
        self.dialog.fields['lower']['z_m'].set('200')
        self.dialog.capture(('upper', 'lower'))
        self.assertIn('请先确认', self.dialog.message.get())
        self.assertEqual(self.app.workflow.device.capture_count, 0)

    def test_configuration_roundtrip_retains_demo_origin_without_authorizing_motion(self):
        self.demo()
        path=self.base/'config.json'
        with patch('zen_mark_gui.filedialog.asksaveasfilename', return_value=str(path)):
            self.dialog.save_config()
        data=json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(data['simulated_configuration'])
        self.dialog.fields['upper']['z_m'].set('999')
        self.dialog.approved.set(True)
        with patch('zen_mark_gui.filedialog.askopenfilename', return_value=str(path)):
            self.dialog.load_config()
        self.assertEqual(self.dialog.fields['upper']['z_m'].get(), '100')
        self.assertFalse(self.dialog.approved.get())
        self.assertTrue(self.dialog.demo_config)
        self.app.workflow.simulated=False  # Model selecting a real connection.
        self.dialog.approved.set(True)
        self.dialog.loading_approved.set(True)
        self.dialog.capture(('upper', 'lower'))
        self.assertIn('演示配置不能', self.dialog.message.get())
        self.assertEqual(self.app.workflow.device.z_moves, [])

        refresh_job = self.dialog._refresh_job
        self.dialog.close()
        self.assertFalse(self.dialog.winfo_exists())
        self.assertNotIn(refresh_job, self.root.tk.call('after', 'info'))

    def test_no_xy_mode_disables_angle_and_saves_confocal_tiff(self):
        self.app.xy_mode.set('no_xy');self.app.change_device_mode()
        self.demo()
        self.assertFalse(self.app.workflow.xy_enabled)
        self.assertFalse(self.app.field_ready())
        self.assertEqual(str(self.app.field_af.cget('state')), 'disabled')
        self.assertEqual(self.app.connect_button.cget('text'), 'Gateway 只读 Z')
        self.dialog.use_af.set(False)
        self.dialog.approved.set(True)
        self.dialog.loading_approved.set(True)
        self.dialog.capture(('upper',))
        self.wait(lambda:not self.app.workflow.busy and self.app.workflow.mark_output_path
                  and (self.app.workflow.mark_output_path/'upper.tif').exists())
        document=json.loads((self.app.workflow.mark_output_path/'capture.json').read_text(encoding='utf-8'))
        self.assertFalse(document['xy_verified'])
        self.assertIsNone(document['start_xy_m'])
        self.assertEqual(document['recipe']['capture_kind'], 'confocal')
        self.assertEqual(self.app.workflow.device.af_attempts, 0)

    def test_read_only_report_can_be_saved_without_authorizing_capture(self):
        self.demo()
        self.dialog.inspect_experiments()
        self.wait(lambda:self.dialog._rpc_report is not None)
        report=self.dialog._rpc_report
        self.assertEqual(report['kind'], 'read_only_experiment_check')
        self.assertEqual(report['reported_zen_version'], '3.12')
        self.assertFalse(report['device_actions_performed'])
        self.assertEqual(self.app.workflow.device.z_moves, [])
        self.assertEqual(self.app.workflow.device.capture_count, 0)
        self.assertFalse(self.dialog.loading_approved.get())
        path=self.base/'diagnostic.json'
        with patch('zen_mark_gui.filedialog.asksaveasfilename', return_value=str(path)):
            self.dialog.save_diagnostic()
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['kind'], 'read_only_experiment_check')

    def test_parameters_restore_automatically_but_approvals_and_connection_do_not(self):
        self.app.xy_mode.set('no_xy');self.app.change_device_mode()
        self.dialog.fields['upper']['experiment'].set('PLLOW扫描')
        self.dialog.fields['upper']['z_m'].set('1000')
        self.dialog.fields['upper']['half_range_m'].set('5')
        self.dialog.use_af.set(False)
        self.dialog.approved.set(True)
        self.dialog.loading_approved.set(True)
        self.dialog.close()
        from zen_preferences import Preferences
        self.app.preferences=Preferences(self.app.preferences.path)
        self.app.open_mark_capture();self.dialog=self.app.mark_dialog
        self.assertEqual(self.dialog.fields['upper']['experiment'].get(), 'PLLOW扫描')
        self.assertEqual(self.dialog.fields['upper']['z_m'].get(), '1000')
        self.assertEqual(self.app.xy_mode.get(), 'no_xy')
        self.assertFalse(self.dialog.use_af.get())
        self.assertFalse(self.dialog.approved.get())
        self.assertFalse(self.dialog.loading_approved.get())
        self.assertIsNone(self.app.workflow)
        self.assertIn('历史值', self.dialog.message.get())
