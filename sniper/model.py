"""Mathematical probability model for BTC price prediction.

Uses log-normal distribution based on historical volatility.
No AI needed — pure math.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from scipy import stats

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class VolatilityData:
    """BTC volatility parameters."""

    current_price: float
    daily_volatility: float  # daily log-return std
    annual_volatility: float
    last_updated: datetime
    prices_30d: list[float]


def fetch_btc_volatility() -> VolatilityData:
    """Fetch current BTC price and calculate historical volatility."""
    with httpx.Client(timeout=15) as client:
        # Current price
        r = client.get(
            f"{settings.coingecko_url}/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
        )
        r.raise_for_status()
        current_price = r.json()["bitcoin"]["usd"]

        # Historical prices for volatility
        r2 = client.get(
            f"{settings.coingecko_url}/coins/bitcoin/market_chart",
            params={
                "vs_currency": "usd",
                "days": str(settings.volatility_lookback_days),
            },
        )
        r2.raise_for_status()
        prices = [p[1] for p in r2.json()["prices"]]

    # Calculate log-return volatility (standard deviation)
    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    mean_r = sum(log_returns) / len(log_returns)
    daily_vol = math.sqrt(
        sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
    )

    # Apply multiplier for safety margin
    daily_vol *= settings.volatility_multiplier

    annual_vol = daily_vol * math.sqrt(365)

    logger.info(
        "BTC: $%.0f | Daily vol: %.2f%% | Annual vol: %.1f%%",
        current_price,
        daily_vol * 100,
        annual_vol * 100,
    )

    return VolatilityData(
        current_price=current_price,
        daily_volatility=daily_vol,
        annual_volatility=annual_vol,
        last_updated=datetime.now(tz=timezone.utc),
        prices_30d=prices,
    )


def prob_above(current: float, target: float, days: float, vol: float) -> float:
    """Probability that BTC will be above target in N days (log-normal model)."""
    if days <= 0:
        return 1.0 if current >= target else 0.0
    if target <= 0:
        return 1.0

    sigma = vol * math.sqrt(days)
    if sigma <= 0:
        return 1.0 if current >= target else 0.0

    z = math.log(target / current) / sigma
    return float(1 - stats.norm.cdf(z))


def prob_between(
    current: float, low: float, high: float, days: float, vol: float
) -> float:
    """Probability that BTC will be between low and high in N days."""
    return prob_above(current, low, days, vol) - prob_above(current, high, days, vol)


def prob_below(current: float, target: float, days: float, vol: float) -> float:
    """Probability that BTC will be below target in N days."""
    return 1 - prob_above(current, target, days, vol)


@dataclass
class TradeSignal:
    """A computed trade signal for a BTC market."""

    market_id: str
    question: str
    market_type: str
    side: str  # 'YES' or 'NO'
    model_prob: float  # our calculated probability for YES
    market_prob: float  # market's YES price
    edge: float  # model_prob - market_prob (positive = buy YES)
    kelly_fraction: float  # full Kelly allocation
    trade_size_pct: float  # actual allocation (fractional Kelly, capped)
    days_to_expiry: float
    threshold: float
    threshold_high: float = 0.0


def compute_signals(btc_markets: list, vol_data: VolatilityData) -> list[TradeSignal]:
    """Compute trade signals for all BTC markets."""
    from sniper.markets import BTCMarket

    signals: list[TradeSignal] = []

    for mkt in btc_markets:
        if not isinstance(mkt, BTCMarket):
            continue
        if mkt.days_to_expiry <= 0:
            continue

        # Calculate model probability for YES outcome
        if mkt.market_type == "above" or mkt.market_type == "reach":
            model_yes = prob_above(
                vol_data.current_price,
                mkt.threshold,
                mkt.days_to_expiry,
                vol_data.daily_volatility,
            )
        elif mkt.market_type == "below":
            model_yes = prob_below(
                vol_data.current_price,
                mkt.threshold,
                mkt.days_to_expiry,
                vol_data.daily_volatility,
            )
        elif mkt.market_type == "between":
            model_yes = prob_between(
                vol_data.current_price,
                mkt.threshold,
                mkt.threshold_high,
                mkt.days_to_expiry,
                vol_data.daily_volatility,
            )
        elif mkt.market_type == "dip":
            # "Will BTC dip to $X?" — YES means price will go below threshold
            model_yes = prob_below(
                vol_data.current_price,
                mkt.threshold,
                mkt.days_to_expiry,
                vol_data.daily_volatility,
            )
        elif mkt.market_type == "updown":
            # "Bitcoin Up or Down" — UP outcome probability
            # Use very short timeframe from market duration
            duration = (
                mkt.updown_duration_days
                if mkt.updown_duration_days > 0
                else 5 / (24 * 60)
            )
            model_yes = prob_above(
                vol_data.current_price,
                vol_data.current_price,
                duration,
                vol_data.daily_volatility,
            )
        else:
            continue

        # Clamp
        model_yes = max(0.001, min(0.999, model_yes))

        # Determine side and edge
        edge_yes = model_yes - mkt.yes_price
        edge_no = (1 - model_yes) - mkt.no_price

        if abs(edge_yes) < settings.min_edge:
            continue
        if abs(edge_yes) > settings.max_edge:
            continue

        if edge_yes > 0:
            side = "YES"
            edge = edge_yes
            buy_price = mkt.yes_price
            win_prob = model_yes
        else:
            side = "NO"
            edge = abs(edge_yes)
            buy_price = mkt.no_price
            win_prob = 1 - model_yes

        # Kelly criterion: f = (b*p - q) / b
        # where b = (1/buy_price - 1), p = win_prob, q = 1-win_prob
        if buy_price <= 0 or buy_price >= 1:
            continue
        b = (1 / buy_price) - 1
        kelly = (b * win_prob - (1 - win_prob)) / b
        kelly = max(0, kelly)

        # Apply fractional Kelly and caps
        trade_pct = kelly * settings.kelly_fraction
        trade_pct = min(trade_pct, settings.max_trade_pct)

        if trade_pct <= 0:
            continue

        signals.append(
            TradeSignal(
                market_id=mkt.market_id,
                question=mkt.question,
                market_type=mkt.market_type,
                side=side,
                model_prob=model_yes,
                market_prob=mkt.yes_price,
                edge=edge_yes if side == "YES" else -abs(edge_yes),
                kelly_fraction=kelly,
                trade_size_pct=trade_pct,
                days_to_expiry=mkt.days_to_expiry,
                threshold=mkt.threshold,
                threshold_high=mkt.threshold_high,
            )
        )

    # Sort by edge magnitude
    signals.sort(key=lambda s: abs(s.edge), reverse=True)
    return signals
