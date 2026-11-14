"""Offscreen interaction tests for the recipe management page."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from zen_qt.app import WorkbenchWindow
from zen_recipe_store import ARCHIVED, DRAFT, RELEASED, STATUS_NAMES


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


class TestRecipesPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.window = WorkbenchWindow(Path(self.tmp.name) / "data")
        self.window.navigate("recipes")
        self.page = self.window.pages["recipes"]

    def _select(self, status):
        root = self.page.recipe_tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent = root.child(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                value = child.data(0, Qt.UserRole)
                if not value:
                    continue
                version = self.page.library.get(*value)
                if version and version["status"] == status:
                    self.page.recipe_tree.setCurrentItem(child)
                    return value
        self.fail(f"no {status} revision in the library")

    def _holes(self, body):
        for node in body.get("step_tree", ()):
            if node.get("kind") == "group":
                return node.get("children", [])
        return []

    def test_seed_library_has_real_version_states(self):
        states = {v["status"] for r in self.page.library.recipes()
                  for v in r["revisions"]}
        self.assertTrue({DRAFT, RELEASED, ARCHIVED}.issubset(states))
        self.assertGreater(self.page.step_tree.topLevelItemCount(), 4)
        self.assertIn(STATUS_NAMES[DRAFT], self.page.meta_badge.text())

    def test_check_then_release_makes_the_revision_read_only(self):
        with patch("zen_qt.pages.recipes.QMessageBox.information"), \
             patch("zen_qt.pages.recipes.QMessageBox.warning"):
            self.page.check_recipe()
            version = self.page.library.get(self.page.current_id, self.page.current_rev)
            self.assertEqual(version["checks"]["result"], "passed")
            self.window.operator_edit.setText("王工")
            self.page.release_recipe()
        version = self.page.library.get(self.page.current_id, self.page.current_rev)
        self.assertEqual(version["status"], RELEASED)
        self.assertEqual(version["validation"]["approved_by"], "王工")
        self.assertTrue(self.page._read_only())
        self.assertTrue(self.page.change_note.isReadOnly())

    def test_new_revision_from_released_keeps_old_one_untouched(self):
        released = self._select(RELEASED)
        before = self.page.library.get(*released)["content_sha256"]
        with patch("zen_qt.pages.recipes.QMessageBox.warning"):
            self.page.change_note.setText("基于已验证版本改孔位")
            self.page.save_new_version()
        self.assertEqual(self.page.library.get(*released)["status"], RELEASED)
        self.assertEqual(self.page.library.get(*released)["content_sha256"], before)
        self.assertEqual(self.page.library.get(self.page.current_id,
                                               self.page.current_rev)["status"], DRAFT)
        self.assertEqual(self.page.library.get(self.page.current_id,
                                               self.page.current_rev)["parent_revision"],
                         released[1])

    def test_add_hole_does_not_touch_protected_steps(self):
        before = [n.get("kind") for n in self.page._body.get("step_tree", ())]
        self.page.add_hole()
        after = [n.get("kind") for n in self.page._body.get("step_tree", ())]
        self.assertEqual(before, after)
        self.assertEqual(len(self._holes(self.page._body)), 2)
        self.assertEqual(self.page.step_tree.topLevelItemCount(), len(after))

    def test_released_version_refuses_new_holes(self):
        self._select(RELEASED)
        before = len(self._holes(self.page._body))
        with patch("zen_qt.pages.recipes.QMessageBox.information"):
            self.page.add_hole()
        self.assertEqual(len(self._holes(self.page._body)), before)

    def test_compare_reports_differences(self):
        with patch("zen_qt.pages.recipes.QDialog.exec", return_value=0), \
             patch("zen_qt.pages.recipes.QMessageBox.information"):
            self.page.compare_versions()
        recipe = self.page.library.find(self.page.current_id)
        self.assertGreaterEqual(len(recipe["revisions"]), 2)


if __name__ == "__main__":
    unittest.main()
