from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .client import SIIClient
from .state import save_api_result


def find_reference_csv(raw_cadastral_dir: Path) -> Path:
    candidates = sorted(raw_cadastral_dir.glob("catastro_2026_1*.csv"))
    if not candidates:
        raise FileNotFoundError(
            f"No se encontró catastro_2026_1*.csv en {raw_cadastral_dir}. "
            "Indica uno con --reference-csv."
        )
    return candidates[-1]


def extract_role_keys(reference_csv: Path, commune_code: str) -> pd.DataFrame:
    selected: list[pd.DataFrame] = []
    if reference_csv.suffix.lower() in {".parquet", ".pq"}:
        roles = pd.read_parquet(
            reference_csv,
            columns=["comuna", "manzana", "predio"],
            filters=[("comuna", "=", int(commune_code))],
        )
        selected.append(roles[["manzana", "predio"]])
    else:
        for chunk in pd.read_csv(
            reference_csv,
            dtype={"comuna": "string", "manzana": "string", "predio": "string"},
            usecols=["comuna", "manzana", "predio"],
            chunksize=500_000,
            low_memory=False,
        ):
            normalized_commune = chunk["comuna"].str.replace(r"\.0$", "", regex=True).str.lstrip("0")
            match = chunk.loc[normalized_commune == str(int(commune_code)), ["manzana", "predio"]]
            if not match.empty:
                selected.append(match)
    if not selected:
        return pd.DataFrame(columns=["manzana", "predio", "rol"])
    roles = pd.concat(selected, ignore_index=True).dropna().drop_duplicates()
    for column in ["manzana", "predio"]:
        roles[column] = roles[column].astype("string").str.replace(r"\.0$", "", regex=True).str.lstrip("0").replace("", "0")
    roles["rol"] = roles["manzana"] + "-" + roles["predio"]
    return roles.sort_values(["manzana", "predio"], kind="stable").reset_index(drop=True)


def fetch_current_roles(
    client: SIIClient,
    connection: sqlite3.Connection,
    commune_code: str,
    layer: str,
    role_keys: pd.DataFrame,
    limit: int | None = None,
    force: bool = False,
) -> None:
    records = role_keys.head(limit) if limit else role_keys
    existing = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT query_key, status FROM api_results WHERE source='role' AND commune=?",
            (commune_code,),
        )
    }
    total = len(records)
    for ordinal, row in enumerate(records.itertuples(index=False), start=1):
        query_key = f"role:{commune_code}:{row.manzana}:{row.predio}"
        if not force and existing.get(query_key) in {"ok", "not_found"}:
            continue
        result = client.get_predio(commune_code, row.manzana, row.predio, layer)
        exists = bool(
            result.data
            and (
                result.data.get("existePredio") in {1, "1", True}
                or result.data.get("nombreComuna") is not None
            )
        )
        status = "ok" if exists else "not_found" if result.error is None else "error"
        save_api_result(
            connection,
            query_key=query_key,
            source="role",
            commune=commune_code,
            manzana=row.manzana,
            predio=row.predio,
            status=status,
            attempts=result.attempts,
            point_lon=None,
            point_lat=None,
            response=result.data,
            error=result.error,
        )
        if ordinal % 100 == 0 or ordinal == total:
            print(f"    API roles: {ordinal:,}/{total:,}", flush=True)


def _slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^\w\s-]+", "", text, flags=re.UNICODE)
    return re.sub(r"[\s-]+", "_", text).strip("_")[:120]


