"""Independent causal research rules. No broker imports, learned odds, or fusion."""

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Protocol

from atlas.validation import validate_chronology


def finite(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("boolean is not numeric data")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("invalid numeric data") from exc
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError("nonfinite or nonpositive numeric data")
    return value


def validate_bars(bars, *, now=None, require_open=False):
    """Copy closed bars. timestamp/to_ts means bar close, never bar open.

    This validates supplied data; it cannot establish provider authenticity or
    corporate-action adjustment. Gaps are retained and explicitly disclosed.
    """
    result = []
    for bar in bars:
        timestamp = bar.get("timestamp", bar.get("to_ts"))
        row = {"timestamp": timestamp, "close": finite(bar["close"], positive=True)}
        for key in ("open", "high", "low", "volume"):
            if key in bar:
                row[key] = finite(bar[key], positive=key != "volume")
        if row.get("volume", 0) < 0:
            raise ValueError("negative volume")
        if require_open and "open" not in row:
            raise ValueError("backtests require the observed next bar open")
        if "high" in row and row["high"] < max(row["close"], row.get("open", row["close"])):
            raise ValueError("high is below open or close")
        if "low" in row and row["low"] > min(row["close"], row.get("open", row["close"])):
            raise ValueError("low is above open or close")
        result.append(row)
    validate_chronology([row["timestamp"] for row in result], now=now)
    return result


class Strategy(Protocol):
    strategy_id: str
    version: str
    lookback: int
    asset_classes: tuple[str, ...]

    def evaluate(self, closes: list[float]) -> tuple[str, float, str]: ...
    def descriptor(self) -> dict: ...


@dataclass(frozen=True)
class Baseline:
    strategy_id: str
    lookback: int = 20
    version: str = "1.0.0"
    asset_classes: tuple[str, ...] = ("equity", "crypto")

    def __post_init__(self):
        if self.strategy_id not in {"momentum", "mean_reversion"}:
            raise ValueError("unknown baseline")
        if isinstance(self.lookback, bool) or not isinstance(self.lookback, int) or self.lookback < 2:
            raise ValueError("lookback must be an integer >= 2")

    def evaluate(self, closes):
        if len(closes) < self.lookback + 1:
            raise ValueError("insufficient history")
        if self.strategy_id == "momentum":
            score = closes[-1] / closes[-1 - self.lookback] - 1
            reason = f"Trailing {self.lookback}-bar close return; positive means rising."
        else:
            prior_mean = sum(closes[-1 - self.lookback:-1]) / self.lookback
            score = prior_mean / closes[-1] - 1
            reason = f"Distance below the prior {self.lookback}-bar mean; positive means below mean."
        direction = "buy" if score > 0 else "sell" if score < 0 else "hold"
        return direction, score, reason

    def descriptor(self):
        return {
            "strategy_id": self.strategy_id, "version": self.version,
            "asset_classes": list(self.asset_classes), "lookback_bars": self.lookback,
            "horizon": {"unit": "bar", "value": 1},
            "evidence_status": "unvalidated_research_rule", "live_validated": False,
            "probability_calibrated": False, "score_units": "fractional_price_distance",
        }


class StrategyRegistry:
    def __init__(self, strategies=None):
        self._strategies = {}
        self._enabled = {}
        for strategy in (strategies if strategies is not None else [Baseline("momentum"), Baseline("mean_reversion")]):
            self.register(strategy)

    def register(self, strategy: Strategy):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", strategy.strategy_id):
            raise ValueError("invalid strategy id")
        if strategy.strategy_id in self._strategies:
            raise ValueError("strategy already registered")
        self._strategies[strategy.strategy_id] = strategy
        self._enabled[strategy.strategy_id] = True

    def set_enabled(self, strategy_id, enabled):
        if strategy_id not in self._strategies or not isinstance(enabled, bool):
            raise ValueError("unknown strategy or invalid enabled flag")
        self._enabled[strategy_id] = enabled

    def list_strategies(self):
        return [dict(s.descriptor(), enabled=self._enabled[key]) for key, s in self._strategies.items()]

    def analyze(self, symbol, asset_class, bars, *, now):
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-/]{0,19}", symbol):
            raise ValueError("invalid normalized symbol")
        if asset_class not in {"equity", "crypto", "fx_binary"}:
            raise ValueError("unsupported asset class")
        validate_chronology([now])
        rows = validate_bars(bars, now=now)
        data_hash = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        closes = [r["close"] for r in rows]
        signals, unavailable = [], []
        for key, strategy in self._strategies.items():
            if not self._enabled[key]:
                unavailable.append({"strategy_id": key, "reason": "disabled"})
                continue
            if asset_class not in strategy.asset_classes:
                unavailable.append({"strategy_id": key, "reason": "unsupported_asset_class"})
                continue
            if len(rows) < strategy.lookback + 1:
                unavailable.append({"strategy_id": key, "reason": "insufficient_history"})
                continue
            direction, score, reason = strategy.evaluate(closes)
            if direction not in {"buy", "sell", "hold"}:
                raise ValueError("invalid strategy direction")
            signal = {
                "strategy_id": key, "version": strategy.version,
                "symbol": symbol, "asset_class": asset_class,
                "direction": direction, "score": finite(score),
                "score_units": "fractional_price_distance",
                "confidence": None, "probability": None, "expected_return": None,
                "horizon": {"unit": "bar", "value": 1},
                "data_timestamp": rows[-1]["timestamp"], "computed_at": now,
                "data_hash": data_hash, "evidence": "unvalidated_research_rule",
                "reasons": [reason], "invalidation": ["new closed bar", "changed input data", "strategy disabled"],
                "limitations": ["Not calibrated odds or a return forecast.",
                    "Research only; sell means reduce a long position, not permission to short.",
                    "Provider authenticity, stale data, gaps and corporate actions require separate checks."],
                "live_validated": False,
            }
            identity = {k: v for k, v in signal.items() if k != "computed_at"}
            signal["signal_id"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            signals.append(signal)
        return {"symbol": symbol, "asset_class": asset_class, "signals": signals,
                "unavailable": unavailable, "fusion": None, "live_validated": False,
                "data_timestamp": rows[-1]["timestamp"] if rows else None}
