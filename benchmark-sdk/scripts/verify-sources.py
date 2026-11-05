"""Verify the source lock; --remote additionally compares the public native files."""

import argparse
import json
import urllib.request
from pathlib import Path

from evalhive_benchmark.sources import load_source_lock, verify_source_bytes

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--lock", type=Path, default=Path(__file__).parents[1] / "sources.lock.json")
parser.add_argument("--remote", action="store_true")
parser.add_argument("--packages", action="store_true")
args = parser.parse_args()
lock = load_source_lock(args.lock)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "EvalHive-source-verifier/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        # Published distributions are bounded independently of protocol records.
        data = response.read(128 * 1024 * 1024 + 1)
    if len(data) > 128 * 1024 * 1024:
        raise ValueError("Public source artifact exceeds the verification limit")
    return data


if args.packages and not args.remote:
    parser.error("--packages requires --remote")
print(
    json.dumps(
        verify_source_bytes(lock, fetch, include_packages=args.packages)
        if args.remote
        else {"sources": len(lock["sources"]), "datasets": len(lock["datasets"]), "lock": "valid"}
    )
)
