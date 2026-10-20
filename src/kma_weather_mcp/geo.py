"""좌표 변환: 위경도 → 기상청 격자(nx, ny), 위경도 → 에어코리아 TM 좌표."""

from __future__ import annotations

import math
from functools import lru_cache

from pyproj import Transformer

# 기상청 동네예보 격자 (Lambert Conformal Conic) 상수
_RE = 6371.00877  # 지구 반경(km)
_GRID = 5.0  # 격자 간격(km)
_SLAT1 = 30.0
_SLAT2 = 60.0
_OLON = 126.0
_OLAT = 38.0
_XO = 43  # 기준점 X 격자
_YO = 136  # 기준점 Y 격자


def latlon_to_grid(lat: float, lon: float) -> tuple[int, int]:
    """위경도를 기상청 단기예보 격자(nx, ny)로 변환."""
    degrad = math.pi / 180.0
    re = _RE / _GRID
    slat1, slat2 = _SLAT1 * degrad, _SLAT2 * degrad
    olon, olat = _OLON * degrad, _OLAT * degrad

    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re * sf / math.pow(ro, sn)

    ra = math.tan(math.pi * 0.25 + lat * degrad * 0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lon * degrad - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn

    nx = int(math.floor(ra * math.sin(theta) + _XO + 0.5))
    ny = int(math.floor(ro - ra * math.cos(theta) + _YO + 0.5))
    return nx, ny


@lru_cache(maxsize=1)
def _tm_transformer() -> Transformer:
    # 에어코리아 측정소 좌표계: TM 중부원점(Bessel) — EPSG:2097
    return Transformer.from_crs("EPSG:4326", "EPSG:2097", always_xy=True)


def latlon_to_tm(lat: float, lon: float) -> tuple[float, float]:
    """위경도를 에어코리아 근접측정소 조회용 TM 좌표(tmX, tmY)로 변환."""
    x, y = _tm_transformer().transform(lon, lat)
    return round(x, 2), round(y, 2)


def in_korea(lat: float, lon: float) -> bool:
    """기상청 격자가 커버하는 대략적인 범위(한반도 및 인근 해역)인지."""
    return 32.0 <= lat <= 39.5 and 124.0 <= lon <= 132.0
