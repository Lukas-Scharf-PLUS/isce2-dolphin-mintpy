"""Area of interest helpers: read the AOI and express it as a lat/lon bounding box.

ISCE2 and the radar crop both work with a simple [south, north, west, east]
box in WGS84 degrees. A vector file (GeoPackage, GeoJSON, Shapefile) is reduced
to the bounding box of all its features.
"""

import math

from osgeo import ogr, osr

METERS_PER_DEGREE_LATITUDE = 111_320.0


def read_vector_bbox(vector_file):
    """Return the WGS84 bounding box [S, N, W, E] of all features in a vector file.

    Raises
    ------
    ValueError
        If the file cannot be opened or contains no features.
    """
    dataset = ogr.Open(str(vector_file))
    if dataset is None:
        raise ValueError(f"Cannot open AOI vector file: {vector_file}")

    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    south, north, west, east = math.inf, -math.inf, math.inf, -math.inf
    for layer in dataset:
        source_srs = layer.GetSpatialRef()
        transform = None
        if source_srs is not None:
            source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            transform = osr.CoordinateTransformation(source_srs, wgs84)
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is None:
                continue
            geometry = geometry.Clone()
            if transform is not None:
                geometry.Transform(transform)
            min_x, max_x, min_y, max_y = geometry.GetEnvelope()
            west, east = min(west, min_x), max(east, max_x)
            south, north = min(south, min_y), max(north, max_y)

    if not math.isfinite(south):
        raise ValueError(f"AOI vector file has no features: {vector_file}")
    return [south, north, west, east]


def buffer_bbox(bbox, buffer_m):
    """Grow a [S, N, W, E] box by ``buffer_m`` meters on every side.

    The meter-to-degree conversion uses the box center latitude, which is
    accurate enough for a processing margin.
    """
    south, north, west, east = bbox
    center_latitude = math.radians((south + north) / 2.0)
    buffer_lat = buffer_m / METERS_PER_DEGREE_LATITUDE
    buffer_lon = buffer_m / (METERS_PER_DEGREE_LATITUDE * math.cos(center_latitude))
    return [south - buffer_lat, north + buffer_lat, west - buffer_lon, east + buffer_lon]


def get_aoi_bbox(aoi_config, with_buffer=True):
    """Return the AOI as [S, N, W, E] degrees, optionally including the buffer.

    Parameters
    ----------
    aoi_config : AoiConfig
        The ``aoi`` section of the pipeline config.
    with_buffer : bool
        Add ``aoi_config.buffer_m`` around the box.
    """
    if aoi_config.file is not None:
        bbox = read_vector_bbox(aoi_config.file)
    else:
        bbox = [float(value) for value in aoi_config.bbox]
    if with_buffer and aoi_config.buffer_m > 0:
        bbox = buffer_bbox(bbox, aoi_config.buffer_m)
    return bbox
