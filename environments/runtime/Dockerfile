FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS toolchain
# Freeze build dependencies; no compiler is installed on the execution host.
RUN rm -f /etc/apt/sources.list.d/* && printf '%s\n' \
    'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261001T000000Z bookworm main' \
    > /etc/apt/sources.list && apt-get update && apt-get install -y --no-install-recommends musl-tools gcc \
    && rm -rf /var/lib/apt/lists/*
FROM toolchain AS bundle-tools
# Pin the modern ELF rewriter, including executable section/segment fixes.
ADD --checksum=sha256:2abdc34fbe949c995a1baa4ec310896a085588a45a99cd0d726851bdfac2a4bb \
    https://github.com/NixOS/patchelf/releases/download/0.19.2/patchelf-0.19.2-x86_64.tar.gz /tmp/patchelf.tar.gz
RUN mkdir /tmp/patchelf && tar -xzf /tmp/patchelf.tar.gz -C /tmp/patchelf \
    && cp /tmp/patchelf/bin/patchelf /usr/bin/patchelf && /usr/bin/patchelf --version
FROM toolchain AS compiler
COPY environments/runtime/supervisor.c /build/supervisor.c
RUN musl-gcc -static -Os -Wall -Wextra -Werror -o /evalhive-supervisor /build/supervisor.c

FROM scratch AS artifact
COPY --from=compiler /evalhive-supervisor /evalhive-supervisor
