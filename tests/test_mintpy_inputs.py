"""Tests for converting dolphin rasters to MintPy inputs."""

import numpy as np
from osgeo import gdal

from isce_dolphin_mintpy.mintpy_inputs import parse_date_pair, read_connected_components


def test_conncomp_nodata_becomes_zero(tmp_path):
    path = tmp_path / "20220101_20220113.unw.conncomp.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), 3, 1, 1, gdal.GDT_UInt16)
    dataset.GetRasterBand(1).WriteArray(np.array([[0, 1, 65535]], dtype=np.uint16))
    dataset.GetRasterBand(1).SetNoDataValue(65535)
    dataset = None
    labels = read_connected_components(path)
    assert labels.dtype == np.int16
    assert labels.tolist() == [[0, 1, 0]]


def test_date_pair_is_parsed_from_file_name():
    assert parse_date_pair("unwrapped/20220101_20220125.unw.tif") == ("20220101", "20220125")
