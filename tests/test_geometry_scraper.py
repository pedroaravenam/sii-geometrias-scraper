from __future__ import annotations

import unittest
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from shapely.geometry import Point, box

from sii_geometry.catalog import normalize_name
from sii_geometry.cli import build_parser, resolve_reference_csv
from sii_geometry.config import load_settings
from sii_geometry.geometry import _block_origins, merge_overlapping_polygons, vectorize_image
from sii_geometry.matching import match_roles_to_polygons
from sii_geometry.pipeline import summarize_final_output
from sii_geometry.records import extract_role_keys
from sii_geometry.reference_assets import file_sha256, validate_reference_asset
from sii_geometry.state import checkpoint_database, read_manifest, save_api_result, write_json_atomic
from sii_geometry.storage import publish_commune, save_storage_configuration


TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp"


class GeometryScraperTests(unittest.TestCase):
    def test_cli_accepts_rematch_without_force(self):
        args = build_parser().parse_args(["scrape", "--comuna", "5101", "--rematch"])
        self.assertTrue(args.rematch)
        self.assertFalse(args.force)

    def test_normalize_name_removes_accents(self):
        self.assertEqual(normalize_name("  Peñaflor "), "PENAFLOR")

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


if __name__ == "__main__":
    unittest.main()
