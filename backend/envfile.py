"""Minimal .env loader (no extra dependency).

Reads KEY=VALUE lines from the project's .env into os.environ for this backend process only.
Variables already set in the environment win. Values are never logged, written anywhere or sent to the browser.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path) -> list[str]:
    """Returns the names (never the values) of the variables that were loaded."""
    if not path.is_file():
        return []
    loaded = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key in os.environ or not value:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded
