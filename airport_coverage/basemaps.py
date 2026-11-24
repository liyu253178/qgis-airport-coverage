# -*- coding: utf-8 -*-
"""内置在线图源（参考《QGIS图源问题.docx》，不含高德）。

每个图源的字段：
    id / group / name  标识、分组、显示名（name 同时作为图层名，必须唯一）
    kind     base=底图（放最底层，同一时间只留一个） / overlay=透明叠加层（注记、路网、境界，放在底图之上）
    crs      坐标系，目前内置图源均为 WGS84，无需偏移校正。
    url      XYZ 模板。{z}/{x}/{y} 顺序各家不同，严格按各自模板，{-y} 表示 TMS 翻转。

天地图的 t0~t7 是 8 个等价子域名，但并非每个都能用（白屏就换一个：改下面的 TIANDITU_HOST，
或直接用 QGIS 的 TianDiTu 插件）。
"""

from qgis.core import QgsLayerTree, QgsProject, QgsRasterLayer, QgsSettings

# 天地图 key 不写在源码里：默认为空，由用户在面板里填写，保存在 QGIS 用户设置中（跨工程、跨重启有效）
KEY_SETTING = "airport_coverage/tianditu_key"
KEY_PLACEHOLDER = "@TK@"
TIANDITU_HOST = "t0"  # 白屏时改成 t1 ~ t7


def _td(layer, label, kind, zmax):
    url = (
        "https://" + TIANDITU_HOST + ".tianditu.gov.cn/" + layer + "_w/wmts"
        "?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=" + layer +
        "&STYLE=default&TILEMATRIXSET=w&FORMAT=tiles"
        "&TileCol={x}&TileRow={y}&TileMatrix={z}&tk=" + KEY_PLACEHOLDER
    )
    return {
        "id": "td_" + layer, "group": "天地图", "name": "天地图 " + label,
        "kind": kind, "crs": "WGS84", "url": url, "maxzoom": zmax,
        "attribution": "天地图", "needs_key": True,
        "note": "需要天地图 key（浏览器端），在下方填写并保存",
    }


BASEMAPS = [
    # ---- 国际 / 影像（WGS84） ----
    {
        "id": "esri", "group": "影像", "name": "Esri World Imagery", "kind": "base", "crs": "WGS84",
        "url": "https://server.arcgisonline.com/arcgis/rest/services/world_imagery/mapserver/tile/{z}/{y}/{x}",
        "maxzoom": 19, "attribution": "Esri, Maxar, Earthstar Geographics",
    },
    {
        "id": "google", "group": "影像", "name": "谷歌卫星（第三方代理）", "kind": "base", "crs": "WGS84",
        "url": "https://0pn.cn/maps/vt?lyrs=s&x={x}&y={y}&z={z}",
        "maxzoom": 19, "attribution": "Google",
        "note": "经第三方代理 0pn.cn 转发，稳定性无保证",
    },
    {
        "id": "geovis", "group": "影像", "name": "星图地球 GEOVIS 影像", "kind": "base", "crs": "WGS84",
        "url": (
            "https://tiles1.geovisearth.com/base/v1/img/{z}/{x}/{y}?format=webp&tmsids=w"
            "&token=3c7931ed450acfc9c8170efeead4759b146cfb78f0ee6908dd7e287270ee1b6f"
        ),
        "maxzoom": 18, "attribution": "GEOVIS Earth",
    },
    {
        "id": "siwei", "group": "影像", "name": "四维地球 2024 影像", "kind": "base", "crs": "WGS84",
        "url": "https://aoweiservice.siweiearth.com/map/ng/2024tria/satellite/{z}/{y}/{x}?ak=babceffbedece",
        "maxzoom": 18, "attribution": "四维地球 Siwei Earth",
    },
    {
        "id": "jl1", "group": "影像", "name": "吉林一号 影像", "kind": "base", "crs": "WGS84",
        "url": (
            "https://api.jl1mall.com/getMap/{z}/{x}/{-y}"
            "?mk=3ddec00f5f435270285ffc7ad1a60ce5&tk=50d6270999cf817cebc636fb8a259ceb"
        ),
        "maxzoom": 18, "attribution": "吉林一号 / 长光卫星",
    },
    # ---- 天地图（需 tk；底图 + 透明注记叠加层） ----
    _td("img", "影像底图", "base", 18),
    _td("vec", "矢量底图", "base", 18),
    _td("ter", "地形晕染", "base", 14),
    _td("cia", "影像注记（叠加）", "overlay", 18),
    _td("cva", "矢量注记（叠加）", "overlay", 18),
    _td("cta", "地形注记（叠加）", "overlay", 14),
    _td("ibo", "全球境界（叠加）", "overlay", 10),
]

