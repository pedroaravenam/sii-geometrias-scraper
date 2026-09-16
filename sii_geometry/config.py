from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config" / "sii_geometry.json"
DEFAULT_LOCAL_CONFIG_PATH = REPOSITORY_ROOT / "config" / "sii_geometry.local.json"


@dataclass(frozen=True)
class Settings:
    periodo_geometria: str
    zoom: int
    supercell_tiles: int
    block_supercells: int
    block_overlap_supercells: int
    boundary_buffer_m: float
    request_delay_s: float
    request_timeout_s: float
    max_retries: int
    backoff_base_s: float
    urban_min_area_m2: float
    urban_max_area_m2: float
    large_polygon_min_area_m2: float
    fill_color: tuple[int, int, int]
    fill_tolerance: tuple[int, int, int]
    min_fill_alpha: int
    wms_url: str
    api_url: str
    reference_csv: Path | None = None
    storage_root: Path | None = None
    archive_wms_tiles: bool = True
    repository_root: Path = REPOSITORY_ROOT

    @property
    def raw_root(self) -> Path:
        return self.repository_root / "data" / "raw" / "geometrias"

    @property
    def processed_root(self) -> Path:
        return self.repository_root / "data" / "processed" / "geometrias"

    @property
    def reference_root(self) -> Path:
        return self.raw_root / "_reference"


def load_settings(path: Path | None = None, **overrides: Any) -> Settings:
    config_path = path or DEFAULT_CONFIG_PATH
    values = json.loads(config_path.read_text(encoding="utf-8"))
    local_path = config_path.with_name(f"{config_path.stem}.local.json")
    if local_path.exists():
        values.update(json.loads(local_path.read_text(encoding="utf-8")))
    environment_root = os.environ.get("SII_GEOMETRY_STORAGE_ROOT")
    if environment_root:
        values["storage_root"] = environment_root
    values.update({key: value for key, value in overrides.items() if value is not None})
    values["fill_color"] = tuple(values["fill_color"])
    values["fill_tolerance"] = tuple(values["fill_tolerance"])
    if values.get("storage_root"):
        expanded = os.path.expandvars(os.path.expanduser(str(values["storage_root"])))
        values["storage_root"] = Path(expanded).resolve()
    if values.get("reference_csv"):
        expanded = os.path.expandvars(os.path.expanduser(str(values["reference_csv"])))
        values["reference_csv"] = Path(expanded).resolve()
    return Settings(**values)
