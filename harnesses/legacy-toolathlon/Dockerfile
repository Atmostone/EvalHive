# Temporary wrapper over the existing environment; removed with the legacy agent.
FROM evalhive-agent-toolathlon:latest
COPY sdk/python /opt/evalhive-sdk
RUN pip install --no-cache-dir /opt/evalhive-sdk
COPY agent-image/agent.py agent-image/feedback_server.py /app/
COPY harnesses/legacy/worker.py harnesses/legacy-toolathlon/manifest.json /opt/connector/
WORKDIR /workspace
ENTRYPOINT []
CMD ["python", "/opt/connector/worker.py"]
