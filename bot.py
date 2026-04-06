#!/usr/bin/env python3
"""Telegram bot for managing stock portfolio - supports buy/sell commands and natural language via Claude."""

import json
import logging
import time
import requests
import anthropic
import yfinance as yf
from pykrx import stock as pykrx_stock
from ddgs import DDGS
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timedelta
from config_loader import load_config, save_config, DATA_DIR
import kis_api

LOG_PATH = Path(__file__).parent / "bot.log"
HISTORY_PATH = DATA_DIR / "chat_history.json"
PRICE_CACHE_PATH = DATA_DIR / "price_cache.json"

MAX_HISTORY = 20       # 최대 저장 메시지 수
PRICE_CACHE_TTL = 300  # 시세 캐시 유효시간 (초, 5분)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
)


def send_message(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Telegram has 4096 char limit, split if needed
    while text:
        chunk = text[:4000]
        text = text[4000:]
        requests.post(url, json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"})


def load_history():
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return []


def save_history(history):
    # 최신 MAX_HISTORY개만 유지
    with open(HISTORY_PATH, "w") as f:
        json.dump(history[-MAX_HISTORY:], f, ensure_ascii=False)


KR_STOCK_MAP = {
    "마이크로투나노": "424980",
}


def get_portfolio_context(config):
    """Build a context string about the user's portfolio for Claude."""
    lines = ["현재 포트폴리오:"]
    lines.append("\n[미국 주식]")
    for s in config["portfolio"]["us_stocks"]:
        lines.append(f"- {s['ticker']}: {s['shares']}주, 평균매수가 ${s['avg_price']}")
    lines.append("\n[한국 주식]")
    for s in config["portfolio"]["kr_stocks"]:
        name = s.get("name", s["ticker"])
        lines.append(f"- {name}({s['ticker']}): {s['shares']}주, 평균매수가 {s['avg_price']}원")
    return "\n".join(lines)


def _load_price_cache():
    if PRICE_CACHE_PATH.exists():
        with open(PRICE_CACHE_PATH) as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) < PRICE_CACHE_TTL:
            return data.get("prices", "")
    return None


def _save_price_cache(text):
    with open(PRICE_CACHE_PATH, "w") as f:
        json.dump({"ts": time.time(), "prices": text}, f)


def _fetch_us_price(s):
    try:
        hist = yf.Ticker(s["ticker"]).history(period="2d")
        if len(hist) >= 2:
            price, prev = hist["Close"].iloc[-1], hist["Close"].iloc[-2]
            pct = (price - prev) / prev * 100
            return f"- {s['ticker']}: ${price:.2f} ({pct:+.2f}%)"
        elif len(hist) == 1:
            return f"- {s['ticker']}: ${hist['Close'].iloc[-1]:.2f}"
    except Exception:
        pass
    return None


def _fetch_kr_price(s):
    try:
        today = datetime.now()
        df = pykrx_stock.get_market_ohlcv(
            (today - timedelta(days=10)).strftime("%Y%m%d"),
            today.strftime("%Y%m%d"), s["ticker"]
        )
        if len(df) >= 2:
            price, prev = int(df["종가"].iloc[-1]), int(df["종가"].iloc[-2])
            pct = (price - prev) / prev * 100
            return f"- {s.get('name', s['ticker'])}: {price:,}원 ({pct:+.2f}%)"
        elif len(df) == 1:
            return f"- {s.get('name', s['ticker'])}: {int(df['종가'].iloc[-1]):,}원"
    except Exception:
        pass
    return None


def get_live_prices(config):
    """Fetch live prices in parallel with 5-min cache."""
    cached = _load_price_cache()
    if cached:
        return cached

    lines = ["\n현재 시세:"]
    us_stocks = config["portfolio"]["us_stocks"]
    kr_stocks = config["portfolio"]["kr_stocks"]

    # 병렬 조회
    with ThreadPoolExecutor(max_workers=8) as ex:
        us_futures = {ex.submit(_fetch_us_price, s): s for s in us_stocks}
        kr_futures = {ex.submit(_fetch_kr_price, s): s for s in kr_stocks}
        extra_futures = {
            ex.submit(lambda: yf.Ticker("USDKRW=X").history(period="1d")): "fx",
        }
        for fut in as_completed(us_futures):
            r = fut.result()
            if r:
                lines.append(r)
        for fut in as_completed(kr_futures):
            r = fut.result()
            if r:
                lines.append(r)

    # 환율
    try:
        hist = yf.Ticker("USDKRW=X").history(period="1d")
        if len(hist) >= 1:
            lines.append(f"\n환율: 1 USD = {hist['Close'].iloc[-1]:,.2f} KRW")
    except Exception:
        pass

    # 주요 지수
    index_map = {"S&P500": "^GSPC", "NASDAQ": "^IXIC", "KOSPI": "^KS11", "KOSDAQ": "^KQ11"}
    lines.append("\n주요 지수:")
    with ThreadPoolExecutor(max_workers=4) as ex:
        def fetch_idx(name_sym):
            name, sym = name_sym
            try:
                hist = yf.Ticker(sym).history(period="2d")
                if len(hist) >= 2:
                    curr, prev = hist["Close"].iloc[-1], hist["Close"].iloc[-2]
                    pct = (curr - prev) / prev * 100
                    return f"- {name}: {curr:,.2f} ({pct:+.2f}%)"
            except Exception:
                pass
            return None
        for r in ex.map(fetch_idx, index_map.items()):
            if r:
                lines.append(r)

    result = "\n".join(lines)
    _save_price_cache(result)
    return result


# ──────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────

def _tavily_key():
    return load_config().get("tavily_api_key", "")


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo."""
    try:
        tavily_key = _tavily_key()
        if tavily_key:
            from tavily import TavilyClient
            client = TavilyClient(api_key=tavily_key)
            resp = client.search(query, max_results=max_results)
            lines = []
            for r in resp.get("results", []):
                lines.append(f"제목: {r.get('title', '')}")
                lines.append(f"내용: {r.get('content', '')}")
                lines.append(f"출처: {r.get('url', '')}")
                lines.append("")
            return "\n".join(lines) or "결과 없음"

        # fallback: DuckDuckGo
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "검색 결과가 없습니다."
        lines = []
        for r in results:
            lines.append(f"제목: {r.get('title', '')}")
            lines.append(f"내용: {r.get('body', '')}")
            lines.append(f"출처: {r.get('href', '')}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"검색 오류: {e}"


def news_search(query: str, max_results: int = 5) -> str:
    """Search latest news. Tries Tavily first, falls back to DuckDuckGo."""
    try:
        tavily_key = _tavily_key()
        if tavily_key:
            from tavily import TavilyClient
            client = TavilyClient(api_key=tavily_key)
            resp = client.search(query, max_results=max_results, topic="news")
            lines = []
            for r in resp.get("results", []):
                pub = r.get("published_date", "")
                lines.append(f"[{pub}] {r.get('title', '')}")
                lines.append(f"내용: {r.get('content', '')}")
                lines.append(f"출처: {r.get('url', '')}")
                lines.append("")
            return "\n".join(lines) or "결과 없음"

        with DDGS() as ddgs:
            results = list(ddgs.news(query, max_results=max_results))
        if not results:
            return "뉴스 결과가 없습니다."
        lines = []
        for r in results:
            lines.append(f"[{r.get('date', '')}] {r.get('title', '')}")
            lines.append(f"내용: {r.get('body', '')}")
            lines.append(f"출처: {r.get('source', '')} - {r.get('url', '')}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"뉴스 검색 오류: {e}"


def fetch_url(url: str) -> str:
    """Fetch and parse a webpage — returns clean text (max 3000 chars)."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # 연속 빈줄 제거
        lines = [l for l in text.splitlines() if l.strip()]
        return "\n".join(lines)[:3000]
    except Exception as e:
        return f"URL 읽기 오류: {e}"


def get_stock_info(ticker: str) -> str:
    """Return fundamentals: PER, EPS, 목표가, 애널리스트 의견, 52주 고/저."""
    try:
        t = yf.Ticker(ticker)
        info = t.info
        lines = [f"[{ticker} 펀더멘털]"]
        fields = {
            "현재가": "currentPrice",
            "시가총액": "marketCap",
            "PER": "trailingPE",
            "선행PER": "forwardPE",
            "PBR": "priceToBook",
            "EPS(TTM)": "trailingEps",
            "배당수익률": "dividendYield",
            "52주 최고": "fiftyTwoWeekHigh",
            "52주 최저": "fiftyTwoWeekLow",
            "애널리스트 평균목표가": "targetMeanPrice",
            "애널리스트 의견": "recommendationKey",
            "매출(TTM)": "totalRevenue",
            "영업이익률": "operatingMargins",
        }
        for label, key in fields.items():
            val = info.get(key)
            if val is not None:
                if key == "marketCap" or key == "totalRevenue":
                    val = f"${val:,.0f}"
                elif key == "dividendYield" and val:
                    val = f"{val*100:.2f}%"
                elif key == "operatingMargins" and val:
                    val = f"{val*100:.2f}%"
                lines.append(f"{label}: {val}")

        # 애널리스트 추천 분포
        try:
            rec = t.recommendations
            if rec is not None and not rec.empty:
                latest = rec.tail(1).to_dict("records")[0]
                lines.append(f"추천 분포 - 강매수:{latest.get('strongBuy',0)} 매수:{latest.get('buy',0)} 중립:{latest.get('hold',0)} 매도:{latest.get('sell',0)}")
        except Exception:
            pass

        return "\n".join(lines)
    except Exception as e:
        return f"종목 정보 오류: {e}"


def get_earnings_calendar(ticker: str) -> str:
    """Return upcoming / recent earnings info for a ticker."""
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar
        lines = [f"[{ticker} 실적 캘린더]"]
        if cal:
            if isinstance(cal, dict):
                for k, v in cal.items():
                    lines.append(f"{k}: {v}")
            else:
                lines.append(str(cal))
        else:
            lines.append("실적 일정 정보 없음")

        # 최근 실적
        try:
            earnings = t.earnings_history
            if earnings is not None and not earnings.empty:
                lines.append("\n[최근 실적]")
                for _, row in earnings.tail(4).iterrows():
                    lines.append(
                        f"  {row.name if hasattr(row, 'name') else ''} "
                        f"예상EPS: {row.get('epsEstimate', 'N/A')} "
                        f"실제EPS: {row.get('epsActual', 'N/A')} "
                        f"서프라이즈: {row.get('surprisePercent', 'N/A')}%"
                    )
        except Exception:
            pass

        return "\n".join(lines)
    except Exception as e:
        return f"실적 캘린더 오류: {e}"


def get_fear_greed() -> str:
    """Fetch CNN Fear & Greed Index."""
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=8)
        data = resp.json()
        fg = data.get("fear_and_greed", {})
        score = fg.get("score", "N/A")
        rating = fg.get("rating", "N/A")
        prev = fg.get("previous_close", "N/A")
        week = fg.get("previous_1_week", "N/A")
        return (
            f"[CNN 공포/탐욕 지수]\n"
            f"현재: {score:.1f} ({rating})\n"
            f"전일: {prev}\n"
            f"1주전: {week}"
        )
    except Exception as e:
        return f"공포/탐욕 지수 오류: {e}"


