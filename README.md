# isce2-dolphin-mintpy

> Work in progress. Functionality and correctness are not guaranteed; use at your own risk.

An InSAR time-series pipeline for Sentinel-1, built for the city of Vienna. It chains three tools:

| Stage | Tool | Why this tool |
| --- | --- | --- |
| Coregistration | **ISCE2 topsStack** | Enhanced spectral diversity (ESD) gives accurate azimuth coregistration across burst boundaries. |
| Phase linking, interferograms, unwrapping | **dolphin** | Combined persistent and distributed scatterer processing (phase linking). |
| Corrections, time series, geocoding | **MintPy** | Tropospheric, DEM error, ramp and solid earth tide corrections, plus reference point handling. |

Data downloads are not part of this project. SLCs and orbit files come from the
[SLC_downloader](../SLC_downloader). A lat/lon DEM must already exist. It is converted to ellipsoid heights automatically (see DEM preparation).

---

## Setup

```bash
mamba env create -f environment.yml
conda activate isce2-dolphin-mintpy
```

The environment contains only `isce2`, `dolphin`, `snaphu`, `mintpy`, `pyaps3`, `gdal`, `pyyaml` and `pytest`.
The pipeline looks for the topsStack scripts (`stackSentinel.py`) in this order:

1. `isce.topsstack_dir` in the config.
2. `stackSentinel.py` already on `PATH`.
3. `$CONDA_PREFIX/share/isce2/topsStack`, where conda-forge's `isce2` puts them.

If none of these exist, clone `https://github.com/isce-framework/isce2` and set
`isce.topsstack_dir: <clone>/contrib/stack/topsStack`.

