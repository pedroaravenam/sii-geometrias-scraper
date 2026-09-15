from __future__ import annotations

import unittest
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, box

from sii_geometry.catalog import normalize_name
from sii_geometry.cli import resolve_reference_csv
from sii_geometry.config import load_settings
from sii_geometry.geometry import merge_overlapping_polygons, vectorize_image
from sii_geometry.matching import match_roles_to_polygons
from sii_geometry.state import checkpoint_database, read_manifest, save_api_result, write_json_atomic
from sii_geometry.storage import publish_commune, save_storage_configuration


TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp"


class GeometryScraperTests(unittest.TestCase):
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
        self.assertEqual(len(catalog), 2)
        self.assertEqual(
            catalog.set_index("codigo_comuna").loc["14505", "status"],
            "pendiente",
        )
        with sqlite3.connect(central / "2026S2" / "checkpoints" / "14504_penaflor.sqlite") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM api_results").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
