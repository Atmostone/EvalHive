FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS runtime
# Task dependencies are independent of the connector and its native agent loop.
COPY environments/workspace/requirements.lock /opt/environment/requirements.lock
RUN python -m venv /opt/environment/venv && /opt/environment/venv/bin/pip install --no-cache-dir --require-hashes -r /opt/environment/requirements.lock
ENV PATH="/opt/environment/venv/bin:$PATH"
# Preserve registry paths used by existing templates.
COPY environments/workspace/time_server.py environments/workspace/web_server.py /app/
WORKDIR /workspace
ENTRYPOINT []

FROM runtime AS test
COPY environments/workspace/test_mcp.py /opt/environment/test_mcp.py
CMD ["python", "/opt/environment/test_mcp.py"]
