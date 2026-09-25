from __future__ import annotations

import hashlib
import io
import json
import math
from collections import Counter
from pathlib import Path
from typing import Iterable
import geopandas as gpd
import shapely
import numpy as np
from PIL import Image
from rasterio.features import shapes
from rasterio.transform import from_bounds
from scipy import ndimage
from shapely import union_all
from shapely import wkb as shapely_wkb
from shapely.geometry import MultiPolygon, Polygon, box, shape
from shapely.validation import make_valid

from .client import SIIClient
from .config import Settings
from .state import (
    block_metrics_in_order,
    block_row_counts,
    commit_block,
    commit_component,
    commit_graph_page,
    component_member_count,
    component_member_polygons,
    component_roots,
    compute_identity_hash,
    ensure_components,
    fetch_polygon_page,
    graph_last_polygon_id,
    hash_file,
    iter_lower_candidates,
    iter_merged_parts,
    load_uf,
    locked_destination,
    open_vector_checkpoint,
    pending_component_roots,
    set_vector_stage,
    set_vector_total_blocks,
    uf_union,
    vector_stage,
    write_json_atomic,
)


#: Área mínima de intersección (m², EPSG:3857) para considerar dos polígonos de
#: bloques distintos como el mismo predio duplicado por el solape entre bloques
#: (invariante 4). Un simple toque de borde produce área cero y nunca fusiona.
DEFAULT_MERGE_OVERLAP_M2 = 0.25

#: Versión del algoritmo de vectorización/checkpoint. Cambiarla invalida todos
#: los checkpoints existentes (invariante 2): cualquier cambio que altere el
#: resultado geométrico (ventaneo de bloques, criterio de fusión, etc.) debe
#: incrementarla.
ALGO_VERSION = "vector-checkpoint-v1"

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


def _tile_metadata_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".meta.json")


def _tile_request_identity(
    commune_code: str, layer: str, sc_x: int, sc_y: int, settings: Settings
) -> str:
    """Identidad de la solicitud WMS que produjo (o produciría) el tile.

    Incluye comuna, capa, período, zoom y coordenadas de la supercelda
    (invariante de procedencia, hallazgo 4): un PNG cacheado sólo se
    reutiliza si corresponde EXACTAMENTE a la misma solicitud, no sólo a las
    mismas dimensiones de imagen.
    """
    payload = {
        "commune_code": commune_code,
        "layer": layer,
        "period": settings.periodo_geometria,
        "zoom": settings.zoom,
        "supercell_tiles": settings.supercell_tiles,
        "sc_x": sc_x,
        "sc_y": sc_y,
    }
    return compute_identity_hash(payload)


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
    """Descarga (o reutiliza) el PNG de una supercelda junto a su sidecar de
    identidad, publicando ambos bajo un lock exclusivo sobre `destination`
    (invariante 4/hallazgo carrera PNG-sidecar).

    Sin el lock, dos ejecuciones distintas apuntando al mismo `destination`
    (p. ej. reintentos concurrentes, o una capa distinta reutilizando el
    mismo tile) podrían intercalar sus escrituras y dejar el PNG de una
    ejecución conviviendo con el sidecar de la otra -- ambos "válidos" por
    separado pero de identidades distintas. El lock envuelve la sección
    completa "verificar cache válido -> descargar si falta -> publicar
    PNG+sidecar" para que cualquier secuencia intercalada de dos llamadas
    concurrentes se serialice en su totalidad.
    """
    expected_identity = _tile_request_identity(commune_code, layer, sc_x, sc_y, settings)
    metadata_path = _tile_metadata_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with locked_destination(destination):
        if not force and destination.exists() and destination.stat().st_size > 100:
            stored_identity = None
            if metadata_path.exists():
                try:
                    stored_identity = json.loads(metadata_path.read_text(encoding="utf-8")).get("identity_hash")
                except (OSError, json.JSONDecodeError):
                    stored_identity = None
            if stored_identity == expected_identity:
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
            temporary = destination.with_suffix(".png.part")
            rgba.save(temporary, format="PNG")
            # Invalida (borra) el sidecar existente ANTES de reemplazar el PNG:
            # si el proceso muere entre este punto y la escritura del sidecar
            # nuevo (más abajo), el sidecar queda AUSENTE en vez de seguir
            # describiendo la identidad anterior. Sin esto, una ejecución B que
            # reemplaza el PNG y muere antes de escribir su propio sidecar deja
            # el sidecar viejo (válido, de una ejecución A anterior) conviviendo
            # con el PNG nuevo de B; una lectura posterior para A vería una
            # identidad coincidente y serviría el PNG de B como si fuera el de A
            # (invariante 4, hallazgo ronda 4).
            metadata_path.unlink(missing_ok=True)
            temporary.replace(destination)
        write_json_atomic(metadata_path, {"identity_hash": expected_identity})


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


