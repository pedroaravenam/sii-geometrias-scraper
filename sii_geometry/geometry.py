from __future__ import annotations

import io
import math
from collections import Counter
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import numpy as np
from PIL import Image
from rasterio.features import shapes
from rasterio.transform import from_bounds
from scipy import ndimage
from shapely.geometry import MultiPolygon, Polygon, box, shape
from shapely.validation import make_valid

from .client import SIIClient
from .config import Settings


WEB_MERCATOR_HALF = 20_037_508.342789244
TILE_PIXELS = 256


def tile_size_m(zoom: int) -> float:
    return (WEB_MERCATOR_HALF * 2) / (2**zoom)


def supercell_bbox_3857(sc_x: int, sc_y: int, settings: Settings) -> tuple[float, float, float, float]:
    size = tile_size_m(settings.zoom)
    left = -WEB_MERCATOR_HALF + sc_x * size
    top = WEB_MERCATOR_HALF - sc_y * size
    right = left + settings.supercell_tiles * size
    bottom = top - settings.supercell_tiles * size
    return left, bottom, right, top


def calculate_supercells(boundary_4326, settings: Settings) -> list[tuple[int, int]]:
    boundary = gpd.GeoSeries([boundary_4326], crs=4326).to_crs(3857).iloc[0]
    coverage = boundary.buffer(settings.boundary_buffer_m)
    tile_m = tile_size_m(settings.zoom)
    super_tiles = settings.supercell_tiles
    super_m = tile_m * super_tiles
    minx, miny, maxx, maxy = coverage.bounds
    sx_min = math.floor((minx + WEB_MERCATOR_HALF) / tile_m / super_tiles) * super_tiles
    sx_max = math.floor((maxx + WEB_MERCATOR_HALF) / tile_m / super_tiles) * super_tiles
    sy_min = math.floor((WEB_MERCATOR_HALF - maxy) / tile_m / super_tiles) * super_tiles
    sy_max = math.floor((WEB_MERCATOR_HALF - miny) / tile_m / super_tiles) * super_tiles
    selected: list[tuple[int, int]] = []
    for sy in range(sy_min, sy_max + super_tiles, super_tiles):
        for sx in range(sx_min, sx_max + super_tiles, super_tiles):
            if coverage.intersects(box(*supercell_bbox_3857(sx, sy, settings))):
                selected.append((sx, sy))
    return sorted(selected, key=lambda item: (item[1], item[0]))


