"""BTC Sniper Web Dashboard — terminal-style UI with WebSocket."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from config import settings
from sniper.engine import SniperEngine
from sniper.portfolio import Portfolio

logger = logging.getLogger(__name__)

app = FastAPI(title="BTC Sniper")

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Global state
ws_clients: set[WebSocket] = set()
engine: SniperEngine | None = None
_loop: asyncio.AbstractEventLoop | None = None


async def broadcast(event: str, data: dict | str) -> None:
    msg = json.dumps(
        {"event": event, "data": data, "ts": datetime.now(tz=timezone.utc).isoformat()}
    )
    dead: set[WebSocket] = set()
    for client in ws_clients:
        try:
            await client.send_text(msg)
        except Exception:
            dead.add(client)
    ws_clients.difference_update(dead)


def sync_broadcast(event: str, data: dict | str) -> None:
    if _loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(broadcast(event, data), _loop)
    except RuntimeError:
        pass


def _engine_callback(event: str, data: dict) -> None:
    sync_broadcast(event, data)


@app.on_event("startup")
async def _startup() -> None:
    global engine, _loop
    _loop = asyncio.get_running_loop()

    portfolio = Portfolio.load()
    engine = SniperEngine(portfolio)
    engine.listeners.append(_engine_callback)

    # Auto-start scanning
    asyncio.create_task(_scan_loop())
    asyncio.create_task(_price_loop())
    asyncio.create_task(_resolution_loop())
    logger.info("BTC Sniper started | Balance: $%.2f", portfolio.balance)


async def _scan_loop() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, engine.scan)
        except Exception as e:
            logger.error("Scan error: %s", e)
        await asyncio.sleep(settings.scan_interval_sec)


async def _price_loop() -> None:
    await asyncio.sleep(2)
    while True:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, engine.update_volatility)
        except Exception as e:
            logger.error("Price update error: %s", e)
        await asyncio.sleep(settings.price_update_sec)


async def _resolution_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, engine.check_resolutions)
        except Exception as e:
            logger.error("Resolution check error: %s", e)
        await asyncio.sleep(60)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    ws_clients.add(ws)
    try:
        # Send current state
        if engine:
            await ws.send_text(
                json.dumps(
                    {
                        "event": "portfolio",
                        "data": engine.portfolio.get_summary(),
                        "ts": datetime.now(tz=timezone.utc).isoformat(),
                    }
                )
            )
            if engine.vol_data:
                await ws.send_text(
                    json.dumps(
                        {
                            "event": "price",
                            "data": {
                                "btc_price": engine.vol_data.current_price,
                                "daily_vol": round(
                                    engine.vol_data.daily_volatility * 100, 3
                                ),
                            },
                            "ts": datetime.now(tz=timezone.utc).isoformat(),
                        }
                    )
                )
            # Send equity curve
            await ws.send_text(
                json.dumps(
                    {
                        "event": "equity_curve",
                        "data": engine.portfolio.equity_curve[-200:],
                        "ts": datetime.now(tz=timezone.utc).isoformat(),
                    }
                )
            )
            # Send recent trades
            recent = engine.portfolio.trades[-50:]
            await ws.send_text(
                json.dumps(
                    {
                        "event": "trades_history",
                        "data": [_trade_to_dict(t) for t in reversed(recent)],
                        "ts": datetime.now(tz=timezone.utc).isoformat(),
                    }
                )
            )
        while True:
            msg = await ws.receive_text()
            if msg == "scan":
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, engine.scan)
            elif msg == "reset":
                engine.portfolio = Portfolio(
                    initial_balance=settings.initial_balance,
                    balance=settings.initial_balance,
                )
                engine.portfolio.save()
                engine.scan_count = 0
                await broadcast("portfolio", engine.portfolio.get_summary())
                await broadcast("equity_curve", [])
                await broadcast("trades_history", [])
                await broadcast(
                    "log", "Portfolio reset to $%.2f" % settings.initial_balance
                )
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)


def _trade_to_dict(t) -> dict:
    return {
        "trade_id": t.trade_id,
        "question": t.question,
        "side": t.side,
        "entry_price": t.entry_price,
        "size_usd": t.size_usd,
        "quantity": round(t.quantity, 2),
        "model_prob": t.model_prob,
        "market_prob": t.market_prob,
        "edge": t.edge,
        "result": t.result,
        "payout": t.payout,
        "pnl": round(t.pnl, 2),
        "cost": t.size_usd,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (TEMPLATES_DIR / "terminal.html").read_text()
    return HTMLResponse(html)


@app.get("/api/portfolio")
async def api_portfolio():
    return engine.portfolio.get_summary() if engine else {}


@app.get("/api/trades")
async def api_trades():
    if not engine:
        return []
    return [_trade_to_dict(t) for t in reversed(engine.portfolio.trades[-100:])]


@app.get("/api/markets")
async def api_markets():
    if not engine or not engine.last_markets:
        return []
    from dataclasses import asdict

    return [asdict(m) for m in engine.last_markets[:50]]


def start_web() -> None:
    logger.info("Starting BTC Sniper at http://localhost:%d", settings.web_port)
    uvicorn.run(app, host=settings.web_host, port=settings.web_port, log_level="info")
