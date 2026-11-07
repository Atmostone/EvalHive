"""Public discovery contains schemas and cannot execute benchmark effects."""

import json

import pytest
from evalhive_benchmark.tool_catalog import load_catalog


def document():
    return {
        "format_version": "1.0",
        "tools": [
            {
                "name": "lookup",
                "description": "Public lookup",
                "inputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            }
        ],
    }


def test_catalog_preserves_native_schema_and_rejects_duplicate_names_and_links(tmp_path):
    value = document()
    path = tmp_path / "tools.json"
    path.write_text(json.dumps(value))
    assert load_catalog(path) == value
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="links"):
        load_catalog(link)
    value["tools"].append(value["tools"][0])
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="duplicate"):
        load_catalog(path)


@pytest.mark.parametrize("change", ["private", "schema", "format", "name"])
def test_catalog_rejects_nonpublic_fields_and_invalid_discovery(tmp_path, change):
    value = document()
    if change == "private":
        value["native_state"] = {"gold": "private"}
    elif change == "schema":
        value["tools"][0]["inputSchema"]["type"] = "unknown"
    elif change == "name":
        value["tools"][0]["name"] = "invalid/tool"
    else:
        value["format_version"] = "2.0"
    path = tmp_path / "tools.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_catalog(path)
