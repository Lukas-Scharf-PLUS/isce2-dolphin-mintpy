"""Choose the Sentinel-1 swaths (IW1/2/3) that really cover the AOI.

stackSentinel.py keeps every burst whose lat/lon *bounding rectangle* overlaps
the AOI box. Those rectangles are computed for heights from -500 m to 9000 m
and the swaths are tilted, so they are much larger than the real footprint.
A small AOI next to a swath border therefore pulls in a neighbouring swath
with a single burst that does not even cover the AOI. With ESD this breaks
topsStack: a swath with one burst has no burst overlap ("list index out of
range" in run_07_pairs_misreg).

This module computes the real footprint of every burst (its four corners at
sea level, from ISCE2's own Sentinel-1 reader and orbit) and keeps only swaths
that actually intersect the AOI. The result is passed to stackSentinel.py -n.

It also returns the area the DEM must cover: the union of the bounding
rectangles of all bursts stackSentinel.py will select in those swaths (same
overlap test as ISCE2's ``Sentinel1.crop``), plus a margin.
"""

import logging
from dataclasses import dataclass

from osgeo import ogr

logger = logging.getLogger(__name__)

SWATH_NUMBERS = (1, 2, 3)
FOOTPRINT_HEIGHT_M = 0.0
# Margin (degrees) around the burst rectangles for the DEM area. Covers small
# burst position differences between acquisitions of the same track.
DEM_AREA_MARGIN_DEG = 0.1


@dataclass
class BurstFootprint:
    """Footprint of one burst.

    corners   : four (lat, lon) corners at sea level, in ring order
    rectangle : ISCE2's bounding rectangle [S, N, W, E] (``burst.getBbox()``),
                which stackSentinel.py uses to select bursts
    """

    corners: list
    rectangle: list


@dataclass
class SwathSelection:
    """Swaths to process and the [S, N, W, E] area the DEM must cover."""

    swaths: list
    dem_bbox: list


class SwathSelectionError(RuntimeError):
    """Raised when no swath covers the AOI or the reference SAFE cannot be read."""


def bbox_polygon(bbox):
    """Return an OGR polygon (lon/lat) for a [S, N, W, E] box."""
    south, north, west, east = bbox
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for lon, lat in ((west, south), (east, south), (east, north), (west, north), (west, south)):
        ring.AddPoint_2D(lon, lat)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def corners_polygon(corners):
    """Return an OGR polygon from four (lat, lon) corners given in ring order."""
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for lat, lon in list(corners) + [corners[0]]:
        ring.AddPoint_2D(lon, lat)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def burst_corners(burst, height=FOOTPRINT_HEIGHT_M):
    """Return the four (lat, lon) corners of an ISCE2 burst in ring order.

    Order: early/near, early/far, late/far, late/near range.
    """
    corners = []
    for time, slant_range in ((burst.sensingStart, burst.startingRange),
                              (burst.sensingStart, burst.farRange),
                              (burst.sensingStop, burst.farRange),
                              (burst.sensingStop, burst.startingRange)):
        lat, lon, _ = burst.orbit.rdr2geo(time, slant_range, height=height)
        corners.append((lat, lon))
    return corners


def read_swath_footprints(safe_files, orbit_dir, polarization):
    """Read the real burst footprints of every swath of one acquisition.

    Parameters
    ----------
    safe_files : list[Path]
        SAFE folders/zips of the reference date (several if the date is split).
    orbit_dir : Path
        Folder with the orbit files.
    polarization : str
        e.g. ``vv``.

    Returns
    -------
    dict[int, list[BurstFootprint]]
        swath number -> footprints of its bursts.
    """
    import isce  # noqa: F401  (sets up the ISCE2 component paths)
    from isceobj.Sensor.TOPS.Sentinel1 import Sentinel1

    footprints = {}
    for swath in SWATH_NUMBERS:
        reader = Sentinel1()
        reader.configure()
        reader.safe = [str(path) for path in safe_files]
        reader.swathNumber = swath
        reader.polarization = polarization
        reader.orbitDir = str(orbit_dir)
        try:
            reader.parse()
        except Exception as error:  # ISCE2 raises plain Exceptions here
            raise SwathSelectionError(f"Cannot read IW{swath} from {safe_files}: {error}") from error
        footprints[swath] = [
            BurstFootprint(corners=burst_corners(burst), rectangle=list(burst.getBbox()))
            for burst in reader.product.bursts
        ]
    return footprints


