# Pipeline de geometrías prediales SII

## 1. Objetivo del proceso

Descargar la cartografía predial vigente del SII para todo el territorio de una
o más comunas, vectorizarla, asociar cada geometría con su rol y conservar todos
los atributos públicos obtenidos durante las consultas. El primer piloto es
Peñaflor (`14504`) para el snapshot `2026S2`.

La geometría se vincula con la serie histórica existente mediante la clave
`comuna + manzana + predio`. No se presenta la geometría vigente como si fuera
una geometría histórica.

## 2. Usuario o destinatario

El pipeline está preparado para ejecutarse localmente desde VS Code en Windows,
sin Docker, WSL, VPN ni servidores externos.

## 3. Flujo paso a paso

1. Carga el catálogo SII de comunas y permite seleccionar una región, varias
   comunas o una comuna específica.
2. Obtiene la capa WMS correspondiente y el límite territorial comunal.
3. Calcula superceldas de 1.024 x 1.024 píxeles a zoom 19.
4. Descarga los PNG con pausas, reintentos y caché local.
5. Vectoriza por bloques con solape y detección tolerante del color de relleno.
6. Descarga una vez el Parquet histórico 2026S1 de la región, verifica su
   SHA-256 y extrae las claves de roles de la comuna.
7. Consulta `getPredioNacional` y conserva todos los datos publicados en 2026S2.
8. Asocia coordenadas y polígonos mediante punto-en-polígono, vecino hasta 10 m
   y herencias controladas de coordenada o dirección.
9. Consulta por punto los polígonos huérfanos para recuperar roles nuevos.
10. Genera GeoParquet, JSONL crudo, métricas y manifiesto de ejecución.
11. Al terminar, publica un paquete consolidado y verificable en la carpeta
    sincronizada configurada para ese equipo.

## 4. Inputs necesarios

- Conexión capaz de acceder a `www4.sii.cl`.
- Parquet regional 2026S1 descargado automáticamente desde la Release pública;
  contiene las 39 columnas históricas para enumeración y validación.
- Configuración en `config/sii_geometry.json`.
- Catálogo comunal de Catastral.cl y límite comunal derivado de BCN; ambos se
  descargan una vez y quedan cacheados bajo `data/raw/geometrias/_reference/`.

## 5. Outputs esperados

Para Peñaflor:

```text
data/raw/geometrias/2026S2/14504_penaflor/
├── tiles/                         PNG originales WMS
├── checkpoints/state.sqlite       consultas reanudables
├── poligonos_vectorizados.parquet vectorización previa al match
├── respuestas_api.jsonl           respuestas API conservadas
└── manifest.json                  estado y trazabilidad

data/processed/geometrias/2026S2/
├── 14504_penaflor.parquet
└── 14504_penaflor_metrics.json
```

Con almacenamiento central configurado, se crea además. El catálogo contiene
desde el inicio las 346 comunas: las no ejecutadas figuran como `pendiente` y
cada respaldo terminado actualiza su fila correspondiente.

```text
Catastro_SII/
├── insumos/2026S1/catastro_2026S1_metropolitana.parquet
├── catalogo/estado_geometrias.csv
└── 2026S2/
    ├── geoparquet/14504_penaflor.parquet
    ├── respuestas_api/14504_penaflor.jsonl.gz
    ├── metadatos/14504_penaflor_manifest.json
    ├── metadatos/14504_penaflor_metricas.json
    ├── checkpoints/14504_penaflor.sqlite
    └── wms_archivados/14504_penaflor_tiles.zip
```

El GeoParquet conserva el esquema base de Viña y Quilpué, todos los atributos
obtenidos desde la API y los campos técnicos:

- `comuna`, `manzana`, `predio`, `rol`;
- `geometry`, `_poly_idx`, `_match_method`, `_match_dist_m`, `pol_area_m2`;
- `calidad_geom`, `_ok`, `_status`, `_api_attempts`, `_api_queried_at`;
- `periodo`, `periodo_geometria`, `fecha_captura`;
- `wms_layer`, `wms_style`, `zoom`, `geometry_source`.

## 6. Reglas principales

- Una comuna terminada se omite automáticamente.
- Una ejecución interrumpida continúa desde los PNG, reutiliza el GeoParquet de
  polígonos si la vectorización comunal ya terminó y salta las respuestas API
  existentes. La consola informa el avance por bloques y cada 100 consultas.
- `--force` archiva el manifiesto, checkpoint y productos anteriores bajo
  `backups/`, y luego vuelve a solicitar y recalcular el snapshot.
- Los polígonos sin rol y roles sin geometría no se eliminan.
- Ningún dato devuelto por la API se descarta: los campos no canónicos reciben
  prefijo `api_` y la respuesta original permanece en JSONL/SQLite.
- La identidad de un polígono en checkpoints usa una huella de su geometría, no
  su posición temporal dentro de un archivo.
- SQLite y los miles de PNG trabajan localmente. Sólo después de terminar una
  comuna se copian de forma consolidada a OneDrive o Google Drive.
