# Original AppWorld code is installed from verified administrator-only contexts.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY benchmarks/appworld/requirements.lock /tmp/requirements.lock
RUN --mount=from=runtime_wheels,target=/wheels,readonly \
    python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-index --find-links=/wheels --no-deps --require-hashes -r /tmp/requirements.lock
RUN --mount=from=native_wheel,target=/wheel,readonly \
    echo 'de5203e32a9d0147b2bb49bcbb4740fea688f8b9b1844af17a5491e25543c31c  /wheel/appworld-0.2.0.dev0-py3-none-any.whl' | sha256sum -c - \
    && /opt/venv/bin/pip install --no-index --no-deps /wheel/appworld-0.2.0.dev0-py3-none-any.whl
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHON_DOTENV_DISABLED=1 APPWORLD_ROOT=/appworld APPWORLD_CACHE=/tmp/appworld-cache
RUN APPWORLD_CACHE=/opt/appworld-installed-cache /opt/venv/bin/python -c 'from appworld.install import install_package; install_package()'
COPY benchmark-sdk/python/src /opt/benchmark-sdk
COPY benchmarks/appworld/src /opt/native-package
COPY benchmarks/appworld/src/evalhive_appworld/task_client.py /opt/environment/appworld_mcp.py
ENV PYTHONPATH=/opt/benchmark-sdk:/opt/native-package
WORKDIR /tmp
USER 65532:65532
ENTRYPOINT []
CMD ["python", "-m", "evalhive_appworld.worker"]
