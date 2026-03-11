"""Portfolio management — balance, trades, P&L tracking."""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")


@dataclass
class Trade:
    """A single trade record."""

    trade_id: int
    market_id: str
    question: str
    side: str  # YES or NO
    entry_price: float
    size_usd: float
    quantity: float  # shares = size_usd / entry_price
    model_prob: float
    market_prob: float
    edge: float
    kelly: float
    threshold: float
    threshold_high: float
    days_to_expiry: float
    opened_at: str
    # Filled on resolution
    result: str = ""  # 'won', 'lost', ''
    payout: float = 0.0
    pnl: float = 0.0
    closed_at: str = ""
    close_price: float = 0.0


@dataclass
class Portfolio:
    """Portfolio state with trade history."""

    initial_balance: float = 100.0
    balance: float = 100.0
    total_pnl: float = 0.0
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    next_trade_id: int = 1

    def open_trade(
        self,
        market_id: str,
        question: str,
        side: str,
        price: float,
        size_usd: float,
        model_prob: float,
        market_prob: float,
        edge: float,
        kelly: float,
        threshold: float,
        threshold_high: float,
        days_to_expiry: float,
    ) -> Trade:
        """Open a new paper trade."""
        quantity = size_usd / price if price > 0 else 0
        trade = Trade(
            trade_id=self.next_trade_id,
            market_id=market_id,
            question=question,
            side=side,
            entry_price=price,
            size_usd=size_usd,
            quantity=quantity,
            model_prob=model_prob,
            market_prob=market_prob,
            edge=edge,
            kelly=kelly,
            threshold=threshold,
            threshold_high=threshold_high,
            days_to_expiry=days_to_expiry,
            opened_at=datetime.now(tz=timezone.utc).isoformat(),
        )
        self.next_trade_id += 1
        self.balance -= size_usd
        self.trades.append(trade)

        logger.info(
            "OPEN #%d: %s %s @ %.2fc | $%.2f | edge: %+.1f%% | %s",
            trade.trade_id,
            side,
            "YES" if side == "YES" else "NO",
            price * 100,
            size_usd,
            edge * 100,
            question[:60],
        )

        self._record_equity()
        self.save()
        return trade

    def resolve_trade(self, trade: Trade, won: bool) -> None:
        """Resolve a trade as won or lost."""
        if won:
            trade.result = "won"
            trade.payout = trade.quantity  # each share pays $1 if won
            trade.pnl = trade.payout - trade.size_usd
            trade.close_price = 1.0
        else:
            trade.result = "lost"
            trade.payout = 0.0
            trade.pnl = -trade.size_usd
            trade.close_price = 0.0

        trade.closed_at = datetime.now(tz=timezone.utc).isoformat()
        self.balance += trade.payout
        self.total_pnl += trade.pnl

        logger.info(
            "%s #%d: %s | Cost $%.2f | Payout $%.2f | PnL: $%.2f | Bal: $%.2f",
            "WON" if won else "LOST",
            trade.trade_id,
            trade.question[:50],
            trade.size_usd,
            trade.payout,
            trade.pnl,
            self.balance,
        )

        self._record_equity()
        self.save()

    def simulate_resolution(self, trade: Trade, final_btc_price: float) -> bool:
        """Simulate trade resolution based on final BTC price."""
        if trade.result:
            return trade.result == "won"

        yes_wins = False
        ql = trade.question.lower()
        if "dip to" in ql:
            yes_wins = final_btc_price <= trade.threshold
        elif "up or down" in ql:
            # Can't reliably simulate — skip auto-resolution
            return False
        elif "above" in ql or "greater" in ql:
            yes_wins = final_btc_price >= trade.threshold
        elif "below" in ql or "less" in ql:
            yes_wins = final_btc_price < trade.threshold
        elif "between" in ql:
            yes_wins = trade.threshold <= final_btc_price < trade.threshold_high
        elif "reach" in ql or "hit" in ql:
            yes_wins = final_btc_price >= trade.threshold

        won = (trade.side == "YES" and yes_wins) or (
            trade.side == "NO" and not yes_wins
        )
        self.resolve_trade(trade, won)
        return won

    @property
    def open_trades(self) -> list[Trade]:
        return [t for t in self.trades if not t.result]

    @property
    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.result]

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.result == "won")

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.result == "lost")

    @property
    def win_rate(self) -> float:
        closed = len(self.closed_trades)
        return self.wins / closed if closed > 0 else 0

    @property
    def exposure(self) -> float:
        return sum(t.size_usd for t in self.open_trades)

    @property
    def equity(self) -> float:
        return self.balance + self.exposure

    def get_summary(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "total_pnl": round(self.total_pnl, 2),
            "pnl_pct": round((self.equity / self.initial_balance - 1) * 100, 2),
            "total_trades": self.total_trades,
            "open_trades": len(self.open_trades),
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate * 100, 1),
            "exposure": round(self.exposure, 2),
            "initial_balance": self.initial_balance,
        }

    def _record_equity(self) -> None:
        if len(self.equity_curve) > 500:
            self.equity_curve = self.equity_curve[-400:]
        self.equity_curve.append(
            {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "equity": round(self.equity, 2),
                "balance": round(self.balance, 2),
                "trades": self.total_trades,
                "pnl": round(self.total_pnl, 2),
            }
        )

    def save(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        state = {
            "initial_balance": self.initial_balance,
            "balance": self.balance,
            "total_pnl": self.total_pnl,
            "next_trade_id": self.next_trade_id,
            "trades": [asdict(t) for t in self.trades],
            "equity_curve": self.equity_curve[-500:],
        }
        (DATA_DIR / "portfolio.json").write_text(
            json.dumps(state, indent=2, default=str)
        )

    @classmethod
    def load(cls) -> "Portfolio":
        path = DATA_DIR / "portfolio.json"
        if not path.exists():
            p = cls(
                initial_balance=settings.initial_balance,
                balance=settings.initial_balance,
            )
            p._record_equity()
            p.save()
            return p

        data = json.loads(path.read_text())
        p = cls(
            initial_balance=data.get("initial_balance", settings.initial_balance),
            balance=data.get("balance", settings.initial_balance),
            total_pnl=data.get("total_pnl", 0),
            next_trade_id=data.get("next_trade_id", 1),
            equity_curve=data.get("equity_curve", []),
        )
        for td in data.get("trades", []):
            p.trades.append(Trade(**td))
        return p
