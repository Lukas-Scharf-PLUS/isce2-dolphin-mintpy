# Container image that runs the ISCE2 -> dolphin -> MintPy pipeline.
#
# Build:  docker build -t isce2-dolphin-mintpy .
# Run:    see "Run with Docker" in README.md.
#
# The base image ships micromamba, a small conda installer, so the image uses the
# same environment.yml as a local setup instead of a separate requirements list.
FROM mambaorg/micromamba:2.0.5

# Activate the environment during RUN steps as well (needed for projsync below).
ARG MAMBA_DOCKERFILE_ACTIVATE=1

# Install the dependencies first. This layer is cached and only rebuilt when
# environment.yml changes, not on every code change.
COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install --yes --name base --file /tmp/environment.yml \
    && micromamba clean --all --yes

# Geoid grids for the DEM conversion to ellipsoid heights (dem_preparation.py).
# Bundling them means the pipeline needs no internet access at run time.
RUN projsync --system-directory --file us_nga_egm08_25.tif \
    && projsync --system-directory --file us_nga_egm96_15.tif

WORKDIR /app
COPY --chown=$MAMBA_USER:$MAMBA_USER isce_dolphin_mintpy ./isce_dolphin_mintpy
COPY --chown=$MAMBA_USER:$MAMBA_USER config ./config
COPY --chown=$MAMBA_USER:$MAMBA_USER tests ./tests

# The git commit the image is built from (set by the GitHub workflow). It is
# written to logs/versions.json of every run, because the image has no .git folder.
ARG GIT_COMMIT=""
ENV PIPELINE_GIT_COMMIT=$GIT_COMMIT

# Print log lines immediately instead of buffering them. MintPy writes plots;
# matplotlib needs a writable cache folder even when run as another user.
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    MPLCONFIGDIR=/tmp/matplotlib

# The base image's entrypoint activates the conda environment, then runs this.
# Arguments given to `docker run` are appended, e.g. `run /data/config.yaml`.
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "-m", "isce_dolphin_mintpy"]
