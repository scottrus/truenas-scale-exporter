# syntax=docker/dockerfile:1

# Chainguard images are used for their near-zero CVE count. The free public
# tier publishes only `latest` and `latest-dev` — there are no version tags —
# so both stages are pinned by digest instead. Dependabot updates these.
#
# Re-resolve a digest by hand with:
#   crane digest cgr.dev/chainguard/python:latest

# --- build ------------------------------------------------------------------
# The -dev variant carries pip and a shell; the runtime variant carries neither.
FROM cgr.dev/chainguard/python:latest-dev@sha256:7406826ac06aa5e5b9b010c82b3f56aed62946c7fb5c7d4dfba012b88a6570c5 AS build

# Chainguard's -dev variants still default to the nonroot user, so writing to
# / is denied. Switch to root for the build only — this stage is discarded, and
# the runtime stage below runs as 65532 regardless.
USER root

WORKDIR /src

# Build into a venv so the runtime stage copies exactly one self-contained
# directory. The runtime image has no pip to install with.
#
# The venv path must be identical in both stages: a venv bakes its own absolute
# path into the console-script shebangs, so copying it elsewhere silently
# produces entrypoints that point at a python that isn't there.
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

# Drop back to nonroot so this stage does not end as root. The stage is
# discarded either way, but leaving it root trips hadolint's DL3002 — and
# suppressing that lint here would also suppress it for the runtime stage,
# where it genuinely matters.
USER nonroot

# --- runtime ----------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest@sha256:b3d3fbb8b9fe48950bab73d49bffa7496ff6f8a46ba570b302fc366f1396011a

LABEL org.opencontainers.image.title="truenas-scale-exporter" \
      org.opencontainers.image.description="Prometheus exporter for TrueNAS over the JSON-RPC 2.0 WebSocket API" \
      org.opencontainers.image.source="https://github.com/scottrus/truenas-scale-exporter" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="cgr.dev/chainguard/python:latest"

COPY --from=build /venv /venv

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Chainguard runtime images already default to the nonroot user (65532), which
# matches runAsUser in the Helm chart. Stated explicitly so it survives a base
# image change.
USER 65532:65532

EXPOSE 9819

# Exec form keeps the exporter as PID 1, so SIGTERM reaches it directly and
# both `docker stop` and Kubernetes termination are clean.
ENTRYPOINT ["truenas-scale-exporter"]

# No shell in this image, so the check is an exec-form python one-liner.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9819/health', timeout=4).status==200 else 1)"]
