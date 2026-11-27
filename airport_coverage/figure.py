# -*- coding: utf-8 -*-
"""制图输出：把「机位覆盖圆 + 行政区划 + 已加载的卫星底图 + 其它矢量图层」输出为期刊风格成果图。

设计要点（与独立脚本版 qgis-sci-map 的差别）：
  * **不改用户的图层**：所有出图用图层都是克隆体（或临时新建），登记到工程但不进图层树，
    导出结束后全部移除；临时布局也一并移除。
  * **覆盖圆直接取插件图层里的几何**（每个圆半径可以不同，也包含你拖动后的位置），不重算。
  * 底图用页面上已加载的瓦片图层：克隆时把最大级别限制为 opts.basemap_zmax（默认 18）。
  * 区位插图 = 主区域所在行政区的「上一级」：主区域是县，插图就是它所在的市，
    市内同级各县浅灰，主区域涂黑；主区域是市则上一级为省；是省则上一级为全国。
  * 统计只写 CSV，不画进图里。
"""

import csv
import os
import re

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsFillSymbol,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSimpleFillSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)

from . import admin
from .basemaps import BASEMAPS
from .sci_map import core as C

TMP_LAYOUT_NAME = "机场覆盖图（临时）"

# 版式预设：来自 qgis-sci-map 的 build_map.LAYOUTS；右栏式补一个中文字体（原来是 None，中文会变方框）
LAYOUTS = {
    "right_column": {
        "margins": (12.0, 5.0, 3.0, 10.0), "font": "Arial", "cjk_font": "SimSun",
        "graticule": {"style": "zebra", "fmt": "decimal", "all_sides": False, "size_pt": 6.0},
        "scalebar": {"style": "Single Box", "background": True, "segments": 2, "size_pt": 6.5,
                     "corner": "bl", "fraction": 0.28},
        "north_arrow": {"enabled": True, "corner": "tr"},
        "legend": {"position": "right", "size_pt": 7.0},
        "inset": {"corner": None, "width_mm": None, "overview": True},
    },
    "single_frame": {
        "margins": (8.0, 7.0, 8.0, 7.0), "font": "Times New Roman", "cjk_font": "Songti SC",
        "graticule": {"style": "ticks", "fmt": "dm", "all_sides": True, "size_pt": 8.0},
        "scalebar": {"style": "Line Ticks Up", "background": False, "segments": 1,
                     "size_pt": 8.0, "corner": "bl", "fraction": 0.2},
        "north_arrow": {"enabled": False, "corner": "tr"},
        "legend": {"position": "inside_br", "size_pt": 8.0, "frame": False, "background": False},
        "inset": {"corner": "tl", "width_mm": 38.0, "overview": False},
    },
}

WIDTH_PRESETS = (("单栏 85 mm", 85.0), ("1.5 栏 130 mm", 130.0), ("双栏 180 mm", 180.0))

ALBERS_CHINA = "+proj=aea +lat_1=25 +lat_2=47 +lat_0=0 +lon_0=105 +x_0=0 +y_0=0 +ellps=GRS80 +units=m +no_defs"

COMPASS_CN = {"N": "正北", "NE": "东北", "E": "正东", "SE": "东南", "S": "正南", "SW": "西南", "W": "正西", "NW": "西北"}


class FigureError(Exception):
    """可以直接展示给用户看的错误。"""


class FigureOptions:
    def __init__(self, **kw):
        self.layout = "right_column"        # right_column | single_frame
        self.width_mm = 180.0
        self.max_height_mm = 230.0
        self.extent_mode = "circles"         # circles | region | canvas
        self.crs_mode = "gk3"                # gk3 = CGCS2000 3°带 | utm
        self.region_level = "auto"           # auto | province | city | county
        self.basemap_ids = []                # 参与出图的栅格图层 id（自下而上顺序由工程决定）
        self.basemap_zmax = 18
        self.basemap_gray = False
        self.other_ids = []                  # 其它矢量图层 id
        self.inset = True
        self.legend = True
        self.stats = True
        self.panel_label = None              # None = 随版式默认
        self.only_selected = True            # 覆盖圆图层里有选中要素时只用选中的
        self.out_dir = ""
        self.basename = "机场覆盖图"
        self.formats = ["pdf", "png"]
        self.dpi = 300
        self.__dict__.update(kw)


