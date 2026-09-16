from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from .config import Settings
from .state import TERMINAL_STATUSES, read_manifest, utc_now, write_json_atomic


CATALOG_COLUMNS = [
    "periodo",
    "codigo_comuna",
    "comuna",
    "region",
    "status",
    "roles_total",
    "roles_ok",
    "roles_no_encontrados",
    "roles_error",
    "poligonos",
    "filas_salida",
    "fecha_captura",
    "fecha_respaldo",
    "geoparquet_sha256",
    "geoparquet_relativo",
]


def detect_onedrive_root() -> Path | None:
    for variable in ("OneDriveCommercial", "OneDriveConsumer", "OneDrive"):
        value = os.environ.get(variable)
        if value and Path(value).is_dir():
            return Path(value) / "Catastro_SII"
    return None


def local_config_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.stem}.local.json")


def save_storage_configuration(config_path: Path, storage_root: Path, archive_wms_tiles: bool = True) -> Path:
    destination = local_config_path(config_path)
    current: dict[str, Any] = {}
    if destination.exists():
        current = json.loads(destination.read_text(encoding="utf-8"))
    current.update(
        {
            "storage_root": str(storage_root.resolve()),
            "archive_wms_tiles": archive_wms_tiles,
        }
    )
    write_json_atomic(destination, current)
    storage_root.mkdir(parents=True, exist_ok=True)
    return destination


def save_reference_configuration(config_path: Path, reference_csv: Path) -> Path:
    destination = local_config_path(config_path)
    current: dict[str, Any] = {}
    if destination.exists():
        current = json.loads(destination.read_text(encoding="utf-8"))
    current["reference_csv"] = str(reference_csv.resolve())
    write_json_atomic(destination, current)
    return destination


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    shutil.copy2(source, temporary)
    temporary.replace(destination)


def _gzip_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    with source.open("rb") as input_file, gzip.open(temporary, "wb", compresslevel=6) as output_file:
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
    temporary.replace(destination)


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    if temporary.exists():
        temporary.unlink()
    source_db = sqlite3.connect(source)
    destination_db = sqlite3.connect(temporary)
    try:
        source_db.backup(destination_db)
    finally:
        destination_db.close()
        source_db.close()
    temporary.replace(destination)


def _zip_tiles(tiles: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for tile in sorted(tiles.glob("*.png")):
            archive.write(tile, arcname=tile.name)
    temporary.replace(destination)


def _api_counts(database: Path, commune_code: str) -> dict[str, int]:
    counts = {"ok": 0, "not_found": 0, "error": 0}
    with sqlite3.connect(database) as connection:
        for status, count in connection.execute(
            "SELECT status, COUNT(*) FROM api_results WHERE source='role' AND commune=? GROUP BY status",
            (commune_code,),
        ):
            counts[str(status)] = int(count)
    return counts


def _pending_catalog_rows(settings: Settings, period: str) -> list[dict[str, str]]:
    reference = settings.reference_root / "comunas_sii.json"
    if not reference.exists():
        return []
    communes = json.loads(reference.read_text(encoding="utf-8"))
    if not any(str(commune.get("id")) == "8108" for commune in communes):
        communes.append({"id": "8108", "nombre": "Trehuaco", "region": "Ñuble"})
    return [
        {
            **{column: "" for column in CATALOG_COLUMNS},
            "periodo": period,
            "codigo_comuna": str(commune["id"]),
            "comuna": str(commune["nombre"]),
            "region": str(commune["region"]),
            "status": "pendiente",
        }
        for commune in communes
    ]


def initialize_catalog(settings: Settings, period: str) -> Path | None:
    if settings.storage_root is None:
        return None
    catalog = settings.storage_root / "catalogo" / "estado_geometrias.csv"
    existing: list[dict[str, str]] = []
    if catalog.exists():
        with catalog.open("r", encoding="utf-8-sig", newline="") as source:
            existing = list(csv.DictReader(source))
    indexed = {(row.get("periodo"), row.get("codigo_comuna")): row for row in existing}
    for row in _pending_catalog_rows(settings, period):
        indexed.setdefault((row["periodo"], row["codigo_comuna"]), row)
    if not indexed:
        return None
    rows = list(indexed.values())
    rows.sort(key=lambda row: (row.get("periodo", ""), row.get("codigo_comuna", "")))
    catalog.parent.mkdir(parents=True, exist_ok=True)
    temporary = catalog.with_suffix(".csv.part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CATALOG_COLUMNS)
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in CATALOG_COLUMNS} for row in rows)
    temporary.replace(catalog)
    return catalog