**ERA5 tropospheric correction** (`mintpy.troposphere: pyaps`) needs a Copernicus Climate Data Store account
with its key in `~/.cdsapirc`. See the [PyAPS documentation](https://github.com/insarlab/PyAPS).
Without that file, use `height_correlation`, `gacos` or `no`.

No environment variables are required.

## How to run

```bash
conda activate isce2-dolphin-mintpy
cd isce2-dolphin-mintpy

# all steps
python -m isce_dolphin_mintpy run config/test_asc073.yaml

# only some steps (always in pipeline order)
python -m isce_dolphin_mintpy run config/test_asc073.yaml --steps crop dolphin mintpy
```

Steps: `isce`, `crop`, `dolphin`, `mintpy`. The ISCE2 step can be restarted after an interruption.
Finished topsStack run files are marked in `isce/run_status/` and skipped.
Relative paths in the config are resolved against the current directory.

## Run with Docker

GitHub Actions builds an image on every push to `main` and publishes it as
`ghcr.io/lukas-scharf-plus/isce2-dolphin-mintpy` (see `.github/workflows/docker.yml`):

| Event | Image tags |
| --- | --- |
| push to `main` | `latest`, `sha-<commit>` |
| tag `v1.2.3` | `1.2.3`, `1.2` |
| pull request | built and tested only, not published |

The image contains the conda environment, the pipeline code, the configs and the EGM2008/EGM96 geoid grids.
The entry point is `python -m isce_dolphin_mintpy`, and `docker run` arguments are appended to it.
Inside the container the working directory is `/app`, so a relative `work_dir` such as `runs/test_asc073` ends up in
`/app/runs`. Mount that folder to keep the results. The paths in the config must be paths *inside* the container. The
simplest way is to mount the data folder at the same path as on the host:

```bash
docker pull ghcr.io/lukas-scharf-plus/isce2-dolphin-mintpy:latest

docker run --rm --user "$(id -u):$(id -g)" \
  -v ~/projects/ADUCAT/data:/home/lukas/projects/ADUCAT/data:ro \
  -v "$PWD/runs":/app/runs \
  ghcr.io/lukas-scharf-plus/isce2-dolphin-mintpy:latest \
  run config/test_asc073.yaml
```

To use your own config, mount it as well, e.g. `-v "$PWD/my_config.yaml":/app/my_config.yaml`, and pass
`run my_config.yaml`. `--user` makes the output files belong to you instead of the container user. The input data
can be mounted read-only (`:ro`) because the pipeline never changes it.

Build the image locally:

```bash
docker build -t isce2-dolphin-mintpy .
```

## How to run the tests

```bash
python -m pytest tests
```

The unit tests use small synthetic rasters and need no SAR data.

## Project structure

```
environment.yml            conda environment (only the packages the pipeline uses)
Dockerfile                 container image (micromamba + environment.yml + geoid grids)
.dockerignore              files kept out of the Docker build
.github/workflows/
  docker.yml               builds, tests and publishes the image to ghcr.io
config/
  example.yaml             every option, with explanations and defaults
  test_asc073.yaml         3-date test over central Vienna (ascending track 73)
isce_dolphin_mintpy/
  __main__.py              command line entry point
  config.py                YAML config -> validated dataclasses
  aoi.py                   AOI bounding box (from bbox or vector file) and buffer
  shell.py                 runs external commands with per-command log files
  isce_stack.py            step "isce": stackSentinel.py + run files; finds ISCE2 outputs
  swath_selection.py       chooses IW1/2/3 from the real burst footprints + DEM area
  dem_preparation.py       cuts the DEM, converts geoid -> ellipsoid heights, writes ISCE DEM
  radar_crop.py            step "crop": AOI -> radar pixel window -> VRT windows
  dolphin_step.py          step "dolphin": writes dolphin config, runs dolphin
  mintpy_inputs.py         dolphin outputs + ISCE2 metadata -> MintPy HDF5 inputs
  mintpy_step.py           step "mintpy": MintPy template + smallbaselineApp.py runs
tests/                     unit tests
runs/                      processing output of local runs (git-ignored)
```

Output layout inside `paths.work_dir`:

```
isce/      ISCE2 topsStack work dir (merged/SLC, merged/geom_reference, reference, baselines, ...)
crop/      VRT windows of the SLCs and geometry for the AOI + crop_window.json
dolphin/   dolphin work dir (dolphin_config.yaml, interferograms/, unwrapped/, timeseries/)
mintpy/    MintPy work dir (inputs/, timeseries*.h5, velocity.h5, geo/)
logs/      pipeline.log and one log per external command
```

## How it works

### 1. ISCE2 coregistration (`isce`)

`stackSentinel.py` runs the SLC workflow (`-W slc`) with `NESD` coregistration, which is geometry plus ESD.
The buffered AOI box is passed as `-b`, so **only bursts that overlap the AOI** are processed.
ISCE2 cannot cut out smaller parts of a burst.

**Swath selection.** stackSentinel keeps every burst whose lat/lon *bounding rectangle* overlaps the AOI.
These rectangles are much larger than the real footprints, because the swaths are tilted and the rectangle covers
heights from -500 m to 9000 m. Near a swath border, that can add a swath that does not cover the AOI at all and
contributes a single burst. ESD needs the overlap between two consecutive bursts, so topsStack then fails in
`run_07_pairs_misreg`. In the Vienna test, IW2 was added this way.
`swath_selection.py` therefore reads the real burst footprints of the reference date with ISCE2's Sentinel-1 reader.
It passes only the swaths that actually intersect the AOI to stackSentinel (`-n`), unless `isce.swaths` is set.

The run files from stackSentinel end each command with `&`, meant for running them as shell scripts. The pipeline
removes the `&` and runs the commands itself, `isce.num_proc` at a time. Each step waits until all its commands have
finished and stops on the first failure.
Multilooking is off (`-z 1 -r 1`), because dolphin needs full-resolution SLCs.

**DEM preparation.** ISCE2 treats DEM values as heights above the WGS84 **ellipsoid**. Common DEMs store heights above a
**geoid** instead: the Copernicus DEM uses EGM2008 and SRTM uses EGM96. Around Vienna the geoid lies about 45 m above
the ellipsoid. Geoid heights would shift the radar geolocation by roughly 45 m / tan(incidence), about 60 to 70 m, and
leave a residual topographic phase. GeoTIFFs rarely record their vertical reference, so the pipeline cannot detect it.
The config must therefore state it in `paths.dem_vertical_datum`, as `egm2008`, `egm96` or `ellipsoid`.

`dem_preparation.py` then:

1. Cuts the DEM to the area of the bursts stackSentinel will process, plus 0.1°.
2. Adds the geoid undulation from PROJ's official EGM grid (`us_nga_egm08_25.tif` or `us_nga_egm96_15.tif`).
   PROJ downloads the grid from cdn.proj.org on first use and caches it. If the grid is missing, PROJ would
   silently apply no shift. The pipeline requires the exact grid and stops with an error instead.
   To download the grid in advance, run `projsync --file us_nga_egm08_25.tif`.
3. Writes `isce/DEM/dem_ellipsoid.wgs84` with an ISCE header at full precision and with absolute paths. ISCE2 reads
   the header start coordinate as the *center* of the first pixel. GDAL gives the *corner*, so half a pixel is added.
   ISCE2's `gdal2isce_xml.py` skips that step and shifts the DEM by half a pixel.

The original DEM is never changed.

### 2. Crop to the AOI in radar coordinates (`crop`)

dolphin's `output_options.bounds` does not reduce the processing area. It only masks pixels, and only for
geocoded input. For ISCE2's radar-coordinate stack the pipeline crops instead:

1. It reads the full-resolution ISCE2 `lat.rdr` and `lon.rdr` block by block. It then finds the smallest
   row and column window that contains the buffered AOI box.
2. It grows the window to a multiple of dolphin's strides, so the dolphin output grid has a known size.
3. It writes GDAL VRT files that point into the ISCE2 rasters. One VRT is written per SLC and per geometry
   layer. No data is copied. dolphin then processes only the AOI.

### 3. dolphin (`dolphin`)

The config is built with dolphin's own `DisplacementWorkflow` class, so dolphin validates every option.
It is saved as `dolphin/dolphin_config.yaml`. The radar wavelength comes from the ISCE2 reference XML.
dolphin runs phase linking, forms a nearest-neighbour interferogram network (`max_bandwidth`)
and unwraps it with SNAPHU.

### 4. MintPy (`mintpy`)

**Converting dolphin outputs.** MintPy's `load_data` cannot read dolphin's radar-coordinate GeoTIFFs correctly.
It derives `X_FIRST`/`Y_FIRST` from the GeoTIFF, decides the data are geocoded, and then fails to find
`geometryGeo.h5`. Several people have reported this, including
[dolphin discussion #677](https://github.com/isce-framework/dolphin/issues/677) and a MintPy forum thread.

`mintpy_inputs.py` therefore writes `inputs/ifgramStack.h5` and `inputs/geometryRadar.h5` directly:

- **Metadata** comes from MintPy's own ISCE2/topsStack reader of `reference/IW*.xml`. It is then adapted to the
  cropped and strided grid: pixel sizes, looks, starting range of the first output column, and corner coordinates.
- **Geometry** is sampled from the ISCE2 rasters at dolphin's output pixel centers. With strides (sy, sx),
  output pixel (i, j) sits on input pixel (i·sy + sy//2, j·sx + sx//2).
- **Perpendicular baselines** come from the ISCE2 `baselines/` folder.
- **Signs**: dolphin forms interferograms as `ref · conj(sec)`, like ISCE2. dolphin and MintPy both use
  `-λ/4π` (positive = toward the satellite). The unwrapped phase is therefore used unchanged.

[Dolphin2MintPy](https://github.com/bcankara/Dolphin2MintPy) solves the same problem with `.rsc` sidecar files.
It was used as a reference, but it is not a dependency. It hardcodes satellite height, earth radius and
azimuth pixel size, and it sets `PROCESSOR=hyp3` temporarily. This pipeline takes those values from the ISCE2 metadata.

**Two inversion modes** (`mintpy.inversion`):

| Mode | Time series from | What MintPy does |
| --- | --- | --- |
| `mintpy` (default) | MintPy network inversion of dolphin's interferograms | network modification, reference point, optional unwrapping error correction, inversion, all corrections |
| `dolphin` | dolphin's own inversion (`dolphin/timeseries/`) | reference point, then all corrections |

In `mintpy` mode the pixel mask (`maskTempCoh.h5`) is additionally restricted to pixels with dolphin temporal
coherence ≥ `dolphin_temporal_coherence_threshold`. This is needed because phase-linked interferograms have
almost no closure phase, so MintPy's own temporal coherence is close to 1 nearly everywhere and hardly filters
anything. In `dolphin` mode, dolphin's temporal coherence becomes MintPy's `temporalCoherence.h5`.

MintPy steps are run one by one with `smallbaselineApp.py --dostep`, without `load_data` and without
`quick_overview`. `quick_overview` only makes diagnostic plots. On phase-linked interferograms every triplet closes,
which makes MintPy 1.6's closure-phase histogram crash with "`bins` must be positive".

After the inversion, all steps from `correct_LOD` to the end run. That covers the troposphere, DEM error,
deramping, reference date, velocity and geocoding with the ISCE2 lat/lon lookup.

## Test result (3 dates, central Vienna)

The test config `config/test_asc073.yaml` runs end to end. ISCE2 processes 3 bursts of IW1, and the AOI crop is
1080 x 515 full-resolution pixels. The dolphin output with strides 4 x 1 is 270 x 515 pixels.

| Check | Result |
| --- | --- |
| ESD azimuth misregistration per date | 0.0, 0.0007, -0.0005 pixels |
| Geocoded radar height minus original DEM | median 0.00 m, median absolute deviation 1.7 m |
| Geocoded extent | 48.163-48.233 N, 16.321-16.395 E (AOI + 500 m buffer) |
| Median interferogram coherence | 0.73-0.85 |

The height check shows that dolphin's output grid, the sampled ISCE2 geometry and MintPy's geocoding line up.
With only 3 dates the temporal coherence is close to 1 everywhere, so the masks remove almost nothing, and the
24-day "velocity" is not meaningful.

## Known limitations

- ISCE2 always processes complete bursts. The AOI crop only affects dolphin and MintPy.
- An open ISCE2 pull request ([#28139](https://github.com/isce-framework/isce2/pull/28139)) reports phase jumps
  at burst boundaries after ESD in some stacks. It is not merged or reviewed. Check burst boundaries in the results.
- With very few dates, the DEM error correction and temporal coherence thresholds are not meaningful.
  The test config switches the DEM error correction off.
- `aoi.file` uses the bounding box of the vector file, not its exact outline.
