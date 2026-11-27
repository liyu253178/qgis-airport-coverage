# -*- coding: utf-8 -*-
"""sci_map_core —— 基于 PyQGIS 的 SCI / 期刊风格制图核心库。

设计原则
--------
1. 版面按期刊最终印刷尺寸（如双栏 180 mm）直接建，字号 = 最终印刷字号，不再"A2 出大图再缩小"。
2. 图内只放地图要素（地图、经纬网、指北针、比例尺、图例、插图、(a) 标号）；
   标题、数据来源、统计数字放图注 / 表格，统计结果另存 CSV。
3. 比例尺用 QGIS 原生控件并链接到地图项，不手算像素。
4. 所有会随环境变化的东西（路径、字体、QGIS 版本枚举）都做了探测或回退。

文件分两层：
  - 「纯 Python」部分（顶部）：不依赖 QGIS，可单元测试（见 selftest.py）。
  - 「PyQGIS」部分：需要在 QGIS 自带 Python / 已配置好 PyQGIS 的环境里运行。
"""

from __future__ import annotations

import csv
import glob
import json
import math
import os
import platform
import re
import sys
import tempfile

# =====================================================================
# 一、纯 Python 辅助（无 QGIS 依赖）
# =====================================================================

# 常见期刊栏宽（mm）。具体期刊以投稿须知为准，详见 references/journal-specs.md
FIGURE_WIDTH_MM = {
    "single": 85.0,     # 单栏（Nature 89 / Elsevier 90 / Springer 84，取通用值 85）
    "onehalf": 130.0,   # 1.5 栏
    "double": 180.0,    # 双栏（Nature 183 / Elsevier 190 / Springer 174，取通用值 180）
}

# 色盲友好色板（Okabe-Ito），灰度打印时依然可分辨需再配合线型/明度
OKABE_ITO = ["#000000", "#E69F00", "#56B4E9", "#009E73",
             "#F0E442", "#0072B2", "#D55E00", "#CC79A7"]
GRAY_RAMP = ["#1A1A1A", "#4D4D4D", "#808080", "#B3B3B3", "#E6E6E6"]

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")


def contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def hex_to_rgba(hex_color: str, alpha: float = 1.0) -> str:
    """'#1A1A1A', 0.5 -> '26,26,26,128'（QGIS 符号属性使用的颜色字符串）。"""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return "{},{},{},{}".format(r, g, b, int(round(max(0.0, min(1.0, alpha)) * 255)))


def pick_utm_epsg(lon: float, lat: float) -> int:
    """按经纬度选 WGS84 / UTM 带的 EPSG（北半球 326xx，南半球 327xx）。"""
    zone = int((lon + 180.0) // 6.0) + 1
    zone = max(1, min(60, zone))
    return (32600 if lat >= 0 else 32700) + zone


def nice_floor(x: float, steps=(1.0, 2.0, 5.0)) -> float:
    """向下取 1/2/5 × 10^k 的"整齐数"。"""
    if x <= 0:
        raise ValueError("x must be > 0")
    exp = math.floor(math.log10(x))
    base = 10.0 ** exp
    best = steps[0] * base
    for s in steps:
        if s * base <= x * (1 + 1e-9):
            best = s * base
    return best


def nice_scalebar(map_width_m: float, fraction: float = 0.28, segments: int = 2,
                  length_m: float | None = None):
    """返回 (每段长度 m, 段数, 单位 'km'|'m', 每段数值)。

    比例尺总长约占地图宽度的 fraction，每段取 1/2/5 × 10^k；
    给定 length_m 时直接取总长 = length_m（如插图固定画 100 km）。
    """
    seg_m = (length_m / segments) if length_m else nice_floor(map_width_m * fraction / segments)
    if seg_m >= 1000.0:
        return seg_m, segments, "km", seg_m / 1000.0
    return seg_m, segments, "m", seg_m


_DEG_STEPS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2,
              0.25, 0.5, 1.0, 2.0, 5.0, 10.0]


def nice_degree_interval(span_deg: float, target_ticks: int = 4) -> float:
    """为经纬网挑一个整齐的度间隔，使刻度数接近 target_ticks。"""
    ideal = span_deg / max(target_ticks, 1)
    return min(_DEG_STEPS, key=lambda s: abs(math.log(s / ideal)))


_MIN_STEPS = [1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600]   # 单位：角分


def nice_minute_interval(span_deg: float, target_ticks: float = 2.5) -> float:
    """度分格式用的经纬网间隔（返回度）：从 1'、2'、5'、10'、15'、30'、1°… 里挑。"""
    ideal_min = span_deg * 60.0 / max(target_ticks, 1)
    best = min(_MIN_STEPS, key=lambda m: abs(math.log(m / ideal_min)))
    return best / 60.0


def compass8(dx: float, dy: float) -> str:
    """向量（东向 dx、北向 dy）→ 八方位英文缩写；用于自动描述盲区所在方位。"""
    ang = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0  # 自北顺时针
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int((ang + 22.5) // 45) % 8]


# ---- 测地线（Vincenty 正解，WGS84），不依赖任何插件 ----
_WGS84_A = 6378137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_B = _WGS84_A * (1.0 - _WGS84_F)


def vincenty_direct(lon1: float, lat1: float, azimuth_deg: float, dist_m: float):
    """已知起点、方位角、距离，求终点 (lon, lat)。精度约毫米级。"""
    a, f, b = _WGS84_A, _WGS84_F, _WGS84_B
    alpha1 = math.radians(azimuth_deg)
    sin_a1, cos_a1 = math.sin(alpha1), math.cos(alpha1)
    tan_u1 = (1 - f) * math.tan(math.radians(lat1))
    cos_u1 = 1 / math.sqrt(1 + tan_u1 ** 2)
    sin_u1 = tan_u1 * cos_u1
    sigma1 = math.atan2(tan_u1, cos_a1)
    sin_alpha = cos_u1 * sin_a1
    cos2_alpha = 1 - sin_alpha ** 2
    u_sq = cos2_alpha * (a * a - b * b) / (b * b)
    A = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    B = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))

    sigma = dist_m / (b * A)
    for _ in range(100):
        cos2sm = math.cos(2 * sigma1 + sigma)
        sin_s, cos_s = math.sin(sigma), math.cos(sigma)
        d_sigma = B * sin_s * (cos2sm + B / 4 * (
            cos_s * (-1 + 2 * cos2sm ** 2)
            - B / 6 * cos2sm * (-3 + 4 * sin_s ** 2) * (-3 + 4 * cos2sm ** 2)))
        new_sigma = dist_m / (b * A) + d_sigma
        if abs(new_sigma - sigma) < 1e-12:
            sigma = new_sigma
            break
        sigma = new_sigma

    sin_s, cos_s = math.sin(sigma), math.cos(sigma)
    cos2sm = math.cos(2 * sigma1 + sigma)
    x = sin_u1 * sin_s - cos_u1 * cos_s * cos_a1
    lat2 = math.atan2(sin_u1 * cos_s + cos_u1 * sin_s * cos_a1,
                      (1 - f) * math.sqrt(sin_alpha ** 2 + x ** 2))
    lam = math.atan2(sin_s * sin_a1, cos_u1 * cos_s - sin_u1 * sin_s * cos_a1)
    C = f / 16 * cos2_alpha * (4 + f * (4 - 3 * cos2_alpha))
    L = lam - (1 - C) * f * sin_alpha * (
        sigma + C * sin_s * (cos2sm + C * cos_s * (-1 + 2 * cos2sm ** 2)))
    lon2 = lon1 + math.degrees(L)
    return ((lon2 + 540.0) % 360.0) - 180.0, math.degrees(lat2)