def get_macro_data() -> str:
    """Fetch key macro indicators: US10Y, DXY, VIX, Gold, Oil."""
    tickers = {
        "미국10년물금리": "^TNX",
        "달러인덱스(DXY)": "DX-Y.NYB",
        "VIX(공포지수)": "^VIX",
        "금(Gold)": "GC=F",
        "WTI원유": "CL=F",
        "S&P500": "^GSPC",
        "나스닥": "^IXIC",
    }
    lines = ["[주요 매크로 지표]"]
    for name, sym in tickers.items():
        try:
            hist = yf.Ticker(sym).history(period="2d")
            if len(hist) >= 2:
                curr = hist["Close"].iloc[-1]
                prev = hist["Close"].iloc[-2]
                pct = (curr - prev) / prev * 100
                lines.append(f"{name}: {curr:.2f} ({pct:+.2f}%)")
            elif len(hist) == 1:
                lines.append(f"{name}: {hist['Close'].iloc[-1]:.2f}")
        except Exception:
            pass
    return "\n".join(lines)


def get_insider_transactions(ticker: str) -> str:
    """최근 내부자 거래 (임원·대주주 매수/매도)."""
    try:
        t = yf.Ticker(ticker)
        df = t.insider_transactions
        if df is None or df.empty:
            return f"{ticker} 내부자 거래 데이터 없음"
        lines = [f"[{ticker} 최근 내부자 거래]"]
        for _, row in df.head(8).iterrows():
            date = str(row.get("Start Date", ""))[:10]
            name = row.get("Name", "")
            title = row.get("Position", "")
            shares = row.get("Shares", 0)
            value = row.get("Value", 0)
            txn = row.get("Transaction", "")
            lines.append(f"{date} | {name}({title}) | {txn} | {shares:,}주 | ${value:,.0f}")
        return "\n".join(lines)
    except Exception as e:
        return f"내부자 거래 조회 오류: {e}"


