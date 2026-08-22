FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The commit a reproduction bundle (SPA-90) names as the checkout that produced it.
# Baked in rather than discovered at runtime: this image carries no git binary and
# /app is not a work tree, so asking git at runtime could only ever answer "unknown".
ARG GIT_SHA=""
ENV SPAWNHIVE_GIT_SHA=$GIT_SHA

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
