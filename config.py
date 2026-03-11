"""BTC Sniper configuration."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Polymarket API
    gamma_api_url: str = "https://gamma-api.polymarket.com"

    # BTC Price Source
    coingecko_url: str = "https://api.coingecko.com/api/v3"

    # Volatility model
    volatility_lookback_days: int = 30
    volatility_multiplier: float = 1.2  # inflate vol slightly for safety

    # Trading
    initial_balance: float = 100.0
    min_edge: float = 0.05  # 5% minimum edge to trade
    max_edge: float = 0.60  # 60% max edge (model error likely)
    kelly_fraction: float = 0.15  # use 15% of full Kelly (conservative)
    min_trade_size: float = 0.50  # minimum $0.50 per trade
    max_trade_pct: float = 0.10  # max 10% of balance per trade
    max_total_exposure_pct: float = 0.50  # max 50% total exposure
    min_liquidity: float = 5000.0  # min market liquidity

    # Scan interval
    scan_interval_sec: int = 120  # scan every 2 minutes
    price_update_sec: int = 60  # update BTC price every minute

    # Web
    web_host: str = "0.0.0.0"
    web_port: int = 8877

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
