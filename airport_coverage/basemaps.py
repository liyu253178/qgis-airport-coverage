# -*- coding: utf-8 -*-
"""内置在线图源（三种，均为 WGS84 在线影像，无需做 GCJ-02 偏移校正）。

瓦片顺序各家不同，严格按下列模板使用，不要互换 {x}/{y}：
    Esri World Imagery  -> {z}/{y}/{x}
    GEOVIS Earth 影像    -> {z}/{x}/{y}
    四维地球 2024 影像   -> {z}/{y}/{x}

三个源均已实测：QGIS 中 isValid() 为 True，瓦片 HTTP 200 返回真实图像。
"""

from qgis.core import QgsProject, QgsRasterLayer

BASEMAPS = [
    {
        "id": "esri",
        "name": "Esri World Imagery",
        "url": (
            "https://server.arcgisonline.com/arcgis/rest/services/"
            "world_imagery/mapserver/tile/{z}/{y}/{x}"
        ),
        "maxzoom": 19,
        "attribution": "Esri, Maxar, Earthstar Geographics",
    },
    {
        "id": "geovis",
        "name": "GEOVIS Earth 影像",
        "url": (
            "https://tiles1.geovisearth.com/base/v1/img/{z}/{x}/{y}"
            "?format=webp&tmsids=w"
            "&token=3c7931ed450acfc9c8170efeead4759b146cfb78f0ee6908dd7e287270ee1b6f"
        ),
        "maxzoom": 18,
        "attribution": "GEOVIS Earth",
    },
    {
        "id": "siwei",
        "name": "四维地球 2024 影像",
        "url": (
            "https://aoweiservice.siweiearth.com/map/ng/2024tria/satellite/{z}/{y}/{x}"
            "?ak=babceffbedece"
        ),
        "maxzoom": 18,
        "attribution": "四维地球 Siwei Earth",
    },
]

DEFAULT_BASEMAP = "esri"


def get_basemap(key):
    for b in BASEMAPS:
        if b["id"] == key:
            return b
    return None


def build_uri(basemap):
    """构造 QGIS 的 XYZ 瓦片 URI。

    注意：URL 里的 & 必须转义成 %26，否则会被当成 URI 参数分隔符。
    """
    url = basemap["url"].replace("&", "%26")
    return "type=xyz&url={}&zmin=0&zmax={}".format(url, basemap["maxzoom"])


def create_layer(basemap):
    """创建（但不加载）图源图层。"""
    return QgsRasterLayer(build_uri(basemap), basemap["name"], "wms")


def add_basemap(basemap, keep_project_crs=True):
    """加载图源到工程最底层。

    :param keep_project_crs: 加载 XYZ（EPSG:3857）后把工程 CRS 恢复原值。
        —— 添加首个图层时 QGIS 可能自动把工程 CRS 改成 3857，
           而 3857 下做距离相关操作会严重失真（实测 5 km 圈缩水 23.5%~39.3%）。
    :return: (layer, error_message)；成功时 error_message 为 None
    """
    crs_before = QgsProject.instance().crs()

    layer = create_layer(basemap)
    if not layer.isValid():
        return None, "图源加载失败：{}".format(layer.error().summary() or basemap["name"])

    project = QgsProject.instance()
    project.addMapLayer(layer, False)
    root = project.layerTreeRoot()
    root.insertLayer(0, layer)

    if keep_project_crs and crs_before.isValid() and project.crs() != crs_before:
        project.setCrs(crs_before)

    return layer, None