def get_institutional_holders(ticker: str) -> str:
    """주요 기관 보유 현황 (Vanguard, BlackRock 등)."""
    try:
        t = yf.Ticker(ticker)
        df = t.institutional_holders
        if df is None or df.empty:
            return f"{ticker} 기관 보유 데이터 없음"
        lines = [f"[{ticker} 주요 기관 보유 현황]"]
        for _, row in df.head(10).iterrows():
            holder = row.get("Holder", "")
            shares = row.get("Shares", 0)
            value = row.get("Value", 0)
            pct = row.get("% Out", 0)
            chg = row.get("pctChange", 0)
            lines.append(f"{holder}: {shares:,}주 (지분 {pct:.2%}, 전분기비 {chg:+.2%})")
        return "\n".join(lines)
    except Exception as e:
        return f"기관 보유 조회 오류: {e}"


def get_upgrades_downgrades(ticker: str) -> str:
    """최근 애널리스트 등급 변경 (upgrade/downgrade)."""
    try:
        t = yf.Ticker(ticker)
        df = t.upgrades_downgrades
        if df is None or df.empty:
            return f"{ticker} 등급 변경 데이터 없음"
        lines = [f"[{ticker} 최근 애널리스트 등급 변경]"]
        for date, row in df.head(8).iterrows():
            firm = row.get("Firm", "")
            to_grade = row.get("ToGrade", "")
            from_grade = row.get("FromGrade", "")
            action = row.get("Action", "")
            target = row.get("currentPriceTarget", "")
            prior = row.get("priorPriceTarget", "")
            date_str = str(date)[:10]
            target_str = f" | 목표가: ${prior}→${target}" if target else ""
            lines.append(f"{date_str} | {firm} | {from_grade}→{to_grade} ({action}){target_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"등급 변경 조회 오류: {e}"


def get_financials(ticker: str) -> str:
    """최근 4분기 재무제표 요약 (매출, 영업이익, 순이익, FCF)."""
    try:
        t = yf.Ticker(ticker)
        lines = [f"[{ticker} 재무제표 요약]"]

        # 손익계산서
        inc = t.quarterly_income_stmt
        if inc is not None and not inc.empty:
            lines.append("\n<손익계산서 (분기)>")
            rows_to_show = ["Total Revenue", "Operating Income", "Net Income"]
            for row_name in rows_to_show:
                if row_name in inc.index:
                    row = inc.loc[row_name].head(4)
                    vals = " | ".join([f"{v/1e9:.2f}B" if abs(v) >= 1e9 else f"{v/1e6:.0f}M" for v in row])
                    lines.append(f"  {row_name}: {vals}")

        # 현금흐름
        cf = t.quarterly_cashflow
        if cf is not None and not cf.empty:
            lines.append("\n<현금흐름 (분기)>")
            cf_rows = ["Free Cash Flow", "Operating Cash Flow"]
            for row_name in cf_rows:
                if row_name in cf.index:
                    row = cf.loc[row_name].head(4)
                    vals = " | ".join([f"{v/1e9:.2f}B" if abs(v) >= 1e9 else f"{v/1e6:.0f}M" for v in row])
                    lines.append(f"  {row_name}: {vals}")

        return "\n".join(lines)
    except Exception as e:
        return f"재무제표 조회 오류: {e}"


def get_options_summary(ticker: str) -> str:
    """가장 가까운 만기의 옵션 체인 요약 (Put/Call ratio, 주요 행사가)."""
    try:
        t = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            return f"{ticker} 옵션 데이터 없음"

        # 가장 가까운 만기
        exp = expirations[0]
        chain = t.option_chain(exp)
        calls = chain.calls
        puts = chain.puts

        # 현재가 기준 ATM 옵션
        info = t.fast_info
        current = getattr(info, "last_price", None)

        lines = [f"[{ticker} 옵션 요약 - 만기: {exp}]"]
        if current:
            lines.append(f"현재가: ${current:.2f}")

        # Put/Call ratio (OI 기준)
        total_call_oi = calls["openInterest"].sum()
        total_put_oi = puts["openInterest"].sum()
        pc_ratio = total_put_oi / total_call_oi if total_call_oi > 0 else 0
        lines.append(f"Put/Call OI 비율: {pc_ratio:.2f} (1 초과 = 풋 우세 = 하락 헤지 많음)")

        # 콜 상위 5개 (OI 기준)
        top_calls = calls.nlargest(5, "openInterest")[["strike", "lastPrice", "openInterest", "impliedVolatility"]]
        lines.append("\n콜 상위 OI:")
        for _, r in top_calls.iterrows():
            lines.append(f"  행사가 ${r['strike']:.0f} | 프리미엄 ${r['lastPrice']:.2f} | OI {r['openInterest']:,} | IV {r['impliedVolatility']:.1%}")

        # 풋 상위 5개 (OI 기준)
        top_puts = puts.nlargest(5, "openInterest")[["strike", "lastPrice", "openInterest", "impliedVolatility"]]
        lines.append("\n풋 상위 OI:")
        for _, r in top_puts.iterrows():
            lines.append(f"  행사가 ${r['strike']:.0f} | 프리미엄 ${r['lastPrice']:.2f} | OI {r['openInterest']:,} | IV {r['impliedVolatility']:.1%}")

        return "\n".join(lines)
    except Exception as e:
        return f"옵션 조회 오류: {e}"


def get_dividend_history(ticker: str) -> str:
    """배당 이력 및 배당률."""
    try:
        t = yf.Ticker(ticker)
        divs = t.dividends
        if divs is None or divs.empty:
            return f"{ticker} 배당 데이터 없음 (무배당 종목)"
        info = t.info
        lines = [f"[{ticker} 배당 정보]"]
        lines.append(f"배당수익률: {info.get('dividendYield', 0)*100:.2f}%")
        lines.append(f"연간 배당금: ${info.get('dividendRate', 'N/A')}")
        lines.append(f"배당성향: {info.get('payoutRatio', 0)*100:.1f}%")
        lines.append("\n최근 배당 이력:")
        for date, amount in divs.tail(8).items():
            lines.append(f"  {str(date)[:10]}: ${amount:.4f}")
        return "\n".join(lines)
    except Exception as e:
        return f"배당 조회 오류: {e}"


def get_ticker_news(ticker: str) -> str:
    """yfinance에서 종목 전용 최신 뉴스 가져오기."""
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
        if not news:
            return f"{ticker} 뉴스 없음"
        lines = [f"[{ticker} 최신 뉴스]"]
        for n in news[:8]:
            content = n.get("content", {})
            if isinstance(content, dict):
                title = content.get("title", "")
                pub = content.get("pubDate", "")[:10]
                summary = content.get("summary", "")
            else:
                title = n.get("title", "")
                pub = ""
                summary = ""
            if title:
                lines.append(f"[{pub}] {title}")
                if summary:
                    lines.append(f"  → {summary[:120]}")
        return "\n".join(lines)
    except Exception as e:
        return f"뉴스 조회 오류: {e}"


# ──────────────────────────────────────────────
# Claude tool definitions
# ──────────────────────────────────────────────

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "실시간 웹 검색. 기업 정보, 시황, 경제 지표 등을 찾을 때 사용. "
            "Tavily(설정 시) > DuckDuckGo 순으로 자동 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어 (구체적일수록 정확). 영어 권장."},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "news_search",
        "description": "최신 뉴스 검색. 종목 뉴스, 정책 뉴스, 시장 이슈 등. 영어로 검색하면 더 많은 결과.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "뉴스 검색어"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": "특정 URL의 본문 내용을 읽어옴. 뉴스 기사 전문, 공식 발표문 등 확인할 때 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "읽을 URL"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "get_stock_info",
        "description": "종목 펀더멘털 조회: PER, EPS, 52주 고/저, 애널리스트 목표가·의견, 배당 등.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "미국 주식 티커 (예: AAPL, MSFT)"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_earnings_calendar",
        "description": "종목의 실적 발표 일정 및 최근 어닝 서프라이즈 확인.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "미국 주식 티커"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_fear_greed",
        "description": "CNN 공포/탐욕 지수 조회. 시장 심리 파악에 사용.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_macro_data",
        "description": "미국 10년물 금리, DXY, VIX, 금, 원유, S&P500, 나스닥 등 주요 매크로 지표 조회.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_insider_transactions",
        "description": "임원·대주주 내부자 매수/매도 내역. 스마트머니 흐름 파악에 유용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "미국 주식 티커"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_institutional_holders",
        "description": "Vanguard, BlackRock 등 주요 기관 보유 지분 및 전분기 대비 변화.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "미국 주식 티커"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_upgrades_downgrades",
        "description": "최근 애널리스트 등급 변경 이력 (업그레이드/다운그레이드, 목표주가 변경).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "미국 주식 티커"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_financials",
        "description": "분기별 재무제표 요약: 매출, 영업이익, 순이익, FCF. 실적 추이 파악에 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "미국 주식 티커"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_options_summary",
        "description": "옵션 체인 요약: Put/Call ratio, 주요 행사가별 OI. 시장 기대 방향 파악에 유용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "미국 주식 티커"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_dividend_history",
        "description": "배당 이력, 배당수익률, 배당성향 조회.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "미국 주식 티커"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_ticker_news",
        "description": "yfinance 종목 전용 최신 뉴스 (제목+요약). 특정 종목 뉴스를 빠르게 볼 때.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "미국 주식 티커"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_account_balance",
        "description": "한국투자증권 계좌 실제 잔고 조회. 국내/해외 보유 종목, 평가금액, 손익 확인.",
        "input_schema": {
            "type": "object",
            "properties": {
                "market": {
                    "type": "string",
                    "enum": ["all", "kr", "us"],
                    "description": "all=전체, kr=국내만, us=해외만",
                },
            },
            "required": [],
        },
    },
]


