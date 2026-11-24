# -*- coding: utf-8 -*-
"""「拖动机位」工具：按住覆盖圆拖动，半径始终保持 5 km。

关键：拖动时不是平移几何，而是「按新圆心重建圆」。
实测（docs/QGIS插件-可行性验证报告.md §2.3）：
    EPSG:4326 下直接平移，北京→广州半径会变成 5989.8 m（偏差 19.8%）；
    按新圆心重建，偏差恒为 0.000 m，单次开销 2.74 ms。
"""

from qgis.core import QgsPointXY
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import Qt, pyqtSignal

from .geodesy import to_wgs84


class MoveCircleTool(QgsMapTool):
    circleMoved = pyqtSignal(int, float, float)  # fid, lon, lat

    def __init__(self, canvas, coverage, tol_px=12, notify=None):
        super().__init__(canvas)
        self.coverage = coverage
        self.tol_px = tol_px
        self._notify = notify or (lambda msg, level=0: None)
        self._fid = None
        self._radius = None
        self._reported = False
        self.setCursor(Qt.OpenHandCursor)

    # ---------- 事件 ----------

    def canvasPressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        lyr = self.coverage.layer
        if lyr is None:
            return

        point = self.toMapCoordinates(event.pos())
        map_crs = self.canvas().mapSettings().destinationCrs()
        fid = self.coverage.feature_at(point, map_crs, tol_px=self.tol_px, canvas=self.canvas())
        if fid is None:
            return

        self._fid = fid
        self._radius = self.coverage.radius_of(fid)
        lyr.beginEditCommand("移动机位")
        self.setCursor(Qt.ClosedHandCursor)

    def canvasMoveEvent(self, event):
        if self._fid is None:
            return
        try:
            point = self.toMapCoordinates(event.pos())
            map_crs = self.canvas().mapSettings().destinationCrs()
            lon, lat = to_wgs84(QgsPointXY(point), map_crs)
            if lon is None or lat is None:
                return
            self.coverage.update_circle(self._fid, lon, lat, self._radius)
        except Exception as exc:
            # 鼠标移动每秒触发几十次，异常必须吃掉，否则日志会被刷爆
            self._reported = True
            self._notify("拖动时更新几何出错：{}".format(exc), 2)

    def canvasReleaseEvent(self, event):
        if self._fid is None:
            return
        lyr = self.coverage.layer
        lon, lat = self.coverage.center_of(self._fid) or (0.0, 0.0)
        fid, self._fid = self._fid, None
        if lyr is not None:
            lyr.endEditCommand()
        self.setCursor(Qt.OpenHandCursor)
        self.circleMoved.emit(fid, lon, lat)

    def deactivate(self):
        if self._fid is not None and self.coverage.layer is not None:
            self.coverage.layer.endEditCommand()
            self._fid = None
        super().deactivate()
