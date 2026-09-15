"""Read-only equity readiness observations, without a journal or order authority.

A passing report is a point-in-time account/data check, not proof that Alpaca
will accept an order. Existing crypto pilot budgets and state are untouched.
"""
from decimal import Decimal
import re
import time

from atlas.domain import financial_context
from atlas.paper.broker import AlpacaPaperAPI, PaperBrokerError
from atlas.paper.models import PaperError, amount, timestamp
from atlas.paper.risk import PaperRiskPolicy

MAX_SYMBOLS = 10
FINGERPRINT_NAMESPACE = "alpaca-paper"


@financial_context
def collect_preflight(api, *, allocated_principal="100", symbols=("SPY", "QQQ", "GLD", "USO"), clock=time.time):
    """Use bounded GETs only; never create state, bind a gate or prepare orders.

Freshness/spread thresholds are the default execution policy's conservative
thresholds. A later engine run applies its actual bound policy and fresh reads.
An injected API always produces TEST evidence, even when its responses pass.
"""
    if type(api) is not AlpacaPaperAPI or not callable(clock):
        raise PaperError("paper_preflight_configuration_invalid")
    principal = amount(allocated_principal, positive=True)
    if principal > 100:
        raise PaperError("paper_preflight_principal_limit")
    if (type(symbols) not in (tuple, list) or not 1 <= len(symbols) <= MAX_SYMBOLS
            or any(type(symbol) is not str or not re.fullmatch(r"[A-Z]{1,8}(?:\.[A-Z])?", symbol) for symbol in symbols)
            or len(set(symbols)) != len(symbols)):
        raise PaperError("paper_preflight_symbols_invalid")
    policy = PaperRiskPolicy()
    started = timestamp(clock())
    last_time = started
    report = {
        "schema_version": 1, "report_kind": "paper_equity_read_only_preflight",
        "provenance": api.provenance, "account_read_verified": False,
        "account_fingerprint": None, "account_fingerprint_namespace": FINGERPRINT_NAMESPACE,
        "requested_symbols": list(symbols),
        "allocated_principal": str(principal), "account": None, "account_ready": False,
        "account_checks": {}, "account_blockers": [], "history_counts": None,
        "market_state": "UNAVAILABLE", "market_clock": None, "market_clock_fresh": False,
        "assets": {}, "market_data_ready": False, "read_errors": [],
        "execution_ready": False, "order_permission_verified": False,
        "actual_orders_submitted": 0, "account_mutations": 0, "journal_created": False,
        "started_at": started, "observed_at": started, "observation_span_seconds": 0,
        "thresholds": {"max_snapshot_age_seconds": policy.max_snapshot_age_seconds,
                       "max_quote_age_seconds": policy.max_quote_age_seconds,
                       "max_spread_bps": str(policy.max_spread_bps)},
        "limitations": [
            "Read-only observations do not prove broker order permission or a successful lifecycle.",
            "Current empty history and cash do not prove lifetime funding or strategy qualification.",
            "Asset fractionable does not verify the account fractional-trading setting.",
            "IEX quotes cover one venue and are not a consolidated market quote.",
            "The older crypto preflight used the different alpaca_paper fingerprint namespace.",
            "This report creates no journal and never resets the prior halted pilot.",
        ],
    }
    sweep_start = sweep_end = first_account = final_account = None

    def now():
        nonlocal last_time
        observed = timestamp(clock())
        if observed < last_time:
            raise PaperError("paper_preflight_clock_rewind")
        last_time = observed
        return observed

    def read(step, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except PaperBrokerError as exc:
            code = exc.code if type(exc.code) is str and re.fullmatch(r"paper_[a-z0-9_]{1,100}", exc.code) else "paper_preflight_read_failed"
            report["read_errors"].append({"step": step, "code": code})
            raise

    try:
        sweep_start = now()
        first_account = read("account_initial", api.account)
        report["account"] = first_account
        report["account_fingerprint"] = first_account["fingerprint"]
        report["account_read_verified"] = api.provenance == "PAPER_API"
        positions = read("positions", api.positions)
        orders = read("all_orders", api.orders, status="all")
        activities = read("all_activities", api.activities)
        final_account = read("account_final", api.account)
        report["account"] = final_account
        report["account_fingerprint"] = final_account["fingerprint"]
        sweep_end = now()
        report["history_counts"] = {"positions": len(positions), "orders": len(orders), "activities": len(activities)}
        report["account_checks"] = {
            "account_stable": {k: v for k, v in first_account.items() if k != "observed_at"}
                              == {k: v for k, v in final_account.items() if k != "observed_at"},
            "complete_empty_positions": not positions, "complete_empty_order_history": not orders,
            "complete_empty_activity_history": not activities,
            "account_active": final_account["status"] == "ACTIVE" and final_account["blocked"] is False,
            "usd_account": final_account["currency"] == "USD",
            "multiplier_one": Decimal(final_account["multiplier"]) == 1,
            "no_market_exposure": Decimal(final_account["short_market_value"]) == 0 and Decimal(final_account["long_market_value"]) == 0,
            "dedicated_cash_matches_principal": Decimal(final_account["cash"]) == principal == Decimal(final_account["equity"]),
            "non_marginable_buying_power": principal <= Decimal(final_account["buying_power"]) <= Decimal(final_account["cash"]),
        }
        market_clock = read("market_clock", api.clock_status)
        report["market_clock"] = market_clock
        report["market_state"] = "OPEN" if market_clock["is_open"] else "WAIT_MARKET_OPEN"
        for symbol in symbols:
            asset = read("asset_" + symbol, api.asset, symbol)
            eligibility = {"equity": asset["asset_class"] == "us_equity", "active": asset["status"] == "active",
                           "tradable": asset["tradable"] is True}
            entry = {"asset": asset, "eligibility_checks": eligibility,
                     "equity_eligible": all(eligibility.values()),
                     "fractional_asset_eligible": all(eligibility.values()) and asset["fractionable"] is True,
                     "account_fractional_permission_verified": False,
                     "quote": None, "quote_checks": {}, "data_ready": False, "blockers": []}
            report["assets"][symbol] = entry
            if market_clock["is_open"] and entry["equity_eligible"]:
                entry["quote"] = read("quote_" + symbol, api.quote, symbol)
    except PaperBrokerError:
        # The API has already redacted the response. Stop on the first read
        # failure; in particular, do not retry 403s or rate-limit deferrals.
        pass

    observed = now()
    report["observed_at"] = observed
    report["observation_span_seconds"] = observed - started
    checks = report["account_checks"]
    checks["complete_account_sweep"] = sweep_end is not None
    checks["snapshot_fresh"] = bool(sweep_end is not None
        and sweep_start <= first_account["observed_at"] <= final_account["observed_at"] <= sweep_end <= observed
        and observed - sweep_start <= policy.max_snapshot_age_seconds)
    report["account_blockers"] = [name for name, passed in checks.items() if not passed]
    report["account_ready"] = bool(checks) and all(checks.values())
    market_clock = report["market_clock"]
    if market_clock is not None:
        report["market_clock_fresh"] = (0 <= observed - market_clock["at"] <= policy.max_snapshot_age_seconds
            and market_clock["at"] <= market_clock["observed_at"] <= observed
            and 0 <= observed - market_clock["observed_at"] <= policy.max_snapshot_age_seconds
            and market_clock["next_close"] > observed)
    for entry in report["assets"].values():
        quote = entry["quote"]
        entry["blockers"] = ["asset_" + name for name, passed in entry["eligibility_checks"].items() if not passed]
        if report["market_state"] == "WAIT_MARKET_OPEN":
            entry["blockers"].append("wait_market_open")
        if not report["market_clock_fresh"]:
            entry["blockers"].append("market_clock_not_fresh")
        if quote is not None:
            bid, ask = Decimal(quote["bid"]), Decimal(quote["ask"])
            entry["quote_checks"] = {
                "quote_fresh": 0 <= observed - quote["at"] <= policy.max_quote_age_seconds
                               and quote["at"] <= quote["observed_at"] <= observed,
                "spread": (ask - bid) / ((ask + bid) / 2) * 10000 <= policy.max_spread_bps,
                "positive_liquidity": Decimal(quote["bid_size"]) > 0 and Decimal(quote["ask_size"]) > 0,
            }
            entry["blockers"].extend(name for name, passed in entry["quote_checks"].items() if not passed)
        elif report["market_state"] != "WAIT_MARKET_OPEN":
            entry["blockers"].append("quote_not_observed")
        entry["data_ready"] = not entry["blockers"] and quote is not None
    report["market_data_ready"] = (not report["read_errors"] and len(report["assets"]) == len(symbols)
                                   and all(entry["data_ready"] for entry in report["assets"].values()))
    return report
