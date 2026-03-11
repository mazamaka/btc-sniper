"""Polymarket API — поиск BTC price prediction markets."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Regex for BTC price markets
RE_ABOVE = re.compile(
    r"price of Bitcoin be (?:above|greater than) \$?([\d,]+).*?on (.+)\?",
    re.IGNORECASE,
)
RE_BELOW = re.compile(
    r"price of Bitcoin be (?:below|less than) \$?([\d,]+).*?on (.+)\?",
    re.IGNORECASE,
)
RE_BETWEEN = re.compile(
    r"price of Bitcoin be between \$?([\d,]+) and \$?([\d,]+).*?on (.+)\?",
    re.IGNORECASE,
)
RE_REACH = re.compile(
    r"Bitcoin (?:reach|hit) \$?([\d,]+).*?(?:on|in|by) (.+)\?",
    re.IGNORECASE,
)
RE_DIP = re.compile(
    r"Bitcoin dip to \$?([\d,]+).*?(?:in|by) (.+)\?",
    re.IGNORECASE,
)
RE_UPDOWN = re.compile(
    r"Bitcoin Up or Down\s*-\s*(.+)",
    re.IGNORECASE,
)


@dataclass
class BTCMarket:
    """Parsed BTC price prediction market."""

    market_id: str
    question: str
    market_type: str  # 'above', 'below', 'between', 'reach', 'dip', 'updown'
    threshold: float  # target price (or lower bound for 'between')
    threshold_high: float = 0.0  # upper bound for 'between'
    expiry_str: str = ""
    expiry_date: datetime | None = None
    days_to_expiry: float = 0.0
    yes_price: float = 0.5
    no_price: float = 0.5
    volume: float = 0.0
    liquidity: float = 0.0
    clob_token_ids: list[str] = field(default_factory=list)
    slug: str = ""
    updown_duration_days: float = 0.0  # duration for Up/Down markets (in days)


def _parse_price(s: str) -> float:
    return float(s.replace(",", ""))


def _parse_date(s: str) -> datetime | None:
    """Parse date string like 'March 16' or 'March 16, 2026'."""
    s = s.strip().rstrip("?").strip()
    now = datetime.now(tz=timezone.utc)
    for fmt in ("%B %d, %Y", "%B %d %Y", "%B %d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Try "March" alone (end of month)
    for fmt in ("%B %Y", "%B"):
        try:
            dt = datetime.strptime(s.split(",")[0].strip(), fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            # End of month
            if dt.month == 12:
                dt = dt.replace(month=1, year=dt.year + 1)
            else:
                dt = dt.replace(month=dt.month + 1)
            from datetime import timedelta

            dt -= timedelta(days=1)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_updown_expiry(text: str) -> tuple[str, float]:
    """Parse 'Up or Down' time window into expiry string and fractional days.

    Example: 'March 12, 12:55AM-1:00AM ET' → ('March 12', ~0.003)
    """
    # Extract date part and time window
    text = text.strip().rstrip("?").strip()
    # Remove timezone suffix
    text = re.sub(r"\s+ET$", "", text)
    # Try to extract time range like "12:55AM-1:00AM"
    time_match = re.search(
        r"(\d{1,2}(?::\d{2})?[AP]M)\s*-\s*(\d{1,2}(?::\d{2})?[AP]M)", text
    )
    if time_match:
        date_part = text[: time_match.start()].strip().rstrip(",").strip()
        # Calculate duration in days from time range
        start_str = time_match.group(1)
        end_str = time_match.group(2)
        try:
            from datetime import datetime as dt

            for fmt in ("%I:%M%p", "%I%p"):
                try:
                    t1 = dt.strptime(start_str, fmt)
                    t2 = dt.strptime(end_str, fmt)
                    mins = (t2 - t1).total_seconds() / 60
                    if mins <= 0:
                        mins += 24 * 60
                    return date_part, mins / (24 * 60)
                except ValueError:
                    continue
        except Exception:
            pass
        return date_part, 5 / (24 * 60)  # default 5 min
    # Hourly format: "March 11, 2PM" → "March 11", 1 hour
    hour_match = re.search(r"(\d{1,2}[AP]M)$", text.strip())
    if hour_match:
        date_part = text[: hour_match.start()].strip().rstrip(",").strip()
        return date_part, 60 / (24 * 60)  # 1 hour
    return text, 5 / (24 * 60)


def parse_btc_market(raw: dict) -> BTCMarket | None:
    """Parse a raw Gamma API market into BTCMarket if it's a BTC price market."""
    q = raw.get("question", "")
    mid = raw.get("id", "")

    # Try each pattern (order matters: more specific first)
    mkt = None

    m = RE_BETWEEN.search(q)
    if m:
        mkt = BTCMarket(
            market_id=mid,
            question=q,
            market_type="between",
            threshold=_parse_price(m.group(1)),
            threshold_high=_parse_price(m.group(2)),
            expiry_str=m.group(3),
        )

    if not mkt:
        m = RE_DIP.search(q)
        if m:
            mkt = BTCMarket(
                market_id=mid,
                question=q,
                market_type="dip",
                threshold=_parse_price(m.group(1)),
                expiry_str=m.group(2),
            )

    if not mkt:
        m = RE_ABOVE.search(q)
        if m:
            mkt = BTCMarket(
                market_id=mid,
                question=q,
                market_type="above",
                threshold=_parse_price(m.group(1)),
                expiry_str=m.group(2),
            )

    if not mkt:
        m = RE_BELOW.search(q)
        if m:
            mkt = BTCMarket(
                market_id=mid,
                question=q,
                market_type="below",
                threshold=_parse_price(m.group(1)),
                expiry_str=m.group(2),
            )

    if not mkt:
        m = RE_REACH.search(q)
        if m:
            mkt = BTCMarket(
                market_id=mid,
                question=q,
                market_type="reach",
                threshold=_parse_price(m.group(1)),
                expiry_str=m.group(2),
            )

    if not mkt:
        m = RE_UPDOWN.search(q)
        if m:
            date_str, duration_days = _parse_updown_expiry(m.group(1))
            mkt = BTCMarket(
                market_id=mid,
                question=q,
                market_type="updown",
                threshold=0.0,  # no price threshold
                expiry_str=date_str,
            )
            mkt.updown_duration_days = duration_days

    if not mkt:
        return None

    # Parse prices
    import json

    prices_raw = raw.get("outcomePrices", "[]")
    prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    if prices:
        mkt.yes_price = float(prices[0])
        mkt.no_price = float(prices[1]) if len(prices) > 1 else 1 - mkt.yes_price

    mkt.volume = float(raw.get("volume", 0))
    mkt.liquidity = float(raw.get("liquidity", 0))
    mkt.slug = raw.get("slug", "")

    # Parse clob token IDs
    tokens_raw = raw.get("clobTokenIds", "[]")
    mkt.clob_token_ids = (
        json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
    )

    # Parse expiry
    mkt.expiry_date = _parse_date(mkt.expiry_str)
    if mkt.expiry_date:
        now = datetime.now(tz=timezone.utc)
        mkt.days_to_expiry = max(0, (mkt.expiry_date - now).total_seconds() / 86400)

    return mkt


def fetch_btc_markets(max_pages: int = 10) -> list[BTCMarket]:
    """Fetch all active BTC price prediction markets from Polymarket."""
    all_markets: list[BTCMarket] = []
    seen_ids: set[str] = set()

    with httpx.Client(timeout=30) as client:
        for offset in range(0, max_pages * 200, 200):
            try:
                r = client.get(
                    f"{settings.gamma_api_url}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 200,
                        "offset": offset,
                        "order": "liquidity",
                        "ascending": "false",
                    },
                )
                r.raise_for_status()
                raw_markets = r.json()
            except Exception as e:
                logger.error("API error at offset %d: %s", offset, e)
                break

            if not raw_markets:
                break

            for raw in raw_markets:
                q = raw.get("question", "").lower()
                if "bitcoin" not in q and "btc" not in q:
                    continue

                parsed = parse_btc_market(raw)
                if parsed and parsed.market_id not in seen_ids:
                    seen_ids.add(parsed.market_id)
                    all_markets.append(parsed)

    logger.info("Found %d BTC price prediction markets", len(all_markets))
    return all_markets
