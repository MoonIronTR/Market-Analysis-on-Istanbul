
from __future__ import annotations
import time
import contextlib
import numpy as np
import pandas as pd
import geopandas as gpd
import shapely
from shapely.geometry import Point
from sqlalchemy import create_engine
import statsmodels.api as sm
import folium



import time
from pathlib import Path
from typing import Optional, Tuple

import psycopg2
from shapely.geometry import MultiPolygon

# ───── AYARLAR ─────────────────────────────────────────────────────────────
DB_URI = "postgresql+psycopg2://postgres:1234@localhost:5432/anaproje"
GRID   = 100      # metre cinsinden ızgara adımı (kandidat nokta aralığı)
# ───────────────────────────────────────────────────────────────────────────

engine = create_engine(DB_URI, pool_pre_ping=True, echo=False)

@contextlib.contextmanager
def step(msg):
    t0 = time.perf_counter()
    yield
    print(f"{msg:<24}➜ {time.perf_counter() - t0:6.2f}s")

def read_gdf(sql):
    conn = engine.raw_connection()
    try:
        return gpd.read_postgis(sql, conn, geom_col="geom")
    finally:
        conn.close()

# Genel başlangıç zamanı
t0_all = time.perf_counter()

# ─── 1. VERİ OKUMA (mahalle_id sütun olarak) ───────────────────────────────
with step("SQL → mahalle"):
    mahalle = read_gdf("""
      SELECT
        m.id::text                 AS mahalle_id,
        m.name                     AS name,
        m.population::int          AS population,
        ROUND(CAST(ST_Area(m.geom::geography) / 1e6 AS numeric),4) AS alan_km2,
        CASE
          WHEN m.population IS NOT NULL
           AND ST_Area(m.geom::geography) > 0
          THEN ROUND(
            CAST(m.population AS numeric)
            / CAST(ST_Area(m.geom::geography) / 1e6 AS numeric)
          ,2)
          ELSE NULL
        END                         AS nufus_yogunlugu,
        m.geom
      FROM istanbul_mahalleler m
      WHERE m.population IS NOT NULL
    """)
    # mahalle_id artık sütun olarak var

with step("SQL → markets"):
    markets = read_gdf("""
      SELECT id, geom
      FROM istanbul_points
      WHERE other_tags LIKE '%"shop"=>"supermarket"%'
         OR other_tags LIKE '%"shop"=>"convenience"%';
    """)

with step("SQL → stops"):
    stops = read_gdf("""
      SELECT geom
      FROM istanbul_points
      WHERE other_tags LIKE '%"railway"=>"station"%'
         OR other_tags LIKE '%"railway"=>"tram_stop"%'
         OR highway='bus_stop';
    """)

with step("SQL → roads"):
    roads = read_gdf("""
      SELECT id, geom
      FROM istanbul_lines
      WHERE highway IN ('motorway','trunk','primary','secondary');
    """)

# Metre projeksiyona geç
for gdf in (mahalle, markets, stops, roads):
    gdf.to_crs(epsg=3857, inplace=True)

# ─── 2. Uzaklık yardımcıları ──────────────────────────────────────────────
T_MARKET = shapely.STRtree(markets.geometry.values)
T_STOP   = shapely.STRtree(stops.geometry.values)
T_ROAD   = shapely.STRtree(roads.geometry.values)

def nearest_dist(pt: shapely.geometry.base.BaseGeometry, tree, gdf):
    idx = tree.nearest(pt)
    return pt.distance(gdf.geometry.values[idx])

def avg3_market_dist(pt):
    ds = markets.geometry.distance(pt).values
    return np.mean(np.partition(ds, 3)[:3]) if len(ds) >= 3 else ds.mean()

# ─── 3. ÖZNİTELİKLER ─────────────────────────────────────────────────────
with step("Market sayısı"):
    # 1. ADIM: Uzamsal join
    sj = gpd.sjoin(mahalle, markets, how="left", predicate="contains")

    # 2. ADIM: SADECE EŞLEŞEN SATIRLARI say
    cnt = (sj["index_right"]                    # eşleşme var mı?
              .notna()                          # → True / False
              .groupby(sj["mahalle_id"])        # mahalle bazında grupla
              .sum()                            # True'ları topla
              .astype(int)                      # tipe çevir
              .rename("market_sayisi"))

    mahalle = mahalle.merge(cnt, on="mahalle_id", how="left")

