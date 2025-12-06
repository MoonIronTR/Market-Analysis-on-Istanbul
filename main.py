# gui8.py — İstanbul İlçe / Mahalle Analiz Aracı
# =============================================

# -------- 0) Standart -------------------------
import sys
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List
import shapely
from shapely.geometry import Point, LineString, Polygon

# -------- 1) 3-parti --------------------------
import psycopg2
import geopandas as gpd
import pandas as pd

from PyQt5.QtCore import QCoreApplication, Qt
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QPushButton, QMessageBox,
    QDialog, QCheckBox, QGroupBox, QStatusBar, QSizePolicy
)
from PyQt5.QtGui import QFont


# -------- 2) Stil -----------------------------
STYLE = """
QWidget               { font-family:'Segoe UI'; font-size:10pt; }
QPushButton           { background-color:#3A7BD5; color:white;
                        border:1px solid #3A7BD5; border-radius:6px;
                        padding:6px 12px; }
QPushButton:hover     { background-color:#5596E6; }
QPushButton:disabled  { background-color:#CCCCCC; color:#666666; }
QListWidget           { border:1px solid #DDDDDD; }
QGroupBox             { border:1px solid #AAAAAA; border-radius:8px;
                        margin-top:6px; }
QGroupBox::title      { subcontrol-origin:margin; left:8px; padding:0 4px; }
"""

# -------- 3) Ayarlar --------------------------
DB_CONFIG = dict(host="localhost",
                 port=5432,
                 user="postgres",
                 password="1234",
                 database="anaproje")

QGIS_BIN_PATH = r"C:\Program Files\QGIS 3.42.0\bin\qgis-bin.exe"
QGIS_PROJECT_PATH = r"C:\Users\Arınç\Desktop\ara proje\istanbul_analiz.qgz"
# gui8.py başında TEMP_DIR tanımı:
DATA_DIR = Path(r"C:\Users\Arınç\Desktop\ara proje\data_layers")
DATA_DIR.mkdir(exist_ok=True)
TEMP_DIR = DATA_DIR          # tüm _write_geojson çağrıları burayı kullanıyor


# -------- 4) QGIS Oturumu ---------------------
QGIS_PROC = None

def launch_or_update_qgis(layer_paths: List[str]) -> None:
    """"
    global QGIS_PROC
    if QGIS_PROC and QGIS_PROC.poll() is None:
        return
    cmd = [QGIS_BIN_PATH, QGIS_PROJECT_PATH] + layer_paths
    QGIS_PROC = subprocess.Popen(cmd)
    
"""
    return
# -------- 5) Veri tabanı yardımcı -------------
def _get_con():
    return psycopg2.connect(**DB_CONFIG)

def get_ilceler() -> List[str]:
    with _get_con() as con, con.cursor() as cur:
        cur.execute("SELECT name FROM istanbul_ilceler WHERE name IS NOT NULL ORDER BY name;")
        rows = cur.fetchall()
    return ["Tüm İstanbul"] + [r[0] for r in rows]

def get_mahalleler(ilceler: List[str]) -> List[str]:
    if ilceler == ["Tüm İstanbul"]:
        return ["Tüm Mahalleler"]
    with _get_con() as con, con.cursor() as cur:
        ph = ",".join(["%s"] * len(ilceler))
        cur.execute(f"""
            SELECT m.name      AS mahalle,
                   i.name      AS ilce
            FROM   istanbul_mahalleler m
            JOIN   istanbul_ilceler  i ON ST_Contains(i.geom,m.geom)
            WHERE  i.name IN ({ph})  AND m.population IS NOT NULL
            ORDER  BY i.name, m.name;""", tuple(ilceler))
        rows = cur.fetchall()
    # --- Liste öğesini  “Mahalle (İlçe)” biçiminde döndür ---
    return ["Tüm Mahalleler"] + [f"{r[0]} ({r[1]})" for r in rows]


