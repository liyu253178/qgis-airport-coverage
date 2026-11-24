# -*- coding: utf-8 -*-
"""机场覆盖区规划 —— 插件主类。

用途：机场（大疆机场 / DJI Dock）覆盖区范围选择，实测有效覆盖半径 5 km。
功能：三种在线图源 / 5 km 覆盖圆（可复制、可拖动）/ 线测量 / 面积测量。
"""

from qgis.core import QgsApplication, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QAction, QActionGroup

from .admin import add_admin_layer, extent_in_canvas_crs
from .basemaps import add_basemap, apply_key_to_loaded_layers, get_basemap, set_tianditu_key
from .coverage import CoverageLayer, crs_warning
from .dock import CoverageDock
from .geodesy import clear_cache, ensure_project_crs
from .icons import icon
from .tool_add_circle import AddCircleTool
from .tool_measure import MeasureTool
from .tool_move_circle import MoveCircleTool

PLUGIN_TITLE = "机场覆盖区规划"
TOOL_KEYS = ("add", "move", "line", "area")


class AirportCoveragePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.coverage = CoverageLayer()
        self.dock = None
        self.toolbar = None
        self.actions = []  # 进「插件菜单」的动作
        self.tool_actions = {}  # key -> QAction（可勾选，对应当前激活工具）
        self.tool_group = None
        self.dock_action = None
        self.tools = {}
        self._connected_layer_id = None

    # ---------- 生命周期 ----------

    def initGui(self):
        ensure_project_crs()

        self.dock = CoverageDock(self.iface, self.coverage)
        self.dock.toolRequested.connect(self.set_tool)
        self.dock.basemapRequested.connect(self.load_basemap)
        self.dock.tiandituKeyChanged.connect(self.save_tianditu_key)
        self.dock.adminRequested.connect(self.load_admin)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)

        self.toolbar = self.iface.addToolBar(PLUGIN_TITLE)
        self.toolbar.setObjectName("AirportCoverageToolbar")
        self.tool_group = QActionGroup(self.iface.mainWindow())
        self.tool_group.setExclusive(True)

        # 每个功能一个独立图标 + 文字提示；工具类动作可勾选，工具栏上能看出当前在用哪个
        self._add_action(
            "basemap", "加载底图", "加载面板里当前选中的在线图源（置于最底层）",
            lambda: self.load_basemap(self.dock.combo_basemap.currentData()), "basemap",
        )
        self.dock_action = self.dock.toggleViewAction()
        self.dock_action.setIcon(icon("panel"))
        self.dock_action.setText("机位面板")
        self.dock_action.setToolTip("显示 / 隐藏机位面板")
        self.toolbar.addAction(self.dock_action)
        self.toolbar.addSeparator()

        self._add_action("add", "添加机位", "在地图上点击落一个覆盖圆（默认 5 km）", lambda: self.set_tool("add"), "add", tool=True)
        self._add_action("move", "拖动机位", "按住覆盖圆拖动，半径保持不变（Esc 取消）", lambda: self.set_tool("move"), "move", tool=True)
        self._add_action("line", "线测量", "测量折线长度", lambda: self.set_tool("line"), "line", tool=True)
        self._add_action("area", "面积测量", "测量围成区域的面积与周长", lambda: self.set_tool("area"), "area", tool=True)
        self.toolbar.addSeparator()
        self._add_action("export", "导出机位方案", "导出为 GeoPackage（memory 图层关闭即丢失）", lambda: self.dock._export(), "export")

        for act in self.actions:
            self.iface.addPluginToMenu(PLUGIN_TITLE, act)

        QgsProject.instance().crsChanged.connect(self._on_project_crs_changed)

        warn = crs_warning()
        if warn:
            self._message(warn, 1)

    def unload(self):
        try:
            QgsProject.instance().crsChanged.disconnect(self._on_project_crs_changed)
        except Exception:
            pass

        for tool in self.tools.values():
            try:
                self.canvas.unsetMapTool(tool)
                tool.deactivate()
            except Exception:
                pass
        self.tools.clear()

        for act in self.actions:
            self.iface.removePluginMenu(PLUGIN_TITLE, act)
        self.actions = []
        self.tool_actions = {}

        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.setParent(None)
            self.dock.deleteLater()
            self.dock = None

        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
            self.toolbar = None

        clear_cache()

    # ---------- 界面构件 ----------

    def _add_action(self, key, text, tip, slot, icon_name, tool=False):
        act = QAction(icon(icon_name), text, self.iface.mainWindow())
        act.setToolTip("{}：{}".format(text, tip))
        act.setStatusTip(tip)
        if tool:
            act.setCheckable(True)
            self.tool_group.addAction(act)
            self.tool_actions[key] = act
        act.triggered.connect(lambda _checked=False: slot())
        self.toolbar.addAction(act)
        self.actions.append(act)
        return act

    def _ensure_layer(self):
        lyr = self.coverage.ensure_layer()
        # 图层被用户删掉又重建后，是一个新对象，必须按 id 重新连信号
        # （原实现只连一次，之后面板就不再刷新了）
        if lyr is not None and self.dock is not None and self._connected_layer_id != lyr.id():
            lyr.layerModified.connect(self.dock.refresh_later)
            self._connected_layer_id = lyr.id()
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
            tool = self._create_tool(key)
            act = self.tool_actions.get(key)
            if act is not None:
                tool.setAction(act)  # 工具激活时工具栏按钮自动高亮，切走自动取消
            self.tools[key] = tool
        self.canvas.setMapTool(self.tools[key])

        act = self.tool_actions.get(key)
        if act is not None:
            act.setChecked(True)

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
        if self.dock is not None:
            self.dock.refresh_later()

    def _on_project_crs_changed(self):
        clear_cache()
        if self.dock is not None:
            self.dock.refresh_later()

    # ---------- 图源 ----------

    def load_basemap(self, key):
        basemap = get_basemap(key)
        if basemap is None:
            self._message("未找到图源：{}".format(key), 2)
            return
        layer, err = add_basemap(basemap)
        if err:
            self._message(err, 1 if basemap.get("needs_key") else 2)
            return
        if basemap.get("crs") == "GCJ-02":
            self._message("已加载：{}。⚠ {}".format(basemap["name"], basemap.get("note", "")), 1)
        else:
            self._message("已加载：{}".format(basemap["name"]))
        if self.dock is not None:
            self.dock.refresh_later()

    # ---------- 行政区划 ----------

    def load_admin(self, level, prov_gb, city_gb, labels):
        try:
            layer, err, is_new = add_admin_layer(level, prov_gb or None, city_gb or None, labels)
        except Exception as exc:
            self._message("加载行政区划出错：{}".format(exc), 2)
            return
        if err:
            self._message(err, 2)
            return
        # 缩放到所选范围，选了省 / 市才有意义；全国就缩到全国
        try:
            ext = extent_in_canvas_crs(layer, self.canvas)
            if not ext.isEmpty():
                self.canvas.setExtent(ext.buffered(ext.width() * 0.05))
                self.canvas.refresh()
        except Exception:
            pass
        self._message("已加载：{}（{} 个要素）".format(layer.name(), layer.featureCount()) if is_new
                      else "已显示：{}".format(layer.name()))

    def save_tianditu_key(self, key):
        set_tianditu_key(key)
        n = apply_key_to_loaded_layers() if key else 0
        if self.dock is not None:
            self.dock._update_basemap_note()
        if key:
            tail = "，已更新 {} 个已加载的天地图图层".format(n) if n else ""
            self._message("天地图 key 已保存" + tail)
        else:
            self._message("天地图 key 已清空", 1)

    def _message(self, text, level=0):
        self.iface.messageBar().pushMessage(PLUGIN_TITLE, text, level=level, duration=6)

    @staticmethod
    def tr(text):
        return QgsApplication.translate("AirportCoverage", text)
