"""Tests for reading topsStack run files."""

from isce_dolphin_mintpy.isce_stack import (
    find_isce_raster,
    find_merged_slcs,
    list_run_files,
    read_run_file_commands,
)


def test_run_file_commands_run_in_foreground(tmp_path):
    run_file = tmp_path / "run_07_pairs_misreg"
    run_file.write_text(
        "SentinelWrapper.py -c configs/config_a &\n"
        "\n"
        "# comment\n"
        "SentinelWrapper.py -c configs/config_b&\n"
        "wait\n"
        "SentinelWrapper.py -c configs/config_c\n"
    )
    assert read_run_file_commands(run_file) == [
        "SentinelWrapper.py -c configs/config_a",
        "SentinelWrapper.py -c configs/config_b",
        "SentinelWrapper.py -c configs/config_c",
    ]


def test_run_files_are_sorted_by_step_number(tmp_path):
    run_dir = tmp_path / "run_files"
    run_dir.mkdir()
    for name in ("run_10_fullBurst_resample", "run_02_unpack_secondary_slc",
                 "run_01_unpack_topo_reference", "run_01_unpack_topo_reference.job"):
        (run_dir / name).write_text("")
    assert [p.name for p in list_run_files(tmp_path)] == [
        "run_01_unpack_topo_reference", "run_02_unpack_secondary_slc", "run_10_fullBurst_resample",
    ]



def test_merged_binary_is_preferred_over_burst_mosaic_vrt(tmp_path):
    # <name>.full.vrt is a mosaic of the burst files (for the reference date they
    # point into the raw .SAFE); the merged binary <name>.full must win.
    date_dir = tmp_path / "merged" / "SLC" / "20220113"
    date_dir.mkdir(parents=True)
    for suffix in (".full", ".full.vrt", ".vrt", ""):
        (date_dir / f"20220113.slc{suffix}").write_text("")
    assert find_merged_slcs(tmp_path) == {"20220113": date_dir / "20220113.slc.full"}


def test_burst_mosaic_vrt_is_used_when_binary_is_missing(tmp_path):
    (tmp_path / "lat.rdr.full.vrt").write_text("")
    (tmp_path / "lat.rdr").write_text("")
    assert find_isce_raster(tmp_path / "lat.rdr") == tmp_path / "lat.rdr.full.vrt"
