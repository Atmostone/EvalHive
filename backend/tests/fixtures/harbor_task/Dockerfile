FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
RUN mkdir -p /workspace \
    && chmod 1777 /workspace \
    && printf 'Native image input\n' > /workspace/result.txt \
    && chmod 666 /workspace/result.txt
WORKDIR /workspace
ENTRYPOINT []
CMD ["sh", "-c", "sleep infinity"]
