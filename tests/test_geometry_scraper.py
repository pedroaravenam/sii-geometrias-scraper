from __future__ import annotations

import hashlib
import unittest
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from shapely import wkb as shapely_wkb
from shapely.geometry import Point, box

from sii_geometry.catalog import Commune, ensure_reference_files, load_boundary, load_catalog, normalize_name
from sii_geometry.cli import build_parser, resolve_reference_csv
from sii_geometry.config import load_settings
from sii_geometry import geometry
from sii_geometry.geometry import (
    _block_origins,
    _run_block_stage,
    _run_components_stage,
    _run_graph_stage,
    compute_run_identity,
    download_supercell,
    merge_component_adaptive,
    merge_overlapping_polygons,
    vectorize_image,
    vectorize_supercells,
)
from sii_geometry.matching import match_roles_to_polygons
from sii_geometry.pipeline import _archive_before_force, _download_planned_supercells, _paths, summarize_final_output
from sii_geometry.records import extract_role_keys
from sii_geometry.reference_assets import _download_asset, file_sha256, validate_reference_asset
from sii_geometry.state import (
    checkpoint_database,
    commit_block,
    commit_component,
    iter_lower_candidates,
    iter_merged_parts,
    open_vector_checkpoint,
    read_manifest,
    save_api_result,
    set_vector_stage,
    vector_stage,
    write_geoparquet_atomic,
    write_json_atomic,
)
from sii_geometry.storage import publish_commune, save_storage_configuration


TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp"


