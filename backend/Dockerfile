FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

WORKDIR /app

# Freeze the tokenizer vocabulary at build time; trace processing is offline.
ADD --checksum=sha256:223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7 https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken /opt/evalhive-tokenizers/9b5ad71b2ce5302211f9c61530b329a4922fc6a4
ENV TIKTOKEN_CACHE_DIR=/opt/evalhive-tokenizers

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sdk/python /opt/evalhive-sdk
RUN pip install --no-cache-dir /opt/evalhive-sdk
COPY benchmark-sdk/python /opt/evalhive-benchmark-sdk
RUN pip install --no-cache-dir /opt/evalhive-benchmark-sdk
COPY runner /opt/evalhive-runner
RUN pip install --no-cache-dir /opt/evalhive-runner
COPY harnesses /harnesses
COPY benchmarks /benchmarks
COPY backend .

# The commit a reproduction bundle (SPA-90) names as the checkout that produced it.
# Baked in rather than discovered at runtime: this image carries no git binary and
# /app is not a work tree, so asking git at runtime could only ever answer "unknown".
ARG GIT_SHA=""
ENV EVALHIVE_GIT_SHA=$GIT_SHA

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