def download_supercell(
    client: SIIClient,
    commune_code: str,
    layer: str,
    sc_x: int,
    sc_y: int,
    destination: Path,
    settings: Settings,
    force: bool = False,
) -> None:
    if not force and destination.exists() and destination.stat().st_size > 100:
        try:
            with Image.open(destination) as image:
                if image.size == (settings.supercell_tiles * TILE_PIXELS,) * 2:
                    return
        except OSError:
            pass
    bbox = supercell_bbox_3857(sc_x, sc_y, settings)
    pixels = settings.supercell_tiles * TILE_PIXELS
    params = {
        "service": "WMS",
        "request": "GetMap",
        "layers": layer,
        "styles": "PREDIOS_WMS_V0",
        "format": "image/png",
        "transparent": "true",
        "version": "1.1.1",
        "comuna": int(commune_code),
        "eac": 0,
        "eacano": 0,
        "height": pixels,
        "width": pixels,
        "srs": "EPSG:3857",
        "bbox": ",".join(str(value) for value in bbox),
    }
    content = client.get_bytes(settings.wms_url, params=params)
    with Image.open(io.BytesIO(content)) as image:
        rgba = image.convert("RGBA")
        if rgba.size != (pixels, pixels):
            raise RuntimeError(f"Dimensión WMS inesperada para {sc_x},{sc_y}: {rgba.size}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".png.part")
        rgba.save(temporary, format="PNG")
        temporary.replace(destination)


def detect_fill_color(image: np.ndarray, settings: Settings) -> tuple[int, int, int]:
    alpha = image[:, :, 3]
    candidates = image[(alpha >= settings.min_fill_alpha) & (alpha < 250), :3]
    if len(candidates) < 100:
        return settings.fill_color
    quantized = (candidates // 4) * 4
    sample = quantized[:: max(1, len(quantized) // 200_000)]
    color, _ = Counter(map(tuple, sample.tolist())).most_common(1)[0]
    configured = np.asarray(settings.fill_color)
    if np.linalg.norm(np.asarray(color) - configured) > 70:
        return settings.fill_color
    return tuple(int(value) for value in color)


def fill_mask(image: np.ndarray, settings: Settings) -> tuple[np.ndarray, tuple[int, int, int]]:
    color = detect_fill_color(image, settings)
    rgb = image[:, :, :3].astype(np.int16)
    target = np.asarray(color, dtype=np.int16)
    tolerance = np.asarray(settings.fill_tolerance, dtype=np.int16)
    alpha = image[:, :, 3]
    mask = (np.abs(rgb - target) <= tolerance).all(axis=2) & (alpha >= settings.min_fill_alpha)
    return mask, color


def vectorize_image(
    image: np.ndarray,
    bounds_3857: tuple[float, float, float, float],
    settings: Settings,
) -> tuple[list[Polygon], dict[str, object]]:
    mask, color = fill_mask(image, settings)
    transform = from_bounds(*bounds_3857, image.shape[1], image.shape[0])
    polygons: list[Polygon] = []
    for geometry_mapping, value in shapes(
        mask.astype(np.uint8), mask=mask, connectivity=8, transform=transform
    ):
        if value == 0:
            continue
        geometry = shape(geometry_mapping)
        if not geometry.is_valid:
            geometry = make_valid(geometry)
        parts = list(geometry.geoms) if isinstance(geometry, MultiPolygon) else [geometry]
        for polygon in parts:
            if not isinstance(polygon, Polygon):
                continue
            area = polygon.area
            if area >= settings.urban_min_area_m2:
                small_holes_removed = [
                    ring for ring in polygon.interiors if Polygon(ring).area >= 50
                ]
                polygons.append(Polygon(polygon.exterior, small_holes_removed))
    metrics = {
        "fill_color_detected": list(color),
        "fill_pixels": int(mask.sum()),
        "fill_ratio": float(mask.mean()),
        "polygons": len(polygons),
    }
    return polygons, metrics


def _axis_windows(values: Iterable[int], block_size: int, stride: int, step: int):
    """Produce ventanas solapadas sin atravesar saltos territoriales."""
    ordered = sorted(set(values))
    if not ordered:
        return
    runs: list[list[int]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - runs[-1][-1] == step:
            runs[-1].append(value)
        else:
            runs.append([value])
    for run in runs:
        for index in range(0, len(run), stride):
            yield run[index : index + block_size]


def _block_origins(supercells: Iterable[tuple[int, int]], settings: Settings):
    cells = set(supercells)
    stride = max(1, settings.block_supercells - settings.block_overlap_supercells)
    step = settings.supercell_tiles
    x_windows = list(_axis_windows((x for x, _ in cells), settings.block_supercells, stride, step))
    y_windows = list(_axis_windows((y for _, y in cells), settings.block_supercells, stride, step))
    for block_ys in y_windows:
        for block_xs in x_windows:
            members = [(x, y) for y in block_ys for x in block_xs if (x, y) in cells]
            if members:
                yield block_xs[0], block_ys[0], members, step


def merge_overlapping_polygons(
    polygons: list[Polygon],
    sources: list[int] | None = None,
    minimum_overlap_m2: float = 0.25,
) -> list[Polygon]:
    """Une duplicados de bloques solapados sin fusionar predios que sólo se tocan."""
    if not polygons:
        return []
    sources = sources or list(range(len(polygons)))
    from shapely import union_all
    from shapely.strtree import STRtree

    parent = list(range(len(polygons)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    tree = STRtree(polygons)
    for index, polygon in enumerate(polygons):
        for candidate in tree.query(polygon):
            candidate = int(candidate)
            if candidate <= index:
                continue
            if sources[candidate] == sources[index]:
                continue
            intersection = polygon.intersection(polygons[candidate])
            if intersection.area >= minimum_overlap_m2:
                union(index, candidate)

    groups: dict[int, list[Polygon]] = {}
    for index, polygon in enumerate(polygons):
        groups.setdefault(find(index), []).append(polygon)
    merged: list[Polygon] = []
    for group in groups.values():
        geometry = group[0] if len(group) == 1 else union_all(group)
        parts = list(geometry.geoms) if isinstance(geometry, MultiPolygon) else [geometry]
        merged.extend(part for part in parts if isinstance(part, Polygon) and not part.is_empty)
    return merged


def vectorize_supercells(
    tiles_dir: Path,
    supercells: list[tuple[int, int]],
    settings: Settings,
) -> tuple[gpd.GeoDataFrame, list[dict[str, object]]]:
    pixels = settings.supercell_tiles * TILE_PIXELS
    blocks = list(_block_origins(supercells, settings))
    all_polygons: list[Polygon] = []
    polygon_sources: list[int] = []
    block_metrics: list[dict[str, object]] = []
    for block_id, (origin_x, origin_y, members, step) in enumerate(blocks):
        max_x = max(x for x, _ in members)
        max_y = max(y for _, y in members)
        width_cells = ((max_x - origin_x) // step) + 1
        height_cells = ((max_y - origin_y) // step) + 1
        if width_cells > settings.block_supercells or height_cells > settings.block_supercells:
            raise RuntimeError(
                "Bloque cartográfico discontinuo: se evitó una asignación excesiva de memoria "
                f"({width_cells}x{height_cells} superceldas)"
            )
        canvas = np.zeros((height_cells * pixels, width_cells * pixels, 4), dtype=np.uint8)
        for sc_x, sc_y in members:
            path = tiles_dir / f"sc_{sc_x}_{sc_y}.png"
            with Image.open(path) as image:
                rgba = np.asarray(image.convert("RGBA"))
            x_offset = ((sc_x - origin_x) // step) * pixels
            y_offset = ((sc_y - origin_y) // step) * pixels
            canvas[y_offset : y_offset + pixels, x_offset : x_offset + pixels] = rgba
        left, _, _, top = supercell_bbox_3857(origin_x, origin_y, settings)
        _, bottom, right, _ = supercell_bbox_3857(max_x, max_y, settings)
        polygons, metrics = vectorize_image(canvas, (left, bottom, right, top), settings)
        metrics.update({"origin_x": origin_x, "origin_y": origin_y, "supercells": len(members)})
        block_metrics.append(metrics)
        all_polygons.extend(polygons)
        polygon_sources.extend([block_id] * len(polygons))
        completed = block_id + 1
        if completed % 10 == 0 or completed == len(blocks):
            print(
                f"    Vectorizacion: {completed:,}/{len(blocks):,} bloques; "
                f"{len(all_polygons):,} componentes brutos",
                flush=True,
            )

    if not all_polygons:
        return gpd.GeoDataFrame({"_poly_idx": []}, geometry=[], crs=4326), block_metrics
    valid_parts = merge_overlapping_polygons(all_polygons, polygon_sources)
    areas = [part.area for part in valid_parts]
    vectors = gpd.GeoDataFrame(
        {
            "_poly_idx": range(len(valid_parts)),
            "pol_area_m2": areas,
            "pol_size_class": [
                "large_component" if area > settings.urban_max_area_m2 else "parcel_scale"
                for area in areas
            ],
        },
        geometry=valid_parts,
        crs=3857,
    ).to_crs(4326)
    return vectors, block_metrics
