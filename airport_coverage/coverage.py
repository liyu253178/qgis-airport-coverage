# -*- coding: utf-8 -*-
"""机位覆盖圆图层管理。

存储策略（用户确认）：memory 图层 + 面板「导出机位方案」按钮导出 GeoPackage。
memory 图层关闭工程即丢失，所以面板上常驻导出按钮，并在关闭前提示。

坐标系策略：图层 CRS = 工程 CRS（工程无空间参考则为 WGS84），
但圆的几何始终用测地线生成（见 geodesy.geodesic_circle），保证半径是精确的地面 5 km。
"""

import os

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .geodesy import (
    DEFAULT_RADIUS_M,
    fmt_area,
    geodesic_circle,
    measure_area,
    project_crs,
    to_wgs84,
)

LAYER_NAME = "机位覆盖圆"
FIELDS = (
    ("name", "机位名称", False),
    ("radius_m", "半径(米)", True),
    ("lon", "圆心经度", True),
    ("lat", "圆心纬度", True),
    ("area_km2", "面积(km²)", True),
)


def _make_field(key, is_number):
    """跨 QGIS 版本构造字段（3.30+ 推荐 QMetaType，旧版用 QVariant）。"""
    try:
        from qgis.PyQt.QtCore import QMetaType

        return QgsField(key, QMetaType.Type.Double if is_number else QMetaType.Type.QString)
    except Exception:
        from qgis.PyQt.QtCore import QVariant

        return QgsField(key, QVariant.Double if is_number else QVariant.String)


