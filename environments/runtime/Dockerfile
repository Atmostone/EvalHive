FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS toolchain
# Freeze build dependencies; no compiler is installed on the execution host.
RUN rm -f /etc/apt/sources.list.d/* && printf '%s\n' \
    'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261001T000000Z bookworm main' \
    > /etc/apt/sources.list && apt-get update && apt-get install -y --no-install-recommends musl-tools gcc \
    && rm -rf /var/lib/apt/lists/*
FROM toolchain AS compiler
COPY environments/runtime/supervisor.c /build/supervisor.c
RUN musl-gcc -static -Os -Wall -Wextra -Werror -o /evalhive-supervisor /build/supervisor.c

FROM scratch AS artifact
COPY --from=compiler /evalhive-supervisor /evalhive-supervisor
