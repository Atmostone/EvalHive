# Build with verified runtime_wheels and native_wheel build contexts.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY benchmarks/tau2/requirements.lock /tmp/requirements.lock
RUN --mount=from=runtime_wheels,target=/wheels,readonly \
    python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-index --find-links=/wheels --no-deps --require-hashes -r /tmp/requirements.lock
RUN --mount=from=native_wheel,target=/native,readonly \
    echo '4901d05e1c5ca20573253cbc23cacae810bbb577186b17400fd27d36e3f55548  /native/tau2-1.0.1-py3-none-any.whl' | sha256sum -c - \
    && /opt/venv/bin/pip install --no-index --no-deps /native/tau2-1.0.1-py3-none-any.whl
COPY benchmark-sdk/python/src /opt/benchmark-sdk
COPY benchmarks/tau2/src /opt/native-package
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/opt/benchmark-sdk:/opt/native-package PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHON_DOTENV_DISABLED=1 TAU2_DATA_DIR=/dataset/native LITELLM_LOCAL_MODEL_COST_MAP=True
WORKDIR /tmp
USER 65532:65532
ENTRYPOINT []
CMD ["python", "-m", "evalhive_tau2.worker"]
