"""Windows GUI checks for offline path editing and measurement invalidation."""

from dataclasses import replace
from pathlib import Path
import tempfile
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
        self.assertEqual(applied, [((0., 0.), (100e-6, 0.))])

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
            self.assertEqual(app.field_recipe.targets, ((200e-6, 300e-6),))
            self.assertFalse(app.field_verified)
            self.assertIn('路径已更新', app.status.get())
