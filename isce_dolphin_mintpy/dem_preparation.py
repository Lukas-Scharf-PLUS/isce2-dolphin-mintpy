"""Prepare the DEM for ISCE2: cut it to the processed bursts, convert it to
WGS84 ellipsoid heights and write it with a correct ISCE header.

Why this is needed
------------------
ISCE2 treats DEM values as heights above the WGS84 *ellipsoid*. Common DEMs
store heights above a *geoid* instead (Copernicus DEM: EGM2008, SRTM: EGM96),
and GeoTIFFs rarely record their vertical reference in the metadata, so the
datum cannot be detected reliably. The config therefore has to state it in
``paths.dem_vertical_datum``. Around Vienna the geoid lies about 45 m above the
ellipsoid; using geoid heights shifts the radar geolocation by roughly
45 m / tan(incidence), i.e. 60-70 m, and leaves a residual topographic phase.

Conversion: ellipsoid height = geoid height + geoid undulation N(lat, lon).
N comes from PROJ's official EGM grids (``us_nga_egm08_25.tif`` /
``us_nga_egm96_15.tif``). PROJ silently falls back to a zero shift when a grid
is missing, so the transformation is created with ``only_best=True``, which
raises instead. N is smooth, so it is computed on a coarse grid and
interpolated bilinearly to the DEM pixels.

ISCE header convention: ISCE2 uses ``startingValue`` as the *center* of the
first DEM sample. GDAL geotransforms give the *corner*, so half a pixel is
added. (Converting with ``gdal2isce_xml.py`` skips this and shifts the DEM by
half a pixel.)
"""

import logging

import numpy as np
from osgeo import gdal, osr

logger = logging.getLogger(__name__)
gdal.UseExceptions()

# Vertical datums the pipeline can convert, mapped to their EPSG code.
# "ellipsoid" means the DEM already has WGS84 ellipsoid heights.
GEOID_EPSG_CODES = {"egm2008": 3855, "egm96": 5773}
ELLIPSOID_DATUM = "ellipsoid"
VALID_VERTICAL_DATUMS = tuple(GEOID_EPSG_CODES) + (ELLIPSOID_DATUM,)
PROJ_GEOID_GRIDS = {"egm2008": "us_nga_egm08_25.tif", "egm96": "us_nga_egm96_15.tif"}

WGS84_3D_EPSG = 4979
UNDULATION_GRID_STEP_DEG = 0.05
# Geoid undulations on Earth lie between about -107 m and +86 m.
UNDULATION_RANGE_M = (-110.0, 90.0)
ISCE_DEM_FILE_NAME = "dem_ellipsoid.wgs84"


class DemPreparationError(RuntimeError):
    """Raised when the DEM cannot be read, does not cover the area or cannot be converted."""


def read_dem_window(dem_file, bbox):
    """Read the part of a geographic (lat/lon) DEM that covers a [S, N, W, E] box.

    Returns
    -------
    heights : np.ndarray (float32)
    geotransform : tuple
        GDAL geotransform of the window (corner convention).
    nodata : float or None

    Raises
    ------
    DemPreparationError
        If the DEM is not in geographic WGS84 coordinates or does not cover the box.
    """
    dataset = gdal.Open(str(dem_file))
    spatial_ref = dataset.GetSpatialRef()
    if spatial_ref is None or not spatial_ref.IsGeographic():
        raise DemPreparationError(
            f"DEM must be in geographic lat/lon coordinates (WGS84): {dem_file}"
        )

    geotransform = dataset.GetGeoTransform()
    south, north, west, east = bbox
    width, length = dataset.RasterXSize, dataset.RasterYSize
    dem_west, dem_north = geotransform[0], geotransform[3]
    dem_east = dem_west + width * geotransform[1]
    dem_south = dem_north + length * geotransform[5]
    if west < dem_west or east > dem_east or south < dem_south or north > dem_north:
        raise DemPreparationError(
            f"DEM {dem_file} covers [S {dem_south:.3f}, N {dem_north:.3f}, W {dem_west:.3f}, "
            f"E {dem_east:.3f}] but the processed bursts need "
            f"[S {south:.3f}, N {north:.3f}, W {west:.3f}, E {east:.3f}]."
        )

    first_col = int(np.floor((west - dem_west) / geotransform[1]))
    last_col = int(np.ceil((east - dem_west) / geotransform[1]))
    first_row = int(np.floor((north - dem_north) / geotransform[5]))
    last_row = int(np.ceil((south - dem_north) / geotransform[5]))
    band = dataset.GetRasterBand(1)
    heights = band.ReadAsArray(first_col, first_row, last_col - first_col,
                               last_row - first_row).astype(np.float32)
    window_geotransform = (
        dem_west + first_col * geotransform[1], geotransform[1], 0.0,
        dem_north + first_row * geotransform[5], 0.0, geotransform[5],
    )
    return heights, window_geotransform, band.GetNoDataValue()


def pixel_center_coordinates(geotransform, shape):
    """Return 1D arrays of pixel-center latitudes (rows) and longitudes (columns)."""
    length, width = shape
    longitudes = geotransform[0] + (np.arange(width) + 0.5) * geotransform[1]
    latitudes = geotransform[3] + (np.arange(length) + 0.5) * geotransform[5]
    return latitudes, longitudes


