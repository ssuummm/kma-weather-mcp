"""외부 API 클라이언트: OpenStreetMap Nominatim, 기상청(단기예보·특보), 에어코리아."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastmcp.exceptions import ToolError

from .config import Settings

KST = ZoneInfo("Asia/Seoul")

DATA_GO_KR = "https://apis.data.go.kr"
KMA_FCST = f"{DATA_GO_KR}/1360000/VilageFcstInfoService_2.0"
KMA_WRN = f"{DATA_GO_KR}/1360000/WthrWrnInfoService"
AIR_STATION = f"{DATA_GO_KR}/B552584/MsrstnInfoInqireSvc"
AIR_QUALITY = f"{DATA_GO_KR}/B552584/ArpltnInforInqireSvc"
USER_AGENT = "kma-weather-mcp/0.1"

# Nominatim 결과 중 "행정구역"으로 볼 유형 (그 외는 "장소")
_ADMIN_TYPES = {"administrative", "city", "town", "village", "borough", "suburb", "quarter",
                "neighbourhood", "province", "state", "county", "municipality", "city_district"}
# 공개 Nominatim 이용정책: 초당 1회 이하
_NOMINATIM_INTERVAL = 1.0

# data.go.kr 결과코드 중 "자료 없음"
_NO_DATA = {"03"}


class NoDataError(Exception):
    """해당 발표시각 자료가 아직 없음 (이전 발표시각으로 재시도 대상)."""


class TTLCache:
    """아주 단순한 인메모리 TTL 캐시 — 공공 API 일일 호출 한도 절약용."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._store.get(key)
        if not hit:
            return None
        expires, value = hit
        if expires < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if len(self._store) > 2000:  # 무한 증가 방지
            self._store.clear()
        self._store[key] = (time.monotonic() + ttl, value)


def now_kst() -> datetime:
    return datetime.now(KST)


