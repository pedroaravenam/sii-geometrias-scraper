from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import requests

from .config import Settings


@dataclass(frozen=True)
class Commune:
    sii_code: str
    name: str
    region: str

    @property
    def slug(self) -> str:
        normalized = unicodedata.normalize("NFKD", self.name)
        ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
        return "_".join(ascii_name.lower().replace("'", "").split())


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    return " ".join(normalized.encode("ascii", "ignore").decode("ascii").upper().split())


def ensure_reference_files(settings: Settings, session: requests.Session) -> tuple[Path, Path]:
    del session
    reference_root = settings.repository_root / "resources" / "reference"
    catalog_path = reference_root / "comunas_sii.json"
    boundaries_path = reference_root / "limites_comunales.geojson"
    missing = [path.name for path in (catalog_path, boundaries_path) if not path.is_file()]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(
            f"La copia del programa está incompleta: faltan {names} en {reference_root}. "
            "Vuelva a extraer el ZIP completo."
        )
    return catalog_path, boundaries_path


def load_catalog(settings: Settings, session: requests.Session) -> list[Commune]:
    catalog_path, _ = ensure_reference_files(settings, session)
    records = json.loads(catalog_path.read_text(encoding="utf-8"))
    communes = [
        Commune(str(item["id"]), str(item["nombre"]), str(item["region"]))
        for item in records
    ]
    if not any(item.sii_code == "8108" for item in communes):
        communes.append(Commune("8108", "Trehuaco", "Ñuble"))
    return sorted(communes, key=lambda item: (normalize_name(item.region), normalize_name(item.name)))


def load_boundary(commune: Commune, settings: Settings, session: requests.Session):
    _, boundaries_path = ensure_reference_files(settings, session)
    boundaries = gpd.read_file(boundaries_path, engine="pyogrio")
    name_columns = [column for column in boundaries.columns if normalize_name(column) in {"COMUNA", "NOMBRE"}]
    if not name_columns:
        raise RuntimeError(f"La fuente territorial no contiene una columna de comuna: {list(boundaries.columns)}")
    boundary_name = commune.name.replace("Santiago Centro", "Santiago")
    if normalize_name(boundary_name) == "TREHUACO":
        boundary_name = "Treguaco"
    normalized_target = normalize_name(boundary_name)
    mask = boundaries[name_columns[0]].map(normalize_name) == normalized_target
    selected = boundaries.loc[mask]
    if selected.empty:
        raise RuntimeError(f"No se encontró el límite de {commune.name} en {boundaries_path}")
    if len(selected) > 1:
        selected = gpd.GeoDataFrame(
            {"name": [commune.name]}, geometry=[selected.geometry.union_all()], crs=boundaries.crs
        )
    return selected.to_crs(4326).geometry.iloc[0]


def find_commune(catalog: list[Commune], value: str) -> Commune:
    normalized = normalize_name(value)
    exact = [item for item in catalog if item.sii_code == value or normalize_name(item.name) == normalized]
    if len(exact) != 1:
        raise ValueError(f"Comuna no encontrada o ambigua: {value}")
    return exact[0]


def select_interactively(catalog: list[Commune]) -> list[Commune]:
    regions = sorted({item.region for item in catalog}, key=normalize_name)
    print("\nRegiones disponibles:")
    for index, region in enumerate(regions, start=1):
        print(f"  {index:2d}. {region}")
    raw_region = input("\nRegión (número, Enter para elegir comuna directamente): ").strip()
    candidates = catalog
    if raw_region:
        region = regions[int(raw_region) - 1]
        candidates = [item for item in catalog if item.region == region]
        all_region = input(f"¿Procesar todas las comunas de {region}? [s/N]: ").strip().lower()
        if all_region in {"s", "si", "sí", "y", "yes"}:
            return candidates
    print("\nComunas disponibles:")
    for index, commune in enumerate(candidates, start=1):
        print(f"  {index:3d}. {commune.name} ({commune.sii_code})")
    selections = input("\nComunas (números separados por coma): ").strip().split(",")
    return [candidates[int(value.strip()) - 1] for value in selections if value.strip()]
