FROM golang:1.25-bookworm AS build
ENV GOMAXPROCS=2 GOMEMLIMIT=1536MiB CGO_ENABLED=0
RUN git clone --depth 1 --branch RELEASE.2025-10-15T17-29-55Z https://github.com/minio/minio.git /src
WORKDIR /src
RUN go build -p 2 -trimpath -ldflags='-s -w' -o /minio .
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /minio /usr/local/bin/minio
ENTRYPOINT ["/usr/local/bin/minio"]
