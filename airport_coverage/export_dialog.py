# -*- coding: utf-8 -*-
"""「制图输出」对话框：选版式 / 范围 / 图层 / 输出格式，一键导出期刊风格成果图。"""

import os

from qgis.core import QgsProject, QgsSettings
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from . import admin
from .figure import WIDTH_PRESETS, FigureError, FigureOptions, export_figure, scan_layers

KEY = "airport_coverage/fig_"
FORMATS = (("PDF（矢量，推荐）", "pdf"), ("PNG", "png"), ("SVG", "svg"), ("TIFF", "tiff"))


class FigureDialog(QDialog):
    def __init__(self, iface, coverage, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.coverage = coverage
        self.setWindowTitle("制图输出 · 期刊风格成果图")
        self.setMinimumWidth(520)
        self._busy = False
        self._last_dir = None

        lay = QVBoxLayout(self)
        lay.addWidget(self._build_layout_box())
        lay.addWidget(self._build_scope_box())
        lay.addWidget(self._build_layers_box())
        lay.addWidget(self._build_output_box())

        self.label_status = QLabel("")
        self.label_status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        lay.addWidget(self.label_status)
        lay.addWidget(self.progress)

        self.buttons = QDialogButtonBox()
        self.btn_run = self.buttons.addButton("导出", QDialogButtonBox.AcceptRole)
        self.btn_close = self.buttons.addButton("关闭", QDialogButtonBox.RejectRole)
        self.btn_run.clicked.connect(self._run)
        self.btn_close.clicked.connect(self.reject)
        lay.addWidget(self.buttons)

        self._restore()
        self._sync()

    # ---------- 界面 ----------

    def _build_layout_box(self):
        box = QGroupBox("版式")
        g = QGridLayout(box)
        self.combo_layout = QComboBox()
        self.combo_layout.addItem("右栏式（图例、插图在右栏，斑马边框）", "right_column")
        self.combo_layout.addItem("单幅内嵌式（图例、插图在图内，四边度分刻度）", "single_frame")
        self.combo_width = QComboBox()
        for label, w in WIDTH_PRESETS:
            self.combo_width.addItem(label, w)
        self.combo_width.addItem("自定义", -1.0)
        self.combo_width.setCurrentIndex(2)
        self.spin_width = QDoubleSpinBox()
        self.spin_width.setRange(40, 400)
        self.spin_width.setValue(180)
        self.spin_width.setSuffix(" mm")
        self.spin_height = QDoubleSpinBox()
        self.spin_height.setRange(40, 600)
        self.spin_height.setValue(230)
        self.spin_height.setSuffix(" mm")
        self.edit_panel = QLineEdit()
        self.edit_panel.setPlaceholderText("面板标号，如 (a)；留空用版式默认")
        g.addWidget(QLabel("版式"), 0, 0)
        g.addWidget(self.combo_layout, 0, 1, 1, 3)
        g.addWidget(QLabel("宽度"), 1, 0)
        g.addWidget(self.combo_width, 1, 1)
        g.addWidget(self.spin_width, 1, 2)
        g.addWidget(QLabel("最大高度"), 2, 0)
        g.addWidget(self.spin_height, 2, 1)
        g.addWidget(self.edit_panel, 2, 2, 1, 2)
        self.combo_width.currentIndexChanged.connect(self._sync)
        return box

    def _build_scope_box(self):
        box = QGroupBox("范围与区域")
        g = QGridLayout(box)
        self.combo_extent = QComboBox()
        self.combo_extent.addItem("机位覆盖范围（四周留白）", "circles")
        self.combo_extent.addItem("覆盖圆所在行政区的范围", "region")
        self.combo_extent.addItem("当前地图画布范围", "canvas")
        self.combo_crs = QComboBox()
        self.combo_crs.addItem("CGCS2000 / 3° 高斯-克吕格（自动选带）", "gk3")
        self.combo_crs.addItem("WGS84 / UTM（自动选带）", "utm")
        self.combo_level = QComboBox()
        self.combo_level.addItem("自动（取已加载行政区划里最细的一级）", "auto")
        for key, label, *_rest in admin.LEVELS:
            self.combo_level.addItem(label, key)
        self.chk_inset = QCheckBox("区位插图（所在行政区的上一级）")
        self.chk_inset.setChecked(True)
        self.chk_legend = QCheckBox("图例")
        self.chk_legend.setChecked(True)
        self.chk_selected = QCheckBox("只用选中的机位（图层里有选中要素时）")
        self.chk_selected.setChecked(True)
        g.addWidget(QLabel("制图范围"), 0, 0)
        g.addWidget(self.combo_extent, 0, 1)
        g.addWidget(QLabel("坐标系"), 1, 0)
        g.addWidget(self.combo_crs, 1, 1)
        g.addWidget(QLabel("主区域层级"), 2, 0)
        g.addWidget(self.combo_level, 2, 1)
        row = QHBoxLayout()
        row.addWidget(self.chk_inset)
        row.addWidget(self.chk_legend)
        g.addLayout(row, 3, 0, 1, 2)
        g.addWidget(self.chk_selected, 4, 0, 1, 2)
        return box

    def _build_layers_box(self):
        box = QGroupBox("参与出图的图层（沿用页面上已加载的）")
        v = QVBoxLayout(box)
        self.list_layers = QListWidget()
        self.list_layers.setMinimumHeight(110)
        v.addWidget(self.list_layers)

        row = QHBoxLayout()
        self.spin_zmax = QSpinBox()
        self.spin_zmax.setRange(8, 22)
        self.spin_zmax.setValue(18)
        self.spin_zmax.setPrefix("底图最大级别 ")
        self.chk_gray = QCheckBox("底图转灰度（便于黑白印刷）")
        row.addWidget(self.spin_zmax)
        row.addWidget(self.chk_gray)
        row.addStretch(1)
        v.addLayout(row)

        self.label_admin = QLabel("")
        self.label_admin.setStyleSheet("color: gray;")
        self.label_admin.setWordWrap(True)
        v.addWidget(self.label_admin)

        # _fill_layers 会用到 self.label_admin，必须在 label_admin 创建之后再填充
        self._fill_layers()
        return box

    def _build_output_box(self):
        box = QGroupBox("输出")
        g = QGridLayout(box)
        self.edit_dir = QLineEdit()
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._browse)
        self.edit_name = QLineEdit("机场覆盖图")
        self.combo_dpi = QComboBox()
        for d in (150, 300, 600):
            self.combo_dpi.addItem("{} dpi".format(d), d)
        self.combo_dpi.setCurrentIndex(1)
        self.fmt_checks = []
        row = QHBoxLayout()
        for label, key in FORMATS:
            c = QCheckBox(label)
            c.setProperty("fmt", key)
            c.setChecked(key in ("pdf", "png"))
            self.fmt_checks.append(c)
            row.addWidget(c)
        g.addWidget(QLabel("输出目录"), 0, 0)
        g.addWidget(self.edit_dir, 0, 1)
        g.addWidget(btn, 0, 2)
        g.addWidget(QLabel("文件名"), 1, 0)
        g.addWidget(self.edit_name, 1, 1)
        g.addWidget(self.combo_dpi, 1, 2)
        g.addLayout(row, 2, 0, 1, 3)
        tip = QLabel("在线底图出图需要下载瓦片：分辨率越高请求越多，先用 150~300 dpi 看效果，定稿再用 600 dpi。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        g.addWidget(tip, 3, 0, 1, 3)
        return box

    # ---------- 图层列表 ----------

    def _add_header(self, text):
        it = QListWidgetItem(text)
        it.setFlags(Qt.NoItemFlags)
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        self.list_layers.addItem(it)

    def _add_layer(self, layer, checked, kind):
        it = QListWidgetItem(layer.name())
        it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        it.setData(Qt.UserRole, layer.id())
        it.setData(Qt.UserRole + 1, kind)
        self.list_layers.addItem(it)

    def _fill_layers(self):
        found = scan_layers(self.coverage)
        self.list_layers.clear()
        if found["rasters"]:
            self._add_header("底图（栅格）")
            for lyr, chk in found["rasters"]:
                self._add_layer(lyr, chk, "raster")
        if found["vectors"]:
            self._add_header("其它矢量图层（保留各自样式）")
            for lyr, chk in found["vectors"]:
                self._add_layer(lyr, chk, "vector")
        if not found["rasters"] and not found["vectors"]:
            self._add_header("工程里没有其它图层")
        names = "、".join(l.name().replace(admin.PREFIX, "") for l, _ in found["admin"])
        self.label_admin.setText(
            ("行政区划图层（自动使用已加载且可见的）：" + names) if names
            else "未加载行政区划图层：将按所选层级自动补一层边界，并用于区位插图和分区统计。")

    # ---------- 事件 ----------

    def _sync(self):
        self.spin_width.setEnabled(self.combo_width.currentData() == -1.0)
        cov = self.coverage.layer
        has_sel = bool(cov is not None and cov.selectedFeatureCount() > 0)
        self.chk_selected.setEnabled(has_sel)
        if not has_sel:
            self.chk_selected.setText("只用选中的机位（当前没有选中，将使用全部机位）")

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", self.edit_dir.text() or os.path.expanduser("~"))
        if d:
            self.edit_dir.setText(d)

    def _restore(self):
        s = QgsSettings()
        start = QgsProject.instance().homePath() or os.path.expanduser("~")
        self.edit_dir.setText(s.value(KEY + "dir", start))
        for combo, name in ((self.combo_layout, "layout"), (self.combo_dpi, "dpi")):
            i = combo.findData(s.value(KEY + name, combo.currentData(), type=str if name == "layout" else int))
            if i >= 0:
                combo.setCurrentIndex(i)

    def _save(self):
        s = QgsSettings()
        s.setValue(KEY + "dir", self.edit_dir.text())
        s.setValue(KEY + "layout", self.combo_layout.currentData())
        s.setValue(KEY + "dpi", self.combo_dpi.currentData())

    def _collect(self):
        basemaps, others = [], []
        for i in range(self.list_layers.count()):
            it = self.list_layers.item(i)
            if it.data(Qt.UserRole) is None or it.checkState() != Qt.Checked:
                continue
            (basemaps if it.data(Qt.UserRole + 1) == "raster" else others).append(it.data(Qt.UserRole))
        w = self.combo_width.currentData()
        formats = [c.property("fmt") for c in self.fmt_checks if c.isChecked()]
        return FigureOptions(
            layout=self.combo_layout.currentData(),
            width_mm=self.spin_width.value() if w == -1.0 else w,
            max_height_mm=self.spin_height.value(),
            extent_mode=self.combo_extent.currentData(),
            crs_mode=self.combo_crs.currentData(),
            region_level=self.combo_level.currentData(),
            basemap_ids=basemaps, basemap_zmax=self.spin_zmax.value(),
            basemap_gray=self.chk_gray.isChecked(), other_ids=others,
            inset=self.chk_inset.isChecked(), legend=self.chk_legend.isChecked(),
            panel_label=(self.edit_panel.text().strip() or None),
            only_selected=self.chk_selected.isChecked(),
            out_dir=self.edit_dir.text().strip(), basename=self.edit_name.text().strip() or "机场覆盖图",
            formats=formats, dpi=self.combo_dpi.currentData(),
        )

    def _progress(self, msg, pct=None):
        self.label_status.setText(msg)
        if pct is not None:
            self.progress.setValue(int(pct))
        QApplication.processEvents()

    def _set_busy(self, busy):
        self._busy = busy
        self.btn_run.setEnabled(not busy)
        self.progress.setVisible(busy)
        QApplication.setOverrideCursor(Qt.WaitCursor) if busy else QApplication.restoreOverrideCursor()

    def _run(self):
        if self._busy:
            return
        opts = self._collect()
        if not opts.out_dir:
            QMessageBox.warning(self, "制图输出", "请先选择输出目录")
            return
        if not opts.formats:
            QMessageBox.warning(self, "制图输出", "请至少勾选一种输出格式")
            return
        self._save()
        self._set_busy(True)
        try:
            result = export_figure(self.coverage, opts, self._progress)
        except FigureError as exc:
            self._set_busy(False)
            self.label_status.setText("")
            QMessageBox.warning(self, "制图输出", str(exc))
            return
        except Exception as exc:  # 未预料的错误：给出提示，详情在「日志消息」面板
            self._set_busy(False)
            self.label_status.setText("")
            QMessageBox.critical(self, "制图输出失败", "{}: {}\n\n详细信息见 QGIS「日志消息」面板。".format(type(exc).__name__, exc))
            raise
        self._set_busy(False)
        self._last_dir = opts.out_dir
        self._show_result(result, opts)

    def _show_result(self, result, opts):
        s = result["summary"]
        lines = [
            "已导出 {} 个文件到：{}".format(len(result["files"]), opts.out_dir),
            "成品尺寸 {:.0f} × {:.0f} mm，坐标系 {}".format(*result["page_mm"], result["crs"]),
            "覆盖总面积 {:.2f} km²（{} 个机位）".format(s["union_km2"], len(s["points"])),
        ]
        for u in s["units"][:3]:
            lines.append("  {}：覆盖 {:.2f} km²（{:.1f}%）".format(u["name"], u["covered_km2"], u["ratio"] * 100))
        if result["warnings"]:
            lines += ["", "注意："] + ["  · " + w for w in result["warnings"]]
        box = QMessageBox(self)
        box.setWindowTitle("制图输出完成")
        box.setText("\n".join(lines))
        open_btn = box.addButton("打开输出文件夹", QMessageBox.ActionRole)
        box.addButton("好的", QMessageBox.AcceptRole)
        box.exec_()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(opts.out_dir))
        self.label_status.setText("上次导出：" + opts.out_dir)