def vectorize_block(
    tiles_dir: Path,
    origin_x: int,
    origin_y: int,
    members: list[tuple[int, int]],
    step: int,
    settings: Settings,
) -> tuple[list[Polygon], dict[str, object], dict[tuple[int, int], str]]:
    """Vectoriza un único bloque; libera el canvas de píxeles al retornar en vez
    de mantenerlo (o los polígonos de todos los bloques) acumulados en RAM."""
    pixels = settings.supercell_tiles * TILE_PIXELS
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
    png_hashes: dict[tuple[int, int], str] = {}
    for sc_x, sc_y in members:
        path = tiles_dir / f"sc_{sc_x}_{sc_y}.png"
        png_hashes[(sc_x, sc_y)] = hash_file(path)
        with Image.open(path) as image:
            rgba = np.asarray(image.convert("RGBA"))
        x_offset = ((sc_x - origin_x) // step) * pixels
        y_offset = ((sc_y - origin_y) // step) * pixels
        canvas[y_offset : y_offset + pixels, x_offset : x_offset + pixels] = rgba
    left, _, _, top = supercell_bbox_3857(origin_x, origin_y, settings)
    _, bottom, right, _ = supercell_bbox_3857(max_x, max_y, settings)
    polygons, metrics = vectorize_image(canvas, (left, bottom, right, top), settings)
    del canvas
    metrics.update({"origin_x": origin_x, "origin_y": origin_y, "supercells": len(members)})
    return polygons, metrics, png_hashes


def _block_identity(
    origin_x: int, origin_y: int, members: list[tuple[int, int]], png_hashes: dict[tuple[int, int], str]
) -> str:
    """Identidad de bloque (invariante 3): origen, miembros ordenados y huellas
    SHA-256 de los PNG consumidos."""
    payload = {
        "origin": [origin_x, origin_y],
        "members": sorted(members),
        "png_hashes": {f"{x}_{y}": png_hashes[(x, y)] for x, y in sorted(png_hashes)},
    }
    return compute_identity_hash(payload)


def compute_run_identity(
    commune_code: str,
    layer: str,
    period: str,
    settings: Settings,
    supercells: list[tuple[int, int]],
) -> tuple[str, str]:
    """Identidad de ejecución (invariante 2).

    Deliberadamente NO incluye las huellas SHA-256 de cada PNG individual:
    hashear las decenas de miles de tiles antes de poder retomar el progreso
    anularía el propósito de la reanudación. La integridad de los PNG se
    verifica por bloque (invariante 3) al decidir si se reutiliza o se rehace
    cada bloque, lo que ya invalida el grafo/componentes/Parquet completos si
    algún PNG cambió o falta.
    """
    geos_version = getattr(shapely, "geos_version_string", None)
    payload = {
        "algo_version": ALGO_VERSION,
        "commune_code": commune_code,
        "layer": layer,
        "period": period,
        "supercells": sorted(supercells),
        "shapely_version": shapely.__version__,
        "geos_version": geos_version,
        "settings": {
            "zoom": settings.zoom,
            "supercell_tiles": settings.supercell_tiles,
            "block_supercells": settings.block_supercells,
            "block_overlap_supercells": settings.block_overlap_supercells,
            "boundary_buffer_m": settings.boundary_buffer_m,
            "urban_min_area_m2": settings.urban_min_area_m2,
            "urban_max_area_m2": settings.urban_max_area_m2,
            "fill_color": list(settings.fill_color),
            "fill_tolerance": list(settings.fill_tolerance),
            "min_fill_alpha": settings.min_fill_alpha,
            "large_component_max_polygons": settings.large_component_max_polygons,
            "large_component_hard_limit_polygons": settings.large_component_hard_limit_polygons,
            "component_tile_size": settings.component_tile_size,
        },
    }
    identity_hash = compute_identity_hash(payload)
    run_id = hashlib_sha256_short(f"{commune_code}:{period}:{layer}")
    return run_id, identity_hash


def hashlib_sha256_short(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _explode_geometry(geometry) -> list[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    parts = list(geometry.geoms) if isinstance(geometry, MultiPolygon) else [geometry]
    return [part for part in parts if isinstance(part, Polygon) and not part.is_empty]


def merge_component_adaptive(polygons: list[Polygon], tile_size: int) -> list[Polygon]:
    """Fusiona una componente potencialmente gigante en teselas acotadas.

    `union_all` es asociativo y conmutativo: agrupar en teselas de a lo sumo
    `tile_size` geometrías y unir esos resultados produce exactamente el mismo
    polígono final que `union_all(polygons)` de una sola vez -- no hace falta
    reconciliar costuras como en un overlay. Sólo cambia el pico de memoria de
    trabajo, que queda acotado a una tesela (invariante 6). Se valida contra
    `union_all` directo en `tests/test_geometry_scraper.py`.
    """
    if tile_size < 2:
        raise ValueError("component_tile_size debe ser >= 2 para poder subdividir")
    current: list = list(polygons)
    while len(current) > tile_size:
        ordered = sorted(current, key=lambda geom: (geom.bounds[0], geom.bounds[1]))
        current = [union_all(ordered[index : index + tile_size]) for index in range(0, len(ordered), tile_size)]
    if not current:
        return []
    return _explode_geometry(union_all(current) if len(current) > 1 else current[0])


def _run_block_stage(connection, tiles_dir: Path, blocks: list, settings: Settings) -> None:
    """Verifica/confirma cada bloque siempre, sin importar la etapa actual.

    La verificación (rehash de los PNG de cada bloque `done`) es barata frente
    a re-vectorizar, así que corre en cada llamada -- incluso si una ejecución
    previa ya llegó a `vectors` -- para detectar cambios/corrupción (invariante
    3). Sólo si algo se invalida (`commit_block` -> `invalidate_block`) la
    etapa retrocede a `blocks`; si nada cambió, la etapa de una ejecución ya
    completada permanece intacta y las etapas siguientes se saltan sin
    reprocesar el grafo/las componentes.
    """
    total = len(blocks)
    done = 0
    for block_id, (origin_x, origin_y, members, step) in enumerate(blocks):
        record = connection.execute(
            "SELECT identity_hash, status, metrics_json FROM vector_blocks WHERE block_id = ?", (block_id,)
        ).fetchone()
        if record is not None and record[1] == "done":
            verified_hashes = {(x, y): hash_file(tiles_dir / f"sc_{x}_{y}.png") for x, y in members}
            expected_polygons = json.loads(record[2]).get("polygons", 0)
            polygon_count, rtree_count = block_row_counts(connection, block_id)
            rows_consistent = polygon_count == expected_polygons and rtree_count == polygon_count
            if rows_consistent and _block_identity(origin_x, origin_y, members, verified_hashes) == record[0]:
                done += 1
                continue
        polygons, metrics, png_hashes = vectorize_block(tiles_dir, origin_x, origin_y, members, step, settings)
        identity_hash = _block_identity(origin_x, origin_y, members, png_hashes)
        polygons_data = [
            (shapely_wkb.dumps(polygon), polygon.area, *polygon.bounds) for polygon in polygons
        ]
        commit_block(connection, block_id, identity_hash, metrics, polygons_data)
        done += 1
        if done % 10 == 0 or done == total:
            print(f"    Vectorizacion: {done:,}/{total:,} bloques confirmados", flush=True)
    if vector_stage(connection) == "blocks":
        set_vector_stage(connection, "graph")


def _run_graph_stage(connection, settings: Settings) -> None:
    if vector_stage(connection) != "graph":
        return
    page_size = settings.vector_graph_page_size
    parent = load_uf(connection)
    while True:
        last = graph_last_polygon_id(connection)
        rows = fetch_polygon_page(connection, last, page_size)
        if not rows:
            break
        dirty: dict[int, int] = {}
        for polygon_id, block_id, wkb_bytes, minx, miny, maxx, maxy in rows:
            polygon = shapely_wkb.loads(wkb_bytes)
            candidate_iterator = iter_lower_candidates(
                connection, polygon_id, minx, miny, maxx, maxy, batch_size=page_size
            )
            while True:
                candidate_page = next(candidate_iterator, None)
                if candidate_page is None:
                    break
                for candidate_id, candidate_block, candidate_wkb in candidate_page:
                    if candidate_block == block_id:
                        continue
                    candidate_polygon = shapely_wkb.loads(candidate_wkb)
                    if polygon.intersection(candidate_polygon).area >= DEFAULT_MERGE_OVERLAP_M2:
                        changed = uf_union(parent, polygon_id, candidate_id)
                        if changed:
                            dirty.update(changed)
                candidate_page = None
        commit_graph_page(connection, rows[-1][0], dirty)
        print(f"    Grafo de duplicados: hasta polígono {rows[-1][0]:,}", flush=True)
    set_vector_stage(connection, "components")


def _run_components_stage(connection, settings: Settings) -> None:
    if vector_stage(connection) != "components":
        return
    page_size = settings.vector_graph_page_size
    parent = load_uf(connection)
    after_polygon_id = -1
    while True:
        page = connection.execute(
            "SELECT polygon_id FROM vector_polygons WHERE polygon_id > ? ORDER BY polygon_id LIMIT ?",
            (after_polygon_id, page_size),
        ).fetchall()
        if not page:
            break
        polygon_ids = [row[0] for row in page]
        ensure_components(connection, component_roots(parent, polygon_ids))
        after_polygon_id = polygon_ids[-1]
    pending_roots = pending_component_roots(connection)
    total = connection.execute("SELECT COUNT(*) FROM vector_components").fetchone()[0]
    processed = total - len(pending_roots)
    for root_id in pending_roots:
        member_count = component_member_count(connection, root_id)
        if member_count > settings.large_component_hard_limit_polygons:
            raise RuntimeError(
                f"Componente {root_id} tiene {member_count:,} polígonos y supera el límite duro "
                f"configurado large_component_hard_limit_polygons={settings.large_component_hard_limit_polygons:,} "
                "(invariante 6); revise la simbología WMS o los parámetros de fusión antes de continuar. "
                "No se cargaron ni unieron sus geometrías."
            )
        members = component_member_polygons(connection, root_id)
        polygons = [shapely_wkb.loads(wkb_bytes) for _, wkb_bytes, _ in members]
        if len(polygons) == 1:
            merged = polygons
        elif len(polygons) > settings.large_component_max_polygons:
            merged = merge_component_adaptive(polygons, settings.component_tile_size)
        else:
            merged = _explode_geometry(union_all(polygons))
        parts = []
        for polygon in merged:
            area = polygon.area
            size_class = "large_component" if area > settings.urban_max_area_m2 else "parcel_scale"
            parts.append((shapely_wkb.dumps(polygon), area, size_class))
        commit_component(connection, root_id, parts)
        processed += 1
        if processed % 50 == 0 or processed == total:
            print(f"    Componentes fusionadas: {processed:,}/{total:,}", flush=True)
    set_vector_stage(connection, "vectors")


def _load_vectors_from_checkpoint(connection) -> gpd.GeoDataFrame:
    records: list[dict[str, object]] = []
    geometries: list[Polygon] = []
    index = 0
    for rows in iter_merged_parts(connection):
        for _id, wkb_bytes, area, size_class in rows:
            geometries.append(shapely_wkb.loads(wkb_bytes))
            records.append({"_poly_idx": index, "pol_area_m2": area, "pol_size_class": size_class})
            index += 1
    if not geometries:
        return gpd.GeoDataFrame({"_poly_idx": []}, geometry=[], crs=4326)
    return gpd.GeoDataFrame(records, geometry=geometries, crs=3857).to_crs(4326)


def vectorize_supercells(
    tiles_dir: Path,
    supercells: list[tuple[int, int]],
    settings: Settings,
    checkpoint_path: Path,
    *,
    commune_code: str,
    layer: str,
    period: str,
) -> tuple[gpd.GeoDataFrame, list[dict[str, object]]]:
    """Vectoriza por bloques con checkpoint reanudable (etapas
    `blocks -> graph -> components -> vectors`).

    A diferencia de la versión anterior, nunca mantiene todos los polígonos
    crudos de todos los bloques en RAM a la vez: cada bloque se confirma en
    SQLite (WKB + métricas + huellas de PNG) y libera su canvas/polígonos
    antes de procesar el siguiente; el grafo de duplicados y la fusión de
    componentes avanzan por páginas/componentes también confirmados en disco.
    """
    blocks = list(_block_origins(supercells, settings))
    run_id, identity_hash = compute_run_identity(commune_code, layer, period, settings, supercells)
    with open_vector_checkpoint(checkpoint_path, run_id, identity_hash) as connection:
        set_vector_total_blocks(connection, len(blocks))
        _run_block_stage(connection, tiles_dir, blocks, settings)
        _run_graph_stage(connection, settings)
        _run_components_stage(connection, settings)
        block_metrics = block_metrics_in_order(connection)
        vectors = _load_vectors_from_checkpoint(connection)
    return vectors, block_metrics
