#!/usr/bin/env python3
"""Telegram bot for managing stock portfolio - supports buy/sell commands and natural language via Claude."""

import json
import logging
import time
import pytz
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

KST = pytz.timezone("Asia/Seoul")

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


REPLY_KEYBOARD = {
    "keyboard": [
        ["📊 리포트", "💼 잔고"],
        ["📰 뉴스브리핑", "📈 시장현황"],
        ["📉 성과차트", "🏆 기여도"],
        ["📐 CAPM 분석", "🔻 MDD"],
        ["🔔 알림목록", "❓ 도움말"],
    ],
    "resize_keyboard": True,
    "persistent": True,
}


def send_message(bot_token, chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Telegram has 4096 char limit, split if needed
    first = True
    while text:
        chunk = text[:4000]
        text = text[4000:]
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
        if first and reply_markup:
            payload["reply_markup"] = reply_markup
        first = False
        requests.post(url, json=payload)


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
    """Build a context string about the user's portfolio for Claude (KIS 실계좌 우선)."""
    if kis_api.is_configured():
        try:
            return "현재 포트폴리오 (한국투자증권 실계좌):\n" + kis_api.get_full_balance()
        except Exception:
            pass

    # KIS 실패 시 수동 기록 fallback
    lines = ["현재 포트폴리오 (수동 기록):"]
    us = config["portfolio"].get("us_stocks", [])
    kr = config["portfolio"].get("kr_stocks", [])
    if us:
        lines.append("\n[미국 주식]")
        for s in us:
            lines.append(f"- {s['ticker']}: {s['shares']}주, 평균매수가 ${s['avg_price']}")
    if kr:
        lines.append("\n[한국 주식]")
        for s in kr:
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
        fi = yf.Ticker(s["ticker"]).fast_info
        price = fi.last_price
        prev = fi.previous_close
        if price and prev:
            pct = (price - prev) / prev * 100
            return f"- {s['ticker']}: ${price:.2f} ({pct:+.2f}%)"
        elif price:
            return f"- {s['ticker']}: ${price:.2f}"
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

    # KIS 실계좌에서 종목 가져오기
    us_stocks = []
    kr_stocks = []
    try:
        if kis_api.is_configured():
            us_raw = kis_api.get_us_balance_raw()
            kr_raw = kis_api.get_kr_balance_raw()
            us_stocks = [{"ticker": h["ticker"]} for h in us_raw.get("holdings", [])]
            kr_stocks = [{"ticker": h["ticker"], "name": h["name"]} for h in kr_raw.get("holdings", [])]
    except Exception:
        pass

    if not us_stocks and not kr_stocks:
        us_stocks = config["portfolio"].get("us_stocks", [])
        kr_stocks = config["portfolio"].get("kr_stocks", [])

    # 병렬 조회
    with ThreadPoolExecutor(max_workers=8) as ex:
        us_futures = {ex.submit(_fetch_us_price, s): s for s in us_stocks}
        kr_futures = {ex.submit(_fetch_kr_price, s): s for s in kr_stocks}
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
        fi = yf.Ticker("USDKRW=X").fast_info
        lines.append(f"\n환율: 1 USD = {fi.last_price:,.2f} KRW")
    except Exception:
        pass

    # 주요 지수
    index_map = {"S&P500": "^GSPC", "NASDAQ": "^IXIC", "KOSPI": "^KS11", "KOSDAQ": "^KQ11"}
    lines.append("\n주요 지수:")
    with ThreadPoolExecutor(max_workers=4) as ex:
        def fetch_idx(name_sym):
            name, sym = name_sym
            try:
                fi = yf.Ticker(sym).fast_info
                curr, prev = fi.last_price, fi.previous_close
                if curr and prev:
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
    """Fetch key macro indicators: US10Y, DXY, VIX, Gold, Oil (real-time)."""
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
            fi = yf.Ticker(sym).fast_info
            curr, prev = fi.last_price, fi.previous_close
            if curr and prev:
                pct = (curr - prev) / prev * 100
                lines.append(f"{name}: {curr:.2f} ({pct:+.2f}%)")
            elif curr:
                lines.append(f"{name}: {curr:.2f}")
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
    {
        "name": "add_price_alert",
        "description": (
            "가격 알림 등록. 사용자가 '~이면 알려줘' 식으로 말할 때 사용. "
            "예: 'NTR $80 넘으면 알려줘' → ticker=NTR, condition=above, price=80"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":    {"type": "string", "description": "미국 주식 티커 (예: NTR, AAPL)"},
                "condition": {"type": "string", "enum": ["above", "below"],
                              "description": "above=이상/초과, below=이하/미만"},
                "price":     {"type": "number", "description": "기준 가격 (USD)"},
            },
            "required": ["ticker", "condition", "price"],
        },
    },
    {
        "name": "list_alerts",
        "description": "등록된 가격 알림 목록과 자동 이벤트 알림 설정을 보여줌.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "remove_alert",
        "description": "특정 번호의 가격 알림 삭제.",
        "input_schema": {
            "type": "object",
            "properties": {
                "alert_id": {"type": "integer", "description": "삭제할 알림 번호"},
            },
            "required": ["alert_id"],
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
            text, _, _ = kis_api.get_us_balance()
            return text
        else:
            return kis_api.get_full_balance()
    elif tool_name == "add_price_alert":
        from alert_manager import add_price_alert
        return add_price_alert(
            tool_input["ticker"],
            tool_input["condition"],
            float(tool_input["price"]),
        )
    elif tool_name == "list_alerts":
        from alert_manager import list_alerts
        return list_alerts()
    elif tool_name == "remove_alert":
        from alert_manager import remove_alert
        return remove_alert(int(tool_input["alert_id"]))
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

오늘 날짜: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST"""

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


def _check_market_open(is_kr: bool) -> str | None:
    """장 개장 여부 확인. 장 외 시간이면 경고 메시지 반환, 아니면 None."""
    import pytz
    now_kst = datetime.now(pytz.timezone("Asia/Seoul"))
    now_et  = datetime.now(pytz.timezone("America/New_York"))

    if is_kr:
        # 한국장: KST 기준 주말/시간 체크
        if now_kst.weekday() >= 5:
            return "⚠️ 오늘은 주말이라 한국 시장이 닫혀있습니다."
        t = now_kst.hour * 60 + now_kst.minute
        if not (9 * 60 <= t <= 15 * 60 + 30):
            return f"⚠️ 한국장 마감 시간입니다. (개장: 09:00~15:30 KST, 현재: {now_kst.strftime('%H:%M')} KST)"
    else:
        # 미국장: ET 기준 주말/시간 체크 (KST 토요일 = ET 금요일 케이스 대응)
        if now_et.weekday() >= 5:
            return "⚠️ 오늘은 주말이라 미국 시장이 닫혀있습니다."
        t = now_et.hour * 60 + now_et.minute
        if not (9 * 60 + 30 <= t <= 16 * 60):
            return f"⚠️ 미국장 마감 시간입니다. (개장: 09:30~16:00 ET, 현재: {now_et.strftime('%H:%M')} ET)"
    return None


def handle_buy(args, config):
    if len(args) < 3:
        return (
            "사용법: /buy &lt;종목&gt; &lt;수량&gt; &lt;매수가&gt; [한국종목명]\n"
            "예: /buy AAPL 10 150.5\n"
            "예: /buy 424980 44 14340 마이크로투나노\n\n"
            "⚠️ KIS 실계좌로 즉시 매수 주문됩니다."
        )

    ticker = args[0].upper()
    try:
        shares = float(args[1])
        price = float(args[2])
    except ValueError:
        return "수량과 가격은 숫자로 입력해주세요."

    kr_name = args[3] if len(args) > 3 else None
    is_kr = ticker.isdigit() or kr_name is not None

    if not kis_api.is_configured():
        return "❌ KIS API가 설정되지 않았습니다."

    warn = _check_market_open(is_kr)
    if warn:
        return warn

    if is_kr:
        return kis_api.place_kr_order(ticker, "buy", int(shares), int(price))
    else:
        return kis_api.place_us_order(ticker, "buy", int(shares), price)


def handle_sell(args, config):
    if len(args) < 2:
        return (
            "사용법: /sell &lt;종목&gt; &lt;수량&gt; [매도가]\n"
            "예: /sell AAPL 5 150.0\n"
            "예: /sell 424980 10\n\n"
            "⚠️ KIS 실계좌로 즉시 매도 주문됩니다."
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

    is_kr = ticker.isdigit()

    if not kis_api.is_configured():
        return "❌ KIS API가 설정되지 않았습니다."

    warn = _check_market_open(is_kr)
    if warn:
        return warn

    if is_kr:
        return kis_api.place_kr_order(ticker, "sell", int(shares), int(price))
    else:
        return kis_api.place_us_order(ticker, "sell", int(shares), price)


def handle_portfolio(config):
    """KIS 실계좌 잔고 우선, 없으면 수동 기록 표시."""
    if kis_api.is_configured():
        kis_api.invalidate_balance_cache()
        return kis_api.get_full_balance()

    lines = ["<b>현재 포트폴리오 (수동 기록)</b>", ""]
    us = config["portfolio"].get("us_stocks", [])
    kr = config["portfolio"].get("kr_stocks", [])
    if us:
        lines.append("<b>US Stocks:</b>")
        for s in us:
            lines.append(f"  {s['ticker']}: {s['shares']}주 @ ${s['avg_price']:,.2f}")
        lines.append("")
    if kr:
        lines.append("<b>KR Stocks:</b>")
        for s in kr:
            name = s.get("name", s["ticker"])
            lines.append(f"  {name}: {s['shares']}주 @ {s['avg_price']:,.0f}원")
    if not us and not kr:
        lines.append("보유 종목 없음")
    return "\n".join(lines)


def handle_reset():
    save_history([])
    return "대화 기록을 초기화했습니다. 새로운 대화를 시작하세요!"


def handle_help():
    return (
        "<b>📱 버튼으로 바로 실행</b>\n"
        "아래 버튼을 탭하면 바로 실행돼요!\n\n"
        "<b>📝 주문 명령어</b>\n"
        "/buy &lt;종목&gt; &lt;수량&gt; &lt;매수가&gt;\n"
        "  예: /buy AAPL 10 150.5\n"
        "  예: /buy 424980 10 15000 마이크로투나노\n\n"
        "/sell &lt;종목&gt; &lt;수량&gt; [매도가]\n"
        "  예: /sell AAPL 5 (시장가)\n"
        "  예: /sell AAPL 5 150.0 (지정가)\n\n"
        "/reset - 대화 기록 초기화\n\n"
        "<b>💬 자연어 질문도 가능!</b>\n"
        "예: 내 수익률 어때?\n"
        "예: CORN 전망이 어때?\n"
        "예: 지금 팔아야 할까?"
    )


# 하단 버튼 → 명령어 매핑
_BUTTON_MAP = {
    "📊 리포트":    "/report",
    "💼 잔고":      "/portfolio",
    "📰 뉴스브리핑": "__premarket__",
    "📈 시장현황":   "__macro__",
    "📉 성과차트":  "__performance__",
    "🏆 기여도":    "/contrib",
    "📐 CAPM 분석": "/capm",
    "🔻 MDD":       "/mdd",
    "🔔 알림목록":  "__alerts__",
    "❓ 도움말":    "/help",
}


def process_update(update, config):
    """Process a single Telegram update."""
    msg = update.get("message", {})
    text = msg.get("text", "").strip()
    if not text:
        return None

    # 버튼 텍스트 → 명령어 변환
    text = _BUTTON_MAP.get(text, text)

    # 버튼 전용 핸들러
    if text == "__premarket__":
        from premarket_alert import build_premarket_briefing
        return build_premarket_briefing(config)
    if text == "__macro__":
        return get_macro_data()
    if text == "__performance__":
        from portfolio_tracker import send_chart_telegram, get_performance_summary
        bot_token = config["telegram"]["bot_token"]
        chat_id   = config["telegram"]["chat_id"]
        send_chart_telegram(bot_token, chat_id, days=30)
        return get_performance_summary(days=30)
    if text == "__alerts__":
        from alert_manager import list_alerts
        return list_alerts()

    # Command handling
    if text.startswith("/"):
        parts = text.split()
        command = parts[0].lower().split("@")[0]
        args = parts[1:]

        if command == "/buy":
            response = handle_buy(args, config)
        elif command == "/sell":
            response = handle_sell(args, config)
        elif command in ("/portfolio", "/balance"):
            response = handle_portfolio(config)
        elif command == "/report":
            from stock_alert import build_message
            kis_api.invalidate_balance_cache()
            _save_price_cache("")
            response = build_message(config)
        elif command == "/mdd":
            from portfolio_tracker import calc_mdd
            days_arg = 365
            if args:
                try:
                    days_arg = int(args[0])
                except ValueError:
                    pass
            mdd = calc_mdd(days=days_arg)
            if not mdd:
                response = "📉 MDD 계산에 필요한 데이터가 부족해요. (최소 2일치 스냅샷 필요)"
            else:
                mdd_em = "🔴" if mdd["mdd_pct"] > 20 else ("🟡" if mdd["mdd_pct"] > 10 else "🟢")
                rec = mdd["recovery_pct"]
                rec_em = "✅" if rec >= 100 else ("🔄" if rec > 0 else "⏳")
                response = (
                    f"📉 <b>최대낙폭 분석 (최근 {days_arg}일)</b>\n\n"
                    f"{mdd_em} <b>MDD: -{mdd['mdd_pct']:.2f}%</b>\n\n"
                    f"📈 고점: {mdd['peak_date']}\n"
                    f"   {mdd['peak_val']/1e4:,.0f}만원\n\n"
                    f"📉 저점: {mdd['trough_date']}\n"
                    f"   {mdd['trough_val']/1e4:,.0f}만원\n\n"
                    f"💰 현재: {mdd['curr_val']/1e4:,.0f}만원\n\n"
                    f"{rec_em} 낙폭 회복률: <b>{rec:.0f}%</b>\n"
                    f"<i>/mdd 90  →  90일 기준으로 분석</i>"
                )
        elif command == "/contrib":
            from portfolio_tracker import calc_stock_contribution
            days_arg = 30
            if args:
                try:
                    days_arg = int(args[0])
                except ValueError:
                    pass
            contribs = calc_stock_contribution(days=days_arg)
            if not contribs:
                response = "🏆 종목별 기여도 계산에 필요한 데이터가 부족해요."
            else:
                lines = [f"🏆 <b>종목별 기여도 (최근 {days_arg}일)</b>\n"]
                for c in contribs:
                    sign = "+" if c["contrib_pct"] >= 0 else ""
                    em   = "📈" if c["contrib_pct"] >= 0 else "📉"
                    lines.append(
                        f"{em} <b>{c['ticker']}</b>\n"
                        f"   기여도: {sign}{c['contrib_pct']:.2f}%p\n"
                        f"   수익률: {c['ret_pct']:+.1f}%  |  비중: {c['weight_pct']:.0f}%  |  평가: {c['val_end_만']:,.0f}만원"
                    )
                lines.append(f"\n<i>/contrib 60  →  60일 기준으로 분석</i>")
                response = "\n".join(lines)
        elif command == "/capm":
            from portfolio_tracker import calc_capm_metrics
            days_arg = 90
            if args:
                try:
                    days_arg = int(args[0])
                except ValueError:
                    pass
            capm = calc_capm_metrics(days=days_arg)
            if not capm:
                response = "📐 CAPM 분석에 필요한 데이터가 부족해요. (최소 10일치 스냅샷 필요)"
            else:
                alpha_sign = "+" if capm["alpha_pct"] >= 0 else ""
                alpha_em   = "✅" if capm["alpha_pct"] >= 0 else "⚠️"
                beta_em    = "🔴" if capm["beta"] > 1.2 else ("🟡" if capm["beta"] > 0.8 else "🟢")
                sharpe_em  = "✅" if capm["sharpe"] > 1 else ("🟡" if capm["sharpe"] > 0 else "⚠️")
                response = (
                    f"📐 <b>CAPM 포트폴리오 분석</b> (최근 {capm['n_days']}거래일)\n\n"
                    f"{beta_em} <b>베타(β)</b>: {capm['beta']:.3f}\n"
                    f"  시장보다 {'더 민감' if capm['beta'] > 1 else '덜 민감'}하게 움직임\n\n"
                    f"<b>수익률 비교</b>\n"
                    f"  무위험이자율(Rf):   {capm['rf_pct']:.1f}%\n"
                    f"  시장수익률(S&P500): {capm['mkt_pct']:+.1f}%\n"
                    f"  CAPM 기대수익률:   {capm['expected_pct']:+.1f}%\n"
                    f"  실제 수익률:       {capm['actual_pct']:+.1f}%\n\n"
                    f"{alpha_em} <b>알파(α)</b>: {alpha_sign}{capm['alpha_pct']:.2f}%\n"
                    f"  {'시장 대비 초과수익 달성 중 🎉' if capm['alpha_pct'] >= 0 else '시장 기대치 하회 중'}\n\n"
                    f"{sharpe_em} <b>샤프 비율</b>: {capm['sharpe']:.3f}\n"
                    f"  (위험 1단위당 초과수익, 1 이상이면 우수)\n\n"
                    f"📊 <b>트레이너 비율</b>: {capm['treynor_pct']:.2f}%\n"
                    f"  (베타 1단위당 초과수익)\n\n"
                    f"<i>/capm 30  →  30일 기준으로 분석</i>"
                )
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
                    send_message(bot_token, chat_id, response, reply_markup=REPLY_KEYBOARD)
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