# Eksik kalanlar 0 olsun
mahalle["market_sayisi"] = mahalle["market_sayisi"].fillna(0).astype(int)

# ─── 3.1 Durak Yoğunluğu (adet/km²) – DEĞİŞTİRİLDİ ─────────────────────────────────
with step("Durak yoğunluğu"):
    # Mahalle sınırları içinde kaç durak varsa sayıyoruz
    stop_join = gpd.sjoin(mahalle, stops, how="left", predicate="contains")
    stop_count = stop_join.groupby("mahalle_id").size().rename("durak_sayisi")
    mahalle = mahalle.merge(stop_count, on="mahalle_id", how="left")
    mahalle["durak_sayisi"] = mahalle["durak_sayisi"].fillna(0).astype(int)
    # Yoğunluk = durak sayısı / alan (km²)
    mahalle["durak_yogunlugu"] = (mahalle["durak_sayisi"] / mahalle["alan_km2"]).fillna(0)

# ─── 3.2 Yol Uzunluğu Yoğunluğu (km/km²) – DEĞİŞTİRİLDİ ───────────────────────────────────
with step("Yol yoğunluğu"):
    # Mahalle ile yolların kesişim geometrisini al
    roads_proj = roads.copy()
    roads_proj["length_km"] = roads_proj.geometry.length / 1000  # metre → km

    # Her yol parçasının mahalle ile kesişiminde yeni bir geometri oluştur
    yol_icerde = gpd.overlay(roads_proj, mahalle, how="intersection")
    # Mahalle bazında toplam yol uzunluğunu hesapla
    yol_uzunluk = yol_icerde.groupby("mahalle_id")["length_km"].sum().rename("yol_toplam_km")
    mahalle = mahalle.merge(yol_uzunluk, on="mahalle_id", how="left")
    mahalle["yol_toplam_km"] = mahalle["yol_toplam_km"].fillna(0)
    # Yoğunluk = mahalle içindeki yol uzunluğu (km) / alan (km²)
    mahalle["yol_yogunlugu"] = (mahalle["yol_toplam_km"] / mahalle["alan_km2"]).fillna(0)

# ─── 4. TALEP TAHMİNİ (Negatif Binom) ────────────────────────────────────
#  ---  Artık durak_uzak ve yol_uzak yerine durak_yogunlugu ve yol_yogunlugu kullanılıyor ---
X = mahalle[["population", "nufus_yogunlugu", "durak_yogunlugu", "yol_yogunlugu"]].copy()
X["const"] = 1.0  # sabit terim
cols = ["const", "population", "nufus_yogunlugu", "durak_yogunlugu", "yol_yogunlugu"]
X = X[cols]

y = mahalle["market_sayisi"]

with step("NegBin fit+predict"):
    model = sm.GLM(y, X, family=sm.families.NegativeBinomial()).fit()

     # ──> İşte buraya ekle:
    print("\n=== NegBin Coefficients (model.params) ===")
    print(model.params)        # Ham katsayılar
    print("\n=== Model Summary ===")
    print(model.summary())     # Detaylı özet (isteğe bağlı)

    # Belirli mahalleler için X, η ve μ değerlerini yazdıralım:
    targets = ["Kocasinan Merkez Mahallesi", "Başak Mahallesi", "Kayabaşı Mahallesi"]
    for name in targets:
        i = mahalle[mahalle["name"] == name].index
        if len(i)>0:
            idx = i[0]
            xi = X.loc[idx]
            eta = (xi * model.params).sum()
            mu  = np.exp(eta)
            print(f"\n-- {name} --")
            print("X :", xi.to_dict())
            print(f"η = X·β = {eta:.4f}")
            print(f"μ = exp(η) = {mu:.2f}")
            

    mahalle["expected_market"] = np.clip(model.predict(X), 0, None)

    # --- her mahallede en az 1 market olacak ---
    mahalle["expected_market"] = mahalle["expected_market"].clip(lower=1)

    mahalle["delta"] = (
        mahalle["expected_market"].round()
        - mahalle["market_sayisi"]
    ).astype(int)