class WeatherClients:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=settings.http_timeout)
        self.cache = TTLCache()
        self._geo_lock = asyncio.Lock()
        self._geo_last = 0.0

    async def aclose(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ 공통
    def _require(self, value: str, name: str) -> None:
        if not value:
            raise ToolError(f"서버 설정 오류: 환경변수 {name} 가 설정되지 않았습니다. 서버 관리자에게 문의하세요.")

    async def _data_go_kr(self, url: str, params: dict[str, Any], key: str, ttl: float) -> list[dict]:
        """data.go.kr 공통 호출. items 리스트를 반환하고, 에러를 해석한다."""
        cache_key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        q = {"serviceKey": key, **params}
        try:
            r = await self.http.get(url, params=q)
        except httpx.HTTPError as e:
            raise ToolError(f"기상 데이터 서버에 연결하지 못했습니다 ({type(e).__name__}). 잠시 후 다시 시도하세요.") from e

        text = r.text
        try:
            body = r.json()
        except ValueError:
            # 인증 오류 등은 JSON 요청이어도 XML로 내려온다.
            msg = re.search(r"<returnAuthMsg>(.*?)</returnAuthMsg>", text) or re.search(
                r"<resultMsg>(.*?)</resultMsg>", text
            )
            code = re.search(r"<(?:returnReasonCode|resultCode)>(.*?)</", text)
            if code and code.group(1) in _NO_DATA:
                raise NoDataError()
            detail = msg.group(1) if msg else f"HTTP {r.status_code}"
            if "SERVICE_KEY" in detail:
                raise ToolError(f"공공데이터포털 인증키 오류({detail}). 서버의 인증키 설정 또는 활용신청 승인 여부를 확인하세요.")
            raise ToolError(f"기상 데이터 조회 실패: {detail}")

        resp = body.get("response", body)
        header = resp.get("header", {})
        code = str(header.get("resultCode", "00"))
        if code in _NO_DATA:
            raise NoDataError()
        if code != "00":
            raise ToolError(f"기상 데이터 조회 실패 (코드 {code}: {header.get('resultMsg', '')})")

        items = (resp.get("body") or {}).get("items") or []
        if isinstance(items, dict):  # 기상청: {"item": [...]}
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        self.cache.set(cache_key, items, ttl)
        return items

    # ------------------------------------------------------- 지명 검색 (Nominatim)
    async def _nominatim_throttle(self) -> None:
        async with self._geo_lock:
            wait = self._geo_last + _NOMINATIM_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._geo_last = time.monotonic()

    @staticmethod
    def _nominatim_doc(d: dict) -> dict:
        # display_name: "동탄, 동탄역로, 여울동, 화성시, 경기도, 18478, 대한민국" → 큰 단위부터 역순
        parts = [p.strip() for p in d.get("display_name", "").split(",")]
        parts = [p for p in parts if p and p != "대한민국" and not p.isdigit()]
        name = d.get("name") or (parts[0] if parts else "")
        address = " ".join(reversed(parts))
        admin = d.get("type") in _ADMIN_TYPES or d.get("addresstype") in _ADMIN_TYPES
        return {
            "name": address if admin else name,
            "address": address,
            "lat": float(d["lat"]),
            "lon": float(d["lon"]),
            "kind": "행정구역" if admin else "장소",
        }

    async def search_location(self, query: str, size: int = 5) -> list[dict]:
        cache_key = f"geo:{query}:{size}"
        if (c := self.cache.get(cache_key)) is not None:
            return c

        params = {"q": query, "format": "jsonv2", "countrycodes": "kr",
                  "accept-language": "ko", "limit": size}
        await self._nominatim_throttle()
        try:
            r = await self.http.get(f"{self.s.nominatim_url}/search", params=params,
                                    headers={"User-Agent": USER_AGENT})
            if r.status_code in (403, 429):
                raise ToolError("지명 검색 요청이 너무 많습니다. 잠시 후 다시 시도하거나 lat/lon을 직접 입력하세요.")
            r.raise_for_status()
            docs = r.json()
        except httpx.HTTPError as e:
            raise ToolError(f"지명 검색 서버 오류 ({type(e).__name__}). 잠시 후 다시 시도하세요.") from e

        results = [self._nominatim_doc(d) for d in docs][:size]
        self.cache.set(cache_key, results, 24 * 3600)
        return results

    # -------------------------------------------------------------- 기상청 예보
    @staticmethod
    def _ncst_base_times(now: datetime) -> list[datetime]:
        """초단기실황: 매시 정시 발표, 약 10분 후 제공. 최신 → 이전 순 후보 2개."""
        t = (now - timedelta(minutes=15)).replace(minute=0, second=0, microsecond=0)
        return [t, t - timedelta(hours=1)]

    @staticmethod
    def _vilage_base_times(now: datetime) -> list[datetime]:
        """단기예보: 02,05,08,11,14,17,20,23시 발표, 약 10분 후 제공."""
        t = now - timedelta(minutes=15)
        candidates = []
        for back in range(0, 30):
            c = (t - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            if c.hour in (2, 5, 8, 11, 14, 17, 20, 23):
                candidates.append(c)
                if len(candidates) == 2:
                    break
        return candidates

    async def _kma_with_fallback(self, op: str, nx: int, ny: int, bases: list[datetime],
                                 rows: int, ttl: float) -> tuple[datetime, list[dict]]:
        self._require(self.s.kma_service_key, "KMA_SERVICE_KEY")
        for base in bases:
            params = {
                "pageNo": 1, "numOfRows": rows, "dataType": "JSON",
                "base_date": base.strftime("%Y%m%d"), "base_time": base.strftime("%H%M"),
                "nx": nx, "ny": ny,
            }
            try:
                items = await self._data_go_kr(f"{KMA_FCST}/{op}", params, self.s.kma_service_key, ttl)
            except NoDataError:
                continue
            if items:
                return base, items
        raise ToolError("기상청 최신 발표 자료를 아직 받을 수 없습니다. 몇 분 후 다시 시도하세요.")

    async def ultra_srt_ncst(self, nx: int, ny: int) -> tuple[datetime, list[dict]]:
        return await self._kma_with_fallback(
            "getUltraSrtNcst", nx, ny, self._ncst_base_times(now_kst()), rows=20, ttl=600)

    async def vilage_fcst(self, nx: int, ny: int) -> tuple[datetime, list[dict]]:
        return await self._kma_with_fallback(
            "getVilageFcst", nx, ny, self._vilage_base_times(now_kst()), rows=1500, ttl=1800)

    # -------------------------------------------------------------- 기상청 특보
    async def warning_status(self) -> list[dict]:
        """전국 기상특보 발효현황."""
        self._require(self.s.kma_service_key, "KMA_SERVICE_KEY")
        try:
            return await self._data_go_kr(
                f"{KMA_WRN}/getPwnStatus",
                {"pageNo": 1, "numOfRows": 10, "dataType": "JSON"},
                self.s.kma_service_key, ttl=600,
            )
        except NoDataError:
            return []

    # ----------------------------------------------------------------- 에어코리아
    async def nearby_stations(self, tm_x: float, tm_y: float) -> list[dict]:
        self._require(self.s.airkorea_service_key, "AIRKOREA_SERVICE_KEY 또는 KMA_SERVICE_KEY")
        try:
            return await self._data_go_kr(
                f"{AIR_STATION}/getNearbyMsrstnList",
                {"tmX": tm_x, "tmY": tm_y, "returnType": "json", "ver": "1.1"},
                self.s.airkorea_service_key, ttl=7 * 24 * 3600,
            )
        except NoDataError:
            return []

    async def station_realtime(self, station: str) -> list[dict]:
        try:
            return await self._data_go_kr(
                f"{AIR_QUALITY}/getMsrstnAcctoRltmMesureDnsty",
                {"stationName": station, "dataTerm": "DAILY", "pageNo": 1, "numOfRows": 3,
                 "returnType": "json", "ver": "1.3"},
                self.s.airkorea_service_key, ttl=900,
            )
        except NoDataError:
            return []
