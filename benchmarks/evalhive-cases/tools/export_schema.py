"""Export the frozen legacy data contract without importing backend runtime code."""

import ast
import json
import sys
import types
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[3]
CLASSES = {
    "CaseInput",
    "CaseExternalEval",
    "CaseGold",
    "CaseEnvironment",
    "CaseRepro",
    "BenchmarkCase",
}


def main():
    source = ROOT / "backend/app/quality/benchmark.py"
    selected = [
        node
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.ClassDef) and node.name in CLASSES
    ]
    if {node.name for node in selected} != CLASSES:
        raise RuntimeError("Legacy case contract classes changed")
    module = types.ModuleType("case_format_contract")
    module.__dict__.update(
        BaseModel=BaseModel, Field=Field, field_validator=field_validator, Any=Any
    )
    sys.modules[module.__name__] = module
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), module.__dict__)
    schema = module.BenchmarkCase.model_json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://evalhive.dev/cases/format/1.0/schema.json",
        }
    )
    destination = Path(__file__).parents[1] / "src/evalhive_cases/schemas/case-v1.json"
    destination.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
