# Scraper de geometrías prediales SII

Pipeline reproducible para descargar la cartografía predial vigente publicada
por el SII, vectorizarla, consultar los atributos prediales y generar un
GeoParquet por comuna. La captura queda identificada como `2026S2`.

## Requisitos

- Windows 10 u 11.
- Python 3.11 o superior.
- Conexión a internet.

Git no es necesario para ejecutar el programa.

## Instalación y ejecución

### Opción simple, sin Git

1. Descargue el programa desde
   [este archivo ZIP](https://github.com/pedroaravenam/sii-geometrias-scraper/archive/refs/heads/main.zip).
2. Extraiga completamente el ZIP en una carpeta local.
3. Abra la carpeta `sii-geometrias-scraper-main`.
4. Ejecute `SCRAPEAR_GEOMETRIAS.cmd` con doble clic.

No ejecute el archivo directamente dentro del ZIP: primero debe extraer la
carpeta completa.

### Alternativa con Git

Si Git está disponible, abra `cmd` o PowerShell y ejecute:

```bat
git clone https://github.com/pedroaravenam/sii-geometrias-scraper.git
cd sii-geometrias-scraper
.\SCRAPEAR_GEOMETRIAS.cmd
```

En la primera ejecución el programa:

1. Crea `.venv` e instala automáticamente las dependencias.
2. Abre un selector para elegir una carpeta local, OneDrive o Google Drive donde guardar resultados.
3. Permite seleccionar una región y una o más comunas.
4. Descarga automáticamente desde Google Drive el Parquet histórico completo de
   la región seleccionada.

Los insumos corresponden al catastro `2026S1`, conservan sus 39 columnas y se
publican separadamente por región en la
[carpeta pública de Google Drive](https://drive.google.com/drive/folders/1mIjnsXy3t7xs3BR-sDdlg1F8wBNqUrBp?usp=sharing),
que es la única fuente usada automáticamente para estos archivos.
El programa verifica tamaño, SHA-256, esquema, número de filas y presencia de las
comunas solicitadas. Después reutiliza el archivo guardado en
`<carpeta elegida>/insumos/2026S1`, por lo que no vuelve a descargarlo.

El catálogo SII de comunas y los límites comunales vienen incluidos dentro de
`resources/reference`; el programa no intenta descargarlos desde GitHub. Una vez
instaladas las dependencias Python, durante el procesamiento sólo se conecta a
Google Drive para obtener el Parquet regional y a los servicios públicos del SII.

Las rutas elegidas se guardan sólo en `config/sii_geometry.local.json`, archivo
ignorado por Git. Una comuna completa se omite en ejecuciones posteriores; el
avance parcial queda en checkpoints y puede reanudarse.

También puede procesarse una comuna directamente:

```bat
.\SCRAPEAR_GEOMETRIAS.cmd --comuna 14504
```

O una región completa:

```bat
.\SCRAPEAR_GEOMETRIAS.cmd --region "Metropolitana de Santiago"
```

Si una actualización cambia la lógica de asociación, puede recalcular el vínculo
entre roles y polígonos reutilizando los tiles, vectores y respuestas guardadas:

```bat
.\SCRAPEAR_GEOMETRIAS.cmd --comuna 5101 --rematch
```

Si ya dispone del catastro original en CSV o Parquet, puede usarlo en vez de la
descarga automática:

```bat
.\SCRAPEAR_GEOMETRIAS.cmd --comuna 14504 --reference-csv "D:\Microdatos\catastro_2026_1.parquet"
```

## Resultados

La carpeta elegida contiene:

- `2026S2/geoparquet`: geometrías y atributos por comuna.
- `2026S2/respuestas_api`: respuestas consultadas al SII, comprimidas.
- `2026S2/checkpoints`: estado recuperable por comuna.
- `2026S2/metadatos`: manifiestos y métricas de calidad.
- `catalogo/estado_geometrias.csv`: comunas pendientes y procesadas.

Los resultados están excluidos del repositorio. Si varias personas colaboran,
asigne regiones distintas y reúna después sus carpetas de resultados. Para
reducir carga sobre el SII, evite lanzar varios procesos desde la misma conexión
simultáneamente.

Consulte [la documentación técnica](docs/PIPELINE_GEOMETRIAS_SII.md) para ver el
flujo, los estados y los controles de calidad.

## Fuente y uso responsable

El programa consulta servicios públicos del Servicio de Impuestos Internos de
Chile. No es un producto oficial del SII. Respete las condiciones aplicables,
mantenga la pausa configurada entre solicitudes y no publique atributos
personales o prediales sin revisar su finalidad y base jurídica.
