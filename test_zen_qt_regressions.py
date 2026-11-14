"""UI behavior regressions: migration, task isolation and actual dialog actions."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QBoxLayout, QDialogButtonBox

from test_zen_qt_smoke import _app, _wait
from zen_fit import fit_field, validate_recipe
from zen_hardware.mark_simulator import SimulatedMarkDevice
from zen_preferences import Preferences
from zen_qt.app import WorkbenchWindow
from zen_qt.dialogs import GatewayDialog, RecoveryDialog
from zen_qt.pages.records import scan_runs, index_rows


class QtRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'data'
        self.w = WorkbenchWindow(self.root)
        self.w.z_timer.stop()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.w.workflow and self.w.workflow.busy:
            self.w.workflow.stop()
            self.assertTrue(_wait(lambda: not self.w.workflow.busy, 10))
        self.w.close()
        _app().processEvents()

    def demo(self):
        self.w.connect_demo('marks')
        self.assertTrue(_wait(lambda: not self.w.workflow.busy and self.w.workflow.state == '就绪'))
        self.w.z_timer.stop()
        return self.w.pages['marks']

    def select(self, key):
        page = self.w.pages['hardware']
        for i in range(page.tree.topLevelItemCount()):
            group = page.tree.topLevelItem(i)
            for j in range(group.childCount()):
                item = group.child(j)
                if item.data(0, Qt.UserRole) == key:
                    page.tree.setCurrentItem(item)
                    page._select()
                    return page
        self.fail('missing operation ' + key)

    def load(self):
        self.demo()
        h = self.w.pages['hardware']
        h.bind_current()
        self.select('load_experiment')
        h.values['experiment_name'].setText('Demo upper')
        h.approved.setChecked(True)
        h.run()
        self.assertTrue(_wait(lambda: h.latest and not self.w.workflow.busy))
        self.assertEqual(h.latest['status'], 'success')
        return h

    def confirm(self):
        dialog = RecoveryDialog(self.w)
        dialog.show()
        _app().processEvents()
        buttons = dialog.findChild(QDialogButtonBox)
        accept = next(b for b in buttons.buttons() if buttons.buttonRole(b) == QDialogButtonBox.AcceptRole)
        accept.click()
        self.assertEqual(dialog.result(), 0)
        for _, _, box in dialog.checks:
            box.click()
        accept.click()
        self.assertEqual(dialog.result(), 1)
        self.assertFalse(self.w.locked())

    def test_gateway_mode_selector_applies_and_persists(self):
        d = GatewayDialog(self.w)
        d.xy_mode.setCurrentIndex(d.xy_mode.findData('no_xy'))
        with patch.object(self.w, 'connect_field') as connect:
            d._apply()
        connect.assert_called_once()
        self.assertEqual(self.w.xy_mode, 'no_xy')
        self.assertEqual(Preferences(self.w.preferences.path).data['xy_mode'], 'no_xy')
        self.assertIsNone(self.w.workflow)

    def test_legacy_tk_units_do_not_become_million_times_larger(self):
        self.w.close()
        prefs = Preferences(self.root / 'preferences/ui.json')
        prefs.update(mark_capture={'upper': {'experiment': 'CLLOW', 'z_m': '19.996', 'half_range_m': '5'},
                                   'common': {'z_tolerance_m': '0.5', 'capture_timeout_s': '300'}})
        self.w = WorkbenchWindow(self.root)
        m = self.w.pages['marks']
        self.assertAlmostEqual(m.layers['upper']['z'].value(), 19.996)
        self.assertEqual(m.layers['upper']['half'].value(), 5)
        self.assertEqual(m.common['z_tolerance_um'], .5)
        self.assertEqual(m.common['capture_timeout_s'], 300)
        self.assertFalse(m.check_items['zpath'].check.isChecked())
        self.assertIsNone(self.w.workflow)

    def test_non_numeric_legacy_entry_never_becomes_taught(self):
        self.w.close()
        Preferences(self.root / 'preferences/ui.json').update(mark_capture={
            'upper': {'experiment': 'draft', 'z_m': 'not-ready', 'half_range_m': 'nan'}})
        self.w = WorkbenchWindow(self.root)
        self.assertFalse(self.w.pages['marks'].layers['upper']['z_set'])
        self.w.pages['marks'].persist()
        saved = Preferences(self.w.preferences.path).data['mark_capture']
        self.assertEqual(saved['upper']['z_m'], 'not-ready')
        self.assertEqual(saved['upper']['half_range_m'], 'nan')

    def test_field_parameters_roundtrip_in_classic_entry_units(self):
        m = self.w.pages['marks']
        m.layers['upper']['combo'].setCurrentText('CLLOW')
        m.layers['upper']['z'].setValue(19.996)
        m.layers['upper']['half'].setValue(3.5)
        m.common.update(capture_timeout_s=300, xy_tolerance_um=1.5)
        m.output_edit.setText(str(self.base / 'chosen-images'))
        m.use_af.setChecked(False)
        m.capture_kind.setCurrentIndex(m.capture_kind.findData('confocal'))
        m.persist()
        saved = Preferences(self.w.preferences.path).data['mark_capture']
        self.assertEqual(saved['entry_unit'], 'um')
        self.assertAlmostEqual(float(saved['upper']['z_m']), 19.996)
        self.assertEqual(float(saved['common']['xy_tolerance_m']), 1.5)
        self.w.close()
        self.w = WorkbenchWindow(self.root)
        m = self.w.pages['marks']
        self.assertEqual(m.layers['upper']['z'].value(), 19.996)
        self.assertEqual(m.layers['upper']['half'].value(), 3.5)
        self.assertEqual(m.common['capture_timeout_s'], 300)
        self.assertEqual(m.output_edit.text(), str(self.base / 'chosen-images'))
        self.assertFalse(m.use_af.isChecked())
        self.assertEqual(m.capture_kind.currentData(), 'confocal')
        self.assertFalse(m.check_items['loading'].check.isChecked())

    def test_every_critical_mark_edit_invalidates_confirmation(self):
        m = self.demo()
        edits = [lambda: m.layers['upper']['z'].setValue(101),
                 lambda: m.layers['upper']['half'].setValue(4),
                 lambda: m.layers['upper']['combo'].setCurrentText('another'),
                 lambda: m.use_af.setChecked(False),
                 lambda: m.capture_kind.setCurrentIndex(1),
                 lambda: m.output_edit.setText(str(self.base / 'other'))]
        for edit in edits:
            for k in ('zpath', 'loading'): m.check_items[k].check.setChecked(True)
            edit()
            for k in ('zpath', 'loading'): self.assertFalse(m.check_items[k].check.isChecked())

    def test_demo_to_field_drops_demo_coordinates_and_approvals(self):
        m = self.demo()
        for k in ('zpath', 'loading'): m.check_items[k].check.setChecked(True)
        self.w._replace_workflow(SimulatedMarkDevice(), simulated=False, validate=validate_recipe, fit=fit_field)
        self.w.pages['device']._auto_preflight_done = True
        for k in ('upper', 'lower'): self.assertFalse(m.layers[k]['z_set'])
        for k in ('zpath', 'loading'): self.assertFalse(m.check_items[k].check.isChecked())
        self.assertFalse(m._configuration_simulated)

    def test_demo_does_not_overwrite_existing_field_profile(self):
        m = self.w.pages['marks']
        m.layers['upper']['z'].setValue(19.996)
        m.layers['upper']['combo'].setCurrentText('CLLOW')
        m.persist()
        self.demo()
        m.persist()
        saved = Preferences(self.w.preferences.path).data['mark_capture']
        self.assertAlmostEqual(float(saved['upper']['z_m']), 19.996)
        self.w._replace_workflow(SimulatedMarkDevice(), simulated=False, validate=validate_recipe, fit=fit_field)
        self.w.pages['device']._auto_preflight_done = True
        self.assertEqual(m.layers['upper']['z'].value(), 19.996)
        self.assertFalse(m.check_items['zpath'].check.isChecked())

    def test_id_survives_operation_switch_and_clears_on_disconnect(self):
        h = self.load()
        self.select('export_experiment')
        self.assertEqual(h.values['experiment_id'].text(), 'demo-loaded')
        h.run()
        self.assertTrue(_wait(lambda: h.latest.get('operation') == 'export_experiment' and not self.w.workflow.busy))
        self.assertEqual(h.latest['status'], 'success')
        self.assertNotIn('demo-loaded', self.w.preferences.path.read_text(encoding='utf-8'))
        self.w.disconnect()
        self.assertEqual(h.current_experiment_id, '')
        self.assertIsNone(h.session.library)

    def test_actual_recovery_dialog_and_archive_attestation(self):
        h = self.load()
        self.assertTrue(self.w.locked())
        self.confirm()
        runs = scan_runs(self.root)
        self.assertTrue(runs)
        self.assertTrue(all(not r['recovery'] for r in runs))
        self.select('read_z')
        h.run()
        self.assertTrue(_wait(lambda: h.latest.get('operation') == 'read_z' and not self.w.workflow.busy))
        self.assertEqual(h.latest['status'], 'success')

    def test_capture_archive_and_preview_preserve_tiff(self):
        m = self.demo()
        m.output_edit.setText(str(self.base / 'images'))
        for k in ('zpath', 'loading'): m.check_items[k].check.setChecked(True)
        m.capture_buttons['both'].click()
        self.assertTrue(_wait(lambda: self.w.workflow.mark_output_path and not self.w.workflow.busy))
        path = Path(self.w.workflow.mark_output_path)
        capture = json.loads((path / 'capture.json').read_text())
        self.assertTrue(capture['status']['pair_complete'])
        run = next(r for r in scan_runs(self.root) if r['kind'] == 'mark')
        self.assertEqual(run['n_images'], 2)
        self.assertTrue(run['capture_complete'])
        self.assertTrue(run['pair_complete'])
        self.assertEqual(list(index_rows([run]))[0]['n_images'], 2)
        import numpy as np
        import tifffile
        preview16 = path / 'preview-16-bit.tif'
        tifffile.imwrite(preview16, np.array([[0, 65535], [1234, 32768]], dtype=np.uint16))
        sixteen = hashlib.sha256(preview16.read_bytes()).digest()
        self.assertFalse(m._preview(preview16).isNull())
        self.assertEqual(hashlib.sha256(preview16.read_bytes()).digest(), sixteen)
        tiff = path / 'upper.tif'
        before = hashlib.sha256(tiff.read_bytes()).digest()
        self.assertFalse(m._preview(tiff).isNull())
        self.assertEqual(hashlib.sha256(tiff.read_bytes()).digest(), before)
        self.assertFalse(m.check_items['zpath'].check.isChecked())

    def test_experiment_refresh_is_read_only_and_gated(self):
        m = self.demo()
        m.load_experiments()
        self.assertTrue(_wait(lambda: not self.w.workflow.busy and m.layers['upper']['combo'].count() > 0))
        self.assertIn('list_experiments', self.w.workflow.device.lab_calls)
        self.assertEqual(self.w.workflow.device.z_moves, [])
        self.assertEqual(self.w.workflow.device.prepared, [])
        m._summarize_diagnostic({'kind':'capture_failure', 'error':{'message':'failed'}})
        self.assertIn('未通过', m.check_items['experiments'].detail.text())
        m._summarize_diagnostic({'kind':'read_only_experiment_check',
            'requested_experiments':[{'name':'Demo upper','exists':True}], 'errors':[]})
        self.assertIn('硬件设置未核验', m.check_items['experiments'].detail.text())

    def test_query_gate_blocks_device_replacement_and_concurrent_query(self):
        self.demo()
        entered, release = Event(), Event()
        def query(device):
            entered.set()
            release.wait(3)
        try:
            self.w.workflow.read_only('blocking-test', query)
            self.assertTrue(entered.wait(1))
            with self.assertRaises(RuntimeError): self.w.workflow.read_only('other', lambda d: None)
            with self.assertRaises(RuntimeError): self.w._replace_workflow(SimulatedMarkDevice(), simulated=True,
                                                    validate=validate_recipe, fit=fit_field)
        finally:
            release.set()
            self.assertTrue(_wait(lambda: not self.w.workflow.busy))

    def test_small_window_scrolls_and_can_fold_log(self):
        self.w.resize(1366, 740)
        self.w.show()
        self.w.navigate('marks')
        _app().processEvents()
        self.assertLessEqual(self.w.height(), 740)
        scroll = self.w.equipment_tabs.currentWidget()
        self.assertTrue(_wait(lambda: scroll.verticalScrollBar().maximum() > 0))
        self.assertEqual(self.w.pages['marks']._layer_layout.direction(),
                         QBoxLayout.TopToBottom)
        old = self.w.dock.height()
        self.w.dock.fold_button.click()
        self.assertLess(self.w.dock.height(), old)

    def test_session_card_and_identity_persist(self):
        self.w.show()
        _app().processEvents()
        self.assertFalse(self.w.sample_edit.visibleRegion().isEmpty())
        self.w.sample_edit.setText('sample-01')
        self.w.operator_edit.setText('operator-01')
        self.w._save_session()
        self.assertEqual(Preferences(self.w.preferences.path).data['session']['sample'], 'sample-01')

    def test_preflight_adapter_uses_environment_callback_and_mode(self):
        self.demo()
        self.w.workflow.simulated = False
        seen = []
        def check(env, report, *, xy_enabled):
            seen.append((env, xy_enabled))
            report('gateway', '✓ 通过', 'read-only checked')
        with patch('zen_qt.pages.device.preflight.check_gateway', side_effect=check):
            self.w.pages['device']._do_preflight()
            self.assertTrue(_wait(lambda: not self.w.workflow.busy and bool(seen)))
        self.assertTrue(seen[0][1])
        self.assertIn('checks', self.w.pages['device']._last_preflight_data)


if __name__ == '__main__':
    unittest.main()
