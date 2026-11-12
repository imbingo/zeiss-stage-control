import gc
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from zen_gui import Workbench
from zen_hardware_gui import HardwareTestWindow


class HardwareWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=tk.Tk();self.root.withdraw()
        self.app=Workbench(self.root,Path(self.tmp.name))
        self.dialog=HardwareTestWindow(self.app)
        self.app.hardware_dialog=self.dialog
        self.root.update()

    def tearDown(self):
        self.dialog.destroy()
        for task in self.root.tk.call("after", "info"):self.root.after_cancel(task)
        self.root.destroy()
        self.dialog=self.app=self.root=None;gc.collect();self.tmp.cleanup()

    def test_open_and_bind_do_not_connect(self):
        self.assertIsNone(self.dialog.session.library)
        self.dialog.bind(True)
        self.assertFalse(self.dialog.session.library.connected)
        self.assertEqual(self.app.workflow.device.lab_calls,[])

    def test_preferences_exclude_live_id_and_confirmation(self):
        self.dialog.values['experiment_id']=tk.StringVar(self.dialog,value='live-id')
        self.dialog.values['experiment_name']=tk.StringVar(self.dialog,value='test experiment')
        self.dialog.approved.set(True);self.dialog.save_preferences()
        saved=self.app.preferences.data['hardware_test']
        self.assertEqual(saved['experiment_name'],'test experiment')
        self.assertNotIn('experiment_id',saved);self.assertNotIn('approved',saved)

    def test_result_updates_id_and_copy(self):
        self.dialog.show_result({'title':'Load','status':'success','operation':'load_experiment','result':{'experiment_id':'abc'},'writes':True})
        self.assertEqual(self.dialog.values['experiment_id'].get(),'abc')
        self.dialog.copy_latest()
        self.assertIn('abc',self.dialog.clipboard_get())

    def test_execute_through_workflow_and_render_log(self):
        import time
        self.dialog.bind(True)
        self.dialog.tree.selection_set('connect');self.dialog.select();self.dialog.run()
        deadline=time.monotonic()+3
        while self.app.workflow.busy and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.app.workflow.busy)
        self.app.poll()
        self.assertEqual(self.dialog.latest['status'],'success')
        self.assertTrue(self.dialog.session.library.connected)
        self.assertIn('channel_ready',self.dialog.log.get('1.0','end'))
        self.assertEqual(self.app.workflow.device.lab_calls,[])

if __name__=='__main__':unittest.main()
