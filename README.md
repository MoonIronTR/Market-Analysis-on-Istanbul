# Market-Analysis-on-Istanbul
# README – Istanbul Market Analysis Setup & Run Guide 

## 1. Requirements

- PostgreSQL 14+
- PostGIS 3+
- GDAL / ogr2ogr
- osmconvert, osmfilter
- Python 3.11+
- (Optional) QGIS 3+

Python dependencies:

    pip install geopandas pandas shapely sqlalchemy psycopg2 statsmodels folium PyQt5

---

## 2. Database Setup

In PostgreSQL:

    CREATE DATABASE anaproje;
    \c anaproje;
    CREATE EXTENSION postgis;

Connection settings used in the code (update if needed):

    host="localhost"
    port=5432
    user="postgres"
    password="1234"
    database="anaproje"

---

## 3. Prepare Istanbul OSM Data

### 3.1 Download Turkey .pbf

Go to:

    https://download.geofabrik.de/europe/turkey.html

Download:

    turkey-latest.osm.pbf

### 3.2 Convert .pbf → .osm (osmconvert)

    osmconvert turkey-latest.osm.pbf -o=turkey.osm

### 3.3 Filter Istanbul only (osmfilter)

    osmfilter turkey.osm --keep="name=İstanbul" -o=istanbul.osm

---

## 4. Import OSM Data into PostGIS (ogr2ogr)

### 4.1 Point data → istanbul_points

    ogr2ogr -f "PostgreSQL" \
      PG:"dbname=anaproje user=postgres password=1234 host=localhost port=5432" \
      istanbul.osm points \
      -nln istanbul_points \
      -lco GEOMETRY_NAME=geom \
      -nlt POINT

### 4.2 Line data → istanbul_lines

    ogr2ogr -f "PostgreSQL" \
      PG:"dbname=anaproje user=postgres password=1234 host=localhost port=5432" \
      istanbul.osm lines \
      -nln istanbul_lines \
      -lco GEOMETRY_NAME=geom \
      -nlt LINESTRING

Quick checks:

    SELECT COUNT(*) FROM istanbul_points;
    SELECT COUNT(*) FROM istanbul_lines;

---

## 5. District & Neighborhood Boundaries (Overpass Turbo → PostGIS)

### 5.1 Neighborhood boundaries (admin_level = 8)

Open:

    https://overpass-turbo.eu/

Run:

    [out:json][timeout:300];
    area["name"="İstanbul"]["boundary"="administrative"]->.istanbul;
    relation
      ["boundary"="administrative"]
      ["admin_level"="8"]
      (area.istanbul);
    out geom;

Export as `istanbul_mahalleler.geojson`, then import:

    ogr2ogr -f "PostgreSQL" \
      PG:"dbname=anaproje user=postgres password=1234 host=localhost port=5432" \
      istanbul_mahalleler.geojson \
      -nln istanbul_mahalleler \
      -nlt MULTIPOLYGON

### 5.2 District boundaries (admin_level = 6, optional)

Use `admin_level="6"` in Overpass, export `istanbul_ilceler.geojson`, then:

    ogr2ogr -f "PostgreSQL" \
      PG:"dbname=anaproje user=postgres password=1234 host=localhost port=5432" \
      istanbul_ilceler.geojson \
      -nln istanbul_ilceler \
      -nlt MULTIPOLYGON

---

## 6. Load TUIK Population Data

### 6.1 Load CSV into a temporary table

Download Istanbul neighborhood population from TUIK (MEDAS) as `nufus.csv`.

    CREATE TABLE mahalle_nufus_raw (
      district     text,
      neighborhood text,
      population   integer
    );

    \copy mahalle_nufus_raw FROM 'nufus.csv' CSV HEADER ENCODING 'UTF8';

### 6.2 Join with istanbul_mahalleler

    ALTER TABLE istanbul_mahalleler
      ADD COLUMN IF NOT EXISTS population integer;

    UPDATE istanbul_mahalleler m
    SET population = r.population
    FROM mahalle_nufus_raw r
    WHERE lower(m.name) = lower(r.neighborhood);

Check:

    SELECT COUNT(*) FROM istanbul_mahalleler WHERE population IS NOT NULL;

---

## 7. Run the Project

### 7.1 Run analysis

    python algorithm.py

Expected outputs:

- acilan_marketler.geojson
- kapanan_marketler.geojson
- projected_marketler.geojson
- mahalle_summary.geojson
- ilce_summary.geojson
- market_karar_nb.html

### 7.2 Run GUI (optional)

    python main.py

Use the GUI to select districts / neighborhoods and visualize layers (e.g., via QGIS integration).

---

## 8. Quick Checklist

- Database created and PostGIS enabled?
- `istanbul_points`, `istanbul_lines`, `istanbul_mahalleler`, `istanbul_ilceler` populated?
- `istanbul_mahalleler.population` filled from TUIK?
- Python dependencies installed?
- `algorithm.py` runs without errors?

If all answers are “yes”, the project is ready and should run correctly.