def normalize_api_data(
    data: dict[str, Any] | None,
    commune: str,
    manzana: str | None,
    predio: str | None,
) -> dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    canonical = {
        "comuna": int(commune),
        "manzana": str(data.get("manzana", manzana or "")).lstrip("0") or "0",
        "predio": str(data.get("predio", predio or "")).lstrip("0") or "0",
        "existePredio": data.get("existePredio"),
        "eacs": data.get("eacs"),
        "eacano": data.get("eacano"),
        "eacsDescripcion": data.get("eacsDescripcion"),
        "direccion_sii": data.get("direccion"),
        "nombreComuna": data.get("nombreComuna"),
        "destinoDescripcion": data.get("destinoDescripcion"),
        "ubicacion": data.get("ubicacion"),
        "valorTotal": data.get("valorTotal"),
        "valorAfecto": data.get("valorAfecto"),
        "valorExento": data.get("valorExento"),
        "supTerreno": data.get("supTerreno"),
        "supConsMt2": data.get("supConsMt2"),
        "supConsMt3": data.get("supConsMt3"),
        "medidaSup": data.get("medidaSup"),
        "medidaSupConst": data.get("medidaSupConst"),
        "ah": data.get("ah"),
        "sector": data.get("sector"),
        "tablaOrigen": data.get("tablaOrigen"),
        "periodo": data.get("periodo"),
        "lat": data.get("ubicacionX"),
        "lon": data.get("ubicacionY"),
    }
    canonical["rol"] = data.get("rol") or f"{canonical['manzana']}-{canonical['predio']}"
    nested_prefixes = {
        "datosAh": "ah",
        "predioPublicado": "predioPublicado",
    }
    for source, prefix in nested_prefixes.items():
        nested = data.get(source)
        if isinstance(nested, dict):
            for key, value in nested.items():
                canonical[f"{prefix}_{key}"] = value if not isinstance(value, (dict, list)) else json.dumps(value, ensure_ascii=False)
    csa = data.get("datosCsa")
    if isinstance(csa, list) and csa and isinstance(csa[0], dict):
        for key, value in csa[0].items():
            canonical[f"csa_{key}"] = value if not isinstance(value, (dict, list)) else json.dumps(value, ensure_ascii=False)
    capas = data.get("datosCapas")
    if isinstance(capas, list):
        for capa in capas:
            if not isinstance(capa, dict):
                continue
            title = _slug(capa.get("titulo") or "sin_titulo")
            for item in capa.get("datos") or []:
                if not isinstance(item, dict) or item.get("etiqueta") is None:
                    continue
                base_key = f"cap__{title}__{_slug(item['etiqueta'])}"
                key = base_key
                suffix = 2
                while key in canonical and canonical[key] != item.get("valor"):
                    key = f"{base_key}__{suffix}"
                    suffix += 1
                canonical[key] = item.get("valor")
    known = set(canonical) | {"direccion", "ubicacionX", "ubicacionY", "datosAh", "datosCsa", "datosCapas", "predioPublicado", "rol", "manzana", "predio"}
    for key, value in data.items():
        if key not in known:
            canonical[f"api_{key}"] = value if not isinstance(value, (dict, list)) else json.dumps(value, ensure_ascii=False)
    return canonical


def load_role_results(connection: sqlite3.Connection, commune_code: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    query = """
        SELECT manzana, predio, status, attempts, queried_at, response_json, error
        FROM api_results WHERE source='role' AND commune=? ORDER BY manzana, predio
    """
    for manzana, predio, status, attempts, queried_at, response_json, error in connection.execute(query, (commune_code,)):
        data = json.loads(response_json) if response_json else None
        normalized = normalize_api_data(data, commune_code, manzana, predio)
        normalized.update({"_ok": status == "ok", "_status": status, "_api_attempts": attempts, "_api_queried_at": queried_at, "_api_error": error})
        rows.append(normalized)
    return pd.DataFrame(rows)


def export_raw_jsonl(connection: sqlite3.Connection, commune_code: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as output:
        query = """
            SELECT query_key, source, commune, manzana, predio, polygon_id, status,
                   attempts, queried_at, point_lon, point_lat, response_json, error
            FROM api_results WHERE commune=? ORDER BY source, query_key
        """
        columns = ["query_key", "source", "commune", "manzana", "predio", "polygon_id", "status", "attempts", "queried_at", "point_lon", "point_lat", "response", "error"]
        for values in connection.execute(query, (commune_code,)):
            record = dict(zip(columns, values))
            record["response"] = json.loads(record["response"]) if record["response"] else None
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
