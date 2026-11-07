# Build with --build-context runtime_wheels=/path/to/verified/runtime/wheels.
FROM alpine@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a AS transfer
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY benchmarks/harbor/requirements.lock /tmp/requirements.lock
RUN --mount=from=runtime_wheels,target=/wheels,readonly \
    python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-index --find-links=/wheels --no-deps --require-hashes -r /tmp/requirements.lock
COPY --from=transfer /bin/busybox /evalhive-transfer/busybox
COPY --from=transfer /lib/ld-musl-x86_64.so.1 /evalhive-transfer/ld-musl-x86_64.so.1
COPY benchmark-sdk/python/src /opt/benchmark-sdk
COPY benchmarks/harbor/src /opt/native-package
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/opt/benchmark-sdk:/opt/native-package PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /tmp
USER 65532:65532
ENTRYPOINT []
CMD ["python", "-m", "evalhive_harbor.worker"]
