"""Live hypothesis-#2 runner - PAPER MODE BY DEFAULT.

Each minute: fetch fresh 1m candles for every registered instrument, build
v1.3.0 features + cross-asset currency-strength columns for the just-closed
bar, apply the frozen H2 model (models/h2-*.pkl, newest), gate by expected
value against the LIVE binary payout, and append one JSON line per signal
to logs/live_h2.jsonl.

--mode practice (legacy --trade) places 15-minute practice binaries.
--mode real is quarantined before configuration or model loading. New live
execution requires the platform proposal/risk path and validated providers.
The selected practice balance is checked at startup and before every buy.
Default is paper. Retained REAL internals are covered only by fake tests.
"""

import argparse
import json
import os
import pickle
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from features import build_features
from execution_mode import (PENDING_ORDER_FILE, REAL_ORDER_BLOCKED_EXIT,
                            PendingSubmissionError, reject_legacy_real_mode, resolve_execution)
from instruments import INSTRUMENTS
from research_pooled import currencies
from runtime_paths import ENV_FILE, LOGS_DIR, MODELS_DIR, load_env_file
from train import decide_action

CANDLE_COUNT = 560  # covers REGIME_WINDOW=480 warmup plus slack
EV_MARGIN = 0.03    # FORWARD_TEST.md hypothesis #2 primary gate - do not tune
REGISTERED_CUTOFF_TS = 1784678400  # 2026-07-22T00:00:00Z, FORWARD_TEST.md
# A healthy cycle finishes in seconds. Observed 2026-07-28: the runner wedged
# for 20+ minutes at 0% CPU with no exception, no log line and no _call
# timeout firing - blocked inside the vendored library somewhere the daemon-
# thread timeout could not reach. Only the supervisor's 57-minute relaunch
# recovered it, losing a market-hours session. This deadline bounds ANY hang,
# whatever its cause, to a couple of minutes.
CYCLE_DEADLINE_S = 300
_LAST_PROGRESS = 0.0  # epoch of the last completed cycle step
EXPIRY_MINUTES = 15
_LOCK_SOCK = None  # single-instance lock socket, bound in main()
SHUTDOWN_BUY_GRACE_S = 5.0
SHUTDOWN_RECONCILE_S = 8.0  # total budget, never per outstanding order
SHUTDOWN_CLOSE_S = 2.0


class ShutdownRequested(Exception):
    """Stop an interruptible call without pretending the broker cancelled it."""

    def __init__(self, *, call_started: bool = False):
        super().__init__("runner shutdown requested")
        self.call_started = call_started


def _check_shutdown(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise ShutdownRequested()


def _install_signal_handlers(stop_event, watchdog_stop_event=None) -> dict:
    """The handler only sets flags; normal Python code owns cleanup."""
    previous = {}

    def request_shutdown(_signum, _frame):
        stop_event.set()
        if watchdog_stop_event is not None:
            watchdog_stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, request_shutdown)
    return previous


def _call(fn, *args, timeout=60, stop_event=None, shutdown_grace=0.0, **kwargs):
    """Blocking library call on a daemon thread with a hard timeout (the
    iqoptionapi library busy-waits forever on lost replies).

    Cancelling the wait cannot cancel an in-flight library call. Orders get
    a short acknowledgement grace, and an absent acknowledgement must be
    recorded as an unknown submission, never a refused/cancelled order.
    """
    _check_shutdown(stop_event)
    box = {}
    done = threading.Event()

    def runner():
        try:
            _check_shutdown(stop_event)
            box["started"] = True
            box["value"] = fn(*args, **kwargs)
        except Exception as exc:
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=runner, daemon=True).start()
    deadline = time.monotonic() + timeout
    shutdown_deadline = None
    while not done.is_set():
        now = time.monotonic()
        if stop_event is not None and stop_event.is_set():
            if shutdown_deadline is None:
                shutdown_deadline = now + shutdown_grace
            if now >= shutdown_deadline:
                raise ShutdownRequested(call_started=bool(box.get("started")))
        remaining = deadline - now
        if remaining <= 0:
            raise TimeoutError(f"{getattr(fn, '__name__', fn)} timed out after {timeout}s")
        done.wait(min(0.1, remaining))
    if "error" in box:
        raise box["error"]
    return box["value"]


