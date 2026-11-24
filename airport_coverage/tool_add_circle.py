# -*- coding: utf-8 -*-
"""「添加机位」工具：在地图上点一下，落一个 5 km 覆盖圆。"""

from qgis.core import QgsPointXY
from qgis.gui import QgsMapToolEmitPoint
from qgis.PyQt.QtCore import Qt, pyqtSignal

from .geodesy import to_wgs84


class AddCircleTool(QgsMapToolEmitPoint):
    circleAdded = pyqtSignal(int, float, float)  # fid, lon, lat

    def __init__(self, canvas, coverage, radius_getter, notify=None):
        """
        :param radius_getter: 返回当前半径（米）的可调用对象，默认由面板提供（5000）
        :param notify: (message, level) -> None。不传的话出错只会静静地写进 Python 日志，
                       界面上完全看不出发生了什么 —— 曾经就是这样「点下去没反应」。
        """
        super().__init__(canvas)
        self.coverage = coverage
        self.radius_getter = radius_getter or (lambda: 5000.0)
        self._notify = notify or (lambda msg, level=0: None)
        self.setCursor(Qt.CrossCursor)
        self.canvasClicked.connect(self._on_clicked)

    def _on_clicked(self, point, button):
        if button != Qt.LeftButton:
            return
        try:
            map_crs = self.canvas().mapSettings().destinationCrs()
            lon, lat = to_wgs84(QgsPointXY(point), map_crs)
            if lon is None or lat is None:
                self._notify("无法把点击位置换算成经纬度", 2)
                return

            radius = float(self.radius_getter())
            fid = self.coverage.add_circle(lon, lat, radius)
            if fid is None:
                self._notify("机位写入图层失败：{}".format(self.coverage.last_error or "未知原因"), 2)
                return

            self.circleAdded.emit(fid, lon, lat)
            self._notify("已添加机位，半径 {} m".format(int(radius)), 0)
        except Exception as exc:
            self._notify("添加机位出错：{}".format(exc), 2)