class GeometryScraperTests(unittest.TestCase):
    def test_cli_accepts_rematch_without_force(self):
        args = build_parser().parse_args(["scrape", "--comuna", "5101", "--rematch"])
        self.assertTrue(args.rematch)
        self.assertFalse(args.force)

    def test_wms_download_defers_failure_and_recovers_after_scanning(self):
        root = TEST_TMP_ROOT / "unit_wms_recovery"
        manifest_path = root / "manifest.json"
        paths = {"tiles": root / "tiles", "manifest": manifest_path}
        settings = load_settings()
        commune = Commune("2201", "Antofagasta", "Antofagasta")
        cells = [(0, 0), (4, 0), (8, 0)]
        calls: list[tuple[int, int]] = []
        failed_once = False

        def fake_download(_client, _code, _layer, sc_x, sc_y, _destination, _settings, *, force):
            nonlocal failed_once
            del force
            calls.append((sc_x, sc_y))
            if (sc_x, sc_y) == (4, 0) and not failed_once:
                failed_once = True
                raise RuntimeError("500 temporal")

        manifest: dict[str, object] = {}
        with patch("sii_geometry.pipeline.download_supercell", side_effect=fake_download):
            _download_planned_supercells(
                object(),
                commune,
                "sii:test",
                cells,
                paths,
                settings,
                manifest,
                force=False,
            )

        self.assertEqual(calls, [(0, 0), (4, 0), (8, 0), (4, 0)])
        stored = read_manifest(manifest_path)
        self.assertEqual(stored["supercells_downloaded"], 3)
        self.assertEqual(stored["wms_supercells_scanned"], 3)
        self.assertEqual(stored["wms_pending_supercells"], [])

    def test_normalize_name_removes_accents(self):
        self.assertEqual(normalize_name("  Peñaflor "), "PENAFLOR")

    def test_bundled_catalog_and_boundaries_do_not_use_network(self):
        class NetworkMustNotBeUsed:
            def get(self, *_args, **_kwargs):
                raise AssertionError("Los recursos incluidos no deben usar la red")

        settings = load_settings()
        catalog_path, boundaries_path = ensure_reference_files(settings, NetworkMustNotBeUsed())
        self.assertTrue(catalog_path.is_file())
        self.assertTrue(boundaries_path.is_file())

        catalog = load_catalog(settings, NetworkMustNotBeUsed())
        penaflor = next(commune for commune in catalog if commune.sii_code == "14504")
        boundary = load_boundary(penaflor, settings, NetworkMustNotBeUsed())

        self.assertGreaterEqual(len(catalog), 346)
        self.assertFalse(boundary.is_empty)

    def test_vectorizer_separates_cyan_parcels(self):
        settings = load_settings()
        image = np.zeros((20, 20, 4), dtype=np.uint8)
        image[2:18, 2:9] = [182, 221, 232, 179]
        image[2:18, 11:18] = [182, 221, 232, 179]
        polygons, metrics = vectorize_image(image, (0, 0, 20, 20), settings)
        self.assertEqual(len(polygons), 2)
        self.assertGreater(metrics["fill_pixels"], 0)

    def test_blocks_do_not_bridge_distant_islands(self):
        settings = load_settings()
        step = settings.supercell_tiles
        distant_x = step * 100_000
        cells = [(0, 0), (step, 0), (distant_x, 0), (distant_x + step, 0)]

        blocks = list(_block_origins(cells, settings))

        self.assertEqual(len(blocks), 2)
        for origin_x, origin_y, members, _ in blocks:
            width = ((max(x for x, _ in members) - origin_x) // step) + 1
            height = ((max(y for _, y in members) - origin_y) // step) + 1
            self.assertLessEqual(width, settings.block_supercells)
            self.assertLessEqual(height, settings.block_supercells)

    def test_match_uses_point_in_polygon_and_nearest(self):
        polygons = gpd.GeoDataFrame(
            {"_poly_idx": [0, 1], "pol_area_m2": [100, 100]},
            geometry=[box(-70.001, -33.001, -70.000, -33.000), box(-69.999, -33.001, -69.998, -33.000)],
            crs=4326,
        )
        roles = pd.DataFrame(
            {
                "comuna": [14504, 14504],
                "manzana": ["1", "2"],
                "predio": ["1", "1"],
                "rol": ["1-1", "2-1"],
                "lat": [-33.0005, -33.0005],
                "lon": [-70.0005, -69.99795],
                "direccion_sii": ["A", "B"],
            }
        )
        matched, used, metrics = match_roles_to_polygons(roles, polygons, max_distance_m=10)
        self.assertEqual(metrics["point_in_polygon"], 1)
        self.assertEqual(metrics["nearest_10m"], 1)
        self.assertEqual(len(used), 2)
        self.assertTrue(matched.geometry.notna().all())

    def test_match_supports_chilean_insular_longitudes(self):
        polygons = gpd.GeoDataFrame(
            {"_poly_idx": [0], "pol_area_m2": [100]},
            geometry=[box(-109.441, -27.164, -109.439, -27.162)],
            crs=4326,
        )
        roles = pd.DataFrame(
            {
                "rol": ["101-1"],
                "manzana": ["101"],
                "predio": ["1"],
                "lat": [-27.162712],
                "lon": [-109.440281],
                "direccion_sii": ["RAPA NUI"],
            }
        )

        matched, used, metrics = match_roles_to_polygons(roles, polygons)

        self.assertEqual(metrics["point_in_polygon"], 1)
        self.assertEqual(metrics["without_geometry"], 0)
        self.assertEqual(used, {0})
        self.assertTrue(matched.geometry.notna().all())

    def test_unmatched_roles_are_classified_by_current_sii_status(self):
        polygons = gpd.GeoDataFrame(
            {"_poly_idx": [0], "pol_area_m2": [100]},
            geometry=[box(-109.441, -27.164, -109.439, -27.162)],
            crs=4326,
        )
        roles = pd.DataFrame(
            {
                "rol": ["1-1", "2-1", "3-1", "4-1", "5-1"],
                "manzana": ["1", "2", "3", "4", "5"],
                "predio": ["1"] * 5,
                "lat": [-27.163, None, None, None, -27.2],
                "lon": [-109.440, None, None, None, -109.5],
                "direccion_sii": [None] * 5,
                "_status": ["ok", "ok", "not_found", "error", "ok"],
            }
        )

        matched, _, metrics = match_roles_to_polygons(roles, polygons)
        quality = matched.set_index("rol")["calidad_geom"].to_dict()

        self.assertEqual(quality["2-1"], "vigente_sin_visualizacion_sii")
        self.assertEqual(quality["3-1"], "rol_no_encontrado_periodo_actual")
        self.assertEqual(quality["4-1"], "consulta_sii_error")
        self.assertEqual(quality["5-1"], "vigente_con_coordenada_sin_geometria")
        self.assertEqual(metrics["current_without_published_geometry"], 1)
        self.assertEqual(metrics["not_found_current_period"], 1)
        self.assertEqual(metrics["api_error_without_geometry"], 1)
        self.assertEqual(metrics["current_with_coordinates_unmatched"], 1)

    def test_final_summary_counts_all_polygons_and_unique_roles(self):
        output = gpd.GeoDataFrame(
            {
                "rol": ["1-1", "1-1", "2-1", None, "3-1"],
                "_poly_idx": [10, 10, 20, 30, None],
            },
            geometry=[box(0, 0, 1, 1), box(2, 0, 3, 1), box(4, 0, 5, 1), box(6, 0, 7, 1), None],
            crs=4326,
        )

        summary = summarize_final_output(output)

        self.assertEqual(summary["rows_total"], 5)
        self.assertEqual(summary["rows_with_geometry"], 4)
        self.assertEqual(summary["polygons_total"], 3)
        self.assertEqual(summary["polygons_with_role"], 2)
        self.assertEqual(summary["polygons_without_role"], 1)
        self.assertAlmostEqual(summary["polygon_attribution_pct"], 66.667, places=3)
        self.assertEqual(summary["unique_roles_total"], 3)
        self.assertEqual(summary["unique_roles_with_geometry"], 2)
        self.assertEqual(summary["unique_roles_without_geometry"], 1)

    def test_overlap_merge_preserves_touching_parcels(self):
        left = box(0, 0, 10, 10)
        duplicate = box(5, 0, 10, 10)
        touching = box(10, 0, 20, 10)
        merged = merge_overlapping_polygons([left, duplicate, touching], sources=[0, 1, 0])
        self.assertEqual(len(merged), 2)
        self.assertEqual(sorted(round(item.area) for item in merged), [100, 100])

    def test_checkpoint_and_manifest_roundtrip(self):
        root = TEST_TMP_ROOT / "unit_state"
        root.mkdir(parents=True, exist_ok=True)
        manifest_path = root / "manifest.json"
        database_path = root / "state.sqlite"
        if database_path.exists():
            database_path.unlink()
        write_json_atomic(manifest_path, {"status": "parcial"})
        self.assertEqual(read_manifest(manifest_path)["status"], "parcial")
        with checkpoint_database(database_path) as connection:
            save_api_result(
                connection,
                query_key="role:14504:1:1",
                source="role",
                commune="14504",
                manzana="1",
                predio="1",
                polygon_id=None,
                status="ok",
                attempts=1,
                point_lon=None,
                point_lat=None,
                response={"rol": "1-1"},
                error=None,
            )
            count = connection.execute("SELECT COUNT(*) FROM api_results").fetchone()[0]
            self.assertEqual(count, 1)

    def test_local_paths_can_be_configured_outside_repository(self):
        root = TEST_TMP_ROOT / "unit_configuration"
        root.mkdir(parents=True, exist_ok=True)
        config_path = root / "sii_geometry.json"
        local_path = root / "sii_geometry.local.json"
        reference_csv = root / "microdatos" / "catastro_2026_1.csv"
        storage_root = root / "resultados_locales"
        reference_csv.parent.mkdir(parents=True, exist_ok=True)
        reference_csv.write_text("comuna,manzana,predio\n", encoding="utf-8")
        config_path.write_text(
            (Path(__file__).resolve().parents[1] / "config" / "sii_geometry.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        if local_path.exists():
            local_path.unlink()

        save_storage_configuration(config_path, storage_root)
        selected = resolve_reference_csv(config_path, reference_csv)
        settings = load_settings(config_path)

        self.assertEqual(selected, reference_csv.resolve())
        self.assertEqual(settings.storage_root, storage_root.resolve())
        self.assertEqual(settings.reference_csv, reference_csv.resolve())

    def test_extract_role_keys_from_regional_parquet(self):
        root = TEST_TMP_ROOT / "unit_regional_reference"
        root.mkdir(parents=True, exist_ok=True)
        reference = root / "catastro_2026S1_test.parquet"
        pd.DataFrame(
            {
                "comuna": [14504, 14504, 5302],
                "manzana": ["001", "002", "003"],
                "predio": ["0001", "0002", "0003"],
                "rc_avaluo_total": [10, 20, 30],
            }
        ).to_parquet(reference, index=False)

        roles = extract_role_keys(reference, "14504")

        self.assertEqual(roles["rol"].tolist(), ["1-1", "2-2"])

    def test_regional_asset_validation_checks_hash_schema_and_commune(self):
        root = TEST_TMP_ROOT / "unit_asset_validation"
        root.mkdir(parents=True, exist_ok=True)
        reference = root / "region.parquet"
        pd.DataFrame({"comuna": [14504], "manzana": [1], "predio": [2]}).to_parquet(reference, index=False)
        parquet = pq.ParquetFile(reference)
        asset = {
            "bytes": reference.stat().st_size,
            "rows": 1,
            "sha256": file_sha256(reference),
        }

        validate_reference_asset(reference, asset, parquet.schema.names, ["14504"])
        with self.assertRaises(ValueError):
            validate_reference_asset(reference, {**asset, "sha256": "0" * 64}, parquet.schema.names)

    def test_regional_asset_download_uses_fallback_and_verifies_hash(self):
        root = TEST_TMP_ROOT / "unit_asset_download"
        root.mkdir(parents=True, exist_ok=True)
        destination = root / "region.parquet"
        payload = b"PAR1-valid-test-payload"

        class FakeResponse:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                del chunk_size
                yield self.body

        class FakeSession:
            def __init__(self):
                self.calls = []

            def get(self, url, **_kwargs):
                self.calls.append(url)
                return FakeResponse(b"bad" if len(self.calls) == 1 else payload)

        session = FakeSession()
        asset = {
            "region": "Prueba",
            "file": destination.name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "urls": ["https://drive.example/primary", "https://drive-backup.example/fallback"],
        }

        _download_asset(session, asset, destination)

        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(session.calls, asset["urls"])
        self.assertFalse(destination.with_suffix(".parquet.part").exists())

    def test_publish_commune_creates_central_package_and_catalog(self):
        root = TEST_TMP_ROOT / "unit_storage"
        raw = root / "local" / "raw" / "2026S2" / "14504_penaflor"
        processed = root / "local" / "processed" / "2026S2"
        central = root / "central"
        for path in [raw / "tiles", raw / "checkpoints", processed]:
            path.mkdir(parents=True, exist_ok=True)
        output = processed / "14504_penaflor.parquet"
        metrics = processed / "14504_penaflor_metrics.json"
        raw_api = raw / "respuestas_api.jsonl"
        output.write_bytes(b"fake-geoparquet")
        raw_api.write_text('{"response": {"periodo": "SEGUNDO SEMESTRE DE 2026"}}\n', encoding="utf-8")
        (raw / "tiles" / "sc_1_1.png").write_bytes(b"fake-png")
        metrics.write_text(json.dumps({"output_rows": 1, "captured_at": "2026-09-14T00:00:00Z"}), encoding="utf-8")
        reference = root / "local" / "data" / "raw" / "geometrias" / "_reference" / "comunas_sii.json"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text(
            json.dumps(
                [
                    {"id": "14504", "nombre": "Peñaflor", "region": "Metropolitana"},
                    {"id": "14505", "nombre": "Padre Hurtado", "region": "Metropolitana"},
                ]
            ),
            encoding="utf-8",
        )
        database = raw / "checkpoints" / "state.sqlite"
        with checkpoint_database(database) as connection:
            save_api_result(
                connection,
                query_key="role:14504:1:1",
                source="role",
                commune="14504",
                manzana="1",
                predio="1",
                polygon_id=None,
                status="ok",
                attempts=1,
                point_lon=None,
                point_lat=None,
                response={"rol": "1-1"},
                error=None,
            )
        manifest_path = raw / "manifest.json"
        write_json_atomic(
            manifest_path,
            {
                "status": "completa",
                "commune": {"sii_code": "14504", "name": "Peñaflor", "region": "Metropolitana"},
                "periodo_geometria": "2026S2",
                "historical_role_candidates": 1,
                "vectorization": {"polygons": 1},
                "output": str(output),
                "metrics": str(metrics),
            },
        )
        settings = replace(
            load_settings(),
            repository_root=root / "local",
            storage_root=central,
            archive_wms_tiles=True,
        )
        result = publish_commune(settings, manifest_path)
        self.assertTrue(result["geoparquet"].exists())
        self.assertTrue((central / "2026S2" / "respuestas_api" / "14504_penaflor.jsonl.gz").exists())
        self.assertTrue((central / "2026S2" / "checkpoints" / "14504_penaflor.sqlite").exists())
        self.assertTrue((central / "2026S2" / "wms_archivados" / "14504_penaflor_tiles.zip").exists())
        self.assertTrue(result["catalog"].exists())
        catalog = pd.read_csv(result["catalog"], dtype={"codigo_comuna": "string"})
        self.assertEqual(len(catalog), 3)
        self.assertEqual(
            catalog.set_index("codigo_comuna").loc["14505", "status"],
            "pendiente",
        )
        with sqlite3.connect(central / "2026S2" / "checkpoints" / "14504_penaflor.sqlite") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM api_results").fetchone()[0], 1)
        connection.close()

        output.write_bytes(b"updated-geoparquet")
        publish_commune(settings, manifest_path)
        self.assertEqual(result["geoparquet"].read_bytes(), b"updated-geoparquet")

    # --- Fase 5: checkpoints de vectorización reanudables -----------------

    @staticmethod
    def _fresh_checkpoint(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                candidate.unlink()
        return path

    @classmethod
    def _stage_raw_polygons(cls, checkpoint_path: Path, polygons: list, sources: list[int]) -> None:
        """Confirma cada polígono crudo como si viniera de su propio bloque
        `source` y corre grafo+componentes, sin depender del renderizado PNG,
        para comparar directamente contra `merge_overlapping_polygons`."""
        cls._fresh_checkpoint(checkpoint_path)
        settings = load_settings()
        with open_vector_checkpoint(checkpoint_path, "test-run", "test-identity") as connection:
            by_source: dict[int, list] = {}
            for polygon, source in zip(polygons, sources):
                by_source.setdefault(source, []).append(polygon)
            for block_id in sorted(by_source):
                block_polys = by_source[block_id]
                polygons_data = [
                    (shapely_wkb.dumps(polygon), polygon.area, *polygon.bounds) for polygon in block_polys
                ]
                commit_block(connection, block_id, f"hash-{block_id}", {"polygons": len(block_polys)}, polygons_data)
            set_vector_stage(connection, "graph")
            _run_graph_stage(connection, settings)
            _run_components_stage(connection, settings)

    @staticmethod
    def _read_merged_parts(checkpoint_path: Path) -> list:
        parts = []
        with sqlite3.connect(checkpoint_path) as connection:
            for rows in iter_merged_parts(connection):
                for _id, wkb_bytes, _area, _size_class in rows:
                    parts.append(shapely_wkb.loads(wkb_bytes))
        return parts

    def test_graph_and_components_match_reference_merge_criterion(self):
        """Duplicado entre bloques, toque de área cero, mismo bloque y cadena
        transitiva: el checkpoint debe reproducir exactamente el resultado de
        `merge_overlapping_polygons` (invariante 4)."""
        cross_source_dup_a = box(0, 0, 10, 10)
        cross_source_dup_b = box(0, 0, 10, 10)
        zero_area_touch_left = box(20, 0, 30, 10)
        zero_area_touch_right = box(30, 0, 40, 10)
        same_source_overlap_a = box(50, 0, 60, 10)
        same_source_overlap_b = box(55, 0, 65, 10)
        chain_a = box(100, 0, 110, 10)
        chain_b = box(105, 0, 115, 10)
        chain_c = box(112, 0, 122, 10)
        polygons = [
            cross_source_dup_a, cross_source_dup_b,
            zero_area_touch_left, zero_area_touch_right,
            same_source_overlap_a, same_source_overlap_b,
            chain_a, chain_b, chain_c,
        ]
        sources = [0, 1, 2, 3, 4, 4, 5, 6, 7]
        reference = merge_overlapping_polygons(list(polygons), list(sources))

        checkpoint_path = TEST_TMP_ROOT / "unit_vector_checkpoint" / "graph_reference.sqlite"
        self._stage_raw_polygons(checkpoint_path, polygons, sources)
        result = self._read_merged_parts(checkpoint_path)

        self.assertEqual(len(result), len(reference))
        self.assertAlmostEqual(
            sum(p.area for p in result), sum(p.area for p in reference), places=6
        )
        from shapely import union_all
        self.assertAlmostEqual(
            union_all(result).symmetric_difference(union_all(reference)).area, 0.0, places=6
        )

    def test_components_split_back_into_multipolygon_parts(self):
        """Una componente unida que resulta en piezas separadas se persiste
        como varias partes finales, igual que `merge_overlapping_polygons`."""
        island_a = box(0, 0, 10, 10)
        island_a_dup = box(0, 0, 10, 10)
        island_b = box(1000, 0, 1010, 10)
        island_b_dup = box(1000, 0, 1010, 10)
        polygons = [island_a, island_a_dup, island_b, island_b_dup]
        sources = [0, 1, 0, 1]
        reference = merge_overlapping_polygons(list(polygons), list(sources))

        checkpoint_path = TEST_TMP_ROOT / "unit_vector_checkpoint" / "multipolygon.sqlite"
        self._stage_raw_polygons(checkpoint_path, polygons, sources)
        result = self._read_merged_parts(checkpoint_path)

        self.assertEqual(len(result), 2)
        self.assertEqual(len(reference), 2)
        self.assertEqual(sorted(round(p.area) for p in result), sorted(round(p.area) for p in reference))

    def test_merge_component_adaptive_matches_direct_union(self):
        """La fusión por teselas de componentes grandes (invariante 6) debe
        producir exactamente el mismo resultado que `union_all` directo."""
        from shapely import union_all

        chain = [Point(index * 0.5, 0).buffer(1.0) for index in range(37)]
        direct = [
            part for part in (
                union_all(chain).geoms if hasattr(union_all(chain), "geoms") else [union_all(chain)]
            )
        ]
        adaptive = merge_component_adaptive(chain, tile_size=5)
        self.assertEqual(len(direct), len(adaptive))
        self.assertAlmostEqual(
            union_all(direct).symmetric_difference(union_all(adaptive)).area, 0.0, places=6
        )
        with self.assertRaises(ValueError):
            merge_component_adaptive(chain, tile_size=1)

    def _write_flat_tile(self, path: Path, pixels: int, box_px: tuple[int, int, int, int], fill: tuple[int, int, int]) -> None:
        image = np.zeros((pixels, pixels, 4), dtype=np.uint8)
        left, top, right, bottom = box_px
        image[top:bottom, left:right] = (*fill, 255)
        from PIL import Image
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(image, "RGBA").save(path)

    def _checkpoint_settings(self):
        return replace(
            load_settings(),
            block_supercells=2,
            block_overlap_supercells=1,
            supercell_tiles=1,
            urban_min_area_m2=1,
        )

    def test_resume_after_png_change_reprocesses_only_affected_blocks(self):
        settings = self._checkpoint_settings()
        root = TEST_TMP_ROOT / "unit_vector_resume_png"
        tiles_dir = root / "tiles"
        checkpoint_path = root / "checkpoints" / "vectors.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        pixels = settings.supercell_tiles * 256
        fill = tuple(settings.fill_color)
        cells = [(0, 0), (1, 0), (2, 0)]
        for x, y in cells:
            self._write_flat_tile(tiles_dir / f"sc_{x}_{y}.png", pixels, (50, 50, 200, 200), fill)

        vectors_first, metrics_first = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9001", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(vectors_first), 3)

        self._write_flat_tile(tiles_dir / "sc_1_0.png", pixels, (50, 50, 220, 220), fill)
        vectors_second, metrics_second = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9001", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(metrics_second), len(metrics_first))
        areas = sorted(round(a, 1) for a in vectors_second["pol_area_m2"].tolist())
        unchanged = [a for a in areas if a == areas[0]]
        self.assertEqual(len(unchanged), 2)
        self.assertGreater(max(areas), areas[0])

    def test_resume_is_a_no_op_when_nothing_changed(self):
        settings = self._checkpoint_settings()
        root = TEST_TMP_ROOT / "unit_vector_resume_noop"
        tiles_dir = root / "tiles"
        checkpoint_path = root / "checkpoints" / "vectors.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        pixels = settings.supercell_tiles * 256
        fill = tuple(settings.fill_color)
        cells = [(0, 0), (1, 0)]
        for x, y in cells:
            self._write_flat_tile(tiles_dir / f"sc_{x}_{y}.png", pixels, (50, 50, 200, 200), fill)

        vectors_first, _ = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9002", layer="sii:test", period="2026S2",
        )
        vectors_second, _ = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9002", layer="sii:test", period="2026S2",
        )
        self.assertEqual(
            sorted(round(a, 3) for a in vectors_first["pol_area_m2"].tolist()),
            sorted(round(a, 3) for a in vectors_second["pol_area_m2"].tolist()),
        )

    def test_crash_before_block_commit_resumes_from_next_block(self):
        """Simula un corte justo antes de confirmar un bloque: el checkpoint
        conserva sólo bloques ya confirmados y la reanudación completa el resto
        sin duplicar geometrías (invariantes 1 y 7)."""
        settings = self._checkpoint_settings()
        root = TEST_TMP_ROOT / "unit_vector_crash_block"
        tiles_dir = root / "tiles"
        checkpoint_path = root / "checkpoints" / "vectors.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        pixels = settings.supercell_tiles * 256
        fill = tuple(settings.fill_color)
        cells = [(0, 0), (1, 0), (2, 0)]
        for x, y in cells:
            self._write_flat_tile(tiles_dir / f"sc_{x}_{y}.png", pixels, (50, 50, 200, 200), fill)

        class Boom(Exception):
            pass

        blocks = list(_block_origins(cells, settings))
        run_id, identity_hash = compute_run_identity("9003", "sii:test", "2026S2", settings, cells)
        call_count = {"n": 0}
        original_commit_block = commit_block

        def flaky_commit_block(connection, block_id, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise Boom("corte simulado antes de confirmar el segundo bloque")
            return original_commit_block(connection, block_id, *args, **kwargs)

        with self.assertRaises(Boom):
            with patch("sii_geometry.geometry.commit_block", side_effect=flaky_commit_block):
                with open_vector_checkpoint(checkpoint_path, run_id, identity_hash) as connection:
                    _run_block_stage(connection, tiles_dir, blocks, settings)

        with sqlite3.connect(checkpoint_path) as connection:
            committed = connection.execute("SELECT COUNT(*) FROM vector_blocks").fetchone()[0]
        self.assertEqual(committed, 1)

        vectors, block_metrics = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9003", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(block_metrics), len(blocks))
        self.assertEqual(len(vectors), len(cells))

    def test_crash_mid_graph_page_resumes_without_reprocessing_blocks(self):
        settings = self._checkpoint_settings()
        root = TEST_TMP_ROOT / "unit_vector_crash_graph"
        tiles_dir = root / "tiles"
        checkpoint_path = root / "checkpoints" / "vectors.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        pixels = settings.supercell_tiles * 256
        fill = tuple(settings.fill_color)
        cells = [(0, 0), (1, 0), (2, 0)]
        for x, y in cells:
            self._write_flat_tile(tiles_dir / f"sc_{x}_{y}.png", pixels, (50, 50, 200, 200), fill)

        blocks = list(_block_origins(cells, settings))
        run_id, identity_hash = compute_run_identity("9004", "sii:test", "2026S2", settings, cells)

        class Boom(Exception):
            pass

        with open_vector_checkpoint(checkpoint_path, run_id, identity_hash) as connection:
            _run_block_stage(connection, tiles_dir, blocks, settings)

        settings_one_page = replace(settings, vector_graph_page_size=1)
        with self.assertRaises(Boom):
            with patch("sii_geometry.geometry.commit_graph_page", side_effect=Boom("corte a mitad de página")):
                with open_vector_checkpoint(checkpoint_path, run_id, identity_hash) as connection:
                    _run_graph_stage(connection, settings_one_page)

        with sqlite3.connect(checkpoint_path) as connection:
            stage = connection.execute("SELECT stage FROM vector_run").fetchone()[0]
            last_polygon = connection.execute("SELECT last_polygon_id FROM vector_graph_progress").fetchone()[0]
        self.assertEqual(stage, "graph")
        self.assertEqual(last_polygon, -1)

        vectors, _ = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9004", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(vectors), len(cells))

    def test_crash_during_component_union_resumes_remaining_components(self):
        settings = self._checkpoint_settings()
        root = TEST_TMP_ROOT / "unit_vector_crash_components"
        tiles_dir = root / "tiles"
        checkpoint_path = root / "checkpoints" / "vectors.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        pixels = settings.supercell_tiles * 256
        fill = tuple(settings.fill_color)
        cells = [(0, 0), (1, 0), (2, 0)]
        for x, y in cells:
            self._write_flat_tile(tiles_dir / f"sc_{x}_{y}.png", pixels, (50, 50, 200, 200), fill)

        blocks = list(_block_origins(cells, settings))
        run_id, identity_hash = compute_run_identity("9005", "sii:test", "2026S2", settings, cells)

        class Boom(Exception):
            pass

        with open_vector_checkpoint(checkpoint_path, run_id, identity_hash) as connection:
            _run_block_stage(connection, tiles_dir, blocks, settings)
            _run_graph_stage(connection, settings)

        call_count = {"n": 0}
        original_commit_component = commit_component

        def flaky_commit_component(connection, root_id, parts):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise Boom("corte a mitad de la fusión de componentes")
            return original_commit_component(connection, root_id, parts)

        with self.assertRaises(Boom):
            with patch("sii_geometry.geometry.commit_component", side_effect=flaky_commit_component):
                with open_vector_checkpoint(checkpoint_path, run_id, identity_hash) as connection:
                    _run_components_stage(connection, settings)

        with sqlite3.connect(checkpoint_path) as connection:
            done_components = connection.execute(
                "SELECT COUNT(*) FROM vector_components WHERE status = 'done'"
            ).fetchone()[0]
        self.assertEqual(done_components, 1)

        vectors, _ = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9005", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(vectors), len(cells))

    def test_config_change_invalidates_and_recomputes_checkpoint(self):
        settings = self._checkpoint_settings()
        root = TEST_TMP_ROOT / "unit_vector_config_change"
        tiles_dir = root / "tiles"
        checkpoint_path = root / "checkpoints" / "vectors.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        pixels = settings.supercell_tiles * 256
        fill = tuple(settings.fill_color)
        cells = [(0, 0), (1, 0)]
        for x, y in cells:
            self._write_flat_tile(tiles_dir / f"sc_{x}_{y}.png", pixels, (50, 50, 60, 60), fill)

        vectors_first, _ = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9006", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(vectors_first), 2)

        stricter = replace(settings, urban_min_area_m2=1e12)
        vectors_second, _ = vectorize_supercells(
            tiles_dir, cells, settings=stricter, checkpoint_path=checkpoint_path,
            commune_code="9006", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(vectors_second), 0)

    def test_selection_change_never_reuses_partial_checkpoint(self):
        settings = self._checkpoint_settings()
        root = TEST_TMP_ROOT / "unit_vector_selection_change"
        tiles_dir = root / "tiles"
        checkpoint_path = root / "checkpoints" / "vectors.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        pixels = settings.supercell_tiles * 256
        fill = tuple(settings.fill_color)
        cells = [(0, 0), (1, 0), (2, 0)]
        for x, y in cells:
            self._write_flat_tile(tiles_dir / f"sc_{x}_{y}.png", pixels, (50, 50, 200, 200), fill)

        partial_vectors, partial_metrics = vectorize_supercells(
            tiles_dir, cells[:1], settings, checkpoint_path,
            commune_code="9007", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(partial_metrics), 1)

        full_vectors, full_metrics = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9007", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(full_metrics), len(list(_block_origins(cells, settings))))
        self.assertEqual(len(full_vectors), len(cells))

    def test_force_archives_and_resets_vector_checkpoint(self):
        settings = replace(load_settings(), repository_root=TEST_TMP_ROOT / "unit_force_repo")
        commune = Commune("9008", "Comuna Prueba", "Region Prueba")
        paths = _paths(settings, commune)
        if paths["raw"].exists():
            shutil.rmtree(paths["raw"])
        paths["raw"].mkdir(parents=True, exist_ok=True)
        paths["vector_checkpoint"].parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(paths["vector_checkpoint"]) as connection:
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.execute("INSERT INTO marker VALUES ('previo')")
        stale_temp = paths["vectors"].with_suffix(".parquet.part")
        stale_temp.parent.mkdir(parents=True, exist_ok=True)
        stale_temp.write_bytes(b"basura-truncada")

        _archive_before_force(paths)

        self.assertFalse(paths["vector_checkpoint"].exists())
        self.assertFalse(stale_temp.exists())
        backups = list((paths["raw"] / "backups").glob("*/vectors.sqlite"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as connection:
            self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "previo")

    def test_empty_block_is_committed_without_polygons(self):
        settings = self._checkpoint_settings()
        root = TEST_TMP_ROOT / "unit_vector_empty_block"
        tiles_dir = root / "tiles"
        checkpoint_path = root / "checkpoints" / "vectors.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        pixels = settings.supercell_tiles * 256
        cells = [(0, 0)]
        blank = np.zeros((pixels, pixels, 4), dtype=np.uint8)
        from PIL import Image
        tiles_dir.mkdir(parents=True, exist_ok=True)
        Image.fromarray(blank, "RGBA").save(tiles_dir / "sc_0_0.png")

        vectors, block_metrics = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9009", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(block_metrics), 1)
        self.assertTrue(vectors.empty)

    def test_write_geoparquet_atomic_discards_truncated_temp_file(self):
        destination = TEST_TMP_ROOT / "unit_vector_atomic" / "vectors.parquet"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".parquet.part")
        temporary.write_bytes(b"esto-no-es-un-parquet-valido")

        vectors = gpd.GeoDataFrame(
            {"_poly_idx": [0], "pol_area_m2": [123.0], "pol_size_class": ["parcel_scale"]},
            geometry=[box(0, 0, 1, 1)],
            crs=4326,
        )
        write_geoparquet_atomic(vectors, destination)

        self.assertTrue(destination.exists())
        self.assertFalse(temporary.exists())
        read_back = gpd.read_parquet(destination)
        self.assertEqual(len(read_back), 1)
        self.assertEqual(read_back.iloc[0]["pol_size_class"], "parcel_scale")

    def test_missing_block_record_invalidates_downstream_components(self):
        """Reproduce el hallazgo 1: si las filas de un bloque desaparecen
        (`vector_blocks`/`vector_polygons`/`vector_polygons_rtree`, p. ej. por
        corrupción o intervención externa) mientras el resto del checkpoint
        sigue en `vectors`, `commit_block` debe invalidar incondicionalmente
        el grafo/componentes/salida final aguas abajo -- no sólo cuando el
        bloque ya existía -- para que el polígono reconstruido no quede
        huérfano fuera del Parquet final."""
        settings = self._checkpoint_settings()
        root = TEST_TMP_ROOT / "unit_vector_missing_block"
        tiles_dir = root / "tiles"
        checkpoint_path = root / "checkpoints" / "vectors.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        pixels = settings.supercell_tiles * 256
        fill = tuple(settings.fill_color)
        cells = [(0, 0), (1, 0), (2, 0)]
        for x, y in cells:
            self._write_flat_tile(tiles_dir / f"sc_{x}_{y}.png", pixels, (50, 50, 200, 200), fill)

        vectors_first, metrics_first = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9013", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(vectors_first), 3)
        self.assertEqual(len(metrics_first), 3)

        # Simula la pérdida/corrupción externa de un bloque completo: se
        # borran sus filas de las tres tablas involucradas (bloque, polígonos
        # y su índice R-tree) sin que la etapa retroceda por sí sola.
        with sqlite3.connect(checkpoint_path) as connection:
            connection.execute(
                "DELETE FROM vector_polygons_rtree WHERE polygon_id IN "
                "(SELECT polygon_id FROM vector_polygons WHERE block_id = 1)"
            )
            connection.execute("DELETE FROM vector_polygons WHERE block_id = 1")
            connection.execute("DELETE FROM vector_blocks WHERE block_id = 1")
        self._write_flat_tile(tiles_dir / "sc_1_0.png", pixels, (50, 50, 220, 220), fill)

        vectors_second, metrics_second = vectorize_supercells(
            tiles_dir, cells, settings, checkpoint_path,
            commune_code="9013", layer="sii:test", period="2026S2",
        )
        self.assertEqual(len(metrics_second), 3)
        # Los 3 polígonos deben estar presentes en la salida final -- el
        # bloque reconstruido no puede quedar fuera del grafo/componentes.
        self.assertEqual(len(vectors_second), 3)
        areas = sorted(round(a, 1) for a in vectors_second["pol_area_m2"].tolist())
        unchanged = [a for a in areas if a == areas[0]]
        self.assertEqual(len(unchanged), 2)
        self.assertGreater(max(areas), areas[0])

    def test_commit_block_invalidates_downstream_even_without_prior_row(self):
        """Hallazgo 1 (aislado a nivel de `state.py`): `commit_block` debe
        invalidar el grafo/componentes/Parquet aguas abajo SIEMPRE, incluso
        cuando `block_id` no tenía fila previa en `vector_blocks` -- p. ej.
        porque se perdió por corrupción o intervención externa mientras el
        resto del checkpoint ya estaba en `vectors`. De lo contrario el
        polígono reconstruido queda huérfano: nunca entra al grafo de
        duplicados ni a las componentes ya confirmadas."""
        checkpoint_path = TEST_TMP_ROOT / "unit_state" / "unconditional_invalidate.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        with open_vector_checkpoint(checkpoint_path, "run-x", "identity-x") as connection:
            commit_block(
                connection, 0, "hash-a", {"polygons": 1},
                [(shapely_wkb.dumps(box(0, 0, 1, 1)), 1.0, 0.0, 0.0, 1.0, 1.0)],
            )
            commit_component(
                connection, 0, [(shapely_wkb.dumps(box(0, 0, 1, 1)), 1.0, "parcel_scale")]
            )
            set_vector_stage(connection, "vectors")

            # Se pierde la fila de vector_blocks sin pasar por invalidate_block
            # (simula corrupción/intervención externa), y se confirma un
            # reemplazo con contenido distinto para el mismo block_id.
            connection.execute("DELETE FROM vector_blocks WHERE block_id = 0")
            commit_block(
                connection, 0, "hash-b", {"polygons": 1},
                [(shapely_wkb.dumps(box(5, 5, 6, 6)), 1.0, 5.0, 5.0, 6.0, 6.0)],
            )

            self.assertEqual(vector_stage(connection), "blocks")
            merged_count = connection.execute("SELECT COUNT(*) FROM vector_merged_parts").fetchone()[0]
            self.assertEqual(merged_count, 0)

    def test_failed_block_replacement_preserves_previous_block(self):
        """Reproduce el hallazgo 5: si el INSERT del reemplazo de un bloque
        falla a mitad de camino, la transacción completa (DELETE + INSERT)
        debe revertirse -- el bloque anterior NUNCA debe quedar borrado sin
        su reemplazo confirmado."""
        checkpoint_path = TEST_TMP_ROOT / "unit_state" / "atomic_block.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        with open_vector_checkpoint(checkpoint_path, "run-atomic", "identity-atomic") as connection:
            commit_block(
                connection, 0, "hash-v1", {"polygons": 1},
                [(shapely_wkb.dumps(box(0, 0, 1, 1)), 1.0, 0.0, 0.0, 1.0, 1.0)],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                commit_block(
                    connection, 0, "hash-v2", {"polygons": 1},
                    [(None, 1.0, 0.0, 0.0, 1.0, 1.0)],  # wkb NOT NULL -> el INSERT falla
                )
            row = connection.execute(
                "SELECT identity_hash FROM vector_blocks WHERE block_id = 0"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "hash-v1")
            polygon_count = connection.execute(
                "SELECT COUNT(*) FROM vector_polygons WHERE block_id = 0"
            ).fetchone()[0]
            self.assertEqual(polygon_count, 1)

    def test_iter_lower_candidates_chunking_preserves_results(self):
        """Hallazgo 2: paginar la recuperación de candidatos del R-tree en
        páginas (`batch_size`) no debe perder ni duplicar resultados frente a
        una única página que los contenga a todos."""
        checkpoint_path = TEST_TMP_ROOT / "unit_state" / "candidates_chunk.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        with open_vector_checkpoint(checkpoint_path, "run-chunk", "identity-chunk") as connection:
            polygons_data = [
                (shapely_wkb.dumps(box(i, 0, i + 1, 1)), 1.0, float(i), 0.0, float(i + 1), 1.0)
                for i in range(5)
            ]
            commit_block(connection, 0, "hash", {"polygons": len(polygons_data)}, polygons_data)

            candidates_single = [
                row
                for page in iter_lower_candidates(connection, 999, -10.0, -10.0, 10.0, 10.0, batch_size=1000)
                for row in page
            ]
            candidates_chunked = [
                row
                for page in iter_lower_candidates(connection, 999, -10.0, -10.0, 10.0, 10.0, batch_size=1)
                for row in page
            ]
            self.assertEqual(len(candidates_single), 5)
            self.assertEqual(sorted(candidates_single), sorted(candidates_chunked))

    def test_iter_lower_candidates_never_materializes_more_than_one_page(self):
        """Hallazgo 2 (corrección real): con 900 polígonos candidatos y
        `batch_size=3` (la reproducción exacta del revisor), ninguna página
        entregada por el generador puede superar `batch_size` filas vivas a
        la vez -- a diferencia de la versión previa, que acumulaba las 900
        filas en una única lista `results` antes de devolverlas."""
        checkpoint_path = TEST_TMP_ROOT / "unit_state" / "candidates_scale.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        with open_vector_checkpoint(checkpoint_path, "run-scale", "identity-scale") as connection:
            polygon_count = 900
            polygons_data = [
                (shapely_wkb.dumps(box(i, 0, i + 1, 1)), 1.0, float(i), 0.0, float(i + 1), 1.0)
                for i in range(polygon_count)
            ]
            commit_block(connection, 0, "hash", {"polygons": polygon_count}, polygons_data)

            batch_size = 3
            max_page_len = 0
            page_count = 0
            total_rows = 0
            for page in iter_lower_candidates(
                connection, polygon_count + 1, -10.0, -10.0, float(polygon_count) + 10.0, 10.0, batch_size=batch_size
            ):
                max_page_len = max(max_page_len, len(page))
                page_count += 1
                total_rows += len(page)
            self.assertLessEqual(max_page_len, batch_size)
            self.assertGreater(page_count, 1)
            self.assertEqual(total_rows, polygon_count)

    def test_run_graph_stage_holds_at_most_one_candidate_page_at_a_time(self):
        """Hallazgo 2 (ronda 4): `_run_graph_stage` consumía
        `iter_lower_candidates` con `for candidate_page in iter_lower_candidates(...)`.
        Ese patrón sufre el look-ahead propio de CPython: al pedir la
        siguiente página, el generador la computa (fetchmany+fetchall) ANTES
        de que el `for` libere la referencia a la página anterior, dando un
        pico real de 2*batch_size filas WKB vivas (el revisor midió 6 filas
        con batch_size=3, no 3). Se instrumenta el ciclo de vida real de cada
        página entregada durante `_run_graph_stage` (no el tamaño aislado de
        una página) para confirmar que el pico nunca supera batch_size."""
        checkpoint_path = TEST_TMP_ROOT / "unit_state" / "candidates_live_pages.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        with open_vector_checkpoint(checkpoint_path, "run-live", "identity-live") as connection:
            polygon_count = 900
            polygons_data = [
                (shapely_wkb.dumps(box(i, 0, i + 1, 1)), 1.0, float(i), 0.0, float(i + 1), 1.0)
                for i in range(polygon_count - 1)
            ]
            # El último polígono cubre a todos los anteriores en su bbox, forzando
            # que `iter_lower_candidates` deba entregarle sus 899 candidatos en
            # múltiples páginas de `batch_size` filas -- la reproducción exacta
            # del revisor (900 polígonos, batch_size=3) en vez de solo pares
            # contiguos que jamás cruzan una frontera de página.
            polygons_data.append(
                (
                    shapely_wkb.dumps(box(-10.0, -10.0, float(polygon_count) + 10.0, 10.0)),
                    1.0,
                    -10.0,
                    -10.0,
                    float(polygon_count) + 10.0,
                    10.0,
                )
            )
            commit_block(connection, 0, "hash", {"polygons": polygon_count}, polygons_data)
            set_vector_stage(connection, "graph")

            batch_size = 3
            settings = replace(load_settings(), vector_graph_page_size=batch_size)

            state = {"live_rows": 0, "peak_rows": 0}

            class TrackedPage(list):
                def __init__(self, rows):
                    super().__init__(rows)
                    state["live_rows"] += len(self)
                    state["peak_rows"] = max(state["peak_rows"], state["live_rows"])

                def __del__(self):
                    state["live_rows"] -= len(self)

            real_iter_lower_candidates = geometry.iter_lower_candidates

            def tracked_iter_lower_candidates(*args, **kwargs):
                for page in real_iter_lower_candidates(*args, **kwargs):
                    yield TrackedPage(page)

            with patch.object(geometry, "iter_lower_candidates", tracked_iter_lower_candidates):
                geometry._run_graph_stage(connection, settings)

            self.assertLessEqual(state["peak_rows"], batch_size)
            self.assertEqual(state["live_rows"], 0)

    def test_giant_component_raises_before_union_past_hard_limit(self):
        """Hallazgo 3: una componente que excede
        `large_component_hard_limit_polygons` debe abortar con un error
        explícito ANTES de cargar/unir sus geometrías, en vez de fusionarlas
        silenciosamente sin límite."""
        checkpoint_path = TEST_TMP_ROOT / "unit_state" / "hard_limit.sqlite"
        self._fresh_checkpoint(checkpoint_path)
        settings = replace(load_settings(), large_component_hard_limit_polygons=3)
        with open_vector_checkpoint(checkpoint_path, "run-hardlimit", "identity-hardlimit") as connection:
            circles = [Point(i * 0.1, 0).buffer(1.0) for i in range(5)]
            for block_id, circle in enumerate(circles):
                polygon_data = [(shapely_wkb.dumps(circle), circle.area, *circle.bounds)]
                commit_block(connection, block_id, f"hash-{block_id}", {"polygons": 1}, polygon_data)
            set_vector_stage(connection, "graph")
            _run_graph_stage(connection, settings)

            with self.assertRaises(RuntimeError) as ctx:
                _run_components_stage(connection, settings)
            self.assertIn("límite duro", str(ctx.exception))

            done = connection.execute(
                "SELECT COUNT(*) FROM vector_components WHERE status = 'done'"
            ).fetchone()[0]
            self.assertEqual(done, 0)

    def test_download_supercell_reuses_cache_only_for_same_request_identity(self):
        """Reproduce el hallazgo 4: un PNG cacheado con las dimensiones
        correctas pero de una capa/comuna/período distinto NUNCA debe
        reutilizarse -- sólo la identidad exacta de la solicitud WMS habilita
        el cache."""
        import io as _io
        from PIL import Image as _Image

        settings = replace(load_settings(), supercell_tiles=1)
        destination = TEST_TMP_ROOT / "unit_tile_cache" / "sc_0_0.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        destination.with_name(destination.name + ".meta.json").unlink(missing_ok=True)

        class CountingClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_bytes(self, url, *, params):
                self.calls += 1
                pixels = settings.supercell_tiles * 256
                image = np.zeros((pixels, pixels, 4), dtype=np.uint8)
                buffer = _io.BytesIO()
                _Image.fromarray(image, "RGBA").save(buffer, format="PNG")
                return buffer.getvalue()

        client = CountingClient()
        download_supercell(client, "9014", "sii:LAYER_A", 0, 0, destination, settings)
        self.assertEqual(client.calls, 1)

        # Misma comuna/capa/coordenadas: reutiliza el PNG cacheado.
        download_supercell(client, "9014", "sii:LAYER_A", 0, 0, destination, settings)
        self.assertEqual(client.calls, 1)

        # Cambio de capa (hallazgo 4): el PNG cacheado tiene las dimensiones
        # correctas pero corresponde a otra solicitud -- debe redescargarse.
        download_supercell(client, "9014", "sii:LAYER_B", 0, 0, destination, settings)
        self.assertEqual(client.calls, 2)

    def test_download_supercell_publication_window_is_exclusively_locked(self):
        """Hallazgo carrera PNG/sidecar: reproduce, con la misma técnica de
        `patch.object` sobre `write_json_atomic` que usó el revisor para
        forzar la carrera, el instante exacto en que antes existía la
        ventana -- PNG ya publicado (`temporary.replace(destination)`),
        sidecar todavía no (`write_json_atomic(metadata_path, ...)`) -- y
        confirma que ese instante queda exclusivamente bloqueado: ninguna
        segunda publicación del MISMO destino (otra ejecución/capa) puede
        colarse ahí a intercalar su propio PNG o sidecar."""
        import fcntl
        import io as _io

        from PIL import Image as _Image

        settings = replace(load_settings(), supercell_tiles=1)
        destination = TEST_TMP_ROOT / "unit_tile_cache" / "sc_race_0_0.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        destination.with_name(destination.name + ".meta.json").unlink(missing_ok=True)
        lock_path = destination.with_name(destination.name + ".lock")
        lock_path.unlink(missing_ok=True)

        class Client:
            def get_bytes(self, url, *, params):
                pixels = settings.supercell_tiles * 256
                image = np.zeros((pixels, pixels, 4), dtype=np.uint8)
                buffer = _io.BytesIO()
                _Image.fromarray(image, "RGBA").save(buffer, format="PNG")
                return buffer.getvalue()

        probe_results: list[str] = []
        original_write_json_atomic = write_json_atomic

        def probing_write_json_atomic(path, data):
            # download_supercell ya reemplazó el PNG y está a punto de
            # publicar el sidecar (la ventana exacta de la carrera
            # original). Una segunda publicación del mismo destino debe
            # encontrar el lockfile ya tomado en exclusiva.
            probe_handle = open(lock_path, "a+b")
            try:
                fcntl.flock(probe_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                probe_results.append("acquired")
                fcntl.flock(probe_handle.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                probe_results.append("blocked")
            finally:
                probe_handle.close()
            return original_write_json_atomic(path, data)

        with patch("sii_geometry.geometry.write_json_atomic", side_effect=probing_write_json_atomic):
            download_supercell(Client(), "9014", "sii:LAYER_RACE", 0, 0, destination, settings)

        self.assertEqual(probe_results, ["blocked"])
        # Tras liberarse el lock, un lector normal SÍ puede tomarlo: prueba
        # de que el bloqueo es transitorio (sólo cubre la sección crítica),
        # no un candado que quede pegado.
        with open(lock_path, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def test_download_supercell_crash_before_new_sidecar_forces_redownload(self):
        """Ronda 4, hallazgo PNG/sidecar tras crash: reproduce exactamente el
        escenario del revisor. Ejecución A publica PNG+sidecar válidos
        (identidad A). Ejecución B adquiere el lock, reemplaza el PNG (ahora
        contenido de B) y el proceso 'muere' (excepción) ANTES de escribir su
        propio sidecar. Sin el fix, el sidecar en disco seguía siendo el de A
        (válido, identidad A) conviviendo con el PNG de B -- la siguiente
        lectura para A aceptaba ese sidecar como cache válido y servía el PNG
        de B silenciosamente. Con el fix (invalidar/borrar el sidecar ANTES
        de reemplazar el PNG), la muerte de B deja el sidecar AUSENTE,
        forzando una redescarga en la siguiente lectura para A."""
        import io as _io

        from PIL import Image as _Image

        settings = replace(load_settings(), supercell_tiles=1)
        destination = TEST_TMP_ROOT / "unit_tile_cache" / "sc_crash_0_0.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        metadata_path = destination.with_name(destination.name + ".meta.json")
        metadata_path.unlink(missing_ok=True)

        class ColorClient:
            def __init__(self, color):
                self.color = color
                self.calls = 0

            def get_bytes(self, url, *, params):
                self.calls += 1
                pixels = settings.supercell_tiles * 256
                image = np.zeros((pixels, pixels, 4), dtype=np.uint8)
                image[:, :, :3] = self.color
                image[:, :, 3] = 255
                buffer = _io.BytesIO()
                _Image.fromarray(image, "RGBA").save(buffer, format="PNG")
                return buffer.getvalue()

        client_a = ColorClient((10, 20, 30))
        download_supercell(client_a, "9014", "sii:LAYER_A", 0, 0, destination, settings)
        self.assertEqual(client_a.calls, 1)
        with _Image.open(destination) as published:
            self.assertEqual(published.convert("RGB").getpixel((0, 0)), (10, 20, 30))

        client_b = ColorClient((200, 210, 220))

        class Boom(Exception):
            pass

        def crash_before_sidecar(path, data):
            raise Boom("B murió antes de escribir su sidecar")

        with patch("sii_geometry.geometry.write_json_atomic", side_effect=crash_before_sidecar):
            with self.assertRaises(Boom):
                download_supercell(client_b, "9014", "sii:LAYER_B", 0, 0, destination, settings)

        # El PNG en disco quedó reemplazado por el de B, pero el sidecar
        # quedó invalidado (ausente) por la muerte a mitad de publicación.
        with _Image.open(destination) as after_crash:
            self.assertEqual(after_crash.convert("RGB").getpixel((0, 0)), (200, 210, 220))
        self.assertFalse(metadata_path.exists())

        # La siguiente lectura para A (misma identidad original) NO debe
        # aceptar el estado en disco como cache válido -- debe redescargar,
        # nunca servir el PNG de B bajo la identidad de A.
        download_supercell(client_a, "9014", "sii:LAYER_A", 0, 0, destination, settings)
        self.assertEqual(client_a.calls, 2)
        with _Image.open(destination) as republished:
            self.assertEqual(republished.convert("RGB").getpixel((0, 0)), (10, 20, 30))

    def test_force_removes_stale_output_parquet_part(self):
        """Hallazgo 7: `_archive_before_force` debe limpiar también el
        `.parquet.part` del Parquet final publicado (`paths["output"]`,
        bajo `processed/`), no sólo los temporales bajo `raw/`."""
        settings = replace(load_settings(), repository_root=TEST_TMP_ROOT / "unit_force_output_repo")
        commune = Commune("9015", "Comuna Prueba Output", "Region Prueba")
        paths = _paths(settings, commune)
        if paths["raw"].exists():
            shutil.rmtree(paths["raw"])
        paths["raw"].mkdir(parents=True, exist_ok=True)
        output_temp = paths["output"].with_suffix(paths["output"].suffix + ".part")
        output_temp.parent.mkdir(parents=True, exist_ok=True)
        output_temp.write_bytes(b"basura-parquet-temporal")

        _archive_before_force(paths)

        self.assertFalse(output_temp.exists())


if __name__ == "__main__":
    unittest.main()