def run_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return the result."""
    if tool_name == "web_search":
        return web_search(tool_input["query"], tool_input.get("max_results", 5))
    elif tool_name == "news_search":
        return news_search(tool_input["query"], tool_input.get("max_results", 5))
    elif tool_name == "fetch_url":
        return fetch_url(tool_input["url"])
    elif tool_name == "get_stock_info":
        return get_stock_info(tool_input["ticker"])
    elif tool_name == "get_earnings_calendar":
        return get_earnings_calendar(tool_input["ticker"])
    elif tool_name == "get_fear_greed":
        return get_fear_greed()
    elif tool_name == "get_macro_data":
        return get_macro_data()
    elif tool_name == "get_insider_transactions":
        return get_insider_transactions(tool_input["ticker"])
    elif tool_name == "get_institutional_holders":
        return get_institutional_holders(tool_input["ticker"])
    elif tool_name == "get_upgrades_downgrades":
        return get_upgrades_downgrades(tool_input["ticker"])
    elif tool_name == "get_financials":
        return get_financials(tool_input["ticker"])
    elif tool_name == "get_options_summary":
        return get_options_summary(tool_input["ticker"])
    elif tool_name == "get_dividend_history":
        return get_dividend_history(tool_input["ticker"])
    elif tool_name == "get_ticker_news":
        return get_ticker_news(tool_input["ticker"])
    elif tool_name == "get_account_balance":
        market = tool_input.get("market", "all")
        if market == "kr":
            return kis_api.get_kr_balance()
        elif market == "us":
            return kis_api.get_us_balance()
        else:
            return kis_api.get_full_balance()
    return "알 수 없는 도구입니다."


def ask_claude(question, config):
    """Send a natural language question to Claude with tool use support for real-time search."""
    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])

    portfolio_context = get_portfolio_context(config)
    live_prices = get_live_prices(config)

    system_prompt = f"""너는 개인 주식 투자 어시스턴트야. 사용자의 포트폴리오 정보와 실시간 시세를 바탕으로 질문에 답해줘.
