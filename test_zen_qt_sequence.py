"""Actual Qt Sequence controls, persistence and complete simulated task."""
import tempfile
from pathlib import Path
import unittest
from PySide6.QtWidgets import QTableWidgetItem
from test_zen_qt_smoke import _app,_wait
from zen_qt.app import WorkbenchWindow


class QtSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):_app()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'data';self.w=WorkbenchWindow(self.root);self.w.z_timer.stop()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.w.workflow and self.w.workflow.busy:self.w.workflow.stop();_wait(lambda:not self.w.workflow.busy)
        self.w.close();_app().processEvents()

    def demo(self):
        self.w.connect_demo('sequence')
        self.assertTrue(_wait(lambda:not self.w.workflow.busy and self.w.workflow.state=='就绪'))
        return self.w.pages['sequence']

    def check(self,page):
        page.preflight()
        self.assertTrue(_wait(lambda:not self.w.workflow.busy and page._checked is not None),page.status.text())
        page.path_confirm.setChecked(True);page.load_confirm.setChecked(True)
        self.assertTrue(page.start_button.isEnabled())

    def test_whole_sequence_through_page_controls_and_record_archive(self):
        page=self.demo();self.check(page)
        page.start_button.click()
        self.assertTrue(_wait(lambda:not self.w.workflow.busy,20))
        _app().processEvents()
        self.assertEqual(self.w.workflow.state,'完成',page.status.text())
        self.assertEqual(self.w.workflow.device.capture_count,4)
        self.assertGreater(page.steps.rowCount(),5)
        self.assertFalse(page.path_confirm.isChecked());self.assertFalse(page.load_confirm.isChecked())
        self.assertFalse(page.start_button.isEnabled())
        self.w.navigate('records')
        self.assertEqual(self.w.pages['records'].runs[0]['n_images'],4)

    def test_readonly_preflight_and_every_change_invalidate_checks(self):
        page=self.demo();self.check(page)
        self.assertEqual(self.w.workflow.device.moves,[]);self.assertEqual(self.w.workflow.device.z_moves,[])
        page.fields['safe_z_um'].setText('25')
        self.assertIsNone(page._checked);self.assertFalse(page.path_confirm.isChecked())
        self.check(page)
        page.stations.setItem(1,2,QTableWidgetItem('2100'))
        self.assertIsNone(page._checked);self.assertFalse(page.start_button.isEnabled())
        self.check(page)
        self.w.pages['tilt'].apply_recipe(self.w.field_recipe)
        self.assertIsNone(page._checked);self.assertFalse(page.path_confirm.isChecked())

    def test_preferences_keep_draft_not_approvals_and_demo_cannot_be_field(self):
        page=self.demo();self.check(page)
        page.fields['safe_z_um'].setText('unfinished')
        page.persist()
        saved=self.w.preferences.data['sequence']
        self.assertEqual(saved['fields']['safe_z_um'],'unfinished')
        self.assertNotIn('confirmed',saved)
        other=WorkbenchWindow(self.root)
        try:
            p=other.pages['sequence']
            self.assertEqual(p.fields['safe_z_um'].text(),'unfinished')
            self.assertFalse(p.path_confirm.isChecked());self.assertIsNone(p._checked)
            self.assertTrue(p._source_demo)
            self.assertIsNotNone(other.field_recipe)
            self.assertEqual(len(other.field_recipe.targets),4)
        finally:other.close()
        page.new_field()
        self.assertFalse(page._source_demo);self.assertEqual(page.stations.rowCount(),0)

    def test_no_xy_mode_and_device_change_block_launch(self):
        page=self.demo();self.check(page)
        self.w.disconnect();self.assertIsNone(page._checked)
        self.assertFalse(page.start_button.isEnabled())
        self.w.set_xy_mode('no_xy');self.w.connect_demo('sequence')
        self.assertIsNone(self.w.workflow)

    def test_demo_preserves_existing_field_configuration(self):
        page=self.w.pages['sequence']
        page.fields['name'].setText('现场任务')
        page.fields['safe_z_um'].setText('350')
        page.persist()
        self.demo()
        self.assertEqual(self.w.preferences.data['sequence']['fields']['safe_z_um'],'350')
        self.assertFalse(self.w.preferences.data['sequence']['simulated_configuration'])
