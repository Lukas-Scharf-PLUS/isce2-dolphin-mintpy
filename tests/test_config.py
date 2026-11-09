"""Tests for loading and validating the pipeline configuration."""

import pytest
import yaml

from isce_dolphin_mintpy.config import ConfigError, load_config
from isce_dolphin_mintpy.mintpy_template import (
    build_template_options,
    find_mintpy_defaults_dir,
    read_known_option_names,
)


@pytest.fixture
def minimal_config(tmp_path):
    """Write a minimal valid config with fake input paths; return its path."""
    (tmp_path / "slc").mkdir()
    (tmp_path / "orbits").mkdir()
    (tmp_path / "dem.tif").write_text("")
    content = {
        "paths": {
            "slc_dir": str(tmp_path / "slc"),
            "orbit_dir": str(tmp_path / "orbits"),
            "dem_file": str(tmp_path / "dem.tif"),
            "dem_vertical_datum": "EGM2008",
            "work_dir": str(tmp_path / "work"),
        },
        "aoi": {"bbox": [48.1, 48.2, 16.3, 16.4]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(content, sort_keys=False))
    return path


def rewrite(path, update):
    """Apply ``update(content)`` to the YAML file at ``path``."""
    content = yaml.safe_load(path.read_text())
    update(content)
    path.write_text(yaml.safe_dump(content, sort_keys=False))


def test_minimal_config_loads_with_defaults(minimal_config):
    config = load_config(minimal_config)
    assert config.dolphin.strides_x == 4
    assert config.mintpy.inversion == "mintpy"
    assert config.isce_dir == config.paths.work_dir / "isce"


def test_unknown_option_is_rejected(minimal_config):
    rewrite(minimal_config, lambda c: c.update({"dolphin": {"stride_x": 3}}))
    with pytest.raises(ConfigError, match="stride_x"):
        load_config(minimal_config)


def test_bbox_order_is_checked(minimal_config):
    rewrite(minimal_config, lambda c: c["aoi"].update({"bbox": [48.2, 48.1, 16.3, 16.4]}))
    with pytest.raises(ConfigError, match="bbox"):
        load_config(minimal_config)


def test_dem_vertical_datum_is_normalized(minimal_config):
    assert load_config(minimal_config).paths.dem_vertical_datum == "egm2008"


def test_missing_dem_vertical_datum_is_rejected(minimal_config):
    rewrite(minimal_config, lambda c: c["paths"].pop("dem_vertical_datum"))
    with pytest.raises(ConfigError, match="dem_vertical_datum"):
        load_config(minimal_config)


def test_invalid_inversion_mode_is_rejected(minimal_config):
    rewrite(minimal_config, lambda c: c.update({"mintpy": {"inversion": "both"}}))
    with pytest.raises(ConfigError, match="inversion"):
        load_config(minimal_config)


def test_mintpy_options_use_original_names_in_file_order(minimal_config):
    rewrite(minimal_config, lambda c: c.update({"mintpy": {
        "inversion": "mintpy",
        "mintpy.compute.numWorker": "all",
        "mintpy.networkInversion.maskDataset": "connectComponent",
        "mintpy.deramp": "quadratic",
    }}))
    config = load_config(minimal_config)
    assert config.mintpy.inversion == "mintpy"
    options = build_template_options(config.mintpy)
    assert list(options) == ["mintpy.load.processor", "mintpy.compute.numWorker",
                             "mintpy.networkInversion.maskDataset", "mintpy.deramp"]
    assert options["mintpy.deramp"] == "quadratic"


def test_unknown_mintpy_option_is_rejected(minimal_config):
    rewrite(minimal_config, lambda c: c.update({"mintpy": {"mintpy.deRamp": "linear"}}))
    with pytest.raises(ConfigError, match="mintpy.deRamp"):
        load_config(minimal_config)


def test_subset_option_is_passed_on_with_warning(minimal_config, caplog):
    rewrite(minimal_config, lambda c: c.update({"mintpy": {"mintpy.subset.lalo": "48.1:48.2,16.3:16.4"}}))
    options = build_template_options(load_config(minimal_config).mintpy)
    assert options["mintpy.subset.lalo"] == "48.1:48.2,16.3:16.4"
    assert "aoi.bbox" in caplog.text


def append_mintpy_section(path, lines):
    """Append a raw ``mintpy:`` section (YAML text, as a user writes it) to the config."""
    path.write_text(path.read_text() + "mintpy:\n" + "".join(f"  {line}\n" for line in lines))


def test_every_option_of_mintpy_default_template_can_be_passed(minimal_config):
    defaults_dir = find_mintpy_defaults_dir()
    if defaults_dir is None:
        pytest.skip("MintPy is not installed")
    names = sorted(read_known_option_names(defaults_dir))
    append_mintpy_section(minimal_config, [f"{name}: auto" for name in names])
    options = build_template_options(load_config(minimal_config).mintpy)
    assert all(options[name] == "auto" for name in names)


@pytest.mark.parametrize("yaml_value, mintpy_text", [
    ("20080520_20090817", "20080520_20090817"),   # YAML 1.1 would make this an integer
    ("1:5", "1:5"),                               # YAML 1.1 would make this 65
    ("1:5,25", "1:5,25"),
    ("-1.2,0.5,-92,-91", "-1.2,0.5,-92,-91"),
    ("20110311,60;20170908,120", "20110311,60;20170908,120"),
    ("np.nan", "np.nan"),
    ("20220101", "20220101"),
    ("0.7", "0.7"),
    ("no", "no"),
    ("[48.19767391928604, 16.325432508158606]", "48.19767391928604, 16.325432508158606"),
])
def test_mintpy_values_reach_the_template_unchanged(minimal_config, yaml_value, mintpy_text):
    append_mintpy_section(minimal_config, [f"mintpy.network.excludeDate12: {yaml_value}"])
    options = build_template_options(load_config(minimal_config).mintpy)
    assert options["mintpy.network.excludeDate12"] == mintpy_text


def test_other_sections_still_read_numbers(minimal_config):
    rewrite(minimal_config, lambda c: c.update({"dolphin": {"strides_x": 2},
                                                "water_mask": {"max_water_fraction": 0.5}}))
    config = load_config(minimal_config)
    assert config.dolphin.strides_x == 2 and config.water_mask.max_water_fraction == 0.5


def test_old_option_name_points_to_mintpy_name(minimal_config):
    rewrite(minimal_config, lambda c: c.update({"mintpy": {"troposphere": "no"}}))
    with pytest.raises(ConfigError, match="mintpy.troposphericDelay.method"):
        load_config(minimal_config)


def test_non_numeric_min_temporal_coherence_is_rejected(minimal_config):
    rewrite(minimal_config, lambda c: c.update(
        {"mintpy": {"mintpy.networkInversion.minTempCoh": "high"}}))
    with pytest.raises(ConfigError, match="minTempCoh"):
        load_config(minimal_config)
