FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .
COPY entrypoint.py .
COPY feedback_server.py .
COPY time_server.py .
COPY web_server.py .

EXPOSE 8080

WORKDIR /workspace

ENTRYPOINT ["python", "/app/entrypoint.py"]
