from __future__ import annotations

import json
import hashlib
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from .catalog import Commune, load_boundary
from .client import SIIClient
from .config import Settings
from .geometry import calculate_supercells, download_supercell, vectorize_supercells
from .matching import match_roles_to_polygons
from .records import (
    export_raw_jsonl,
    extract_role_keys,
    fetch_current_roles,
    find_reference_csv,
    load_role_results,
    normalize_api_data,
)
from .state import (
    TERMINAL_STATUSES,
    checkpoint_database,
    read_manifest,
    save_api_result,
    utc_now,
    write_json_atomic,
)
from .storage import publish_commune


def _paths(settings: Settings, commune: Commune) -> dict[str, Path]:
    raw = settings.raw_root / settings.periodo_geometria / f"{commune.sii_code}_{commune.slug}"
    processed = settings.processed_root / settings.periodo_geometria
    return {
        "raw": raw,
        "tiles": raw / "tiles",
        "manifest": raw / "manifest.json",
        "checkpoint": raw / "checkpoints" / "state.sqlite",
        "raw_api": raw / "respuestas_api.jsonl",
        "vectors": raw / "poligonos_vectorizados.parquet",
        "metrics": processed / f"{commune.sii_code}_{commune.slug}_metrics.json",
        "output": processed / f"{commune.sii_code}_{commune.slug}.parquet",
    }


def _manifest_base(settings: Settings, commune: Commune) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "iniciando",
        "commune": {"sii_code": commune.sii_code, "name": commune.name, "region": commune.region},
        "periodo_geometria": settings.periodo_geometria,
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "zoom": settings.zoom,
        "supercell_tiles": settings.supercell_tiles,
        "settings": {
            "request_delay_s": settings.request_delay_s,
            "max_retries": settings.max_retries,
            "boundary_buffer_m": settings.boundary_buffer_m,
            "fill_color": list(settings.fill_color),
            "fill_tolerance": list(settings.fill_tolerance),
        },
    }


def _archive_before_force(paths: dict[str, Path]) -> None:
    """Conserva artefactos reemplazables y reinicia sólo el checkpoint activo."""
    if not paths["raw"].exists():
        return
    stamp = utc_now().replace(":", "").replace("+", "_")
    backup = paths["raw"] / "backups" / stamp
    backup.mkdir(parents=True, exist_ok=True)
    for key in ["manifest", "raw_api", "vectors", "metrics", "output"]:
        source = paths[key]
        if source.exists():
            shutil.copy2(source, backup / f"{key}_{source.name}")
    database = paths["checkpoint"]
    if database.exists():
        with sqlite3.connect(database) as source, sqlite3.connect(backup / "state.sqlite") as destination:
            source.backup(destination)
    for suffix in ["", "-wal", "-shm"]:
        candidate = Path(str(database) + suffix)
        if candidate.exists():
            candidate.unlink()


def _update_manifest(path: Path, manifest: dict[str, Any], **updates: Any) -> None:
    manifest.update(updates)
    manifest["updated_at"] = utc_now()
    write_json_atomic(path, manifest)


def plan_commune(
    commune: Commune,
    settings: Settings,
    client: SIIClient,
) -> tuple[dict[str, Any], list[tuple[int, int]], Any]:
    context = client.get_context(commune.sii_code)
    layer = context.get("layer") or f"sii:BR_CART_{commune.slug.upper()}_WMS"
    boundary = load_boundary(commune, settings, client.session)
    supercells = calculate_supercells(boundary, settings)
    return {"layer": layer, "context": context}, supercells, boundary


def _expand_bounds(geometry, minimum_degrees: float = 0.002) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = geometry.bounds
    width = max(maxx - minx, minimum_degrees)
    height = max(maxy - miny, minimum_degrees)
    center_x, center_y = geometry.centroid.x, geometry.centroid.y
    return center_x - width, center_y - height, center_x + width, center_y + height


def _geometry_key(geometry) -> str:
    normalized = geometry.normalize()
    return hashlib.sha256(normalized.wkb).hexdigest()[:24]


