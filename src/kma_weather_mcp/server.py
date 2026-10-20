"""국내 날씨 MCP 서버 (FastMCP, Streamable HTTP)."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import codes
from .clients import KST, WeatherClients, now_kst
from .config import Settings
from .geo import in_korea, latlon_to_grid, latlon_to_tm

settings = Settings.from_env()

_auth = None
if settings.mcp_auth_token:
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    _auth = StaticTokenVerifier(tokens={settings.mcp_auth_token: {"client_id": "weather-client", "scopes": []}})

mcp = FastMCP(
    name="kma-weather",
    version="0.1.0",
    instructions=(
        "대한민국 날씨 정보 서버입니다 (기상청 단기예보·기상특보, 에어코리아 대기질). "
        "모든 날씨 툴은 location(지명) 또는 lat/lon(위경도) 중 하나로 위치를 받습니다. "
        "지명이 모호하면 search_location으로 후보를 먼저 확인한 뒤 lat/lon을 넘기세요. "
        "시각은 모두 한국 표준시(KST)입니다. 해외 지역은 지원하지 않습니다."
    ),
    auth=_auth,
    mask_error_details=True,
)

_clients: WeatherClients | None = None


def clients() -> WeatherClients:
    global _clients
    if _clients is None:
        _clients = WeatherClients(settings)
    return _clients


READ_ONLY = {"read_only_hint": True, "open_world_hint": True, "idempotent_hint": True}

LocationArg = Annotated[
    str | None,
    Field(description="지명·주소·장소명. 예: '화성시 동탄', '강남역', '설악산'. lat/lon을 줄 경우 생략"),
]
LatArg = Annotated[float | None, Field(description="위도 (예: 37.20)", ge=-90, le=90)]
LonArg = Annotated[float | None, Field(description="경도 (예: 127.07)", ge=-180, le=180)]


# ---------------------------------------------------------------- 내부 헬퍼
async def _resolve(location: str | None, lat: float | None, lon: float | None) -> dict[str, Any]:
    if lat is not None and lon is not None:
        place = {"name": location or f"{lat:.4f}, {lon:.4f}", "lat": lat, "lon": lon}
    elif location:
        found = await clients().search_location(location, size=1)
        if not found:
            raise ToolError(
                f"'{location}' 위치를 찾지 못했습니다. 시·군·구나 동 이름, 또는 잘 알려진 장소명으로 다시 시도하세요."
            )
        place = {"name": found[0]["name"], "lat": found[0]["lat"], "lon": found[0]["lon"]}
    else:
        raise ToolError("location 또는 lat/lon 중 하나는 반드시 입력해야 합니다.")

    if not in_korea(place["lat"], place["lon"]):
        raise ToolError("대한민국 범위 밖의 위치입니다. 이 서버는 국내 날씨만 지원합니다.")
    place["lat"], place["lon"] = round(place["lat"], 5), round(place["lon"], 5)
    return place


def _fmt_dt(date: str, time: str) -> str:
    return f"{date[:4]}-{date[4:6]}-{date[6:8]} {time[:2]}:{time[2:]}"


def _group_fcst(items: list[dict]) -> dict[tuple[str, str], dict[str, str]]:
    """단기예보 item 목록을 (fcstDate, fcstTime) → {category: value} 로 묶는다."""
    grouped: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for it in items:
        grouped[(it["fcstDate"], it["fcstTime"])][it["category"]] = it["fcstValue"]
    return dict(sorted(grouped.items()))


def _hour_entry(date: str, time: str, v: dict[str, str]) -> dict[str, Any]:
    wsd = codes.to_float(v.get("WSD"))
    vec = codes.to_float(v.get("VEC"))
    return {
        "time": _fmt_dt(date, time),
        "temperature_c": codes.to_float(v.get("TMP")),
        "condition": codes.sky_condition(v.get("SKY"), v.get("PTY")),
        "precip_probability_pct": codes.to_float(v.get("POP")),
        "precip_mm": codes.to_float(v.get("PCP")),
        "snow_cm": codes.to_float(v.get("SNO")),
        "humidity_pct": codes.to_float(v.get("REH")),
        "wind_speed_ms": wsd,
        "wind_direction": codes.wind_direction(vec),
    }


# ---------------------------------------------------------------------- 툴
@mcp.tool(annotations={**READ_ONLY, "title": "지명 검색"})
async def search_location(
    query: Annotated[str, Field(description="검색할 지명·주소·장소명. 예: '동탄', '해운대', '제주공항'", min_length=1)],
    limit: Annotated[int, Field(description="후보 개수", ge=1, le=10)] = 5,
) -> dict:
    """지명을 위경도 후보 목록으로 변환합니다.

    같은 이름의 지역이 여러 곳일 수 있을 때(예: '신촌', '중앙동') 먼저 호출해
    사용자가 원하는 곳을 고른 뒤, 다른 툴에 lat/lon으로 넘기세요.
    """
    found = await clients().search_location(query, size=limit)
    candidates = []
    for c in found:
        nx, ny = latlon_to_grid(c["lat"], c["lon"])
        candidates.append({**c, "kma_grid": {"nx": nx, "ny": ny}, "supported": in_korea(c["lat"], c["lon"])})
    return {
        "query": query,
        "count": len(candidates),
        "candidates": candidates,
        "hint": None if candidates else "결과가 없습니다. 더 넓은 지명(시·군·구)이나 다른 표기로 검색해 보세요.",
    }


@mcp.tool(annotations={**READ_ONLY, "title": "현재 날씨"})
async def get_current_weather(location: LocationArg = None, lat: LatArg = None, lon: LonArg = None) -> dict:
    """현재 날씨(기상청 초단기실황)를 조회합니다.

    기온, 체감온도, 하늘상태, 강수형태·1시간 강수량, 습도, 바람과 함께
    오늘의 최고·최저 예보기온을 돌려줍니다.
    """
    place = await _resolve(location, lat, lon)
    nx, ny = latlon_to_grid(place["lat"], place["lon"])
    base, items = await clients().ultra_srt_ncst(nx, ny)
    obs = {it["category"]: it["obsrValue"] for it in items}

    temp = codes.to_float(obs.get("T1H"))
    hum = codes.to_float(obs.get("REH"))
    wsd = codes.to_float(obs.get("WSD"))
    pty = str(obs.get("PTY", "0")).split(".")[0]

    # 실황에는 하늘상태(SKY)와 최고/최저가 없으므로 단기예보에서 보완 (실패해도 진행)
    sky, today_max, today_min = None, None, None
    try:
        _, fitems = await clients().vilage_fcst(nx, ny)
        grouped = _group_fcst(fitems)
        now = now_kst()
        key_now = (now.strftime("%Y%m%d"), now.strftime("%H00"))
        for k, v in grouped.items():
            if k >= key_now and "SKY" in v:
                sky = v["SKY"]
                break
        today = now.strftime("%Y%m%d")
        temps = [codes.to_float(v.get("TMP")) for (d, _), v in grouped.items() if d == today]
        temps = [t for t in temps if t is not None]
        for (d, _), v in grouped.items():
            if d == today:
                today_max = codes.to_float(v.get("TMX")) if "TMX" in v else today_max
                today_min = codes.to_float(v.get("TMN")) if "TMN" in v else today_min
        if temps:
            today_max = today_max if today_max is not None else max(temps)
            today_min = today_min if today_min is not None else min(temps)
    except ToolError:
        pass

    return {
        "location": place,
        "observed_at": base.strftime("%Y-%m-%d %H:%M KST"),
        "temperature_c": temp,
        "feels_like_c": codes.feels_like(temp, hum, wsd, base),
        "condition": codes.sky_condition(sky, pty),
        "precipitation_type": codes.PTY.get(pty, pty),
        "rain_1h_mm": codes.to_float(obs.get("RN1")),
        "humidity_pct": hum,
        "wind_speed_ms": wsd,
        "wind_direction": codes.wind_direction(codes.to_float(obs.get("VEC"))),
        "today_high_c": today_max,
        "today_low_c": today_min,
        "source": "기상청 초단기실황·단기예보",
    }


@mcp.tool(annotations={**READ_ONLY, "title": "날씨 예보"})
async def get_forecast(
    location: LocationArg = None,
    lat: LatArg = None,
    lon: LonArg = None,
    type: Annotated[
        Literal["hourly", "daily"],
        Field(description="hourly: 1시간 단위 예보, daily: 날짜별 요약(최고·최저, 오전/오후 날씨)"),
    ] = "hourly",
    hours: Annotated[int, Field(description="hourly일 때 지금부터 몇 시간까지 (1~72)", ge=1, le=72)] = 24,
    days: Annotated[int, Field(description="daily일 때 오늘부터 며칠까지 (1~4)", ge=1, le=4)] = 3,
) -> dict:
    """기상청 단기예보(최대 약 3일)를 시간별 또는 일별로 조회합니다.

    '내일 비 와?', '주말 날씨', '오후에 우산 필요해?' 같은 질문에 사용하세요.
    강수확률(%)과 예상 강수량(mm)을 함께 보고 판단하세요.
    """
    place = await _resolve(location, lat, lon)
    nx, ny = latlon_to_grid(place["lat"], place["lon"])
    base, items = await clients().vilage_fcst(nx, ny)
    grouped = _group_fcst(items)
    now = now_kst()
    key_now = (now.strftime("%Y%m%d"), now.strftime("%H00"))
    common = {
        "location": place,
        "issued_at": base.strftime("%Y-%m-%d %H:%M KST"),
        "source": "기상청 단기예보",
    }

    if type == "hourly":
        entries = [_hour_entry(d, t, v) for (d, t), v in grouped.items() if (d, t) >= key_now and "TMP" in v]
        return {**common, "type": "hourly", "hours": entries[:hours]}

    by_date: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for (d, t), v in grouped.items():
        by_date[d][t] = v

    daily = []
    for d in sorted(by_date)[: days + 1]:
        if d < key_now[0]:
            continue
        slots = by_date[d]
        temps = [codes.to_float(v.get("TMP")) for v in slots.values()]
        temps = [x for x in temps if x is not None]
        tmx = next((codes.to_float(v["TMX"]) for v in slots.values() if "TMX" in v), None)
        tmn = next((codes.to_float(v["TMN"]) for v in slots.values() if "TMN" in v), None)

        def half(hrs: range) -> dict[str, Any] | None:
            sel = [v for t, v in slots.items() if int(t[:2]) in hrs]
            if not sel:
                return None
            conds = [codes.sky_condition(v.get("SKY"), v.get("PTY")) for v in sel]
            rainy = [c for c in conds if c and c not in codes.SKY.values()]
            cond = Counter(rainy).most_common(1)[0][0] if rainy else Counter(conds).most_common(1)[0][0]
            pops = [codes.to_float(v.get("POP")) or 0 for v in sel]
            return {"condition": cond, "max_precip_probability_pct": max(pops)}

        precip = sum((codes.to_float(v.get("PCP")) or 0) for v in slots.values())
        snow = sum((codes.to_float(v.get("SNO")) or 0) for v in slots.values())
        daily.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
            "weekday": "월화수목금토일"[datetime.strptime(d, "%Y%m%d").weekday()],
            "high_c": tmx if tmx is not None else (max(temps) if temps else None),
            "low_c": tmn if tmn is not None else (min(temps) if temps else None),
            "am": half(range(6, 12)),
            "pm": half(range(12, 18)),
            "total_precip_mm": round(precip, 1),
            "total_snow_cm": round(snow, 1),
            "partial_day": len(slots) < 24,
        })
    return {**common, "type": "daily", "days": daily[:days]}


@mcp.tool(annotations={**READ_ONLY, "title": "대기질(미세먼지)"})
async def get_air_quality(location: LocationArg = None, lat: LatArg = None, lon: LonArg = None) -> dict:
    """가장 가까운 에어코리아 측정소의 실시간 대기질을 조회합니다.

    미세먼지(PM10), 초미세먼지(PM2.5), 오존, 통합대기환경지수와 등급
    (좋음/보통/나쁨/매우나쁨)을 돌려줍니다.
    """
    place = await _resolve(location, lat, lon)
    tm_x, tm_y = latlon_to_tm(place["lat"], place["lon"])
    stations = await clients().nearby_stations(tm_x, tm_y)
    if not stations:
        raise ToolError("주변 대기질 측정소를 찾지 못했습니다.")

    for st in stations[:3]:  # 점검 중인 측정소는 건너뛴다
        rows = await clients().station_realtime(st["stationName"])
        row = next((r for r in rows if codes.to_float(r.get("pm10Value")) is not None
                    or codes.to_float(r.get("pm25Value")) is not None), None)
        if not row:
            continue

        def grade(*keys: str) -> str | None:
            for k in keys:
                g = row.get(k)
                if g not in (None, "", "-"):
                    return codes.AIR_GRADE.get(str(g), str(g))
            return None

        return {
            "location": place,
            "station": {"name": st["stationName"], "address": st.get("addr"),
                        "distance_km": codes.to_float(st.get("tm"))},
            "measured_at": row.get("dataTime"),
            "pm10_ugm3": codes.to_float(row.get("pm10Value")),
            "pm10_grade": grade("pm10Grade1h", "pm10Grade"),
            "pm25_ugm3": codes.to_float(row.get("pm25Value")),
            "pm25_grade": grade("pm25Grade1h", "pm25Grade"),
            "o3_ppm": codes.to_float(row.get("o3Value")),
            "o3_grade": grade("o3Grade"),
            "no2_ppm": codes.to_float(row.get("no2Value")),
            "co_ppm": codes.to_float(row.get("coValue")),
            "so2_ppm": codes.to_float(row.get("so2Value")),
            "khai_index": codes.to_float(row.get("khaiValue")),
            "overall_grade": grade("khaiGrade"),
            "source": "에어코리아 (실시간 측정값, 확정 전 자료)",
        }
    raise ToolError("주변 측정소가 모두 점검 중이거나 최근 측정값이 없습니다.")


_SUFFIX = re.compile(r"(특별자치시|특별자치도|특별시|광역시|시|군|구|도)$")


@mcp.tool(annotations={**READ_ONLY, "title": "기상특보"})
async def get_weather_alerts(
    region: Annotated[
        str | None,
        Field(description="지역 키워드로 필터링. 예: '경기도', '화성', '제주'. 생략하면 전국"),
    ] = None,
) -> dict:
    """기상청 기상특보 발효 현황과 예비특보를 조회합니다.

    호우·대설·한파·폭염·태풍·강풍·건조·풍랑 등. 특보가 없으면 빈 목록을 돌려줍니다.
    """
    items = await clients().warning_status()
    if not items:
        return {"region": region or "전국", "active": [], "preliminary": [], "message": "현재 발효 중인 특보가 없습니다."}
    latest = items[0]

    keywords: list[str] = []
    if region:
        r = region.strip()
        keywords = [r]
        stripped = _SUFFIX.sub("", r)
        if stripped and stripped != r:
            keywords.append(stripped)

    def parse(text: str | None) -> list[dict[str, str]]:
        out = []
        for line in (text or "").splitlines():
            line = line.strip().lstrip("o").strip()
            if not line or ":" not in line:
                continue
            kind, areas = (s.strip() for s in line.split(":", 1))
            if keywords and not any(k in areas or k in kind for k in keywords):
                continue
            out.append({"warning": kind, "areas": areas})
        return out

    active = parse(latest.get("t6"))
    prelim = parse(latest.get("t7"))
    tm_fc = str(latest.get("tmFc", ""))
    return {
        "region": region or "전국",
        "announced_at": _fmt_dt(tm_fc[:8], tm_fc[8:12]) + " KST" if len(tm_fc) >= 12 else tm_fc,
        "effective_at": latest.get("tmEf"),
        "active": active,
        "preliminary": prelim,
        "message": None if (active or prelim) else
        (f"'{region}' 지역에 해당하는 특보가 없습니다." if region else "현재 발효 중인 특보가 없습니다."),
        "source": "기상청 기상특보",
    }


# ------------------------------------------------------------ 헬스체크 / 실행
@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "time": datetime.now(KST).isoformat(timespec="seconds"),
        "configured": {
            "kma": bool(settings.kma_service_key),
            "airkorea": bool(settings.airkorea_service_key),
            "auth": bool(settings.mcp_auth_token),
        },
    })


def main() -> None:
    mcp.run(transport="http", host=settings.host, port=settings.port, path=settings.path)


if __name__ == "__main__":
    main()
