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

The environment contains only `isce2`, `dolphin` (0.42.8), `snaphu`, `mintpy`, `pyaps3`, `gdal`, `pyyaml` and `pytest`,
plus the whirlwind unwrapper `whirlwind-insar==0.10.0` from pip.
The pipeline looks for the topsStack scripts (`stackSentinel.py`) in this order:

1. `isce.topsstack_dir` in the config.
2. `stackSentinel.py` already on `PATH`.
3. `$CONDA_PREFIX/share/isce2/topsStack`, where conda-forge's `isce2` puts them.

If none of these exist, clone `https://github.com/isce-framework/isce2` and set
`isce.topsstack_dir: <clone>/contrib/stack/topsStack`.

**ERA5 tropospheric correction** (`mintpy.troposphericDelay.method: pyaps`, MintPy's default) needs a
Copernicus Climate Data Store account with its key in `~/.cdsapirc`. See the [PyAPS documentation](https://github.com/insarlab/PyAPS).
Without that file, use `height_correlation`, `gacos` or `"no"`.

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

## Configuration

One YAML file describes a run. Start from [config/example.yaml](config/example.yaml), which documents every
pipeline option.

| Section | What it holds | Option names |
| --- | --- | --- |
| `paths` | SLC, orbit and DEM inputs, DEM vertical datum, `work_dir` | pipeline names |
| `aoi` | area of interest (`bbox` or vector `file`) and buffer | pipeline names |
| `water_mask` | optional water mask raster and how it is used | pipeline names |
| `isce` | stackSentinel.py options (reference date, swaths, ESD, parallelism) | pipeline names |
| `inversion` | a single top-level option, not a section: `mintpy` (MintPy network inversion, default) or `dolphin` (dolphin's time series, MintPy only corrects). It affects both the dolphin and the MintPy step | `mintpy` / `dolphin` |
| `dolphin` | any dolphin option: phase linking, network, unwrapper (snaphu, whirlwind, ...), time series, workers | dolphin's own nested structure, all listed in [config/dolphin_options_reference.yaml](config/dolphin_options_reference.yaml) |
| `mintpy` | `dolphin_temporal_coherence_threshold` and any `smallbaselineApp.cfg` option | `mintpy.*` keys with MintPy's original names (`smallbaselineApp.py -H`) |

Checked before ISCE2 starts:

- when the config is loaded:
  - unknown options in every section, including dolphin options (checked with dolphin's own model) and
    `mintpy.*` names (checked against MintPy's default templates)
  - that the chosen unwrapper is installed
- when the `isce` step is selected: that an explicit `mintpy.reference.date` is an SLC date

Options that only the pipeline may set (input files, work directories, mask, wavelength) give an error with the
reason. Details are in "How it works": [dolphin](#3-dolphin-dolphin) and [MintPy](#4-mintpy-mintpy).

```yaml
isce:
  reference_date: "20220113"
inversion: mintpy                # top level, between isce and dolphin
dolphin:
  unwrap_options:
    unwrap_method: whirlwind
mintpy:
  mintpy.unwrapError.method: bridging+phase_closure
  mintpy.reference.date: "20220101"
```

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
  example.yaml             every pipeline option, with explanations and defaults
  dolphin_options_reference.yaml  every dolphin option with description (generated from dolphin)
  test_asc073.yaml         3-date test over central Vienna (ascending track 73)
isce_dolphin_mintpy/
  __main__.py              command line entry point
  config.py                YAML config -> validated dataclasses
  aoi.py                   AOI bounding box (from bbox or vector file) and buffer
  shell.py                 runs external commands with per-command log files
  versions.py              writes logs/versions.json (ISCE2, dolphin, MintPy, pipeline commit)
  isce_stack.py            step "isce": stackSentinel.py + run files; finds ISCE2 outputs
  swath_selection.py       chooses IW1/2/3 from the real burst footprints + DEM area
  dem_preparation.py       cuts the DEM, converts geoid -> ellipsoid heights, writes ISCE DEM
  radar_crop.py            step "crop": AOI -> radar pixel window -> VRT windows
  water_mask.py            step "crop": geocoded water mask -> water fraction on dolphin's grid
  dolphin_options.py       dolphin section of the config -> checked dolphin options (defaults, owned keys)
  dolphin_step.py          step "dolphin": writes dolphin config, runs dolphin
  mintpy_inputs.py         dolphin outputs + ISCE2 metadata -> MintPy HDF5 inputs
  mintpy_template.py       mintpy.* options of the config -> checked MintPy template file
  mintpy_step.py           step "mintpy": smallbaselineApp.py runs, step by step with plots
tests/                     unit tests (one test_<module>.py per module)
runs/                      processing output of local runs (git-ignored)
```

Output layout inside `paths.work_dir`:

```
isce/      ISCE2 topsStack work dir (merged/SLC, merged/geom_reference, reference, baselines, ...)
crop/      VRT windows of the SLCs and geometry for the AOI + crop_window.json
dolphin/   dolphin work dir (dolphin_config.yaml, interferograms/, unwrapped/, timeseries/)
mintpy/    MintPy work dir (inputs/, timeseries*.h5, velocity.h5, geo/, pic/ = figures of all steps)
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

### 2b. Water mask in radar coordinates (`crop`, optional)

A geocoded water mask, such as `watermask.tif` in WGS84 with 1 = land and 0 = water, can't be passed to dolphin or MintPy
directly, because both work in radar coordinates. When `water_mask.file` is set, the `crop` step computes the
**water fraction** of every dolphin output pixel. For each pixel it places sample points about 2 m apart inside
the pixel's radar footprint, interpolates their lat/lon from the ISCE2 geometry, and looks them up in the mask.
Mask no-data is ignored.

A pixel counts as water only if **more than `max_water_fraction`** of it is water. The default is 0.8.
This is deliberately conservative. Shore pixels can contain strong scatterers such as quay walls or bridge piers,
and a masked pixel can't be recovered later. Pixels that turn out bad are still removed by the coherence masks.

| File | Content | Used by |
| --- | --- | --- |
| `crop/water/water_fraction.tif` | water fraction 0..1 on dolphin's output grid | MintPy (`waterFraction`) |
| `crop/water/valid_mask.tif` | 1 = keep, 0 = water | dolphin `mask_file` (applied in unwrapping) |

In MintPy's `geometryRadar.h5` the pipeline adds `waterMask` (True = land) and `waterFraction`. The fraction is kept,
so a different threshold can be tried later in MintPy without rerunning dolphin, e.g. with
`generate_mask.py inputs/geometryRadar.h5 waterFraction -M 0.5`.

Check whether bridges are water in your mask. If they are, bridges, which are strong and stable scatterers, are masked
at any threshold.

### 3. dolphin (`dolphin`)

dolphin runs phase linking, forms the interferogram network (by default each date with its 3 nearest
neighbours) and unwraps it with SNAPHU or whirlwind.

**dolphin options in the config.** The `dolphin` section uses dolphin's own config structure, with the same
names and nesting as a dolphin YAML file. Every dolphin option can be set there:

```yaml
dolphin:
  output_options:
    strides: {x: 4, y: 1}
  unwrap_options:
    unwrap_method: whirlwind
    n_parallel_jobs: -1
    whirlwind_options:
      num_threads: 8
```

- [config/dolphin_options_reference.yaml](config/dolphin_options_reference.yaml) lists every option with its
  description and this pipeline's default. Copy the parts you need. The file is generated from dolphin itself;
  its header shows how to regenerate it after a dolphin update.
- Your options are merged key by key over the pipeline defaults (`PIPELINE_DEFAULT_OPTIONS` in
  `dolphin_options.py`). Setting `unwrap_options.unwrap_method` therefore keeps the default `snaphu_options`.
- The pipeline always sets `cslc_file_list`, `work_directory`, `mask_file` (from `water_mask`),
  `input_options.wavelength` (from the ISCE2 reference XML) and `input_options.cslc_date_fmt`. Setting them in the
  config gives an error. `unwrap_options.run_unwrap` cannot be `false`, because MintPy needs unwrapped
  interferograms.
- `timeseries_options.run_inversion`/`run_velocity` follow the top-level `inversion` option. In `mintpy` mode you can still set
  them to `true` to get dolphin's time series as well, for comparison.
- When the config is loaded, the options are checked with dolphin's own model. A typo, a wrong type or an invalid
  choice stops the run before ISCE2 starts. The pipeline also checks that the Python module of the chosen
  unwrapper is installed. Otherwise dolphin would only fail after phase linking.
- The old flat names (`strides_x`, `unwrap_method`, `n_parallel_unwrap`, ...) give an error that names the new path.

The complete config is saved as `dolphin/dolphin_config.yaml`.

**whirlwind** comes from the pip package `whirlwind-insar==0.10.0`, the same version as the compass-dolphin
pipeline. The PyPI package named `whirlwind` is unrelated. dolphin is pinned to 0.42.8, whose whirlwind backend was
tested with it. Test on the 3-date stack (20220101_20220125): the whirlwind result is congruent with the wrapped
phase, and after removing a constant it differs from SNAPHU by more than π on 0.35 % of the pixels.
whirlwind shares one thread pool (`whirlwind_options.num_threads`, `null` = all CPUs) across the
`n_parallel_jobs` interferograms it unwraps at the same time.

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
- **Connected components**: dolphin's no-data value (65535) becomes 0. In MintPy, 0 means unreliable unwrapping.
- **Signs**: dolphin forms interferograms as `ref · conj(sec)`, like ISCE2. dolphin and MintPy both use
  `-λ/4π` (positive = toward the satellite). The unwrapped phase is therefore used unchanged.

[Dolphin2MintPy](https://github.com/bcankara/Dolphin2MintPy) solves the same problem with `.rsc` sidecar files.
It was used as a reference, but it is not a dependency. It hardcodes satellite height, earth radius and
azimuth pixel size, and it sets `PROCESSOR=hyp3` temporarily. This pipeline takes those values from the ISCE2 metadata.

**Two inversion modes** (top-level option `inversion`, written after the `isce` section):

| Mode | Time series from | What MintPy does |
| --- | --- | --- |
| `mintpy` (default) | MintPy network inversion of dolphin's interferograms | network modification, reference point, optional unwrapping error correction, inversion, all corrections |
| `dolphin` | dolphin's own inversion (`dolphin/timeseries/`) | reference point, then all corrections |

In `mintpy` mode the pixel mask (`maskTempCoh.h5`) is additionally restricted to pixels with dolphin temporal
coherence ≥ `dolphin_temporal_coherence_threshold`. This is needed because phase-linked interferograms have
almost no closure phase, so MintPy's own temporal coherence is close to 1 nearly everywhere and hardly filters
anything. In `dolphin` mode, dolphin's temporal coherence becomes MintPy's `temporalCoherence.h5`.

The option also sets dolphin's `timeseries_options.run_inversion`/`run_velocity`. In `dolphin` mode they are always
`true`. In `mintpy` mode they are `false` by default, so `dolphin/timeseries/` does not exist. Rerun dolphin when
you switch modes (`--steps dolphin mintpy`); ISCE2 and crop do not need to run again. In `dolphin` mode, unwrapping
errors are not corrected, because `correct_unwrap_error` only changes the interferograms before MintPy's inversion.
To compare both inversions, stay in `mintpy` mode and let dolphin write its time series as well:

```yaml
inversion: mintpy        # MintPy's inversion is used; dolphin's is in dolphin/timeseries/
dolphin:
  timeseries_options: {run_inversion: true, run_velocity: true}
```

**MintPy options in the config.** The `mintpy` section works like a custom MintPy template. Besides the
pipeline option `dolphin_temporal_coherence_threshold`, every key that starts with `mintpy.` is an
original `smallbaselineApp.cfg` option. These options are written unchanged, in file order, to
`mintpy/isce_dolphin_mintpy.cfg`. MintPy then merges them into its full `mintpy/smallbaselineApp.cfg`, and every
option you leave out keeps MintPy's default (`auto`):

```yaml
mintpy:
  dolphin_temporal_coherence_threshold: 0.6
  mintpy.compute.cluster: local
  mintpy.reference.lalo: 48.19767391928604, 16.325432508158606
  mintpy.unwrapError.method: bridging+phase_closure
  mintpy.networkInversion.maskDataset: connectComponent
  mintpy.deramp: quadratic
  mintpy.reference.date: "20220101"
```

When the config is loaded, the pipeline checks a few things before ISCE2 starts:

- Every option of MintPy's `smallbaselineApp.cfg` can be set (`smallbaselineApp.py -H` lists them).
  MintPy silently ignores unknown options, so option names are checked against MintPy's own default templates.
  A typo stops the run with an error.
- `mintpy.load.*`, `mintpy.subset.*` and `mintpy.multilook.*` are passed on, but they log a warning. They only
  act in `load_data`, which never runs here. Set the area with `aoi.bbox` and the pixel size with the dolphin
  strides instead. The pipeline sets `mintpy.load.processor = isce` by default.
- Values reach the template as written. The config loader reads numbers by YAML 1.2 rules, so
  `20080520_20090817` (`excludeDate12`) and `1:5` (`excludeIfgIndex`) stay text. Standard YAML 1.1 would
  turn them into the numbers 2008052020090817 and 65.
- Unquoted `yes`/`no` are read as booleans and turned back into `yes`/`no`. Lists become `a, b`.
  Quoting (`"no"`) is still clearer.
- The old option names (`troposphere`, `deramp`, `extra_template_options`, ...) give an error that names the
  MintPy option to use instead.

An explicit `mintpy.reference.date` (YYYYMMDD) is checked twice. Before the `isce` step, it is checked against the
SLC dates in `paths.slc_dir`. At the start of the `mintpy` step, it is checked against the dates in
`inputs/ifgramStack.h5`. Without these checks, MintPy would only fail in its `reference_date` step, after ISCE2,
dolphin, the inversion and all corrections.

**Steps.** MintPy steps run one by one with `smallbaselineApp.py --dostep` (never `load_data`):

| Mode | Steps |
| --- | --- |
| `mintpy` | `modify_network`, `reference_point`, `quick_overview`, `correct_unwrap_error`, `invert_network`, then `correct_LOD` ... `hdfeos5` |
| `dolphin` | `modify_network`, `reference_point`, `quick_overview`, then `correct_LOD` ... `hdfeos5` |

`quick_overview` writes `avgPhaseVelocity.h5` (interferogram stacking) and `numTriNonzeroIntAmbiguity.h5`, the
number of triplets per pixel whose unwrapped closure phase is a non-zero multiple of 2π. With phase linking the
wrapped closure phase is zero, so any non-zero count comes from an unwrapping error. When the count is 0
everywhere, MintPy 1.6 crashes while plotting the histogram ("`bins` must be positive"). The pipeline logs a
warning in that case and continues. Any other failure of that step stops the run.

**Plots.** Unless `mintpy.plot: "no"`, every step runs with `--plot`. After each step MintPy draws every result
file that exists so far into `mintpy/pic/`, for example the network, unwrapped interferograms before and after
unwrapping error correction, `avgPhaseVelocity`, the masks, the time series, velocity and geocoded maps. If a
later step fails, the figures of the earlier steps are already saved. Because the figures are drawn again after
every step, this adds some run time on large stacks. Switch it off with `mintpy.plot: "no"` and plot once at the
end with `smallbaselineApp.py isce_dolphin_mintpy.cfg --dir . --plot` inside `mintpy/`.

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

## Next step: point file

[mintpy_postprocessing](../mintpy_postprocessing) turns a run directory (`paths.work_dir`) into one
GeoParquet point file with EGMS-style parameters, the counterpart of dolphin_postprocessing:

```bash
python run_postprocessing.py --run-dir runs/test_asc073 --area-name vienna
```

It reads the software versions from `logs/versions.json`, which every pipeline run writes.
In the Docker image, `PIPELINE_GIT_COMMIT` holds the commit the image was built from.
The Argo workflow for the cloud is in the aducat_argo repository under `pipelines/isce2_dolphin_mintpy`.

## Known limitations

- ISCE2 always processes complete bursts. The AOI crop only affects dolphin and MintPy.
- An open ISCE2 pull request ([#28139](https://github.com/isce-framework/isce2/pull/28139)) reports phase jumps
  at burst boundaries after ESD in some stacks. It is not merged or reviewed. Check burst boundaries in the results.
- With very few dates, the DEM error correction and temporal coherence thresholds are not meaningful.
  The test config switches the DEM error correction off.
- `aoi.file` uses the bounding box of the vector file, not its exact outline.
