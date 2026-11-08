# The native evaluator is installed offline from administrator-verified wheels.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS native-runtime
COPY benchmarks/swebench/requirements.lock /tmp/requirements.lock
RUN --mount=from=runtime_wheels,target=/wheels,readonly \
    python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-index --find-links=/wheels --no-deps --require-hashes -r /tmp/requirements.lock \
    && /opt/venv/bin/pip check
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHON_DOTENV_DISABLED=1 HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
    HF_HOME=/tmp/huggingface XDG_CACHE_HOME=/tmp/cache
COPY benchmark-sdk/python/src /opt/benchmark-sdk
COPY benchmarks/swebench/src /opt/native-package
ENV PYTHONPATH=/opt/benchmark-sdk:/opt/native-package
RUN python -c 'from evalhive_swebench.native import verified_code; verified_code()'
WORKDIR /tmp
USER 65532:65532
ENTRYPOINT []
CMD ["python"]

FROM native-runtime AS runtime
CMD ["python", "-m", "evalhive_swebench.worker"]
