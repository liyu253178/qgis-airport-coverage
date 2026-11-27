# -*- coding: utf-8 -*-
"""SCI / 期刊风格制图核心库（移植自 qgis-sci-map）。

与上游的差异（均已在 core.py 里标注）：
  * log() 同时写入 QGIS 日志面板
  * SciFigure.add_main_map：同名布局先移除；新增 close()
  * SciFigure.add_inset：新增 crs 参数（全国级插图用单独的投影）
"""
