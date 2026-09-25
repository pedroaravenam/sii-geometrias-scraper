# Historial — sii-geometrias-scraper

> Append-only, lo nuevo arriba. **No leer completo**: buscar con `grep -n "<tema>" docs/history/HISTORIAL.md`.
> Formato: `## AAAA-MM-DD — título (commit)`.


## 2026-09-25 — Smoke de reanudación y relanzamiento de Arica (1f63852)
- Verificación aislada con seis bloques PNG sintéticos: crash inyectado después de confirmar 2/6 bloques; el proceso nuevo reabrió SQLite, procesó solo cuatro bloques y terminó en etapa `vectors` con 12 geometrías. Una segunda reanudación confirmó cero commits adicionales.
- Corrida reanudada y corrida limpia tuvieron área de diferencia simétrica de la unión y por geometría = 0,0 m². RSS pico: 194760 KiB reanudada, 190816 KiB limpia; escala pequeña, no demuestra límite de memoria de Arica.
- Arica (1101) relanzada vía `omp-tarea-larga` con código nuevo. En lectura puntual: proceso y monitor activos, `59526/59526` superceldas, estado `vectorizando`. El éxito de escala real queda pendiente del monitor; no se dejó ningún agente esperando. Smoke temporal retirado y árbol de código limpio.

## 2026-09-25 — Checkpoints reanudables de vectorización con memoria acotada (1f63852)
- Motivado por dos OOM-kill reales de la tarea `arica` (comuna 1101) al terminar `Vectorizacion: 6.870/6.870 bloques` (kernel oom-kill, 2026-09-24 17:16 y 22:58): `vectorize_supercells()` acumulaba todo en RAM y solo escribía el Parquet al final, perdiendo todo el trabajo de vectorización ante un OOM.
- Checkpoint SQLite versionado independiente del checkpoint API (`vector_run`/`vector_blocks`/`vector_polygons`+R-tree/`vector_graph_progress`/`vector_uf`/`vector_components`/`vector_merged_parts`); reanudación por etapas (blocks → graph → components → vectors) sin recomenzar desde cero.
- Invalidación+reemplazo de bloque atómicos en una sola transacción; paginado real de candidatos de fusión (sin retener más de una página viva); límite duro configurable antes de cargar/unir una componente gigante; subdivisión adaptativa de componentes grandes (`merge_component_adaptive`).
- Sidecar `.meta.json` con hash de identidad de solicitud para invalidar tiles cacheados que cambiaron de capa/parámetros; publicación de PNG+sidecar bajo lock de archivo (`locked_destination`) que invalida el sidecar viejo ANTES de reemplazar el PNG, a prueba de crash a mitad de publicación.
- Publicación final del Parquet vía `write_geoparquet_atomic` (temporal+validación de esquema/filas+fsync+`os.replace`); `--force` limpia también el `.parquet.part` de salida.
- Diseñado por worker `plan`; implementado y corregido en 4 rondas por worker Claude, cada ronda validada por revisión cruzada independiente (Codex, mismo modelo en las 4 rondas) con reproducciones aisladas de cada hallazgo (7 hallazgos iniciales + 2 en rondas posteriores, incluida una condición de carrera PNG/sidecar). 41/41 tests OK. Limitación conocida aceptada: la materialización final del GeoDataFrame post-dedup (ligada a `match_roles_to_polygons`) sigue completa en RAM.
- Pendiente: relanzar Arica (1101) con el código nuevo para validar en caso real de ~60k superceldas.

## 2026-09-23 — Reintento diferido de superceldas WMS (4809f84)
- Las superceldas que agotan sus intentos quedan pendientes y se reintentan al final del recorrido; solo bloquean la vectorización si vuelven a fallar. 17 tests OK.
## 2026-09-23 — Homologación de agentes: STATUS anterior archivado (commit en este cambio)
- El proyecto no tenía `STATUS.md`; se crea la estructura estándar.

