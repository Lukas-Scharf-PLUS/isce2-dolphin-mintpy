"""Tests for choosing swaths from real (tilted) burst footprints."""

import pytest

from isce_dolphin_mintpy.swath_selection import (
    BurstFootprint,
    SwathSelectionError,
    dem_area_for_bursts,
    swaths_covering_bbox,
)

# Tilted footprints like an ascending Sentinel-1 track near Vienna:
# corners (lat, lon) as early/near, early/far, late/far, late/near.
IW1_BURST = [(47.9, 15.40), (47.95, 16.70), (48.30, 16.62), (48.25, 15.30)]
IW2_BURST = [(48.0, 16.45), (48.05, 17.80), (48.40, 17.72), (48.35, 16.36)]


def footprint(corners):
    """Wrap corners as BurstFootprint with their bounding rectangle."""
    lats = [lat for lat, _ in corners]
    lons = [lon for _, lon in corners]
    return BurstFootprint(corners=corners, rectangle=[min(lats), max(lats), min(lons), max(lons)])


def test_only_swath_with_real_coverage_is_selected():
    footprints = {1: [footprint(IW1_BURST)], 2: [footprint(IW2_BURST)]}
    # AOI at 16.34-16.38 E: inside IW1; IW2's west edge is further east at 48.2 N,
    # although IW2's bounding rectangle (west edge 16.36) overlaps the AOI.
    assert swaths_covering_bbox(footprints, [48.17, 48.22, 16.34, 16.38]) == [1]


def test_aoi_across_swath_border_selects_both_swaths():
    footprints = {1: [footprint(IW1_BURST)], 2: [footprint(IW2_BURST)]}
    assert swaths_covering_bbox(footprints, [48.15, 48.20, 16.40, 16.60]) == [1, 2]


def test_aoi_outside_all_swaths_raises():
    with pytest.raises(SwathSelectionError):
        swaths_covering_bbox({1: [footprint(IW1_BURST)]}, [10.0, 11.0, 10.0, 11.0])


def test_dem_area_covers_all_selected_burst_rectangles():
    north_burst = [(48.25, 15.25), (48.30, 16.60), (48.65, 16.52), (48.60, 15.20)]
    far_burst = [(49.5, 15.0), (49.55, 16.3), (49.9, 16.2), (49.85, 14.9)]
    footprints = {1: [footprint(IW1_BURST), footprint(north_burst), footprint(far_burst)],
                  2: [footprint(IW2_BURST)]}
    south, north, west, east = dem_area_for_bursts(footprints, [1], [48.17, 48.27, 16.34, 16.38],
                                                   margin_deg=0.0)
    # both IW1 bursts touching the AOI box are covered, the far burst and IW2 are not
    assert (south, north, west, east) == (47.9, 48.65, 15.2, 16.7)