def geodesic_circle_lonlat(lon: float, lat: float, radius_m: float, n: int = 360):
    """以 (lon, lat) 为圆心、测地线半径 radius_m 的圆，返回闭合的 [(lon, lat), ...]。"""
    pts = [vincenty_direct(lon, lat, 360.0 * i / n, radius_m) for i in range(n)]
    pts.append(pts[0])
    return pts


def figure_dimensions(width_mm, extent_w, extent_h, margins, right_col_mm=0.0,
                      gap_mm=3.0, max_height_mm=230.0):
    """根据地图范围纵横比算整页尺寸与地图项尺寸（纯计算，可测试）。

    margins = (left, top, right, bottom) mm，左/下要给经纬度刻度标注留位。
    返回 dict: page_w, page_h, map_x, map_y, map_w, map_h, col_x, col_w, warn。
    """
    ml, mt, mr, mb = margins
    aspect = extent_h / extent_w
    gap = gap_mm if right_col_mm > 0 else 0.0
    map_w = width_mm - ml - mr - right_col_mm - gap
    map_h = map_w * aspect
    warn = None
    if mt + map_h + mb > max_height_mm:
        map_h = max_height_mm - mt - mb
        map_w = map_h / aspect
        warn = ("地图过高，已按 max_height_mm=%.0f 缩小；右栏相应变宽。"
                "可考虑收紧范围或改用双栏。" % max_height_mm)
    page_w = width_mm
    col_w = 0.0
    if right_col_mm > 0:
        col_w = page_w - ml - map_w - gap - mr
    else:
        page_w = ml + map_w + mr
        if page_w < width_mm - 0.5:
            warn = (warn or "") + " 页宽小于栏宽（%.1f < %.1f mm）。" % (page_w, width_mm)
    return {
        "page_w": page_w, "page_h": mt + map_h + mb,
        "map_x": ml, "map_y": mt, "map_w": map_w, "map_h": map_h,
        "col_x": ml + map_w + gap, "col_w": col_w, "warn": warn,
    }