def _expected_api_period(period: str) -> str | None:
    match = re.fullmatch(r"(\d{4})S([12])", period.upper())
    if not match:
        return None
    semester = "PRIMER" if match.group(2) == "1" else "SEGUNDO"
    return f"{semester} SEMESTRE DE {match.group(1)}"


def _fetch_orphan_polygons(
    client: SIIClient,
    connection: sqlite3.Connection,
    commune: Commune,
    layer: str,
    polygons: gpd.GeoDataFrame,
    orphan_indices: list[int],
    limit: int | None = None,
    force: bool = False,
) -> None:
    selected = orphan_indices[:limit] if limit else orphan_indices
    existing = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT query_key, status FROM api_results WHERE source='polygon' AND commune=?",
            (commune.sii_code,),
        )
    }
    for ordinal, polygon_id in enumerate(selected, start=1):
        geometry = polygons.geometry.iloc[polygon_id]
        geometry_key = _geometry_key(geometry)
        key = f"polygon:{commune.sii_code}:{geometry_key}"
        if not force and existing.get(key) in {"ok", "not_found"}:
            continue
        point = geometry.representative_point()
        result = client.get_feature_info(
            commune.sii_code,
            layer,
            point.x,
            point.y,
            _expand_bounds(geometry),
        )
        status = "ok" if result.data else "not_found" if result.error in {None, "El punto no devolvió un predio"} else "error"
        save_api_result(
            connection,
            query_key=key,
            source="polygon",
            commune=commune.sii_code,
            polygon_id=polygon_id,
            geometry_key=geometry_key,
            status=status,
            attempts=result.attempts,
            point_lon=point.x,
            point_lat=point.y,
            response=result.data,
            error=result.error,
        )
        if ordinal % 100 == 0 or ordinal == len(selected):
            print(f"    API polígonos huérfanos: {ordinal:,}/{len(selected):,}", flush=True)


def _load_orphan_rows(
    connection: sqlite3.Connection,
    commune: Commune,
    polygons: gpd.GeoDataFrame,
    orphan_indices: list[int],
) -> gpd.GeoDataFrame:
    stored = {
        row[0]: (row[1], json.loads(row[2]) if row[2] else None, row[3], row[4])
        for row in connection.execute(
            "SELECT geometry_key, status, response_json, attempts, queried_at "
            "FROM api_results WHERE source='polygon' AND commune=? AND geometry_key IS NOT NULL",
            (commune.sii_code,),
        )
        if row[0]
    }
    records: list[dict[str, Any]] = []
    geometries = []
    for polygon_id in orphan_indices:
        geometry_key = _geometry_key(polygons.geometry.iloc[polygon_id])
        status, data, attempts, queried_at = stored.get(geometry_key, ("pending", None, 0, None))
        if data:
            record = normalize_api_data(data, commune.sii_code, None, None)
            record["_match_method"] = "get_feature_info_orphan"
            record["calidad_geom"] = "rol_recuperado"
        else:
            record = {
                "comuna": int(commune.sii_code),
                "manzana": None,
                "predio": None,
                "rol": None,
                "_match_method": "unmatched_polygon",
                "calidad_geom": "poligono_sin_rol",
            }
        record.update(
            {
                "_poly_idx": polygon_id,
                "_match_dist_m": 0.0 if data else None,
                "pol_area_m2": polygons.iloc[polygon_id]["pol_area_m2"],
                "pol_size_class": polygons.iloc[polygon_id].get("pol_size_class"),
                "_ok": status == "ok",
                "_status": status,
                "_api_attempts": attempts,
                "_api_queried_at": queried_at,
            }
        )
        records.append(record)
        geometries.append(polygons.geometry.iloc[polygon_id])
    return gpd.GeoDataFrame(records, geometry=geometries, crs=4326)


