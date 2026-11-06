"""Read the existing case format without importing backend or evaluator modules."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from evalhive_benchmark import ProtocolError, validate
from evalhive_benchmark.protocol import MAX_LINE_BYTES
from jsonschema import Draft202012Validator

SCHEMA = json.loads((Path(__file__).with_name("schemas") / "case-v1.json").read_text())
Draft202012Validator.check_schema(SCHEMA)
CASE_VALIDATOR = Draft202012Validator(SCHEMA)

SUFFIXES = {".yaml", ".yml", ".json"}


class CaseFormatError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise CaseFormatError("Case YAML aliases are unsupported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        values = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in values:
                raise CaseFormatError("Case mappings require unique string keys")
            values[key] = self.construct_object(value_node, deep=deep)
        return values


def _unique_json(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise CaseFormatError("Case mappings require unique string keys")
        values[key] = value
    return values


def _object(value, name):
    if not isinstance(value, dict):
        raise CaseFormatError(name + " must be an object")
    return value


def _text(value, name, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise CaseFormatError(name + " must be nonempty text")
    return value


@dataclass(frozen=True)
class NativeCase:
    source_digest: str
    _data: dict = field(repr=False)

    @property
    def id(self):
        return self._data["id"]

    @property
    def suite(self):
        return self._data["suite"]

    @property
    def title(self):
        return self._data["input"]["title"]

    @property
    def external_eval(self):
        return deepcopy(self._data.get("gold", {}).get("external_eval"))

    def native_data(self):
        """Private installation/evaluator data; never use it as an agent prompt."""
        return deepcopy(self._data)

    def agent_input(self):
        source = self._data["input"]
        return {"title": source["title"], "prompt": source.get("description") or source["title"]}

    def descriptor(self, revision, *, dataset_id=None, relative_path=None):
        value = {
            "ref": {
                "dataset_id": dataset_id or self.suite,
                "revision": revision,
                "case_id": self.id,
            },
            "title": self.title,
            "source_digest": self.source_digest,
            "requirements": {"capabilities": ["host.agent_api_v1"]},
            "native_ref": {
                "suite": self.suite,
                "case_id": self.id,
                **({"path": relative_path} if relative_path is not None else {}),
            },
            "metadata": {
                "category": self._data.get("category"),
                "has_external_eval": self.external_eval is not None,
            },
        }
        if self._data["input"].get("description"):
            value["description"] = self._data["input"]["description"]
        validate("case_descriptor", value)
        return value


def parse_case(data: bytes, *, suffix=".yaml"):
    if not isinstance(data, bytes) or len(data) > MAX_LINE_BYTES:
        raise CaseFormatError("Case file exceeds its byte limit")
    try:
        if suffix == ".json":
            value = json.loads(
                data,
                object_pairs_hook=_unique_json,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    CaseFormatError("Case JSON requires finite numbers")
                ),
            )
        elif suffix in {".yaml", ".yml"}:
            value = yaml.load(data, Loader=UniqueLoader)
        else:
            raise CaseFormatError("Unsupported case file extension")
        value = _object(value, "Case")
        if not CASE_VALIDATOR.is_valid(value):
            raise CaseFormatError("Native case fields do not match format schema v1")
        for key in ("id", "suite"):
            _text(value.get(key), key)
        source = _object(value.get("input"), "input")
        _text(source.get("title"), "input.title")
        _text(source.get("description"), "input.description", optional=True)
        if not isinstance(source.get("context", []), list):
            raise CaseFormatError("input.context must be an array")
        gold = _object(value.get("gold", {}), "gold")
        external = gold.get("external_eval")
        if external is not None:
            external = _object(external, "gold.external_eval")
            _text(external.get("preprocess_command"), "preprocess_command")
            _text(external.get("eval_command"), "eval_command")
            _text(external.get("groundtruth_path"), "groundtruth_path", optional=True)
        for key in ("repro", "meta"):
            _object(value.get(key, {}), key)
        environment = value.get("environment")
        if environment is not None:
            environment = _object(environment, "environment")
            for key in ("required_services", "mcp_servers"):
                items = environment.get(key, [])
                if not isinstance(items, list) or not all(
                    isinstance(item, str) and item for item in items
                ):
                    raise CaseFormatError("environment entries must be arrays of names")
        result = NativeCase("sha256:" + hashlib.sha256(data).hexdigest(), deepcopy(value))
        descriptor = result.descriptor("format-validation")
        validate("case_descriptor", {**descriptor, "native_ref": {"document": value}})
        return result
    except CaseFormatError:
        raise
    except (ValueError, TypeError, UnicodeError, yaml.YAMLError, ProtocolError, RecursionError):
        raise CaseFormatError("Invalid native case document") from None


def load_suite(root, suite):
    root = Path(root).resolve(strict=True)
    if (
        not isinstance(suite, str)
        or not suite
        or suite in {".", ".."}
        or "/" in suite
        or "\\" in suite
        or "\0" in suite
    ):
        raise CaseFormatError("Suite must be a single installed directory name")
    directory = root / suite
    if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != root:
        raise CaseFormatError("Suite is unavailable in the installed dataset")
    cases, seen = [], set()
    for path in sorted(directory.iterdir()):
        if path.suffix not in SUFFIXES:
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LINE_BYTES:
            raise CaseFormatError("Case must be a bounded regular installed file")
        with path.open("rb") as stream:
            case = parse_case(stream.read(MAX_LINE_BYTES + 1), suffix=path.suffix)
        if case.suite != suite or case.id in seen:
            raise CaseFormatError("Case suite or identity conflicts with its installation")
        seen.add(case.id)
        cases.append(case)
    return cases


def resolve_case(root, descriptor):
    """Resolve an immutable descriptor without trusting a locator as a host path."""
    validate("case_descriptor", descriptor)
    native = descriptor["native_ref"]
    suite = native.get("suite")
    candidates = load_suite(root, suite)
    selected = [case for case in candidates if case.id == descriptor["ref"]["case_id"]]
    if (
        len(selected) != 1
        or selected[0].source_digest != descriptor["source_digest"]
        or native.get("case_id") != selected[0].id
    ):
        raise CaseFormatError("Installed case bytes or identity differ from the frozen descriptor")
    return selected[0]
