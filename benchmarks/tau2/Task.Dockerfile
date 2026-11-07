FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY environments/workspace/requirements.lock /opt/environment/requirements.lock
RUN python -m venv /opt/environment/venv \
    && /opt/environment/venv/bin/pip install --no-cache-dir --require-hashes -r /opt/environment/requirements.lock \
    && mkdir /workspace && chmod 1777 /workspace
ENV PATH=/opt/environment/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
USER 1000:1000
ENTRYPOINT []
CMD ["python", "-c", "import time; time.sleep(86400)"]
