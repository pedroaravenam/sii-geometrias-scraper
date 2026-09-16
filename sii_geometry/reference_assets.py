from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import pyarrow.parquet as pq
import requests

from .catalog import normalize_name
from .config import Settings


DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "config" / "catastro_2026S1_assets.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_asset_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_reference_asset(
    path: Path,
    asset: dict[str, Any],
    expected_columns: list[str],
    commune_codes: Iterable[str] = (),
) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size != int(asset["bytes"]):
        raise ValueError(f"Tamaño incorrecto en {path.name}")
    if file_sha256(path) != asset["sha256"]:
        raise ValueError(f"Checksum SHA-256 incorrecto en {path.name}")
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows != int(asset["rows"]):
        raise ValueError(f"Cantidad de filas incorrecta en {path.name}")
    if parquet.schema.names != expected_columns:
        raise ValueError(f"Esquema histórico incorrecto en {path.name}")
    for code in commune_codes:
        rows = pq.read_table(path, columns=["comuna"], filters=[("comuna", "=", int(code))]).num_rows
        if rows == 0:
            raise ValueError(f"El insumo {path.name} no contiene la comuna {code}")


def _asset_urls(asset: dict[str, Any]) -> list[str]:
    configured = asset.get("urls")
    if configured is None:
        configured = [asset["url"]]
    elif isinstance(configured, str):
        configured = [configured]
    urls = list(dict.fromkeys(str(url).strip() for url in configured if str(url).strip()))
    if not urls:
        raise ValueError(f"El insumo {asset['file']} no tiene fuentes de descarga")
    return urls


def _source_name(url: str) -> str:
    return urlparse(url).hostname or "fuente configurada"


def _download_asset(session: requests.Session, asset: dict[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    required = int(asset["bytes"])
    if shutil.disk_usage(destination.parent).free < required * 2:
        raise OSError(f"Espacio insuficiente para descargar {asset['file']}")
    temporary = destination.with_suffix(destination.suffix + ".part")
    if temporary.exists():
        temporary.unlink()
    errors: list[str] = []
    last_error: Exception | None = None
    urls = _asset_urls(asset)
    for index, url in enumerate(urls, start=1):
        source = _source_name(url)
        print(
            f"Descargando insumo histórico {asset['region']} "
            f"({required / 1_000_000:.1f} MB) desde {source}...",
            flush=True,
        )
        downloaded = 0
        next_report = 10 * 1024 * 1024
        try:
            with session.get(url, stream=True, timeout=(15, 180)) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        output.write(chunk)
                        downloaded += len(chunk)
                        if downloaded >= next_report or downloaded == required:
                            print(
                                f"    Insumo: {downloaded / 1_000_000:.1f}/"
                                f"{required / 1_000_000:.1f} MB",
                                flush=True,
                            )
                            next_report += 10 * 1024 * 1024
            if downloaded != required:
                raise ValueError(f"se recibieron {downloaded} bytes; se esperaban {required}")
            if file_sha256(temporary) != asset["sha256"]:
                raise ValueError("el checksum SHA-256 no coincide")
            temporary.replace(destination)
            return
        except (OSError, ValueError, requests.RequestException) as error:
            last_error = error
            errors.append(f"{source}: {error}")
            if temporary.exists():
                temporary.unlink()
            if index < len(urls):
                print(f"[AVISO] Falló {source}; probando fuente alternativa...", flush=True)
    details = "; ".join(errors)
    raise RuntimeError(f"No fue posible descargar {asset['file']}. {details}") from last_error


def ensure_regional_references(
    settings: Settings,
    communes: Iterable[Any],
    session: requests.Session | None = None,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, Path]:
    if settings.storage_root is None:
        raise RuntimeError("Primero configure la carpeta de resultados")
    manifest = load_asset_manifest(manifest_path)
    expected_columns = list(manifest["column_names"])
    assets = {normalize_name(item["region"]): item for item in manifest["assets"]}
    codes_by_region: dict[str, list[str]] = {}
    for commune in communes:
        codes_by_region.setdefault(normalize_name(commune.region), []).append(str(commune.sii_code))
    client = session or requests.Session()
    result: dict[str, Path] = {}
    for region_key, codes in codes_by_region.items():
        asset = assets.get(region_key)
        if asset is None:
            raise KeyError(f"No existe insumo histórico para la región {region_key}")
        destination = settings.storage_root / "insumos" / manifest["period"] / asset["file"]
        try:
            validate_reference_asset(destination, asset, expected_columns, codes)
            print(f"[OK] Insumo histórico verificado: {destination}", flush=True)
        except (FileNotFoundError, ValueError):
            _download_asset(client, asset, destination)
            try:
                validate_reference_asset(destination, asset, expected_columns, codes)
            except Exception:
                if destination.exists():
                    destination.unlink()
                raise
            print(f"[OK] Descarga verificada: {destination}", flush=True)
        result[region_key] = destination
    return result
