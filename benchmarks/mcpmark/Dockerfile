# Native wheel and runtime wheels are verified administrator build contexts.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY benchmarks/mcpmark/requirements.lock /tmp/requirements.lock
RUN --mount=from=runtime_wheels,target=/wheels,readonly \
    python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-index --find-links=/wheels --no-deps --require-hashes -r /tmp/requirements.lock
RUN --mount=from=native_wheel,target=/wheel,readonly \
    echo 'c68ee23f436d50a82600cdca77eb72ea80226d14810150428346ea0297cec2db  /wheel/mcpmark-0.0.1-py3-none-any.whl' | sha256sum -c - \
    && /opt/venv/bin/pip install --no-index --no-deps --target /opt/native /wheel/mcpmark-0.0.1-py3-none-any.whl
COPY benchmark-sdk/python/src /opt/benchmark-sdk
COPY benchmarks/mcpmark/src /opt/native-package
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/opt/benchmark-sdk:/opt/native-package PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHON_DOTENV_DISABLED=1 EVALHIVE_MCPMARK_NATIVE_ROOT=/opt/native
WORKDIR /tmp
USER 65532:65532
ENTRYPOINT []
CMD ["python", "-m", "evalhive_mcpmark.worker"]
