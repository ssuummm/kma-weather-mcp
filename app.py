"""Prefect Horizon 엔트리포인트: `app.py:mcp`."""

import sys
from pathlib import Path

# 패키지가 설치되지 않은 환경에서도 src 레이아웃을 import할 수 있게 한다.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from kma_weather_mcp.server import mcp  # noqa: E402

__all__ = ["mcp"]