def _note_progress() -> None:
    """Mark the cycle loop as alive. Called at each step the loop reaches."""
    global _LAST_PROGRESS
    _LAST_PROGRESS = time.time()


def _start_cycle_watchdog(deadline_s: int = CYCLE_DEADLINE_S,
                          stop_event=None) -> threading.Thread:
    """Force a relaunch if the cycle loop stops making progress.

    Deliberately uses os._exit: the main thread is, by definition, blocked
    somewhere uninterruptible when this fires, so sys.exit or an exception
    would never be delivered. That skips settle_open_orders, but an order left
    open is recoverable later with settle_missing.py, whereas a wedged runner
    silently stops trading until the hourly relaunch."""
    stop_event = stop_event if stop_event is not None else threading.Event()

    def watch():
        while not stop_event.wait(30):
            if _LAST_PROGRESS and time.time() - _LAST_PROGRESS > deadline_s:
                if stop_event.is_set():
                    return
                stalled = int(time.time() - _LAST_PROGRESS)
                print(f"CYCLE WATCHDOG: no progress for {stalled}s "
                      f"(deadline {deadline_s}s) - forcing relaunch",
                      file=sys.stderr, flush=True)
                if not stop_event.is_set():
                    os._exit(1)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def _load_env() -> None:
    load_env_file(ENV_FILE)


def _real_account(client, stop_event):
    """Pin a readable account identity and currency, without assuming USD."""
    balance_id = _call(client.get_balance_id, timeout=30, stop_event=stop_event)
    currency = _call(client.get_currency, timeout=30, stop_event=stop_event)
    valid_id = ((isinstance(balance_id, int) and not isinstance(balance_id, bool)
                 and balance_id > 0)
                or (isinstance(balance_id, str) and bool(balance_id.strip())))
    if (not valid_id
            or not isinstance(currency, str) or not currency.strip()):
        raise ValueError("REAL balance identity or currency is unreadable")
    return {"balance_id": balance_id, "currency": currency}


