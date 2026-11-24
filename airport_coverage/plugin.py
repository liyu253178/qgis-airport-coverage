# -*- coding: utf-8 -*-
"""机场覆盖区规划 —— 插件主类。

用途：机场（大疆机场 / DJI Dock）覆盖区范围选择，实测有效覆盖半径 5 km。
功能：三种在线图源 / 5 km 覆盖圆（可复制、可拖动）/ 线测量 / 面积测量。
"""

import os

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .basemaps import add_basemap, get_basemap
from .coverage import CoverageLayer, crs_warning
from .dock import CoverageDock
from .geodesy import ensure_project_crs
from .tool_add_circle import AddCircleTool
from .tool_measure import MeasureTool
from .tool_move_circle import MoveCircleTool

PLUGIN_TITLE = "机场覆盖区规划"


def _icon():
    return QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))


class AirportCoveragePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.coverage = CoverageLayer()
        self.dock = None
        self.toolbar = None
        self.menu = None
        self.actions = []
        self.tools = {}
        self._layer_connected = False

    # ---------- 生命周期 ----------

    def initGui(self):
        ensure_project_crs()

        self.dock = CoverageDock(self.iface, self.coverage)
        self.dock.toolRequested.connect(self.set_tool)
        self.dock.basemapRequested.connect(self.load_basemap)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)

        self.toolbar = self.iface.addToolBar(PLUGIN_TITLE)
        self.toolbar.setObjectName("AirportCoverageToolbar")

        self._add_action("添加机位", "在地图上点击落一个覆盖圆（默认 5 km）", lambda: self.set_tool("add"), True)
        self._add_action("拖动机位", "按住覆盖圆拖动，半径保持不变", lambda: self.set_tool("move"), True)
        self._add_action("线测量", "测量折线长度", lambda: self.set_tool("line"), True)
        self._add_action("面积测量", "测量围成区域的面积与周长", lambda: self.set_tool("area"), True)
        self._add_action("导出机位方案", "导出为 GeoPackage（memory 图层关闭即丢失）", self.dock._export, True)

        menu_bar = self.iface.mainWindow().menuBar()
        plugin_menu = menu_bar.addMenu(PLUGIN_TITLE)
        for act in self.actions:
            plugin_menu.addAction(act)
        self.menu = plugin_menu

        warn = crs_warning()
        if warn:
            self._message(warn, 1)

    def unload(self):
        for tool in self.tools.values():
            try:
                tool.deactivate()
            except Exception:
                pass
        self.tools.clear()

        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.setParent(None)
            self.dock = None

        for act in self.actions:
            self.iface.removePluginMenu(PLUGIN_TITLE, act)
        self.actions = []

        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None

    # ---------- 界面构件 ----------

    def _add_action(self, text, tip, slot, to_toolbar=True):
        act = QAction(_icon(), text, self.iface.mainWindow())
        act.setToolTip(tip)
        act.setStatusTip(tip)
        act.triggered.connect(slot)
        if to_toolbar and self.toolbar is not None:
            self.toolbar.addAction(act)
        self.actions.append(act)
        return act

    def _ensure_layer(self):
        lyr = self.coverage.ensure_layer()
        if lyr is not None and not self._layer_connected and self.dock is not None:
            lyr.editingStopped.connect(self.dock.refresh)
            lyr.featureAdded.connect(lambda _fid: self.dock.refresh())
            lyr.featuresDeleted.connect(lambda _fids: self.dock.refresh())
            self._layer_connected = True
        return lyr

    # ---------- 工具切换 ----------

    def set_tool(self, key):
        if key == "clear":
            for k in ("line", "area"):
                if k in self.tools:
                    self.tools[k].clear()
            self.dock.set_measure_text("")
            return

        if key in ("add", "move"):
            self._ensure_layer()

        if key not in self.tools:
            self.tools[key] = self._create_tool(key)
        self.canvas.setMapTool(self.tools[key])

    def _create_tool(self, key):
        if key == "add":
            tool = AddCircleTool(self.canvas, self.coverage, self.dock.radius, notify=self._notify_tool)
            tool.circleAdded.connect(self._on_circle_changed)
            return tool
        if key == "move":
            tool = MoveCircleTool(self.canvas, self.coverage, notify=self._notify_tool)
            tool.circleMoved.connect(self._on_circle_changed)
            return tool
        if key in ("line", "area"):
            mode = MeasureTool.MODE_LINE if key == "line" else MeasureTool.MODE_AREA
            tool = MeasureTool(self.canvas, mode)
            tool.measured.connect(self.dock.set_measure_text)
            tool.cleared.connect(lambda: self.dock.set_measure_text(""))
            return tool
        raise ValueError("未知工具：{}".format(key))

    def _notify_tool(self, message, level=0):
        """工具里出错时打到消息栏，避免「点了没反应、只有日志里有字」。"""
        self._message(message, level)

    def _on_circle_changed(self, *args):
        self.coverage.commit()
        if self.dock is not None:
            self.dock.refresh()

    # ---------- 图源 ----------

    def load_basemap(self, key):
        basemap = get_basemap(key)
        if basemap is None:
            self._message("未找到图源：{}".format(key), 2)
            return
        layer, err = add_basemap(basemap)
        if err:
            self._message(err, 2)
            return
        self._message("已加载底图：{}".format(basemap["name"]))
        if self.dock is not None:
            self.dock.refresh()

    def _message(self, text, level=0):
        self.iface.messageBar().pushMessage(PLUGIN_TITLE, text, level=level, duration=6)

    @staticmethod
    def tr(text):
        return QgsApplication.translate("AirportCoverage", text)