def write_north_arrow_svg(path: str) -> str:
    """黑白半填充指北针；N 用路径画，不依赖字体。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="60" viewBox="0 0 40 60">'
        '<path d="M14 14 V3 L26 14 V3" stroke="#000" stroke-width="2.2" fill="none" '
        'stroke-linejoin="miter"/>'
        '<polygon points="20,18 10,56 20,48" fill="#000" stroke="#000" stroke-width="1"/>'
        '<polygon points="20,18 30,56 20,48" fill="#fff" stroke="#000" stroke-width="1.6"/>'
        '</svg>'
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return path


# =====================================================================
# 二、PyQGIS 部分
# =====================================================================

try:
    from qgis.core import (
        Qgis,
        QgsApplication,
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsDistanceArea,
        QgsFeature,
        QgsFillSymbol,
        QgsGeometry,
        QgsLayoutExporter,
        QgsLayoutItem,
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemMap,
        QgsLayoutItemMapGrid,
        QgsLayoutItemMapOverview,
        QgsLayoutItemPicture,
        QgsLayoutItemScaleBar,
        QgsLayoutMeasurement,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsLegendStyle,
        QgsLineSymbol,
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsPointXY,
        QgsPrintLayout,
        QgsProject,
        QgsRasterLayer,
        QgsRectangle,
        QgsSingleSymbolRenderer,
        QgsTextBufferSettings,
        QgsTextFormat,
        QgsUnitTypes,
        QgsVectorLayer,
        QgsVectorLayerSimpleLabeling,
        QgsWkbTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor, QFont
    HAVE_QGIS = True
except ImportError:  # 允许在无 QGIS 环境下导入本模块做纯 Python 测试
    HAVE_QGIS = False


def log(msg: str) -> None:
    print("[sci-map] " + str(msg), flush=True)
    try:  # 在 QGIS 里同时写入「日志消息」面板
        from qgis.core import QgsMessageLog
        QgsMessageLog.logMessage(str(msg), "机场覆盖区规划·出图")
    except Exception:
        pass


def enum(owner, scope: str, name: str):
    """兼容不同 QGIS 版本的枚举写法：优先 Owner.Scope.Name，回退 Owner.Name。"""
    sc = getattr(owner, scope, None)
    if sc is not None and hasattr(sc, name):
        return getattr(sc, name)
    return getattr(owner, name)


def _mm():
    lu = getattr(Qgis, "LayoutUnit", None)
    if lu is not None and hasattr(lu, "Millimeters"):
        return lu.Millimeters
    return QgsUnitTypes.LayoutMillimeters


def _item_size(item, default_w=30.0, default_h=15.0):
    """读控件自动调整后的实际尺寸（mm）；读不到时用默认值。"""
    try:
        r = item.rect()
        if r.width() > 0 and r.height() > 0:
            return r.width(), r.height()
    except Exception:
        pass
    return default_w, default_h


def _place(item, x, y, w, h):
    mm = _mm()
    item.attemptMove(QgsLayoutPoint(x, y, mm))
    item.attemptResize(QgsLayoutSize(w, h, mm))


# ---------------- 环境 ----------------

def _qgis_prefix_candidates():
    sysname = platform.system()
    c = [os.environ.get("QGIS_PREFIX_PATH")]
    if sysname == "Darwin":
        c += glob.glob("/Applications/QGIS*.app/Contents/MacOS")
    elif sysname == "Windows":
        c += glob.glob(r"C:\Program Files\QGIS*\apps\qgis*")
    else:
        c += ["/usr", "/usr/local"]
    return [p for p in c if p and os.path.isdir(p)]


def init_qgis(prefix: str | None = None):
    """初始化 QgsApplication（无界面）。返回 (qgs, owned)。

    若已在 QGIS 进程里（QGIS Python 控制台 / QGIS MCP 的 execute_code），直接复用现有实例，
    不重复初始化，也不会在结束时退出用户的 QGIS（owned=False）。
    """
    if not HAVE_QGIS:
        raise RuntimeError("无法 import qgis。请用 QGIS 自带 Python 运行，或先配置 PYTHONPATH，"
                           "详见 references/pyqgis-pitfalls.md 的「运行环境」。")
    existing = QgsApplication.instance()
    if existing is not None:
        log("检测到已在运行的 QGIS %s，复用现有实例" % Qgis.QGIS_VERSION)
        return existing, False
    if platform.system() == "Darwin":
        for app in glob.glob("/Applications/QGIS*.app"):
            proj = os.path.join(app, "Contents/Resources/qgis/proj")
            if os.path.isdir(proj):
                os.environ.setdefault("PROJ_DATA", proj)
                break
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    cands = [prefix] if prefix else _qgis_prefix_candidates()
    if cands:
        QgsApplication.setPrefixPath(cands[0], True)
    qgs = QgsApplication([], False)
    qgs.initQgis()
    log("QGIS %s 已初始化" % Qgis.QGIS_VERSION)
    return qgs, True


def shutdown_qgis(qgs, owned: bool = True) -> None:
    """仅退出自己创建的实例；macOS 无头模式 exitQgis 偶发段错误，成果已写出，包一层保护。"""
    if not owned:
        return
    try:
        qgs.exitQgis()
    except Exception:
        pass


def probe_environment() -> dict:
    """探测当前 QGIS 的版本、字体、关键枚举是否可用——出图前或报错时调用，把结果贴给我。"""
    info = {"qgis": Qgis.QGIS_VERSION, "platform": platform.platform(), "python": sys.version.split()[0]}
    checks = {
        "Qgis.LayoutUnit.Millimeters": lambda: Qgis.LayoutUnit.Millimeters,
        "Qgis.LabelPlacement.OverPoint": lambda: Qgis.LabelPlacement.OverPoint,
        "Qgis.TextRenderFormat.AlwaysText": lambda: Qgis.TextRenderFormat.AlwaysText,
        "MapGrid.DegreeMinuteNoPadding": lambda: enum(QgsLayoutItemMapGrid, "AnnotationFormat", "DegreeMinuteNoPadding"),
        "MapGrid.InteriorTicks": lambda: enum(QgsLayoutItemMapGrid, "FrameStyle", "InteriorTicks"),
        "ScaleBar.setTextFormat": lambda: QgsLayoutItemScaleBar.setTextFormat,
        "TextFormat.setForcedBold": lambda: QgsTextFormat.setForcedBold,
    }
    info["api"] = {}
    for k, fn in checks.items():
        try:
            fn()
            info["api"][k] = "ok"
        except Exception as e:  # noqa: BLE001
            info["api"][k] = "MISSING (%s)" % type(e).__name__
    info["fonts"] = {f: resolve_font([f], default="(未安装)") for f in
                     ("Arial", "Times New Roman", "Songti SC", "SimSun", "Noto Serif CJK SC")}
    return info


def resolve_font(candidates, default="Arial") -> str:
    """从候选字体中挑第一个系统已装的；枚举失败时返回第一个候选。"""
    if isinstance(candidates, str):
        candidates = [candidates]
    try:
        from qgis.PyQt.QtGui import QFontDatabase
        try:
            fams = set(QFontDatabase().families())
        except TypeError:           # Qt6：静态方法
            fams = set(QFontDatabase.families())
    except Exception:
        return candidates[0] if candidates else default
    for c in candidates:
        if c in fams:
            return c
    log("警告：候选字体 %s 均未安装，回退到 %s" % (candidates, default))
    return default


# ---------------- 文字格式 ----------------

def text_format(family: str, size_pt: float, color: str = "#000000", bold: bool = False,
                halo: bool = False, halo_mm: float = 0.25) -> "QgsTextFormat":
    tf = QgsTextFormat()
    f = QFont(family)
    f.setBold(bold)
    tf.setFont(f)
    tf.setSize(size_pt)
    try:
        tf.setSizeUnit(enum(Qgis, "RenderUnit", "Points"))
    except Exception:
        tf.setSizeUnit(QgsUnitTypes.RenderPoints)
    tf.setColor(QColor(color))
    if bold and hasattr(tf, "setForcedBold"):
        tf.setForcedBold(True)
    if halo:
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(halo_mm)
        buf.setSizeUnit(_mm_render())
        buf.setColor(QColor("#FFFFFF"))
        tf.setBuffer(buf)
    return tf


def _mm_render():
    try:
        return enum(Qgis, "RenderUnit", "Millimeters")
    except Exception:
        return QgsUnitTypes.RenderMillimeters


# ---------------- 几何 / 统计 ----------------

def ctransform(src: str, dst: str, project) -> "QgsCoordinateTransform":
    return QgsCoordinateTransform(QgsCoordinateReferenceSystem(src),
                                  QgsCoordinateReferenceSystem(dst),
                                  project.transformContext())


def union_layer_geometry(layer, to_crs_authid: str, project) -> "QgsGeometry":
    """把图层里所有要素并成一个几何并转到目标 CRS（边界图层常含多个要素，不能只取第一个）。"""
    tr = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem(to_crs_authid),
                                project.transformContext())
    geoms = []
    for f in layer.getFeatures():
        g = QgsGeometry(f.geometry())
        if g.isNull() or g.isEmpty():
            continue
        if not g.isGeosValid():
            g = g.makeValid()
        g.transform(tr)
        geoms.append(g)
    if not geoms:
        raise RuntimeError("图层 %s 没有有效几何" % layer.name())
    return QgsGeometry.unaryUnion(geoms)


def distance_area(crs_authid: str, project, ellipsoid: str = "WGS84") -> "QgsDistanceArea":
    da = QgsDistanceArea()
    da.setSourceCrs(QgsCoordinateReferenceSystem(crs_authid), project.transformContext())
    da.setEllipsoid(ellipsoid)
    return da


def area_km2(geom, da) -> float:
    if geom is None or geom.isNull() or geom.isEmpty():
        return 0.0
    return da.measureArea(geom) / 1e6      # 已设椭球 → 返回 m²


def coverage_stats(region_geom, circle_geoms, labels, crs_authid, project):
    """覆盖率统计：全部量算在椭球面上；盲区自动按方位描述（不再手写文字）。"""
    da = distance_area(crs_authid, project)
    union = QgsGeometry.unaryUnion(circle_geoms)
    covered = union.intersection(region_geom)
    blind = region_geom.difference(union)
    town = area_km2(region_geom, da)
    cover = area_km2(covered, da)

    per_point = []
    for i, (lab, g) in enumerate(zip(labels, circle_geoms)):
        others = QgsGeometry.unaryUnion([x for j, x in enumerate(circle_geoms) if j != i])
        per_point.append({
            "label": lab,
            "in_region_km2": area_km2(g.intersection(region_geom), da),
            "unique_km2": area_km2(g.difference(others).intersection(region_geom), da),
        })

    rc = region_geom.centroid().asPoint()
    blind_parts = []
    for part in blind.asGeometryCollection():
        if part.type() != QgsWkbTypes.PolygonGeometry:
            continue
        a = area_km2(part, da)
        if a < 0.05:            # 忽略碎屑
            continue
        c = part.centroid().asPoint()
        blind_parts.append({"direction": compass8(c.x() - rc.x(), c.y() - rc.y()), "km2": a})
    blind_parts.sort(key=lambda d: -d["km2"])

    return {
        "region_km2": town, "covered_km2": cover,
        "blind_km2": max(town - cover, 0.0),
        "coverage_ratio": cover / town if town else 0.0,
        "per_point": per_point, "blind_parts": blind_parts,
    }


def write_stats(stats: dict, base_path: str) -> None:
    """统计结果落盘（JSON + CSV），写论文表格直接引用，不要写进图里。"""
    with open(base_path + "_stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    with open(base_path + "_stats.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item", "in_region_km2", "unique_km2"])
        for p in stats["per_point"]:
            w.writerow([p["label"], "%.3f" % p["in_region_km2"], "%.3f" % p["unique_km2"]])
        w.writerow([])
        w.writerow(["region_km2", "covered_km2", "blind_km2", "coverage_ratio"])
        w.writerow(["%.3f" % stats["region_km2"], "%.3f" % stats["covered_km2"],
                    "%.3f" % stats["blind_km2"], "%.4f" % stats["coverage_ratio"]])
        w.writerow([])
        w.writerow(["blind_direction", "km2"])
        for b in stats["blind_parts"]:
            w.writerow([b["direction"], "%.3f" % b["km2"]])


# ---------------- 图层构建 ----------------

def add_vector(project, path: str, name: str, assume_crs: str = "EPSG:4326"):
    lyr = QgsVectorLayer(path, name, "ogr")
    if not lyr.isValid():
        raise RuntimeError("矢量图层加载失败: " + path)
    if not lyr.crs().isValid():             # GeoJSON 常无 CRS 声明
        lyr.setCrs(QgsCoordinateReferenceSystem(assume_crs))
    project.addMapLayer(lyr)
    return lyr


def add_raster(project, path: str, name: str, opacity: float = 1.0):
    lyr = QgsRasterLayer(path, name)
    if not lyr.isValid():
        raise RuntimeError("栅格图层加载失败: " + path)
    lyr.setOpacity(opacity)
    project.addMapLayer(lyr)
    return lyr


def make_point_layer(project, name: str, features: list, fields: dict | None = None):
    """内存点图层（WGS84）。features: [{'lon','lat', 其余键作属性}]；
    fields: {属性名: 'integer'|'double'|'string'}，缺省按值类型推断。"""
    if fields is None:
        fields = {}
        for k, v in features[0].items():
            if k in ("lon", "lat"):
                continue
            fields[k] = "integer" if isinstance(v, int) else "double" if isinstance(v, float) else "string"
    uri = "Point?crs=EPSG:4326" + "".join("&field=%s:%s" % (k, t) for k, t in fields.items())
    lyr = QgsVectorLayer(uri, name, "memory")
    prov = lyr.dataProvider()
    feats = []
    for d in features:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(d["lon"], d["lat"])))
        f.setAttributes([d.get(k) for k in fields])
        feats.append(f)
    prov.addFeatures(feats)
    lyr.updateExtents()
    project.addMapLayer(lyr)
    return lyr


def make_geodesic_buffer_layer(project, name: str, points_layer, radius_m: float,
                               label_field: str | None = None, n: int = 360):
    """每个点生成测地线圆（WGS84 顶点，渲染时由 QGIS 重投影成正圆），不依赖第三方插件。"""
    lyr = QgsVectorLayer("Polygon?crs=EPSG:4326&field=src:string&field=radius_m:double",
                         name, "memory")
    prov = lyr.dataProvider()
    tr = QgsCoordinateTransform(points_layer.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                project.transformContext())
    feats = []
    for pf in points_layer.getFeatures():
        p = tr.transform(pf.geometry().asPoint())
        ring = [QgsPointXY(x, y) for x, y in geodesic_circle_lonlat(p.x(), p.y(), radius_m, n)]
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPolygonXY([ring]))
        src = str(pf[label_field]) if label_field else str(pf.id())
        f.setAttributes([src, radius_m])
        feats.append(f)
    prov.addFeatures(feats)
    lyr.updateExtents()
    project.addMapLayer(lyr)
    return lyr


# ---------------- 符号 / 标注 ----------------

def style_polygon(layer, fill: str | None = "#EDEDED", fill_alpha: float = 1.0,
                  stroke: str = "#000000", stroke_mm: float = 0.25, dash: bool = False):
    props = {
        "outline_color": hex_to_rgba(stroke), "outline_width": str(stroke_mm),
        "outline_style": "dash" if dash else "solid", "joinstyle": "round",
    }
    if fill is None:
        props.update({"style": "no", "color": "0,0,0,0"})
    else:
        props.update({"style": "solid", "color": hex_to_rgba(fill, fill_alpha)})
    layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(props)))


def style_line(layer, color: str = "#000000", width_mm: float = 0.25, dash: bool = False):
    layer.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({
        "line_color": hex_to_rgba(color), "line_width": str(width_mm),
        "line_style": "dash" if dash else "solid"})))


def style_points(layer, size_mm: float = 3.6, fill: str = "#1A1A1A",
                 stroke: str | None = "#FFFFFF", stroke_mm: float = 0.3, shape: str = "circle",
                 fill_alpha: float = 1.0):
    """stroke=None 表示无描边（密集小点，如参考图里的橙色点）。"""
    props = {"name": shape, "size": str(size_mm), "color": hex_to_rgba(fill, fill_alpha)}
    if stroke is None:
        props["outline_style"] = "no"
    else:
        props.update({"outline_color": hex_to_rgba(stroke), "outline_width": str(stroke_mm)})
    layer.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple(props)))


def label_layer(layer, field: str, tf: "QgsTextFormat", mode: str = "center",
                dist_mm: float = 1.5):
    """mode: 'center' 标在点正中（做编号圆点）；'around' 绕点放置（名称标注）。"""
    pal = QgsPalLayerSettings()
    pal.fieldName = field
    pal.setFormat(tf)
    if mode == "center":           # 点：标在点上；面：标在质心（区名标注）
        pal.placement = enum(Qgis, "LabelPlacement", "OverPoint")
        pal.dist = 0
        if hasattr(pal, "centroidInside"):
            pal.centroidInside = True
    else:
        pal.placement = enum(Qgis, "LabelPlacement", "AroundPoint")
        pal.dist = dist_mm
        pal.distUnits = _mm_render()
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def field_sample(layer, field: str) -> str:
    """取字段全部取值拼成字符串，用来判断标注是否含中文（决定用哪种字体）。"""
    try:
        return "".join(str(f[field]) for f in layer.getFeatures())
    except Exception:
        return ""


# ---------------- 版面 ----------------

_LATIN_FALLBACK = {
    "Times New Roman": ["Times New Roman", "Times", "Liberation Serif", "DejaVu Serif"],
    "Arial": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
}
_CJK_SERIF = ["Songti SC", "STSong", "SimSun", "Noto Serif CJK SC", "Source Han Serif SC"]
_CJK_SANS = ["PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC"]


class SciFigure:
    """期刊尺寸的打印布局。

    两种版式（由 right_col_mm 与各 add_* 的 position 组合出来）：
      A. 右栏式：地图 + 右侧栏放图例/插图，经纬网用斑马边框（英文 SCI 常用）
      B. 单幅内嵌式：地图撑满页面，图例/插图/比例尺都在图内，四边刻度 + 度分标注
         （中文期刊常见，见 add_graticule(style="ticks", fmt="dm", all_sides=True)）
    典型用法见 scripts/examples/ 与 build_map.py。
    """

    def __init__(self, project, name: str, width_mm: float = 180.0, max_height_mm: float = 230.0,
                 font: str = "Arial", cjk_font: str | None = None,
                 margins=(12.0, 5.0, 3.0, 10.0), right_col_mm: float = 36.0):
        self.project = project
        self.name = name
        self.width_mm = width_mm
        self.max_height_mm = max_height_mm
        self.font = resolve_font(_LATIN_FALLBACK.get(font, [font] + _LATIN_FALLBACK["Arial"]))
        if cjk_font:
            cands = [cjk_font] + _CJK_SERIF + _CJK_SANS
            self.cjk_font = resolve_font(cands, default=cjk_font)
        else:
            self.cjk_font = None
        self.margins = margins
        self.right_col_mm = right_col_mm
        self.layout = None
        self.map_item = None
        self.dim = None
        self.inset_box = None

    # -- 字体：含中文的文本改用 CJK 字体（sample 传入将要显示的文字）
    def tf(self, size, bold=False, color="#000000", sample="", halo=False):
        fam = self.cjk_font if (self.cjk_font and contains_cjk(sample)) else self.font
        return text_format(fam, size, color, bold, halo)

    # -- 主地图
    def add_main_map(self, layers_bottom_to_top, extent: "QgsRectangle", crs_authid: str):
        self.dim = figure_dimensions(self.width_mm, extent.width(), extent.height(),
                                     self.margins, self.right_col_mm,
                                     max_height_mm=self.max_height_mm)
        if self.dim["warn"]:
            log("警告：" + self.dim["warn"])
        lay = QgsPrintLayout(self.project)
        lay.initializeDefaults()
        lay.setName(self.name)
        lm = self.project.layoutManager()
        old = lm.layoutByName(self.name)
        if old is not None:          # 重名时 addLayout 会返回 False，布局就不在工程里了
            lm.removeLayout(old)
        lm.addLayout(lay)
        page = lay.pageCollection().pages()[0]
        page.setPageSize(QgsLayoutSize(self.dim["page_w"], self.dim["page_h"], _mm()))
        self.layout = lay

        m = QgsLayoutItemMap(lay)
        m.setCrs(QgsCoordinateReferenceSystem(crs_authid))
        m.setLayers(list(reversed(layers_bottom_to_top)))   # QGIS 约定：列表首项在最上层
        m.setKeepLayerSet(True)
        lay.addLayoutItem(m)
        d = self.dim
        _place(m, d["map_x"], d["map_y"], d["map_w"], d["map_h"])
        m.setExtent(extent)     # 纵横比已与范围一致，不会被改高
        m.setFrameEnabled(True)
        m.setFrameStrokeColor(QColor("#000000"))
        m.setFrameStrokeWidth(QgsLayoutMeasurement(0.25, _mm()))
        m.setBackgroundColor(QColor("#FFFFFF"))
        self.map_item = m
        self.extent = extent
        self.crs_authid = crs_authid
        return m

    def close(self):
        """把临时布局从工程里移除（插件里用完即弃，避免污染用户工程）。"""
        try:
            if self.layout is not None:
                self.project.layoutManager().removeLayout(self.layout)
        except Exception:
            pass
        self.layout = None

    # -- 经纬网
    def add_graticule(self, interval_deg: float | None = None, show_lines: bool = False,
                      size_pt: float = 6.0, decimals: int = 2, style: str = "zebra",
                      fmt: str = "decimal", ticks: str = "interior", all_sides: bool = False):
        """style: 'zebra' 黑白斑马边框 | 'ticks' 仅短刻度。
        fmt: 'decimal' 110.40°E | 'dm' 113°10'E | 'dms' 度分秒。
        all_sides=True：上下标经度、左右标纬度（左右文字竖排）；False：仅左纬度、下经度。
        ticks: 'interior' 刻度朝图内 | 'exterior' 朝图外（仅 style='ticks'）。"""
        m = self.map_item
        if interval_deg is None:
            bb = ctransform(self.crs_authid, "EPSG:4326", self.project).transformBoundingBox(self.extent)
            span = min(bb.width(), bb.height())
            interval_deg = (nice_minute_interval(span, 2.5) if fmt in ("dm", "dms")
                            else nice_degree_interval(span, 4))
        grid = QgsLayoutItemMapGrid("Graticule", m)
        grid.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        grid.setIntervalX(interval_deg)
        grid.setIntervalY(interval_deg)
        gs = lambda n: enum(QgsLayoutItemMapGrid, "GridStyle", n)
        fs = lambda n: enum(QgsLayoutItemMapGrid, "FrameStyle", n)
        bs = lambda n: enum(QgsLayoutItemMapGrid, "BorderSide", n)
        grid.setStyle(gs("Solid") if show_lines else gs("FrameAnnotationsOnly"))
        if show_lines:
            grid.setLineSymbol(QgsLineSymbol.createSimple({
                "line_color": "140,140,140,255", "line_width": "0.1", "line_style": "dash"}))
        if style == "ticks":
            grid.setFrameStyle(fs("InteriorTicks" if ticks == "interior" else "ExteriorTicks"))
            grid.setFrameWidth(1.2)         # 刻度长度
            grid.setFramePenSize(0.2)
        else:
            grid.setFrameStyle(fs("Zebra"))
            grid.setFrameWidth(1.0)
            grid.setFramePenSize(0.2)
            grid.setFrameFillColor1(QColor("#000000"))
            grid.setFrameFillColor2(QColor("#FFFFFF"))
        grid.setFramePenColor(QColor("#000000"))
        grid.setAnnotationEnabled(True)
        # QGIS 3.44 起 DegreeMinuteNoPadding / DegreeMinuteSecondNoPadding 已改名（或并入基础枚举），
        # 这里按「无补零」语义优先、逐名回退，保证跨版本可用。
        af_candidates = {
            "decimal": ["DecimalWithSuffix"],
            "dm": ["DegreeMinuteNoPadding", "DegreeMinute", "DegreeMinutePadded"],
            "dms": ["DegreeMinuteSecondNoPadding", "DegreeMinuteSecond", "DegreeMinuteSecondPadded"],
        }[fmt]
        af_enum = None
        for cand in af_candidates:
            try:
                af_enum = enum(QgsLayoutItemMapGrid, "AnnotationFormat", cand)
                break
            except AttributeError:
                continue
        if af_enum is None:
            raise RuntimeError("没有可用的经纬网标注格式：{}".format(fmt))
        grid.setAnnotationFormat(af_enum)
        grid.setAnnotationPrecision(decimals if fmt == "decimal" else 0)
        sample = "0123456789°'\"EN"
        grid.setAnnotationTextFormat(self.tf(size_pt, sample=sample))
        grid.setAnnotationFrameDistance(1.0)
        pos_out = enum(QgsLayoutItemMapGrid, "AnnotationPosition", "OutsideMapFrame")
        dm = lambda n: enum(QgsLayoutItemMapGrid, "DisplayMode", n)
        ad = lambda n: enum(QgsLayoutItemMapGrid, "AnnotationDirection", n)
        sides = (
            ("Left", "LatitudeOnly", "Vertical"),
            ("Bottom", "LongitudeOnly", "Horizontal"),
            ("Top", "LongitudeOnly" if all_sides else "HideAll", "Horizontal"),
            ("Right", "LatitudeOnly" if all_sides else "HideAll", "VerticalDescending"),
        )
        for side, mode, direction in sides:
            grid.setAnnotationPosition(pos_out, bs(side))
            grid.setAnnotationDisplay(dm(mode), bs(side))
            grid.setAnnotationDirection(ad(direction), bs(side))
        m.grids().addGrid(grid)
        return grid

    # -- 图内小部件
    def add_north_arrow(self, corner: str = "tr", size_mm: float = 9.0):
        path = write_north_arrow_svg(os.path.join(tempfile.gettempdir(), "_sci_north_arrow.svg"))
        pic = QgsLayoutItemPicture(self.layout)
        pic.setPicturePath(path)
        self.layout.addLayoutItem(pic)
        w, h = size_mm * 2 / 3, size_mm
        x, y = self._corner(corner, w, h)
        _place(pic, x, y, w, h)
        try:
            pic.setLinkedMap(self.map_item)     # 随地图旋转
        except Exception:
            pass
        return pic

    def add_scalebar(self, corner: str = "bl", segments: int = 2, size_pt: float = 6.5,
                     fraction: float = 0.28, style: str = "Single Box",
                     length_m: float | None = None, background: bool = True,
                     target: str = "main"):
        """style: 'Single Box'（黑白相间）| 'Line Ticks Up'（线型刻度，中文期刊常见）。
        length_m：总长固定值（如插图的 100 km）；target='inset' 时挂到插图上。"""
        if target == "inset":
            m = self.inset_item
            ext_w = self.inset_extent.width()
            box = self.inset_box
        else:
            m, ext_w, box = self.map_item, self.extent.width(), None
        seg_m, n, unit, per_seg = nice_scalebar(ext_w, fraction, segments, length_m)
        sb = QgsLayoutItemScaleBar(self.layout)
        sb.setLinkedMap(m)                      # 原生控件，读数由 QGIS 按地图比例计算
        sb.setStyle(style)
        try:
            sb.setUnits(enum(Qgis, "DistanceUnit", "Kilometers" if unit == "km" else "Meters"))
        except Exception:
            sb.setUnits(QgsUnitTypes.DistanceKilometers if unit == "km" else QgsUnitTypes.DistanceMeters)
        sb.setUnitLabel(unit)
        sb.setNumberOfSegmentsLeft(0)
        sb.setNumberOfSegments(n)
        sb.setUnitsPerSegment(per_seg)
        sb.setHeight(1.4)
        sb.setLabelBarSpace(1.0)
        sb.setBoxContentSpace(1.2)
        sb.setLineWidth(0.2)
        sb.setLineColor(QColor("#000000"))
        sb.setFillColor(QColor("#000000"))
        sb.setFillColor2(QColor("#FFFFFF"))
        sb.setTextFormat(self.tf(size_pt, sample="0123456789 km"))
        sb.setBackgroundEnabled(background)
        if background:
            sb.setBackgroundColor(QColor(255, 255, 255, 215))
        self.layout.addLayoutItem(sb)
        sb.update()
        w, h = _item_size(sb, 40.0, 10.0)
        x, y = self._corner(corner, w, h, box=box)
        sb.attemptMove(QgsLayoutPoint(x, y, _mm()))
        return sb

    def add_legend(self, entries, position: str = "right", title: str | None = None,
                   size_pt: float = 7.0, frame: bool | None = None,
                   background: bool | None = None, symbol_w_mm: float = 7.0,
                   symbol_h_mm: float = 3.2):
        """entries: [(QgsMapLayer, 显示文字), ...]；position: 'right' | 'inside_tl/tr/bl/br'。
        图内图例默认带框+半透明底；frame=False, background=False 得到参考图那种“纯文字+符号”。"""
        leg = QgsLayoutItemLegend(self.layout)
        leg.setLinkedMap(self.map_item)
        leg.setAutoUpdateModel(False)
        root = leg.model().rootGroup()
        root.removeAllChildren()
        for layer, label in entries:
            # 直接改临时图层名：渲染时 QGIS 图例按图层名显示标题，
            # legend/title-label 在部分版本渲染阶段不可靠，改名可保证图例显示短名、不超宽。
            try:
                if layer.name() != label:
                    layer.setName(label)
            except Exception:
                pass
            node = root.addLayer(layer)
            node.setCustomProperty("legend/title-label", label)
        sample = "".join(l for _, l in entries) + (title or "")
        for st in ("Title", "Group", "Subgroup", "SymbolLabel"):
            leg.rstyle(enum(QgsLegendStyle, "Style", st)).setTextFormat(
                self.tf(size_pt, bold=(st == "Title"), sample=sample))
        leg.setTitle(title or "")
        leg.setSymbolWidth(symbol_w_mm)
        leg.setSymbolHeight(symbol_h_mm)
        leg.setBoxSpace(1.0)
        leg.setResizeToContents(True)
        inside = position != "right"
        frame = inside if frame is None else frame
        background = inside if background is None else background
        leg.setFrameEnabled(frame and inside)
        if frame and inside:
            leg.setFrameStrokeWidth(QgsLayoutMeasurement(0.2, _mm()))
        leg.setBackgroundEnabled(background)
        if background:
            leg.setBackgroundColor(QColor(255, 255, 255, 230))
        self.layout.addLayoutItem(leg)
        leg.updateLegend()
        try:
            leg.adjustBoxSize()
        except Exception:
            pass
        if position == "right":
            leg.attemptMove(QgsLayoutPoint(self.dim["col_x"], self.dim["map_y"], _mm()))
        else:
            # 图例真实宽度要到渲染时才可知，摆放时按内容估算（留安全余量），
            # 避免内嵌图例超出地图框 / 页面。
            est_w = self._estimate_legend_width(entries, size_pt, symbol_w_mm)
            _h = _item_size(leg, 30.0, 20.0)[1]
            x, y = self._corner(position.replace("inside_", ""), est_w, _h)
            leg.attemptMove(QgsLayoutPoint(x, y, _mm()))
        return leg

    def _estimate_legend_width(self, entries, size_pt, symbol_w_mm):
        """按内容估算图例宽度(mm)，用于内嵌图例定位（真实宽度要到渲染后才可知）。"""
        try:
            from qgis.PyQt.QtGui import QFontMetricsF
            sample = "".join(l for _, l in entries)
            fam = self.cjk_font if (self.cjk_font and contains_cjk(sample)) else self.font
            f = QFont(fam)
            f.setPointSizeF(size_pt)
            fm = QFontMetricsF(f)
            text_pt = max((fm.horizontalAdvance(lbl) for _, lbl in entries), default=0.0)
            text_mm = text_pt * 25.4 / 72.0
        except Exception:
            text_mm = 0.0
            for _, lbl in entries:
                cjk = sum(1 for ch in lbl if contains_cjk(ch))
                text_mm = max(text_mm, (cjk + (len(lbl) - cjk) * 0.55) * size_pt * 0.3528)
        # 符号列 + 列间距 + 文字 + 内边距/边框 + 安全余量
        return symbol_w_mm + 2.5 + text_mm + 3.0 + 2.0

    def add_inset(self, layers_bottom_to_top, extent: "QgsRectangle", width_mm: float | None = None,
                  corner: str | None = None, overview: bool = True,
                  highlight_stroke: str = "#000000", crs=None):
        """区位插图。corner=None 放右栏底部；'tl'/'tr'/'bl'/'br' 放图内角落。
        overview=True 在插图里用方框标出主图范围；False 时靠图层样式（如把研究区涂黑）表达位置。
        插图用独立图层（图层标注会在所有显示该图层的地图里出现，所以别和主图共用带标注的图层）。"""
        # 区位插图比例：按地理形状的纵横比自动适配到可用区域（保持纵横比、不拉伸）。
        # 高长形状以高度为限（宽度随之收窄），宽扁形状以宽度为限（高度随之变矮）。
        max_w = width_mm or (self.dim["col_w"] if self.dim["col_w"] > 0 else 30.0)
        max_h = min(54.0, self.dim["map_h"] * 0.5)
        if max_w <= 0:
            max_w = 30.0
        if max_h <= 0:
            max_h = 30.0
        aspect = extent.height() / extent.width() if extent.width() > 0 else 1.0
        if aspect >= max_h / max_w:      # 高长形状：高度受限
            h = max_h
            w = h / aspect
        else:                            # 宽扁形状：宽度受限
            w = max_w
            h = w * aspect
        inset = QgsLayoutItemMap(self.layout)
        if crs is None:
            inset_crs = QgsCoordinateReferenceSystem(self.crs_authid)
        elif isinstance(crs, str):
            inset_crs = QgsCoordinateReferenceSystem(crs)
        else:
            inset_crs = crs
        inset.setCrs(inset_crs)
        inset.setLayers(list(reversed(layers_bottom_to_top)))
        inset.setKeepLayerSet(True)
        self.layout.addLayoutItem(inset)
        _place(inset, 0, 0, w, h)
        inset.setExtent(extent)
        inset.setFrameEnabled(True)
        inset.setFrameStrokeColor(QColor("#000000"))
        inset.setFrameStrokeWidth(QgsLayoutMeasurement(0.3, _mm()))
        inset.setBackgroundColor(QColor("#FFFFFF"))
        if overview:
            ov = QgsLayoutItemMapOverview("Overview", inset)
            ov.setLinkedMap(self.map_item)
            ov.setFrameSymbol(QgsFillSymbol.createSimple({
                "style": "no", "color": "0,0,0,0",
                "outline_color": hex_to_rgba(highlight_stroke), "outline_width": "0.4"}))
            inset.overviews().addOverview(ov)
        if corner is None:
            # 右栏内水平居中，底部对齐
            x = self.dim["col_x"] + max((self.dim["col_w"] - w) / 2.0, 0.0)
            y = self.dim["map_y"] + self.dim["map_h"] - h
        else:
            x, y = self._corner(corner, w, h)
        inset.attemptMove(QgsLayoutPoint(x, y, _mm()))
        self.inset_item, self.inset_extent, self.inset_box = inset, extent, (x, y, x + w, y + h)
        return inset

    def add_panel_label(self, text: str = "(a)", size_pt: float = 9.0):
        lab = QgsLayoutItemLabel(self.layout)
        lab.setText(text)
        lab.setTextFormat(self.tf(size_pt, bold=True))
        self.layout.addLayoutItem(lab)
        _place(lab, 0.8, 0.4, 10.0, 4.6)
        return lab

    def add_note(self, text: str, x: float, y: float, w: float, h: float, size_pt: float = 6.5):
        lab = QgsLayoutItemLabel(self.layout)
        lab.setText(text)
        lab.setTextFormat(self.tf(size_pt, sample=text))
        lab.setHAlign(Qt.AlignmentFlag.AlignLeft)
        self.layout.addLayoutItem(lab)
        _place(lab, x, y, w, h)
        return lab

    def _corner(self, corner: str, w: float, h: float, pad: float = 1.8, box=None):
        if box is None:
            d = self.dim
            box = (d["map_x"], d["map_y"], d["map_x"] + d["map_w"], d["map_y"] + d["map_h"])
        x0, y0, x1, y1 = box
        return {
            "tl": (x0 + pad, y0 + pad), "tr": (x1 - w - pad, y0 + pad),
            "bl": (x0 + pad, y1 - h - pad), "br": (x1 - w - pad, y1 - h - pad),
        }[corner]

    # -- 版面自检：图内控件互相重叠 / 超出页面
    def check_layout(self) -> list:
        """返回告警列表（空 = 通过）。导出前自动调用；在 QGIS MCP 里也可单独调用并把结果贴回来。"""
        if self.layout is None:
            return ["尚未创建主地图"]
        d = self.dim
        widgets = []
        for it in self.layout.items():
            if not isinstance(it, QgsLayoutItem):
                continue
            if isinstance(it, (QgsLayoutItemLegend, QgsLayoutItemScaleBar, QgsLayoutItemPicture)) \
                    or it is getattr(self, "inset_item", None):
                widgets.append((type(it).__name__, it.sceneBoundingRect(), it))
        warns = []
        for i, (na, ra, ia) in enumerate(widgets):
            if ra.left() < -0.01 or ra.top() < -0.01 or ra.right() > d["page_w"] + 0.01 \
                    or ra.bottom() > d["page_h"] + 0.01:
                warns.append("%s 超出页面（%.1f,%.1f → %.1f,%.1f mm）" % (
                    na, ra.left(), ra.top(), ra.right(), ra.bottom()))
            for nb, rb, ib in widgets[i + 1:]:
                if ra.intersects(rb):
                    # 比例尺放在其所属地图图框内是正常排版，不算重叠
                    if isinstance(ia, QgsLayoutItemScaleBar) and self._scalebar_linked(ia, ib):
                        continue
                    if isinstance(ib, QgsLayoutItemScaleBar) and self._scalebar_linked(ib, ia):
                        continue
                    warns.append("%s 与 %s 重叠" % (na, nb))
        return warns

    @staticmethod
    def _scalebar_linked(scalebar, item):
        """比例尺是否挂在 item（地图）上：是则两者重叠属正常排版。"""
        try:
            return scalebar.linkedMap() is item
        except Exception:
            return False

    # -- 导出
    def export(self, out_dir: str, basename: str, formats=("pdf", "png"), dpi: int = 600):
        os.makedirs(out_dir, exist_ok=True)
        self.layout.refresh()
        for w in self.check_layout():
            log("版面警告：" + w)
        ex = QgsLayoutExporter(self.layout)
        ok = enum(QgsLayoutExporter, "ExportResult", "Success")
        written = []
        for fmt in formats:
            path = os.path.join(out_dir, "%s.%s" % (basename, fmt))
            if fmt == "pdf":
                s = QgsLayoutExporter.PdfExportSettings()
                s.dpi = dpi
                s.rasterizeWholeImage = False
                s.forceVectorOutput = True
                s.appendGeoreference = False
                s.exportMetadata = False
                try:
                    s.textRenderFormat = enum(Qgis, "TextRenderFormat", "AlwaysText")  # 保留可编辑文字
                except Exception:
                    pass
                res = ex.exportToPdf(path, s)
            elif fmt == "svg":
                s = QgsLayoutExporter.SvgExportSettings()
                s.dpi = dpi
                s.forceVectorOutput = True
                try:
                    s.textRenderFormat = enum(Qgis, "TextRenderFormat", "AlwaysText")
                except Exception:
                    pass
                res = ex.exportToSvg(path, s)
            elif fmt in ("png", "tif", "tiff", "jpg"):
                s = QgsLayoutExporter.ImageExportSettings()
                s.dpi = dpi             # 以 dpi 定像素，不手写 imageSize
                s.generateWorldFile = False
                res = ex.exportToImage(path, s)
            else:
                raise ValueError("不支持的格式: " + fmt)
            if res != ok:
                raise RuntimeError("导出 %s 失败 (result=%s)" % (path, res))
            written.append(path)
            log("已导出 " + path)
        d = self.dim
        log("成品尺寸 %.1f × %.1f mm；%d dpi → 位图约 %d × %d px" % (
            d["page_w"], d["page_h"], dpi,
            round(d["page_w"] / 25.4 * dpi), round(d["page_h"] / 25.4 * dpi)))
        return written


def expand_rect(rect: "QgsRectangle", pad_ratio: float) -> "QgsRectangle":
    r = QgsRectangle(rect)
    r.grow(max(rect.width(), rect.height()) * pad_ratio)
    return r