- Cada GeoParquet respaldado queda registrado con SHA-256 en el catálogo.
- Si falla el respaldo, la extracción local sigue marcada como terminada pero
  el manifiesto conserva `storage_backup.status=fallido`; volver a ejecutar la
  comuna reintenta el respaldo sin repetir el scraping.

## 7. Excepciones y casos límite

- Cambios de color o transparencia del WMS: se estima el color dominante y se
  registran color y proporción de píxeles por bloque.
- Roles eliminados desde 2026S1: quedan como consulta `not_found`.
- Roles vigentes que el visor del SII no puede representar conservan todos sus
  datos y la llave histórica, con calidad `vigente_sin_visualizacion_sii`.
- Los roles ausentes del período actual se distinguen como
  `rol_no_encontrado_periodo_actual`; no se mezclan con los vigentes sin dibujo.
- Roles nuevos en 2026S2: se intentan recuperar desde polígonos huérfanos.
- Copropiedades: se permiten varios registros asociados a un mismo polígono o rol.
- Caídas, límites de tasa y cortes de red: reintentos exponenciales y checkpoints.
- Una comuna puede finalizar como `completa_con_observaciones`; no se exige un
  100% artificial para cerrar el proceso.

## 8. Criterios de calidad

- Todas las superceldas planificadas fueron descargadas y leídas como PNG.
- Todas las geometrías resultantes son válidas o quedan reportadas.
- Cada fila conserva el método y distancia de asociación.
- El cierre informa por separado la cobertura del catastro histórico y el
  resultado completo: polígonos totales, con rol, sin rol, porcentaje de
  atribución y roles únicos con o sin geometría.
- El período informado por la API queda separado del período y fecha de captura.
- Una ejecución limitada de diagnóstico nunca queda marcada como completa.

## 9. Riesgos o ambigüedades pendientes

- El WMS es raster y sus contornos son referenciales, no deslindes legales.
- La simbología puede volver a cambiar y requerir recalibración.
- Los 30.653 roles candidatos de Peñaflor implican una corrida API de varias
  horas con la pausa conservadora configurada.
- El CSV 2026S1 no enumera altas de 2026S2; la recuperación por polígonos
  huérfanos mitiga, pero no demuestra que encuentre roles nuevos sin dibujo.
- Antes de escalar nacionalmente deben revisarse los resultados urbanos y
  agrícolas de Peñaflor y fijarse umbrales de alerta por tipo de comuna.

## 10. Siguiente acción recomendada

Completar Peñaflor, revisar visualmente muestras urbanas y agrícolas, contrastar
roles con el CSV original y recién entonces habilitar una región completa.

## Instalación y uso

Desde el terminal de VS Code:

```powershell
scripts\setup_geometry_scraper.cmd
```

La primera instalación abre un selector para elegir una carpeta local o
sincronizada donde guardar los resultados. Después de seleccionar región o
comunas se descarga y verifica automáticamente el Parquet histórico necesario.
La ruta local se guarda en `config/sii_geometry.local.json`, que no se versiona.
En otro equipo se vuelve a ejecutar el instalador o se indica la ruta:

```powershell
.\.venv\Scripts\python.exe -m sii_geometry configure-storage --path "D:\Catastro_SII"
```

También puede definirse `SII_GEOMETRY_STORAGE_ROOT`, útil si cada equipo usa una
ruta local, OneDrive o Google Drive diferente. Para controles especiales todavía
puede indicarse un CSV o Parquet alternativo con `--reference-csv RUTA`.

Selector interactivo:

```powershell
.\.venv\Scripts\python.exe -m sii_geometry scrape
```

Planificar o ejecutar Peñaflor:

```powershell
.\.venv\Scripts\python.exe -m sii_geometry scrape --comuna 14504 --dry-run
.\.venv\Scripts\python.exe -m sii_geometry scrape --comuna 14504
```

También están disponibles las tareas `SII: instalar scraper`, `SII: seleccionar
región/comunas`, `SII: planificar Peñaflor` y `SII: procesar Peñaflor` mediante
**Terminal > Run Task** de VS Code.

Se incluye también una variante PowerShell. En equipos cuya política impide
ejecutar archivos `.ps1`, use el instalador `.cmd`, que es el predeterminado en
las tareas de VS Code.

Otros comandos:

```powershell
# Varias comunas
.\.venv\Scripts\python.exe -m sii_geometry scrape --comuna 14504 --comuna 5304

# Región completa
.\.venv\Scripts\python.exe -m sii_geometry scrape --region Metropolitana

# Estado del snapshot
.\.venv\Scripts\python.exe -m sii_geometry status --periodo 2026S2

# Respaldar manualmente todas las comunas terminadas
.\.venv\Scripts\python.exe -m sii_geometry publish --periodo 2026S2

# Respaldar sólo una comuna
.\.venv\Scripts\python.exe -m sii_geometry publish --periodo 2026S2 --comuna 14504

# Reprocesamiento explícito
.\.venv\Scripts\python.exe -m sii_geometry scrape --comuna 14504 --force
```
