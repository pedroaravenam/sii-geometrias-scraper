# Roadmap — sii-geometrias-scraper

## Fase actual: Robustez de descargas y reintento diferido (2026S2)
- [ ] Validar e integrar reintento diferido de superceldas WMS fallidas sin detener el recorrido comunal.
- [ ] Ejecutar y certificar extracción completa de la comuna piloto Peñaflor (`14504`).
- [ ] Validar tasa de match punto-en-polígono y vecino más cercano sobre el dataset 2026S2.

## Próximas fases
### Cobertura regional y comunas prioritarias
- Procesamiento de comunas de la Región Metropolitana y regiones piloto seleccionadas.
- Monitoreo de estabilidad y pausas de los endpoints WMS y API `getPredioNacional` ante scraping continuo.
- Generación de paquetes GeoParquet y métricas de calidad por comuna para sincronización en almacenamiento central.

### Automatización y control de calidad
- Control cruzado de atributos 2026S2 contra series históricas 2026S1.
- Detección y clasificación de roles nuevos o modificados entre semestres.
- Consolidación y publicación de catálogos comunales actualizados.

## Ideas sin compromiso
- Exportación a formatos espaciales adicionales (GeoPackage, FlatGeobuf).
- Visualizador liviano de polígonos no asociados o huérfanos.
- Paralelización multi-nodo o distribución por IPs para diferentes regiones.
