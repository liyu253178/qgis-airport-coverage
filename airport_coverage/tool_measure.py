# -*- coding: utf-8 -*-
"""线测量 / 面积测量工具。

量算走 QgsDistanceArea + 显式椭球（跟随工程椭球，缺省 WGS84）。
注意：不设椭球时 QGIS 会把经纬度当笛卡尔坐标算，结果毫无意义
（实测北京→上海会返回 10.1）。
"""

from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from .geodesy import fmt_area, fmt_length, measure_area, measure_length, measure_perimeter

LINE_COLOR = QColor(226, 75, 74)
AREA_COLOR = QColor(29, 158, 117)


class MeasureTool(QgsMapTool):
    MODE_LINE = "line"
    MODE_AREA = "area"

    measured = pyqtSignal(str)
    cleared = pyqtSignal()

    def __init__(self, canvas, mode=MODE_LINE):
        super().__init__(canvas)
        self.mode = mode
        self.points = []
        self.finished = False

        geom_type = QgsWkbTypes.LineGeometry if mode == self.MODE_LINE else QgsWkbTypes.PolygonGeometry
        self.rubber = QgsRubberBand(canvas, geom_type)
        self.rubber.setColor(AREA_COLOR if mode == self.MODE_AREA else LINE_COLOR)
        self.rubber.setWidth(2)
        self.setCursor(Qt.CrossCursor)

    # ---------- 交互 ----------

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.finish()
            return
        if event.button() != Qt.LeftButton:
            return
        self.finished = False
        self.points.append(QgsPointXY(self.toMapCoordinates(event.pos())))
        self._refresh()

    def canvasMoveEvent(self, event):
        if not self.points or self.finished:
            return
        # 预览：把鼠标位置当作临时末点
        preview = self.points + [QgsPointXY(self.toMapCoordinates(event.pos()))]
        self._draw(preview)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Backspace:
            self.remove_last()
            event.accept()
        elif key == Qt.Key_Escape:
            self.clear()
            event.accept()
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self.finish()
            event.accept()
        else:
            super().keyPressEvent(event)

    # ---------- 行为 ----------

    def remove_last(self):
        if self.points:
            self.points.pop()
            self.finished = False
            self._refresh()

    def clear(self):
        self.points = []
        self.finished = False
        self.rubber.reset(self._rb_type())
        self.cleared.emit()

    def finish(self):
        if len(self.points) < (2 if self.mode == self.MODE_LINE else 3):
            return
        self.finished = True
        self._draw(self.points)
        self.measured.emit(self.result_text())

    def result_text(self):
        crs = self.canvas().mapSettings().destinationCrs()
        geom = self._geometry(self.points)
        if geom is None or geom.isEmpty():
            return ""
        if self.mode == self.MODE_LINE:
            return "折线长度 {}（{} 个点）".format(
                fmt_length(measure_length(self.points, crs)), len(self.points)
            )
        return "面积 {} | 周长 {}".format(
            fmt_area(measure_area(geom, crs)), fmt_length(measure_perimeter(geom, crs))
        )

    def _rb_type(self):
        return QgsWkbTypes.LineGeometry if self.mode == self.MODE_LINE else QgsWkbTypes.PolygonGeometry

    def _draw(self, points):
        """安全绘制：点数不够时 _geometry() 会返回 None，直接传给 setToGeometry 会抛 TypeError。"""
        geom = self._geometry(points)
        if geom is None or geom.isEmpty():
            self.rubber.reset(self._rb_type())
            return
        self.rubber.setToGeometry(geom)

    def _geometry(self, points):
        if self.mode == self.MODE_LINE:
            if len(points) < 2:
                return None
            return QgsGeometry.fromPolylineXY(list(points))
        if len(points) < 3:
            return None
        ring = list(points) + [QgsPointXY(points[0])]
        return QgsGeometry.fromPolygonXY([ring])

    def _refresh(self):
        if len(self.points) >= (2 if self.mode == self.MODE_LINE else 3):
            self._draw(self.points)
        self.measured.emit(self._draft_text())

    def _draft_text(self):
        crs = self.canvas().mapSettings().destinationCrs()
        n = len(self.points)
        if self.mode == self.MODE_LINE:
            if n < 2:
                return "点击落点开始量距（{} 点）".format(n)
            return "量距中：{}".format(fmt_length(measure_length(self.points, crs)))
        if n < 3:
            return "点击落点围成区域（{} 点）".format(n)
        geom = self._geometry(self.points)
        return "量面中：{}".format(fmt_area(measure_area(geom, crs)))

    def deactivate(self):
        self.rubber.reset(self._rb_type())
        super().deactivate()
