# Prueba de geometrías prediales SII

Fecha de prueba: 2026-09-14.

Se evaluó el repositorio [`DanielD-S/sii-predios`](https://github.com/DanielD-S/sii-predios) sobre un sector acotado de Peñaflor. La red institucional inicialmente redirigía el visor a autenticación y los servicios respondían con error. Tras cambiar de conexión, los endpoints públicos volvieron a responder sin autenticación.

## Parámetros verificados

- Código SII: `14504`.
- Capa WMS: `sii:BR_CART_PENAFLOR_WMS`.
- Estilo: `PREDIOS_WMS_V0`.
- Zoom: `16`.
- Bbox solicitado: latitud `-33.6145` a `-33.6101`; longitud `-70.8947` a `-70.8893`.
- Descarga: un tile de 256 x 256 píxeles.

## Resultado

- 37.939 píxeles clasificados como superficie predial.
- 813 polígonos crudos.
- 772 polígonos después del filtro de área del scraper.
- 772 geometrías `Polygon`, válidas y no vacías, en EPSG:4326.
- Área mediana aproximada: 86,83 m² en UTM 19S.
- Una consulta puntual de control asoció correctamente un polígono al rol `662-33`.

La muestra web está en `public/data/predios_14504_muestra.geojson` (archivo local ignorado por Git). El GeoPackage y el GeoTIFF originales de la prueba están bajo `.tmp/sii-predios-source/output_sii/`.

## Reproducción

Desde el checkout temporal del repositorio externo:

```powershell
..\sii-venv\Scripts\python.exe sii_predios_completo.py `
  --comuna 14504 --zoom 16 --solo-wms `
  --lat-min -33.6145 --lon-min -70.8947 `
  --lat-max -33.6101 --lon-max -70.8893
```

## Limitaciones

- El WMS entrega una imagen; los polígonos se reconstruyen por clasificación de color, no se descargan como vectores oficiales.
- A zoom 16 los bordes tienen resolución de pantalla y no sirven como deslindes legales o topográficos.
- La API se comportó de forma intermitente; se requieren reintentos, pausas y checkpoints para una comuna completa.
- Los polígonos no traen el rol incorporado. Cada asociación necesita una consulta `getFeatureInfo` y una validación espacial posterior.
- El diccionario incluido en el repositorio externo no contiene Peñaflor, aunque el script acepta directamente el código `14504`.

## Validación cruzada

### Peñaflor: WMS/API 2026S2 contra CSV 2026S1

Se seleccionaron 40 polígonos distribuidos espacialmente dentro del tile y se consultó un punto interior de cada uno mediante `getFeatureInfo`.

- Respuestas API: 40/40.
- Roles únicos: 40.
- Roles encontrados en el CSV anterior: 39/40 (97,5%).
- Rol no encontrado: `95-90002`, compatible con un alta o cambio posterior al corte `2026S1`.
- Ubicación urbana/rural coincidente: 39/39.
- Direcciones exactamente iguales: 7/39; direcciones compatibles por prefijo: 39/39. El API agrega referencias de condominio que el CSV omite.
- Los avalúos no son idénticos entre semestres: en los 39 cruces el valor `2026S2` fue aproximadamente 1,028 veces el de `2026S1`.

Esta prueba confirma la asociación polígono–rol y la consistencia del microdato, pero el CSV no permite comparar el contorno porque no contiene geometría.

### Viña y Quilpué: GeoParquet 2026S1 contra CSV original 2026S1

El control leyó directamente el archivo fuente nacional `data/raw/catastral/catastro_2026_1 (2).csv` (aproximadamente 1,62 GB), no el CSV derivado de `data/processed`. El cruce completo de registros con clave y geometría obtuvo:

- Quilpué: 67.985 de 67.989 registros encontrados en el CSV original (99,994%).
- Viña del Mar: 232.412 de 232.422 registros encontrados (99,996%).
- El código de destino coincidió en todos los registros encontrados.

Además se validó una muestra reproducible de 1.000 geometrías por comuna:

- Geometrías válidas: 1.000/1.000 en ambas comunas.
- Intersección exacta del punto SII con el polígono: 89,6% en Quilpué y 89,0% en Viña.
- Distancia de hasta 1 metro: 99,8% en Quilpué y 100% en Viña.
- Distancia máxima observada: 5,21 m en Quilpué y 0,96 m en Viña.

Los casos sin intersección exacta están asociados principalmente al método `_match_method = nearest_10m`, declarado en el propio GeoParquet. El resultado respalda el uso analítico y cartográfico de las geometrías, manteniendo la advertencia de que no son deslindes legales.

## Control con el método Catastral

Se ejecutó sobre el mismo sector de Peñaflor y los mismos 40 roles el pipeline
publicado en [`crishernandezmaps/catastral.cl`](https://github.com/crishernandezmaps/catastral.cl),
checkout `82c6cf6942d33dc79033c08eb4a54a0f65b0e193` del 27 de junio de 2026.
La prueba utilizó cuatro superceldas de 1.024 x 1.024 píxeles a zoom 19,
ensambladas en una imagen de 2.048 x 2.048 píxeles.

### Código publicado, sin cambios

El vectorizador produjo solamente 4 polígonos y el matcher asignó 1 de los 40
roles (2,5%). La causa no fue una caída del SII: las imágenes contenían el mapa
predial completo. El código identifica el relleno urbano mediante rojo 160--200
y `alpha == 255`, pero la respuesta WMS observada usa para el relleno celeste
predominante RGBA `[182, 221, 232, 179]`. A la vez, el pipeline reserva
`alpha == 179` para el relleno agrícola. Por eso la capa urbana actual se
interpreta como grandes componentes agrícolas y no como predios individuales.

Este resultado es una incompatibilidad de clasificación con la simbología WMS
actual, no una estimación de cobertura nacional del método histórico.

### Adaptación mínima de compatibilidad

Para aislar el resto del método se mantuvieron las componentes conexas, filtros
de área y el matcher originales, reemplazando únicamente la detección del
relleno urbano por un rango RGB celeste tolerante a transparencia.

- Componentes celestes detectadas antes de filtrar: 1.965.
- Polígonos válidos después del filtro de área: 824/824.
- Roles asignados: 40/40 (100%).
- Polígonos distintos asignados: 40/40.
- Punto dentro del polígono: 21/40.
- Vecino más cercano: 19/40, con distancias entre 0 y 0,6 m y mediana de 0,3 m.
- Roles nuevamente contrastados con el CSV original 2026S1: 39/40.

El único rol ausente del CSV original sigue siendo `95-90002`, pero aparece en
el WMS/API 2026S2 y recibió un polígono propio en las dos vectorizaciones. Por
lo tanto, no corresponde contarlo como geometría perdida: es una diferencia
entre cortes semestrales del microdato.

Los GeoParquet ya disponibles para Viña del Mar y Quilpué contienen los campos
y métodos de matching del pipeline Catastral (`point_in_polygon`, `nearest_10m`,
herencia y polígonos huérfanos). En consecuencia, el control completo descrito
en la sección anterior también constituye una validación independiente de una
salida Catastral ya generada: 99,996% de cruce con el CSV original en Viña y
99,994% en Quilpué.

### Interpretación

La observación `39/40` de Peñaflor no debe extrapolarse como una pérdida del
2,5% de 9,5 millones de geometrías. El denominador mezclaba dos semestres y la
única ausencia comprobada está en el microdato antiguo, no en la geometría.
Para estimar una tasa nacional de omisión se necesita muestreo estratificado
por comuna y tipo urbano/rural, usando WMS, coordenadas y microdato del mismo
corte, además de separar explícitamente los estados `sin geometría`, `sin rol`
y `rol nuevo/eliminado entre semestres`.
