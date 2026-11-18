"""Actual-Z ingestion, explicit TIFF mapping and lossless algorithm handoff."""

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np
import tifffile
from zen_stack_data import load_stack, export_stack, write_stack_xml


class StackDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.array = np.arange(36, dtype=np.uint16).reshape(3, 3, 4)
        self.path = self.root / "stack.xml"
        for i, frame in enumerate(self.array):
            tifffile.imwrite(self.root / f"{i}.tif", frame, photometric="minisblack")
        write_stack_xml(
            self.path,
            [dict(file=f"{i}.tif", z_um=z) for i, z in enumerate([10, 10.3, 10.9])],
            width=4,
            height=3,
            scale_x_um=0.2,
            scale_y_um=0.25,
            z_source="api_frame_stage_position_z",
        )

    def mutate(self, fn):
        tree = ET.parse(self.path)
        fn(tree.getroot())
        tree.write(self.path, encoding="utf-8")

    def test_actual_nonuniform_z_roundtrip_and_lossless_algorithm_arrays(self):
        data = load_stack(self.path)
        np.testing.assert_array_equal(data.pixels, self.array)
        np.testing.assert_array_equal(data.z_um, [10, 10.3, 10.9])
        out = export_stack(data, self.root / "out")
        restored = load_stack(out / "stack.xml")
        np.testing.assert_array_equal(restored.pixels, data.pixels)
        np.testing.assert_array_equal(restored.z_um, data.z_um)
        np.testing.assert_array_equal(
            np.load(out / "images_zyx.npy", allow_pickle=False), data.pixels
        )
        np.testing.assert_array_equal(
            np.load(out / "z_um.npy", allow_pickle=False), data.z_um
        )
        self.assertEqual(data.scale_y_um, 0.25)
        with self.assertRaises(FileExistsError):
            export_stack(data, out)

    def test_descending_z_is_preserved(self):
        self.mutate(lambda r: [p.set("z", str(20 - i * 0.4)) for i, p in enumerate(r)])
        np.testing.assert_allclose(load_stack(self.path).z_um, [20, 19.6, 19.2])

    def test_missing_or_duplicate_layer_and_unknown_units_rejected(self):
        for fn in (
            lambda r: r.remove(r[-1]),
            lambda r: r[1].set("index", "0"),
            lambda r: r.set("unit", "pixel"),
            lambda r: r[1].set("z", "nan"),
            lambda r: r[1].set("z", "10"),
            lambda r: r[2].set("z", "10.1"),
        ):
            original = self.path.read_bytes()
            self.mutate(fn)
            with self.assertRaises((ValueError, KeyError)):
                load_stack(self.path)
            self.path.write_bytes(original)

    def test_missing_image_and_parent_escape_rejected(self):
        for name in ("missing.tif", "../0.tif", "C:\\data\\0.tif"):
            self.mutate(lambda r: r[0].set("file", name))
            with self.assertRaises(ValueError):
                load_stack(self.path)

    def test_wrong_shape_dtype_and_memory_limit_rejected(self):
        for image in (
            np.ones((2, 4), dtype="u2"),
            np.ones((3, 4), dtype="u1"),
            np.ones((3, 4), dtype="f4"),
        ):
            tifffile.imwrite(self.root / "1.tif", image)
            with self.assertRaises(ValueError):
                load_stack(self.path)
        tifffile.imwrite(self.root / "1.tif", self.array[1])
        with patch("zen_stack_data.MAX_BYTES", 10):
            with self.assertRaises(ValueError):
                load_stack(self.path)

    def test_save_failure_leaves_no_complete_output(self):
        data = load_stack(self.path)
        with patch("zen_stack_data.np.save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                export_stack(data, self.root / "failed")
        self.assertFalse((self.root / "failed").exists())
        self.assertFalse(list(self.root.glob(".stack-*")))

    def test_source_change_during_read_is_not_accepted(self):
        import os

        original = tifffile.TiffPage.asarray

        def changing(page, *args, **kwargs):
            pixels = original(page, *args, **kwargs)
            path = self.root / "0.tif"
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
            return pixels

        with patch.object(tifffile.TiffPage, "asarray", changing):
            with self.assertRaisesRegex(ValueError, "源图像发生变化"):
                load_stack(self.path)

    def test_configuration_xml_and_entities_are_not_acquisition_metadata(self):
        for data in (
            b"<Experiment><StartZ>10</StartZ></Experiment>",
            b'<!DOCTYPE a [<!ENTITY b "oops">]><a>&b;</a>',
        ):
            self.path.write_bytes(data)
            with self.assertRaises(ValueError):
                load_stack(self.path)

    def ome(self, *, embedded=False):
        # Z is the fastest varying acquisition axis; select C=1,T=1 explicitly.
        pixels = np.arange(6 * 3 * 4, dtype="u2").reshape(6, 3, 4)
        tifffile.imwrite(
            self.root / "multi.tif", pixels, photometric="minisblack", metadata=None
        )
        r = ET.Element("OME", xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06")
        image = ET.SubElement(r, "Image")
        p = ET.SubElement(
            image,
            "Pixels",
            DimensionOrder="XYZCT",
            SizeX="4",
            SizeY="3",
            SizeZ="3",
            SizeC="2",
            SizeT="1",
            PhysicalSizeX="200",
            PhysicalSizeXUnit="nm",
            PhysicalSizeY=".25",
            PhysicalSizeYUnit="µm",
        )
        ET.SubElement(p, "Channel", SamplesPerPixel="1")
        td = ET.SubElement(p, "TiffData", IFD="0", PlaneCount="6")
        if not embedded:
            ET.SubElement(td, "UUID", FileName="multi.tif")
        for c in range(2):
            for z in range(3):
                ET.SubElement(
                    p,
                    "Plane",
                    TheZ=str(z),
                    TheC=str(c),
                    TheT="0",
                    PositionZ=str(0.0001 + z * 0.000001),
                    PositionZUnit="m",
                )
        if embedded:
            tifffile.imwrite(
                self.root / "embedded.tif",
                pixels,
                photometric="minisblack",
                metadata=None,
                description=ET.tostring(r, encoding="ascii").decode("ascii"),
            )
            return self.root / "embedded.tif"
        ET.ElementTree(r).write(self.path, encoding="utf-8")
        return self.path

    def test_ome_multichannel_mapping_unit_conversion(self):
        data = load_stack(self.ome(), channel=1)
        expected = np.arange(72, dtype="u2").reshape(6, 3, 4)[3:]
        np.testing.assert_array_equal(data.pixels, expected)
        np.testing.assert_allclose(data.z_um, [100, 101, 102])
        self.assertAlmostEqual(data.scale_x_um, 0.2)
        self.assertEqual(data.planes[0]["page"], 3)
        with self.assertRaises(ValueError):
            load_stack(self.path, channel=2)

    def test_embedded_ome_tiff_uses_same_z_reader(self):
        data = load_stack(self.ome(embedded=True), channel=1)
        np.testing.assert_allclose(data.z_um, [100, 101, 102])
        self.assertEqual(data.pixels.shape, (3, 3, 4))

    def test_ome_missing_z_is_not_filled_using_physical_size_z(self):
        self.ome()
        self.mutate(lambda r: r.find(".//{*}Plane").attrib.pop("PositionZ"))
        with self.assertRaisesRegex(ValueError, "PositionZ"):
            load_stack(self.path)

    def test_ome_duplicate_mapping_and_missing_page_rejected(self):
        self.ome()
        self.mutate(
            lambda r: r.find(".//{*}Pixels").append(
                ET.fromstring(
                    '<TiffData IFD="0" PlaneCount="1"><UUID FileName="multi.tif"/></TiffData>'
                )
            )
        )
        with self.assertRaises(ValueError):
            load_stack(self.path)
        self.ome()
        self.mutate(lambda r: r.find(".//{*}TiffData").set("IFD", "100"))
        with self.assertRaisesRegex(ValueError, "图像页缺失"):
            load_stack(self.path)


if __name__ == "__main__":
    unittest.main()
