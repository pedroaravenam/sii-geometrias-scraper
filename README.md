# Scraper de geometrías prediales SII

Pipeline reproducible para descargar la cartografía predial vigente publicada
por el SII, vectorizarla, consultar los atributos prediales y generar un
GeoParquet por comuna. La captura queda identificada como `2026S2`.

## Requisitos

- Windows 10 u 11.
- Python 3.11 o superior.
- Git, sólo para clonar y actualizar el repositorio.
- El CSV original del catastro `2026_1`, que no se distribuye en este repositorio.

## Instalación y ejecución

Abra `cmd` o PowerShell y ejecute:

```bat
git clone https://github.com/pedroaravenam/sii-geometrias-scraper.git
cd sii-geometrias-scraper
SCRAPEAR_GEOMETRIAS.cmd
```

En la primera ejecución el programa:

1. Crea `.venv` e instala automáticamente las dependencias.
2. Abre un selector para elegir una carpeta local, OneDrive o Google Drive donde guardar resultados.
3. Solicita el CSV original `catastro_2026_1*.csv`.
4. Permite seleccionar una región y una o más comunas.

Las rutas elegidas se guardan sólo en `config/sii_geometry.local.json`, archivo
ignorado por Git. Una comuna completa se omite en ejecuciones posteriores; el
avance parcial queda en checkpoints y puede reanudarse.

También puede procesarse una comuna directamente:

```bat
SCRAPEAR_GEOMETRIAS.cmd --comuna 14504
```

O una región completa:

```bat
SCRAPEAR_GEOMETRIAS.cmd --region "Metropolitana de Santiago"
```

## Resultados

La carpeta elegida contiene:

- `2026S2/geoparquet`: geometrías y atributos por comuna.
- `2026S2/respuestas_api`: respuestas consultadas al SII, comprimidas.
- `2026S2/checkpoints`: estado recuperable por comuna.
- `2026S2/metadatos`: manifiestos y métricas de calidad.
- `catalogo/estado_geometrias.csv`: comunas pendientes y procesadas.

Los datos, microdatos y resultados están excluidos del repositorio. Si varias
personas colaboran, asigne regiones distintas y reúna después sus carpetas de
resultados. Para reducir carga sobre el SII, evite lanzar varios procesos desde
la misma conexión simultáneamente.

Consulte [la documentación técnica](docs/PIPELINE_GEOMETRIAS_SII.md) para ver el
flujo, los estados y los controles de calidad.

## Fuente y uso responsable

El programa consulta servicios públicos del Servicio de Impuestos Internos de
Chile. No es un producto oficial del SII. Respete las condiciones aplicables,
mantenga la pausa configurada entre solicitudes y no publique atributos
personales o prediales sin revisar su finalidad y base jurídica.