def geoid_undulation_function(vertical_datum):
    """Return a function N(lon, lat) -> geoid undulation in meters for the datum.

    Raises
    ------
    DemPreparationError
        If PROJ cannot use the exact geoid grid (missing and not downloadable).
    """
    from pyproj import Transformer, network
    from pyproj.exceptions import ProjError

    network.set_network_enabled(True)  # lets PROJ fetch the grid from cdn.proj.org once
    source = f"EPSG:4326+{GEOID_EPSG_CODES[vertical_datum]}"
    try:
        transformer = Transformer.from_crs(source, f"EPSG:{WGS84_3D_EPSG}",
                                           always_xy=True, only_best=True)
    except ProjError as error:
        raise DemPreparationError(
            f"PROJ cannot use the geoid grid for {vertical_datum} "
            f"({PROJ_GEOID_GRIDS[vertical_datum]}): {error}\n"
            f"Download it once with:  projsync --file {PROJ_GEOID_GRIDS[vertical_datum]}"
        ) from error

    def undulation(longitudes, latitudes):
        _, _, ellipsoid_height = transformer.transform(
            longitudes, latitudes, np.zeros_like(longitudes, dtype=np.float64)
        )
        return np.asarray(ellipsoid_height)

    return undulation


def compute_undulation_grid(latitudes, longitudes, undulation_function,
                            step_deg=UNDULATION_GRID_STEP_DEG):
    """Return the geoid undulation for every DEM pixel (2D array, rows x columns).

    N is evaluated on a coarse grid with ``step_deg`` spacing and interpolated
    bilinearly; EGM undulations vary by centimeters over such distances.

    Raises
    ------
    DemPreparationError
        If the computed undulations are not finite or outside the physical range.
    """
    from scipy.interpolate import RegularGridInterpolator

    coarse_lat = np.arange(latitudes.min() - step_deg, latitudes.max() + 2 * step_deg, step_deg)
    coarse_lon = np.arange(longitudes.min() - step_deg, longitudes.max() + 2 * step_deg, step_deg)
    lon_grid, lat_grid = np.meshgrid(coarse_lon, coarse_lat)
    coarse_undulation = undulation_function(lon_grid.ravel(), lat_grid.ravel()).reshape(lat_grid.shape)

    low, high = UNDULATION_RANGE_M
    if not np.all(np.isfinite(coarse_undulation)) or coarse_undulation.min() < low \
            or coarse_undulation.max() > high:
        raise DemPreparationError(
            "Geoid undulation is not finite or outside the physical range "
            f"({low} .. {high} m): min {np.nanmin(coarse_undulation)}, "
            f"max {np.nanmax(coarse_undulation)}"
        )

    interpolator = RegularGridInterpolator((coarse_lat, coarse_lon), coarse_undulation)
    lat_pixels, lon_pixels = np.meshgrid(latitudes, longitudes, indexing="ij")
    return interpolator((lat_pixels, lon_pixels)).astype(np.float32)


def to_ellipsoid_heights(heights, nodata, undulation):
    """Add the geoid undulation to valid DEM heights; no-data pixels are set to 0.

    ISCE2 has no no-data concept for DEMs, so 0 is used like in ISCE2's own
    DEM stitcher.
    """
    valid = np.isfinite(heights)
    if nodata is not None:
        valid &= heights != nodata
    return np.where(valid, heights + undulation, 0.0).astype(np.float32)


def write_isce_dem(heights, geotransform, out_file):
    """Write heights as a float32 ISCE DEM with an XML header and a GDAL VRT.

    ``startingValue`` is the first pixel *center* (ISCE2 convention); paths are absolute.
    """
    import isce  # noqa: F401  (sets up the ISCE2 component paths)
    import isceobj

    out_file.parent.mkdir(parents=True, exist_ok=True)
    heights.astype(np.float32).tofile(out_file)

    length, width = heights.shape
    image = isceobj.createDemImage()
    image.setFilename(str(out_file.resolve()))
    image.setWidth(width)
    image.setLength(length)
    image.setAccessMode("read")
    image.setDataType("FLOAT")
    image.setFirstLongitude(geotransform[0] + 0.5 * geotransform[1])
    image.setFirstLatitude(geotransform[3] + 0.5 * geotransform[5])
    image.setDeltaLongitude(geotransform[1])
    image.setDeltaLatitude(geotransform[5])
    image.reference = "WGS84"
    image.renderHdr()


def prepare_isce_dem(config, dem_bbox):
    """Create the ISCE2 DEM for this run (cut, converted to ellipsoid heights).

    Parameters
    ----------
    config : PipelineConfig
    dem_bbox : list[float]
        [S, N, W, E] area the DEM must cover (all processed bursts).

    Returns
    -------
    Path
        The ISCE DEM file to pass to stackSentinel.py.
    """
    out_file = config.isce_dir / "DEM" / ISCE_DEM_FILE_NAME
    if out_file.is_file() and out_file.with_name(out_file.name + ".xml").is_file():
        logger.info("Using existing ISCE DEM %s", out_file)
        return out_file

    datum = config.paths.dem_vertical_datum
    heights, geotransform, nodata = read_dem_window(config.paths.dem_file, dem_bbox)
    if datum == ELLIPSOID_DATUM:
        undulation = np.zeros_like(heights)
    else:
        latitudes, longitudes = pixel_center_coordinates(geotransform, heights.shape)
        undulation = compute_undulation_grid(latitudes, longitudes, geoid_undulation_function(datum))
        logger.info("Geoid undulation (%s) in the DEM area: %.2f .. %.2f m",
                    datum, float(undulation.min()), float(undulation.max()))

    write_isce_dem(to_ellipsoid_heights(heights, nodata, undulation), geotransform, out_file)
    logger.info("ISCE DEM written: %s (%d x %d pixels, %s -> WGS84 ellipsoid)",
                out_file, heights.shape[1], heights.shape[0], datum)
    return out_file
