# -*- coding: utf-8 -*-
"""内置中国省 / 市 / 县行政区划，按需加载。

数据（data/ 目录，坐标系 CGCS2000 / EPSG:4490，只含面要素）：
    provinces.geojson  34 个省级
    cities.geojson     375 个地级（含直辖市、港澳台、省直辖县级单位）
    counties.geojson   2890 个县级
    regions.json       省 -> 市 的下拉框索引

字段：name 名称；gb 行政区划代码（"156" + 6 位）；prov 所属省；city 所属市（仅县级）。

按需加载靠 gb 前缀过滤（OGR 子集字符串），只读取选中范围，不会把全国县界一次性塞进工程。
前缀规则（GB/T 2260 的层级）：
    xx0000 省级（含直辖市）-> 前 2 位；  xxxx00 地级 -> 前 4 位；  其余（省直辖县等）-> 精确匹配
"""

import json
import os

from qgis.core import (
    QgsCoordinateTransform,
    QgsFillSymbol,
    QgsLayerTree,
    QgsPalLayerSettings,
    QgsProject,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor

from .basemaps import BASEMAPS
from .coverage import LAYER_NAME as COVERAGE_LAYER_NAME

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PREFIX = "行政区划｜"

# key, 显示名, 文件, 描边颜色, 描边宽度(mm), 标注字号
LEVELS = (
    ("province", "省", "provinces.geojson", "#B91C1C", 0.8, 11),
    ("city", "市", "cities.geojson", "#D97706", 0.5, 10),
    ("county", "县（区）", "counties.geojson", "#6B7280", 0.3, 9),
)
_LEVEL = {k: i for i, (k, *_rest) in enumerate(LEVELS)}
_LABEL_IDX = {lv[1]: i for i, lv in enumerate(LEVELS)}

_REGIONS = None


def regions():
    """省 / 市下拉框索引（读一次后缓存）。"""
    global _REGIONS
    if _REGIONS is None:
        with open(os.path.join(DATA_DIR, "regions.json"), encoding="utf-8") as fh:
            _REGIONS = json.load(fh)
    return _REGIONS


def gb_prefix(gb):
    code = gb[3:]
    if code.endswith("0000"):
        return gb[:5]
    if code.endswith("00"):
        return gb[:7]
    return gb


def subset_for(prov_gb=None, city_gb=None):
    """范围 -> OGR 子集字符串；全国返回 None。"""
    gb = city_gb or prov_gb
    if not gb:
        return None
    p = gb_prefix(gb)
    if len(p) == 9:
        return "\"gb\" = '{}'".format(p)
    return "\"gb\" LIKE '{}%'".format(p)


def _scope_names(prov_gb, city_gb):
    names = []
    reg = regions()
    if prov_gb:
        names += [p["name"] for p in reg["provinces"] if p["gb"] == prov_gb][:1]
    if city_gb:
        for lst in reg["cities"].values():
            names += [c["name"] for c in lst if c["gb"] == city_gb][:1]
    return names


def layer_name(level_key, prov_gb=None, city_gb=None):
    label = LEVELS[_LEVEL[level_key]][1]
    return PREFIX + "｜".join([label] + _scope_names(prov_gb, city_gb))


# ---------- 样式 ----------

def _apply_style(layer, level_key, labels):
    _k, _label, _f, color, width, size = LEVELS[_LEVEL[level_key]]
    layer.renderer().setSymbol(
        QgsFillSymbol.createSimple(
            {"style": "no", "color": "0,0,0,0", "color_border": color, "width_border": str(width), "joinstyle": "round"}
        )
    )
    if not labels:
        layer.setLabelsEnabled(False)
        return
    fmt = QgsTextFormat()
    fmt.setSize(size)
    fmt.setColor(QColor(color))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(1.0)
    buf.setColor(QColor(255, 255, 255))
    fmt.setBuffer(buf)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.setFormat(fmt)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


# ---------- 图层树位置 ----------
# 自上而下：机位覆盖圆 > 省 > 市 > 县 > 叠加层(注记) > 底图

def _place(root, layer, level_key):
    me = _LEVEL[level_key]
    basemap_names = {b["name"] for b in BASEMAPS}
    above, below = [], []
    for i, child in enumerate(root.children()):
        if not QgsLayerTree.isLayer(child):
            continue
        name = child.name()
        if name == COVERAGE_LAYER_NAME:
            above.append(i)
        elif name.startswith(PREFIX):
            label = name[len(PREFIX):].split("｜")[0]
            other = _LABEL_IDX.get(label)
            if other is None:
                continue
            (above if other < me else below).append(i)
        elif name in basemap_names:
            below.append(i)

    if above:
        pos = max(above) + 1
        if below:
            pos = min(pos, min(below))
    elif below:
        pos = min(below)
    else:
        pos = 0
    root.insertLayer(pos, layer)


# ---------- 加载 ----------

def add_admin_layer(level_key, prov_gb=None, city_gb=None, labels=True):
    """加载一个行政区划图层。

    :return: (layer, error_message, is_new)
    """
    if level_key not in _LEVEL:
        return None, "未知层级：{}".format(level_key), False

    project = QgsProject.instance()
    root = project.layerTreeRoot()
    name = layer_name(level_key, prov_gb, city_gb)

    existing = project.mapLayersByName(name)
    if existing:
        node = root.findLayer(existing[0].id())
        if node is not None:
            node.setItemVisibilityChecked(True)
        return existing[0], None, False

    path = os.path.join(DATA_DIR, LEVELS[_LEVEL[level_key]][2])
    if not os.path.isfile(path):
        return None, "缺少数据文件：{}".format(path), False

    layer = QgsVectorLayer(path, name, "ogr")
    if not layer.isValid():
        return None, "行政区划数据加载失败：{}".format(path), False

    sub = subset_for(prov_gb, city_gb)
    if sub and not layer.setSubsetString(sub):
        return None, "按范围过滤失败：{}".format(sub), False
    if layer.featureCount() == 0:
        return None, "所选范围内没有要素", False

    _apply_style(layer, level_key, labels)

    crs_before = project.crs()
    project.addMapLayer(layer, False)
    _place(root, layer, level_key)
    # 加首个图层时 QGIS 可能把工程 CRS 改成数据的 4490，这里恢复，保持覆盖圆 / 量算用的坐标系不变
    if crs_before.isValid() and project.crs() != crs_before:
        project.setCrs(crs_before)
    return layer, None, True


def extent_in_canvas_crs(layer, canvas):
    """图层范围换算到画布坐标系，用于缩放。"""
    ext = layer.extent()
    dst = canvas.mapSettings().destinationCrs()
    if layer.crs() != dst:
        ext = QgsCoordinateTransform(layer.crs(), dst, QgsProject.instance().transformContext()).transformBoundingBox(ext)
    return ext