def read_sql(sql: str, params: tuple | None):
    with _get_con() as con:
        return gpd.read_postgis(sql, con, geom_col="geom", params=params)

# ===================================================================
# 6) ActionPanel
# ===================================================================
class ActionPanel(QDialog):
    def __init__(self, ilceler: List[str], mahalleler: List[str]) -> None:
        super().__init__()
        self.ilceler = ilceler
        self.mahalleler = mahalleler
        self.layer_paths: List[str] = []
        self.setWindowTitle("Katman Seçimi")
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        grp = QGroupBox("Gösterilecek Katmanlar")
        vgrp = QVBoxLayout(grp)
        self.cb_area = QCheckBox("Mahalle Alanı")
        self.cb_roads = QCheckBox("Yollar")
        self.cb_stops = QCheckBox("Toplu Taşıma Durakları")
        self.cb_markets = QCheckBox("Mevcut Marketler")
        self.cb_open = QCheckBox("Açılan Marketler")
        self.cb_close = QCheckBox("Kapanan Marketler")
        self.cb_proj = QCheckBox("Projeksiyon Marketler")
        self.cb_mah_sum = QCheckBox("Mahalle Özeti")
        self.cb_ilce_sum = QCheckBox("İlçe Özeti")
        for cb in (self.cb_area, self.cb_roads, self.cb_stops, self.cb_markets,
                   self.cb_open, self.cb_close, self.cb_proj,
                   self.cb_mah_sum, self.cb_ilce_sum):
            vgrp.addWidget(cb)
        btn_exec = QPushButton("🛠  Sorguyu Çalıştır")
        btn_exec.clicked.connect(self.execute_queries)
        btn_back = QPushButton("Kapat")
        btn_back.clicked.connect(self.close)
        vbox = QVBoxLayout(self)
        vbox.addWidget(grp)
        vbox.addWidget(btn_exec)
        vbox.addWidget(btn_back)

    # ---------- Yardımcılar ----------
    def _mah_by_ilce(self):
        out = {}
        for lab in self.mahalleler:
            if lab == "Tüm Mahalleler":
                continue
            name, ilc = lab.rsplit(" (", 1)
            ilc = ilc.rstrip(")")
            out.setdefault(ilc, []).append(name)
        return out

    def _build_where_pairs(self, table_alias="m"):
        pairs = [(lab.rsplit(" (", 1)[0], lab.rsplit(" (", 1)[1][:-1])
                 for lab in self.mahalleler if lab != "Tüm Mahalleler"]
        if not pairs:
            return "", []
        conds = []
        params = []
        for mah, ilc in pairs:
            conds.append(f"(i.name=%s AND {table_alias}.name=%s)")
            params.extend([ilc, mah])
        return " OR ".join(conds), params
    

    def _dummy_geom(self, gdf: gpd.GeoDataFrame):
        """Yer-tutucu: dosyanın geometri tipine uygun, harita dışında görünmez."""
        gtype = "Point"
        if not gdf.empty:
            gtype = gdf.geom_type.iloc[0]
        if gtype.startswith("Line"):
            return LineString([(9_999_999, 9_999_999), (10_000_000, 10_000_000)])
        if gtype.startswith("Poly"):
            return Polygon([
                (9_999_999, 9_999_999), (9_999_999, 10_000_000),
                (10_000_000,10_000_000), (10_000_000, 9_999_999),
                (9_999_999, 9_999_999)])
        return Point(9_999_999, 9_999_999)          # varsayılan Point
    

    # ---------- Atomik GeoJSON ----------
    def _write_geojson(self, gdf: gpd.GeoDataFrame, path: Path) -> None:
        tmp = path.with_suffix(".NEW.geojson")
        gdf.to_file(tmp, driver="GeoJSON")

        for _ in range(4):
            try:
                os.replace(tmp, path)          # atomik
                break
            except PermissionError:
                try:
                    shutil.copyfile(tmp, path) # yedek yol
                    break
                except PermissionError:
                    time.sleep(0.3)

        os.utime(path, None)   # ←  ❶ mtime’i şimdiye çek (gui7’deki satır)

        if str(path) not in self.layer_paths:
            self.layer_paths.append(str(path))

        tmp.unlink(missing_ok=True)


    def _clear_geojson(self, prefix: str) -> None:
        path = TEMP_DIR / f"{prefix}.geojson"

        # 1) Dosya varsa şemayı oku, yoksa boş şema oluştur
        if path.exists():
            gdf = gpd.read_file(path)
        else:
            gdf = gpd.GeoDataFrame(geometry=[], crs=4326)

        # 2) placeholder sütunu ekle (yoksa)
        if "placeholder" not in gdf.columns:
            gdf["placeholder"] = pd.Series(dtype="int64")

        # 3) Tek satırlık görünmez yer-tutucu
        row = {c: None for c in gdf.columns}
        row["placeholder"] = 1
        row[gdf.geometry.name] = self._dummy_geom(gdf)
        dummy = gpd.GeoDataFrame([row], columns=gdf.columns,
                                 geometry=gdf.geometry.name,
                                 crs=gdf.crs or 4326)

        # 4) Yaz
        self._write_geojson(dummy, path)

    def _run_to_geojson(self, sql: str, params: tuple | None, prefix: str) -> None:
        gdf = read_sql(sql, params)
        if not gdf.empty:
            self._write_geojson(gdf.to_crs(4326), TEMP_DIR / f"{prefix}.geojson")

    # ---------- Sorgular ----------
    def _query_area(self) -> None:
        sel = ("SELECT m.name, "
               "ROUND((ST_Area(m.geom::geography)/1e6)::numeric,2) AS alan_km2, "
               "m.geom ")
        if self.ilceler == ["Tüm İstanbul"] and self.mahalleler == ["Tüm Mahalleler"]:
            self._run_to_geojson(sel + "FROM istanbul_mahalleler m WHERE m.population IS NOT NULL",
                                 None, "mahalle")
            return
        where_pairs, prms = self._build_where_pairs()
        if where_pairs:
            sql = (sel +
                   "FROM istanbul_mahalleler m "
                   "JOIN istanbul_ilceler i ON ST_Contains(i.geom,m.geom) "
                   f"WHERE {where_pairs}")
            self._run_to_geojson(sql, tuple(prms), "mahalle")
            return
        ilc = [i for i in self.ilceler if i != "Tüm İstanbul"]
        sql = (sel +
               "FROM istanbul_mahalleler m "
               "JOIN istanbul_ilceler i ON ST_Contains(i.geom,m.geom) "
               "WHERE i.name = ANY(%s) AND m.population IS NOT NULL")
        self._run_to_geojson(sql, (ilc,), "mahalle")

    def _query_roads(self) -> None:
        base = "SELECT l.id,l.geom,l.highway FROM istanbul_lines l "
        cond = "l.highway IS NOT NULL"
        if self.ilceler == ["Tüm İstanbul"] and self.mahalleler == ["Tüm Mahalleler"]:
            self._run_to_geojson(base + "WHERE " + cond, None, "roads")
            return
        where_pairs, prms = self._build_where_pairs("m")
        if where_pairs:
            sql = (base +
                   "JOIN istanbul_mahalleler m ON ST_Intersects(l.geom,m.geom) "
                   "JOIN istanbul_ilceler  i ON ST_Contains(i.geom,m.geom) "
                   f"WHERE ({where_pairs}) AND {cond}")
            self._run_to_geojson(sql, tuple(prms), "roads")
            return
        ilc = [i for i in self.ilceler if i != "Tüm İstanbul"]
        sql = (base +
               "JOIN istanbul_ilceler i ON ST_Intersects(l.geom,i.geom) "
               "WHERE i.name = ANY(%s) AND " + cond)
        self._run_to_geojson(sql, (ilc,), "roads")

    def _query_stops(self) -> None:
        filt = ("p.other_tags LIKE '%%\"railway\"=>\"station\"%%' OR "
                "p.other_tags LIKE '%%\"railway\"=>\"tram_stop\"%%' OR "
                "p.highway='bus_stop'")
        base = "SELECT p.id,p.geom FROM istanbul_points p "
        if self.ilceler == ["Tüm İstanbul"] and self.mahalleler == ["Tüm Mahalleler"]:
            self._run_to_geojson(base + "WHERE " + filt, None, "stops")
            return
        where_pairs, prms = self._build_where_pairs("m")
        if where_pairs:
            sql = (base +
                   "JOIN istanbul_mahalleler m ON ST_Contains(m.geom,p.geom) "
                   "JOIN istanbul_ilceler  i ON ST_Contains(i.geom,m.geom) "
                   f"WHERE ({where_pairs}) AND ({filt})")
            self._run_to_geojson(sql, tuple(prms), "stops")
            return
        ilc = [i for i in self.ilceler if i != "Tüm İstanbul"]
        sql = (base +
               "JOIN istanbul_ilceler i ON ST_Contains(i.geom,p.geom) "
               "WHERE i.name = ANY(%s) AND (" + filt + ")")
        self._run_to_geojson(sql, (ilc,), "stops")

    def _query_markets(self) -> None:
        filt = ("p.other_tags LIKE '%%\"shop\"=>\"supermarket\"%%' OR "
                "p.other_tags LIKE '%%\"shop\"=>\"convenience\"%%'")
        base = "SELECT p.id,p.geom FROM istanbul_points p "
        if self.ilceler == ["Tüm İstanbul"] and self.mahalleler == ["Tüm Mahalleler"]:
            self._run_to_geojson(base + "WHERE " + filt, None, "markets")
            return
        where_pairs, prms = self._build_where_pairs("m")
        if where_pairs:
            sql = (base +
                   "JOIN istanbul_mahalleler m ON ST_Contains(m.geom,p.geom) "
                   "JOIN istanbul_ilceler  i ON ST_Contains(i.geom,m.geom) "
                   f"WHERE ({where_pairs}) AND ({filt})")
            self._run_to_geojson(sql, tuple(prms), "markets")
            return
        ilc = [i for i in self.ilceler if i != "Tüm İstanbul"]
        sql = (base +
               "JOIN istanbul_ilceler i ON ST_Contains(i.geom,p.geom) "
               "WHERE i.name = ANY(%s) AND (" + filt + ")")
        self._run_to_geojson(sql, (ilc,), "markets")

    # ---------- GeoJSON filtreleyici ----------
    def _filter_geojson(self, path: str | Path):
        path = Path(path)
        if not path.exists():
            return None
        gdf = gpd.read_file(path).set_crs(4326)
        if self.ilceler == ["Tüm İstanbul"] and self.mahalleler == ["Tüm Mahalleler"]:
            return gdf
        where_pairs, prms = self._build_where_pairs("m")
        if where_pairs:
            with _get_con() as con:
                sin = gpd.read_postgis(
                    "SELECT m.geom FROM istanbul_mahalleler m "
                    "JOIN istanbul_ilceler i ON ST_Contains(i.geom,m.geom) "
                    f"WHERE {where_pairs}", con, geom_col="geom", params=tuple(prms))
        else:
            ilc = [i for i in self.ilceler if i != "Tüm İstanbul"]
            with _get_con() as con:
                sin = gpd.read_postgis(
                    "SELECT geom FROM istanbul_ilceler WHERE name = ANY(%s)",
                    con, geom_col="geom", params=(ilc,))
        if sin.crs is None:
            sin = sin.set_crs(4326)
        return gpd.sjoin(gdf, sin, how="inner", predicate="intersects")

    # ---------- Açılan / Kapanan ----------
    def _add_open_or_close(self, geojson_name: str, prefix: str) -> None:
        gdf = self._filter_geojson(geojson_name)
        if gdf is None or gdf.empty:
            self._clear_geojson(prefix)
            return
        self._write_geojson(gdf, TEMP_DIR / f"{prefix}.geojson")

    # ---------- Projeksiyon -----------------
    def _add_projected(self) -> None:
        where_pairs, prms = self._build_where_pairs("m")
        filt = (" AND (p.other_tags LIKE '%%\"shop\"=>\"supermarket\"%%' "
                "      OR p.other_tags LIKE '%%\"shop\"=>\"convenience\"%%')")
        if where_pairs:
            sql = ("SELECT p.id,p.geom FROM istanbul_points p "
                   "JOIN istanbul_mahalleler m ON ST_Contains(m.geom,p.geom) "
                   "JOIN istanbul_ilceler  i ON ST_Contains(i.geom,m.geom) "
                   f"WHERE ({where_pairs})" + filt)
            params = tuple(prms)
        elif self.ilceler != ["Tüm İstanbul"]:
            ilc = [i for i in self.ilceler if i != "Tüm İstanbul"]
            sql = ("SELECT p.id,p.geom FROM istanbul_points p "
                   "JOIN istanbul_ilceler i ON ST_Contains(i.geom,p.geom) "
                   "WHERE i.name = ANY(%s)" + filt)
            params = (ilc,)
        else:
            sql = "SELECT p.id,p.geom FROM istanbul_points p WHERE " + filt.lstrip(" AND")
            params = None
        with _get_con() as con:
            mevcut = gpd.read_postgis(sql, con, geom_col="geom", params=params)
        if mevcut.empty:
            self._clear_geojson("projected")
            return
        mevcut = mevcut.set_crs(4326)
        kap = self._filter_geojson("kapanan_marketler.geojson")
        if kap is None or kap.empty:
            kap = gpd.GeoDataFrame(geometry=[], crs=4326)
        ac = self._filter_geojson("acilan_marketler.geojson")
        if ac is None or ac.empty:
            ac = gpd.GeoDataFrame(geometry=[], crs=4326)
        merged = gpd.sjoin(mevcut, kap[["geometry"]], how="left", predicate="intersects")
        kalan = merged[merged.index_right.isna()].drop(columns="index_right")
        if not ac.empty:
            ac = ac.rename(columns={"geometry": "geom"})
            ac["id"] = [f"yeni_{i}" for i in range(len(ac))]
            proj_df = pd.concat([kalan, ac[["id", "geom"]]], ignore_index=True)
        else:
            proj_df = kalan
        if proj_df.empty:
            self._clear_geojson("projected")
            return
        self._write_geojson(gpd.GeoDataFrame(proj_df, geometry="geom", crs=4326),
                            TEMP_DIR / "projected.geojson")

    # ---------- Özet kopyalayıcı -------------
    def _copy_geojson(self, src_name: str, prefix: str) -> None:
        gdf = self._filter_geojson(src_name)
        if gdf is None or gdf.empty:
            self._clear_geojson(prefix)
            return
        self._write_geojson(gdf, TEMP_DIR / f"{prefix}.geojson")

    # ---------- Execute ----------------------
    def execute_queries(self) -> None:
        self.layer_paths.clear()
        sel = set()
        if self.cb_area.isChecked():
            self._query_area(); sel.add("mahalle")
        if self.cb_roads.isChecked():
            self._query_roads(); sel.add("roads")
        if self.cb_stops.isChecked():
            self._query_stops(); sel.add("stops")
        if self.cb_markets.isChecked():
            self._query_markets(); sel.add("markets")
        if self.cb_open.isChecked():
            self._add_open_or_close("acilan_marketler.geojson", "acilan"); sel.add("acilan")
        if self.cb_close.isChecked():
            self._add_open_or_close("kapanan_marketler.geojson", "kapanan"); sel.add("kapanan")
        if self.cb_proj.isChecked():
            self._add_projected(); sel.add("projected")
        if self.cb_mah_sum.isChecked():
            self._copy_geojson("mahalle_summary.geojson", "mah_sum"); sel.add("mah_sum")
        if self.cb_ilce_sum.isChecked():
            self._copy_geojson("ilce_summary.geojson", "ilce_sum"); sel.add("ilce_sum")
        for pfx in {"mahalle","roads","stops","markets",
                    "acilan","kapanan","projected",
                    "mah_sum","ilce_sum"} - sel:
            self._clear_geojson(pfx)
        if not sel:
            QMessageBox.warning(        # önce 'Bilgi' idi → 'Uyarı' grubuna alındı
                self,
                "Uyarı",
                "Katman panelinde en az bir katman seçmelisiniz."
            )
            return
        launch_or_update_qgis(self.layer_paths)


