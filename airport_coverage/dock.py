# -*- coding: utf-8 -*-
"""机位覆盖区面板。

面板上的「导出机位方案」按钮是必需的：覆盖圆存放在 memory 图层里，
关闭工程即丢失，必须显式导出 GeoPackage。
"""

from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QFrame,
    QScrollArea,
    QLineEdit,
    QAbstractItemView,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .admin import LEVELS, regions
from .basemaps import BASEMAPS, DEFAULT_BASEMAP, get_basemap, get_tianditu_key
from .coverage import crs_warning
from .geodesy import DEFAULT_RADIUS_M, fmt_area
from .icons import icon

COLUMNS = ("机位名称", "半径(m)", "圆心经度", "圆心纬度", "面积(km²)")


class CoverageDock(QDockWidget):
    toolRequested = pyqtSignal(str)  # add / move / line / area / clear
    basemapRequested = pyqtSignal(str)
    tiandituKeyChanged = pyqtSignal(str)
    adminRequested = pyqtSignal(str, str, str, bool)  # level, 省 gb, 市 gb, 是否标注

    def __init__(self, iface, coverage, parent=None):
        super().__init__("机场覆盖区规划", parent)
        self.iface = iface
        self.coverage = coverage

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(self._build_basemap_box())
        layout.addWidget(self._build_admin_box())
        layout.addWidget(self._build_radius_box())
        layout.addWidget(self._build_tools_box())
        layout.addWidget(self._build_table())
        layout.addWidget(self._build_stats_box())
        layout.addWidget(self._build_action_box())
        layout.addWidget(self._build_about())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(root)
        self.setWidget(scroll)

        # 刷新合并：短时间内多次请求只刷一次（统计里有 unaryUnion，不便宜）
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    # ---------- 界面分段 ----------

    def _build_basemap_box(self):
        box = QGroupBox("在线图源")
        outer = QVBoxLayout(box)
        row = QHBoxLayout()
        self.combo_basemap = QComboBox()
        for b in BASEMAPS:
            tag = "（叠加）" if b["kind"] == "overlay" and "叠加" not in b["name"] else ""
            self.combo_basemap.addItem(b["name"] + tag, b["id"])
        idx = self.combo_basemap.findData(DEFAULT_BASEMAP)
        if idx >= 0:
            self.combo_basemap.setCurrentIndex(idx)
        btn = QPushButton(icon("basemap"), "加载")
        btn.clicked.connect(lambda: self.basemapRequested.emit(self.combo_basemap.currentData()))
        row.addWidget(self.combo_basemap, 1)
        row.addWidget(btn)
        outer.addLayout(row)

        # 天地图 key（仅选中天地图图源时显示）
        self.key_row = QWidget()
        key_layout = QHBoxLayout(self.key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        self.edit_key = QLineEdit(get_tianditu_key())
        self.edit_key.setPlaceholderText("天地图 Key（浏览器端）")
        self.edit_key.setClearButtonEnabled(True)
        btn_key = QPushButton("保存 Key")
        btn_key.setToolTip("保存到 QGIS 用户设置；已加载的天地图图层会自动更新")
        btn_key.clicked.connect(lambda: self.tiandituKeyChanged.emit(self.edit_key.text().strip()))
        self.edit_key.returnPressed.connect(btn_key.click)
        key_layout.addWidget(self.edit_key, 1)
        key_layout.addWidget(btn_key)
        outer.addWidget(self.key_row)

        self.label_basemap_note = QLabel("")
        self.label_basemap_note.setWordWrap(True)
        self.label_basemap_note.setStyleSheet("color: #BA7517;")
        outer.addWidget(self.label_basemap_note)
        self.combo_basemap.currentIndexChanged.connect(self._update_basemap_note)
        self._update_basemap_note()
        return box

    def _update_basemap_note(self):
        b = get_basemap(self.combo_basemap.currentData())
        self.key_row.setVisible(bool(b and b.get("needs_key")))
        if b is None:
            self.label_basemap_note.setText("")
            return
        parts = []
        if b.get("crs") == "GCJ-02":
            parts.append("⚠ " + b.get("note", ""))
        elif b.get("note"):
            parts.append(b["note"])
        if b["kind"] == "overlay":
            parts.append("透明叠加层：需先加载一个底图")
        if b.get("needs_key") and not get_tianditu_key():
            parts.append("⚠ 尚未设置 key")
        self.label_basemap_note.setText("\n".join(parts))

    def _build_admin_box(self):
        box = QGroupBox("行政区划（内置，按需加载）")
        outer = QVBoxLayout(box)

        row1 = QHBoxLayout()
        self.combo_level = QComboBox()
        for key, label, *_rest in LEVELS:
            self.combo_level.addItem(label, key)
        self.combo_prov = QComboBox()
        self.combo_prov.addItem("全国", "")
        for p in regions()["provinces"]:
            self.combo_prov.addItem(p["name"], p["gb"])
        row1.addWidget(self.combo_level)
        row1.addWidget(self.combo_prov, 1)
        outer.addLayout(row1)

        self.combo_city = QComboBox()
        self.combo_city.addItem("全部市", "")
        self.combo_city.setEnabled(False)
        outer.addWidget(self.combo_city)

        row3 = QHBoxLayout()
        self.chk_labels = QCheckBox("标注名称")
        self.chk_labels.setChecked(True)
        btn = QPushButton(icon("admin"), "加载")
        btn.clicked.connect(self._emit_admin)
        row3.addWidget(self.chk_labels)
        row3.addStretch(1)
        row3.addWidget(btn)
        outer.addLayout(row3)

        tip = QLabel("先选层级，再选范围：县级可再细化到某个市。数据为 CGCS2000，工程中自动转换；只读取所选范围。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        outer.addWidget(tip)

        self.combo_level.currentIndexChanged.connect(self._sync_admin_scope)
        self.combo_prov.currentIndexChanged.connect(self._sync_admin_scope)
        self._sync_admin_scope()
        return box

    def _sync_admin_scope(self):
        """市下拉框只在「县」层级且已选省时可用，并随省刷新。"""
        is_county = self.combo_level.currentData() == "county"
        prov_gb = self.combo_prov.currentData()
        self.combo_city.blockSignals(True)
        self.combo_city.clear()
        self.combo_city.addItem("全部市", "")
        if is_county and prov_gb:
            for c in regions()["cities"].get(prov_gb[3:5], []):
                self.combo_city.addItem(c["name"], c["gb"])
        self.combo_city.setEnabled(is_county and bool(prov_gb))
        self.combo_city.blockSignals(False)

    def _emit_admin(self):
        self.adminRequested.emit(
            self.combo_level.currentData(),
            self.combo_prov.currentData() or "",
            self.combo_city.currentData() or "" if self.combo_city.isEnabled() else "",
            self.chk_labels.isChecked(),
        )

    def _build_radius_box(self):
        box = QGroupBox("覆盖半径")
        row = QHBoxLayout(box)
        self.spin_radius = QDoubleSpinBox()
        self.spin_radius.setRange(50, 200000)
        self.spin_radius.setDecimals(0)
        self.spin_radius.setSingleStep(500)
        self.spin_radius.setSuffix(" m")
        self.spin_radius.setValue(DEFAULT_RADIUS_M)
        tip = QLabel("大疆机场实测覆盖半径 5 km")
        tip.setStyleSheet("color: gray;")
        row.addWidget(self.spin_radius)
        row.addWidget(tip, 1)
        return box

    def _build_tools_box(self):
        box = QGroupBox("工具")
        row1 = QHBoxLayout()
        self.btn_add = QPushButton(icon("add"), "添加机位")
        self.btn_move = QPushButton(icon("move"), "拖动机位")
        self.btn_add.clicked.connect(lambda: self.toolRequested.emit("add"))
        self.btn_move.clicked.connect(lambda: self.toolRequested.emit("move"))
        row1.addWidget(self.btn_add)
        row1.addWidget(self.btn_move)

        row2 = QHBoxLayout()
        self.btn_line = QPushButton(icon("line"), "线测量")
        self.btn_area = QPushButton(icon("area"), "面积测量")
        self.btn_line.clicked.connect(lambda: self.toolRequested.emit("line"))
        self.btn_area.clicked.connect(lambda: self.toolRequested.emit("area"))
        row2.addWidget(self.btn_line)
        row2.addWidget(self.btn_area)

        row3 = QHBoxLayout()
        self.btn_clear = QPushButton(icon("clear"), "清除测量")
        self.btn_clear.clicked.connect(lambda: self.toolRequested.emit("clear"))
        row3.addWidget(self.btn_clear)

        wrap = QVBoxLayout(box)
        wrap.addLayout(row1)
        wrap.addLayout(row2)
        wrap.addLayout(row3)
        return box

    def _build_table(self):
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumHeight(140)
        return self.table

    def _build_stats_box(self):
        box = QGroupBox("统计")
        layout = QVBoxLayout(box)
        self.label_stats = QLabel("暂无机位")
        self.label_measure = QLabel("测量：—")
        self.label_measure.setWordWrap(True)
        self.label_warn = QLabel("")
        self.label_warn.setWordWrap(True)
        self.label_warn.setStyleSheet("color: #BA7517;")
        layout.addWidget(self.label_stats)
        layout.addWidget(self.label_measure)
        layout.addWidget(self.label_warn)
        return box

    def _build_action_box(self):
        box = QGroupBox("操作")
        row1 = QHBoxLayout()
        self.btn_recalc = QPushButton(icon("recalc"), "重算所选")
        self.btn_recalc.setToolTip("按面板设定的半径重建所选圆的几何（默认即 5 km）")
        self.btn_delete = QPushButton(icon("delete"), "删除所选")
        self.btn_recalc.clicked.connect(self._recalc_selected)
        self.btn_delete.clicked.connect(self._delete_selected)
        row1.addWidget(self.btn_recalc)
        row1.addWidget(self.btn_delete)

        row2 = QHBoxLayout()
        self.btn_export = QPushButton(icon("export"), "导出机位方案…")
        self.btn_export.setToolTip("memory 图层关闭工程即丢失，务必导出 GeoPackage")
        self.btn_export.clicked.connect(self._export)
        row2.addWidget(self.btn_export)

        wrap = QVBoxLayout(box)
        wrap.addLayout(row1)
        wrap.addLayout(row2)
        return box

    def _build_about(self):
        label = QLabel("由微信公众号【点点 GIS】开发<br>联系：zheyuchen6@gmail.com")
        label.setAlignment(Qt.AlignCenter)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setStyleSheet("color: gray; font-size: 11px;")
        return label

    # ---------- 行为 ----------

    def radius(self):
        return float(self.spin_radius.value())

    def refresh_later(self):
        self._timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        if not self.isVisible():  # 面板关着就不白算，重新打开时 showEvent 会补刷
            return
        self.refresh_table()
        self.label_stats.setText(self.coverage.stats_text())
        warn = crs_warning()
        self.label_warn.setText(warn or "")

    def refresh_table(self):
        feats = self.coverage.features()
        self.table.setRowCount(len(feats))
        for r, f in enumerate(feats):
            vals = [
                str(f.attribute("name") or ""),
                "{:.0f}".format(float(f.attribute("radius_m") or 0)),
                "{:.6f}".format(float(f.attribute("lon") or 0)),
                "{:.6f}".format(float(f.attribute("lat") or 0)),
                "{:.4f}".format(float(f.attribute("area_km2") or 0)),
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, f.id())
                self.table.setItem(r, c, item)

    def _selected_fids(self):
        fids = []
        for item in self.table.selectedItems():
            if item.column() != 0:
                continue
            fid = item.data(Qt.UserRole)
            if fid is not None and fid not in fids:
                fids.append(fid)
        return fids or self.coverage.selected_ids()

    def _recalc_selected(self):
        fids = self._selected_fids()
        if not fids:
            self._info("请先在列表里选中机位")
            return
        radius = self.radius()
        done = 0
        for fid in fids:
            center = self.coverage.center_of(fid)
            if not center:
                continue
            if self.coverage.update_circle(fid, center[0], center[1], radius):
                done += 1
        self.coverage.commit()
        self.refresh()
        self._info("已按 {:.0f} m 重算 {} 个机位".format(radius, done))

    def _delete_selected(self):
        fids = self._selected_fids()
        if not fids:
            self._info("请先在列表里选中机位")
            return
        n = self.coverage.remove_features(fids)
        self.coverage.commit()
        self.refresh()
        self._info("已删除 {} 个机位".format(n))

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出机位方案（GeoPackage）", "机位覆盖方案.gpkg", "GeoPackage (*.gpkg)"
        )
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        ok, msg = self.coverage.export_gpkg(path)
        self._info(msg, level=0 if ok else 2)

    def set_measure_text(self, text):
        self.label_measure.setText("测量：{}".format(text) if text else "测量：—")

    def _info(self, text, level=0):
        if self.iface is not None:
            self.iface.messageBar().pushMessage("机场覆盖区", text, level=level, duration=5)
