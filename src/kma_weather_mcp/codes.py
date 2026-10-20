"""기상청·에어코리아 코드값을 사람이 읽는 값으로 변환."""

from __future__ import annotations

import math
from datetime import datetime

SKY = {"1": "맑음", "3": "구름많음", "4": "흐림"}

PTY = {
    "0": "없음",
    "1": "비",
    "2": "비/눈",
    "3": "눈",
    "4": "소나기",
    "5": "빗방울",
    "6": "빗방울눈날림",
    "7": "눈날림",
}

AIR_GRADE = {"1": "좋음", "2": "보통", "3": "나쁨", "4": "매우나쁨"}

_DIRS16 = [
    "북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동",
    "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서",
]


def wind_direction(deg: float | None) -> str | None:
    """풍향(deg) → 16방위 (바람이 불어오는 방향)."""
    if deg is None:
        return None
    return _DIRS16[int((deg % 360) / 22.5 + 0.5) % 16]


def sky_condition(sky: str | None, pty: str | None) -> str | None:
    """하늘상태 + 강수형태를 하나의 문장형 상태로 합친다."""
    if pty and pty != "0":
        return PTY.get(pty, pty)
    if sky:
        return SKY.get(sky, sky)
    return None


def to_float(value) -> float | None:
    """'1.5', '강수없음', '1mm 미만', '-' 같은 값을 숫자로 변환 (불가하면 None)."""
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "-", "강수없음", "적설없음", "null"):
        return 0.0 if s in ("강수없음", "적설없음") else None
    if "미만" in s:  # '1mm 미만', '0.5cm 미만'
        return 0.0
    s = s.replace("mm", "").replace("cm", "").strip()
    if "~" in s:  # '30.0~50.0mm'
        s = s.split("~")[0]
    if "이상" in s:
        s = s.replace("이상", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def feels_like(temp: float | None, humidity: float | None, wind_ms: float | None,
               when: datetime) -> float | None:
    """기상청 체감온도 공식.

    - 여름(5~9월): 습구온도 기반 여름철 체감온도
    - 그 외: 겨울철 체감온도(바람냉각), 기온 10°C 이하·풍속 1.3m/s 이상일 때만 적용
    """
    if temp is None:
        return None
    if 5 <= when.month <= 9:
        if humidity is None:
            return temp
        rh = humidity
        tw = (
            temp * math.atan(0.151977 * math.sqrt(rh + 8.313659))
            + math.atan(temp + rh)
            - math.atan(rh - 1.67633)
            + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
            - 4.686035
        )
        val = -0.2442 + 0.55399 * tw + 0.45535 * temp - 0.0022 * tw ** 2 + 0.00278 * tw * temp + 3.0
        return round(val, 1)
    if wind_ms is None:
        return temp
    v = wind_ms * 3.6  # km/h
    if temp > 10 or v < 4.8:
        return temp
    val = 13.12 + 0.6215 * temp - 11.37 * v ** 0.16 + 0.3965 * temp * v ** 0.16
    return round(val, 1)
