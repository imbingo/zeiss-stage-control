import unittest
from zen_scan_path import generate_path, parse_points, fit_prediction, predict_z


class ScanPathTests(unittest.TestCase):
    def test_grid_twenty_unique_snake_and_four_corners(self):
        rows = generate_path(0, 0, 30000, 30000)
        self.assertEqual(len(rows), 20)
        self.assertEqual(len({tuple(r[:2]) for r in rows}), 20)
        self.assertEqual(rows[0][:2], [0, 0])
        self.assertEqual(rows[4][:2], [30000, 0])
        self.assertEqual(rows[5][:2], [30000, 10000])
        self.assertEqual(rows[-1][:2], [0, 30000])

    def test_line_and_invalid_generation(self):
        self.assertEqual(
            generate_path(0, 0, 10, 20, kind="line", count=3)[1][:2], [5, 10]
        )
        for args in ((0, 0, 0, 1), (0, 0, float("nan"), 1)):
            with self.assertRaises(ValueError):
                generate_path(*args)
        with self.assertRaises(ValueError):
            generate_path(0, 0, 10, 10, rows=100, columns=100)

    def test_excel_csv_and_bad_rows(self):
        self.assertEqual(parse_points("1\t2\n3\t4\t5"), [[1, 2, ""], [3, 4, 5]])
        self.assertEqual(parse_points("1,2,\n3,4,5")[0], [1, 2, ""])
        for text in ("X\tY", "1,2\n1,2", "1,nan", "1,", "1,2,3,4", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_points(text)

    def test_centred_model_handles_large_absolute_coordinates(self):
        points = [
            (1e7 + x, 2e7 + y, 100 + 0.001 * x - 0.002 * y)
            for x, y in ((0, 0), (30000, 0), (0, 30000), (30000, 30000))
        ]
        model = fit_prediction(points, 0.01)
        self.assertAlmostEqual(predict_z(model, 1e7 + 15000, 2e7 + 15000), 85)
        self.assertAlmostEqual(model["max_abs_residual_um"], 0)

    def test_warped_corners_fail_residual_gate(self):
        with self.assertRaises(ValueError):
            fit_prediction([(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 10)], 1)
