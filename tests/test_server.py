"""외부 API를 respx로 목킹해 툴 동작을 검증한다."""

from __future__ import annotations

import json
import os
from datetime import timedelta

os.environ.setdefault("KMA_SERVICE_KEY", "test-key")

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from kma_weather_mcp import server
from kma_weather_mcp.clients import AIR_QUALITY, AIR_STATION, KMA_FCST, KMA_WRN, now_kst
from kma_weather_mcp.geo import latlon_to_grid, latlon_to_tm


def kma(items, code="00"):
    return {"response": {"header": {"resultCode": code, "resultMsg": "NORMAL_SERVICE"},
                         "body": {"items": {"item": items}, "totalCount": len(items)}}}


def air(items):
    return {"response": {"header": {"resultCode": "00"}, "body": {"items": items}}}


def vilage_items():
    now = now_kst().replace(minute=0, second=0, microsecond=0)
    out = []
    for h in range(0, 80):
        t = now + timedelta(hours=h)
        d, tm = t.strftime("%Y%m%d"), t.strftime("%H00")
        rainy = t.date() == (now + timedelta(days=1)).date() and 12 <= t.hour < 15  # 내일 오후 비
        vals = {"TMP": str(10 + (t.hour % 12)), "SKY": "4" if rainy else "1", "PTY": "1" if rainy else "0",
                "POP": "80" if rainy else "10", "PCP": "2.0mm" if rainy else "강수없음", "SNO": "적설없음",
                "REH": "60", "WSD": "3.2", "VEC": "300"}
        if tm == "0600":
            vals["TMN"] = "5.0"
        if tm == "1500":
            vals["TMX"] = "21.0"
        for k, v in vals.items():
            out.append({"fcstDate": d, "fcstTime": tm, "category": k, "fcstValue": v})
    return out


@pytest.fixture(autouse=True)
def fresh_clients():
    server._clients = None
    yield
    server._clients = None


@pytest.fixture
def mocked():
    with respx.mock(assert_all_called=False) as m:
        m.get(f"{server.settings.nominatim_url}/search").respond(json=[
            {"name": "화성시", "display_name": "화성시, 경기도, 대한민국", "lat": "37.1995", "lon": "126.8310",
             "category": "boundary", "type": "administrative", "addresstype": "city"},
            {"name": "동탄", "display_name": "동탄, 동탄역로, 여울동, 화성시, 경기도, 18478, 대한민국",
             "lat": "37.2003", "lon": "127.0960", "category": "railway", "type": "station", "addresstype": "railway"}])
        m.get(f"{KMA_FCST}/getUltraSrtNcst").respond(json=kma([
            {"category": "T1H", "obsrValue": "14.2"}, {"category": "RN1", "obsrValue": "0"},
            {"category": "REH", "obsrValue": "55"}, {"category": "PTY", "obsrValue": "0"},
            {"category": "WSD", "obsrValue": "2.1"}, {"category": "VEC", "obsrValue": "315"}]))
        m.get(f"{KMA_FCST}/getVilageFcst").respond(json=kma(vilage_items()))
        m.get(f"{AIR_STATION}/getNearbyMsrstnList").respond(json=air([
            {"stationName": "점검소", "addr": "A", "tm": 1.1},
            {"stationName": "동탄", "addr": "경기 화성시 동탄", "tm": 2.4}]))
        m.get(f"{AIR_QUALITY}/getMsrstnAcctoRltmMesureDnsty", params={"stationName": "점검소"}).respond(
            json=air([{"dataTime": "2026-10-20 22:00", "pm10Value": "-", "pm25Value": "-"}]))
        m.get(f"{AIR_QUALITY}/getMsrstnAcctoRltmMesureDnsty", params={"stationName": "동탄"}).respond(
            json=air([{"dataTime": "2026-10-20 22:00", "pm10Value": "42", "pm25Value": "28", "o3Value": "0.021",
                       "pm10Grade1h": "2", "pm25Grade1h": "3", "khaiValue": "88", "khaiGrade": "2"}]))
        m.get(f"{KMA_WRN}/getPwnStatus").respond(json=kma([{
            "tmFc": "202610202200", "tmEf": "202610202300",
            "t6": "o 건조주의보 : 강원도(강릉시평지, 동해시평지), 경상북도(포항)\r\no 풍랑주의보 : 동해중부앞바다",
            "t7": "(1) 강풍 예비특보\r\no 10월 21일 오전 : 경기도(화성, 평택), 충청남도(태안)"}]))
        yield m


