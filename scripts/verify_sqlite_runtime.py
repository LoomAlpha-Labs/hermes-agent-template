#!/usr/bin/env python3
"""Fail closed unless stdlib sqlite3 uses the pinned Hermes SQLite runtime."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def loaded_sqlite_libraries() -> list[str]:
    """Return the real SQLite shared objects loaded into this process."""
    maps = Path("/proc/self/maps").read_text(encoding="utf-8")
    return sorted(
        {
            line.split()[-1]
            for line in maps.splitlines()
            if line.split() and "libsqlite3.so" in line.split()[-1]
        }
    )


def probe(expected_version: str, expected_source_id: str, expected_prefix: Path) -> dict:
    result = {
        "ok": False,
        "python": sys.executable,
        "sqlite": sqlite3.sqlite_version,
        "source_id": None,
        "loaded_libraries": loaded_sqlite_libraries(),
        "fts5": None,
        "flush": None,
        "integrity-check": None,
        "errors": [],
    }

    if sqlite3.sqlite_version != expected_version:
        result["errors"].append(
            f"expected SQLite {expected_version}, got {sqlite3.sqlite_version}"
        )

    prefix = str(expected_prefix.resolve()) + "/"
    if len(result["loaded_libraries"]) != 1 or not result["loaded_libraries"][0].startswith(prefix):
        result["errors"].append(
            f"stdlib sqlite3 did not load exactly one library below {expected_prefix}"
        )

    connection = sqlite3.connect(":memory:")
    try:
        result["source_id"] = connection.execute("SELECT sqlite_source_id()").fetchone()[0]
        if result["source_id"] != expected_source_id:
            result["errors"].append("SQLite source ID does not match the pinned release")

        options = {row[0] for row in connection.execute("PRAGMA compile_options")}
        result["fts5"] = "ENABLE_FTS5" in options
        if not result["fts5"]:
            result["errors"].append("SQLite was built without ENABLE_FTS5")

        connection.execute("CREATE VIRTUAL TABLE f USING fts5(content)")
        connection.execute("INSERT INTO f(content) VALUES('safe synthetic text')")
        for command in ("flush", "integrity-check"):
            try:
                connection.execute("INSERT INTO f(f) VALUES(?)", (command,))
                result[command] = "ok"
            except sqlite3.Error as error:
                result[command] = str(error)
                result["errors"].append(f"FTS5 {command} failed: {error}")
    except sqlite3.Error as error:
        result["errors"].append(f"SQLite capability probe failed: {error}")
    finally:
        connection.close()

    result["ok"] = not result["errors"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-source-id", required=True)
    parser.add_argument("--expected-library-prefix", required=True, type=Path)
    args = parser.parse_args()
    result = probe(args.expected_version, args.expected_source_id, args.expected_library_prefix)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())