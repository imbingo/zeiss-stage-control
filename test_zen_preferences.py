"""Remember UI entries without remembering credentials or action authority."""

import json
from pathlib import Path
import tempfile
import unittest
from zen_preferences import Preferences


class PreferencesTest(unittest.TestCase):
    def test_entries_survive_new_instance_but_credentials_and_approvals_do_not(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'preferences/ui.json'
            prefs=Preferences(path)
            self.assertTrue(prefs.update(xy_mode='no_xy', connection={
                'ZEN_GATEWAY_ADDRESS':'localhost:5002', 'ZEN_GATEWAY_CA':'C:/test/ca.pem',
                'ZEN_GATEWAY_TOKEN':'NEVER-SAVE-THIS'}, mark_capture={
                'upper':{'experiment':'PLLOW扫描','z_m':'1000','half_range_m':'5'},
                'common':{'capture_timeout_s':'60'}, 'reported_zen_version':'3.12',
                'capture_kind':'confocal','use_autofocus':False,'simulated_configuration':False,
                'approved':True,'loading_approved':True}))
            restored=Preferences(path)
            self.assertEqual(restored.data['xy_mode'], 'no_xy')
            self.assertEqual(restored.data['mark_capture']['upper']['experiment'], 'PLLOW扫描')
            self.assertEqual(restored.data['connection']['ZEN_GATEWAY_ADDRESS'], 'localhost:5002')
            content=path.read_text(encoding='utf-8')
            for forbidden in ('NEVER-SAVE-THIS', 'ZEN_GATEWAY_TOKEN', 'approved'):
                self.assertNotIn(forbidden, content)

    def test_corrupt_preferences_are_preserved_without_automatic_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ui.json';path.write_text('{invalid')
            prefs=Preferences(path)
            self.assertTrue(prefs.warning)
            self.assertFalse(prefs.update(xy_mode='no_xy'))
            self.assertEqual(path.read_text(), '{invalid')

    def test_incomplete_entry_is_retained_without_becoming_a_capture_recipe(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ui.json'
            prefs=Preferences(path)
            self.assertTrue(prefs.update(mark_capture={'upper':{'experiment':'draft','z_m':'not-yet-numeric'}}))
            restored=Preferences(path)
            self.assertEqual(restored.data['mark_capture']['upper']['z_m'], 'not-yet-numeric')
            self.assertNotIn('approved', restored.data['mark_capture'])

    def test_explicit_si_entries_migrate_to_classic_micrometre_entries_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ui.json'
            prefs = Preferences(path)
            prefs.update(mark_capture={'entry_unit':'m', 'upper':{
                'experiment':'CLLOW','z_m':19.996e-6,'half_range_m':5e-6},
                'output_dir':'D:/images','common':{'z_tolerance_m':.5e-6,'capture_timeout_s':300}})
            for _ in range(2):
                prefs = Preferences(path)
                cap = prefs.data['mark_capture']
                self.assertEqual(cap['entry_unit'], 'um')
                self.assertAlmostEqual(float(cap['upper']['z_m']), 19.996)
                self.assertAlmostEqual(float(cap['upper']['half_range_m']), 5)
                self.assertAlmostEqual(float(cap['common']['z_tolerance_m']), .5)
                self.assertEqual(cap['output'], 'D:/images')
                self.assertTrue(prefs.update(mark_capture=cap))

    def test_unknown_entry_unit_preserves_original_and_disables_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ui.json'
            original = json.dumps({'ui_schema_version':1,'mark_capture':{'entry_unit':'furlong'}})
            path.write_text(original, encoding='utf-8')
            prefs = Preferences(path)
            self.assertTrue(prefs.warning)
            self.assertFalse(prefs.update(xy_mode='no_xy'))
            self.assertEqual(path.read_text(encoding='utf-8'), original)
