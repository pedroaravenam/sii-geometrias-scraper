# Avance de geometrías SII — 2026S2

Corte: 2026-10-29. **Todo el avance local está en una sola carpeta de datos: `avance/datos/`**. Este enlace lleva al directorio físico `/home/pedroaravenam/resultados-sii`, fuera del repositorio Git. Allí viven el respaldo publicado, el trabajo para reanudar, las capturas parciales y los archivos fuente. No hay que reunir manualmente otras carpetas.

| Ruta dentro de `avance/datos/` | Contenido |
|---|---|
| `2026S2/` y `catalogo/` | 18 GeoParquet publicados, API, checkpoints, metadatos, ZIP de tiles y estado nacional. `config/sii_geometry.local.json` apunta a esta misma carpeta como `storage_root`. |
| `trabajo/` | Tiles, manifiestos, respuestas y productos intermedios del scraper; `data/` en la raíz del proyecto es solo un enlace de compatibilidad a esta ruta, no una segunda copia. |
| `avances-importados/2026S2/` | Capturas incompletas importadas de Antofagasta, Calama y Río Verde; aisladas para no reemplazar capturas locales distintas. |
| `fuentes-drive/` | Cinco archivos fuente locales: `Catastro_SII.7z.001–004` y `data.7z`. Son respaldos comprimidos, no comunas terminadas adicionales. |

## Estado nacional

- **18 GeoParquet terminados**: Coquimbo 15/15, Arica 1, Peñaflor 1 e Isla de Pascua 1. Todos están en `avance/datos/2026S2/geoparquet/` y registrados en `avance/datos/catalogo/estado_geometrias.csv` como `completa_con_observaciones`.
- El catálogo local contiene **347 códigos SII**: 18 terminados y 329 pendientes. El archivo de referencia trae 346 códigos y el pipeline agrega Trehuaco (`8108`), ausente de ese archivo; esta cifra no debe confundirse sin más con el número oficial de comunas administrativas.
- **Capturas parciales importadas, no productos terminados**: Antofagasta (42.534 PNG), Calama (102.029 PNG) y Río Verde (941 PNG). Las versiones locales de Antofagasta y Calama tienen 5.959 y 35.178 PNG respectivamente; aunque comparten nombres, los bytes de los archivos coincidentes difieren. **No combinar ni sobrescribir tiles/checkpoints a ciegas.**
- Peñaflor e Isla de Pascua se importaron al directorio de trabajo, se publicaron mediante `publish_commune` y ahora forman parte del respaldo y catálogo canónicos. El Río Hurtado parcial antiguo en el archivo `data.7z` no se restauró: Río Hurtado ya está terminado localmente.
- `Catastro_SII.7z` y `geometrias.7z` contenían la misma carpeta `geometrias` (comparación byte a byte); se retiraron las tres partes redundantes de `geometrias.7z` de la copia local. También pueden retirarse de Drive; conservar `Catastro_SII.7z.001–004` y `data.7z`. Los tres archivos fuente originales pasaron `7z t`.

## Crudos de comunas publicadas

Se retiraron 675.563 PNG de trabajo, checkpoints de vectorización `vectors.sqlite` y Parquet intermedios de **las 18 comunas ya terminadas y respaldadas** (aprox. 26,76 GiB recuperados). Antes se verificaron el SHA-256 del GeoParquet local y publicado, el contenido y CRC de cada PNG frente a su ZIP WMS, la integridad del ZIP, del checkpoint SQLite publicado y del respaldo API gzip. Quedan los manifiestos, respuestas API originales, `state.sqlite`, métricas y salida procesada necesarios para que el scraper siga omitiendo esas comunas y reconozca su respaldo; también se conservaron los sidecars de identidad de los tiles.

Para reprocesar una comuna terminada sin volver a descargar el WMS hay que restaurar manualmente sus PNG desde `2026S2/wms_archivados/<comuna>_tiles.zip` a `trabajo/raw/geometrias/2026S2/<comuna>/tiles/`. Los checkpoints de vectorización eliminados se regenerarán; la restauración no es automática. No se tocaron los parciales de Antofagasta, Calama y Río Verde, ni Camarones (`1106`), General Lagos (`1302`) o Putre (`1301`) en la corrida activa. Sí se limpió Arica (`1101`), ya publicada.

## Región Arica y Parinacota en curso

El 2026-10-29 se lanzó `arica-restante` bajo `omp-tarea-larga` para Camarones (`1106`), General Lagos (`1302`) y Putre (`1301`); Arica (`1101`) ya está terminada. Servicio y monitor estaban activos tras el arranque; el insumo histórico regional se verificó. Consultar `omp-tarea-larga estado arica-restante`, `omp-tarea-larga log arica-restante 30` y `avance/datos/catalogo/estado_geometrias.csv` para cifras actuales. **No ejecutar otro scraper SII simultáneo.** El recuento de 18 terminadas arriba es el corte anterior a finalizar esta corrida.

El informe horario (mismo formato verificado que usamos en Coquimbo) corre en punto cada hora vía `informe-horario-arica-restante.timer`, persistente entre reinicios. Es una herramienta personal de este equipo (`~/.local/bin/informe-horario-arica-restante` + unidades en `~/.config/systemd/user/`), no versionada en este repo, igual que su contraparte de Coquimbo. Se muestra de verdad en el panel lateral `w5:p6` (pane configurado en `~/.local/state/omp-tareas/arica-restante/panel_avances`) usando `herdr pane run`, el mismo mecanismo que usa `mostrar_panel()` dentro de `omp-tarea-larga`; además deja cada corte en `informes-horarios.log` con tabla de estado, evidencia de manifiesto/WMS/API/checkpoint, chequeo de OOM/memoria y comentario opcional de un modelo gratuito; se autodesactiva al detectar el fin de la tarea. El monitor de anomalías sigue cada 30 minutos.

El enlace `avance/datos` y la ruta `data/` son propios de este equipo; al clonar en otro hay que configurar `storage_root` y recrear los accesos. No publicar respuestas prediales ni datos descargados en Git. Los archivos `.7z` se mantienen como respaldo comprimido de origen, no como una segunda carpeta de trabajo.
