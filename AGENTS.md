# sii-geometrias-scraper

Las reglas globales (cierre de avance, producción, estructura documental, modelos, secretos) están en `~/.codex/AGENTS.md` y aplican aquí. Este archivo tiene solo lo propio del proyecto (≤120 líneas).

## Proyecto
Pipeline reproducible para descargar la cartografía predial vigente publicada por el Servicio de Impuestos Internos (SII) de Chile, vectorizarla a partir de tiles WMS, consultar atributos prediales vía API pública y generar archivos GeoParquet por comuna para el corte `2026S2`. Stack principal en Python (GeoPandas, Shapely, Rasterio, Requests, PyArrow) con soporte para ejecución interactiva y automatizada en Windows y Linux.

## Leer primero
- `STATUS.md` (snapshot corto).
- `README.md` (instrucciones de instalación y uso).
- `docs/PIPELINE_GEOMETRIAS_SII.md` (especificación técnica del flujo WMS, vectorización y match).
- `docs/history/HISTORIAL.md` solo con búsqueda, nunca completo.

## Comandos
| Para qué | Comando |
|---|---|
| Instalar (Linux) | `python3 -m venv .venv && .venv/bin/pip install -r requirements-geometry.txt` |
| Instalar (Windows) | `scripts\setup_geometry_scraper.cmd` |
| Verificar (tests) | `.venv/bin/python -m unittest tests/test_geometry_scraper.py` |
| Ejecutar en local (comuna) | `.venv/bin/python -m sii_geometry scrape --comuna <codigo>` |
| Ejecutar en local (Windows) | `.\SCRAPEAR_GEOMETRIAS.cmd --comuna <codigo>` |

## Publicar
- Destino: sin despliegue: commit + push
- Tipo: —
- Comando: commit + push directo a `main`

## Reglas propias
- Los procesos largos del scraper se corren con `omp-tarea-larga` (nunca una sesión de modelo esperando).
- Las salidas de datos masivas (GeoParquet, tiles, checkpoints, respuestas API) se guardan fuera del repositorio según la ruta configurada en `config/sii_geometry.local.json`; nunca versionar datos descargados ni salidas en git.
- Respetar las pausas y reintentos configurados para no saturar los servicios públicos del SII (`www4.sii.cl`) ni provocar bloqueos de IP; no lanzar múltiples procesos simultáneos desde la misma conexión.
- El catálogo de comunas y los límites comunales de referencia residen en `resources/reference/`; no intentar descargarlos desde la red durante la ejecución estándar.
