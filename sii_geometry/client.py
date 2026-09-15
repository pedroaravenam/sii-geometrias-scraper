from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests

from .config import Settings


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www4.sii.cl/mapasui/index.html",
    "Origin": "https://www4.sii.cl",
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9",
}


@dataclass
class RequestResult:
    data: dict[str, Any] | None
    attempts: int
    error: str | None = None


class SIIClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _sleep(self, seconds: float | None = None) -> None:
        time.sleep(self.settings.request_delay_s if seconds is None else seconds)

    def get_bytes(self, url: str, *, params: dict[str, Any]) -> bytes:
        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                self._sleep()
                response = self.session.get(url, params=params, timeout=self.settings.request_timeout_s)
                if response.status_code == 429:
                    self._sleep(self.settings.backoff_base_s * (2 ** attempt))
                    continue
                response.raise_for_status()
                return response.content
            except requests.RequestException as error:
                last_error = error
                if attempt < self.settings.max_retries:
                    self._sleep(self.settings.backoff_base_s * (2 ** (attempt - 1)))
        raise RuntimeError(f"Solicitud WMS fallida después de {self.settings.max_retries} intentos: {last_error}")

    def rpc(self, endpoint: str, data: dict[str, Any]) -> RequestResult:
        url = f"{self.settings.api_url}/{endpoint}"
        payload = {
            "data": data,
            "metaData": {
                "namespace": f"cl.sii.sdi.lob.bbrr.mapas.data.api.interfaces.MapasFacadeService/{endpoint}",
                "conversationId": "UNAUTHENTICATED-CALL",
                "transactionId": f"geometry-{uuid.uuid4()}",
            },
        }
        last_error = None
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                self._sleep()
                response = self.session.post(url, json=payload, timeout=self.settings.request_timeout_s)
                if response.status_code == 429:
                    self._sleep(self.settings.backoff_base_s * (2 ** attempt))
                    continue
                response.raise_for_status()
                body = response.json()
                return RequestResult(body.get("data"), attempt)
            except (requests.RequestException, ValueError) as error:
                last_error = str(error)
                if attempt < self.settings.max_retries:
                    self._sleep(self.settings.backoff_base_s * (2 ** (attempt - 1)))
        return RequestResult(None, self.settings.max_retries, last_error)

    def get_context(self, commune_code: str) -> dict[str, Any]:
        result = self.rpc("getServicioPredio", {"comuna": int(commune_code), "eac": -1})
        if result.data is None:
            raise RuntimeError(f"No fue posible obtener el contexto SII de la comuna {commune_code}: {result.error}")
        if isinstance(result.data, list):
            if not result.data:
                raise RuntimeError(f"El SII devolvió contexto vacío para {commune_code}")
            return result.data[0]
        return result.data

    def get_predio(
        self,
        commune_code: str,
        manzana: str,
        predio: str,
        layer: str,
    ) -> RequestResult:
        services = [
            {
                "comuna": int(commune_code),
                "layer": layer,
                "style": "PREDIOS_WMS_V0",
                "eac": 0,
                "eacano": 0,
            },
            {
                "comuna": int(commune_code),
                "layer": "sii:BR_CART_AH_MUESTRAS",
                "style": "AH_MUESTRA_EAC_14_2022",
                "eac": 14,
                "eacano": 2022,
            },
        ]
        return self.rpc(
            "getPredioNacional",
            {
                "predio": {
                    "comuna": str(int(commune_code)),
                    "manzana": str(int(manzana)),
                    "predio": str(int(predio)),
                },
                "servicios": services,
            },
        )

    def get_feature_info(
        self,
        commune_code: str,
        layer: str,
        point_lon: float,
        point_lat: float,
        bounds: tuple[float, float, float, float],
        eac: int = 0,
        eacano: int = 0,
    ) -> RequestResult:
        min_lon, min_lat, max_lon, max_lat = bounds
        width, height = 800, 600
        x = ((point_lon - min_lon) / (max_lon - min_lon)) * width
        y = height - ((point_lat - min_lat) / (max_lat - min_lat)) * height
        request_data = {
            "clickInfo": {
                "x": round(x, 4),
                "y": round(y, 4),
                "southwestx": min_lat,
                "southwesty": min_lon,
                "northeastx": max_lat,
                "northeasty": max_lon,
                "width": width,
                "height": height,
                "layer": layer,
                "servicios": [
                    {
                        "comuna": int(commune_code),
                        "layer": layer,
                        "style": "PREDIOS_WMS_V0",
                        "eac": eac,
                        "eacano": eacano,
                    }
                ],
            }
        }
        result = self.rpc("getFeatureInfo", request_data)
        if result.data and (result.data.get("existePredio", -1) == -1 or result.data.get("manzana", 0) == 0):
            return RequestResult(None, result.attempts, "El punto no devolvió un predio")
        return result