class CoverageLayer:
    """管理「机位覆盖圆」memory 图层。"""

    def __init__(self, project=None):
        self.project = project or QgsProject.instance()
        self._layer = None
        self.last_error = None

    # ---------- 图层生命周期 ----------

    @property
    def layer(self):
        if self._layer is not None and self._layer.isValid():
            return self._layer
        found = self.project.mapLayersByName(LAYER_NAME)
        self._layer = found[0] if found else None
        return self._layer

    def ensure_layer(self, crs=None):
        """取得图层，不存在则创建（CRS 跟随工程）。"""
        lyr = self.layer
        if lyr is not None:
            return lyr

        crs = crs or project_crs()
        lyr = QgsVectorLayer("Polygon?crs={}".format(crs.authid() or "EPSG:4326"), LAYER_NAME, "memory")
        if not crs.authid():
            lyr.setCrs(crs)
        lyr.dataProvider().addAttributes([_make_field(k, n) for k, _, n in FIELDS])
        lyr.updateFields()
        lyr.setRenderer(QgsSingleSymbolRenderer(self._default_symbol()))
        self.project.addMapLayer(lyr)
        # 保险：确保图层在图层树里是勾选可见的（否则会出现「图层在、但什么都看不到」）
        node = self.project.layerTreeRoot().findLayer(lyr)
        if node is not None:
            node.setItemVisibilityChecked(True)
        self._layer = lyr
        return lyr

    @staticmethod
    def _default_symbol():
        return QgsFillSymbol.createSimple(
            {
                "color": "30,144,255,55",
                "color_border": "30,144,255,220",
                "width_border": "0.6",
                "style": "solid",
            }
        )

    def _idx(self, key):
        return self.layer.fields().indexOf(key)

    def _ensure_editable(self):
        if not self.layer.isEditable():
            self.layer.startEditing()
        return True

    def commit(self):
        if self.layer is not None and self.layer.isEditable():
            return self.layer.commitChanges()
        return True

    # ---------- 增删改 ----------

    def add_circle(self, lon, lat, radius_m=DEFAULT_RADIUS_M, name=None):
        """在指定圆心加一个覆盖圆，返回要素 id。"""
        lyr = self.ensure_layer()
        geom = geodesic_circle(lon, lat, radius_m, target_crs=lyr.crs())
        area = measure_area(geom, lyr.crs()) / 1e6

        if name is None:
            name = "机位{}".format(lyr.featureCount() + 1)

        feat = QgsFeature(lyr.fields())
        feat.setGeometry(geom)
        feat.setAttributes([name, float(radius_m), float(lon), float(lat), float(area)])

        # 注意两点：
        # 1) QgsVectorLayer.addFeature(feat) 只返回 bool，拿不到 id；
        # 2) 编辑缓冲区里拿到的是临时负 id（如 -2），提交后才会变成真实 id。
        # 所以这里添加后立刻提交，用「提交前后的 id 差集」定位新要素。
        before = {f.id() for f in lyr.getFeatures()}
        self._ensure_editable()
        ok = lyr.addFeature(feat)
        committed = lyr.commitChanges()
        lyr.triggerRepaint()

        self.last_error = None
        if not ok:
            self.last_error = "图层拒绝了写入（可能是几何或字段不匹配）"
            return None
        if not committed:
            self.last_error = "图层 {} 提交失败：{}".format(LAYER_NAME, lyr.commitErrors() or "未知原因")
            return None

        new_ids = {f.id() for f in lyr.getFeatures()} - before
        return sorted(new_ids)[-1] if new_ids else None

    def update_circle(self, fid, lon, lat, radius_m):
        """把圆挪到新圆心（半径不变）。拖动时调用。"""
        lyr = self.layer
        if lyr is None:
            return False
        geom = geodesic_circle(lon, lat, radius_m, target_crs=lyr.crs())
        area = measure_area(geom, lyr.crs()) / 1e6

        self._ensure_editable()
        lyr.changeGeometry(fid, geom)
        lyr.changeAttributeValue(fid, self._idx("radius_m"), float(radius_m))
        lyr.changeAttributeValue(fid, self._idx("lon"), float(lon))
        lyr.changeAttributeValue(fid, self._idx("lat"), float(lat))
        lyr.changeAttributeValue(fid, self._idx("area_km2"), float(area))
        lyr.triggerRepaint()
        return True

    def set_name(self, fid, name):
        if self.layer is None:
            return
        self._ensure_editable()
        self.layer.changeAttributeValue(fid, self._idx("name"), name)

    def remove_features(self, fids):
        lyr = self.layer
        if lyr is None or not fids:
            return 0
        self._ensure_editable()
        ok = lyr.deleteFeatures(list(fids))
        lyr.triggerRepaint()
        return len(fids) if ok else 0

    # ---------- 查询 ----------

    def feature_at(self, point_map_crs, map_crs, tol_px=12, canvas=None):
        """找出点击位置下的圆。

        :param point_map_crs: 画布坐标系下的点
        :param map_crs: 画布坐标系
        :param tol_px: 命中容差（像素）
        """
        lyr = self.layer
        if lyr is None:
            return None

        tol = self._tolerance(lyr.crs(), map_crs, tol_px, canvas, point_map_crs)
        pt_layer = self._to_layer_crs(point_map_crs, map_crs, lyr.crs())
        click = QgsGeometry.fromPointXY(pt_layer)

        best_fid, best_dist = None, None
        for feat in lyr.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            if geom.contains(pt_layer):
                return feat.id()
            d = geom.distance(click)
            if d <= tol and (best_dist is None or d < best_dist):
                best_fid, best_dist = feat.id(), d
        return best_fid

    def _to_layer_crs(self, point, src_crs, dst_crs):
        if src_crs.authid() and src_crs.authid() == dst_crs.authid():
            return QgsPointXY(point)
        from .geodesy import transformer

        return transformer(src_crs, dst_crs).transform(QgsPointXY(point))

    def _tolerance(self, layer_crs, map_crs, tol_px, canvas, origin):
        """把像素容差换算成图层单位。"""
        from .geodesy import measurer

        try:
            if canvas is not None:
                tr = canvas.getCoordinateTransform()
                p1 = tr.toMapCoordinates(0, 0)
                p2 = tr.toMapCoordinates(int(tol_px), 0)
            else:
                p1, p2 = origin, QgsPointXY(origin.x() + 1e-5, origin.y())
            a = self._to_layer_crs(p1, map_crs, layer_crs)
            b = self._to_layer_crs(p2, map_crs, layer_crs)
            d = measurer(layer_crs).measureLine([a, b])
            return max(d, 1e-9)
        except Exception:
            return 1e-5

    def center_of(self, fid):
        """返回 (lon, lat)。"""
        lyr = self.layer
        if lyr is None:
            return None
        feat = lyr.getFeature(fid)
        if not feat.isValid():
            return None
        lon = feat.attribute("lon")
        lat = feat.attribute("lat")
        if lon is not None and lat is not None:
            return float(lon), float(lat)
        c = feat.geometry().centroid().asPoint()
        return to_wgs84(c, lyr.crs())

    def radius_of(self, fid, default=DEFAULT_RADIUS_M):
        lyr = self.layer
        if lyr is None:
            return default
        feat = lyr.getFeature(fid)
        if not feat.isValid():
            return default
        r = feat.attribute("radius_m")
        return float(r) if r not in (None, "") else default

    def selected_ids(self):
        lyr = self.layer
        return [] if lyr is None else list(lyr.selectedFeatureIds())

    def features(self):
        lyr = self.layer
        return [] if lyr is None else list(lyr.getFeatures())

    # ---------- 统计 ----------

    def statistics(self):
        """返回机位统计：数量、面积总和、并集面积、重叠面积。"""
        lyr = self.layer
        out = {"count": 0, "sum_area": 0.0, "union_area": 0.0, "overlap_area": 0.0}
        if lyr is None:
            return out

        geoms = [f.geometry() for f in lyr.getFeatures() if f.geometry() and not f.geometry().isEmpty()]
        out["count"] = len(geoms)
        if not geoms:
            return out

        out["sum_area"] = sum(measure_area(g, lyr.crs()) for g in geoms)
        merged = QgsGeometry.unaryUnion(geoms)
        if merged and not merged.isEmpty():
            out["union_area"] = measure_area(merged, lyr.crs())
        out["overlap_area"] = max(out["sum_area"] - out["union_area"], 0.0)
        return out

    def stats_text(self):
        s = self.statistics()
        if s["count"] == 0:
            return "暂无机位"
        ratio = (s["overlap_area"] / s["sum_area"] * 100) if s["sum_area"] else 0.0
        return "机位 {} 个 | 净覆盖 {} | 重叠 {}（{:.1f}%）".format(
            s["count"], fmt_area(s["union_area"]), fmt_area(s["overlap_area"]), ratio
        )

    # ---------- 导出 ----------

    def export_gpkg(self, path):
        """导出为 GeoPackage（memory 图层关闭即丢，需显式导出）。"""
        lyr = self.layer
        if lyr is None:
            return False, "尚无「{}」图层可导出".format(LAYER_NAME)
        if lyr.featureCount() == 0:
            return False, "图层里没有机位，无需导出"

        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            return False, "目录不存在：{}".format(folder)

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.fileEncoding = "UTF-8"
        options.layerName = LAYER_NAME

        if os.path.exists(path):
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer

        err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
            lyr, path, self.project.transformContext(), options
        )
        if err != QgsVectorFileWriter.NoError:
            return False, "导出失败：{}".format(msg or err)
        return True, "已导出 {}（{} 个机位）".format(path, lyr.featureCount())


def crs_warning():
    """工程 CRS 为 Web Mercator 时给出提示。"""
    crs = project_crs()
    if crs.authid() == "EPSG:3857":
        return (
            "当前工程坐标系是 Web Mercator (EPSG:3857)，5 km 覆盖圆会显示成南北拉长的椭圆"
            "（墨卡托的正常现象，不是画错了）。若想看到正圆，建议把工程 CRS 设为 "
            "EPSG:4326 或当地高斯/UTM 带。"
        )
    return None
