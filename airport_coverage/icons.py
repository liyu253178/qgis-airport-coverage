# -*- coding: utf-8 -*-
"""图标加载：每个功能一个独立 SVG（icons/ 目录）。"""

import os

from qgis.PyQt.QtGui import QIcon

_DIR = os.path.join(os.path.dirname(__file__), "icons")


def icon(name):
    return QIcon(os.path.join(_DIR, name + ".svg"))