# ---------------------------------------------------------------------------
# 工程图层扫描（对话框和出图共用）
# ---------------------------------------------------------------------------

def scan_layers(coverage):
    """返回 dict(rasters=[(layer, 默认勾选)], vectors=[(layer, 默认勾选)], admin=[(layer, level_key)])，
    各列表均为工程图层树里「自上而下」的顺序。"""
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    cov = coverage.layer
    base_names = {b["name"] for b in BASEMAPS if b["kind"] == "base"}
    overlay_names = {b["name"] for b in BASEMAPS if b["kind"] == "overlay"}
    out = {"rasters": [], "vectors": [], "admin": []}
    for lyr in root.layerOrder():
        node = root.findLayer(lyr.id())
        visible = bool(node and node.isVisible())
        if cov is not None and lyr.id() == cov.id():
            continue
        if isinstance(lyr, QgsRasterLayer):
            is_tile = "type=xyz" in (lyr.source() or "")
            default = visible and (lyr.name() in base_names or (is_tile and lyr.name() not in overlay_names))
            out["rasters"].append((lyr, default))
        elif isinstance(lyr, QgsVectorLayer):
            level = admin.level_of_layer_name(lyr.name())
            if level:
                out["admin"].append((lyr, level))
            elif lyr.isSpatial():
                out["vectors"].append((lyr, visible))
    return out


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def pick_gk3_epsg(lon):
    """CGCS2000 / 3 度带高斯-克吕格（按中央经线编号，EPSG:4534=75°E … 4554=135°E）。中国范围外返回 None。"""
    if lon < 73.5 or lon > 136.5:
        return None
    zone = max(25, min(45, int(round(lon / 3.0))))
    return 4534 + (zone - 25)


def _quote(values):
    return ",".join("'{}'".format(str(v).replace("'", "''")) for v in values)


def _cap_zmax(source, zmax):
    """XYZ 数据源字符串里的最大级别限制为 zmax（已有更小的值则保留）。"""
    m = re.search(r"zmax=(\d+)", source)
    if m:
        return source[:m.start(1)] + str(min(int(m.group(1)), zmax)) + source[m.end(1):]
    return source + "&zmax=" + str(zmax)


# ---------------------------------------------------------------------------
# 出图任务
# ---------------------------------------------------------------------------

