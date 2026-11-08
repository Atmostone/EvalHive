FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY benchmarks/evalhive-cases/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements.lock
COPY benchmark-sdk/python/src /opt/benchmark-sdk
COPY benchmarks/evalhive-cases/src /opt/native-package
ENV PYTHONPATH=/opt/benchmark-sdk:/opt/native-package PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /tmp
USER 65532:65532
ENTRYPOINT []
CMD ["python", "-m", "evalhive_cases.worker"]
