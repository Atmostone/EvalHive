"""Compatibility entry point for shared Benchmark SDK native control."""

from evalhive_benchmark.world_control import (  # noqa: F401
    SOCKET,
    decode,
    exchange,
    main,
    serve,
)

if __name__ == "__main__":
    main()
