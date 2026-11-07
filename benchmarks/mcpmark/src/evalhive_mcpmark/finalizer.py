"""Compatibility entry point for shared Benchmark SDK native control."""

from evalhive_benchmark.finalizer import (  # noqa: F401
    finalize,
    main,
)

if __name__ == "__main__":
    main()
