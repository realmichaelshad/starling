# Starling container image: supervisor, gateway, services process and customer engines.
#
# Build from the repository root (the build context must be the repository root):
#   docker build -f deploy/Dockerfile -t starling:1.0.0 .
#   docker build -f deploy/Dockerfile --target test -t starling:test . && docker run --rm --network none starling:test
#
# Targets: `runtime` (the last stage, so the default; this is what gets deployed) and `test`
# (runtime plus both test suites; never deployed).
#
# Reproducible builds
#   * backend/requirements.lock pins every Python package with hashes; regenerate it with
#     scripts/lock-requirements.sh (or .ps1). The build fails if the lock is missing or incomplete.
#   * Pin the base image by digest for releases:
#       docker buildx imagetools inspect python:3.11-slim        (copy the top-level "Digest: sha256:...")
#       docker build -f deploy/Dockerfile --build-arg PYTHON_IMAGE=python:3.11-slim@sha256:<digest> .
#     Use the same tag or digest (PYTHON_IMAGE) when regenerating the lock.
#
# The gateway runs as exactly one uvicorn worker (per-account slots and the engine pool live in
# process memory); the supervisor starts it that way. Do not add worker settings here.

ARG PYTHON_IMAGE=python:3.11-slim

# --------------------------------------------------------------------------- dependencies
FROM ${PYTHON_IMAGE} AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY backend/requirements.lock /tmp/requirements.lock
# Hash-checked wheels only: nothing unpinned can be installed and nothing is compiled from source.
RUN python -m pip install --prefix=/install --require-hashes --only-binary=:all: -r /tmp/requirements.lock

# --------------------------------------------------------------------------- base (shared by runtime and test)
FROM ${PYTHON_IMAGE} AS base

ARG STARLING_VERSION=1.0.0
LABEL org.opencontainers.image.title="Starling" \
      org.opencontainers.image.description="Starling hosted service: supervisor, gateway, AI services and per-customer engines" \
      org.opencontainers.image.version="${STARLING_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend/server \
    STARLING_DATA_DIR=/data \
    STARLING_WEB_DIR=/app/web

# tini runs as PID 1: it forwards stop signals to the supervisor and reaps exited processes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# Users and groups from docs/architecture/CONTRACTS.md section 2.1. The gateway also belongs to
# starling-eng (services admin socket) and starling-db (control database); the services process to
# starling-db. Engines use numeric uid/gid 200000 + tenant_seq chosen by the supervisor.
RUN groupadd --gid 10001 starling-gw \
 && groupadd --gid 10002 starling-svc \
 && groupadd --gid 10010 starling-eng \
 && groupadd --gid 10020 starling-db \
 && useradd --uid 10001 --gid starling-gw --groups starling-eng,starling-db \
      --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin starling-gw \
 && useradd --uid 10002 --gid starling-svc --groups starling-db \
      --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin starling-svc

COPY --from=build /install /usr/local

# Application code only, owned by root and read-only for the service users. No tests, no local tooling.
WORKDIR /app
COPY backend/server/starling_server /app/backend/server/starling_server
COPY backend/server/config /app/backend/server/config
COPY backend/engine/app /app/backend/engine/app
COPY backend/engine/models /app/backend/engine/models
COPY web/templates /app/web/templates
COPY web/static /app/web/static
COPY web/templating.py /app/web/templating.py

# Precompile bytecode (the service users cannot write __pycache__ at runtime). Hash-based pyc files
# keep layers reproducible and are validated against the source on import.
RUN python -m compileall -q -j 0 --invalidation-mode checked-hash /app/backend /app/web \
 && mkdir -p /data \
 && chmod 0755 /data

# Control database, customer data and backup archives. Mount an encrypted volume here.
VOLUME ["/data"]
EXPOSE 8080
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]

# No USER instruction: the supervisor starts as root, creates /run/starling and runs the gateway,
# services and engines under their own users (CONTRACTS section 2.1).
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "starling_server.supervisor"]

# --------------------------------------------------------------------------- test
# Runtime plus both test suites, for CI and the clean-checkout test. Never deploy this target.
FROM base AS test

COPY backend/requirements.lock backend/requirements-dev.txt /tmp/requirements/
# The runtime lock (hashes removed) is a constraint, so test tools cannot change a runtime version.
RUN sed -e '/--hash=/d' -e 's/[[:space:]]*\\$//' /tmp/requirements/requirements.lock > /tmp/requirements/constraints.txt \
 && python -m pip install --no-cache-dir --disable-pip-version-check \
      -c /tmp/requirements/constraints.txt -r /tmp/requirements/requirements-dev.txt

COPY backend/server/pytest.ini /app/backend/server/pytest.ini
COPY backend/server/tests /app/backend/server/tests
COPY backend/engine/tests /app/backend/engine/tests
# One test reads the contract and checks the code still matches it. The runtime image ships no
# documentation, and should not; the test target is never deployed, so it may have this one file.
COPY docs/architecture/CONTRACTS.md /app/docs/architecture/CONTRACTS.md

# Runs both suites even when the first fails; the exit status is non-zero if either failed.
CMD ["sh", "-c", "cd /app/backend/server && python -m pytest -q -p no:cacheprovider; server=$?; cd /app/backend/engine && python -m pytest -q -p no:cacheprovider; engine=$?; echo \"server tests: $server, engine tests: $engine\"; [ $server -eq 0 ] && [ $engine -eq 0 ]"]

# --------------------------------------------------------------------------- runtime (default target)
# Last stage on purpose: `docker build` without --target, compose and Fly all produce this image.
FROM base AS runtime

# Fail closed. STARLING_ENV defaults to development when it is missing, and development switches the
# production checks off (OS isolation, real email, Stripe verification). A container started without
# the variable must refuse to start half-configured, not run wide open; a local run says
# STARLING_ENV=development in its .env (as .env.example does).
ENV STARLING_ENV=production

# Nothing installs packages at run time. The base image's pip and setuptools are removed from the
# deployed image (the `test` target keeps them): image scanners flag setuptools' advisories, and an
# intruder gets one tool fewer. `ensurepip` stays in the standard library if an operator needs pip back.
RUN python -m pip uninstall --yes setuptools wheel pip
