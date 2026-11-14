"""Recipe library: immutable revisions, checks, release and comparison."""
import json
import tempfile
import unittest
from pathlib import Path

from zen_recipe_store import (ARCHIVED, DRAFT, RELEASED, RecipeLibrary,
                              default_body)


def valid_body():
    body = default_body("产品 A")
    body["example"] = False
    body["common_params"].update(upper_experiment="Upper", lower_experiment="Lower")
    body["external_dependencies"][0]["expected_xml_sha256"] = "a" * 64
    return body


class RecipeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "data" / "recipes.json"
        self.library = RecipeLibrary(self.path)

    def test_create_persists_hash_and_identity(self):
        recipe_id, revision = self.library.create("产品 A", "tester", body=valid_body())
        self.assertEqual(revision, 1)
        version = self.library.get(recipe_id, revision)
        self.assertEqual(version["status"], DRAFT)
        self.assertEqual(len(version["content_sha256"]), 64)
        reloaded = RecipeLibrary(self.path)
        self.assertEqual(reloaded.latest(recipe_id)["content_sha256"],
                         version["content_sha256"])

    def test_revision_is_frozen_copy(self):
        recipe_id, revision = self.library.create("产品 A", "tester", body=valid_body())
        body = valid_body()
        before = self.library.get(recipe_id, revision)["content_sha256"]
        body["common_params"]["safe_z_um"] = 99.0
        new_rev = self.library.new_revision(recipe_id, body, by="tester",
                                            change_note="改安全 Z")
        body["common_params"]["safe_z_um"] = 123.0
        self.assertEqual(self.library.get(recipe_id, revision)["content_sha256"], before)
        self.assertEqual(self.library.get(recipe_id, new_rev)["body"]
                         ["common_params"]["safe_z_um"], 99.0)

    def test_check_gates_release(self):
        recipe_id, revision = self.library.create("产品 A", "tester", body=valid_body())
        with self.assertRaises(ValueError):
            self.library.release(recipe_id, revision, by="tester")
        report = self.library.check(recipe_id, revision)
        self.assertEqual(report["result"], "passed")
        self.library.release(recipe_id, revision, by="王工", note="已核对")
        version = self.library.get(recipe_id, revision)
        self.assertEqual(version["status"], RELEASED)
        self.assertEqual(version["validation"]["approved_by"], "王工")

    def test_example_body_can_never_be_released(self):
        recipe_id, revision = self.library.create("示例", "tester",
                                                  body=default_body("示例", example=True))
        report = self.library.check(recipe_id, revision)
        self.assertEqual(report["result"], "failed")
        with self.assertRaises(ValueError):
            self.library.release(recipe_id, revision, by="tester")

    def test_check_after_edit_is_rejected(self):
        recipe_id, revision = self.library.create("产品 A", "tester", body=valid_body())
        self.library.check(recipe_id, revision)
        version = self.library.get(recipe_id, revision)
        version["body"]["common_params"]["safe_z_um"] = 42.0
        with self.assertRaises(ValueError):
            self.library.release(recipe_id, revision, by="tester")

    def test_released_revision_is_read_only(self):
        recipe_id, revision = self.library.create("产品 A", "tester", body=valid_body())
        self.library.check(recipe_id, revision)
        self.library.release(recipe_id, revision, by="王工")
        new_rev = self.library.new_revision(recipe_id, valid_body(), by="李工",
                                            change_note="复制为草稿",
                                            base_revision=revision)
        self.assertEqual(new_rev, 2)
        self.assertEqual(self.library.get(recipe_id, revision)["status"], RELEASED)
        self.assertEqual(self.library.get(recipe_id, new_rev)["status"], DRAFT)
        self.assertEqual(self.library.get(recipe_id, new_rev)["parent_revision"], revision)

    def test_compare_reports_field_level_changes(self):
        recipe_id, revision = self.library.create("产品 A", "tester", body=valid_body())
        changed = valid_body()
        changed["common_params"]["capture_timeout_s"] = 240.0
        newer = self.library.new_revision(recipe_id, changed, by="tester",
                                          change_note="延长采图超时")
        report = self.library.compare(recipe_id, revision, newer)
        paths = [item["path"] for item in report["changes"]]
        self.assertIn("common_params.capture_timeout_s", paths)
        self.assertEqual(report["change_count"], len(paths))

    def test_status_transitions_are_constrained(self):
        recipe_id, revision = self.library.create("产品 A", "tester", body=valid_body())
        self.library.check(recipe_id, revision)
        self.library.release(recipe_id, revision, by="王工")
        with self.assertRaises(ValueError):
            self.library.check(recipe_id, revision)
        self.library.archive(recipe_id, revision, by="王工")
        self.assertEqual(self.library.get(recipe_id, revision)["status"], ARCHIVED)
        with self.assertRaises(ValueError):
            self.library.release(recipe_id, revision, by="王工")

    def test_library_file_is_written(self):
        recipe_id, _ = self.library.create("产品 A", "tester", body=valid_body())
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["kind"], "recipe_library")
        self.assertEqual(raw["recipes"][0]["recipe_id"], recipe_id)


if __name__ == "__main__":
    unittest.main()
