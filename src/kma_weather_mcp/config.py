"""환경변수 기반 설정."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # 공공데이터포털(data.go.kr) 일반 인증키 — "Decoding" 키를 넣으세요.
    kma_service_key: str
    # 에어코리아 키. 같은 data.go.kr 계정이면 보통 기상청 키와 동일하므로 생략 가능.
    airkorea_service_key: str
    # 지명 → 좌표 변환용 Nominatim(OpenStreetMap) 주소. 키 불필요, 자체 호스팅 시 교체.
    nominatim_url: str
    # 원격 접속 보호용 Bearer 토큰. 비워두면 인증 없이 열립니다.
    mcp_auth_token: str
    ########
    # 툴 인가용 OPA API 주소. 비워두면 하드코딩된 허용 목록(MOCK_RESPONSE)을 사용합니다.
    opa_url: str
    ########
    host: str
    port: int
    path: str
    http_timeout: float

    @classmethod
    def from_env(cls) -> "Settings":
        kma_key = os.getenv("KMA_SERVICE_KEY", "").strip()
        return cls(
            kma_service_key=kma_key,
            airkorea_service_key=os.getenv("AIRKOREA_SERVICE_KEY", "").strip() or kma_key,
            nominatim_url=os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org").strip().rstrip("/"),
            mcp_auth_token=os.getenv("MCP_AUTH_TOKEN", "").strip(),
            ########
            opa_url=os.getenv("OPA_URL", "").strip(),
            ########
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            path=os.getenv("MCP_PATH", "/mcp"),
            http_timeout=float(os.getenv("HTTP_TIMEOUT", "10")),
        )