# ===================================================================
# 7) Ana Pencere
# ===================================================================
class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("İstanbul İlçe / Mahalle Analiz Aracı")
        self._algo_running = False
        self._build_ui()

    def _build_ui(self) -> None:
        left_ratio = 0.5
        list_ratio = 0.5
        self.ilce = QListWidget()
        self.ilce.setSelectionMode(QListWidget.MultiSelection)
        for i in get_ilceler():
            QListWidgetItem(i, self.ilce)
        ilc_box = QGroupBox("İlçeler")
        vilc = QVBoxLayout(ilc_box)
        vilc.addWidget(QLabel("Çoklu seçim için Ctrl / Shift"))
        vilc.addWidget(self.ilce)
        self.mah = QListWidget()
        self.mah.setSelectionMode(QListWidget.MultiSelection)
        mah_box = QGroupBox("Mahalleler")
        vmah = QVBoxLayout(mah_box)
        vmah.addWidget(QLabel("Önce ilçe seçin"))
        vmah.addWidget(self.mah)
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(ilc_box)
        self.split.addWidget(mah_box)
        self._left_ratio = left_ratio
        self.resizeEvent(None)
        btn_load = QPushButton("Mahalleleri Yükle")
        btn_load.clicked.connect(self.load_mahalleler)
        self.btn_algo = QPushButton("Algoritmayı Çalıştır (Tüm İstanbul)")
        self.btn_algo.clicked.connect(self.run_algorithm)
        btn_panel = QPushButton("Katman Paneli ➜")
        btn_panel.clicked.connect(self.open_action_panel)
        hbtn = QHBoxLayout()
        hbtn.addWidget(btn_load)
        hbtn.addWidget(self.btn_algo)
        hbtn.addWidget(btn_panel)
        self.status = QStatusBar()
        self.status.showMessage("Hazır")
        vmain = QVBoxLayout(self)
        vmain.addWidget(self.split)
        vmain.addLayout(hbtn)
        vmain.addWidget(self.status)
        vmain.setStretch(0, int(list_ratio * 100))
        self.ilce.itemSelectionChanged.connect(self._check_ilce_selection)   # (+)
        self.mah.itemSelectionChanged.connect(self._check_mah_selection)     # (+)

    def resizeEvent(self, e):
        w = self.width()
        self.split.setSizes([int(w * self._left_ratio), w - int(w * self._left_ratio)])
        super().resizeEvent(e)

    def load_mahalleler(self) -> None:
        self.mah.clear()
        ilc = [i.text() for i in self.ilce.selectedItems()]
        if not ilc:
            QMessageBox.warning(self, "Uyarı", "En az bir ilçe seçin.")
            return
        for item in get_mahalleler(ilc):
            QListWidgetItem(item, self.mah)

    # MainWindow sınıfında, open_action_panel'den önce bir yere ekleyin
    def _check_ilce_selection(self):                                     # (+)
        """'Tüm İstanbul' ile başka ilçe aynı anda seçildiyse engelle + uyar."""
        items = self.ilce.selectedItems()
        if any(i.text() == "Tüm İstanbul" for i in items) and len(items) > 1:
            for it in items:
                if it.text() != "Tüm İstanbul":
                    it.setSelected(False)                                # izin verme
            QMessageBox.warning(self, "Uyarı",
                                "'Tüm İstanbul' seçiliyken başka ilçe seçilemez.")

    def _check_mah_selection(self):                                      # (+)
        """'Tüm Mahalleler' ile başka mahalle aynı anda seçildiyse engelle + uyar."""
        items = self.mah.selectedItems()
        if any(m.text() == "Tüm Mahalleler" for m in items) and len(items) > 1:
            for it in items:
                if it.text() != "Tüm Mahalleler":
                    it.setSelected(False)
            QMessageBox.warning(self, "Uyarı",
                                "'Tüm Mahalleler' seçiliyken başka mahalle seçilemez.")        

    def _parse_selected_mahalleler(self):
        """
        Dönüş: dict  { ilce_adı : [mahalle_adları] }
        'Tüm Mahalleler' seçiliyse boş dict döner.
        """
        sel = [m.text() for m in self.mah.selectedItems()]
        if not sel or sel == ["Tüm Mahalleler"]:
            return {}
        out = {}
        for label in sel:
            # "Atatürk (Adalar)" -> ("Atatürk", "Adalar")
            name, ilce = label.rsplit(" (", 1)
            ilce = ilce.rstrip(")")
            out.setdefault(ilce, []).append(name)
        return out        

    def run_algorithm(self) -> None:
        if self._algo_running:
            return
        self._algo_running = True
        self.btn_algo.setEnabled(False)
        self.status.showMessage("Algoritma çalışıyor…")
        start = time.perf_counter()
        proc = subprocess.Popen([sys.executable, "ver7.py"])
        while proc.poll() is None:
            QCoreApplication.processEvents()
            time.sleep(0.1)
        dt = time.perf_counter() - start
        QMessageBox.information(self, "Tamam", f"Algoritma {dt:.1f} sn")
        self.status.showMessage("Algoritma bitti")
        self.btn_algo.setEnabled(True)
        self._algo_running = False
        
    def open_action_panel(self) -> None:
        # 1) Her iki listede de mutlaka seçim olmalı (+)
        if not self.ilce.selectedItems() or not self.mah.selectedItems():
            QMessageBox.warning(
                self,
                "Uyarı",
                "Katman panelini açmak için hem ilçe hem de mahalle seçmelisiniz."
            )
            return

        # 2) 'Tüm İstanbul' + başka ilçe kombinasyonunu engelle (+)
        if any(i.text() == "Tüm İstanbul" for i in self.ilce.selectedItems()) \
        and len(self.ilce.selectedItems()) > 1:
            QMessageBox.warning(
                self,
                "Uyarı",
                "'Tüm İstanbul' seçiliyken başka ilçe seçilemez."
            )
            return

        # 3) 'Tüm Mahalleler' + başka mahalle kombinasyonunu engelle (+)
        if any(m.text() == "Tüm Mahalleler" for m in self.mah.selectedItems()) \
        and len(self.mah.selectedItems()) > 1:
            QMessageBox.warning(
                self,
                "Uyarı",
                "'Tüm Mahalleler' seçiliyken başka mahalle seçilemez."
            )
            return

        # 4) Geçerli seçimle ActionPanel aç
        ilc = [i.text() for i in self.ilce.selectedItems()]
        mah = [m.text() for m in self.mah.selectedItems()]
        ActionPanel(ilc, mah).exec_()

        
# ===================================================================
# 8) main
# ===================================================================
if __name__ == "__main__":
    QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Segoe UI", 10))
    win = MainWindow()
    win.resize(900, 650)
    win.show()
    sys.exit(app.exec_())
