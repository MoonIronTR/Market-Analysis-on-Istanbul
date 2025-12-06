# empty_layers_with_dummy.py
from pathlib import Path
import geopandas as gpd
from shapely.geometry import Point, LineString, Polygon
import pandas as pd
import os, tempfile, time

# Katmanların bulunduğu klasör
DATA_DIR = Path(r"C:\Users\Arınç\Desktop\ara proje\data_layers")

# Bu dosyalar daima 'Point' tipinde tutulacak
FORCE_POINT = {"mah_sum.geojson", "ilce_sum.geojson"}

def dummy(typ: str):
    """Görünmez, harita dışında yer-tutucu geometri döner."""
    if typ.startswith("Line"):
        return LineString([(9_999_999, 9_999_999), (10_000_000, 10_000_000)])
    if typ.startswith("Poly"):
        return Polygon([
            (9_999_999, 9_999_999),
            (9_999_999, 10_000_000),
            (10_000_000, 10_000_000),
            (10_000_000, 9_999_999),
            (9_999_999, 9_999_999)
        ])
    return Point(9_999_999, 9_999_999)          # varsayılan Point

for fp in DATA_DIR.glob("*.geojson"):
    gdf = gpd.read_file(fp)

    # ---------- geometri tipi ----------
    if fp.name in FORCE_POINT:
        gtype = "Point"
    else:
        gtype = gdf.geom_type.iloc[0] if not gdf.empty else "Point"

    # ---------- boş GDF + placeholder ----------
    empty = gdf.head(0).copy()                  # şema + CRS
    if "placeholder" not in empty.columns:
        empty["placeholder"] = pd.Series(dtype="int64")

    place = empty.iloc[:0].copy()
    place.loc[0] = [None] * len(empty.columns)
    place["placeholder"] = 1
    place[empty.geometry.name] = dummy(gtype)

    new_gdf = gpd.GeoDataFrame(
        pd.concat([place], ignore_index=True),
        geometry=empty.geometry.name,
        crs=empty.crs or 4326
    )

    # ---------- atomik replace ----------
    tmp = Path(tempfile.gettempdir()) / (fp.stem + ".tmp.geojson")
    new_gdf.to_file(tmp, driver="GeoJSON")
    for _ in range(4):
        try:
            os.replace(tmp, fp)
            break
        except PermissionError:          # dosya QGIS tarafından kilitliyse
            time.sleep(0.3)

    print(f"Boşaltıldı ({gtype}): {fp}")

print("\nTüm katmanlar yer-tutucu ile sıfırlandı – QGIS tipi korur.")
