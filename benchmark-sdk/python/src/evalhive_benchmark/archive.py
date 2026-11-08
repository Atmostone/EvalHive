"""Bounded native result archives, independent of databases and executable code."""

import argparse
import json
from copy import deepcopy
from pathlib import Path

from .aggregation import public_record, recompute, verify_record
from .peer import identity
from .protocol import ProtocolError, _json_value

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
KIND = "evalhive.native-benchmark-archive"


def canonical(value):
    _json_value(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def create_archive(sections, *, include_native_inputs=False):
    sections = deepcopy(sections)
    records = sections.get("native_aggregates", [])
    for record in records:
        verify_record(record)
    if not include_native_inputs:
        sections["native_aggregates"] = [public_record(record) for record in records]
    header = {
        "kind": KIND,
        "format_version": "1.0",
        "policy": {
            "native_aggregate_inputs": "included" if include_native_inputs else "omitted",
            "credentials": "excluded",
            "artifact_blobs": "retained_outside_archive",
            "replay": "recorded_results_and_native_aggregation_only",
        },
        "section_digests": {key: identity(value) for key, value in sections.items()},
    }
    document = {**header, "manifest_digest": identity(header), "sections": sections}
    if len(canonical(document)) > MAX_ARCHIVE_BYTES:
        raise ProtocolError(
            "ARCHIVE_TOO_LARGE", "Select a smaller native archive population", "unsupported"
        )
    verify_archive(document)
    return document


def verify_archive(document):
    try:
        _json_value(document)
        if set(document) != {
            "kind",
            "format_version",
            "policy",
            "section_digests",
            "manifest_digest",
            "sections",
        }:
            raise ValueError
        if document["kind"] != KIND or document["format_version"] != "1.0":
            raise ValueError
        header = {
            key: document[key] for key in ("kind", "format_version", "policy", "section_digests")
        }
        if identity(header) != document["manifest_digest"]:
            raise ValueError
        sections, digests = document["sections"], document["section_digests"]
        if not isinstance(sections, dict) or set(sections) != set(digests):
            raise ValueError
        for key, value in sections.items():
            if identity(value) != digests[key]:
                raise ValueError
        included = document["policy"]["native_aggregate_inputs"] == "included"
        if document["policy"]["native_aggregate_inputs"] not in {"included", "omitted"}:
            raise ValueError
        if included:
            for record in sections.get("native_aggregates", []):
                verify_record(record)
        nested_count = 0
        for trial in sections.get("trials", []):
            nested_count += verify_archive(trial)["native_aggregate_count"]
            if (
                trial["policy"]["native_aggregate_inputs"]
                != document["policy"]["native_aggregate_inputs"]
            ):
                raise ValueError
        return {
            "valid": True,
            "native_recompute_inputs": included,
            "native_aggregate_count": len(sections.get("native_aggregates", [])) + nested_count,
            "manifest_digest": document["manifest_digest"],
        }
    except (KeyError, ValueError, TypeError):
        raise ProtocolError(
            "ARCHIVE_INVALID", "Native archive integrity check failed", "recording"
        ) from None


def recompute_archive(document, adapters):
    """Adapters are selected by a trusted caller and keyed by frozen identities."""
    verified = verify_archive(document)
    if not verified["native_recompute_inputs"]:
        raise ProtocolError(
            "AGGREGATE_UNAVAILABLE", "This archive omits native checker outputs", "unsupported"
        )
    output = []
    for record in document["sections"].get("native_aggregates", []):
        if record["status"] != "completed":
            output.append(
                {
                    "status": "unavailable",
                    "evaluation_id": record["evaluation_id"],
                    "reason": record["reason"],
                }
            )
            continue
        benchmark, dataset = record["conditions"]["benchmark"], record["conditions"]["dataset"]
        key = (
            benchmark["id"],
            benchmark["version"],
            benchmark["worker_image"],
            dataset["source_digest"],
        )
        adapter = adapters.get(key)
        if adapter is None:
            raise ProtocolError(
                "AGGREGATOR_UNAVAILABLE",
                "Install the recorded native aggregator before recomputing",
                "unsupported",
            )
        result = recompute(record, adapter)
        output.append(
            {
                "status": "recomputed",
                "evaluation_id": record["evaluation_id"],
                "result_digest": identity(result),
            }
        )
    for trial in document["sections"].get("trials", []):
        output.extend(recompute_archive(trial, adapters))
    return output


def load_archive(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    with Path(path).open("rb") as stream:
        data = stream.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ProtocolError(
            "ARCHIVE_TOO_LARGE", "Native archive exceeds its read limit", "unsupported"
        )
    try:
        document = json.loads(data, object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolError(
            "ARCHIVE_INVALID", "Native archive is not canonical JSON", "recording"
        ) from None
    verify_archive(document)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_archive(load_archive(args.archive)), sort_keys=True))


if __name__ == "__main__":
    main()
