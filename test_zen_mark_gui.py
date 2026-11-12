"""Windows UI acceptance for current-position paired mark capture."""

import json
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
        for task in self.root.tk.call('after', 'info'):
            self.root.after_cancel(task)
        self.root.destroy()

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
        self.dialog.capture(('upper', 'lower'))
        self.assertIn('演示配置不能', self.dialog.message.get())
        self.assertEqual(self.app.workflow.device.z_moves, [])
