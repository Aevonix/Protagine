# Build context contains hermes/ and protagine/ source trees, never agent homes.
# Compare using the resulting immutable image digest, not this build-time tag.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /opt/benchmark-requirements.lock
RUN pip install --no-cache-dir -r /opt/benchmark-requirements.lock
COPY hermes/ /opt/hermes/
COPY protagine/ /opt/protagine/
RUN pip install --no-deps --no-build-isolation --no-cache-dir -e /opt/hermes -e /opt/protagine -e /opt/protagine/sidecar \
    && pip freeze --all > /opt/benchmark-installed.txt
RUN python /opt/protagine/benchmarks/paired/runtime_prune.py
WORKDIR /state
USER 65534:65534
ENTRYPOINT ["python", "-I", "-B", "-m", "protagine.qualification.paired_worker"]
