# kma-weather-mcp

기상청·에어코리아 공공 API 기반의 **국내 날씨 MCP 서버**입니다.
FastMCP로 작성했고, 원격 **Streamable HTTP** 방식으로 동작합니다.

## 제공 툴

| 툴 | 설명 | 데이터 |
|---|---|---|
| `search_location` | 지명·주소·장소명 → 위경도 후보 (기상청 격자 포함) | OpenStreetMap Nominatim |
| `get_current_weather` | 현재 기온·체감온도·하늘상태·강수·습도·바람 + 오늘 최고/최저 | 기상청 초단기실황 + 단기예보 |
| `get_forecast` | 시간별(최대 72시간) / 일별(최대 4일, 오전·오후 요약) 예보 | 기상청 단기예보 |
| `get_air_quality` | 가장 가까운 측정소의 PM10·PM2.5·오존·통합지수와 등급 | 에어코리아 |
| `get_weather_alerts` | 기상특보 발효 현황·예비특보 (지역 키워드 필터) | 기상청 기상특보 |

날씨 툴은 모두 `location`(지명) 또는 `lat`/`lon` 중 하나로 위치를 받습니다.
기상청 격자(nx, ny)·에어코리아 TM 좌표 변환은 서버 내부에서 처리합니다.

## 준비물 (API 키)

1. **공공데이터포털** (https://www.data.go.kr) 에서 아래 4개 API 활용신청 → 일반 인증키(**Decoding**)
   - 기상청_단기예보 ((구)_동네예보) 조회서비스
   - 기상청_기상특보 조회서비스
   - 한국환경공단_에어코리아_대기오염정보
   - 한국환경공단_에어코리아_측정소정보
2. 지명 검색은 **OpenStreetMap Nominatim**을 사용하므로 별도 키가 필요 없습니다.
   공개 서버는 [이용정책](https://operations.osmfoundation.org/policies/nominatim/)상 초당 1회 이하만 허용되어
   서버가 자동으로 간격을 두고, 결과를 24시간 캐시합니다. 트래픽이 많다면 `NOMINATIM_URL`에 자체 호스팅 주소를 넣으세요.

> 공공데이터포털은 Encoding/Decoding 두 키를 줍니다. 이 서버는 httpx가 URL 인코딩을 하므로 **Decoding 키**를 넣어야 합니다.
> 활용신청 직후에는 키가 동기화되기까지 1~2시간 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`가 날 수 있습니다.

## 실행

```bash
pip install -e ".[dev]"
export KMA_SERVICE_KEY=<공공데이터포털 Decoding 키>
kma-weather-mcp        # → http://0.0.0.0:8000/mcp
```

헬스체크: `GET /health` (키 설정 여부도 표시)

### Docker

```bash
docker build -t kma-weather-mcp .
docker run -d -p 8000:8000 -e KMA_SERVICE_KEY=<키> kma-weather-mcp
```

### Prefect Horizon

[Prefect Horizon](https://horizon.prefect.io)에 GitHub 저장소를 연결해 배포할 수 있습니다.

1. 이 저장소를 GitHub에 push (`.env`는 `.gitignore`로 제외됨)
2. horizon.prefect.io 에 GitHub 계정으로 로그인 → 저장소 선택
3. 설정
   - **Entrypoint**: `app.py:mcp` (src 레이아웃·상대 import 때문에 루트 래퍼를 사용)
   - **Environment variables**: `KMA_SERVICE_KEY` (필요하면 `AIRKOREA_SERVICE_KEY`, `NOMINATIM_URL`)
   - **Authentication**: Horizon 인증을 켜면 조직 사용자만 OAuth로 접속합니다. 이 경우 `MCP_AUTH_TOKEN`은 비워 두세요.
4. **Deploy Server** → `https://<server-name>.fastmcp.app/mcp`

`main` 브랜치에 push하면 자동 재배포되고, PR마다 미리보기 배포가 생깁니다.
배포 전 확인: `fastmcp inspect app.py:mcp`

## 인증

`MCP_AUTH_TOKEN`을 설정하면 `Authorization: Bearer <토큰>` 헤더가 없는 요청은 401로 거부됩니다.
인터넷에 공개 배포할 때는 반드시 설정하세요 (공공 API 일일 호출 한도 보호).
OAuth가 필요해지면 FastMCP의 `auth=` 에 OAuth Provider로 교체하면 됩니다.

## 클라이언트 연결 예시

**Claude Code**
```bash
claude mcp add --transport http kma-weather https://<your-host>/mcp \
  --header "Authorization: Bearer <토큰>"
```

**Python (FastMCP Client)**
```python
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport("https://<your-host>/mcp",
                                    headers={"Authorization": "Bearer <토큰>"})
async with Client(transport) as c:
    print(await c.call_tool("get_forecast", {"location": "화성시 동탄", "type": "daily"}))
```

Claude.ai 커스텀 커넥터처럼 OAuth만 받는 클라이언트는 고정 Bearer 토큰을 넣을 수 없으므로,
그 경우 토큰 없이 띄우거나 OAuth Provider를 붙여야 합니다.

## 동작 메모

- **발표시각 자동 선택**: 초단기실황은 매시 정시, 단기예보는 02·05·08·11·14·17·20·23시 발표분 중 최신을 쓰고,
  아직 자료가 없으면(결과코드 03) 직전 발표분으로 자동 재시도합니다.
- **캐시**: 실황 10분, 예보 30분, 대기질 15분, 측정소 목록 7일, 지명 24시간 (인메모리).
- **체감온도**: 기상청 공식 — 5~9월은 습구온도 기반 여름철 공식, 그 외에는 바람냉각(기온 10°C 이하, 풍속 1.3m/s 이상).
- **대기질**: 가장 가까운 측정소가 점검 중(`-`)이면 다음 측정소로 넘어갑니다.
- **지명 검색**: `countrycodes=kr`로 국내만 검색합니다. 행정구역(시·군·구·동)은 `"경기도 화성시"`처럼 전체 주소를, 장소(역·산·공항 등)는 장소명을 `name`으로 돌려줍니다.
- 국내 범위(위도 32~39.5, 경도 124~132) 밖의 좌표는 오류를 돌려줍니다.

## 테스트

```bash
pytest -q   # 외부 API는 respx로 목킹
```

## 다음 단계 후보

- 중기예보(4~10일), 생활기상지수(자외선 등), 일출·일몰
- Redis 캐시 (다중 인스턴스 배포 시)
