# -*- coding: utf-8 -*-
"""测地线几何工具：5 km 覆盖圆生成、长度 / 面积量算、单位格式化。

核心约定（已在本机 QGIS 3.44 实测定型，见 docs/QGIS插件-可行性验证报告.md）：

    圆的几何一律在「以圆心为中心的方位等距投影（aeqd）」中生成，
    再转到目标（工程 / 图层）坐标系存储。

这样图层 CRS 与工程一致（存储、显示、编辑都一致），而半径是严格的地面距离，
不会因工程是 EPSG:3857（实测会缩水 23.5%~39.3%）或地理坐标系而失真。

反面教材（均为实测）：
    * 在 EPSG:3857 下直接 buffer(5000)  -> 北京误差 1175 m，漠河 1965 m
    * 在 EPSG:4326 下直接 buffer(0.0449 度) -> 误差 1158.90 m
"""

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)

WGS84_AUTHID = "EPSG:4326"
WGS84_ELLIPSOID = "EPSG:7030"

DEFAULT_RADIUS_M = 5000.0
DEFAULT_SEGMENTS = 72

_AEQD_PROJ = "+proj=aeqd +lat_0={lat:.10f} +lon_0={lon:.10f} +datum=WGS84 +units=m +no_defs"


def wgs84():
    """WGS84 地理坐标系。"""
    return QgsCoordinateReferenceSystem(WGS84_AUTHID)


def project_crs():
    """工程坐标系；工程无空间参考时按 WGS84 处理。"""
    crs = QgsProject.instance().crs()
    return crs if crs.isValid() else wgs84()


def ensure_project_crs():
    """工程 CRS 无效时显式设为 WGS84，返回生效的 CRS。"""
    crs = QgsProject.instance().crs()
    if not crs.isValid():
        QgsProject.instance().setCrs(wgs84())
        return wgs84()
    return crs


def _ctx():
    return QgsProject.instance().transformContext()


def transformer(src_crs, dst_crs):
    return QgsCoordinateTransform(src_crs, dst_crs, _ctx())


def geodesic_circle(lon, lat, radius_m=DEFAULT_RADIUS_M, segments=DEFAULT_SEGMENTS, target_crs=None):
    """生成地面半径精确的圆，返回 target_crs 下的多边形几何。

    :param lon,lat: 圆心（WGS84 经纬度）
    :param radius_m: 半径（米），默认 5000（大疆机场实测覆盖半径）
    :param target_crs: 目标坐标系，默认取工程 CRS
    """
    tgt = target_crs or project_crs()
    aeqd = QgsCoordinateReferenceSystem.fromProj(_AEQD_PROJ.format(lat=float(lat), lon=float(lon)))
    if not aeqd.isValid():
        raise RuntimeError("无法构造方位等距投影（aeqd）")

    to_aeqd = transformer(wgs84(), aeqd)
    to_target = transformer(aeqd, tgt)

    geom = QgsGeometry.fromPointXY(to_aeqd.transform(QgsPointXY(float(lon), float(lat))))
    geom = geom.buffer(float(radius_m), int(segments))
    geom.transform(to_target)
    return geom


def to_wgs84(point, src_crs=None):
    """把 src_crs 下的点转成 WGS84 经纬度，返回 (lon, lat)。"""
    src = src_crs or project_crs()
    if src.authid() == WGS84_AUTHID:
        return point.x(), point.y()
    p = transformer(src, wgs84()).transform(point)
    return p.x(), p.y()


def from_wgs84(lon, lat, dst_crs=None):
    """把 WGS84 经纬度转成 dst_crs 下的点。"""
    dst = dst_crs or project_crs()
    if dst.authid() == WGS84_AUTHID:
        return QgsPointXY(lon, lat)
    return transformer(wgs84(), dst).transform(QgsPointXY(lon, lat))


def measurer(crs=None, ellipsoid=None):
    """返回一个已设好椭球的 QgsDistanceArea。

    坑：不设椭球（或椭球为 NONE）时 QGIS 会把经纬度当笛卡尔坐标算 ——
    实测北京→上海返回 10.1，面积量出来是 0，毫无意义。

    而 QGIS 新建工程的默认椭球恰恰就是 "NONE"，所以这里必须显式兜底：
    工程设了具体椭球（如 CGCS2000 的 GRS80）时跟随工程，否则一律用 WGS84。
    """
    ell = ellipsoid or QgsProject.instance().ellipsoid() or ""
    if not ell or ell.upper() == "NONE":
        ell = WGS84_ELLIPSOID

    da = QgsDistanceArea()
    da.setSourceCrs(crs or project_crs(), _ctx())
    da.setEllipsoid(ell)
    return da


def measure_length(points, crs=None):
    """折线长度（米）。points 为 crs 下的点序列。"""
    if len(points) < 2:
        return 0.0
    return measurer(crs).measureLine(list(points))


def measure_area(geom, crs=None):
    """多边形面积（平方米）。"""
    return abs(measurer(crs).measureArea(geom))


def measure_perimeter(geom, crs=None):
    """多边形周长（米）。"""
    return measurer(crs).measurePerimeter(geom)


def fmt_length(metres):
    if metres is None:
        return "-"
    m = abs(float(metres))
    if m < 1000:
        return "{:.2f} m".format(m)
    return "{:.3f} km".format(m / 1000.0)


def fmt_area(sqm):
    if sqm is None:
        return "-"
    a = abs(float(sqm))
    if a < 1e6:
        return "{:.2f} m²".format(a)
    return "{:.4f} km²".format(a / 1e6)
