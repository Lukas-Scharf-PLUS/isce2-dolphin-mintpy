"""Tests for MintPy variants: config checks and preparation of the variant folders."""

from types import SimpleNamespace

import pytest

from isce_dolphin_mintpy import mintpy_variants
from isce_dolphin_mintpy.mintpy_template import build_template_options


def test_variant_options_are_converted_to_mintpy_text():
    variants = mintpy_variants.parse_variants(
        {"no_tropo": {"mintpy.troposphericDelay.method": False},
         "opera": {"mintpy.troposphericDelay.method": "opera"}})
    assert variants == {"no_tropo": {"mintpy.troposphericDelay.method": "no"},
                        "opera": {"mintpy.troposphericDelay.method": "opera"}}


def test_no_variants_gives_empty_mapping():
    assert mintpy_variants.parse_variants(None) == {}


@pytest.mark.parametrize("name", ["main", "with space", "a/b"])
def test_invalid_variant_names_are_rejected(name):
    with pytest.raises(ValueError, match="Variant name"):
        mintpy_variants.parse_variants({name: {"mintpy.deramp": "no"}})


def test_options_of_shared_steps_are_rejected():
    with pytest.raises(ValueError, match="share"):
        mintpy_variants.parse_variants({"x": {"mintpy.networkInversion.minTempCoh": 0.5}})


def test_empty_variant_is_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        mintpy_variants.parse_variants({"x": {}})


def test_variant_overrides_replace_main_options():
    mintpy_config = SimpleNamespace(template_options={"mintpy.troposphericDelay.method": "pyaps",
                                                      "mintpy.deramp": "quadratic"})
    options = build_template_options(mintpy_config, {"mintpy.troposphericDelay.method": "no"})
    assert options["mintpy.troposphericDelay.method"] == "no"
    assert options["mintpy.deramp"] == "quadratic"


@pytest.mark.parametrize("file_name, shared", [
    ("timeseries.h5", True),
    ("timeseries_SET.h5", True),
    ("timeseries_LOD_SET_ion.h5", True),
    ("timeseries_SET_ERA5.h5", False),
    ("timeseriesResidual.h5", False),
])
def test_shared_timeseries_files_are_recognized(tmp_path, file_name, shared):
    assert mintpy_variants.is_shared_timeseries_file(tmp_path / file_name) == shared


def make_main_folder(mintpy_dir):
    """Create a fake main MintPy folder after the shared steps."""
    (mintpy_dir / "inputs").mkdir(parents=True)
    for name in ("timeseries.h5", "timeseries_SET.h5", "timeseries_SET_ERA5.h5",
                 "timeseriesResidual.h5", "maskTempCoh.h5", "temporalCoherence.h5"):
        (mintpy_dir / name).write_text(name)


def test_variant_folder_copies_time_series_and_links_the_rest(tmp_path):
    make_main_folder(tmp_path)
    target = mintpy_variants.variant_dir(tmp_path, "no_tropo")

    mintpy_variants.prepare_variant_dir(tmp_path, target)

    for name in ("timeseries.h5", "timeseries_SET.h5"):
        assert (target / name).is_file() and not (target / name).is_symlink()
    for name in ("inputs", "maskTempCoh.h5", "temporalCoherence.h5"):
        assert (target / name).is_symlink()
    # Results of the main run's per-run steps are not taken over.
    assert not (target / "timeseries_SET_ERA5.h5").exists()
    assert not (target / "timeseriesResidual.h5").exists()


def test_preparing_twice_replaces_links_and_copies(tmp_path):
    make_main_folder(tmp_path)
    target = mintpy_variants.variant_dir(tmp_path, "no_tropo")
    mintpy_variants.prepare_variant_dir(tmp_path, target)
    (tmp_path / "timeseries_SET.h5").write_text("new")

    mintpy_variants.prepare_variant_dir(tmp_path, target)

    assert (target / "timeseries_SET.h5").read_text() == "new"


def test_variant_folder_needs_the_shared_time_series(tmp_path):
    (tmp_path / "inputs").mkdir()
    with pytest.raises(FileNotFoundError, match="timeseries.h5"):
        mintpy_variants.prepare_variant_dir(tmp_path, tmp_path / "variants" / "x")