def _sync_directory(path):
    """Persist directory entries for crash recovery on the server filesystem."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mark_submission(log_path, record):
    """Write before the broker call; never overwrite a prior unknown order."""
    marker = log_path.with_name(PENDING_ORDER_FILE)
    with marker.open("x", encoding="utf-8") as fh:
        json.dump({**record, "submission_status": "pending_acknowledgement"}, fh)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    _sync_directory(marker.parent)
    return marker


def _prepare_log_directory(path, *, durable=False):
    missing = []
    directory = path
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    path.mkdir(parents=True, exist_ok=True)
    if durable:
        for directory in reversed(missing):
            _sync_directory(directory.parent)


def _clear_submission(marker):
    marker.unlink()
    _sync_directory(marker.parent)


def settle_open_orders(client, open_orders, log_path,
                       timeout_budget=None, stop_event=None) -> None:
    """Query the broker verdict for any still-open orders and append their
    settled records. Ordinary completion preserves the existing expiry
    wait; SIGTERM/SIGINT interrupts it and switches to one shared shutdown
    deadline. Unresolved records remain in the signal log for
    settle_missing.py; no settlement is invented. An explicit budget is
    useful for callers that already know they need a bounded reconciliation.
    """
    deadline = (time.monotonic() + timeout_budget
                if timeout_budget is not None else None)
    pending = []
    for index, (order_id, record) in enumerate(open_orders):
        if deadline is None and stop_event is not None and stop_event.is_set():
            deadline = time.monotonic() + SHUTDOWN_RECONCILE_S
        timeout = EXPIRY_MINUTES * 60 + 120
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pending.extend(open_orders[index:])
                break
            timeout = remaining / (len(open_orders) - index)
        try:
            result, profit = _call(client.check_win_v4, order_id,
                                   timeout=timeout,
                                   stop_event=stop_event if deadline is None else None)
            if (result not in {"win", "loose", "equal"}
                    or isinstance(profit, bool)
                    or not isinstance(profit, (int, float))
                    or not np.isfinite(profit)):
                raise ValueError("broker did not return a completed, finite verdict")
            outcome = {**record, "result": result, "profit": profit, "settled": True}
            with open(log_path, "a") as fh:
                fh.write(json.dumps(outcome) + "\n")
        except ShutdownRequested:
            # The old call may still finish in its daemon thread. Do not
            # duplicate it or guess its result; later history can recover it.
            deadline = time.monotonic() + SHUTDOWN_RECONCILE_S
            pending.append((order_id, record))
        except Exception as exc:
            pending.append((order_id, record))
            print(f"WARN settle {order_id}: {exc}", file=sys.stderr, flush=True)
    open_orders[:] = pending
    if pending:
        print(f"{len(pending)} order(s) remain unresolved in {log_path}; "
              "recover broker verdicts later with settle_missing.py",
              file=sys.stderr, flush=True)


def _close_client(client) -> None:
    """Bound disconnect too: the optional broker library may hang here."""
    close = getattr(client, "close", None)
    if not callable(close):
        close = getattr(getattr(client, "api", None), "close", None)
    if callable(close):
        try:
            _call(close, timeout=SHUTDOWN_CLOSE_S)
        except Exception as exc:
            print(f"WARN disconnect: {exc}", file=sys.stderr, flush=True)


def load_frozen_model():
    paths = sorted(MODELS_DIR.glob("h2-*.pkl"))
    if not paths:
        raise SystemExit(f"no h2-*.pkl in {MODELS_DIR}; restore and verify the "
                         "approved frozen bundle before running")
    with open(paths[-1], "rb") as fh:
        bundle = pickle.load(fh)
    return paths[-1].name, bundle["model"], bundle["meta"]


def load_shadow_h4():
    """Optional H4 (extra-vol) SHADOW model. Never gates or trades - its
    probability is logged per signal so the shadow track can be scored later
    on identical timestamps/prices/payouts (Farxida-style parallel tracks).
    The inverted / no-meta / threshold tracks need no extra logging: they are
    derivable from p_up and meta_p already in the log."""
    paths = sorted(MODELS_DIR.glob("h4-*.pkl"))
    if not paths:
        return None
    with open(paths[-1], "rb") as fh:
        return pickle.load(fh)


def load_meta_filter():
    """Optional hypothesis-#3 meta model. Signals are NEVER gated by it here;
    its probability is logged per signal so H3 can be evaluated later."""
    path = MODELS_DIR / "meta-h3.pkl"
    if not path.exists():
        return None
    with open(path, "rb") as fh:
        return pickle.load(fh)


def meta_probability(meta_bundle, row, p: float, action: str) -> float | None:
    if meta_bundle is None:
        return None
    feats = {
        "p_up": p, "conf": abs(p - 0.5),
        "is_call": float(action == "binary_call"),
    }
    for col in meta_bundle["features"]:
        if col in feats:
            continue
        if col.startswith("pair_"):
            feats[col] = float(col == f"pair_{row['asset'].replace('-OTC', '')}")
        else:
            feats[col] = float(row[col]) if col in row.index else 0.0
    frame = pd.DataFrame([feats])[meta_bundle["features"]]
    return float(meta_bundle["model"].predict_proba(frame)[0, 1])


def normalize(raw: list) -> pd.DataFrame:
    rows = [
        {
            "from_ts": int(c["from"]), "to_ts": int(c["to"]),
            "open": float(c["open"]), "high": float(c["max"]),
            "low": float(c["min"]), "close": float(c["close"]),
            "volume": float(c.get("volume") or 0.0),
        }
        for c in raw
    ]
    return pd.DataFrame(rows).drop_duplicates("from_ts").sort_values("from_ts")


def cross_asset_columns(frame: pd.DataFrame, basket: set[str] | None):
    """(xs_base_str, xs_quote_str, xs_mkt_vol) for one timestamp snapshot,
    aggregated over the FROZEN training basket only.

    Mirrors research_pooled.add_cross_asset exactly - signed currency
    strength with each pair's own contribution excluded, and cross-sectional
    mean |ret_5| for activity - but restricted to `basket` (the model's
    meta['assets']).

    Audit 2026-07-24: this used the LIVE instrument list, which has grown
    from the frozen 16 to 39. Aggregating strength and market volatility
    over pairs the model never saw (gold, exotic OTC crosses) shifted
    xs_mkt_vol ~42%, feeding the frozen model out-of-distribution columns on
    every signal. Assets outside the basket are still SCORED - they just no
    longer contribute to, and are not self-excluded from, the aggregates.
    """
    ret5 = dict(zip(frame["asset"], frame["ret_5"]))
    contributors = {a: float(r) for a, r in ret5.items()
                    if basket is None or a in basket}

    num: dict[str, float] = {}
    cnt: dict[str, int] = {}
    for asset, r in contributors.items():
        base, quote = currencies(asset)
        for cur, sign in ((base, 1.0), (quote, -1.0)):
            num[cur] = num.get(cur, 0.0) + sign * r
            cnt[cur] = cnt.get(cur, 0) + 1

    base_str, quote_str = [], []
    for asset in frame["asset"]:
        base, quote = currencies(asset)
        own = contributors.get(asset, 0.0)
        own_n = 1 if asset in contributors else 0
        b_n, b_c = num.get(base, 0.0) - own, cnt.get(base, 0) - own_n
        q_n, q_c = num.get(quote, 0.0) + own, cnt.get(quote, 0) - own_n
        base_str.append(b_n / b_c if b_c > 0 else 0.0)
        quote_str.append(q_n / q_c if q_c > 0 else 0.0)

    mkt_vol = (float(np.mean([abs(r) for r in contributors.values()]))
               if contributors else 0.0)
    return base_str, quote_str, mkt_vol


def latest_feature_rows(client, horizon: int, basket: set[str] | None = None,
                        stop_event=None) -> pd.DataFrame:
    """One feature row per asset for the most recent CLOSED bar, with
    cross-asset currency-strength columns computed from the same snapshot."""
    per_asset = []
    for asset in INSTRUMENTS:
        _check_shutdown(stop_event)
        try:
            now = time.time()  # per-asset: early timeouts must not stale the tail
            raw = _call(client.get_candles, asset, 60, CANDLE_COUNT, now,
                        timeout=30, stop_event=stop_event)
            _check_shutdown(stop_event)
            candles = normalize(raw)
            # The newest bar may still be forming; keep only closed bars.
            candles = candles[candles["to_ts"] <= now + 1]
            ff = build_features(candles, interval=60, horizon=horizon, extra_vol=True)
            if ff.empty:
                continue
            row = ff.iloc[[-1]].copy()
            row["asset"] = asset
            per_asset.append(row)
        except ShutdownRequested:
            raise
        except Exception as exc:
            print(f"WARN {asset}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    if not per_asset:
        return pd.DataFrame()
    frame = pd.concat(per_asset, ignore_index=True)
    # Cross-asset strengths at this single timestamp, aggregated over the
    # model's frozen training basket (own pair excluded).
    base_str, quote_str, mkt_vol = cross_asset_columns(frame, basket)
    frame["xs_base_str"] = base_str
    frame["xs_quote_str"] = quote_str
    frame["xs_mkt_vol"] = mkt_vol
    return frame


def live_quotes(client, stop_event=None) -> dict:
    """Payout table only. get_all_open_time is NOT used: the vendored
    library's digital-options sub-fetch crashes internally and the call
    times out every time. Openness is inferred from candle freshness
    instead - a market that closed a bar in the last 3 minutes is trading,
    and a closed market cannot produce fresh bars."""
    return _call(client.get_all_profit, timeout=90, stop_event=stop_event)


def _run(args, client, stop_event, watchdog_stop_event, open_orders, log_path) -> int:
    _check_shutdown(stop_event)
    execution = args.execution
    model_name, model, meta = load_frozen_model()
    meta_bundle = load_meta_filter()
    h4_bundle = load_shadow_h4()
    horizon = meta["horizon_bars"]
    feature_cols = meta["feature_columns"]
    # Cross-asset columns must be aggregated over the basket the model was
    # trained on, not today's (larger) live instrument list - see
    # cross_asset_columns. Taken from the bundle so it self-corrects for any
    # future model.
    basket = set(meta.get("assets") or []) or None
    print(f"model={model_name} rows={meta['rows']} data_end={meta['data_end_ts']}", flush=True)
    # Visibility, not enforcement: the evaluator REFUSES a post-cutoff model,
    # but the live track is a measurement and halting it would cost more than
    # it protects. Say so loudly every session instead (audit 2026-07-24).
    end = meta.get("data_end_ts")
    if end is not None and end > REGISTERED_CUTOFF_TS:
        print(f"WARN {model_name} trained through {end}, "
              f"{(end - REGISTERED_CUTOFF_TS) / 3600:.2f}h PAST the registered "
              f"cutoff {REGISTERED_CUTOFF_TS} - forward_eval will refuse this "
              "bundle; see FORWARD_TEST.md 'SECOND pre-verdict audit'",
              file=sys.stderr, flush=True)
    if basket:
        extra = len(INSTRUMENTS) - len(basket & set(INSTRUMENTS))
        print(f"cross-asset basket: {len(basket)} frozen assets "
              f"({extra} live instruments scored but not contributing)",
              flush=True)
    else:
        print("WARN model bundle has no 'assets' - cross-asset columns fall "
              "back to the full live basket", file=sys.stderr, flush=True)

    ok, reason = _call(client.connect, timeout=90, stop_event=stop_event)
    if not ok:
        raise SystemExit(f"login failed: {reason}")
    _call(client.change_balance, execution.balance_mode, stop_event=stop_event)
    if _call(client.get_balance_mode, stop_event=stop_event) != execution.balance_mode:
        raise SystemExit(f"refusing to run: balance mode is not {execution.balance_mode}")
    account = _real_account(client, stop_event) if execution.mode == "real" else {}
    attribution = {"execution_mode": execution.mode,
                   "balance_mode": execution.balance_mode, **account}
    print(f"execution={execution.mode} balance={execution.balance_mode} "
          f"amount={execution.amount} currency={account.get('currency', 'account currency')}",
          flush=True)

    _prepare_log_directory(log_path.parent, durable=execution.mode == "real")
    deadline = time.time() + args.minutes * 60
    consecutive_failures = 0

    _note_progress()
    _start_cycle_watchdog(stop_event=watchdog_stop_event)

    while not stop_event.is_set() and time.time() < deadline:
        # Wait for the next minute boundary + 2s so the bar is fully closed.
        pause = max(0.0, 60 - time.time() % 60) + 2
        if stop_event.wait(min(pause, max(0.0, deadline - time.time()))):
            break
        if time.time() >= deadline:
            break
        cycle_ts = int(time.time())
        _note_progress()
        try:
            frame = latest_feature_rows(client, horizon, basket, stop_event=stop_event)
            _check_shutdown(stop_event)
            if frame.empty:
                # A fully dead connection lands here for every cycle; the
                # old code skipped both the heartbeat and the failure
                # counter, defeating the relaunch design (audit live-H1).
                with open(log_path.with_name("live_h2_heartbeat.jsonl"), "a") as fh:
                    fh.write(json.dumps({**attribution, "ts": int(time.time()), "assets": 0,
                                         "max_conf": None, "signals": 0}) + "\n")
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    print("5 consecutive empty cycles - exiting for relaunch",
                          file=sys.stderr, flush=True)
                    sys.exit(1)
                continue
            profits = live_quotes(client, stop_event=stop_event)
            _check_shutdown(stop_event)
            p_up = model.predict_proba_up(frame[feature_cols])
            fired = 0
            for (_, row), p in zip(frame.iterrows(), p_up):
                _check_shutdown(stop_event)
                asset = row["asset"]
                spec = INSTRUMENTS[asset]
                quote = profits.get(spec.quote_key, {})
                payout = quote.get("binary")
                if not isinstance(payout, (int, float)) or payout <= 0:
                    payout = quote.get(spec.option_kind)
                is_open = (int(time.time()) - int(row["to_ts"])) < 180  # fresh bar = trading
                if not isinstance(payout, (int, float)) or not is_open:
                    continue
                action = decide_action(float(p), float(payout), EV_MARGIN)
                if action == "no_trade":
                    continue
                meta_p = meta_probability(meta_bundle, row, float(p), action)
                h4_p = None
                if h4_bundle is not None:
                    try:
                        cols = h4_bundle["meta"]["feature_columns"]
                        h4_p = float(h4_bundle["model"].predict_proba_up(
                            frame.loc[[row.name], cols]
                        )[0])
                    except Exception:
                        pass  # shadow track must never break the primary
                record = {
                    **attribution,
                    "ts": int(time.time()),  # true wall-clock at decision (audit M4)
                    "bar_to_ts": int(row["to_ts"]),
                    "asset": asset,
                    "action": action,
                    "p_up": round(float(p), 6),
                    "meta_p": round(meta_p, 4) if meta_p is not None else None,
                    "payout": float(payout),
                    "ev_margin": EV_MARGIN,
                    "model": model_name,
                    "mode": ("real" if execution.mode == "real" else
                             "trade" if execution.orders_enabled else "paper"),
                    "amount": execution.amount,
                    # shadow + execution attribution
                    "h4_p": round(h4_p, 6) if h4_p is not None else None,
                    "decision_latency_s": int(time.time()) - int(row["to_ts"]),
                }
                submission_failed = False
                marker = None
                # OTC EXCLUSION (2026-07-24, research_otc.py): pre-cutoff
                # broker data shows OTC at 47.1% independent win rate -
                # below break-even and below coin flip, while spot runs
                # 65.5%. The broker synthesises OTC prices, and they resist
                # the model. Orders are placed on spot only; paper
                # logging (and the frozen hypothesis evaluation) is
                # untouched and still covers every instrument.
                if execution.orders_enabled and asset.endswith("-OTC"):
                    record["trade_skipped"] = "otc_below_breakeven"
                elif execution.orders_enabled:
                    # Reconnects can change the selected balance. Verify it
                    # again per buy and never silently switch or fall back.
                    account_error = None
                    try:
                        if execution.mode == "real":
                            execution.assert_ready(LOGS_DIR)
                            if _real_account(client, stop_event) != account:
                                raise ValueError("REAL account identity or currency changed")
                        mode = _call(client.get_balance_mode, timeout=30,
                                     stop_event=stop_event)
                    except ShutdownRequested:
                        raise
                    except Exception as exc:
                        mode = f"unreadable:{type(exc).__name__}"
                        account_error = str(exc)
                    record["observed_balance_mode"] = mode
                    if mode != execution.balance_mode:
                        record["trade_skipped"] = f"balance_mode_{mode}"
                        if account_error:
                            record["account_error"] = account_error
                        print(f"REFUSING ORDER {asset}: balance mode is "
                              f"{mode!r}, expected {execution.balance_mode}",
                              file=sys.stderr, flush=True)
                    else:
                        if execution.mode == "real":
                            _check_shutdown(stop_event)
                            record["submission_id"] = uuid.uuid4().hex
                            marker = _mark_submission(log_path, record)
                        try:
                            ok, order_id = _call(
                                client.buy, execution.amount, spec.order_active,
                                "call" if action == "binary_call" else "put",
                                EXPIRY_MINUTES, timeout=60, stop_event=stop_event,
                                shutdown_grace=SHUTDOWN_BUY_GRACE_S,
                            )
                            if not isinstance(ok, bool) or (
                                ok and (isinstance(order_id, bool)
                                        or not isinstance(order_id, (str, int))
                                        or not order_id)
                            ):
                                raise ValueError("invalid broker acknowledgement")
                        except Exception as exc:
                            record["order_id"] = None
                            submission_failed = not isinstance(exc, ShutdownRequested)
                            if submission_failed or exc.call_started:
                                record["order_submission_unknown"] = True
                                record["order_error"] = (
                                    "shutdown_before_acknowledgement"
                                    if isinstance(exc, ShutdownRequested) else
                                    "acknowledgement_timeout" if isinstance(exc, TimeoutError)
                                    else f"acknowledgement_error:{type(exc).__name__}"
                                )
                                print(f"WARN {asset}: order acknowledgement absent; "
                                      "submission status UNKNOWN. Inspect broker history "
                                      "before resuming; this does not mean cancelled.",
                                      file=sys.stderr, flush=True)
                            else:
                                record["trade_skipped"] = "shutdown_requested"
                        else:
                            record["order_id"] = order_id if ok else None
                            if not ok:
                                record["order_error"] = str(order_id)
                            else:
                                open_orders.append((order_id, record))
                with open(log_path, "a") as fh:
                    fh.write(json.dumps(record) + "\n")
                    if "order_id" in record:
                        fh.flush()
                        os.fsync(fh.fileno())
                if marker is not None and not record.get("order_submission_unknown"):
                    # Clear only after the acknowledgement or proven
                    # not-started outcome is durable. Crashes leave a block.
                    # Persist a newly created log's name before unlinking
                    # the marker; a file fsync alone does not persist it.
                    _sync_directory(log_path.parent)
                    _clear_submission(marker)
                print(json.dumps(record), flush=True)
                fired += 1
                if submission_failed or (execution.mode == "real"
                                         and record.get("order_submission_unknown")):
                    # The normal call deadline may win the race with a
                    # shutdown signal. Preserve the unknown submission in
                    # either case, then end this session without more buys.
                    stop_event.set()
                    return REAL_ORDER_BLOCKED_EXIT if execution.mode == "real" else 1
            _check_shutdown(stop_event)
            # FULL CROSS-SECTION (2026-07-26). Only signals clearing the
            # frozen EV gate reach live_h2.jsonl, so no lower gate, wider
            # universe or different meta threshold can EVER be evaluated on
            # real-time probabilities - only re-derived from candles, which
            # loses the leak-immunity that makes the live track valuable.
            # Recording every asset's probability each cycle makes all of those
            # variants derivable AFTER the fact from one live run, which is the
            # only way to evaluate many strategies without trading them, and
            # without paying a multiplicity cost for hypotheses nobody tested.
            # Separate file: live_h2.jsonl is a verdict input and is untouched.
            try:
                cross = {
                    **attribution,
                    "ts": cycle_ts,
                    "model": model_name,
                    "rows": [],
                }
                for (_, row), p in zip(frame.iterrows(), p_up):
                    a = row["asset"]
                    spec_x = INSTRUMENTS[a]
                    quote_x = profits.get(spec_x.quote_key, {})
                    pay_x = quote_x.get("binary")
                    if not isinstance(pay_x, (int, float)) or pay_x <= 0:
                        pay_x = quote_x.get(spec_x.option_kind)
                    cross["rows"].append({
                        "a": a,
                        "p": round(float(p), 6),
                        "bar": int(row["to_ts"]),
                        "pay": float(pay_x) if isinstance(pay_x, (int, float)) else None,
                        "open": (int(time.time()) - int(row["to_ts"])) < 180,
                    })
                with open(log_path.with_name("cross_section.jsonl"), "a") as fh:
                    fh.write(json.dumps(cross) + "\n")
            except Exception as exc:  # never let telemetry break trading
                print(f"WARN cross-section: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)

            # Heartbeat: an idle cycle and a broken one look identical in the
            # signal log, so record that the model really did evaluate.
            with open(log_path.with_name("live_h2_heartbeat.jsonl"), "a") as fh:
                fh.write(json.dumps({
                    **attribution,
                    "ts": cycle_ts,
                    "assets": len(frame),
                    "max_conf": round(float(np.max(np.abs(p_up - 0.5))), 4),
                    "signals": fired,
                }) + "\n")
            consecutive_failures = 0
        except ShutdownRequested:
            raise
        except Exception as exc:
            print(f"WARN cycle: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if execution.mode == "real" and os.path.lexists(execution.pending_path(LOGS_DIR)):
                stop_event.set()
                return REAL_ORDER_BLOCKED_EXIT
            consecutive_failures += 1
            # A dead websocket/network wastes the whole slot erroring; bail
            # nonzero so launchd (KeepAlive SuccessfulExit=false) relaunches
            # immediately with a fresh connection instead of at the next hour.
            if consecutive_failures >= 5:
                print("5 consecutive cycle failures - exiting for relaunch",
                      file=sys.stderr, flush=True)
                sys.exit(1)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=60, help="how long to run")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--mode", choices=("paper", "practice", "real"), default="paper")
    modes.add_argument("--trade", action="store_const", const="practice", dest="mode",
                       help="legacy alias for --mode practice")
    parser.add_argument("--amount", type=float,
                        help="stake in account currency; required explicitly for real mode")
    args = parser.parse_args()
    try:
        reject_legacy_real_mode(args.mode)
    except ValueError as exc:
        parser.error(str(exc))
    if args.minutes <= 0:
        parser.error("--minutes must be positive")
    _load_env()
    try:
        args.execution = resolve_execution(args.mode, args.amount)
        args.execution.assert_ready(LOGS_DIR)
    except PendingSubmissionError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr, flush=True)
        return REAL_ORDER_BLOCKED_EXIT
    except ValueError as exc:
        parser.error(str(exc))

    import socket

    global _LOCK_SOCK
    stop_event = threading.Event()
    watchdog_stop_event = threading.Event()
    previous_handlers = _install_signal_handlers(stop_event, watchdog_stop_event)
    client = None
    open_orders = []
    log_path = args.execution.log_dir(LOGS_DIR) / "live_h2.jsonl"
    try:
        _check_shutdown(stop_event)
        # This lock coordinates processes on one host only. Account handover
        # across servers needs an explicit single active host.
        _LOCK_SOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _LOCK_SOCK.bind(("127.0.0.1", 47201))
        except OSError:
            print("another runner instance holds the lock - exiting cleanly", flush=True)
            return 0

        from iqoptionapi.stable_api import IQ_Option

        _check_shutdown(stop_event)
        client = IQ_Option(os.environ["IQ_EMAIL"], os.environ["IQ_PASSWORD"])
        return _run(args, client, stop_event, watchdog_stop_event, open_orders, log_path)
    except ShutdownRequested:
        print("shutdown requested - stopping new decisions and orders", flush=True)
        return 0
    finally:
        # Also stops the hard-exit watchdog during reconciliation. Already
        # running broker calls cannot be force-cancelled safely in Python.
        watchdog_stop_event.set()
        try:
            if client is not None:
                try:
                    settle_open_orders(client, open_orders, log_path,
                                       stop_event=stop_event)
                finally:
                    stop_event.set()
                    _close_client(client)
        finally:
            stop_event.set()
            try:
                if _LOCK_SOCK is not None:
                    _LOCK_SOCK.close()
                    _LOCK_SOCK = None
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
        print(f"runner cleanup complete {datetime.now(timezone.utc).isoformat()}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
