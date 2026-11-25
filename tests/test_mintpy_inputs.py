"""Tests for converting dolphin rasters to MintPy inputs."""

import numpy as np
from osgeo import gdal

from isce_dolphin_mintpy.mintpy_inputs import (
    parse_date_pair,
    read_connected_components,
    read_strided_geometry,
)


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


def _write_geometry_layer(path, bands, data_type):
    """Write a GeoTIFF with one or more bands (GDAL opens it by content, whatever the suffix)."""
    length, width = bands[0].shape
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), width, length, len(bands), data_type)
    for index, values in enumerate(bands, start=1):
        dataset.GetRasterBand(index).WriteArray(values)
    dataset = None


def test_strided_geometry_keeps_full_coordinate_precision(tmp_path):
    geometry_dir = tmp_path / "geometry"
    geometry_dir.mkdir()
    # 1e-7 degrees (~1 cm) apart: float32 (~4e-6 degree steps at 48 degrees) would merge them.
    latitude = 48.2 + 1e-7 * np.arange(12, dtype=np.float64).reshape(3, 4)
    longitude = 16.3 + 1e-7 * np.arange(12, dtype=np.float64).reshape(3, 4)
    _write_geometry_layer(geometry_dir / "lat.vrt", [latitude], gdal.GDT_Float64)
    _write_geometry_layer(geometry_dir / "lon.vrt", [longitude], gdal.GDT_Float64)
    _write_geometry_layer(geometry_dir / "hgt.vrt", [np.full((3, 4), 200.0)], gdal.GDT_Float32)
    _write_geometry_layer(geometry_dir / "los.vrt",
                          [np.full((3, 4), 39.0), np.full((3, 4), 102.0)], gdal.GDT_Float32)

    geometry = read_strided_geometry(tmp_path, 1, 1, 3, 4)

    assert geometry["latitude"].dtype == np.float64
    assert geometry["longitude"].dtype == np.float64
    np.testing.assert_array_equal(geometry["latitude"], latitude)
    np.testing.assert_array_equal(geometry["longitude"], longitude)
