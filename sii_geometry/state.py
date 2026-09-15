from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


TERMINAL_STATUSES = {"completa", "completa_con_observaciones"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def checkpoint_database(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS api_results (
            query_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            commune TEXT NOT NULL,
            manzana TEXT,
            predio TEXT,
            polygon_id INTEGER,
            geometry_key TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            queried_at TEXT NOT NULL,
            point_lon REAL,
            point_lat REAL,
            response_json TEXT,
            error TEXT
        )
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(api_results)")}
    if "geometry_key" not in columns:
        connection.execute("ALTER TABLE api_results ADD COLUMN geometry_key TEXT")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def save_api_result(
    connection: sqlite3.Connection,
    query_key: str,
    source: str,
    commune: str,
    status: str,
    attempts: int,
    point_lon: float | None,
    point_lat: float | None,
    response: dict[str, Any] | None,
    error: str | None,
    manzana: str | None = None,
    predio: str | None = None,
    polygon_id: int | None = None,
    geometry_key: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO api_results
            (query_key, source, commune, manzana, predio, polygon_id, geometry_key, status,
             attempts, queried_at, point_lon, point_lat, response_json, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(query_key) DO UPDATE SET
            status=excluded.status,
            attempts=excluded.attempts,
            queried_at=excluded.queried_at,
            point_lon=excluded.point_lon,
            point_lat=excluded.point_lat,
            response_json=excluded.response_json,
            error=excluded.error
        """,
        (
            query_key,
            source,
            commune,
            manzana,
            predio,
            polygon_id,
            geometry_key,
            status,
            attempts,
            utc_now(),
            point_lon,
            point_lat,
            json.dumps(response, ensure_ascii=False) if response is not None else None,
            error,
        ),
    )
    connection.commit()
