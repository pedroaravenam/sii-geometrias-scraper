from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import geopandas as gpd
import pyarrow.parquet as pq

try:  # pragma: no cover - rama exclusiva de POSIX
    import fcntl
except ImportError:  # pragma: no cover - rama exclusiva de Windows
    fcntl = None

try:  # pragma: no cover - rama exclusiva de Windows
    import msvcrt
except ImportError:  # pragma: no cover - rama exclusiva de POSIX
    msvcrt = None

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
def locked_destination(path: Path) -> Iterator[None]:
    """Serializa, entre procesos y threads, la sección crítica que publica
    `path` (y cualquier sidecar asociado) frente a lectores/escritores
    concurrentes del mismo destino.

    Usa un lockfile `<path>.lock` con bloqueo exclusivo del sistema operativo
    (`fcntl.flock` en POSIX, `msvcrt.locking` en Windows) en vez de una
    bandera propia: dos ejecuciones distintas del scraper -- o los reintentos
    internos de una misma ejecución -- que intenten publicar el mismo
    destino se turnan la sección completa "verificar cache válido ->
    descargar si falta -> escribir archivo(s) finales", evitando la carrera
    TOCTOU donde una ejecución publica su parte y otra la suya intercaladas
    (hallazgo: PNG/sidecar de identidades distintas conviviendo como
    "válidos"). El lock se libera siempre al salir del bloque, incluso si el
    cuerpo lanza una excepción.
    """
    lock_path = path.parent / (path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - ejercitado sólo en Windows
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.02)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - ejercitado sólo en Windows
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()

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


# --- Checkpoint de vectorización reanudable -------------------------------
#
# Invariante 1: un bloque completado significa polígonos + métricas + huellas
# de sus PNG confirmados en una misma transacción durable (no un contador).
# Invariante 2: identidad distinta (algoritmo/comuna/periodo/capa/selección/
# parámetros geométricos) implica un espacio de checkpoint distinto: NUNCA
# se mezclan bloques de dos ejecuciones. Aquí se resuelve wipeando las tablas
# cuando la identidad guardada no coincide, en vez de particionar por run_id
# dentro de las mismas tablas (una base de datos por comuna == un run activo).
# Invariante 5/7: el grafo de duplicados y la fusión de componentes avanzan
# en páginas confirmadas transaccionalmente; nunca se cargan todas las
# geometrías crudas o todas las componentes fusionadas a la vez.

VECTOR_STAGES = ("blocks", "graph", "components", "vectors")

_VECTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS vector_run (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    run_id TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'blocks',
    total_blocks INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vector_blocks (
    block_id INTEGER PRIMARY KEY,
    identity_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    committed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vector_polygons (
    polygon_id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id INTEGER NOT NULL,
    wkb BLOB NOT NULL,
    area REAL NOT NULL,
    minx REAL NOT NULL,
    miny REAL NOT NULL,
    maxx REAL NOT NULL,
    maxy REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS vector_polygons_rtree USING rtree(
    polygon_id, minx, maxx, miny, maxy
);
CREATE TABLE IF NOT EXISTS vector_graph_progress (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_polygon_id INTEGER NOT NULL DEFAULT -1
);
CREATE TABLE IF NOT EXISTS vector_uf (
    polygon_id INTEGER PRIMARY KEY,
    parent INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS vector_components (
    root_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vector_component_members (
    root_id INTEGER NOT NULL,
    polygon_id INTEGER NOT NULL,
    PRIMARY KEY (root_id, polygon_id)
);
CREATE TABLE IF NOT EXISTS vector_merged_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER NOT NULL,
    part_index INTEGER NOT NULL,
    wkb BLOB NOT NULL,
    area REAL NOT NULL,
    size_class TEXT NOT NULL,
    UNIQUE (root_id, part_index)
);
"""

_VECTOR_TABLES = (
    "vector_merged_parts",
    "vector_component_members",
    "vector_components",
    "vector_uf",
    "vector_graph_progress",
    "vector_polygons_rtree",
    "vector_polygons",
    "vector_blocks",
    "vector_run",
)


def hash_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_identity_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reset_vector_checkpoint(connection: sqlite3.Connection, run_id: str, identity_hash: str) -> None:
    with connection:
        for table in _VECTOR_TABLES:
            connection.execute(f"DELETE FROM {table}")
        now = utc_now()
        connection.execute(
            "INSERT INTO vector_run (id, run_id, identity_hash, stage, total_blocks, created_at, updated_at) "
            "VALUES (1, ?, ?, 'blocks', 0, ?, ?)",
            (run_id, identity_hash, now, now),
        )
        connection.execute("INSERT INTO vector_graph_progress (id, last_polygon_id) VALUES (1, -1)")


@contextmanager
def open_vector_checkpoint(path: Path, run_id: str, identity_hash: str) -> Iterator[sqlite3.Connection]:
    """Abre (o reinicia) el checkpoint de vectorización de una comuna.

    Si el checkpoint ya existe con otra `run_id`/`identity_hash`, o está
    corrupto, se reinicia por completo: nunca se mezclan bloques de dos
    ejecuciones (invariante 2).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = bool(integrity) and integrity[0] == "ok"
        except sqlite3.DatabaseError:
            integrity_ok = False
        if not integrity_ok:
            connection.close()
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(path) + suffix)
                if candidate.exists():
                    candidate.unlink()
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(_VECTOR_SCHEMA)
        connection.commit()
        existing = connection.execute(
            "SELECT run_id, identity_hash FROM vector_run WHERE id = 1"
        ).fetchone()
        if existing is None:
            _reset_vector_checkpoint(connection, run_id, identity_hash)
        elif tuple(existing) != (run_id, identity_hash):
            _reset_vector_checkpoint(connection, run_id, identity_hash)
        yield connection
    finally:
        connection.close()


def vector_stage(connection: sqlite3.Connection) -> str:
    return connection.execute("SELECT stage FROM vector_run WHERE id = 1").fetchone()[0]


def set_vector_stage(connection: sqlite3.Connection, stage: str) -> None:
    if stage not in VECTOR_STAGES:
        raise ValueError(f"Etapa de vectorización desconocida: {stage}")
    with connection:
        connection.execute("UPDATE vector_run SET stage = ?, updated_at = ? WHERE id = 1", (stage, utc_now()))


def set_vector_total_blocks(connection: sqlite3.Connection, total: int) -> None:
    with connection:
        connection.execute(
            "UPDATE vector_run SET total_blocks = ?, updated_at = ? WHERE id = 1", (total, utc_now())
        )


def block_record(connection: sqlite3.Connection, block_id: int) -> tuple[str, str, str] | None:
    """Devuelve (identity_hash, status, metrics_json) del bloque si existe."""
    row = connection.execute(
        "SELECT identity_hash, status, metrics_json FROM vector_blocks WHERE block_id = ?", (block_id,)
    ).fetchone()
    return tuple(row) if row is not None else None


def block_row_counts(connection: sqlite3.Connection, block_id: int) -> tuple[int, int]:
    """(polígonos, filas R-tree indexadas) persistidos para `block_id`.

    Usado para detectar corrupción parcial (borrado manual de filas de
    `vector_polygons`/`vector_polygons_rtree` sin tocar `vector_blocks`)
    aunque el bloque siga marcado como `done` con el hash de identidad
    correcto -- el hash de identidad sólo certifica los PNG de origen, no
    que las filas derivadas sigan presentes (hallazgo 1).
    """
    polygon_count = connection.execute(
        "SELECT COUNT(*) FROM vector_polygons WHERE block_id = ?", (block_id,)
    ).fetchone()[0]
    rtree_count = connection.execute(
        "SELECT COUNT(*) FROM vector_polygons_rtree WHERE polygon_id IN "
        "(SELECT polygon_id FROM vector_polygons WHERE block_id = ?)",
        (block_id,),
    ).fetchone()[0]
    return polygon_count, rtree_count


def _invalidate_block_locked(connection: sqlite3.Connection, block_id: int) -> None:
    """Cuerpo de `invalidate_block` sin abrir su propia transacción.

    Debe ejecutarse dentro de una transacción ya abierta por el llamador
    (p.ej. `commit_block`) para que el descarte del bloque y la inserción de
    su reemplazo sean atómicos (hallazgo 5): si el reemplazo falla, el
    `ROLLBACK` de la transacción exterior deshace también este descarte, en
    vez de dejarlo confirmado por separado.
    """
    connection.execute(
        "DELETE FROM vector_polygons_rtree WHERE polygon_id IN "
        "(SELECT polygon_id FROM vector_polygons WHERE block_id = ?)",
        (block_id,),
    )
    connection.execute("DELETE FROM vector_polygons WHERE block_id = ?", (block_id,))
    connection.execute("DELETE FROM vector_blocks WHERE block_id = ?", (block_id,))
    connection.execute("DELETE FROM vector_uf")
    connection.execute("UPDATE vector_graph_progress SET last_polygon_id = -1")
    connection.execute("DELETE FROM vector_components")
    connection.execute("DELETE FROM vector_component_members")
    connection.execute("DELETE FROM vector_merged_parts")
    connection.execute("UPDATE vector_run SET stage = 'blocks', updated_at = ? WHERE id = 1", (utc_now(),))


def invalidate_block(connection: sqlite3.Connection, block_id: int) -> None:
    """Descarta un bloque y todo su progreso derivado (grafo/componentes/parquet).

    Invariante 3: ausencia, cambio o corrupción del bloque invalida el grafo,
    las componentes y el Parquet final de la ejecución completa, no sólo ese
    bloque, porque el grafo de duplicados depende de comparar contra TODOS
    los polígonos ya indexados. Punto de entrada independiente: abre su
    propia transacción. Cuando se llama desde dentro de otra transacción
    (p.ej. `commit_block`) usar `_invalidate_block_locked` en su lugar.
    """
    with connection:
        _invalidate_block_locked(connection, block_id)


def commit_block(
    connection: sqlite3.Connection,
    block_id: int,
    identity_hash: str,
    metrics: dict[str, Any],
    polygons: Sequence[tuple[bytes, float, float, float, float, float]],
) -> None:
    """Confirma un bloque en una única transacción durable (invariante 1).

    Siempre invalida primero el grafo/componentes/vectors aguas abajo del
    `run_id` -- exista o no un registro previo del bloque (hallazgo 1): una
    reconstrucción puede ocurrir porque el bloque cambió, porque faltaba, o
    porque sus filas de polígonos/R-tree quedaron inconsistentes, y en
    cualquiera de esos casos el grafo de duplicados y las componentes ya
    calculados dejan de ser válidos. La invalidación corre en la MISMA
    transacción que la inserción del reemplazo (hallazgo 5): si el INSERT
    falla, el `ROLLBACK` deshace también el DELETE.

    `polygons`: tuplas (wkb, area, minx, miny, maxx, maxy).
    """
    with connection:
        _invalidate_block_locked(connection, block_id)
        connection.execute(
            "INSERT INTO vector_blocks (block_id, identity_hash, status, metrics_json, committed_at) "
            "VALUES (?, ?, 'done', ?, ?)",
            (block_id, identity_hash, json.dumps(metrics, ensure_ascii=False), utc_now()),
        )
        for wkb, area, minx, miny, maxx, maxy in polygons:
            cursor = connection.execute(
                "INSERT INTO vector_polygons (block_id, wkb, area, minx, miny, maxx, maxy) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (block_id, wkb, area, minx, miny, maxx, maxy),
            )
            polygon_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO vector_polygons_rtree (polygon_id, minx, maxx, miny, maxy) VALUES (?, ?, ?, ?, ?)",
                (polygon_id, minx, maxx, miny, maxy),
            )


def block_metrics_in_order(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Métricas de bloques leídas desde filas persistidas, no desde RAM acumulada."""
    return [
        json.loads(row[0])
        for row in connection.execute("SELECT metrics_json FROM vector_blocks ORDER BY block_id")
    ]


def load_uf(connection: sqlite3.Connection) -> dict[int, int]:
    return dict(connection.execute("SELECT polygon_id, parent FROM vector_uf"))


def uf_find(parent: dict[int, int], polygon_id: int) -> int:
    root = polygon_id
    while parent.get(root, root) != root:
        root = parent[root]
    return root


def uf_union(parent: dict[int, int], left: int, right: int) -> dict[int, int] | None:
    """Une dos conjuntos por el índice de raíz menor; devuelve sólo la entrada
    modificada para persistir (invariante 7: no reescribir todo el árbol)."""
    left_root, right_root = uf_find(parent, left), uf_find(parent, right)
    if left_root == right_root:
        return None
    lower, higher = (left_root, right_root) if left_root < right_root else (right_root, left_root)
    parent[higher] = lower
    return {higher: lower}


def graph_last_polygon_id(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT last_polygon_id FROM vector_graph_progress WHERE id = 1").fetchone()[0]


def fetch_polygon_page(
    connection: sqlite3.Connection, after_polygon_id: int, limit: int
) -> list[tuple[int, int, bytes, float, float, float, float]]:
    """Página de (polygon_id, block_id, wkb, minx, miny, maxx, maxy) ordenada."""
    return connection.execute(
        "SELECT polygon_id, block_id, wkb, minx, miny, maxx, maxy FROM vector_polygons "
        "WHERE polygon_id > ? ORDER BY polygon_id LIMIT ?",
        (after_polygon_id, limit),
    ).fetchall()


def iter_lower_candidates(
    connection: sqlite3.Connection,
    polygon_id: int,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    batch_size: int = 500,
) -> Iterator[list[tuple[int, int, bytes]]]:
    """Candidatos con `candidate_id < polygon_id` cuya envolvente se superpone,
    entregados página por página (por defecto tamaño `vector_graph_page_size`).

    Tanto los ids candidatos del R-tree como las filas con su WKB -- lo caro
    en memoria -- se recuperan con un cursor abierto y `fetchmany(batch_size)`
    en vez de un `fetchall()` que materialice todos los ids o un único
    `IN (...)` con todos los candidatos a la vez: en ningún momento hay más
    de una página (`batch_size` filas) viva en memoria, sin importar cuántos
    candidatos totales existan (invariante 5/hallazgo 2). El llamador debe
    consumir cada página (actualizar union-find, avanzar el cursor de
    progreso) antes de pedir la siguiente para que la generadora la libere.
    """
    cursor = connection.execute(
        "SELECT polygon_id FROM vector_polygons_rtree "
        "WHERE polygon_id < ? AND minx <= ? AND maxx >= ? AND miny <= ? AND maxy >= ?",
        (polygon_id, maxx, minx, maxy, miny),
    )
    limit = max(1, batch_size)
    while True:
        id_rows = cursor.fetchmany(limit)
        if not id_rows:
            return
        ids = [row[0] for row in id_rows]
        placeholders = ",".join("?" * len(ids))
        yield connection.execute(
            f"SELECT polygon_id, block_id, wkb FROM vector_polygons WHERE polygon_id IN ({placeholders})",
            ids,
        ).fetchall()


def commit_graph_page(
    connection: sqlite3.Connection, last_polygon_id: int, uf_updates: dict[int, int]
) -> None:
    with connection:
        for polygon_id, parent in uf_updates.items():
            connection.execute(
                "INSERT INTO vector_uf (polygon_id, parent) VALUES (?, ?) "
                "ON CONFLICT(polygon_id) DO UPDATE SET parent = excluded.parent",
                (polygon_id, parent),
            )
        connection.execute("UPDATE vector_graph_progress SET last_polygon_id = ? WHERE id = 1", (last_polygon_id,))


def component_roots(parent: dict[int, int], polygon_ids: Iterable[int]) -> dict[int, list[int]]:
    groups: dict[int, list[int]] = {}
    for polygon_id in polygon_ids:
        groups.setdefault(uf_find(parent, polygon_id), []).append(polygon_id)
    return groups


def ensure_components(connection: sqlite3.Connection, groups: dict[int, list[int]]) -> None:
    with connection:
        for root_id, members in groups.items():
            connection.execute(
                "INSERT OR IGNORE INTO vector_components (root_id, status) VALUES (?, 'pending')",
                (root_id,),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO vector_component_members (root_id, polygon_id) VALUES (?, ?)",
                [(root_id, polygon_id) for polygon_id in members],
            )


def pending_component_roots(connection: sqlite3.Connection) -> list[int]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT root_id FROM vector_components WHERE status = 'pending' ORDER BY root_id"
        )
    ]


def component_member_count(connection: sqlite3.Connection, root_id: int) -> int:
    """Cuenta los miembros de una componente sin traer sus WKB a RAM.

    Se usa para decidir si una componente supera el límite duro (hallazgo 3)
    ANTES de cargar/deserializar sus geometrías, no después."""
    return connection.execute(
        "SELECT COUNT(*) FROM vector_component_members WHERE root_id = ?", (root_id,)
    ).fetchone()[0]


def component_member_polygons(connection: sqlite3.Connection, root_id: int) -> list[tuple[int, bytes, float]]:
    return connection.execute(
        "SELECT p.polygon_id, p.wkb, p.area FROM vector_component_members m "
        "JOIN vector_polygons p ON p.polygon_id = m.polygon_id WHERE m.root_id = ? ORDER BY p.polygon_id",
        (root_id,),
    ).fetchall()


def commit_component(
    connection: sqlite3.Connection, root_id: int, parts: Sequence[tuple[bytes, float, str]]
) -> None:
    with connection:
        connection.execute("DELETE FROM vector_merged_parts WHERE root_id = ?", (root_id,))
        connection.executemany(
            "INSERT INTO vector_merged_parts (root_id, part_index, wkb, area, size_class) VALUES (?, ?, ?, ?, ?)",
            [(root_id, index, wkb, area, size_class) for index, (wkb, area, size_class) in enumerate(parts)],
        )
        connection.execute("UPDATE vector_components SET status = 'done' WHERE root_id = ?", (root_id,))


def iter_merged_parts(
    connection: sqlite3.Connection, batch_size: int = 2000
) -> Iterator[list[tuple[int, bytes, float, str]]]:
    """Pagina las partes fusionadas finales sin cargar todas a la vez (invariante 5)."""
    after = 0
    while True:
        rows = connection.execute(
            "SELECT id, wkb, area, size_class FROM vector_merged_parts WHERE id > ? ORDER BY id LIMIT ?",
            (after, batch_size),
        ).fetchall()
        if not rows:
            return
        yield rows
        after = rows[-1][0]


def write_geoparquet_atomic(vectors: gpd.GeoDataFrame, destination: Path) -> None:
    """Escribe el Parquet final a un temporal, valida esquema/recuento y
    reemplaza atómicamente (invariante 8).

    La validación lee sólo el pie de página del Parquet (`pyarrow.parquet
    .ParquetFile`, metadata + esquema) en vez de volver a cargar todas las
    filas con `gpd.read_parquet`: el GeoDataFrame recién escrito ya vive
    completo en RAM en el llamador, así que una segunda lectura íntegra
    duplicaría ese pico de memoria sin aportar nada que el pie de página no
    certifique igual de bien (hallazgo 2). Tras el `os.replace` también se
    sincroniza el directorio contenedor, no sólo el archivo, para que el
    reemplazo sobreviva una pérdida de energía justo después del rename
    (hallazgo 6)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    vectors.to_parquet(temporary)
    try:
        parquet_file = pq.ParquetFile(temporary)
        row_count = parquet_file.metadata.num_rows
        columns = list(parquet_file.schema_arrow.names)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Parquet temporal ilegible tras escribirlo: {error}") from error
    if row_count != len(vectors) or columns != list(vectors.columns):
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"Parquet temporal corrupto: se escribieron {len(vectors)} filas/"
            f"{list(vectors.columns)} columnas pero el pie de página reporta {row_count}/{columns}"
        )
    with open(temporary, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    try:
        directory_fd = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        print(
            f"    [AVISO] No se pudo sincronizar el directorio {destination.parent} "
            f"tras publicar {destination.name}: {error}",
            flush=True,
        )
