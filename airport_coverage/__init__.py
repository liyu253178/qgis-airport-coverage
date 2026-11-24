# -*- coding: utf-8 -*-
"""机场覆盖区规划 —— QGIS 插件入口。"""


def classFactory(iface):  # noqa: N802  QGIS 要求的固定函数名
    from .plugin import AirportCoveragePlugin

    return AirportCoveragePlugin(iface)
