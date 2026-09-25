# STATUS — sii-geometrias-scraper

> Snapshot del estado actual, ≤120 líneas. El detalle histórico va en `docs/history/HISTORIAL.md` y los planes largos en `docs/ROADMAP.md`.

## Resumen
- Fecha: 2026-09-25
- Último commit de código: `1f63852` — checkpoints reanudables de vectorización; documentación anterior: `afc68b5`.
- ¿Publicado?: Sí, sincronizado con origin/main en GitHub. Herramienta local CLI sin despliegue a servidor. Sin cambios locales pendientes.

## Entorno
- Destino / Ejecución: Local Windows (PowerShell/CMD con Python 3.11+) o Linux (.venv con Python 3.11+).
- Insumos externos: Google Drive público (carpeta `1mIjnsXy3t7xs3BR-sDdlg1F8wBNqUrBp`) para Parquets 2026S1 regionales; WMS público y API de consulta del SII de Chile (`www4.sii.cl`).
- Salidas locales: GeoParquet, respuestas API comprimidas, checkpoints y catálogos en carpeta configurada en `config/sii_geometry.local.json` (por defecto fuera del repositorio). Sin secretos ni credenciales requeridas.

## Próximo paso exacto
Dejar correr `arica` (1101) bajo `omp-tarea-larga` y consultar `omp-tarea-larga estado arica` cuando el monitor anuncie terminación o anomalía. Confirmar resultado final y memoria en escala real; no inferirlo del smoke sintético.

## Pendientes priorizados
1. Arica (1101) relanzada con checkpoint nuevo; proceso y monitor activos, `59526/59526` superceldas y estado `vectorizando` en la lectura puntual del smoke. Confirmar que termina sin el OOM sufrido en las dos ejecuciones con código viejo.
2. Ejecutar prueba completa de la comuna piloto Peñaflor (`14504`) con la recuperación diferida activa.
3. Monitorear tasa de respuesta y pausas frente a la API WMS/getFeatureInfo del SII.
4. Evaluar tasa de match punto-en-polígono y vecino más cercano sobre el dataset 2026S2.
5. Mantener actualizado el catálogo de insumos regionales (`config/catastro_2026S1_assets.json`).

## Verificación de reanudación
- Suite: 41/41 tests OK. Smoke aislado con seis bloques PNG sintéticos: interrupción tras 2/6 bloques confirmados; al reabrir, solo se procesaron los cuatro restantes y se llegó a etapa `vectors` (12 geometrías). Segunda reanudación: cero commits.
- Contra corrida limpia: área de diferencia simétrica de la unión y diferencias por geometría = 0,0 m². RSS pico observado en fixture pequeño: 190816 KiB limpia y 194760 KiB reanudada; no extrapolable a Arica. Script y fixture efímeros eliminados.

## Riesgos activos
- Bloqueo o intermitencia de endpoints WMS/API del SII — mitigado con pausas configuradas, caché local de tiles, reintentos diferidos y checkpoints por comuna.
- Consumo de memoria en comunas extensas o polígonos complejos — mitigado con checkpoint SQLite reanudable por etapas (blocks→graph→components→vectors) con memoria acotada por página en la fase de fusión de duplicados; la materialización final del GeoDataFrame (post-dedup, ligada al matching de roles) sigue completa en RAM y es la limitación de memoria conocida restante.
- Sobrecarga de red o IP — mitigado con serialización de descargas y recomendación de no concurrencia en la misma conexión.

## Enlaces
- `docs/ROADMAP.md`
- `docs/history/HISTORIAL.md`
