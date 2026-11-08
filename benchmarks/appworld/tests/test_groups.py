"""Catalog partitioning preserves every public native tool exactly once."""

import pytest
from evalhive_appworld.groups import partition
from evalhive_appworld.native import NativeConfigurationError


def tools(app, count):
    return [
        {"name": app + "__tool_" + str(index), "input_schema": {"type": "object"}}
        for index in range(count)
    ]


def test_large_catalog_uses_bounded_groups_without_changing_membership():
    catalogs = {
        app: tools(app, count)
        for app, count in {
            "supervisor": 6,
            "amazon": 75,
            "phone": 30,
            "file_system": 26,
            "spotify": 92,
            "venmo": 54,
            "gmail": 48,
            "splitwise": 65,
            "simple_note": 17,
            "todoist": 56,
        }.items()
    }
    grouped = partition(catalogs)
    assert len(grouped) == 2
    assert sum(group["tool_count"] for group in grouped) == 469
    assert sorted(app for group in grouped for app in group["apps"]) == sorted(catalogs)
    assert all(group["tool_count"] <= 256 for group in grouped)
    assert partition(dict(reversed(list(catalogs.items())))) == grouped


@pytest.mark.parametrize(
    "catalogs",
    [
        {"admin": tools("admin", 1)},
        {"spotify": tools("spotify", 257)},
        {"spotify": tools("gmail", 1)},
        {"spotify": [*tools("spotify", 1), *tools("spotify", 1)]},
    ],
)
def test_invalid_native_surfaces_are_rejected(catalogs):
    with pytest.raises(NativeConfigurationError):
        partition(catalogs)