class FigureJob:
    def __init__(self, coverage, opts, progress=None):
        self.cov = coverage
        self.o = opts
        self.project = QgsProject.instance()
        self._progress = progress or (lambda msg, pct=None: None)
        self._temp = []          # 临时登记到工程里的图层，结束时统一移除
        self.warnings = []
        self.fig = None

    # ----- 小工具 -----
    def step(self, msg, pct=None):
        C.log(msg)
        self._progress(msg, pct)

    def _ctx(self):
        return self.project.transformContext()

    def _tr(self, src, dst):
        return QgsCoordinateTransform(src, dst, self._ctx())

    def _visible(self, lyr):
        node = self.project.layerTreeRoot().findLayer(lyr.id())
        return bool(node and node.isVisible())

    def _register(self, layer):
        self.project.addMapLayer(layer, False)     # False：不进图层树
        self._temp.append(layer)
        return layer

    def _cleanup(self):
        for lyr in self._temp:
            try:
                self.project.removeMapLayer(lyr.id())
            except Exception:
                pass
        self._temp = []
        if self.fig is not None:
            self.fig.close()

    # ----- 图层克隆 -----
    def _clone_vector(self, src):
        if src.providerType() == "memory":          # clone() 对 memory 图层不带要素，改用 materialize
            new = src.materialize(QgsFeatureRequest())
            new.setName(src.name())
        else:
            new = src.clone()
            if new.subsetString() != src.subsetString():
                new.setSubsetString(src.subsetString())
        try:
            if src.renderer() is not None:
                new.setRenderer(src.renderer().clone())
            if src.labeling() is not None:
                new.setLabeling(src.labeling().clone())
            new.setLabelsEnabled(src.labelsEnabled())
            new.setOpacity(src.opacity())
        except Exception:
            pass
        return self._register(new)

    def _clone_basemap(self, src):
        """栅格底图克隆：XYZ 限制最大级别；可选灰度。"""
        source = src.source() or ""
        if "type=xyz" in source:
            new = QgsRasterLayer(_cap_zmax(source, int(self.o.basemap_zmax)), src.name(), "wms")
        else:
            new = src.clone()
        if not new.isValid():
            raise FigureError("底图「{}」克隆失败，请检查图层是否能正常显示".format(src.name()))
        new.setOpacity(src.opacity())
        if self.o.basemap_gray:
            try:
                from qgis.core import QgsHueSaturationFilter
                new.hueSaturationFilter().setGrayscaleMode(
                    C.enum(QgsHueSaturationFilter, "GrayscaleMode", "GrayscaleLuminosity"))
            except Exception:
                self.warnings.append("底图灰度化失败，已保留彩色")
        return self._register(new)

    def _ogr(self, path, name, subset=None):
        lyr = QgsVectorLayer(path, name, "ogr")
        if not lyr.isValid():
            raise FigureError("读取内置行政区划数据失败：" + path)
        if not lyr.crs().isValid():
            lyr.setCrs(QgsCoordinateReferenceSystem("EPSG:4490"))
        if subset and not lyr.setSubsetString(subset):
            raise FigureError("行政区划按范围过滤失败：" + subset)
        return self._register(lyr)

    def _mem(self, geom_type, crs, name, fields):
        uri = "{}?crs={}".format(geom_type, crs) + "".join("&field={}:{}".format(k, t) for k, t in fields)
        return self._register(QgsVectorLayer(uri, name, "memory"))

    # ----- 样式（只作用在克隆 / 临时图层上） -----
    def _style_double_outline(self, layer, outer, outer_w, inner, inner_w, dash=False):
        """黑底白线的双线轮廓：叠在卫星影像上任何底色都看得清，灰度打印也能分辨。"""
        base = {"style": "no", "color": "0,0,0,0", "joinstyle": "round"}
        sym = QgsFillSymbol.createSimple({**base, "outline_color": C.hex_to_rgba(outer),
                                          "outline_width": str(outer_w), "outline_style": "solid"})
        sym.appendSymbolLayer(QgsSimpleFillSymbolLayer.create({
            **base, "outline_color": C.hex_to_rgba(inner), "outline_width": str(inner_w),
            "outline_style": "dash" if dash else "solid"}))
        layer.setRenderer(QgsSingleSymbolRenderer(sym))

    def _style_circles(self, layer, on_imagery):
        if on_imagery:
            self._style_double_outline(layer, "#000000", 0.75, "#FFFFFF", 0.4, dash=True)
        else:
            C.style_polygon(layer, None, 1.0, "#000000", 0.35, True)

    def _style_admin(self, layer, level_key, on_imagery):
        width = {"province": 0.4, "city": 0.3, "county": 0.2}[level_key]
        if on_imagery:
            self._style_double_outline(layer, "#000000", width + 0.25, "#FFFFFF", max(width - 0.05, 0.12))
        else:
            C.style_polygon(layer, None, 1.0, "#000000" if level_key != "county" else "#4D4D4D", width, False)

    # ----- 主流程 -----
    def run(self):
        o = self.o
        was_dirty = self.project.isDirty()
        try:
            return self._run()
        finally:
            self._cleanup()
            self.project.setDirty(was_dirty)

    def _run(self):
        o = self.o

        # 1. 覆盖圆（直接取插件图层里的几何）
        self.step("读取覆盖圆…", 5)
        cov_layer = self.cov.layer
        if cov_layer is None or cov_layer.featureCount() == 0:
            raise FigureError("还没有机位覆盖圆：请先用「添加机位」落几个圆")
        feats = list(cov_layer.getFeatures())
        sel = set(cov_layer.selectedFeatureIds()) if o.only_selected else set()
        if sel:
            feats = [f for f in feats if f.id() in sel]
        feats.sort(key=lambda f: f.id())
        records = []
        for i, f in enumerate(feats, 1):
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            records.append({
                "no": i, "name": str(f["name"]), "radius": float(f["radius_m"]),
                "lon": float(f["lon"]), "lat": float(f["lat"]), "geom": g,
            })
        if not records:
            raise FigureError("覆盖圆没有有效几何")

        # 2. 制图坐标系
        lon0 = sum(r["lon"] for r in records) / len(records)
        lat0 = sum(r["lat"] for r in records) / len(records)
        epsg = pick_gk3_epsg(lon0) if o.crs_mode == "gk3" else None
        crs = QgsCoordinateReferenceSystem("EPSG:{}".format(epsg)) if epsg else QgsCoordinateReferenceSystem()
        if not crs.isValid():
            epsg = C.pick_utm_epsg(lon0, lat0)
            crs = QgsCoordinateReferenceSystem("EPSG:{}".format(epsg))
            if o.crs_mode == "gk3":
                self.warnings.append("所在位置不在 CGCS2000 3° 带范围内，已改用 WGS84 / UTM")
        crs_id = "EPSG:{}".format(epsg)
        self.step("制图坐标系 " + crs_id, 10)

        to_fig = self._tr(cov_layer.crs(), crs)
        for r in records:
            g = QgsGeometry(r["geom"])
            g.transform(to_fig)
            r["gfig"] = g
        circles_union = QgsGeometry.unaryUnion([r["gfig"] for r in records])

        # 3. 工程里的图层
        found = scan_layers(self.cov)
        admin_layers = [(lyr, lv) for lyr, lv in found["admin"] if self._visible(lyr)]
        level = o.region_level
        if level == "auto":
            level = admin.finest_level([lv for _, lv in admin_layers]) or "county"
        on_imagery = bool(o.basemap_ids)

        # 4. 主区域（落在哪些行政单元里）——用内置数据，不依赖用户加载了哪些行政区划图层
        self.step("识别覆盖圆所在的{}…".format(admin.level_label(level)), 15)
        units = self._region_units(level, circles_union, crs)
        if not units:
            self.warnings.append("覆盖圆不在内置行政区划范围内，已跳过区位插图与分区统计")

        # 5. 主图图层（自下而上）
        self.step("准备图层…", 25)
        stack = []   # 自下而上的图层对象

        order_bottom_up = [lyr for lyr in reversed(list(self.project.layerTreeRoot().layerOrder()))]
        for lyr in order_bottom_up:                       # 底图
            if lyr.id() in o.basemap_ids and isinstance(lyr, QgsRasterLayer):
                stack.append(self._clone_basemap(lyr))
        for lyr in order_bottom_up:                       # 其它矢量图层（保留原样式）
            if lyr.id() in o.other_ids and isinstance(lyr, QgsVectorLayer):
                stack.append(self._clone_vector(lyr))

        main_admin = {}                                   # level -> 克隆图层（用来标注、做图例）
        if admin_layers:
            for lyr, lv in sorted(admin_layers, key=lambda t: -["province", "city", "county"].index(t[1])):
                cl = self._clone_vector(lyr)
                cl.setLabelsEnabled(False)          # 面板里的标注是屏幕用的 11 pt，这里统一重新设置
                self._style_admin(cl, lv, on_imagery)
                stack.append(cl)
                main_admin.setdefault(lv, cl)
        elif units:                                       # 没加载任何行政区划图层时，自动补一层边界
            cl = self._ogr(admin.data_path(level), "行政边界")
            self._style_admin(cl, level, on_imagery)
            stack.append(cl)
            main_admin[level] = cl

        circ = self._mem("Polygon", crs_id, "覆盖范围", [("name", "string"), ("radius_m", "double")])
        pr = circ.dataProvider()
        cf = []
        for r in records:
            f = QgsFeature(circ.fields())
            f.setGeometry(r["gfig"])
            f.setAttributes([r["name"], r["radius"]])
            cf.append(f)
        pr.addFeatures(cf)
        circ.updateExtents()
        self._style_circles(circ, on_imagery)
        stack.append(circ)

        pts = self._mem("Point", "EPSG:4326", "机位", [("no", "string"), ("name", "string")])
        pf = []
        for r in records:
            f = QgsFeature(pts.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(r["lon"], r["lat"])))
            f.setAttributes([str(r["no"]), r["name"]])
            pf.append(f)
        pts.dataProvider().addFeatures(pf)
        pts.updateExtents()
        C.style_points(pts, 3.8, "#1A1A1A", "#FFFFFF", 0.3, "circle")
        stack.append(pts)

        # 6. 版面
        P = LAYOUTS[o.layout]
        right_col = 0.0
        has_inset = bool(o.inset and units)
        inset_spec = None
        if has_inset:
            inset_spec = self._build_inset(level, units, crs)
            if inset_spec is None:
                has_inset = False
        legend_pos = P["legend"]["position"] if o.legend else "none"
        need_col = legend_pos == "right" or (has_inset and P["inset"]["corner"] is None)
        right_col = 36.0 if need_col else 0.0

        self.step("建立版面…", 40)
        fig = C.SciFigure(self.project, TMP_LAYOUT_NAME, o.width_mm, o.max_height_mm,
                          P["font"], P["cjk_font"], P["margins"], right_col)
        self.fig = fig
        rect = self._main_extent(o.extent_mode, circles_union, units, crs)
        fig.add_main_map(stack, rect, crs_id)

        # 标注（中文）：机位编号（白字）、最细一级行政区名称
        pal_pts = fig.tf(6.0, True, "#FFFFFF", sample="0123456789")
        C.label_layer(pts, "no", pal_pts, "center")
        fine = admin.finest_level(list(main_admin))
        if fine:
            lyr = main_admin[fine]
            C.label_layer(lyr, "name", fig.tf(7.0 if o.layout == "right_column" else 8.0, False, "#000000",
                                              sample=C.field_sample(lyr, "name"), halo=True), "center")

        gr = P["graticule"]
        fig.add_graticule(None, False, gr["size_pt"], 2, gr["style"], gr["fmt"], "interior", gr["all_sides"])
        na = P["north_arrow"]
        if na["enabled"]:
            fig.add_north_arrow(na["corner"])
        if has_inset:
            ic = P["inset"]
            fig.add_inset(inset_spec["layers"], inset_spec["extent"], ic["width_mm"], ic["corner"],
                          ic["overview"], crs=inset_spec["crs"])
            sbc = P["scalebar"]
            fig.add_scalebar("bl", 1, sbc["size_pt"], 0.4, style=sbc["style"], length_m=None,
                             background=False, target="inset")
            inset_label_layers = inset_spec.get("label_layers", [])
            for il, size in inset_label_layers:
                C.label_layer(il, "name", fig.tf(size, False, "#000000", sample=C.field_sample(il, "name"),
                                                 halo=True), "center")
        sb = P["scalebar"]
        fig.add_scalebar(sb["corner"], sb["segments"], sb["size_pt"], sb["fraction"],
                         sb["style"], None, sb["background"])
        if legend_pos != "none":
            fig.add_legend(self._legend_entries(records, main_admin, stack),
                           legend_pos, None, P["legend"]["size_pt"],
                           P["legend"].get("frame"), P["legend"].get("background"))
        label = o.panel_label if o.panel_label is not None else ("(a)" if o.layout == "right_column" else "")
        if label:
            fig.add_panel_label(label)

        # 7. 统计 + 导出
        self.step("统计覆盖面积…", 60)
        os.makedirs(o.out_dir, exist_ok=True)
        base = os.path.join(o.out_dir, o.basename)
        summary = self._write_tables(base, records, units, crs_id, level)

        self.step("导出（{} dpi，在线底图需要下载瓦片，请稍候）…".format(o.dpi), 70)
        written = fig.export(o.out_dir, o.basename, o.formats, o.dpi)
        for w in fig.check_layout():
            self.warnings.append("版面：" + w)

        self._write_caption(base + "_图注建议.txt", summary, crs_id, level, units, stack)
        self.step("完成", 100)
        return {"files": written + summary["files"] + [base + "_图注建议.txt"],
                "summary": summary, "warnings": self.warnings, "crs": crs_id,
                "page_mm": (fig.dim["page_w"], fig.dim["page_h"])}

    # ----- 范围 -----
    def _main_extent(self, mode, circles_union, units, crs):
        if mode == "region" and units:
            geom = QgsGeometry.unaryUnion([u["geom"] for u in units] + [circles_union])
            return C.expand_rect(geom.boundingBox(), 0.04)
        if mode == "canvas":
            from qgis.utils import iface
            ext = iface.mapCanvas().extent()
            dst = iface.mapCanvas().mapSettings().destinationCrs()
            return self._tr(dst, crs).transformBoundingBox(ext)
        return C.expand_rect(circles_union.boundingBox(), 0.15)

    # ----- 主区域：覆盖圆落入的行政单元（取内置数据，几何转到制图坐标系） -----
    def _region_units(self, level, circles_union, crs):
        lyr = self._ogr(admin.data_path(level), "_region_" + level)
        bb = self._tr(crs, lyr.crs()).transformBoundingBox(circles_union.boundingBox())
        to_fig = self._tr(lyr.crs(), crs)
        units = []
        for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(bb)):
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            if not g.isGeosValid():
                g = g.makeValid()
            g.transform(to_fig)
            if g.intersects(circles_union):
                units.append({"gb": f["gb"], "name": f["name"], "geom": g})
        return units

    # ----- 区位插图：上一级 -----
    def _build_inset(self, level, units, crs):
        # 选覆盖面积最大的上级（覆盖圆跨市时取占比最大的那个）
        score = {}
        for u in units:
            pl, pg = admin.parent_of(level, u["gb"])
            score[(pl, pg)] = score.get((pl, pg), 0.0) + u["geom"].area()
        parent_level, parent_gb = max(score, key=score.get)

        mine = [u["gb"] for u in units if admin.parent_of(level, u["gb"]) == (parent_level, parent_gb)]
        hi_subset = "\"gb\" IN ({})".format(_quote(mine))
        label_layers = []
        if parent_level is None:                              # 上一级 = 全国
            sib = self._ogr(admin.data_path("province"), "插图·省界")
            hi = self._ogr(admin.data_path("province"), "插图·主区域", hi_subset)
            lines = QgsVectorLayer(admin.BOUNDARY_LINES, "插图·境界线", "ogr")
            layers = [sib, hi]
            C.style_polygon(sib, "#E6E6E6", 1.0, "#7F7F7F", 0.08, False)
            C.style_polygon(hi, "#000000", 1.0, "#000000", 0.08, False)
            if lines.isValid():
                lines.setCrs(QgsCoordinateReferenceSystem("EPSG:4490"))
                self._register(lines)
                C.style_line(lines, "#4D4D4D", 0.1, False)
                layers.append(lines)
            ins_crs = QgsCoordinateReferenceSystem.fromProj(ALBERS_CHINA)
            if not ins_crs.isValid():
                return None
            extent_src = sib
            extra_src = lines if lines.isValid() else None
        else:
            parent_sub = admin.subset_for(None, parent_gb)
            sib = self._ogr(admin.data_path(level), "插图·同级", parent_sub)
            hi = self._ogr(admin.data_path(level), "插图·主区域", hi_subset)
            outline = self._ogr(admin.data_path(parent_level), "插图·上级轮廓", "\"gb\" = '{}'".format(parent_gb))
            C.style_polygon(sib, "#D9D9D9", 1.0, "#7F7F7F", 0.08, False)
            C.style_polygon(hi, "#000000", 1.0, "#000000", 0.08, False)
            C.style_polygon(outline, None, 1.0, "#000000", 0.25, False)
            layers = [sib, hi, outline]
            # 区位插图不标注区县文字：小图里文字互相压叠，仅保留色块与轮廓更清爽
            label_layers = []
            ins_crs = crs
            extent_src = outline
            extra_src = None
        extent_src.updateExtents()
        rect = self._tr(extent_src.crs(), ins_crs).transformBoundingBox(extent_src.extent())
        if extra_src is not None:
            extra_src.updateExtents()
            rect.combineExtentWith(self._tr(extra_src.crs(), ins_crs).transformBoundingBox(extra_src.extent()))
        return {"layers": layers, "extent": C.expand_rect(rect, 0.06), "crs": ins_crs,
                "label_layers": label_layers, "parent": (parent_level, parent_gb)}

    # ----- 图例（中文） -----
    def _legend_entries(self, records, main_admin, stack):
        entries = []
        pts = next(l for l in stack if l.name() == "机位")
        circ = next(l for l in stack if l.name() == "覆盖范围")
        radii = {round(r["radius"]) for r in records}
        entries.append((pts, "机位（编号见表）"))
        entries.append((circ, "覆盖范围（半径 {:g} km）".format(next(iter(radii)) / 1000.0)
                        if len(radii) == 1 else "覆盖范围"))
        names = {"province": "省界", "city": "市界", "county": "县（区）界"}
        for lv in ("province", "city", "county"):
            if lv in main_admin:
                entries.append((main_admin[lv], names[lv]))
        skip = {id(pts), id(circ)} | {id(v) for v in main_admin.values()}
        for l in reversed(stack):
            if id(l) in skip or isinstance(l, QgsRasterLayer):
                continue
            entries.append((l, l.name()))
        return entries

    # ----- 统计表 -----
    def _write_tables(self, base, records, units, crs_id, level):
        da = C.distance_area(crs_id, self.project)
        area = lambda g: C.area_km2(g, da)
        geoms = [r["gfig"] for r in records]
        union = QgsGeometry.unaryUnion(geoms)
        union_km2 = area(union)

        per = []
        for i, r in enumerate(records):
            others = [g for j, g in enumerate(geoms) if j != i]
            uniq = r["gfig"].difference(QgsGeometry.unaryUnion(others)) if others else r["gfig"]
            per.append({"no": r["no"], "name": r["name"], "radius_km": r["radius"] / 1000.0,
                        "lon": r["lon"], "lat": r["lat"],
                        "area_km2": area(r["gfig"]), "unique_km2": area(uniq)})
        total_single = sum(p["area_km2"] for p in per)

        unit_rows = []
        for u in units:
            ua = area(u["geom"])
            ca = area(u["geom"].intersection(union))
            unit_rows.append({"name": u["name"], "gb": u["gb"], "area_km2": ua, "covered_km2": ca,
                              "ratio": (ca / ua) if ua else 0.0})
        unit_rows.sort(key=lambda r: -r["covered_km2"])

        files = []
        with open(base + "_机位对照.csv", "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["编号", "机位名称", "经度", "纬度", "半径(km)", "覆盖面积(km²)", "独有面积(km²)"])
            for p in per:
                w.writerow([p["no"], p["name"], "%.6f" % p["lon"], "%.6f" % p["lat"],
                            "%.2f" % p["radius_km"], "%.3f" % p["area_km2"], "%.3f" % p["unique_km2"]])
        files.append(base + "_机位对照.csv")

        with open(base + "_统计.csv", "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["覆盖总面积(并集,km²)", "各机位面积之和(km²)", "重叠面积(km²)"])
            w.writerow(["%.3f" % union_km2, "%.3f" % total_single, "%.3f" % max(total_single - union_km2, 0.0)])
            w.writerow([])
            w.writerow(["{}名称".format(admin.level_label(level)), "行政区划代码", "面积(km²)", "被覆盖面积(km²)", "覆盖率"])
            for r in unit_rows:
                w.writerow([r["name"], r["gb"], "%.3f" % r["area_km2"], "%.3f" % r["covered_km2"],
                            "%.2f%%" % (r["ratio"] * 100)])
        files.append(base + "_统计.csv")
        return {"union_km2": union_km2, "total_km2": total_single, "points": per, "units": unit_rows, "files": files}

    # ----- 图注建议 -----
    def _write_caption(self, path, summary, crs_id, level, units, stack):
        attrs = []
        for l in stack:
            if isinstance(l, QgsRasterLayer):
                for b in BASEMAPS:
                    if b["name"] == l.name() and b.get("attribution"):
                        attrs.append("{}（{}）".format(b["attribution"], b["name"]))
        lines = [
            "图注建议（请按期刊格式修改；图内不放图名与统计数字）",
            "",
            "图 X  机场覆盖范围示意。圆为机场有效覆盖范围，数字为机位编号，对照见 *_机位对照.csv。",
            "",
            "覆盖总面积 %.2f km²（并集）" % summary["union_km2"]
            + ("；其中" + "、".join("{}覆盖 {:.2f} km²（{:.1f}%）".format(u["name"], u["covered_km2"], u["ratio"] * 100)
                                    for u in summary["units"][:5]) if summary["units"] else ""),
            "坐标系：%s；经纬度为 CGCS2000/WGS84（二者差异在厘米级，制图可忽略）。" % crs_id,
            "底图来源：" + ("；".join(attrs) if attrs else "（未使用底图）"),
            "行政区划数据：（请补充数据来源）",
            "审图号：（使用天地图等底图或公开发布时必须标注，请补充）",
            "",
            "提醒：在线影像用于出版前请确认对应图源的使用条款与署名要求。",
        ]
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write("\n".join(lines) + "\n")


def export_figure(coverage, opts, progress=None):
    """对话框调用的入口。返回结果 dict；出错抛 FigureError（消息可直接展示）。"""
    return FigureJob(coverage, opts, progress).run()