항상 한국어로 답하고, 간결하게 핵심만 말해줘. 텔레그램 메시지이므로 너무 길지 않게.
이전 대화 내용을 기억하고 맥락을 이어서 답해줘.

## 도구 사용 지침
정확한 답변을 위해 도구를 적극적으로 사용해:

- **최신 뉴스/이슈** → news_search (영어로 검색하면 더 풍부한 결과)
- **기업·시황 정보** → web_search
- **기사 전문 확인** → fetch_url (검색에서 찾은 URL을 직접 읽기)
- **종목 최신 뉴스** → get_ticker_news
- **PER·EPS·목표가** → get_stock_info
- **분기 재무제표** → get_financials
- **실적 발표 일정** → get_earnings_calendar
- **애널리스트 등급 변경** → get_upgrades_downgrades
- **내부자 매수/매도** → get_insider_transactions
- **기관 보유 현황** → get_institutional_holders
- **옵션 Put/Call 비율** → get_options_summary
- **배당 이력** → get_dividend_history
- **시장 심리 파악** → get_fear_greed
- **금리·VIX·달러·금·원유** → get_macro_data

여러 도구를 순서대로 조합해서 깊이 있는 답변을 줘.
예) get_ticker_news → get_upgrades_downgrades → get_financials → 종합 의견

