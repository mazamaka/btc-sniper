"""BTC Sniper Engine — core trading loop."""

import logging
import threading
from datetime import datetime, timezone

from config import settings
from sniper.markets import BTCMarket, fetch_btc_markets
from sniper.model import VolatilityData, compute_signals, fetch_btc_volatility
from sniper.portfolio import Portfolio

logger = logging.getLogger(__name__)


class SniperEngine:
    """Main sniper engine — scan, analyze, trade."""

    def __init__(self, portfolio: Portfolio) -> None:
        self.portfolio = portfolio
        self.vol_data: VolatilityData | None = None
        self.last_markets: list[BTCMarket] = []
        self.scan_count: int = 0
        self.listeners: list = []  # callbacks for UI updates
        self._lock = threading.Lock()

    def _notify(self, event: str, data: dict) -> None:
        for cb in self.listeners:
            try:
                cb(event, data)
            except Exception:
                pass

    def update_volatility(self) -> VolatilityData:
        """Refresh BTC price and volatility data."""
        with self._lock:
            self.vol_data = fetch_btc_volatility()
        self._notify(
            "price",
            {
                "btc_price": self.vol_data.current_price,
                "daily_vol": round(self.vol_data.daily_volatility * 100, 3),
            },
        )
        return self.vol_data

    def scan(self) -> dict:
        """Run a full scan: fetch markets, compute signals, execute trades."""
        with self._lock:
            return self._scan_inner()

    def _scan_inner(self) -> dict:
        self.scan_count += 1
        logger.info("=== SCAN #%d ===", self.scan_count)

        # 1. Update volatility if stale
        if self.vol_data is None:
            self.update_volatility()

        # 2. Fetch BTC markets
        self.last_markets = fetch_btc_markets()
        if not self.last_markets:
            logger.warning("No BTC price markets found")
            return {"markets": 0, "signals": 0, "trades": 0}

        # Filter by liquidity and expiry
        active_markets = [
            m
            for m in self.last_markets
            if m.liquidity >= settings.min_liquidity and m.days_to_expiry > 0
        ]

        # 3. Compute signals
        signals = compute_signals(active_markets, self.vol_data)

        # 4. Execute trades
        open_market_ids = {t.market_id for t in self.portfolio.open_trades}
        trades_opened = 0

        for sig in signals:
            # Skip if already have position
            if sig.market_id in open_market_ids:
                continue

            # Check exposure limit
            if (
                self.portfolio.exposure
                >= self.portfolio.equity * settings.max_total_exposure_pct
            ):
                logger.info("Exposure limit reached, stopping")
                break

            # Calculate trade size
            trade_size = self.portfolio.balance * sig.trade_size_pct
            trade_size = max(settings.min_trade_size, trade_size)
            trade_size = min(
                trade_size, self.portfolio.balance * settings.max_trade_pct
            )

            if trade_size > self.portfolio.balance:
                continue
            if trade_size > self.portfolio.balance * 0.95:
                continue
            if trade_size < settings.min_trade_size:
                continue

            # Determine buy price
            buy_price = sig.market_prob if sig.side == "YES" else (1 - sig.market_prob)

            trade = self.portfolio.open_trade(
                market_id=sig.market_id,
                question=sig.question,
                side=sig.side,
                price=buy_price,
                size_usd=trade_size,
                model_prob=sig.model_prob,
                market_prob=sig.market_prob,
                edge=sig.edge,
                kelly=sig.kelly_fraction,
                threshold=sig.threshold,
                threshold_high=sig.threshold_high,
                days_to_expiry=sig.days_to_expiry,
            )

            self._notify(
                "trade",
                {
                    "trade_id": trade.trade_id,
                    "side": trade.side,
                    "question": trade.question,
                    "price": trade.entry_price,
                    "size": trade.size_usd,
                    "edge": sig.edge,
                    "model_prob": sig.model_prob,
                    "market_prob": sig.market_prob,
                },
            )

            trades_opened += 1
            open_market_ids.add(sig.market_id)

        result = {
            "scan": self.scan_count,
            "markets_found": len(self.last_markets),
            "markets_active": len(active_markets),
            "signals": len(signals),
            "trades_opened": trades_opened,
            "btc_price": self.vol_data.current_price if self.vol_data else 0,
            "balance": self.portfolio.balance,
            "equity": self.portfolio.equity,
            "total_pnl": self.portfolio.total_pnl,
            "total_trades": self.portfolio.total_trades,
        }

        self._notify("scan", result)
        self._notify("portfolio", self.portfolio.get_summary())

        logger.info(
            "Scan #%d: %d markets, %d signals, %d trades | Bal: $%.2f | PnL: $%.2f",
            self.scan_count,
            len(active_markets),
            len(signals),
            trades_opened,
            self.portfolio.balance,
            self.portfolio.total_pnl,
        )

        return result

    def check_resolutions(self) -> int:
        """Check if any open trades have expired and resolve them."""
        with self._lock:
            return self._check_resolutions_inner()

    def _check_resolutions_inner(self) -> int:
        if not self.vol_data:
            return 0

        resolved = 0
        now = datetime.now(tz=timezone.utc)

        for trade in self.portfolio.open_trades:
            # Check if market expired (rough check by days_to_expiry)
            if not trade.opened_at:
                continue
            try:
                opened = datetime.fromisoformat(trade.opened_at)
                elapsed_days = (now - opened).total_seconds() / 86400
                if elapsed_days < trade.days_to_expiry:
                    continue
            except (ValueError, TypeError):
                continue

            # Resolve based on current BTC price
            won = self.portfolio.simulate_resolution(trade, self.vol_data.current_price)
            result_str = "WON" if won else "LOST"

            self._notify(
                "resolution",
                {
                    "trade_id": trade.trade_id,
                    "result": result_str,
                    "pnl": trade.pnl,
                    "payout": trade.payout,
                    "cost": trade.size_usd,
                    "question": trade.question,
                    "balance": self.portfolio.balance,
                },
            )

            resolved += 1

        if resolved:
            logger.info("Resolved %d trades", resolved)
        return resolved
