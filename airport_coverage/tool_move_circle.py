# -*- coding: utf-8 -*-
"""「拖动机位」工具：按住覆盖圆拖动，松手后按新圆心重建，半径恒为设定值。

性能设计（原实现的卡顿根源）：
    原来每次 mouseMove 都：新建 aeqd CRS + 2 个坐标转换 + buffer + 量面积 + 改图层
    几何和 4 个属性（每个都压一条撤销命令）+ triggerRepaint 整张地图。
    现在拖动过程中只移动一个 QgsRubberBand（纯画布叠加层，几乎零开销），
    松手时才真正重建一次几何并写入图层。

    抓取点保持不变：圆不会在按下的瞬间「跳」到光标中心。
"""

from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from .geodesy import from_wgs84, to_wgs84, transformer

PREVIEW_STROKE = QColor(245, 158, 11)
PREVIEW_FILL = QColor(245, 158, 11, 70)


class MoveCircleTool(QgsMapTool):
    circleMoved = pyqtSignal(int, float, float)  # fid, lon, lat

    def __init__(self, canvas, coverage, tol_px=12, notify=None):
        super().__init__(canvas)
        self.coverage = coverage
        self.tol_px = tol_px
        self._notify = notify or (lambda msg, level=0: None)
        self._reset_state()

        self.rubber = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber.setStrokeColor(PREVIEW_STROKE)
        self.rubber.setFillColor(PREVIEW_FILL)
        self.rubber.setWidth(2)
        self.setCursor(Qt.OpenHandCursor)

    def _reset_state(self):
        self._fid = None
        self._radius = None
        self._press = None  # 按下时的地图坐标
        self._base = None  # 按下时圆的几何（画布 CRS）
        self._center_map = None  # 按下时圆心（画布 CRS）
        self._delta = (0.0, 0.0)

    def _clear_preview(self):
        self.rubber.reset(QgsWkbTypes.PolygonGeometry)

    def _show(self, geom):
        try:
            self.rubber.setToGeometry(geom, None)
        except TypeError:
            self.rubber.setToGeometry(geom)

    # ---------- 事件 ----------

    def canvasPressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        lyr = self.coverage.layer
        if lyr is None:
            return

        map_crs = self.canvas().mapSettings().destinationCrs()
        point = self.toMapCoordinates(event.pos())
        fid = self.coverage.feature_at(point, map_crs, tol_px=self.tol_px, canvas=self.canvas())
        if fid is None:
            return

        try:
            center = self.coverage.center_of(fid)
            feat = lyr.getFeature(fid)
            if not center or not feat.isValid():
                return
            geom = QgsGeometry(feat.geometry())
            if lyr.crs() != map_crs:
                geom.transform(transformer(lyr.crs(), map_crs))

            self._fid = fid
            self._radius = self.coverage.radius_of(fid)
            self._base = geom
            self._press = QgsPointXY(point)
            self._center_map = from_wgs84(center[0], center[1], map_crs)
            self._delta = (0.0, 0.0)
            self._show(geom)
            self.setCursor(Qt.ClosedHandCursor)
        except Exception as exc:
            self._reset_state()
            self._notify("开始拖动出错：{}".format(exc), 2)

    def canvasMoveEvent(self, event):
        if self._fid is None:
            return
        p = self.toMapCoordinates(event.pos())
        dx, dy = p.x() - self._press.x(), p.y() - self._press.y()
        self._delta = (dx, dy)
        g = QgsGeometry(self._base)
        g.translate(dx, dy)
        self._show(g)

    def canvasReleaseEvent(self, event):
        if self._fid is None:
            return
        fid, radius, (dx, dy) = self._fid, self._radius, self._delta
        center_map = self._center_map
        self._reset_state()
        self._clear_preview()
        self.setCursor(Qt.OpenHandCursor)
        if dx == 0 and dy == 0:  # 只是点了一下，没拖
            return

        try:
            map_crs = self.canvas().mapSettings().destinationCrs()
            lon, lat = to_wgs84(QgsPointXY(center_map.x() + dx, center_map.y() + dy), map_crs)
            if self.coverage.update_circle(fid, lon, lat, radius):
                self.circleMoved.emit(fid, lon, lat)
        except Exception as exc:
            self._notify("移动机位出错：{}".format(exc), 2)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._fid is not None:  # Esc 取消本次拖动
            self._reset_state()
            self._clear_preview()
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
        else:
            super().keyPressEvent(event)

    def deactivate(self):
        self._reset_state()
        self._clear_preview()
        super().deactivate()
