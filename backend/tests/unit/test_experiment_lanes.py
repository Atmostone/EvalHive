"""SPA-69: unit tests for the Toolathlon parallel-lane helpers (pure functions).

Covers the scheduler's lane allocation/pin primitives in
``app.quality.experiments``: which lane a newly claimed run takes, the per-lane PG
host override, and the opt-in gate. No DB and no Docker — just the pure logic that
the audit flagged as untested."""

import pathlib
import re

import pytest
from types import SimpleNamespace

from app.quality.experiments import (
    MAX_TOOLATHLON_LANES,
    _first_free_lane,
    _lanes_enabled,
    _pg_host_for_lane,
)


def test_first_free_lane_picks_smallest_unused():
    assert _first_free_lane(set(), 4) == 0
    assert _first_free_lane({0}, 4) == 1
    assert _first_free_lane({0, 1}, 4) == 2
    # a freed lane in the middle is reused before higher indices
    assert _first_free_lane({0, 2}, 4) == 1


def test_first_free_lane_none_when_all_busy():
    assert _first_free_lane({0, 1}, 2) is None
    assert _first_free_lane({0, 1, 2, 3}, 4) is None


def test_first_free_lane_zero_lanes_is_none():
    assert _first_free_lane(set(), 0) is None


def test_pg_host_for_lane():
    assert _pg_host_for_lane(0) == "toolathlon_pg_lane_0"
    assert _pg_host_for_lane(3) == "toolathlon_pg_lane_3"
    # None → fall back to the shared default host (serial / non-lane runs)
    assert _pg_host_for_lane(None) is None


def test_lanes_enabled_is_opt_in():
    # an explicit >=1 enables; None/0 stay on the legacy serial path (None)
    assert _lanes_enabled(SimpleNamespace(n_toolathlon_lanes=2)) == 2
    assert _lanes_enabled(SimpleNamespace(n_toolathlon_lanes=1)) == 1
    assert _lanes_enabled(SimpleNamespace(n_toolathlon_lanes=None)) is None
    assert _lanes_enabled(SimpleNamespace(n_toolathlon_lanes=0)) is None


def test_max_lanes_matches_provisioned_containers():
    # The create-time cap must equal the number of `toolathlon_pg_lane_<i>`
    # containers docker-compose actually provisions: asking for more lanes than
    # exist pins a run to a non-existent PG host.
    #
    # This used to assert the literal `== 4`, which is a second copy of the
    # number rather than a comparison against it — so it would have passed
    # unchanged while compose said anything at all. It now reads the compose
    # file, which is mounted read-only into the api image for exactly this.
    compose = pathlib.Path("/app/docker-compose.yml")
    if not compose.exists():  # running outside the container
        compose = pathlib.Path(__file__).resolve().parents[3] / "docker-compose.yml"
    if not compose.exists():
        pytest.skip("docker-compose.yml not reachable from here")

    provisioned = set(
        re.findall(r"container_name:\s*(toolathlon_pg_lane_\d+)", compose.read_text())
    )
    assert MAX_TOOLATHLON_LANES == len(provisioned), (
        f"cap is {MAX_TOOLATHLON_LANES} but compose provisions "
        f"{len(provisioned)} lanes: {sorted(provisioned)}"
    )
    # Lanes are indexed 0..n-1 by `_first_free_lane`; a gap would pin a run to a
    # host that is not there even when the counts happen to match.
    assert provisioned == {f"toolathlon_pg_lane_{i}" for i in range(MAX_TOOLATHLON_LANES)}