def _update_catalog(settings: Settings, record: dict[str, Any]) -> Path:
    if settings.storage_root is None:
        raise RuntimeError("No hay almacenamiento configurado")
    catalog = settings.storage_root / "catalogo" / "estado_geometrias.csv"
    initialize_catalog(settings, str(record["periodo"]))
    rows: list[dict[str, str]] = []
    if catalog.exists():
        with catalog.open("r", encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
    key = (str(record["periodo"]), str(record["codigo_comuna"]))
    rows = [row for row in rows if (row.get("periodo"), row.get("codigo_comuna")) != key]
    rows.append({column: str(record.get(column, "")) for column in CATALOG_COLUMNS})
    rows.sort(key=lambda row: (row["periodo"], row["codigo_comuna"]))
    catalog.parent.mkdir(parents=True, exist_ok=True)
    temporary = catalog.with_suffix(".csv.part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CATALOG_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(catalog)
    return catalog


def publish_commune(settings: Settings, manifest_path: Path) -> dict[str, Any]:
    if settings.storage_root is None:
        raise RuntimeError("No hay almacenamiento configurado. Ejecute configure-storage.")
    manifest = read_manifest(manifest_path)
    if manifest.get("status") not in TERMINAL_STATUSES:
        raise RuntimeError(f"La comuna todavía no está terminada: {manifest.get('status', 'sin estado')}")

    commune = manifest.get("commune", {})
    code = str(commune.get("sii_code"))
    slug = manifest_path.parent.name.split("_", 1)[1]
    stem = f"{code}_{slug}"
    period = str(manifest["periodo_geometria"])
    raw = manifest_path.parent
    checkpoint = raw / "checkpoints" / "state.sqlite"
    raw_api = raw / "respuestas_api.jsonl"
    output = Path(str(manifest["output"]))
    metrics = Path(str(manifest["metrics"]))
    required = [checkpoint, raw_api, output, metrics]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("Faltan artefactos para respaldar: " + ", ".join(missing))

    period_root = settings.storage_root / period
    remote_output = period_root / "geoparquet" / f"{stem}.parquet"
    remote_api = period_root / "respuestas_api" / f"{stem}.jsonl.gz"
    remote_metrics = period_root / "metadatos" / f"{stem}_metricas.json"
    remote_manifest = period_root / "metadatos" / f"{stem}_manifest.json"
    remote_checkpoint = period_root / "checkpoints" / f"{stem}.sqlite"
    remote_tiles = (
        period_root / "wms_archivados" / f"{stem}_tiles.zip"
        if settings.archive_wms_tiles
        else None
    )
    catalog = settings.storage_root / "catalogo" / "estado_geometrias.csv"
    initialize_catalog(settings, period)
    expected_remote = [remote_output, remote_api, remote_metrics, remote_manifest, remote_checkpoint, catalog]
    if remote_tiles:
        expected_remote.append(remote_tiles)
    previous_backup = manifest.get("storage_backup", {})
    if (
        previous_backup.get("status") == "completo"
        and previous_backup.get("root") == str(settings.storage_root)
        and all(path.exists() for path in expected_remote)
        and previous_backup.get("geoparquet_sha256") == _sha256(output)
        and previous_backup.get("geoparquet_sha256") == _sha256(remote_output)
    ):
        return {"root": settings.storage_root, "geoparquet": remote_output, "catalog": catalog}

    _copy_atomic(output, remote_output)
    _gzip_atomic(raw_api, remote_api)
    _copy_atomic(metrics, remote_metrics)
    _backup_sqlite(checkpoint, remote_checkpoint)
    if remote_tiles:
        _zip_tiles(raw / "tiles", remote_tiles)

    output_hash = _sha256(remote_output)
    api_counts = _api_counts(checkpoint, code)
    metrics_data = json.loads(metrics.read_text(encoding="utf-8"))
    backed_up_at = utc_now()
    manifest["storage_backup"] = {
        "status": "completo",
        "root": str(settings.storage_root),
        "backed_up_at": backed_up_at,
        "geoparquet_sha256": output_hash,
        "tiles_archived": bool(remote_tiles),
    }
    write_json_atomic(manifest_path, manifest)
    _copy_atomic(manifest_path, remote_manifest)
    relative_output = remote_output.relative_to(settings.storage_root).as_posix()
    catalog = _update_catalog(
        settings,
        {
            "periodo": period,
            "codigo_comuna": code,
            "comuna": commune.get("name", slug),
            "region": commune.get("region", ""),
            "status": manifest["status"],
            "roles_total": manifest.get("historical_role_candidates", ""),
            "roles_ok": api_counts.get("ok", 0),
            "roles_no_encontrados": api_counts.get("not_found", 0),
            "roles_error": api_counts.get("error", 0),
            "poligonos": manifest.get("vectorization", {}).get("polygons", ""),
            "filas_salida": metrics_data.get("output_rows", ""),
            "fecha_captura": metrics_data.get("captured_at", ""),
            "fecha_respaldo": backed_up_at,
            "geoparquet_sha256": output_hash,
            "geoparquet_relativo": relative_output,
        },
    )
    return {"root": settings.storage_root, "geoparquet": remote_output, "catalog": catalog}
