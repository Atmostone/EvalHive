FROM python:3.12-slim
WORKDIR /app
COPY agent-image/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY sdk/python /opt/evalhive-sdk
RUN pip install --no-cache-dir /opt/evalhive-sdk
COPY agent-image/agent.py agent-image/feedback_server.py /app/
COPY harnesses/legacy/worker.py harnesses/legacy/manifest.json /opt/connector/
WORKDIR /workspace
ENTRYPOINT []
CMD ["python", "/opt/connector/worker.py"]
