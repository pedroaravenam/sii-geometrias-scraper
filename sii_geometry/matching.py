from __future__ import annotations

import re

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point
from shapely.strtree import STRtree


def normalize_address(value) -> str | None:
    if pd.isna(value) or not value:
        return None
    text = str(value).upper().strip()
    text = re.sub(r"\s+(A|B|C|D|E)?DP\s+\d+\w*.*$", "", text)
    text = re.sub(r"\s+B[XD]\s+\d+.*$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def match_roles_to_polygons(
    roles: pd.DataFrame,
    polygons: gpd.GeoDataFrame,
    max_distance_m: float = 10,
) -> tuple[gpd.GeoDataFrame, set[int], dict[str, int | float]]:
    roles = roles.copy()
    polygons_3857 = polygons.to_crs(3857).reset_index(drop=True)
    tree = STRtree(polygons_3857.geometry.values)
    lat = pd.to_numeric(roles.get("lat"), errors="coerce")
    lon = pd.to_numeric(roles.get("lon"), errors="coerce")
    valid = lat.between(-62, -17) & lon.between(-80, -64)
    points = gpd.GeoSeries(
        [Point(x, y) if ok else None for x, y, ok in zip(lon, lat, valid)], crs=4326
    ).to_crs(3857)
    poly_index = np.full(len(roles), -1, dtype=int)
    method = np.full(len(roles), "", dtype=object)
    distance = np.full(len(roles), np.nan)

    for index in np.flatnonzero(valid.to_numpy()):
        point = points.iloc[index]
        for candidate in tree.query(point):
            if polygons_3857.geometry.iloc[candidate].contains(point):
                poly_index[index] = int(candidate)
                method[index] = "point_in_polygon"
                distance[index] = 0.0
                break

    for index in np.flatnonzero(valid.to_numpy() & (poly_index < 0)):
        point = points.iloc[index]
        candidate = int(tree.nearest(point))
        gap = float(point.distance(polygons_3857.geometry.iloc[candidate]))
        if gap <= max_distance_m:
            poly_index[index] = candidate
            method[index] = "nearest_10m"
            distance[index] = round(gap, 2)

    coordinate_map: dict[tuple[float, float], int] = {}
    for index in np.flatnonzero(poly_index >= 0):
        coordinate_map.setdefault((round(float(lat.iloc[index]), 6), round(float(lon.iloc[index]), 6)), poly_index[index])
    for index in np.flatnonzero(valid.to_numpy() & (poly_index < 0)):
        candidate = coordinate_map.get((round(float(lat.iloc[index]), 6), round(float(lon.iloc[index]), 6)))
        if candidate is not None:
            poly_index[index] = candidate
            method[index] = "coord_inheritance"

    address_column = "dc_direccion" if "dc_direccion" in roles else "direccion_sii"
    addresses = roles.get(address_column, pd.Series([None] * len(roles))).map(normalize_address)
    address_map: dict[tuple[str, str], int] = {}
    for index in np.flatnonzero(poly_index >= 0):
        if addresses.iloc[index]:
            key = (str(roles.iloc[index].get("manzana", "")), addresses.iloc[index])
            address_map.setdefault(key, poly_index[index])
    for index in np.flatnonzero(poly_index < 0):
        if addresses.iloc[index]:
            key = (str(roles.iloc[index].get("manzana", "")), addresses.iloc[index])
            candidate = address_map.get(key)
            if candidate is not None:
                poly_index[index] = candidate
                method[index] = "address_inheritance"

    roles["_poly_idx"] = poly_index
    roles["_match_method"] = method
    roles["_match_dist_m"] = distance
    geometry = [polygons.geometry.iloc[index] if index >= 0 else None for index in poly_index]
    areas = [polygons_3857.geometry.iloc[index].area if index >= 0 else None for index in poly_index]
    roles["pol_area_m2"] = areas
    roles["pol_size_class"] = [
        polygons.iloc[index].get("pol_size_class") if index >= 0 else None for index in poly_index
    ]
    roles["calidad_geom"] = np.where(
        roles["_match_method"] == "point_in_polygon",
        "predio_con_dibujo",
        np.where(roles["_poly_idx"] >= 0, "dibujo_impreciso", "sin_dibujo"),
    )
    roles.loc[roles["pol_size_class"] == "large_component", "calidad_geom"] = "componente_grande_revision"
    result = gpd.GeoDataFrame(roles, geometry=geometry, crs=4326)
    used = set(int(value) for value in poly_index if value >= 0)
    total = len(roles)
    metrics = {
        "roles": total,
        "point_in_polygon": int((method == "point_in_polygon").sum()),
        "nearest_10m": int((method == "nearest_10m").sum()),
        "coord_inheritance": int((method == "coord_inheritance").sum()),
        "address_inheritance": int((method == "address_inheritance").sum()),
        "without_geometry": int((poly_index < 0).sum()),
        "coverage_pct": round(float((poly_index >= 0).sum() / total * 100), 3) if total else 0.0,
    }
    return result, used, metrics