# Delta (ihtiyaç fazlası / eksiği) ------------------------------------------------
mahalle["delta"]  = (mahalle["expected_market"] - mahalle["market_sayisi"]).round().astype(int)
mahalle["ekle"]   = mahalle["delta"].clip(lower=0)
mahalle["kapat"]  = (-mahalle["delta"]).clip(lower=0)

print("\n=== NegBin Coefficients ===")
for name, coef, p in zip(cols, model.params, model.pvalues):
    print(f"{name:18s}: {coef:9.4f}   p={p:.3g}")

# Basit metrik – Poisson/NB için R² anlamlı olmasa da genel fikir verir
from sklearn.metrics import mean_absolute_error, mean_squared_error
print("\n=== Regresyon Metrikleri (NegBin) ===")
print(f"MSE : {mean_squared_error(y, mahalle['expected_market']):.2f}")
print(f"MAE : {mean_absolute_error(y, mahalle['expected_market']):.2f}\n")


# ─── 5. NOKTA SEÇİM (HEURİSTİK) – İTERATİF AÇMA VE KAPAMA ──────────────────

def pick_open_iterative(poly, k):
    """
    İteratif greedy ekleme:
    Her adımda mevcut pazarlara (hem veritabanındaki hem eklenen) göre
    grid üzerindeki kandidat noktaların skorlarını hesaplar, en yüksek skorlu olanı seçer,
    ekler ve sonraki adımda yeniden skor hesaplar.
    """
    if k <= 0:
        return []

    # 1) Polygon içinde GRID tabanlı tüm kandidat noktaları üret
    minx, miny, maxx, maxy = poly.bounds
    xs = np.arange(minx, maxx, GRID)
    ys = np.arange(miny, maxy, GRID)
    candidates = [Point(x, y) for x in xs for y in ys if poly.contains(Point(x, y))]
    if not candidates:
        # Eğer hiç GRID noktası yoksa, polygon içindeki temsil noktasını k kez geri döndür
        rep_point = poly.representative_point()
        return [rep_point] * k

    opened = []
    # current_markets: iterasyonda dikkate alacağımız tüm varolan market noktaları (veritabanından + eklenen)
    current_markets_geoms = list(markets.geometry.values)

    for _ in range(int(k)):
        if not candidates:
            break

        # Her iterasyonda bir STRtree oluştur                   +
        tree_current = shapely.STRtree(current_markets_geoms)

        # Distans ve skor hesaplamaları
        dm_list = []
        ds_list = []
        dr_list = []
        for p in candidates:
            # nearest p -> mevcut market mesafesi
            dm = p.distance(current_markets_geoms[tree_current.nearest(p)])
            ds = nearest_dist(p, T_STOP, stops)
            dr = nearest_dist(p, T_ROAD, roads)
            dm_list.append(dm)
            ds_list.append(ds)
            dr_list.append(dr)

        # Normalize etme (0–1 aralığına çekme)
        dm_arr = np.array(dm_list)
        ds_arr = np.array(ds_list)
        dr_arr = np.array(dr_list)

        dm_range = np.ptp(dm_arr)
        ds_range = np.ptp(ds_arr)
        dr_range = np.ptp(dr_arr)

        dm_norm = (dm_arr - dm_arr.min()) / dm_range if dm_range != 0 else np.zeros_like(dm_arr, dtype=float)
        ds_norm = (ds_arr - ds_arr.min()) / ds_range if ds_range != 0 else np.zeros_like(ds_arr, dtype=float)
        dr_norm = (dr_arr - dr_arr.min()) / dr_range if dr_range != 0 else np.zeros_like(dr_arr, dtype=float)


        # Skor = 0.5*dm + 0.3*(1-ds) + 0.2*(1-dr)
        scores = 0.5 * dm_norm + 0.3 * (1 - ds_norm) + 0.2 * (1 - dr_norm)

        # En yüksek skorlu index
        idx_best = np.argmax(scores)
        best_pt = candidates[idx_best]

        opened.append(best_pt)
        current_markets_geoms.append(best_pt)  # sonraki iterasyonda eklenen de dikkate alınsın
        candidates.pop(idx_best)               # seçilen noktayı aday listesinden çıkar

    return opened