투자 조언 시 "개인적인 의견이며 투자 판단은 본인 책임"이라는 점을 명시해.

{portfolio_context}

{live_prices}

오늘 날짜: {datetime.now().strftime('%Y-%m-%d %H:%M')}"""

    history = load_history()
    history.append({"role": "user", "content": question})

    def call_claude(msgs, retries=3):
        for attempt in range(retries):
            try:
                return client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=1024,
                    system=system_prompt,
                    tools=TOOLS,
                    messages=msgs,
                )
            except anthropic.APIConnectionError:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise

    # Agentic loop - Claude can call tools multiple times
    messages = history.copy()
    while True:
        response = call_claude(messages)

        # If Claude wants to use a tool
        if response.stop_reason == "tool_use":
            # Add Claude's response (with tool calls) to messages
            messages.append({"role": "assistant", "content": response.content})

            # Execute each tool call and collect results
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logging.info(f"Tool call: {block.name}({block.input})")
                    result = run_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            # Feed results back to Claude
            messages.append({"role": "user", "content": tool_results})

        else:
            # Final answer
            answer = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            # Save to history (user question + final answer only)
            history.append({"role": "assistant", "content": answer})
            save_history(history)
            return answer


def handle_balance():
    """한투 실제 계좌 잔고 조회."""
    if not kis_api.is_configured():
        return "❌ 한국투자증권 API가 설정되지 않았습니다."
    return kis_api.get_full_balance()


def handle_buy(args, config):
    if len(args) < 3:
        return (
            "사용법: /buy &lt;종목&gt; &lt;수량&gt; &lt;매수가&gt; [한국종목명] [실주문]\n"
            "예: /buy AAPL 10 150.5\n"
            "예: /buy 424980 44 14340 마이크로투나노\n"
            "실제 주문: /buy AAPL 10 150.5 실주문"
        )

    ticker = args[0].upper()
    try:
        shares = float(args[1])
        price = float(args[2])
    except ValueError:
        return "수량과 가격은 숫자로 입력해주세요."

    # "실주문" 키워드가 있으면 KIS API로 실제 주문
    real_order = "실주문" in args

    kr_name = None
    for a in args[3:]:
        if a != "실주문":
            kr_name = a
            break

    is_kr = ticker.isdigit() or kr_name is not None

    # KIS 실제 주문 실행
    if real_order and kis_api.is_configured():
        if is_kr:
            result = kis_api.place_kr_order(ticker, "buy", int(shares), int(price))
        else:
            result = kis_api.place_us_order(ticker, "buy", int(shares), price)
        # 포트폴리오에도 기록
        portfolio_result = _update_portfolio_buy(ticker, shares, price, kr_name, is_kr, config)
        save_config(config)
        return f"{result}\n\n{portfolio_result}"

    if is_kr:
        stock_list = config["portfolio"]["kr_stocks"]
        existing = next((s for s in stock_list if s["ticker"] == ticker), None)
        if existing:
            total_cost = existing["avg_price"] * existing["shares"] + price * shares
            total_shares = existing["shares"] + shares
            existing["avg_price"] = round(total_cost / total_shares)
            existing["shares"] = total_shares
            if kr_name:
                existing["name"] = kr_name
        else:
            stock_list.append({
                "ticker": ticker, "name": kr_name or ticker,
                "shares": shares, "avg_price": price, "currency": "KRW",
            })
        display_name = kr_name or ticker
        return f"<b>매수 기록 완료</b>\n{display_name} {shares}주 @ {price:,.0f}원"
    else:
        stock_list = config["portfolio"]["us_stocks"]
        existing = next((s for s in stock_list if s["ticker"] == ticker), None)
        if existing:
            total_cost = existing["avg_price"] * existing["shares"] + price * shares
            total_shares = existing["shares"] + shares
            existing["avg_price"] = round(total_cost / total_shares, 2)
            existing["shares"] = total_shares
        else:
            stock_list.append({
                "ticker": ticker, "shares": shares,
                "avg_price": price, "currency": "USD",
            })
        return f"<b>매수 기록 완료</b>\n{ticker} {shares}주 @ ${price:,.2f}"


def _update_portfolio_buy(ticker, shares, price, kr_name, is_kr, config):
    """포트폴리오에 매수 기록 (KIS 주문 후 호출)."""
    if is_kr:
        stock_list = config["portfolio"]["kr_stocks"]
        existing = next((s for s in stock_list if s["ticker"] == ticker), None)
        if existing:
            total_cost = existing["avg_price"] * existing["shares"] + price * shares
            total_shares = existing["shares"] + shares
            existing["avg_price"] = round(total_cost / total_shares)
            existing["shares"] = total_shares
            if kr_name:
                existing["name"] = kr_name
        else:
            stock_list.append({"ticker": ticker, "name": kr_name or ticker,
                                "shares": shares, "avg_price": price, "currency": "KRW"})
        return f"포트폴리오 기록 완료: {kr_name or ticker} {shares}주 @ {price:,.0f}원"
    else:
        stock_list = config["portfolio"]["us_stocks"]
        existing = next((s for s in stock_list if s["ticker"] == ticker), None)
        if existing:
            total_cost = existing["avg_price"] * existing["shares"] + price * shares
            total_shares = existing["shares"] + shares
            existing["avg_price"] = round(total_cost / total_shares, 2)
            existing["shares"] = total_shares
        else:
            stock_list.append({"ticker": ticker, "shares": shares,
                                "avg_price": price, "currency": "USD"})
        return f"포트폴리오 기록 완료: {ticker} {shares}주 @ ${price:.2f}"


def handle_sell(args, config):
    if len(args) < 2:
        return (
            "사용법: /sell &lt;종목&gt; &lt;수량&gt; [매도가] [실주문]\n"
            "예: /sell AAPL 5\n"
            "실제 주문: /sell AAPL 5 150.0 실주문"
        )

    ticker = args[0].upper()
    try:
        shares = float(args[1])
    except ValueError:
        return "수량은 숫자로 입력해주세요."

    price = 0.0
    if len(args) > 2:
        try:
            price = float(args[2])
        except ValueError:
            pass

    real_order = "실주문" in args
    is_kr = ticker.isdigit()

    # KIS 실제 주문
    if real_order and kis_api.is_configured():
        if is_kr:
            kis_result = kis_api.place_kr_order(ticker, "sell", int(shares), int(price))
        else:
            kis_result = kis_api.place_us_order(ticker, "sell", int(shares), price)
        # 포트폴리오에서도 제거
        portfolio_result = _update_portfolio_sell(ticker, shares, is_kr, config)
        save_config(config)
        return f"{kis_result}\n\n{portfolio_result}"

    return _update_portfolio_sell(ticker, shares, is_kr, config)


def _update_portfolio_sell(ticker, shares, is_kr, config):
    """포트폴리오에서 매도 기록."""
    stock_list = config["portfolio"]["kr_stocks" if is_kr else "us_stocks"]
    existing = next((s for s in stock_list if s["ticker"] == ticker), None)

    if not existing:
        return f"{ticker} 종목을 보유하고 있지 않습니다."

    if shares >= existing["shares"]:
        stock_list.remove(existing)
        display = existing.get("name", ticker)
        return f"<b>전량 매도 완료</b>\n{display} {existing['shares']}주 전량 매도"
    else:
        existing["shares"] = round(existing["shares"] - shares, 6)
        display = existing.get("name", ticker)
        return f"<b>매도 기록 완료</b>\n{display} {shares}주 매도 (잔여: {existing['shares']}주)"


def handle_portfolio(config):
    lines = ["<b>현재 포트폴리오</b>", ""]
    if config["portfolio"]["us_stocks"]:
        lines.append("<b>US Stocks:</b>")
        for s in config["portfolio"]["us_stocks"]:
            lines.append(f"  {s['ticker']}: {s['shares']}주 @ ${s['avg_price']:,.2f}")
        lines.append("")
    if config["portfolio"]["kr_stocks"]:
        lines.append("<b>KR Stocks:</b>")
        for s in config["portfolio"]["kr_stocks"]:
            name = s.get("name", s["ticker"])
            lines.append(f"  {name}: {s['shares']}주 @ {s['avg_price']:,.0f}원")
    return "\n".join(lines)


def handle_reset():
    save_history([])
    return "대화 기록을 초기화했습니다. 새로운 대화를 시작하세요!"


def handle_help():
    return (
        "<b>사용 가능한 명령어</b>\n\n"
        "/buy &lt;종목&gt; &lt;수량&gt; &lt;매수가&gt; [한국종목명] [실주문]\n"
        "  예: /buy AAPL 10 150.5\n"
        "  예: /buy 424980 44 14340 마이크로투나노\n"
        "  실제주문: /buy AAPL 10 150.5 실주문\n\n"
        "/sell &lt;종목&gt; &lt;수량&gt; [매도가] [실주문]\n"
        "  예: /sell AAPL 5\n"
        "  실제주문: /sell AAPL 5 150.0 실주문\n\n"
        "/portfolio - 포트폴리오 기록 확인\n"
        "/balance - 한투 실제 계좌 잔고 조회\n"
        "/report - 즉시 리포트 받기\n"
        "/reset - 대화 기록 초기화\n"
        "/help - 도움말\n\n"
        "<b>자연어 질문도 가능!</b>\n"
        "예: 내 수익률 어때?\n"
        "예: CORN 전망이 어때?\n"
        "예: 지금 팔아야 할까?"
    )


def process_update(update, config):
    """Process a single Telegram update."""
    msg = update.get("message", {})
    text = msg.get("text", "").strip()
    if not text:
        return None

    # Command handling
    if text.startswith("/"):
        parts = text.split()
        command = parts[0].lower().split("@")[0]
        args = parts[1:]

        if command == "/buy":
            response = handle_buy(args, config)
            save_config(config)
        elif command == "/sell":
            response = handle_sell(args, config)
            save_config(config)
        elif command == "/portfolio":
            response = handle_portfolio(config)
        elif command == "/balance":
            response = handle_balance()
        elif command == "/report":
            from stock_alert import build_message
            response = build_message(config)
        elif command == "/reset":
            response = handle_reset()
        elif command in ("/help", "/start"):
            response = handle_help()
        else:
            response = f"알 수 없는 명령어: {command}\n/help 로 사용법을 확인하세요."
        return response

    # Natural language - send to Claude
    try:
        response = ask_claude(text, config)
        return response
    except Exception as e:
        logging.error(f"Claude API error: {e}")
        return f"AI 응답 오류가 발생했습니다. 잠시 후 다시 시도해주세요."


def poll():
    """Long-polling loop to receive Telegram updates."""
    config = load_config()
    bot_token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]
    offset = None

    print(f"Bot started at {datetime.now()}")
    logging.info("Bot started")

    while True:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=35)
            data = resp.json()

            if not data.get("ok"):
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                config = load_config()
                response = process_update(update, config)
                if response:
                    send_message(bot_token, chat_id, response)
                    logging.info(f"Processed: {update.get('message', {}).get('text', '')}")

        except requests.exceptions.Timeout:
            continue
        except KeyboardInterrupt:
            print("Bot stopped.")
            break
        except Exception as e:
            logging.error(f"Error: {e}")
            continue


if __name__ == "__main__":
    poll()
