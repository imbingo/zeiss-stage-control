"""Acquisition integrity, recovery and calibrated TIFF tests without hardware."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

import numpy as np
import tifffile

from zen_fit import fit_demo, validate_recipe
from zen_hardware.mark_gateway import MarkCapabilityError
from zen_hardware.mark_simulator import SimulatedMarkDevice
from zen_images import RawFrame, write_tiff
from zen_marks import MarkLayer, MarkRecipe
from zen_records import pending_recovery, confirm_recovery
from zen_workflow import Workflow
from test_zen_workflow import RECIPE


MARKS = MarkRecipe(MarkLayer('Upper', 100e-6, 5e-6), MarkLayer('Lower', 200e-6, 5e-6), settle_s=.001)


class MarkCaptureTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'records'
        self.output = self.base / 'images'

    def wait(self, workflow):
        deadline = time.monotonic() + 5
        while workflow.busy and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(workflow.busy)

    def connected(self, device=None, simulated=True):
        workflow = Workflow(device or SimulatedMarkDevice(), self.root, simulated=simulated,
                            validate=validate_recipe, fit=fit_demo)
        workflow.connect(); self.wait(workflow)
        self.assertEqual(workflow.state, '就绪')
        return workflow

    def test_pair_is_lossless_calibrated_and_xy_never_moves(self):
        w = self.connected()
        w.start_mark_capture(MARKS, self.output, confirmed=True, experiment_loading_confirmed=True); self.wait(w)
        self.assertEqual(w.state, '就绪')
        self.assertEqual(w.device.moves, [])
        self.assertEqual(w.device.z_moves, [100e-6, 200e-6])
        self.assertEqual(w.device.capture_count, 2)
        self.assertAlmostEqual(w.device.z, 200e-6)
        self.assertIsNone(w.result)
        self.assertFalse(pending_recovery(self.root, simulated=True))
        document = json.loads((w.mark_output_path / 'capture.json').read_text(encoding='utf-8'))
        self.assertTrue(document['status']['pair_complete'])
        self.assertFalse(document['status']['result_valid'])
        self.assertEqual(set(document['images']), {'upper', 'lower'})
        self.assertTrue(document['simulated'])
        with tifffile.TiffFile(w.mark_output_path / 'upper.tif') as image:
            pixels = image.asarray()
            self.assertEqual(pixels.shape, (96, 128))
            self.assertEqual(pixels.dtype, np.dtype('uint16'))
            y, x = np.indices((96, 128))
            expected = np.where((abs(x-64) < 3) | (abs(y-48) < 3), 50000, 1000).astype('uint16')
            np.testing.assert_array_equal(pixels, expected)
            metadata = json.loads(image.pages[0].description)
            self.assertEqual(metadata['layer'], 'upper')
            self.assertEqual(metadata['scale_x_m_per_pixel'], .2e-6)
            self.assertEqual(image.pages[0].tags['ResolutionUnit'].value, 3)
            n, d = image.pages[0].tags['XResolution'].value
            self.assertAlmostEqual(n/d, 50000)

    def test_loading_requires_separate_operator_confirmation(self):
        w = self.connected()
        with self.assertRaisesRegex(ValueError, '未确认 ZEN 实验'):
            w.start_mark_capture(MARKS, self.output, confirmed=True)
        self.assertEqual(w.device.prepared, [])
        self.assertEqual(w.device.z_moves, [])
        self.assertEqual(w.device.capture_count, 0)

    def test_single_layer_and_config_roundtrip(self):
        self.assertEqual(MARKS, MarkRecipe.from_dict(MARKS.to_dict()))
        w = self.connected()
        w.start_mark_capture(replace(MARKS, lower=None), self.output, layers=('upper',), confirmed=True, experiment_loading_confirmed=True)
        self.wait(w)
        self.assertEqual(w.device.capture_count, 1)
        self.assertTrue((w.mark_output_path / 'upper.tif').exists())
        self.assertFalse((w.mark_output_path / 'lower.tif').exists())
        status = json.loads((w.mark_run_path / 'status.json').read_text())
        self.assertEqual(status['state'], 'complete')
        self.assertFalse(status['pair_complete'])

    def test_invalid_configuration_never_starts_device_actions(self):
        w = self.connected()
        for recipe in (replace(MARKS, lower=MARKS.upper), replace(MARKS, z_tolerance_m=float('nan')),
                       replace(MARKS, upper=replace(MARKS.upper, experiment='')),
                       replace(MARKS, upper=replace(MARKS.upper, half_range_m=-1))):
            with self.subTest(recipe=recipe):
                with self.assertRaises(ValueError):w.start_mark_capture(recipe, self.output, confirmed=True, experiment_loading_confirmed=True)
        with self.assertRaises(ValueError):w.start_mark_capture(MARKS, self.output)
        with self.assertRaises(ValueError):w.start_mark_capture(MARKS, self.root / 'field', confirmed=True, experiment_loading_confirmed=True)
        self.assertEqual(w.device.z_moves, [])
        self.assertEqual(w.device.capture_count, 0)

    def test_failures_keep_partial_images_and_lock_both_workflows(self):
        for fault, saved in (('af_timeout', 0), ('wrong_plane', 0), ('xy', 0),
                             ('scale', 1), ('lower_capture', 1), ('optics', 0)):
            with self.subTest(fault=fault):
                self.root = self.base / fault
                w = self.connected(SimulatedMarkDevice(fault=fault), simulated=False)
                w.start_mark_capture(MARKS, self.output, confirmed=True, experiment_loading_confirmed=True); self.wait(w)
                self.assertEqual(w.state, '设备状态待确认')
                self.assertEqual(len(list(w.mark_output_path.glob('*.tif'))), saved)
                self.assertEqual(w.device.moves, [])
                run, = pending_recovery(self.root, simulated=False)
                self.assertEqual(run, w.mark_run_path)
                with self.assertRaises(RuntimeError):w.start(RECIPE, path_confirmed=True, single_point=True)
                with self.assertRaises(RuntimeError):w.start_mark_capture(MARKS, self.output, confirmed=True, experiment_loading_confirmed=True)
                w.connect(); self.wait(w)
                self.assertEqual(w.state, '设备状态待确认')
                confirm_recovery(run, xy_confirmed=True, af_z_confirmed=True)
                w.device.mark_fault = None
                w.connect(); self.wait(w)
                self.assertEqual(w.state, '就绪')
                self.assertEqual(w.device.moves, [])

    def test_stop_and_exclusive_task_ownership(self):
        class BlockingAF(SimulatedMarkDevice):
            def __init__(self):
                super().__init__(); self.entered = Event(); self.release = Event()
            def autofocus(self, timeout):
                self.entered.set()
                if not self.release.wait(3):raise AssertionError('AF test not released')
                return super().autofocus(timeout)
        device = BlockingAF(); w = self.connected(device)
        w.start_mark_capture(MARKS, self.output, confirmed=True, experiment_loading_confirmed=True)
        try:
            self.assertTrue(device.entered.wait(2))
            with self.assertRaises(RuntimeError):w.start_mark_capture(MARKS, self.output, confirmed=True, experiment_loading_confirmed=True)
            with self.assertRaises(RuntimeError):w.read_mark_z('upper')
            w.stop()
        finally:
            device.release.set(); self.wait(w)
        self.assertEqual(device.capture_count, 0)
        self.assertEqual(len(device.z_moves), 1)
        self.assertEqual(w.state, '设备状态待确认')
        self.assertTrue(pending_recovery(self.root, simulated=True))

    def test_missing_optional_service_preserves_angle_capability(self):
        class MissingFocus(SimulatedMarkDevice):
            def read_z(self):raise MarkCapabilityError('No Focus v2')
        w = self.connected(MissingFocus())
        w.start_mark_capture(MARKS, self.output, confirmed=True, experiment_loading_confirmed=True); self.wait(w)
        self.assertEqual(w.state, '就绪')
        self.assertIsNone(w.mark_run_path)
        self.assertEqual(w.device.z_moves, [])
        w.start(RECIPE, path_confirmed=True); self.wait(w)
        self.assertEqual(w.state, '完成')

    def test_tiff_write_failure_prevents_lower_layer_and_persists_lock(self):
        w = self.connected()
        with patch('zen_marks.write_tiff', side_effect=OSError('disk full')):
            w.start_mark_capture(MARKS, self.output, confirmed=True, experiment_loading_confirmed=True); self.wait(w)
        self.assertEqual(w.device.capture_count, 1)
        self.assertEqual(len(w.device.z_moves), 1)
        self.assertEqual(w.state, '设备状态待确认')
        self.assertTrue(pending_recovery(self.root, simulated=True))

    def test_output_setup_failure_leaves_central_recovery_record(self):
        self.output.write_text('not a directory')
        w = self.connected()
        w.start_mark_capture(MARKS, self.output, confirmed=True, experiment_loading_confirmed=True); self.wait(w)
        self.assertEqual(w.state, '设备状态待确认')
        self.assertEqual(w.device.z_moves, [])
        self.assertEqual(len(pending_recovery(self.root, simulated=True)), 1)


class TiffTest(unittest.TestCase):
    def test_all_pixel_types_preserve_values_and_channel_order(self):
        for code, dtype, channels in ((1, 'u1', 1), (2, '<u2', 1), (4, 'u1', 3), (5, '<u2', 3)):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                shape = (3, 4) if channels == 1 else (3, 4, 3)
                data = np.arange(np.prod(shape), dtype=dtype).reshape(shape)
                if code in (2, 5):data *= 1000
                frame = RawFrame(4, 3, code, data.tobytes(), 1e-6, 2e-6, (0, 0, .001))
                path = Path(folder) / 'image.tif'
                write_tiff(path, frame, {'layer': 'upper'})
                np.testing.assert_array_equal(tifffile.imread(path), data if channels == 1 else data[..., ::-1])
                with self.assertRaises(FileExistsError):write_tiff(path, frame, {})

    def test_bad_frames_rejected_without_publishing_tiff(self):
        base = RawFrame(2, 2, 2, bytes(8), 1e-6, 1e-6, (0, 0, 0))
        for invalid in (replace(base, data=bytes(6)), replace(base, pixel_type=0),
                        replace(base, scale_x_m=0), replace(base, xyz_m=(0, 0, float('nan')))):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'bad.tif'
                with self.assertRaises(ValueError):write_tiff(path, invalid, {})
                self.assertFalse(path.exists())

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'existing.tif'
            temporary = path.with_suffix('.tif.tmp')
            temporary.write_text('pre-existing file')
            with self.assertRaises(FileExistsError):write_tiff(path, base, {})
            self.assertEqual(temporary.read_text(), 'pre-existing file')