def pick_close_iterative_score(markets_in, k):
    """
    İteratif greedy kapama:
    Her adımda polygon içindeki mevcut pazar noktaları (markets_in) üzerinde
    (pazar-pazar ve erişim bilgilerine göre) skor hesaplar, en düşük skorlu olanı kapatır,
    iterasyonda tekrar skorları güncelleyerek devam eder.
    """
    if k <= 0 or markets_in.empty:
        return []

    remaining = markets_in.copy()
    removed = []

    for _ in range(int(k)):
        if remaining.empty:
            break

        geoms = list(remaining.geometry.values)
        n = len(geoms)

        # 1) Pazar-pazar mesafeleri (her nokta için en yakın diğer nokta mesafesi)
        dm_list = []
        for i, g in enumerate(geoms):
            # Diğer tüm remaining noktalarla mesafe listesi
            dists = [g.distance(h) for j, h in enumerate(geoms) if j != i]
            dm = min(dists) if dists else 0
            dm_list.append(dm)

        # 2) Pazar-durak ve pazar-yol mesafeleri
        ds_list = [nearest_dist(g, T_STOP, stops) for g in geoms]
        dr_list = [nearest_dist(g, T_ROAD, roads) for g in geoms]

        # Normalize etme
        dm_arr = np.array(dm_list)
        ds_arr = np.array(ds_list)
        dr_arr = np.array(dr_list)

        dm_range = np.ptp(dm_arr)
        ds_range = np.ptp(ds_arr)
        dr_range = np.ptp(dr_arr)

        dm_norm = (dm_arr - dm_arr.min()) / dm_range if dm_range != 0 else np.zeros_like(dm_arr, dtype=float)
        ds_norm = (ds_arr - ds_arr.min()) / ds_range if ds_range != 0 else np.zeros_like(ds_arr, dtype=float)
        dr_norm = (dr_arr - dr_arr.min()) / dr_range if dr_range != 0 else np.zeros_like(dr_arr, dtype=float)


        # Skor = 0.5*dm + 0.3*(1-ds) + 0.2*(1-dr)
        # Kapama yaparken en DÜŞÜK skorlu pazarları seçelim
        scores = 0.5 * dm_norm + 0.3 * (1 - ds_norm) + 0.2 * (1 - dr_norm)

        idx_worst = np.argmin(scores)
        geom_to_remove = geoms[idx_worst]

        removed.append(geom_to_remove)
        # remaining DataFrame'den bu index'i çıkar
        remaining = remaining.drop(remaining.index[idx_worst])

    return removed


# ─── 5.1 Skor Yazdırma Yardımcıları ────────────────────────────────────────

def print_market_scores(markets_in, k, label):
    """
    markets_in: GeoDataFrame
    k: kaç tane en düşük skorlu marketi yazdırmak istiyorsanız
    label: başlık
    """
    geoms = list(markets_in.geometry.values)
    if not geoms:
        print(f"{label}: pazar yok")
        return

    # 1) pazar–pazar
    dm = np.array([min([g.distance(h) for h in geoms if h!=g]) if len(geoms)>1 else 0 for g in geoms])
    # 2) pazar–durak, pazar–yol
    ds = np.array([nearest_dist(g, T_STOP, stops) for g in geoms])
    dr = np.array([nearest_dist(g, T_ROAD, roads) for g in geoms])
    # normalize
    def norm(a):
        r = np.ptp(a)
        return (a - a.min())/r if r!=0 else np.zeros_like(a)
    dm_n, ds_n, dr_n = norm(dm), norm(ds), norm(dr)
    scores = 0.5*dm_n + 0.3*(1-ds_n) + 0.2*(1-dr_n)

    # en düşük k skorlu index'ler
    worst_idx = np.argsort(scores)[:k]

    print(f"\n=== {label} (en düşük {k} skorlu pazar) ===")
    for idx in worst_idx:
        g = geoms[idx]
        sc = scores[idx]
        x,y = (g.x,g.y) if hasattr(g,"x") else (g.centroid.x,g.centroid.y)
        print(f"    Pazar@({x:.2f},{y:.2f}) → score={sc:.3f}")


