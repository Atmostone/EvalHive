"""Compatibility entry point for shared Benchmark SDK native control."""

from evalhive_benchmark.world_stage import (  # noqa: F401
    CHUNK,
    MAX_BYTES,
    ROOT,
    execute,
    main,
    read,
)

if __name__ == "__main__":
    main()
