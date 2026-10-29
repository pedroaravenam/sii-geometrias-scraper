# STATUS — sii-geometrias-scraper

> Snapshot del estado actual, ≤120 líneas. El detalle histórico va en `docs/history/HISTORIAL.md` y los planes largos en `docs/ROADMAP.md`.

## Resumen
- Fecha: 2026-10-29.
- Avance consolidado: 18 GeoParquet comunales para `2026S2` en `/home/pedroaravenam/resultados-sii`; 15 de Coquimbo, Arica, Peñaflor e Isla de Pascua. Todos figuran `completa_con_observaciones`.
- Inventario y acceso desde este proyecto: `avance/README.md` y su única carpeta `avance/datos/`. El directorio físico `/home/pedroaravenam/resultados-sii` contiene resultados, trabajo (`trabajo/`), parciales (`avances-importados/`) y archivos fuente (`fuentes-drive/`); `data/` en el proyecto es un enlace de compatibilidad. Los datos siguen fuera de Git.

## Entorno
- Ejecución local Windows o Linux (Python 3.11+).
- `storage_root`: `/home/pedroaravenam/resultados-sii` (configuración local de este equipo).
- Catálogo: 347 códigos SII (18 terminados, 329 pendientes); la referencia incluida contiene 346 y el pipeline agrega Trehuaco (`8108`).

## Ejecución regional activa
- `arica-restante` iniciada el 2026-10-29 con `omp-tarea-larga`, servicio y timer activos (monitoreo cada 30 min, sin modelo). Comando: `.venv/bin/python -u -m sii_geometry scrape --comuna 1106 --comuna 1302 --comuna 1301`. Insumo histórico regional verificado y Camarones seleccionado primero; Arica (1101) ya estaba terminada.
- Revisar con `omp-tarea-larga estado arica-restante` y `omp-tarea-larga log arica-restante 30`. No lanzar otro scraper SII en paralelo. Los 18 GeoParquet documentados arriba son el corte anterior al término de esta corrida.
- Informe horario verificado (mismo formato que usamos en Coquimbo): `informe-horario-arica-restante.timer` (systemd de usuario, personal, no versionado, hora en punto, persistente). Genera tabla de estado de unidad/timer, evidencia de manifiesto/WMS/API/checkpoint, chequeo de OOM y memoria, y comentario opcional de un modelo gratuito. Salida: `~/.local/state/omp-tareas/arica-restante/informes-horarios.log`; se autodesactiva al detectar que la tarea terminó. Independiente del monitor de anomalías cada 30 min.

## Pendientes priorizados
1. Antofagasta y Calama tienen capturas parciales distintas entre la copia del trabajo y la local; no reemplazar PNG ni checkpoints con el mismo nombre sin resolver identidad de captura.
2. Río Verde tiene 941 PNG importados, aislados en `resultados-sii/avances-importados/2026S2/`.
3. Evaluar calidad y cobertura geométrica de las 18 comunas `completa_con_observaciones`.

## Verificación de consolidación
- Tres archivos fuente de Drive verificados con `7z t`: `Catastro_SII` (145.785 archivos), `geometrias` (145.778) y `data` (3.902). Las carpetas `geometrias` de los dos primeros son idénticas.
- Peñaflor (32.119 filas) e Isla de Pascua (3.045 filas) se publicaron con `publish_commune`; 18 GeoParquet finales y catálogo actualizado. El manifiesto de Isla y su checkpoint SQLite fueron comprobados antes de la importación.
- Limpieza conservadora de las 18 comunas publicadas: 675.563 PNG locales, checkpoints `vectors.sqlite` y vectores Parquet intermedios retirados tras verificar ZIP/CRC, GeoParquet/SHA-256, API gzip y SQLite publicado. Se recuperaron ~26,76 GiB; `/home` mostró 35 GiB disponibles al terminar. Se conservaron manifiestos, salida procesada, API y `state.sqlite` locales; `process_commune` omitió Isla de Pascua sin red. No se tocaron Camarones, General Lagos, Putre ni los parciales importados.

## Riesgos activos
- Bloqueo/intermitencia del SII: pausas y reintentos configurados; nunca descargas simultáneas desde la misma conexión.
- Memoria durante el match final de comunas extensas.
- Archivos originales de Drive con atributos prediales: no publicarlos en Git ni volver a descomprimirlos junto a los resultados canónicos.

## Enlaces
- `avance/README.md`
- `docs/ROADMAP.md`
- `docs/history/HISTORIAL.md`