def print_open_candidate_scores(poly, k, label):
    """
    poly: mahalle poligonu
    k: kaç tane en yüksek skorlu adayı yazdırmak istiyorsanız
    label: başlık
    """
    minx, miny, maxx, maxy = poly.bounds
    xs = np.arange(minx, maxx, GRID); ys = np.arange(miny, maxy, GRID)
    candidates = [Point(x,y) for x in xs for y in ys if poly.contains(Point(x,y))]
    if not candidates:
        print(f"{label}: aday yok"); return

    tree = shapely.STRtree(list(markets.geometry.values))
    dm = np.array([p.distance(markets.geometry.values[tree.nearest(p)]) for p in candidates])
    ds = np.array([nearest_dist(p, T_STOP, stops) for p in candidates])
    dr = np.array([nearest_dist(p, T_ROAD, roads) for p in candidates])
    def norm(a):
        r = np.ptp(a)
        return (a - a.min())/r if r!=0 else np.zeros_like(a)
    dm_n, ds_n, dr_n = norm(dm), norm(ds), norm(dr)
    scores = 0.5*dm_n + 0.3*(1-ds_n) + 0.2*(1-dr_n)

    # en yüksek k skorlu index'ler
    best_idx = np.argsort(scores)[-k:]

    print(f"\n=== {label} (en yüksek {k} skorlu aday) ===")
    for idx in best_idx:
        p = candidates[idx]
        sc = scores[idx]
        print(f"    Aday@({p.x:.2f},{p.y:.2f}) → score={sc:.3f}")


# ─── 5.2 Skorları Yazdır (sadece gerekli kadar) ───────────────────────────────────

# Seyrantepe Mahallesi
poly_sey   = mahalle[mahalle["name"]=="Seyrantepe Mahallesi"].geometry.iloc[0]
markets_sey= markets[markets.within(poly_sey)]
k_open     = int(mahalle.loc[mahalle["name"]=="Seyrantepe Mahallesi","ekle"].iloc[0])

print_market_scores(markets_sey, k_open, "Seyrantepe Mah. Kapatma Aday Pazar Skorları")
print_open_candidate_scores(poly_sey, k_open, "Seyrantepe Mah. Açma Aday Pazar Skorları")

# Harmandere Mahallesi
poly_har   = mahalle[mahalle["name"]=="Harmandere Mahallesi"].geometry.iloc[0]
markets_har= markets[markets.within(poly_har)]
k_close    = int(mahalle.loc[mahalle["name"]=="Harmandere Mahallesi","kapat"].iloc[0])

print_market_scores(markets_har, k_close, "Harmandere Mah. Kapatma Aday Pazar Skorları")
print_open_candidate_scores(poly_har, k_close, "Harmandere Mah. Açma Aday Pazar Skorları") 



# ─── 6. AÇ/KAPA NOKTALARI (GÜNCELLENDİ) ───────────────────────────────────
open_rows, close_rows = [], []
with step("AÇ/KAPA noktaları"):
    for r in mahalle.itertuples():
        poly = getattr(r, mahalle.geometry.name)

        # --- İTERATİF AÇMA ---
        if r.ekle > 0:
            opened_points = pick_open_iterative(poly, int(r.ekle))
            for p in opened_points:
                open_rows.append({"mahalle_id": r.mahalle_id, "geometry": p})

        # --- İTERATİF KAPAMA (Skorlara Bağlı) ---
        if r.kapat > 0:
            # Polygon içindeki mevcut pazarları çek
            m_in = markets[markets.within(poly)]
            if not m_in.empty:
                closed_points = pick_close_iterative_score(m_in, int(r.kapat))
                for p in closed_points:
                    close_rows.append({"mahalle_id": r.mahalle_id, "geometry": p})

open_gdf  = gpd.GeoDataFrame(open_rows, crs=mahalle.crs).to_crs(4326)
close_gdf = gpd.GeoDataFrame(close_rows, crs=mahalle.crs).to_crs(4326)



# ─── EK: Açılan ve Kapanan GeoJSON çıktılarını oluştur ───────────────────
open_gdf.to_file("acilan_marketler.geojson", driver="GeoJSON")
close_gdf.to_file("kapanan_marketler.geojson", driver="GeoJSON")

