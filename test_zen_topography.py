import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch
import numpy as np
import tifffile
from zen_stack_data import StackData
from zen_topography import (
    HeightParameters,
    build_height_map,
    export_height_map,
    AnalysisCancelled,
)


def fixture():
    y, x = np.mgrid[:20, :24]
    surface = 100 + 0.01 * x + 0.02 * y
    z = np.arange(96, 106, 0.5)
    pixels = np.rint(
        100 + 50000 * np.exp(-0.5 * ((z[:, None, None] - surface) / 0.65) ** 2)
    ).astype("u2")
    stack = StackData(pixels, z, 2.0, 5.0, [], "synthetic", "simulation", "test-hash")
    return stack, surface


class TopographyTests(unittest.TestCase):
    def test_numerical_core_matches_pinned_upstream_golden(self):
        from zen_topography_core import _fit_height_maps_vectorized

        path = Path(__file__).parent / "tests/fixtures/topography/upstream_gaussian.npz"
        with np.load(path, allow_pickle=False) as golden:
            result = _fit_height_maps_vectorized(
                golden["curves"],
                golden["z"],
                np.ones((6, 8), bool),
                True,
                "peak",
                False,
            )
            for key in ("height", "r2", "sigma"):
                np.testing.assert_allclose(
                    result[key], golden[key], rtol=1e-12, atol=1e-12, equal_nan=True
                )

    def test_gaussian_known_surface_anisotropic_xy_and_urad(self):
        stack, surface = fixture()
        r = build_height_map(stack, HeightParameters(grid_px=1, residual_sigma=0))
        np.testing.assert_allclose(r["height_um"], surface, atol=0.004)
        s = r["statistics"]
        self.assertAlmostEqual(s["rx_urad"], np.arctan(0.02 / 5) * 1e6, delta=2)
        self.assertAlmostEqual(s["ry_urad"], -np.arctan(0.01 / 2) * 1e6, delta=2)
        self.assertLess(s["rms_um"], 0.002)
        self.assertTrue(r["simulation"])
        np.testing.assert_array_equal(r["mip"], stack.pixels.max(axis=0))

    def test_descending_and_all_modes_use_actual_z(self):
        stack, _ = fixture()
        for method in ("peak", "parabolic", "gaussian", "clarity"):
            a = build_height_map(
                stack,
                HeightParameters(
                    method=method, grid_px=4, residual_sigma=0, signal_fraction=0
                ),
            )
            reverse = StackData(
                stack.pixels[::-1],
                stack.z_um[::-1],
                2,
                5,
                [],
                stack.source,
                stack.z_source,
                "hash",
            )
            b = build_height_map(
                reverse,
                HeightParameters(
                    method=method, grid_px=4, residual_sigma=0, signal_fraction=0
                ),
            )
            np.testing.assert_allclose(a["height_um"], b["height_um"], equal_nan=True)

    def test_weak_and_boundary_points_are_invalid_not_filled(self):
        stack, _ = fixture()
        stack.pixels[:, :5, :] = 100
        stack.pixels[:, 5:10, :] = (
            np.arange(len(stack.z_um), dtype="u2")[:, None, None] * 2000
        )
        r = build_height_map(stack, HeightParameters(grid_px=1, residual_sigma=0))
        self.assertTrue(np.isnan(r["height_um"][:10]).all())
        self.assertFalse(r["valid_mask"][:10].any())
        self.assertEqual(r["statistics"]["boundary_count"], 240)

    def test_flat_stack_fails_instead_of_first_layer_surface(self):
        stack, _ = fixture()
        stack.pixels[:] = 200
        with self.assertRaisesRegex(ValueError, "有效高度点不足"):
            build_height_map(stack)

    def test_nonuniform_z_requires_discrete_and_no_implicit_fallback(self):
        stack, _ = fixture()
        stack.z_um[2] += 0.1
        with self.assertRaisesRegex(ValueError, "等间距"):
            build_height_map(stack)
        r = build_height_map(
            stack, HeightParameters(method="peak", grid_px=2, residual_sigma=0)
        )
        self.assertTrue(set(np.unique(r["height_um"])).issubset(set(stack.z_um)))

    def test_cancel_and_oversize_grid(self):
        stack, _ = fixture()
        stop = Event()
        stop.set()
        with self.assertRaises(AnalysisCancelled):
            build_height_map(stack, stop=stop)
        with self.assertRaises(ValueError):
            build_height_map(stack, HeightParameters(grid_px=100))

        def cancel(*_):
            stop.set()

        stop.clear()
        with self.assertRaises(AnalysisCancelled):
            build_height_map(stack, stop=stop, progress=cancel)

    def test_export_nan_and_masks_roundtrip_and_failed_save_is_not_complete(self):
        stack, _ = fixture()
        stack.pixels[:, :5, :] = 100
        r = build_height_map(stack, HeightParameters(grid_px=1, residual_sigma=0))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out = export_height_map(r, root / "good")
            np.testing.assert_allclose(
                tifffile.imread(out / "height_um.tif"), r["height_um"], equal_nan=True
            )
            self.assertTrue(np.isnan(tifffile.imread(out / "height_um.tif")[:5]).all())
            np.testing.assert_array_equal(
                tifffile.imread(out / "valid_mask.tif"), r["valid_mask"]
            )
            import json

            report = json.loads((out / "result.json").read_text(encoding="utf-8"))
            self.assertTrue(report["height_map_computed"])
            self.assertTrue(report["complete"])
            with patch(
                "zen_topography.np.savez_compressed", side_effect=OSError("disk full")
            ):
                with self.assertRaises(OSError):
                    export_height_map(r, root / "bad")
            self.assertFalse((root / "bad").exists())
            self.assertFalse(list(root.glob(".height-*")))


if __name__ == "__main__":
    unittest.main()
