"""Check microradian results and compatibility with saved degree-era recipes."""

import json
from math import atan
from pathlib import Path
import tempfile
import unittest

from zen_acquisition import PointRecord
from zen_fit import FIELD_CONVENTION, fit_field
from zen_recipe import load_recipe


class MicroradianTest(unittest.TestCase):
    def plane(self, a, b):
        points = ((0, 0), (.001, 0), (.001, .002), (0, .002))
        rows = tuple(PointRecord(i, xy, before_af=xy, after_af=xy,
                                 z=50e-6 + a*xy[0] + b*xy[1], status="ok")
                     for i, xy in enumerate(points, 1))
        return fit_field(rows)

    def test_exact_atan_conversion_and_signs(self):
        # Large slopes distinguish atan from a small-angle shortcut and degrees.
        for a, b in ((.5, -.25), (-.5, .25)):
            with self.subTest(a=a, b=b):
                result = self.plane(a, b)
                self.assertAlmostEqual(result['rx_urad'], atan(b) * 1e6)
                self.assertAlmostEqual(result['ry_urad'], -atan(a) * 1e6)
                self.assertEqual(result['angle_unit'], 'urad')
                self.assertNotIn('rx_deg', result)
                self.assertLess(result['rms_m'], 1e-15)

    def test_zero_plane(self):
        result = self.plane(0, 0)
        self.assertAlmostEqual(result['rx_urad'], 0)
        self.assertAlmostEqual(result['ry_urad'], 0)

    def test_known_legacy_recipe_convention_is_migrated(self):
        original = json.loads((Path(__file__).parent / 'recipe.example.json').read_text(encoding='utf-8'))
        original['sign_convention'] = FIELD_CONVENTION.replace('µrad', '°')
        original['point_order'] = [[0, 0], [.001, 0], [0, .001]]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.json'
            text = json.dumps(original, ensure_ascii=False)
            path.write_text(text, encoding='utf-8')
            recipe = load_recipe(path)
            self.assertEqual(recipe.sign_convention, FIELD_CONVENTION)
            self.assertEqual(recipe.targets, ((0, 0), (.001, 0), (0, .001)))
            self.assertEqual(path.read_text(encoding='utf-8'), text)
            original['sign_convention'] = '待核对 V1.1'
            path.write_text(json.dumps(original), encoding='utf-8')
            self.assertEqual(load_recipe(path).sign_convention, '待核对 V1.1')