# ─── 8. ÖZET CSV ─────────────────────────────────────────────────────────
mahalle["market_son"]       = mahalle["market_sayisi"] + mahalle["ekle"] - mahalle["kapat"]
mahalle["pop_per_market"]   = (mahalle["population"] / mahalle["market_son"].replace(0, np.nan)).round()
mahalle["market_yogunlugu"] = (mahalle["market_son"] / mahalle["alan_km2"]).round(2)

OUT_COLS = [
    "mahalle_id", "name", "population", "nufus_yogunlugu",
    "market_sayisi", "ekle", "kapat",
    "market_son", "pop_per_market", "market_yogunlugu"
]
with step("CSV yazımı"):
    mahalle[OUT_COLS].to_csv("mahalle_ozet_nb2.csv", index=False, float_format="%.2f")


# ---------- AYARLAR (temp.py) ----------
DB_CONFIG = dict(
    host="localhost", port=5432,
    user="postgres",  password="1234",
    database="anaproje",
)
OUT_DIR = Path.cwd()

# ---------- YARDIMCILAR ----------
def read_gdf_pg(sql: str, params: Optional[Tuple] = None) -> gpd.GeoDataFrame:
    """Geometri içeren sorgular için GeoDataFrame."""
    with psycopg2.connect(**DB_CONFIG) as con:
        return gpd.read_postgis(sql, con, geom_col="geom", params=params)

def read_df_pg(sql: str, params: Optional[Tuple] = None) -> pd.DataFrame:
    """Geometri içermeyen sorgular için DataFrame."""
    with psycopg2.connect(**DB_CONFIG) as con:
        return pd.read_sql(sql, con, params=params)

