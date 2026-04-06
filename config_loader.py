"""Centralized config loader - supports local config.json and Railway env vars."""
import os
import json
from pathlib import Path

# 로컬: 스크립트와 같은 디렉토리
# Railway: 볼륨 마운트 경로 (/data)
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_LOCAL_CONFIG = Path(__file__).parent / "config.json"
_PORTFOLIO_FILE = DATA_DIR / "portfolio.json"


DEFAULT_PORTFOLIO = {
    "holdings": [
        {"ticker": "CORN", "exchange": "US", "avg_price": 18.69, "shares": 22},
        {"ticker": "NTR",  "exchange": "US", "avg_price": 78.65, "shares": 1},
        {"ticker": "VOO",  "exchange": "US", "avg_price": 626.51, "shares": 0.270143},
        {"ticker": "MSFT", "exchange": "US", "avg_price": 443.61, "shares": 2.964648},
        {"ticker": "950160", "exchange": "KR", "name": "마이크로투나노", "avg_price": 14340, "shares": 44}
    ]
}


def load_config() -> dict:
    config = {}

    # 1. 로컬 config.json (로컬 개발용)
    if _LOCAL_CONFIG.exists():
        with open(_LOCAL_CONFIG) as f:
            config = json.load(f)

    # 2. 볼륨의 portfolio.json이 있으면 덮어쓰기 (Railway 배포 시)
    if _PORTFOLIO_FILE.exists():
        with open(_PORTFOLIO_FILE) as f:
            config["portfolio"] = json.load(f)

    # 3. 환경변수 PORTFOLIO_JSON으로 포트폴리오 설정 가능
    portfolio_json = os.environ.get("PORTFOLIO_JSON")
    if portfolio_json:
        try:
            config["portfolio"] = json.loads(portfolio_json)
        except json.JSONDecodeError:
            pass

    # 4. 포트폴리오가 없으면 기본값 사용
    if "portfolio" not in config:
        config["portfolio"] = DEFAULT_PORTFOLIO

    # 5. 환경변수로 시크릿 덮어쓰기 (Railway 환경변수 우선)
    for key, env in [
        ("anthropic_api_key", "ANTHROPIC_API_KEY"),
        ("tavily_api_key",    "TAVILY_API_KEY"),
    ]:
        val = os.environ.get(env)
        if val:
            config[key] = val

    tg = config.setdefault("telegram", {})
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        tg["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        tg["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]

    return config


def save_config(config: dict):
    """포트폴리오만 볼륨에 저장 (시크릿은 저장 안 함)."""
    with open(_PORTFOLIO_FILE, "w") as f:
        json.dump(config["portfolio"], f, indent=2, ensure_ascii=False)
