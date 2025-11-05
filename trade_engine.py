from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Optional

from alpaca_trade_api import REST

LOGGER = logging.getLogger(__name__)


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


class TradeEngine:
    def __init__(
        self,
        daily_budget: float,
        per_trade_budget: float,
        max_trades: int,
        stop_loss_percent: float,
        take_profit_percent: float,
        max_positions: int,
        drawdown_limit_percent: float,
        pnl_log_file: str,
    ) -> None:
        self.daily_budget = daily_budget
        self.per_trade_budget = per_trade_budget
        self.max_trades = max_trades
        self.stop_loss = stop_loss_percent
        self.take_profit = take_profit_percent
        self.max_positions = max_positions
        self.drawdown_limit_percent = drawdown_limit_percent
        self.pnl_log_file = pnl_log_file

        live_mode = _bool_env("LIVE_MODE")
        base_url = "https://api.alpaca.markets" if live_mode else "https://paper-api.alpaca.markets"
        api_key = os.getenv("APCA_API_KEY_ID")
        api_secret = os.getenv("APCA_API_SECRET_KEY")
        if api_key and api_secret:
            self.api: Optional[REST] = REST(api_key, api_secret, base_url=base_url)
        else:
            LOGGER.warning("Alpaca credentials missing – trading disabled")
            self.api = None

        self.trade_stats = {
            "date": str(date.today()),
            "used_capital": 0.0,
            "trades": 0,
            "pnl": 0.0,
            "stopped": False,
        }

    def reset_if_new_day(self) -> None:
        today = str(date.today())
        if self.trade_stats["date"] != today:
            LOGGER.info("Resetting daily trade statistics")
            self.trade_stats = {
                "date": today,
                "used_capital": 0.0,
                "trades": 0,
                "pnl": 0.0,
                "stopped": False,
            }

    def get_status(self) -> dict:
        status = dict(self.trade_stats)
        if self.api:
            try:
                account = self.api.get_account()
                status["equity"] = float(account.equity)
            except Exception as exc:
                LOGGER.warning("Could not fetch account equity: %s", exc)
        return status

    def _log_pnl(self) -> None:
        if not self.api:
            return
        try:
            account = self.api.get_account()
            daily_pnl = float(account.equity) - float(account.last_equity)
            self.trade_stats["pnl"] = daily_pnl
            if daily_pnl < -self.daily_budget * self.drawdown_limit_percent:
                self.trade_stats["stopped"] = True
                LOGGER.info("Trading stopped because drawdown exceeded limit")

            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "stats": self.trade_stats.copy(),
            }
            with open(self.pnl_log_file, "a", encoding="utf-8") as f:
                json.dump(entry, f)
                f.write("\n")
        except Exception as exc:
            LOGGER.warning("PnL logging failed: %s", exc)

    def _can_trade(self, trade_value: float) -> Optional[dict]:
        if self.trade_stats["stopped"]:
            return {"error": "Daily drawdown limit reached", "stats": self.trade_stats}
        if self.trade_stats["trades"] >= self.max_trades:
            return {"error": "Daily trade count exceeded", "stats": self.trade_stats}
        if self.trade_stats["used_capital"] + trade_value > self.daily_budget:
            return {"error": "Daily limit reached", "stats": self.trade_stats}
        if self.api:
            try:
                positions = self.api.list_positions()
                if len(positions) >= self.max_positions:
                    return {"error": "Max concurrent positions reached", "stats": self.trade_stats}
            except Exception as exc:
                LOGGER.warning("Could not fetch positions: %s", exc)
        return None

    def attempt_trade(self, symbol: str, price: float) -> dict:
        self.reset_if_new_day()
        if not self.api:
            return {"status": "simulated", "reason": "No Alpaca credentials"}

        qty = max(1, int(self.per_trade_budget // price))
        trade_value = price * qty
        if qty <= 0 or trade_value > self.per_trade_budget:
            return {"error": "Trade value exceeds per-trade budget", "stats": self.trade_stats}

        rejection = self._can_trade(trade_value)
        if rejection:
            return rejection

        tp = round(price * (1 + self.take_profit), 2)
        sl = round(price * (1 - self.stop_loss), 2)

        try:
            self.api.submit_order(
                symbol=symbol,
                qty=qty,
                side="buy",
                type="limit",
                limit_price=price,
                time_in_force="gtc",
                take_profit={"limit_price": tp},
                stop_loss={"stop_price": sl},
            )
            LOGGER.info("Submitted trade", extra={"symbol": symbol, "qty": qty, "price": price})
        except Exception as exc:
            LOGGER.error("Trade failed for %s: %s", symbol, exc)
            return {"error": str(exc)}

        self.trade_stats["used_capital"] += trade_value
        self.trade_stats["trades"] += 1
        self._log_pnl()

        return {
            "status": "order placed",
            "symbol": symbol,
            "qty": qty,
            "price": round(price, 4),
            "take_profit": tp,
            "stop_loss": sl,
            "stats": self.trade_stats,
        }