def swaths_covering_bbox(footprints, bbox):
    """Return the sorted swath numbers whose burst footprints intersect the box.

    Parameters
    ----------
    footprints : dict[int, list]
        Output of :func:`read_swath_footprints`.
    bbox : list[float]
        AOI as [S, N, W, E].

    Raises
    ------
    SwathSelectionError
        If no swath intersects the box.
    """
    aoi = bbox_polygon(bbox)
    selected = [
        swath for swath, bursts in sorted(footprints.items())
        if any(corners_polygon(burst.corners).Intersects(aoi) for burst in bursts)
    ]
    if not selected:
        raise SwathSelectionError(f"No Sentinel-1 swath covers the AOI {bbox}.")
    return selected


def find_reference_safes(slc_dir, reference_date):
    """Return the SAFE folders/zips of the reference date (or the first date).

    Raises
    ------
    SwathSelectionError
        If no SAFE exists for that date.
    """
    safes = sorted(list(slc_dir.glob("S1*_IW_SLC_*.SAFE")) + list(slc_dir.glob("S1*_IW_SLC_*.zip")))
    if not safes:
        raise SwathSelectionError(f"No Sentinel-1 SLC SAFE/zip files in {slc_dir}")

    def acquisition_date(path):
        return path.name.split("_")[5][:8]

    date = reference_date or min(acquisition_date(path) for path in safes)
    matching = [path for path in safes if acquisition_date(path) == date]
    if not matching:
        raise SwathSelectionError(f"No SLC for reference date {date} in {slc_dir}")
    return matching


def rectangles_overlap(box1, box2):
    """Return True if two [S, N, W, E] boxes overlap (same test as ISCE2 ``Sentinel1.crop``)."""
    horizontal = not (box1[2] >= box2[3] or box1[3] <= box2[2])
    vertical = not (box1[1] <= box2[0] or box1[0] >= box2[1])
    return horizontal and vertical


def dem_area_for_bursts(footprints, swaths, bbox, margin_deg=DEM_AREA_MARGIN_DEG):
    """Return the [S, N, W, E] area covering every burst stackSentinel.py will select.

    Raises
    ------
    SwathSelectionError
        If no burst of the given swaths overlaps the box.
    """
    rectangles = [
        burst.rectangle
        for swath in swaths
        for burst in footprints.get(swath, [])
        if rectangles_overlap(burst.rectangle, bbox)
    ]
    if not rectangles:
        raise SwathSelectionError(f"No burst of swaths {swaths} overlaps the AOI {bbox}.")
    return [
        min(r[0] for r in rectangles) - margin_deg,
        max(r[1] for r in rectangles) + margin_deg,
        min(r[2] for r in rectangles) - margin_deg,
        max(r[3] for r in rectangles) + margin_deg,
    ]


def select_swaths(config, bbox):
    """Choose the swaths to process and the area the DEM must cover.

    Uses the configured ``isce.swaths`` if set, otherwise the swaths whose real
    burst footprints intersect ``bbox``.

    Returns
    -------
    SwathSelection
    """
    safes = find_reference_safes(config.paths.slc_dir, config.isce.reference_date)
    footprints = read_swath_footprints(safes, config.paths.orbit_dir, config.isce.polarization)
    if config.isce.swaths:
        swaths = [int(swath) for swath in config.isce.swaths]
    else:
        swaths = swaths_covering_bbox(footprints, bbox)
        logger.info("Swaths covering the AOI (from real burst footprints): %s", swaths)
    dem_bbox = dem_area_for_bursts(footprints, swaths, bbox)
    logger.info("DEM area needed for the selected bursts [S, N, W, E]: %s",
                [round(value, 4) for value in dem_bbox])
    return SwathSelection(swaths=swaths, dem_bbox=dem_bbox)
