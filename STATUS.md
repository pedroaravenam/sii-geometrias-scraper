# STATUS — sii-geometrias-scraper

> Snapshot del estado actual, ≤120 líneas. El detalle histórico va en `docs/history/HISTORIAL.md` y los planes largos en `docs/ROADMAP.md`.

## Resumen
- Fecha: 2026-09-25
- Último commit: `1f63852` — feat: checkpoints reanudables de vectorización con memoria acotada
- ¿Publicado?: Sí, sincronizado con origin/main en GitHub. Herramienta local CLI sin despliegue a servidor. Sin cambios locales pendientes.

## Entorno
- Destino / Ejecución: Local Windows (PowerShell/CMD con Python 3.11+) o Linux (.venv con Python 3.11+).
- Insumos externos: Google Drive público (carpeta `1mIjnsXy3t7xs3BR-sDdlg1F8wBNqUrBp`) para Parquets 2026S1 regionales; WMS público y API de consulta del SII de Chile (`www4.sii.cl`).
- Salidas locales: GeoParquet, respuestas API comprimidas, checkpoints y catálogos en carpeta configurada en `config/sii_geometry.local.json` (por defecto fuera del repositorio). Sin secretos ni credenciales requeridas.

## Próximo paso exacto
Relanzar la comuna de Arica (1101) con el código nuevo (`.venv/bin/python -m sii_geometry scrape --comuna 1101`, vía `omp-tarea-larga`) para validar en un caso real de ~60k superceldas que la vectorización checkpointeada sostiene memoria acotada hasta el final y que una interrupción a mitad de vectorización reanuda sin recomenzar desde cero.

## Pendientes priorizados
1. Relanzar y monitorear Arica (1101) con el nuevo checkpoint de vectorización; confirmar que no repite el OOM que mató dos ejecuciones previas (2026-09-24 17:16 y 22:58, kernel oom-kill, justo tras `Vectorizacion: 6.870/6.870 bloques` con el código viejo).
2. Ejecutar prueba completa de la comuna piloto Peñaflor (`14504`) con la recuperación diferida activa.
3. Monitorear tasa de respuesta y pausas frente a la API WMS/getFeatureInfo del SII.
4. Evaluar tasa de match punto-en-polígono y vecino más cercano sobre el dataset 2026S2.
5. Mantener actualizado el catálogo de insumos regionales (`config/catastro_2026S1_assets.json`).

## Riesgos activos
- Bloqueo o intermitencia de endpoints WMS/API del SII — mitigado con pausas configuradas, caché local de tiles, reintentos diferidos y checkpoints por comuna.
- Consumo de memoria en comunas extensas o polígonos complejos — mitigado con checkpoint SQLite reanudable por etapas (blocks→graph→components→vectors) con memoria acotada por página en la fase de fusión de duplicados; la materialización final del GeoDataFrame (post-dedup, ligada al matching de roles) sigue completa en RAM y es la limitación de memoria conocida restante.
- Sobrecarga de red o IP — mitigado con serialización de descargas y recomendación de no concurrencia en la misma conexión.

## Enlaces
- `docs/ROADMAP.md`
- `docs/history/HISTORIAL.md`
