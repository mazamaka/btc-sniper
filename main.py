"""BTC Sniper — Polymarket BTC Price Prediction Bot.

Mathematical edge detection using log-normal volatility model + Kelly sizing.
No AI needed — pure probability math.
"""

import argparse
import json
import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "sniper.log", maxBytes=5 * 1024 * 1024, backupCount=3
        ),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def run_scan() -> None:
    """Run a single scan and print results."""
    from sniper.engine import SniperEngine
    from sniper.portfolio import Portfolio

    portfolio = Portfolio.load()
    engine = SniperEngine(portfolio)
    engine.update_volatility()
    result = engine.scan()
    logger.info("Result: %s", json.dumps(result, indent=2))


def run_web() -> None:
    """Start web dashboard."""
    from web.app import start_web

    start_web()


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC Sniper")
    parser.add_argument("--scan", action="store_true", help="Run single scan")
    parser.add_argument("--web", action="store_true", help="Start web dashboard")
    args = parser.parse_args()

    if args.scan:
        run_scan()
    elif args.web:
        run_web()
    else:
        run_web()


if __name__ == "__main__":
    main()
