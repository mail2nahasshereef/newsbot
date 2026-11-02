"""
Stock news alert bot — GitHub Actions version
---------------------------------------------
Runs every ~5 minutes on GitHub. Each run:
  1. Pulls the latest US stock news from Alpaca (Benzinga feed)
  2. Skips anything already alerted (remembered in state/seen.json)
  3. Filters with keywords (free) or Claude (if a key is set)
  4. Checks the live price move
  5. Sends a Telegram alert

Keys come from GitHub Secrets (never put them in this file).
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ALPACA_KEY = os.environ["ALPACA_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
USE_AI = ANTHROPIC_API_KEY.startswith("sk-ant-") and len(ANTHROPIC_API_KEY) > 20

# ---- Tuning (set as GitHub "Variables" to change without editing code) -------
MIN_IMPACT = int(os.getenv("MIN_IMPACT") or 7)
CONFIRM_MOVE_PCT = float(os.getenv("CONFIRM_MOVE_PCT") or 3)
WATCHLIST = [s.strip().upper() for s in (os.getenv("WATCHLIST") or "").split(",") if s.strip()]
LOOKBACK_MIN = int(os.getenv("LOOKBACK_MIN") or 30)     # how far back each run looks
MODEL = os.getenv("CLAUDE_MODEL") or "claude-haiku-5-5"
TEST_RUN = os.getenv("TEST_RUN") == "1"                  # manual run → send a status message
# -----------------------------------------------------------------------------

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks/snapshots"
HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
STATE = Path("state/seen.json")

http = httpx.Client(timeout=15)

# ---- Keyword rules (free mode) ------------------------------------------------
STRONG_GOOD = ["fda approv", "to be acquired", "agrees to be acquired", "buyout",
               "definitive agreement to acquire", "merger agreement", "takeover offer",
               "met primary endpoint", "added to s&p 500"]
STRONG_BAD = ["complete response letter", "fda reject", "chapter 11", "bankruptcy",
              "did not meet primary endpoint", "fails to meet primary", "trading halt",
              "sec charges", "fraud", "delisting", "going concern"]
GOOD = ["beats", "tops estimates", "raises guidance", "raises outlook", "raises forecast",
        "record revenue", "awarded", "contract worth", "upgrade", "partnership with",
        "share buyback", "special dividend"]
BAD = ["misses", "below estimates", "cuts guidance", "lowers guidance", "withdraws guidance",
       "lowers outlook", "public offering", "stock offering", "priced offering", "dilution",
       "investigation", "subpoena", "resigns", "steps down", "recall", "downgrade",
       "layoffs", "warns"]

PROMPT = """You are a US equities news analyst. Judge if this headline is likely to move
the named stock(s) by 5% or more today.

Big movers: earnings beat/miss with guidance change, FDA approval/rejection, clinical
trial results, M&A / buyout, CEO exit, fraud/investigation, share offering/dilution,
bankruptcy, major contract win/loss, index inclusion, large analyst up/downgrade on a
small cap. Routine market commentary, ETF lists, "stocks to watch" articles = low impact.

Headline: {headline}
Summary: {summary}
Tickers: {symbols}

