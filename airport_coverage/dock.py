# -*- coding: utf-8 -*-
"""机位覆盖区面板。

面板上的「导出机位方案」按钮是必需的：覆盖圆存放在 memory 图层里，
关闭工程即丢失，必须显式导出 GeoPackage。
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
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

from .basemaps import BASEMAPS, DEFAULT_BASEMAP
from .coverage import crs_warning
from .geodesy import DEFAULT_RADIUS_M, fmt_area

COLUMNS = ("机位名称", "半径(m)", "圆心经度", "圆心纬度", "面积(km²)")


class CoverageDock(QDockWidget):
    toolRequested = pyqtSignal(str)  # add / move / line / area / clear
    basemapRequested = pyqtSignal(str)

    def __init__(self, iface, coverage, parent=None):
        super().__init__("机场覆盖区规划", parent)
        self.iface = iface
        self.coverage = coverage

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(self._build_basemap_box())
        layout.addWidget(self._build_radius_box())
        layout.addWidget(self._build_tools_box())
        layout.addWidget(self._build_table())
        layout.addWidget(self._build_stats_box())
        layout.addWidget(self._build_action_box())

        self.setWidget(root)
        self.refresh()

    # ---------- 界面分段 ----------

    def _build_basemap_box(self):
        box = QGroupBox("在线图源（WGS84）")
        row = QHBoxLayout(box)
        self.combo_basemap = QComboBox()
        for b in BASEMAPS:
            self.combo_basemap.addItem(b["name"], b["id"])
        idx = self.combo_basemap.findData(DEFAULT_BASEMAP)
        if idx >= 0:
            self.combo_basemap.setCurrentIndex(idx)
        btn = QPushButton("加载底图")
        btn.clicked.connect(lambda: self.basemapRequested.emit(self.combo_basemap.currentData()))
        row.addWidget(self.combo_basemap, 1)
        row.addWidget(btn)
        return box

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
        self.btn_add = QPushButton("添加机位")
        self.btn_move = QPushButton("拖动机位")
        self.btn_add.clicked.connect(lambda: self.toolRequested.emit("add"))
        self.btn_move.clicked.connect(lambda: self.toolRequested.emit("move"))
        row1.addWidget(self.btn_add)
        row1.addWidget(self.btn_move)

        row2 = QHBoxLayout()
        self.btn_line = QPushButton("线测量")
        self.btn_area = QPushButton("面积测量")
        self.btn_line.clicked.connect(lambda: self.toolRequested.emit("line"))
        self.btn_area.clicked.connect(lambda: self.toolRequested.emit("area"))
        row2.addWidget(self.btn_line)
        row2.addWidget(self.btn_area)

        row3 = QHBoxLayout()
        self.btn_clear = QPushButton("清除测量")
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
        self.btn_recalc = QPushButton("重算所选")
        self.btn_recalc.setToolTip("按面板设定的半径重建所选圆的几何（默认即 5 km）")
        self.btn_delete = QPushButton("删除所选")
        self.btn_recalc.clicked.connect(self._recalc_selected)
        self.btn_delete.clicked.connect(self._delete_selected)
        row1.addWidget(self.btn_recalc)
        row1.addWidget(self.btn_delete)

        row2 = QHBoxLayout()
        self.btn_export = QPushButton("导出机位方案…")
        self.btn_export.setToolTip("memory 图层关闭工程即丢失，务必导出 GeoPackage")
        self.btn_export.clicked.connect(self._export)
        row2.addWidget(self.btn_export)

        wrap = QVBoxLayout(box)
        wrap.addLayout(row1)
        wrap.addLayout(row2)
        return box

    # ---------- 行为 ----------

    def radius(self):
        return float(self.spin_radius.value())

    def refresh(self):
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
