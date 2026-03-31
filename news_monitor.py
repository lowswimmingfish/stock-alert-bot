#!/usr/bin/env python3
"""News monitor - checks for important events related to portfolio holdings and sends Telegram alerts."""

import json
import hashlib
import logging
import requests
import anthropic
import yfinance as yf
from pathlib import Path
from datetime import datetime, timedelta

CONFIG_PATH = Path(__file__).parent / "config.json"
SEEN_PATH = Path(__file__).parent / "seen_news.json"
LOG_PATH = Path(__file__).parent / "news_monitor.log"

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_seen():
    if SEEN_PATH.exists():
        with open(SEEN_PATH) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_PATH, "w") as f:
        json.dump(list(seen), f)


def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    })


def fetch_news(ticker):
    """Fetch recent news for a ticker via yfinance."""
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
        return news
    except Exception as e:
        logging.error(f"Failed to fetch news for {ticker}: {e}")
        return []


def fetch_kr_news(ticker, name):
    """Fetch Korean stock news via web search fallback."""
    try:
        url = f"https://finance.naver.com/item/news_news.naver?code={ticker}&page=1"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        # Simple extraction of titles from the response
        from html.parser import HTMLParser

        class TitleParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_title = False
                self.titles = []
                self.links = []
                self.current_link = None

            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    attrs_dict = dict(attrs)
                    href = attrs_dict.get("href", "")
                    title = attrs_dict.get("title", "")
                    if "/news/read" in href and title:
                        self.titles.append({"title": title, "link": href})

        parser = TitleParser()
        parser.feed(resp.text)
        return [{"title": item["title"], "link": item["link"], "source": "naver"} for item in parser.titles[:5]]
    except Exception as e:
        logging.error(f"Failed to fetch KR news for {name}: {e}")
        return []


def is_important(news_items, ticker, name, config):
    """Use Claude to determine if any news is important enough to alert."""
    if not news_items:
        return None

    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])

    # Build news summary for Claude
    news_text = "\n".join([
        f"- {item.get('title', item.get('content', ''))}"
        for item in news_items[:10]
    ])

    prompt = f"""다음은 {name}({ticker}) 종목의 최근 뉴스 헤드라인이야.
투자자 입장에서 즉시 알아야 할 중요한 이슈가 있는지 판단해줘.

중요한 이슈 기준:
- 실적 발표 / 어닝 서프라이즈 또는 쇼크
- 대규모 인수합병 (M&A)
- CEO 교체 / 경영진 변동
- 규제 이슈, 소송, 정부 제재
- 주가에 직접적 영향을 줄 수 있는 사건
- 섹터 전체에 영향을 주는 거시경제 이슈

뉴스:
{news_text}

만약 중요한 이슈가 있다면 다음 형식으로 답해:
ALERT: [한 줄 요약]
이유: [왜 중요한지 2-3줄]

중요한 이슈가 없다면:
NONE"""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    result = resp.content[0].text.strip()

    if result.startswith("ALERT:"):
        return result
    return None


def run():
    config = load_config()
    seen = load_seen()
    bot_token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]

    all_stocks = []
    for s in config["portfolio"]["us_stocks"]:
        all_stocks.append({"ticker": s["ticker"], "name": s["ticker"], "is_kr": False})
    for s in config["portfolio"]["kr_stocks"]:
        all_stocks.append({"ticker": s["ticker"], "name": s.get("name", s["ticker"]), "is_kr": True})

    alerts_sent = 0

    for stock in all_stocks:
        ticker = stock["ticker"]
        name = stock["name"]

        if stock["is_kr"]:
            news_items = fetch_kr_news(ticker, name)
        else:
            raw_news = fetch_news(ticker)
            news_items = []
            for n in raw_news:
                content = n.get("content", {})
                if isinstance(content, dict):
                    title = content.get("title", "")
                    link = content.get("canonicalUrl", {}).get("url", "") if isinstance(content.get("canonicalUrl"), dict) else ""
                else:
                    title = n.get("title", "")
                    link = n.get("link", "")
                if title:
                    news_items.append({"title": title, "link": link})

        # Filter already-seen news using content hash
        new_items = []
        for item in news_items:
            key = hashlib.md5(item["title"].encode()).hexdigest()
            if key not in seen:
                new_items.append(item)
                seen.add(key)

        if not new_items:
            logging.info(f"{name}: no new articles")
            continue

        logging.info(f"{name}: {len(new_items)} new articles, checking importance...")

        alert = is_important(new_items, ticker, name, config)
        if alert:
            msg = (
                f"<b>중요 이슈 알림</b>\n"
                f"종목: <b>{name}</b> ({ticker})\n"
                f"시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"{alert}"
            )
            send_telegram(bot_token, chat_id, msg)
            logging.info(f"ALERT sent for {name}: {alert[:80]}")
            alerts_sent += 1

    save_seen(seen)
    logging.info(f"Run complete. {alerts_sent} alerts sent.")


if __name__ == "__main__":
    run()
