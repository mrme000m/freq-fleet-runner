#!/usr/bin/env python3
"""Vendored-file sync — the SAME geometry module and strategy live in
several locations, and silent drift between them would be a correctness
bug (the M3 backend copies GridStrategy.py from the runtime dir; instance
copies then fall back to the legacy grid-autonomy module).

Rules enforced:
  - grid/execution/grid_geometry.py == grid/strategies/grid_geometry.py
    (committed pair — always checked)
  - when the runtime scratch ft_user_data/strategies/ exists (dev
    machine), its GridStrategy.py / GridStrategy.json /
    grid_geometry.py must be byte-identical to the committed copies.
    Absent scratch (CI, fresh clone) skips those assertions.
"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GRID = os.path.dirname(HERE)
WS = os.path.dirname(GRID)


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _exists(path):
    return os.path.isfile(path)


COMMITTED_GEOMETRY = os.path.join(GRID, "execution", "grid_geometry.py")
STRATEGY_DIR = os.path.join(GRID, "strategies")
RUNTIME_DIR = os.path.join(WS, "ft_user_data", "strategies")


def test_committed_geometry_pair_in_sync():
    a = os.path.join(STRATEGY_DIR, "grid_geometry.py")
    assert _exists(a)
    assert _sha(a) == _sha(COMMITTED_GEOMETRY)


def test_runtime_copies_in_sync_when_present():
    # the runtime dir is gitignored scratch; on machines where it exists
    # (the dev box, the M3 fleet host) it must match the committed copies
    pairs = [
        (os.path.join(STRATEGY_DIR, "GridStrategy.py"),
         os.path.join(RUNTIME_DIR, "GridStrategy.py")),
        (os.path.join(STRATEGY_DIR, "GridStrategy.json"),
         os.path.join(RUNTIME_DIR, "GridStrategy.json")),
        (COMMITTED_GEOMETRY,
         os.path.join(RUNTIME_DIR, "grid_geometry.py")),
    ]
    checked = 0
    for committed, runtime in pairs:
        if not _exists(runtime):
            continue
        assert _sha(committed) == _sha(runtime), (
            f"drift between {committed} and {runtime} — re-vendor the "
            "copies (all locations must stay byte-identical)")
        checked += 1
    if checked == 0 and _exists(RUNTIME_DIR):
        raise AssertionError(
            f"{RUNTIME_DIR} exists but none of the vendored files do — "
            "the runtime strategy dir must not be half-populated")