def summarize_final_output(output: gpd.GeoDataFrame) -> dict[str, Any]:
    """Resume todo el resultado sin confundir filas, polígonos y roles únicos."""
    geometry_present = output.geometry.notna()
    if "rol" in output.columns:
        normalized_roles = output["rol"].astype("string").str.strip()
        role_present = normalized_roles.notna() & normalized_roles.ne("")
    else:
        normalized_roles = pd.Series(pd.NA, index=output.index, dtype="string")
        role_present = pd.Series(False, index=output.index)

    geometry_rows = output.loc[geometry_present].copy()
    geometry_rows["_has_role_for_summary"] = role_present.loc[geometry_present].to_numpy()
    if "_poly_idx" in geometry_rows.columns and geometry_rows["_poly_idx"].notna().any():
        polygon_roles = geometry_rows.loc[geometry_rows["_poly_idx"].notna()].groupby("_poly_idx")[
            "_has_role_for_summary"
        ].any()
    else:
        polygon_roles = geometry_rows["_has_role_for_summary"]
    polygon_total = int(len(polygon_roles))
    polygons_with_role = int(polygon_roles.sum())
    polygons_without_role = polygon_total - polygons_with_role
    unique_roles = set(normalized_roles.loc[role_present].tolist())
    unique_roles_with_geometry = set(normalized_roles.loc[role_present & geometry_present].tolist())
    unique_roles_without_geometry = unique_roles - unique_roles_with_geometry

    return {
        "rows_total": int(len(output)),
        "rows_with_geometry": int(geometry_present.sum()),
        "polygons_total": polygon_total,
        "polygons_with_role": polygons_with_role,
        "polygons_without_role": polygons_without_role,
        "polygon_attribution_pct": round(polygons_with_role / polygon_total * 100, 3) if polygon_total else 0.0,
        "unique_roles_total": len(unique_roles),
        "unique_roles_with_geometry": len(unique_roles_with_geometry),
        "unique_roles_without_geometry": len(unique_roles_without_geometry),
        "unique_role_geometry_coverage_pct": (
            round(len(unique_roles_with_geometry) / len(unique_roles) * 100, 3) if unique_roles else 0.0
        ),
    }