async def call(name, **args):
    async with Client(server.mcp) as c:
        res = await c.call_tool(name, args)
        return res.structured_content or json.loads(res.content[0].text)


def test_grid_and_tm():
    assert latlon_to_grid(37.5665, 126.9780) == (60, 127)  # 서울시청
    assert latlon_to_grid(33.4996, 126.5312) == (53, 38)  # 제주시
    x, y = latlon_to_tm(37.5665, 126.9780)
    assert 195000 < x < 201000 and 448000 < y < 455000


async def test_list_tools():
    async with Client(server.mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert set(tools) == {"search_location", "get_current_weather", "get_forecast",
                          "get_air_quality", "get_weather_alerts"}
    assert tools["get_forecast"].annotations.read_only_hint is True


async def test_search_location(mocked):
    r = await call("search_location", query="화성")
    assert r["count"] == 2
    assert r["candidates"][0]["kind"] == "행정구역"
    assert r["candidates"][0]["name"] == "경기도 화성시"
    assert r["candidates"][1]["kind"] == "장소"
    assert r["candidates"][1]["name"] == "동탄"
    assert r["candidates"][1]["address"] == "경기도 화성시 여울동 동탄역로 동탄"
    assert "nx" in r["candidates"][0]["kma_grid"]


async def test_current_weather(mocked):
    r = await call("get_current_weather", location="화성시")
    assert r["location"]["name"] == "경기도 화성시"
    assert r["temperature_c"] == 14.2
    assert r["condition"] == "맑음"
    assert r["wind_direction"] == "북서"
    assert r["feels_like_c"] is not None


async def test_forecast_hourly_and_daily(mocked):
    h = await call("get_forecast", lat=37.2, lon=127.07, type="hourly", hours=6)
    assert len(h["hours"]) == 6
    assert h["hours"][0]["precip_mm"] == 0.0

    d = await call("get_forecast", lat=37.2, lon=127.07, type="daily", days=3)
    assert 1 <= len(d["days"]) <= 3
    tomorrow = d["days"][1]
    assert tomorrow["pm"]["condition"] == "비"
    assert tomorrow["pm"]["max_precip_probability_pct"] == 80
    assert tomorrow["total_precip_mm"] > 0
    assert tomorrow["high_c"] == 21.0 and tomorrow["low_c"] == 5.0


async def test_air_quality_skips_station_under_maintenance(mocked):
    r = await call("get_air_quality", location="동탄")
    assert r["station"]["name"] == "동탄"
    assert r["pm25_grade"] == "나쁨"
    assert r["overall_grade"] == "보통"


async def test_alerts_filter(mocked):
    allr = await call("get_weather_alerts")
    assert len(allr["active"]) == 2
    gg = await call("get_weather_alerts", region="화성시")
    assert gg["active"] == []
    assert gg["preliminary"][0]["areas"].startswith("경기도")
    gw = await call("get_weather_alerts", region="강원도")
    assert gw["active"][0]["warning"] == "건조주의보"


async def test_errors(mocked):
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="location 또는 lat/lon"):
            await c.call_tool("get_current_weather", {})
        with pytest.raises(ToolError, match="대한민국 범위 밖"):
            await c.call_tool("get_current_weather", {"lat": 35.68, "lon": 139.76})


async def test_no_data_falls_back_to_previous_base_time(mocked):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.params["base_time"])
        if len(calls) == 1:
            return httpx.Response(200, json=kma([], code="03"))
        return httpx.Response(200, json=kma([{"category": "T1H", "obsrValue": "9.0"}]))

    mocked.get(f"{KMA_FCST}/getUltraSrtNcst").mock(side_effect=handler)
    r = await call("get_current_weather", lat=37.2, lon=127.07)
    assert r["temperature_c"] == 9.0 and len(calls) == 2


async def test_invalid_service_key_xml(mocked):
    xml = ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
           "<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
           "<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
    mocked.get(f"{KMA_WRN}/getPwnStatus").respond(text=xml)
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="인증키 오류"):
            await c.call_tool("get_weather_alerts", {})
