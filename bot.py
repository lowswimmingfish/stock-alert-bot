#!/usr/bin/env python3
"""Telegram bot for managing stock portfolio - supports buy/sell commands and natural language via Claude."""

import json
import logging
import requests
import anthropic
import yfinance as yf
from pykrx import stock as pykrx_stock
from duckduckgo_search import DDGS
from pathlib import Path
from datetime import datetime, timedelta

CONFIG_PATH = Path(__file__).parent / "config.json"
LOG_PATH = Path(__file__).parent / "bot.log"
HISTORY_PATH = Path(__file__).parent / "chat_history.json"

MAX_HISTORY = 20  # 최대 저장 메시지 수 (user+assistant 합산)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


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


def get_live_prices(config):
    """Fetch live prices for all holdings."""
    lines = ["\n현재 시세:"]

    # US stocks
    for s in config["portfolio"]["us_stocks"]:
        try:
            t = yf.Ticker(s["ticker"])
            hist = t.history(period="2d")
            if len(hist) >= 1:
                price = hist["Close"].iloc[-1]
                change = ""
                if len(hist) >= 2:
                    prev = hist["Close"].iloc[-2]
                    pct = (price - prev) / prev * 100
                    change = f" (전일비 {pct:+.2f}%)"
                lines.append(f"- {s['ticker']}: ${price:.2f}{change}")
        except Exception:
            pass

    # KR stocks
    today = datetime.now()
    for s in config["portfolio"]["kr_stocks"]:
        try:
            end = today.strftime("%Y%m%d")
            start = (today - timedelta(days=10)).strftime("%Y%m%d")
            df = pykrx_stock.get_market_ohlcv(start, end, s["ticker"])
            if len(df) >= 1:
                price = int(df["종가"].iloc[-1])
                change = ""
                if len(df) >= 2:
                    prev = int(df["종가"].iloc[-2])
                    pct = (price - prev) / prev * 100
                    change = f" (전일비 {pct:+.2f}%)"
                name = s.get("name", s["ticker"])
                lines.append(f"- {name}: {price:,}원{change}")
        except Exception:
            pass

    # Exchange rate
    try:
        t = yf.Ticker("USDKRW=X")
        hist = t.history(period="1d")
        if len(hist) >= 1:
            rate = hist["Close"].iloc[-1]
            lines.append(f"\n환율: 1 USD = {rate:,.2f} KRW")
    except Exception:
        pass

    # Major indices
    indices = {"S&P500": "^GSPC", "NASDAQ": "^IXIC", "KOSPI": "^KS11", "KOSDAQ": "^KQ11"}
    lines.append("\n주요 지수:")
    for name, ticker in indices.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                curr = hist["Close"].iloc[-1]
                prev = hist["Close"].iloc[-2]
                pct = (curr - prev) / prev * 100
                lines.append(f"- {name}: {curr:,.2f} ({pct:+.2f}%)")
        except Exception:
            pass

    return "\n".join(lines)


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo and return formatted results."""
    try:
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
    """Search for latest news using DuckDuckGo News."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(query, max_results=max_results))
        if not results:
            return "뉴스 결과가 없습니다."
        lines = []
        for r in results:
            date = r.get("date", "")
            lines.append(f"[{date}] {r.get('title', '')}")
            lines.append(f"내용: {r.get('body', '')}")
            lines.append(f"출처: {r.get('source', '')} - {r.get('url', '')}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"뉴스 검색 오류: {e}"


# Claude tool definitions
TOOLS = [
    {
        "name": "web_search",
        "description": "실시간 웹 검색. 최신 주가, 경제 지표, 기업 정보, 시황 등을 검색할 때 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어 (영어 또는 한국어)"},
                "max_results": {"type": "integer", "description": "결과 수 (기본 5)", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "news_search",
        "description": "최신 뉴스 검색. 종목 관련 뉴스, 경제 뉴스, 시장 이슈 등을 찾을 때 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "뉴스 검색어"},
                "max_results": {"type": "integer", "description": "결과 수 (기본 5)", "default": 5},
            },
            "required": ["query"],
        },
    },
]


def run_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return the result."""
    if tool_name == "web_search":
        return web_search(tool_input["query"], tool_input.get("max_results", 5))
    elif tool_name == "news_search":
        return news_search(tool_input["query"], tool_input.get("max_results", 5))
    return "알 수 없는 도구입니다."


def ask_claude(question, config):
    """Send a natural language question to Claude with tool use support for real-time search."""
    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])

    portfolio_context = get_portfolio_context(config)
    live_prices = get_live_prices(config)

    system_prompt = f"""너는 개인 주식 투자 어시스턴트야. 사용자의 포트폴리오 정보와 실시간 시세를 바탕으로 질문에 답해줘.
항상 한국어로 답하고, 간결하게 핵심만 말해줘. 텔레그램 메시지이므로 너무 길지 않게.
이전 대화 내용을 기억하고 맥락을 이어서 답해줘.

최신 정보가 필요하면 반드시 web_search 또는 news_search 도구를 사용해서 실시간으로 찾아와.
특히 다음 경우엔 검색해:
- 최근 뉴스나 이슈 질문
- 현재 시황이나 전망
- 특정 종목의 최근 동향
- 경제 지표나 금리 관련 질문

투자 조언 시 "개인적인 의견이며 투자 판단은 본인 책임"이라는 점을 명시해.

{portfolio_context}

{live_prices}

오늘 날짜: {datetime.now().strftime('%Y-%m-%d %H:%M')}"""

    history = load_history()
    history.append({"role": "user", "content": question})

    # Agentic loop - Claude can call tools multiple times
    messages = history.copy()
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

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


def handle_buy(args, config):
    if len(args) < 3:
        return "사용법: /buy &lt;종목&gt; &lt;수량&gt; &lt;매수가&gt; [한국종목명]\n예: /buy AAPL 10 150.5\n예: /buy 424980 44 14340 마이크로투나노"

    ticker = args[0].upper()
    try:
        shares = float(args[1])
        price = float(args[2])
    except ValueError:
        return "수량과 가격은 숫자로 입력해주세요."

    kr_name = args[3] if len(args) > 3 else None
    is_kr = ticker.isdigit() or kr_name is not None

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


def handle_sell(args, config):
    if len(args) < 2:
        return "사용법: /sell &lt;종목&gt; &lt;수량&gt;\n예: /sell AAPL 5"

    ticker = args[0].upper()
    try:
        shares = float(args[1])
    except ValueError:
        return "수량은 숫자로 입력해주세요."

    is_kr = ticker.isdigit()
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
        "/buy &lt;종목&gt; &lt;수량&gt; &lt;매수가&gt; [한국종목명]\n"
        "  예: /buy AAPL 10 150.5\n"
        "  예: /buy 424980 44 14340 마이크로투나노\n\n"
        "/sell &lt;종목&gt; &lt;수량&gt;\n"
        "  예: /sell AAPL 5\n\n"
        "/portfolio - 현재 보유종목 확인\n"
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
