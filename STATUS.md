# STATUS — sii-geometrias-scraper

> Snapshot del estado actual, ≤120 líneas. El detalle histórico va en `docs/history/HISTORIAL.md` y los planes largos en `docs/ROADMAP.md`.

## Resumen
- Fecha: 2026-09-23
- Último commit: `4809f84` — Retry failed WMS supercells in a deferred second pass
- ¿Publicado?: Sí, sincronizado con origin/main en GitHub. Herramienta local CLI sin despliegue a servidor. Hay cambios locales no commiteados del usuario en pipeline, tests y documentación.

## Entorno
- Destino / Ejecución: Local Windows (PowerShell/CMD con Python 3.11+) o Linux (.venv con Python 3.11+).
- Insumos externos: Google Drive público (carpeta `1mIjnsXy3t7xs3BR-sDdlg1F8wBNqUrBp`) para Parquets 2026S1 regionales; WMS público y API de consulta del SII de Chile (`www4.sii.cl`).
- Salidas locales: GeoParquet, respuestas API comprimidas, checkpoints y catálogos en carpeta configurada en `config/sii_geometry.local.json` (por defecto fuera del repositorio). Sin secretos ni credenciales requeridas.

## Próximo paso exacto
Verificar e integrar los cambios locales de reintento diferido de superceldas WMS ejecutando `.venv/bin/python -m unittest tests/test_geometry_scraper.py`.

## Pendientes priorizados
1. Validar e integrar cambios pendientes de reintento diferido de superceldas WMS (`sii_geometry/pipeline.py`, `tests/test_geometry_scraper.py`, `docs/PIPELINE_GEOMETRIAS_SII.md`).
2. Ejecutar prueba completa de la comuna piloto Peñaflor (`14504`) con la recuperación diferida activa.
3. Monitorear tasa de respuesta y pausas frente a la API WMS/getFeatureInfo del SII.
4. Evaluar tasa de match punto-en-polígono y vecino más cercano sobre el dataset 2026S2.
5. Mantener actualizado el catálogo de insumos regionales (`config/catastro_2026S1_assets.json`).

## Riesgos activos
- Bloqueo o intermitencia de endpoints WMS/API del SII — mitigado con pausas configuradas, caché local de tiles, reintentos diferidos y checkpoints por comuna.
- Consumo de memoria en comunas extensas o polígonos complejos — mitigado con partición en superceldas y GeoParquet comunal.
- Sobrecarga de red o IP — mitigado con serialización de descargas y recomendación de no concurrencia en la misma conexión.

## Enlaces
- `docs/ROADMAP.md`
- `docs/history/HISTORIAL.md`
