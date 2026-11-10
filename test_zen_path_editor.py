"""Windows GUI checks for offline path editing and measurement invalidation."""

from dataclasses import replace
from pathlib import Path
import tempfile
import json
import time
from unittest.mock import patch
import tkinter as tk
import unittest

from zen_path_editor import PathEditor, parse_xy
from zen_recipe import load_recipe
from zen_gui import Workbench


class PathEditorTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for task in self.root.tk.call('after', 'info'):
            self.root.after_cancel(task)
        self.root.destroy()

    def test_edit_order_and_apply(self):
        applied = []
        editor = PathEditor(self.root, (), applied.append)
        for x, y in ((0, 0), (100, 0), (100, 100)):
            editor.x.set(str(x)); editor.y.set(str(y)); editor.add()
            self.root.update()
        self.assertEqual(len(editor.targets), 3)
        editor.x.set('125.5'); editor.y.set('-20'); editor.update_point()
        self.root.update()
        self.assertAlmostEqual(editor.targets[2][0], 125.5e-6)
        editor.move(-1)
        self.root.update()
        self.assertAlmostEqual(editor.targets[1][1], -20e-6)
        editor.delete()
        self.root.update()
        self.assertEqual(len(editor.targets), 2)
        editor.apply()
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0][0], (0., 0.))
        self.assertAlmostEqual(applied[0][1][0], 100e-6)
        self.assertEqual(applied[0][1][1], 0.)

    def test_invalid_input_and_cancel(self):
        original = ((0., 0.),)
        applied = []
        editor = PathEditor(self.root, original, applied.append)
        for x in ('nan', 'inf', '文字', ''):
            editor.x.set(x); editor.y.set('0'); editor.add()
            self.assertEqual(editor.targets, list(original))
        editor.x.set('100'); editor.add()
        editor.destroy()
        self.assertEqual(applied, [])
        self.assertEqual(original, ((0., 0.),))
        self.assertEqual(parse_xy('-1000', '2000'), (-.001, .002))

    def test_apply_path_invalidates_af_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Workbench(self.root, Path(folder))
            recipe = load_recipe(Path(__file__).parent / 'recipe.example.json')
            app.apply_field_recipe(recipe)
            app.field_verified = True
            app.edit_path()
            editor = next(w for w in self.root.winfo_children() if isinstance(w, PathEditor))
            editor.x.set('200'); editor.y.set('300'); editor.add()
            editor.apply()
            self.assertEqual(len(app.field_recipe.targets), 1)
            self.assertAlmostEqual(app.field_recipe.targets[0][0], 200e-6)
            self.assertAlmostEqual(app.field_recipe.targets[0][1], 300e-6)
            self.assertFalse(app.field_verified)
            self.assertIn('路径已更新', app.status.get())

    def test_header_microradian_display_and_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = Workbench(self.root, root / 'runs')
            self.assertEqual(app.heading.get(), '设备：未连接')
            self.assertEqual(app.mode_badge.cget('text'), '真实设备')
            app.launch(None)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                self.root.update()
                if not app.pending and not app.workflow.busy and app.workflow.state == '完成':
                    break
                time.sleep(.02)
            self.assertEqual(app.workflow.state, '完成')
            self.assertEqual(app.heading.get(), '离线演示：完成')
            self.assertEqual(app.mode_badge.cget('text'), '离线演示')
            self.assertEqual(app.kpi[0].get(), f"{app.workflow.result['rx_urad']:+.3f}")
            self.assertEqual(app.kpi[1].get(), f"{app.workflow.result['ry_urad']:+.3f}")
            def texts(widget):
                result = [str(widget.cget('text'))] if 'text' in widget.keys() else []
                for child in widget.winfo_children():
                    result.extend(texts(child))
                return result
            labels = texts(self.root)
            self.assertEqual(labels.count('µrad'), 2)
            self.assertIn('演示与测试', labels)
            self.assertNotIn('工程工具', labels)
            destination = root / 'export.json'
            with patch('zen_gui.filedialog.asksaveasfilename', return_value=str(destination)):
                app.export()
            result = json.loads(destination.read_text(encoding='utf-8'))
            self.assertEqual(result['angle_unit'], 'urad')
            self.assertEqual(result['rx_urad'], app.workflow.result['rx_urad'])
            self.assertEqual(result['ry_urad'], app.workflow.result['ry_urad'])
            self.assertIn('µrad', result['recipe']['sign_convention'])
            self.assertNotIn('rx_deg', result)