def _numeric_cast(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        if df[c].dtype == "object":
            try:
                df[c] = pd.to_numeric(df[c])
            except ValueError:
                pass
    return df

# =================================================================
# 1) PROJEKSİYON  (mevcut – kapanan + açılan)
# =================================================================
def build_projected_markets() -> gpd.GeoDataFrame:
    sql = """
    SELECT p.id, p.geom
    FROM   istanbul_points p
    WHERE  p.other_tags LIKE '%"shop"=>"supermarket"%'
        OR p.other_tags LIKE '%"shop"=>"convenience"%';
    """
    mevcut = read_gdf_pg(sql).rename_geometry("geometry").set_crs(4326)

    acilan  = Path("acilan_marketler.geojson")
    kapanan = Path("kapanan_marketler.geojson")

    gdf_open  = gpd.read_file(acilan)  if acilan.exists()  else gpd.GeoDataFrame(geometry=[], crs=4326)
    gdf_close = gpd.read_file(kapanan) if kapanan.exists() else gpd.GeoDataFrame(geometry=[], crs=4326)

    # kapananları çıkar
    if not gdf_close.empty:
        s = gpd.sjoin(mevcut, gdf_close[["geometry"]],
                      how="left", predicate="intersects")
        mevcut = s[s.index_right.isna()].drop(columns="index_right")

    # açılanları ekle
    projected = pd.concat([mevcut[["geometry"]], gdf_open[["geometry"]]],
                          ignore_index=True)
    return gpd.GeoDataFrame(projected, geometry="geometry", crs=4326)


# ─── 7. HARİTA GÖRSELİ ───────────────────────────────────────────────────
with step("HTML harita yazımı"):
    # İstanbul merkezini ortalayacak basit bir OSM haritası
    m = folium.Map(
        location=[41.0082, 28.9784],  # Sultanahmet civarı
        zoom_start=11,
        tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="© OpenStreetMap"
    )

    # YEŞİL artı simgesi → açılacak marketler
    for _, r in open_gdf.iterrows():
        pt = r.geometry if r.geometry.geom_type == "Point" \
             else r.geometry.representative_point()
        folium.Marker(
            [pt.y, pt.x],
            icon=folium.Icon(color="green", icon="plus", prefix="fa"),
            tooltip=f"{r.mahalle_id} | +1"
        ).add_to(m)

    # KIRMIZI eksi simgesi → kapanacak marketler
    for _, r in close_gdf.iterrows():
        pt = r.geometry if r.geometry.geom_type == "Point" \
             else r.geometry.representative_point()
        folium.Marker(
            [pt.y, pt.x],
            icon=folium.Icon(color="red", icon="remove", prefix="fa"),
            tooltip=f"{r.mahalle_id} | -1"
        ).add_to(m)

    # Sonuç HTML'i kaydet
    m.save("market_karar_nb.html")





# =================================================================
# 2) MAHALLE ÖZETİ
# =================================================================
def _get_stop_counts() -> pd.DataFrame:
    sql = """
    SELECT
      m.id AS mahalle_id,
      SUM(CASE WHEN p.highway='bus_stop' THEN 1 ELSE 0 END)       AS bus,
      SUM(CASE WHEN p.other_tags ILIKE '%"station"%' THEN 1 ELSE 0 END)  AS metro,
      SUM(CASE WHEN p.other_tags ILIKE '%"tram_stop"%' THEN 1 ELSE 0 END) AS tram
    FROM   istanbul_mahalleler m
    JOIN   istanbul_points  p
      ON   ST_Contains(m.geom, p.geom)
    WHERE  m.population IS NOT NULL
      AND (
            p.highway='bus_stop'
         OR p.other_tags ILIKE '%"station"%'
         OR p.other_tags ILIKE '%"tram_stop"%'
      )
    GROUP  BY m.id;
    """
    return read_df_pg(sql)

def build_mahalle_summary(projected: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    # temel mahalle bilgileri + gerçek geography‐alan
    mahalle = read_gdf_pg("""
        SELECT
            id::text AS mahalle_id,
            name,
            population,
            ROUND(CAST(ST_Area(geom::geography)/1e6 AS numeric),4) AS alan_km2,
            geom
        FROM   istanbul_mahalleler
        WHERE  population IS NOT NULL
    """).rename_geometry("geometry").to_crs(3857)

    # roads sorgusu (geometri yok)
    roads = read_df_pg("""
        SELECT
            m.id AS mahalle_id,
            SUM(ST_Length(l.geom::geography))/1000.0 AS yol_toplam_km
        FROM   istanbul_mahalleler m
        JOIN   istanbul_lines l ON ST_Intersects(m.geom,l.geom)
        WHERE  l.highway IS NOT NULL
          AND  m.population IS NOT NULL
        GROUP  BY m.id
    """)
    mahalle = mahalle.merge(roads, on="mahalle_id", how="left")
    mahalle["yol_toplam_km"] = mahalle["yol_toplam_km"].fillna(0).round(3)

    # mevcut market sayısı (geometri yok)
    df_mevcut = read_df_pg("""
        SELECT
            m.id     AS mahalle_id,
            COUNT(*) AS mevcut
        FROM   istanbul_mahalleler m
        JOIN   istanbul_points p ON ST_Contains(m.geom,p.geom)
        WHERE (p.other_tags LIKE '%"shop"=>"supermarket"%'
               OR p.other_tags LIKE '%"shop"=>"convenience"%')
          AND m.population IS NOT NULL
        GROUP  BY m.id
    """)
    mahalle = mahalle.merge(df_mevcut, on="mahalle_id", how="left")
    mahalle["mevcut"] = mahalle["mevcut"].fillna(0).astype(int)

    # açılan/kapanan marketler
    mahalle_4326 = mahalle.to_crs(4326)[["mahalle_id","geometry"]]
    def _count_from(fp: Path, col: str) -> pd.DataFrame:
        if not fp.exists():
            return pd.DataFrame(columns=["mahalle_id",col])
        gdf = gpd.read_file(fp).to_crs(4326)
        if "mahalle_id" in gdf.columns:
            return gdf.groupby("mahalle_id").size().rename(col).reset_index()
        joined = gpd.sjoin(gdf, mahalle_4326, how="left",
                           predicate="within")
        if "mahalle_id_mah" in joined.columns:
            joined = joined.rename(columns={"mahalle_id_mah":"mahalle_id"})
        return (joined.dropna(subset=["mahalle_id"])
                      .groupby("mahalle_id")
                      .size().rename(col).reset_index())

    mahalle = mahalle.merge(_count_from(Path("acilan_marketler.geojson"), "ekle"),
                            on="mahalle_id", how="left") \
                     .merge(_count_from(Path("kapanan_marketler.geojson"), "kapat"),
                            on="mahalle_id", how="left")
    mahalle[["ekle","kapat"]] = mahalle[["ekle","kapat"]].fillna(0).astype(int)

    # durak/metro/tram sayıları
    stops = _get_stop_counts()
    mahalle = mahalle.merge(stops, on="mahalle_id", how="left")
    for c in ["bus","metro","tram"]:
        mahalle[c] = mahalle[c].fillna(0).astype(int)
    mahalle["stops_total"] = mahalle[["bus","metro","tram"]].sum(axis=1)

    # nihai market sayısı ve yol/metre per market
    mahalle["market_son"] = (
        mahalle["mevcut"] + mahalle["ekle"] - mahalle["kapat"]
    ).astype(int)
    mahalle["yol_per_market_km"] = (
        mahalle["yol_toplam_km"] / mahalle["market_son"].replace(0, np.nan)
    ).round(3)

    # sütun seçimi ve GeoJSON’a yaz
    cols = [
        "mahalle_id","name","population","alan_km2",
        "mevcut","ekle","kapat","market_son",
        "yol_toplam_km","yol_per_market_km",
        "bus","metro","tram","stops_total","geometry"
    ]
    mahalle = mahalle[cols]
    mahalle["geometry"] = mahalle.to_crs(3857).geometry.centroid.to_crs(4326)
    mahalle = _numeric_cast(mahalle)
    mahalle.to_file(OUT_DIR/"mahalle_summary.geojson", driver="GeoJSON")
    return mahalle



# =================================================================
# 3) İLÇE + İSTANBUL ÖZETİ
# =================================================================
def build_ilce_summary(mahalle_sum: gpd.GeoDataFrame) -> None:
    ilce = read_gdf_pg("""
        SELECT id AS ilce_id,
               name AS ilce_name,
               geom
        FROM   istanbul_ilceler;
    """).rename_geometry("geometry").to_crs(3857)

    ilce = ilce[ilce.geometry.type.isin(["Polygon","MultiPolygon"])].copy()
    ilce["geometry"] = ilce.geometry.apply(lambda g: MultiPolygon([g]) if g.geom_type=="Polygon" else g)

    inter = gpd.overlay(mahalle_sum.to_crs(3857), ilce, how="intersection")

    agg = (inter.groupby(["ilce_id","ilce_name"])
                 .agg(population    = ("population","sum"),
                      mevcut        = ("mevcut","sum"),
                      market_son    = ("market_son","sum"),
                      ekle          = ("ekle","sum"),
                      kapat         = ("kapat","sum"),
                      yol_toplam_km = ("yol_toplam_km","sum"),
                      bus           = ("bus","sum"),
                      metro         = ("metro","sum"),
                      tram          = ("tram","sum"))
                 .reset_index())
    agg["stops_total"] = agg["bus"] + agg["metro"] + agg["tram"]
    agg["yol_per_market_km"] = (agg["yol_toplam_km"] / agg["market_son"].replace(0,np.nan)).round(3)

    ilce_out = ilce.merge(agg, on="ilce_id")
    ilce_out["geometry"] = ilce_out.geometry.centroid.to_crs(4326)
    ilce_out = _numeric_cast(ilce_out)
    ilce_out.to_file(OUT_DIR/"ilce_summary.geojson", driver="GeoJSON")

    ist = dict(
        population        = agg["population"].sum(),
        markets_now       = agg["mevcut"].sum(),
        opened            = agg["ekle"].sum(),
        closed            = agg["kapat"].sum(),
        markets_projected = agg["market_son"].sum(),
        road_km           = agg["yol_toplam_km"].sum().round(3),
        bus               = agg["bus"].sum(),
        metro             = agg["metro"].sum(),
        tram              = agg["tram"].sum(),
        stops_total       = agg["stops_total"].sum(),
    )
    pd.DataFrame([ist]).to_csv(OUT_DIR/"istanbul_summary.csv", index=False)

# =================================================================
# 4) TEMP BLOĞU ANA ÇAĞRI (ver7 sonuna ek)
# =================================================================
if __name__ == "__main__":
    print("\n─── Ek temp.py özet üretimi ───")
    t_temp = time.perf_counter()
    proj   = build_projected_markets()
    mah    = build_mahalle_summary(proj)
    build_ilce_summary(mah)
    print(f"temp-özetler: {time.perf_counter()-t_temp:5.2f}s")


print(f"\nToplam çalışma süresi: {time.perf_counter() - t0_all:6.2f}s")