def process_commune(
    commune: Commune,
    settings: Settings,
    *,
    force: bool = False,
    reference_csv: Path | None = None,
    dry_run: bool = False,
    max_supercells: int | None = None,
    only_supercells: list[tuple[int, int]] | None = None,
    max_roles: int | None = None,
    max_orphans: int | None = None,
    rematch: bool = False,
) -> dict[str, Any]:
    paths = _paths(settings, commune)
    current = read_manifest(paths["manifest"])
    reusable_vectors = (
        not force
        and max_supercells is None
        and not only_supercells
        and paths["vectors"].exists()
        and bool(current.get("vectorization"))
        and current.get("supercells_downloaded") == current.get("supercells_planned")
    )
    if not force and not rematch and current.get("status") in TERMINAL_STATUSES and paths["output"].exists():
        if settings.storage_root:
            try:
                published = publish_commune(settings, paths["manifest"])
                print(f"[BACKUP] {commune.name}: {published['geoparquet']}")
            except Exception as error:
                print(f"[AVISO] Respaldo pendiente para {commune.name}: {error}")
        print(f"[SKIP] {commune.name} ({commune.sii_code}) ya está {current['status']}.")
        return current

    if force:
        _archive_before_force(paths)
    client = SIIClient(settings)
    manifest = _manifest_base(settings, commune) if force or not current else current
    paths["raw"].mkdir(parents=True, exist_ok=True)
    paths["tiles"].mkdir(parents=True, exist_ok=True)
    paths["output"].parent.mkdir(parents=True, exist_ok=True)
    _update_manifest(
        paths["manifest"],
        manifest,
        status="planificando",
        error=None,
        rematch_requested=rematch,
    )

    try:
        plan, all_supercells, boundary = plan_commune(commune, settings, client)
        if only_supercells:
            outside = [cell for cell in only_supercells if cell not in all_supercells]
            if outside:
                raise ValueError(f"Superceldas fuera del plan comunal: {outside}")
            selected_supercells = only_supercells
        else:
            selected_supercells = all_supercells[:max_supercells] if max_supercells else all_supercells
        _update_manifest(
            paths["manifest"],
            manifest,
            status="planificado" if dry_run else "descargando_wms",
            wms_layer=plan["layer"],
            supercells_planned=len(all_supercells),
            supercells_selected=len(selected_supercells),
            boundary_bounds=list(boundary.bounds),
        )
        if dry_run:
            print(f"[PLAN] {commune.name}: {len(all_supercells):,} superceldas z{settings.zoom}")
            return manifest

        for ordinal, (sc_x, sc_y) in enumerate(selected_supercells, start=1):
            destination = paths["tiles"] / f"sc_{sc_x}_{sc_y}.png"
            download_supercell(client, commune.sii_code, plan["layer"], sc_x, sc_y, destination, settings, force=force)
            if ordinal % 10 == 0 or ordinal == len(selected_supercells):
                _update_manifest(paths["manifest"], manifest, supercells_downloaded=ordinal)
                print(f"    WMS: {ordinal:,}/{len(selected_supercells):,}", flush=True)

        if reusable_vectors:
            polygons = gpd.read_parquet(paths["vectors"])
            vector_metrics = manifest["vectorization"]
            print(f"    Vectorizacion: reutilizando {len(polygons):,} poligonos guardados", flush=True)
        else:
            _update_manifest(paths["manifest"], manifest, status="vectorizando")
            polygons, block_metrics = vectorize_supercells(paths["tiles"], selected_supercells, settings)
            if polygons.empty:
                raise RuntimeError("La vectorización no produjo polígonos; revise la simbología WMS")
            polygons.to_parquet(paths["vectors"])
            fill_ratios = [float(item["fill_ratio"]) for item in block_metrics]
            vector_metrics = {
                "polygons": len(polygons),
                "valid": int(polygons.is_valid.sum()),
                "blocks": len(block_metrics),
                "large_components": int((polygons["pol_size_class"] == "large_component").sum()),
                "fill_ratio_min": min(fill_ratios),
                "fill_ratio_max": max(fill_ratios),
                "blocks_detail": block_metrics,
            }
        _update_manifest(paths["manifest"], manifest, status="consultando_api", vectorization=vector_metrics)

        reference = reference_csv or find_reference_csv(settings.repository_root / "data" / "raw" / "catastral")
        role_keys = extract_role_keys(reference, commune.sii_code)
        _update_manifest(paths["manifest"], manifest, reference_csv=str(reference), historical_role_candidates=len(role_keys))

        with checkpoint_database(paths["checkpoint"]) as connection:
            fetch_current_roles(
                client,
                connection,
                commune.sii_code,
                plan["layer"],
                role_keys,
                limit=max_roles,
                force=force,
            )
            current_roles = load_role_results(connection, commune.sii_code)
            matched, used, match_metrics = match_roles_to_polygons(current_roles, polygons)
            orphan_indices = sorted(set(range(len(polygons))) - used)
            _fetch_orphan_polygons(
                client,
                connection,
                commune,
                plan["layer"],
                polygons,
                orphan_indices,
                limit=max_orphans,
                force=force,
            )
            orphan_rows = _load_orphan_rows(connection, commune, polygons, orphan_indices)
            export_raw_jsonl(connection, commune.sii_code, paths["raw_api"])
            api_counts = dict(
                connection.execute(
                    "SELECT status, COUNT(*) FROM api_results WHERE commune=? GROUP BY status",
                    (commune.sii_code,),
                ).fetchall()
            )

        output = pd.concat([matched, orphan_rows], ignore_index=True, sort=False)
        output = gpd.GeoDataFrame(output, geometry="geometry", crs=4326)
        captured_at = utc_now()
        output["periodo_geometria"] = settings.periodo_geometria
        output["fecha_captura"] = captured_at
        output["wms_layer"] = plan["layer"]
        output["wms_style"] = "PREDIOS_WMS_V0"
        output["zoom"] = settings.zoom
        output["geometry_source"] = "SII_WMS_raster_vectorized"
        temporary_output = paths["output"].with_suffix(".parquet.part")
        output.to_parquet(temporary_output)
        temporary_output.replace(paths["output"])

        observed_periods = sorted(str(value) for value in output.get("periodo", pd.Series(dtype=str)).dropna().unique())
        expected_period = _expected_api_period(settings.periodo_geometria)
        unexpected_periods = [value for value in observed_periods if expected_period and value.upper() != expected_period]
        final_result = summarize_final_output(output)
        incomplete_control = any(value is not None for value in [max_supercells, max_roles, max_orphans, only_supercells])
        observations = {
            "api_error": int(api_counts.get("error", 0)),
            "api_not_found": int(api_counts.get("not_found", 0)),
            "roles_without_geometry": int(match_metrics["without_geometry"]),
            "current_roles_without_published_geometry": int(
                match_metrics["current_without_published_geometry"]
            ),
            "current_roles_with_coordinates_unmatched": int(
                match_metrics["current_with_coordinates_unmatched"]
            ),
            "roles_not_found_current_period": int(match_metrics["not_found_current_period"]),
            "unmatched_polygons": int((output["calidad_geom"] == "poligono_sin_rol").sum()),
            "invalid_geometries": int((output.geometry.notna() & ~output.geometry.is_valid).sum()),
            "large_components": int((output.get("pol_size_class") == "large_component").sum()),
            "unexpected_api_periods": len(unexpected_periods),
        }
        final_status = "parcial_control" if incomplete_control else (
            "completa" if not any(observations.values()) else "completa_con_observaciones"
        )
        metrics = {
            "commune": manifest["commune"],
            "periodo_geometria": settings.periodo_geometria,
            "captured_at": captured_at,
            "status": final_status,
            "wms": {"supercells": len(selected_supercells), "layer": plan["layer"], "zoom": settings.zoom},
            "vectorization": vector_metrics,
            "api": api_counts,
            "match": match_metrics,
            "final_result": final_result,
            "observed_api_periods": observed_periods,
            "expected_api_period": expected_period,
            "observations": observations,
            "output_rows": len(output),
            "output": str(paths["output"]),
        }
        write_json_atomic(paths["metrics"], metrics)
        _update_manifest(
            paths["manifest"],
            manifest,
            status=final_status,
            completed_at=utc_now(),
            metrics=str(paths["metrics"]),
            output=str(paths["output"]),
            final_result=final_result,
            observations=observations,
            error=None,
        )
        print(
            "[RESUMEN FINAL] "
            f"{final_result['polygons_total']:,} polígonos: "
            f"{final_result['polygons_with_role']:,} con rol y "
            f"{final_result['polygons_without_role']:,} sin rol "
            f"({final_result['polygon_attribution_pct']:.3f}% atribuidos). "
            f"Roles únicos con geometría: {final_result['unique_roles_with_geometry']:,}/"
            f"{final_result['unique_roles_total']:,} "
            f"({final_result['unique_role_geometry_coverage_pct']:.3f}%). "
            f"Vigentes sin visualización SII: {match_metrics['current_without_published_geometry']:,}; "
            f"no encontrados en el período actual: {match_metrics['not_found_current_period']:,}.",
            flush=True,
        )
        if settings.storage_root:
            try:
                published = publish_commune(settings, paths["manifest"])
                print(f"[BACKUP] {commune.name}: {published['geoparquet']}", flush=True)
            except Exception as backup_error:
                manifest["storage_backup"] = {
                    "status": "fallido",
                    "error": str(backup_error),
                    "attempted_at": utc_now(),
                }
                write_json_atomic(paths["manifest"], manifest)
                print(f"[AVISO] La comuna terminó, pero su respaldo falló: {backup_error}", flush=True)
        return manifest
    except Exception as error:
        _update_manifest(paths["manifest"], manifest, status="fallida", error=str(error))
        raise
