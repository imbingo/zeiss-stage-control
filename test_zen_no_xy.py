"""No-XY mode never invents coordinates or calls a stage service."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

import tifffile

from test_zen_marks import MARKS
from test_zen_workflow import RECIPE
from zen_fit import fit_demo, validate_recipe
from zen_hardware.mark_simulator import SimulatedMarkDevice
from zen_hardware.preflight import check_gateway
from zen_images import RawFrame
from zen_marks import MarkRecipe
from zen_records import pending_recovery
from zen_workflow import Workflow


class NoXYTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.base=Path(temp.name)
        class FixedLens(SimulatedMarkDevice):
            def read_optics(self):return (None, None)
        self.device=FixedLens(xy_enabled=False)
        self.workflow=Workflow(self.device, self.base/'records', simulated=False, validate=validate_recipe, fit=fit_demo)
        self.workflow.connect();self.wait()
        self.recipe=replace(MARKS, xy_mode='no_xy', capture_kind='confocal', use_autofocus=False)

    def wait(self):
        deadline=time.monotonic()+5
        while self.workflow.busy and time.monotonic()<deadline:time.sleep(.005)
        self.assertFalse(self.workflow.busy)

    def test_no_xy_pair_stores_null_positions_and_no_af_is_optional(self):
        self.assertEqual(self.workflow.state, '就绪')
        self.workflow.read_mark_z('upper');self.wait()
        events=[]
        while not self.workflow.events.empty():events.append(self.workflow.events.get())
        taught=next(event for event in events if event[0]=='mark_z')
        self.assertIsNone(taught[3])
        self.workflow.start_mark_capture(self.recipe, self.base/'images', confirmed=True, experiment_loading_confirmed=True);self.wait()
        self.assertEqual(self.workflow.state, '就绪')
        self.assertEqual(self.device.moves, [])
        self.assertEqual(self.device.af_attempts, 0)
        self.assertEqual(self.device.capture_kinds, ['confocal', 'confocal'])
        document=json.loads((self.workflow.mark_output_path/'capture.json').read_text(encoding='utf-8'))
        self.assertIsNone(document['start_xy_m'])
        self.assertFalse(document['xy_verified'])
        self.assertEqual(document['optics_verified'], [False, False])
        self.assertTrue(document['status']['pair_complete'])
        with tifffile.TiffFile(self.workflow.mark_output_path/'upper.tif') as image:
            metadata=json.loads(image.pages[0].description)
            self.assertEqual(metadata['frame_xyz_m'][:2], [None, None])
            self.assertFalse(metadata['xy_verified'])
            self.assertEqual(metadata['capture_kind'], 'confocal')
            self.assertIsNone(metadata['af_z_m'])

    def test_angle_and_mismatched_connection_modes_are_rejected(self):
        with self.assertRaises(RuntimeError):self.workflow.start(RECIPE, path_confirmed=True)
        with self.assertRaises(RuntimeError):self.workflow.teach_xy()
        with self.assertRaises(ValueError):self.workflow.start_mark_capture(MARKS, self.base/'images', confirmed=True, experiment_loading_confirmed=True)
        self.assertEqual(self.device.z_moves, [])
        self.assertEqual(self.device.capture_count, 0)

    def test_af_failure_still_locks_no_xy_mode_after_reconnect(self):
        self.device.mark_fault='af_timeout'
        self.workflow.start_mark_capture(replace(self.recipe, use_autofocus=True), self.base/'images', confirmed=True, experiment_loading_confirmed=True)
        self.wait()
        self.assertEqual(self.workflow.state, '设备状态待确认')
        self.workflow.connect();self.wait()
        self.assertEqual(self.workflow.state, '设备状态待确认')
        self.assertTrue(pending_recovery(self.base/'records', simulated=False))

    def test_legacy_configs_and_raw_frame_validation(self):
        legacy=MARKS.to_dict()
        for key in ('xy_mode', 'capture_kind', 'use_autofocus'):legacy.pop(key)
        self.assertEqual(MarkRecipe.from_dict(legacy), MARKS)
        frame=RawFrame(2, 2, 2, bytes(8), 1e-6, 1e-6, (None, None, .001), xy_verified=False)
        self.assertEqual(frame.array().shape, (2, 2))
        with self.assertRaises(ValueError):replace(frame, xy_verified=True).array()
        with self.assertRaises(ValueError):replace(frame, xyz_m=(0, 0, .001)).array()
        with self.assertRaises(ValueError):replace(self.recipe, capture_kind='unknown').validate()

    def test_preflight_reports_z_and_xy_not_applicable(self):
        pem=self.base/'ca.pem';pem.write_text('test-only placeholder')
        device=Mock()
        device.connect.return_value=100e-6
        device.read_xy.side_effect=AssertionError('must not read XY')
        device.motion_state.side_effect=AssertionError('must not query XY motion')
        reports={}
        with patch('zen_hardware.preflight.ssl.SSLContext'), patch('zen_hardware.preflight.GatewayDevice', return_value=device) as factory:
            check_gateway({'ZEN_GATEWAY_ADDRESS':'localhost:5002','ZEN_GATEWAY_CA':str(pem),
                           'ZEN_GATEWAY_TOKEN':'synthetic-test-token'},
                          lambda key, state, detail:reports.update({key:(state,detail)}), xy_enabled=False)
        self.assertFalse(factory.call_args.kwargs['xy_enabled'])
        self.assertTrue(reports['gateway'][0].startswith('✓'))
        self.assertIn('Z 100.000', reports['gateway'][1])
        self.assertIn('不适用', reports['stage'][0])
        device.read_xy.assert_not_called();device.motion_state.assert_not_called()