DEFAULT_BASEMAP = "esri"


def get_tianditu_key():
    return str(QgsSettings().value(KEY_SETTING, "") or "").strip()


def set_tianditu_key(key):
    QgsSettings().setValue(KEY_SETTING, (key or "").strip())


def apply_key_to_loaded_layers():
    """key 改了之后，把工程里已加载的天地图图层的地址同步更新，不用删了重加。"""
    n = 0
    for b in BASEMAPS:
        if not b.get("needs_key"):
            continue
        for lyr in QgsProject.instance().mapLayersByName(b["name"]):
            lyr.setDataSource(build_uri(b), b["name"], "wms")
            lyr.triggerRepaint()
            n += 1
    return n


def get_basemap(key):
    for b in BASEMAPS:
        if b["id"] == key:
            return b
    return None


def build_uri(basemap):
    """构造 QGIS 的 XYZ 瓦片 URI。

    注意：URL 里的 & 必须转义成 %26，否则会被当成 URI 参数分隔符。
    """
    url = basemap["url"].replace(KEY_PLACEHOLDER, get_tianditu_key()).replace("&", "%26")
    return "type=xyz&url={}&zmin=0&zmax={}".format(url, basemap["maxzoom"])


def create_layer(basemap):
    """创建（但不加载）图源图层。"""
    return QgsRasterLayer(build_uri(basemap), basemap["name"], "wms")


def _names(kind):
    return {b["name"] for b in BASEMAPS if b["kind"] == kind}


def _place(root, layer, kind):
    """底图 -> 最底层；叠加层 -> 紧贴在最上面那张底图之上（仍在矢量图层之下）。

    注意 QGIS 图层树里 index 0 是最顶层。原实现 insertLayer(0, ...) 会把不透明影像盖在所有图层之上。
    """
    if kind == "overlay":
        base_names = _names("base")
        for i, child in enumerate(root.children()):
            if QgsLayerTree.isLayer(child) and child.name() in base_names:
                root.insertLayer(i, layer)
                return
    root.addLayer(layer)  # 追加到末尾 = 最底层


def add_basemap(basemap, keep_project_crs=True):
    """加载图源。

    * 底图：同一时间只保留一个（会移除本插件加载过的其它底图，否则新底图在最下面看不出变化）
    * 叠加层：不影响底图
    * 同名图层已存在则只确保可见，不重复添加

    :return: (layer, error_message)；成功时 error_message 为 None
    """
    if basemap.get("needs_key") and not get_tianditu_key():
        return None, "天地图需要 key：请先在面板「天地图 Key」里填写并保存（没有的话到 console.tianditu.gov.cn 申请，应用类型选「浏览器端」）"

    project = QgsProject.instance()
    root = project.layerTreeRoot()

    existing = project.mapLayersByName(basemap["name"])
    if existing:
        node = root.findLayer(existing[0].id())
        if node is not None:
            node.setItemVisibilityChecked(True)
        return existing[0], None

    crs_before = project.crs()

    layer = create_layer(basemap)
    if not layer.isValid():
        return None, "图源加载失败：{}".format(layer.error().summary() or basemap["name"])

    if basemap["kind"] == "base":
        for name in _names("base"):
            for old in project.mapLayersByName(name):
                project.removeMapLayer(old.id())

    project.addMapLayer(layer, False)
    _place(root, layer, basemap["kind"])

    if keep_project_crs and crs_before.isValid() and project.crs() != crs_before:
        project.setCrs(crs_before)

    return layer, None
