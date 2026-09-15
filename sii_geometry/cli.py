from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import requests

from .catalog import find_commune, load_catalog, normalize_name, select_interactively
from .client import SIIClient
from .config import DEFAULT_CONFIG_PATH, load_settings
from .dialogs import select_directory
from .pipeline import process_commune
from .reference_assets import ensure_regional_references
from .state import read_manifest
from .storage import (
    detect_onedrive_root,
    initialize_catalog,
    local_config_path,
    publish_commune,
    save_reference_configuration,
    save_storage_configuration,
)


REQUIRED_MODULES = ["requests", "numpy", "pandas", "PIL", "scipy", "shapely", "rasterio", "geopandas", "pyarrow", "pyogrio"]


def doctor(config_path: Path) -> int:
    failures = []
    for module in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except ImportError as error:
            failures.append(f"{module}: {error}")
    try:
        settings = load_settings(config_path)
        settings.raw_root.mkdir(parents=True, exist_ok=True)
        settings.processed_root.mkdir(parents=True, exist_ok=True)
    except Exception as error:
        failures.append(f"configuración: {error}")
    if failures:
        print("Instalación incompleta:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("Entorno listo para ejecutar el scraper de geometrías SII.")
    print(f"Configuración: {config_path}")
    return 0


def show_status(config_path: Path, period: str | None) -> int:
    settings = load_settings(config_path, periodo_geometria=period)
    period_root = settings.raw_root / settings.periodo_geometria
    manifests = sorted(period_root.glob("*/manifest.json")) if period_root.exists() else []
    if not manifests:
        print(f"No hay ejecuciones registradas para {settings.periodo_geometria}.")
        return 0
    print(f"Estado de geometrías {settings.periodo_geometria}:")
    for path in manifests:
        manifest = read_manifest(path)
        commune = manifest.get("commune", {})
        counts: dict[str, int] = {}
        database = path.parent / "checkpoints" / "state.sqlite"
        if database.exists():
            try:
                uri = f"file:{database.resolve().as_posix()}?mode=ro"
                with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                    counts = {
                        str(status): int(count)
                        for status, count in connection.execute(
                            "SELECT status, COUNT(*) FROM api_results "
                            "WHERE source='role' GROUP BY status"
                        )
                    }
            except sqlite3.Error:
                counts = {}
        completed_roles = counts.get("ok", 0) + counts.get("not_found", 0)
        total_roles = manifest.get("historical_role_candidates")
        role_progress = f" | roles {completed_roles:,}/{int(total_roles):,}" if total_roles else ""
        if counts.get("error"):
            role_progress += f" | errores {counts['error']:,}"
        backup = manifest.get("storage_backup", {}).get("status", "pendiente")
        print(
            f"  {commune.get('sii_code', '?'):>5}  {commune.get('name', path.parent.name):<28} "
            f"{manifest.get('status', 'desconocido')}{role_progress} | respaldo {backup}"
        )
    return 0


def configure_storage(config_path: Path, selected_path: Path | None, archive_wms_tiles: bool) -> int:
    current = load_settings(config_path)
    if selected_path is None and current.storage_root:
        initialize_catalog(current, current.periodo_geometria)
        print(f"Almacenamiento ya configurado: {current.storage_root}")
        print(f"Configuración local: {local_config_path(config_path)}")
        return 0
    candidate = selected_path
    if candidate is None and not sys.stdin.isatty():
        print("Indique la carpeta de resultados con --path.", file=sys.stderr)
        return 1
    if candidate is None:
        suggested = detect_onedrive_root()
        initial = suggested.parent if suggested else Path.home()
        print("Seleccione la carpeta local o sincronizada donde guardar los resultados.")
        candidate = select_directory("Carpeta para resultados del Catastro SII", initial)
        if candidate is None:
            entered = input("Ruta de la carpeta (Enter para cancelar): ").strip()
            if not entered:
                print("Configuración cancelada.")
                return 1
            candidate = Path(entered)
    destination = save_storage_configuration(config_path, candidate, archive_wms_tiles)
    configured = load_settings(config_path)
    initialize_catalog(configured, configured.periodo_geometria)
    print(f"Almacenamiento configurado: {candidate.resolve()}")
    print(f"Configuración local (no se sube a Git): {destination}")
    return 0


def resolve_reference_csv(config_path: Path, selected_path: Path | None) -> Path | None:
    settings = load_settings(config_path)
    default_candidates = sorted(
        (settings.repository_root / "data" / "raw" / "catastral").glob("catastro_2026_1*.csv")
    )
    candidate = selected_path or settings.reference_csv or (default_candidates[-1] if default_candidates else None)
    if candidate and candidate.is_file():
        if selected_path:
            save_reference_configuration(config_path, candidate)
        return candidate.resolve()
    if selected_path is not None:
        raise FileNotFoundError(f"No existe el insumo histórico configurado: {candidate}")
    if candidate is not None:
        print(f"[AVISO] Insumo local no encontrado; se usará la descarga regional: {candidate}")
    return None


def publish_storage(config_path: Path, period: str | None, commune: str | None) -> int:
    settings = load_settings(config_path, periodo_geometria=period)
    if settings.storage_root is None:
        print("Primero ejecute configure-storage.", file=sys.stderr)
        return 1
    period_root = settings.raw_root / settings.periodo_geometria
    manifests = sorted(period_root.glob("*/manifest.json")) if period_root.exists() else []
    if commune:
        target = normalize_name(commune)
        manifests = [
            path
            for path in manifests
            if target
            in {
                normalize_name(str(read_manifest(path).get("commune", {}).get("sii_code", ""))),
                normalize_name(str(read_manifest(path).get("commune", {}).get("name", ""))),
            }
        ]
    if not manifests:
        print("No se encontraron comunas locales para respaldar.")
        return 0
    failures = 0
    for manifest_path in manifests:
        manifest = read_manifest(manifest_path)
        if manifest.get("status") not in {"completa", "completa_con_observaciones"}:
            print(f"[SKIP] {manifest_path.parent.name}: {manifest.get('status', 'sin estado')}")
            continue
        try:
            result = publish_commune(settings, manifest_path)
            print(f"[BACKUP] {manifest_path.parent.name}: {result['geoparquet']}")
        except Exception as error:
            failures += 1
            print(f"[ERROR] {manifest_path.parent.name}: {error}", file=sys.stderr)
    return 1 if failures else 0


def _resolve_communes(args, settings, session: requests.Session):
    catalog = load_catalog(settings, session)
    if args.comuna:
        return [find_commune(catalog, value) for value in args.comuna]
    if args.region:
        target = normalize_name(args.region)
        selected = [item for item in catalog if normalize_name(item.region) == target]
        if not selected:
            raise ValueError(f"Región no encontrada: {args.region}")
        return selected
    return select_interactively(catalog)


def scrape(args) -> int:
    settings = load_settings(args.config, periodo_geometria=args.periodo)
    bootstrap_client = SIIClient(settings)
    communes = _resolve_communes(args, settings, bootstrap_client.session)
    reference_csv = resolve_reference_csv(args.config, args.reference_csv)
    regional_references: dict[str, Path] = {}
    if not args.dry_run and reference_csv is None:
        regional_references = ensure_regional_references(settings, communes)
    initialize_catalog(settings, settings.periodo_geometria)
    print(f"\nPeríodo geométrico: {settings.periodo_geometria}")
    print("Comunas seleccionadas: " + ", ".join(f"{item.name} ({item.sii_code})" for item in communes))
    failures = 0
    for commune in communes:
        print(f"\n{'=' * 72}\n{commune.name} ({commune.sii_code})\n{'=' * 72}")
        try:
            commune_reference = reference_csv
            if not args.dry_run and commune_reference is None:
                commune_reference = regional_references[normalize_name(commune.region)]
            manifest = process_commune(
                commune,
                settings,
                force=args.force,
                reference_csv=commune_reference,
                dry_run=args.dry_run,
                max_supercells=args.max_supercells,
                only_supercells=args.supercell,
                max_roles=args.max_roles,
                max_orphans=args.max_orphans,
            )
            print(f"Estado: {manifest.get('status')}")
        except KeyboardInterrupt:
            print("\nInterrumpido por el usuario. El avance quedó guardado.")
            return 130
        except Exception as error:
            failures += 1
            print(f"ERROR: {error}", file=sys.stderr)
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scraper reproducible de geometrías prediales SII")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="Verificar instalación local")
    doctor_parser.set_defaults(handler=lambda args: doctor(args.config))

    status_parser = subparsers.add_parser("status", help="Mostrar comunas procesadas")
    status_parser.add_argument("--periodo")
    status_parser.set_defaults(handler=lambda args: show_status(args.config, args.periodo))

    configure_parser = subparsers.add_parser("configure-storage", help="Configurar carpeta de resultados")
    configure_parser.add_argument("--path", type=Path, help="Carpeta local o sincronizada para resultados")
    configure_parser.add_argument(
        "--without-wms-tiles",
        action="store_true",
        help="No archivar los PNG WMS en ZIP",
    )
    configure_parser.set_defaults(
        handler=lambda args: configure_storage(args.config, args.path, not args.without_wms_tiles)
    )

    publish_parser = subparsers.add_parser("publish", help="Respaldar comunas terminadas")
    publish_parser.add_argument("--periodo")
    publish_parser.add_argument("--comuna", help="Código SII o nombre; omitir para todas")
    publish_parser.set_defaults(handler=lambda args: publish_storage(args.config, args.periodo, args.comuna))

    scrape_parser = subparsers.add_parser("scrape", help="Seleccionar y procesar regiones o comunas")
    scope = scrape_parser.add_mutually_exclusive_group()
    scope.add_argument("--comuna", action="append", help="Código SII o nombre; puede repetirse")
    scope.add_argument("--region", help="Procesar todas las comunas de una región")
    scrape_parser.add_argument("--periodo", default=None, help="Snapshot, por ejemplo 2026S2")
    scrape_parser.add_argument("--reference-csv", type=Path, help="CSV o Parquet histórico opcional")
    scrape_parser.add_argument("--force", action="store_true", help="Reprocesar aunque la comuna esté completa")
    scrape_parser.add_argument("--dry-run", action="store_true", help="Planificar sin descargar tiles")
    scrape_parser.add_argument("--max-supercells", type=int, help="Límite de control; deja estado parcial")
    scrape_parser.add_argument(
        "--supercell",
        action="append",
        type=lambda value: tuple(int(part) for part in value.split(",")),
        help="Supercelda x,y específica para diagnóstico; puede repetirse",
    )
    scrape_parser.add_argument("--max-roles", type=int, help="Límite de consultas por rol; deja estado parcial")
    scrape_parser.add_argument("--max-orphans", type=int, help="Límite de recuperación de huérfanos; deja estado parcial")
    scrape_parser.set_defaults(handler=scrape)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.handler(args))