Reply with ONLY JSON:
{{"ticker": "MAIN_TICKER", "direction": "good" | "bad", "event": "short event type",
  "impact": 1-10, "reason": "one short sentence"}}"""


def load_seen() -> list:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return []


def save_seen(seen: list):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(seen[-3000:]))


def send_telegram(text: str):
    r = http.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    })
    if r.status_code != 200:
        print("Telegram error:", r.text)


def fetch_news() -> list:
    start = (datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"start": start, "limit": 50, "sort": "desc"}
    items, token = [], None
    for _ in range(5):  # up to 250 headlines per run
        if token:
            params["page_token"] = token
        r = http.get(NEWS_URL, headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()
        items += data.get("news", [])
        token = data.get("next_page_token")
        if not token:
            break
    return items


def price_move(ticker: str):
    try:
        r = http.get(SNAPSHOT_URL, headers=HEADERS, params={"symbols": ticker, "feed": "iex"})
        snap = r.json().get(ticker) or {}
        last = (snap.get("latestTrade") or {}).get("p")
        prev = (snap.get("prevDailyBar") or {}).get("c")
        if last and prev:
            return last, (last - prev) / prev * 100
    except Exception as e:
        print("Price error:", e)
    return None, None


def keyword_classify(item: dict):
    text = f"{item.get('headline', '')} {item.get('summary', '')}".lower()
    for words, direction, impact in ((STRONG_GOOD, "good", 9), (STRONG_BAD, "bad", 9),
                                     (GOOD, "good", 7), (BAD, "bad", 7)):
        hit = next((w for w in words if w in text), None)
        if hit:
            return {"ticker": item["symbols"][0], "direction": direction,
                    "event": hit, "impact": impact, "reason": f"Matched '{hit}'"}
    return None


def ai_classify(item: dict):
    from anthropic import Anthropic
    try:
        msg = Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
            model=MODEL, max_tokens=200,
            messages=[{"role": "user", "content": PROMPT.format(
                headline=item.get("headline", ""),
                summary=(item.get("summary") or "")[:600],
                symbols=", ".join(item.get("symbols", [])),
            )}],
        )
        text = msg.content[0].text
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as e:
        print("Claude error:", e)
        return None


def evaluate(item: dict):
    """Returns (result, ticker, last, pct) if this item deserves an alert, else None."""
    symbols = item.get("symbols") or []
    if not symbols or len(symbols) > 4:
        return None
    if WATCHLIST and not any(s in WATCHLIST for s in symbols):
        return None

    if USE_AI:
        result = ai_classify(item)
        if not result or int(result.get("impact", 0)) < MIN_IMPACT:
            return None
        ticker = (result.get("ticker") or symbols[0]).upper()
        last, pct = price_move(ticker)
        return result, ticker, last, pct

    result = keyword_classify(item)
    ticker = (result["ticker"] if result else symbols[0]).upper()
    last, pct = price_move(ticker)
    moving = pct is not None and abs(pct) >= CONFIRM_MOVE_PCT
    if result and (result["impact"] >= 9 or moving):
        return result, ticker, last, pct
    if pct is not None and abs(pct) >= 5:
        return ({"direction": "good" if pct > 0 else "bad", "event": "big move on news",
                 "impact": 8, "reason": "Stock already moving 5%+ around this headline."},
                ticker, last, pct)
    return None


def format_alert(item, result, ticker, last, pct) -> str:
    icon = "🟢" if result.get("direction") == "good" else "🔴"
    if pct is None:
        price_line = "Price: n/a"
    else:
        tag = " ✅ moving" if abs(pct) >= CONFIRM_MOVE_PCT else " ⏳ not moving yet"
        price_line = f"Price: ${last:.2f} ({pct:+.1f}% vs prev close){tag}"
    try:
        published = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        age = int((datetime.now(timezone.utc) - published).total_seconds() // 60)
        age_line = f"{age} min ago"
    except Exception:
        age_line = ""
    return (f"{icon} <b>{ticker}</b> · {result.get('event', '')} · impact {result.get('impact')}/10\n"
            f"{item.get('headline', '')}\n\n"
            f"{result.get('reason', '')}\n{price_line}\n"
            f"<a href=\"{item.get('url', '')}\">Source</a> · {age_line}")


def main():
    seen = load_seen()
    seen_set = set(seen)
    news = fetch_news()
    fresh = [n for n in news if n.get("id") not in seen_set]
    alerts = 0

    for item in reversed(fresh):  # oldest first
        seen.append(item.get("id"))
        hit = evaluate(item)
        if hit:
            send_telegram(format_alert(item, *hit))
            alerts += 1
            print("ALERT:", hit[1], item.get("headline"))

    save_seen(seen)
    mode = "Claude AI" if USE_AI else "FREE (keywords + price)"
    summary = f"Scanned {len(news)} headlines ({len(fresh)} new), sent {alerts} alerts. Mode: {mode}"
    print(summary)
    if TEST_RUN:
        send_telegram(f"✅ GitHub bot is working.\n{summary}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Run failed:", e)
        if TEST_RUN:
            send_telegram(f"⚠️ Bot run failed: {e}")
        sys.exit(1)
