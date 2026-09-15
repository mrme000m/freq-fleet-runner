#!/usr/bin/env python3
"""Round-trip pairing — grid orders → per-level round-trips (M4 core).

Two sources, one pairing engine:
  from_sqlite(path)        a freqtrade tradesv3.sqlite (dry-run / live
                            instance, or any DB with the trades+orders
                            schema subset)
  from_backtest_zip(path)   a freqtrade backtesting export zip

Order vocabulary (GridStrategy; freqtrade persists the adjust_trade_position
reason tag on Order.ft_order_tag — verified in backtest exports and the
Order schema `ft_order_tag` column):

  grid_recenter / (no _L tag, entry side)   the anchor: the initial entry
                          fills exactly ON a grid line (custom_entry_price
                          returns the line) and — per the spike's state
                          machine — is never sold by a grid_sell; it closes
                          with the trade's full exit
  grid_buy_L<i>          position-increase buy at line i
  grid_sell_L<j>         per-line TP partial sell at line j
  (exit side, no _L tag) the full exit (channel_top_exit / force_exit /
                          stoploss / roi) — closes everything still open

Pairing per CLOSED trade (m1_reconcile semantics, the M1-verified oracle):

  - grid sells pair FIFO per line with that line's grid buys, one trip per
    (buy fill, sell fill) pair — the spike buys/sells at most once per line
    per trade (the pending_sell one-shot), but multi-fill lines pair
    through the same FIFO unchanged.
  - each tp trip closes the WHOLE matched buy lot: when the sell's stake
    converts to a slightly smaller base qty (sell price > buy price), the
    leftover sliver is merged into the same trip at the trade's full-exit
    price — one trip per line keeps the ledger counts identical to the
    oracle (127 tp trips on the tuned M2 export), while `pnl_usd` stays
    exact cash.
  - unsold lines' lots and the anchor close at the trade's full-exit fill
    as `full_exit` trips (one per lot), inheriting exit_reason.
  - open (is_open) trades are skipped entirely — unrealized PnL is not
    ledger evidence; their count lands in the report.
  - surprises (never-closed lots, sells with no buy on the line, sells
    exceeding the whole position) are reported, never silently dropped;
    a sell with no line buy drains the fungible pool (exact cash) and is
    surfaced as a state-anomaly surprise.

Each trip (the ledger's "trade", WT-ledger compatible):
  {pnl_usd, close_ts, entered_at, strategy_id, line, buy_px, sell_px,
   qty, kind, exit_reason, gain_pct, synthetic, slices}

  pnl_usd   net of per-leg fees at the trade's fee_open / fee_close rates
            (sqlite trades.fee_open/fee_close; export trade fee_open/
            fee_close; 0.0 when the source carries none)
  gain_pct  100 * (sell_px / buy_px - 1) over the MATCHED part — the
            fee-less price ratio m1_reconcile reports, so acceptance
            asserts against the oracle's numbers directly
  synthetic  True for research/backtest evidence (never gates the
            sizing ladder — wt_reference.py §synthetic semantics)

`load_*` functions return (trips, report); `report` always carries:
  {"source", "source_kind", "trades_closed", "trades_open_skipped",
   "tp_trips", "full_exit_trips", "surprises": [...], "fees_modeled"}
"""
from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

GRID_TAG_RE = re.compile(r"grid_(buy|sell)_L(\d+)$")

# qty below this is rounding dust (freqtrade amounts are precision-rounded)
_QTY_EPS = 1e-12

__all__ = [
    "GRID_TAG_RE", "pair_trades", "pair_trade",
    "from_sqlite", "from_backtest_zip", "load_sqlite", "load_backtest_zip",
]


# ── time / number helpers (wt_reference._ts_epoch semantics) ────────────

def _ts(value):
    """Epoch seconds from an ISO string / epoch number; None when absent."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000 if v > 1e12 else v
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()
    except ValueError:
        try:
            v = float(s)
            return v / 1000 if v > 1e12 else v
        except ValueError:
            return None


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── normalized trade rows ───────────────────────────────────────────────

def _norm_orders(trade, orders):
    """(anchors, buys, sells, full_exits) — normalized per GridStrategy tags.

    buys/sells: {line_idx: [order, ...]} in fill order. anchors / full_exits:
    plain lists in fill order. Orders with no positive qty are dropped
    (unfilled/cancelled rows in a live sqlite).
    """
    entry_side = "sell" if trade.get("is_short") else "buy"
    anchors, buys, sells, full_exits = [], {}, {}, []
    for o in orders:
        qty = _num(o.get("filled", o.get("amount")))
        if qty <= 0:
            continue
        is_entry = bool(o.get("ft_is_entry")) if "ft_is_entry" in o \
            else (o.get("ft_order_side") == entry_side)
        tag = o.get("ft_order_tag") or ""
        m = GRID_TAG_RE.search(tag)
        if is_entry:
            if m:
                buys.setdefault(int(m.group(2)), []).append(o)
            else:
                anchors.append(o)
        else:
            if m and m.group(1) == "sell":
                sells.setdefault(int(m.group(2)), []).append(o)
            else:
                # exit-side order without a grid_sell tag; also a stray
                # "grid_buy_Ln" tag on an exit side is impossible geometry
                full_exits.append(o)
    return anchors, buys, sells, full_exits


def _order_ts(o, fallback):
    return _ts(o.get("order_filled_date") or o.get("order_filled_timestamp")
               or o.get("order_date")) or fallback


def _order_px(o):
    # freqtrade Order.safe_price: average → price → stop_price → ft_price
    for k in ("average", "price", "stop_price", "ft_price"):
        if o.get(k) is not None:
            return float(o[k])
    return _num(o.get("safe_price"))


def pair_trade(trade, source_id=""):
    """One normalized trade row → (trips, surprises).

    Model: freqtrade keeps ONE fungible position per trade — every
    entry order is a lot {line, bp, qty}; every sell drains qty from
    lots. A grid_sell_L<i> drains the line's own lots FIFO, then (amount
    overshoot — freqtrade rounds amounts to the pair precision, so a
    sell can exceed its line's buy) the trade-wide pool FIFO, exactly
    like the position itself. An undersold line's leftover sliver stays
    in its lot and merges into the SAME tp trip at the trade's exit
    price (one lot, one journey). Remaining lots close as full_exit
    trips, one per lot, at the exit fill.
    `trade` carries id, pair, is_short, is_open, open_date, close_date,
    exit_reason, fee_open, fee_close, open_rate, orders.
    """
    surprises: list[str] = []
    if trade.get("is_open"):
        return [], surprises
    anchors, buys, sells, full_exits = _norm_orders(trade, trade.get("orders", []))
    if not (anchors or buys or sells or full_exits):
        return [], surprises

    fee_open = _num(trade.get("fee_open"))
    fee_close = _num(trade.get("fee_close"))
    exit_reason = trade.get("exit_reason") or "closed"
    open_ts = _ts(trade.get("open_date")) or 0.0
    tid = trade.get("id")

    def _mk_lot(o, line):
        return {"bp": _order_px(o), "ts": _order_ts(o, open_ts),
                "line": line, "qty": _num(o.get("filled", o.get("amount"))),
                "seq": o.get("id", 0)}

    # the trade-wide fungible pool: every entry order, oldest fill first
    pool: list[dict] = []
    line_queues: dict[int, list[dict]] = {}
    for o in sorted(anchors + [x for q in buys.values() for x in q],
                    key=lambda o: (_order_ts(o, open_ts), o.get("id", 0))):
        m = GRID_TAG_RE.search(o.get("ft_order_tag") or "")
        lot = _mk_lot(o, int(m.group(2)) if m else None)
        pool.append(lot)
        if m:
            line_queues.setdefault(int(m.group(2)), []).append(lot)

    # trade-level full-exit fill: the last exit-side untagged order, else
    # close_rate (freqtrade exports set close_rate on closed trades)
    exit_px = _order_px(full_exits[-1]) if full_exits else \
        _num(trade.get("close_rate"), default=None) or None
    exit_ts = _ts(trade.get("close_date")) or _order_ts(full_exits[-1], open_ts) \
        if full_exits else _ts(trade.get("close_date"))

    def _trip(line, sell_px, sell_ts, slices, kind):
        """One trip from consumed slices [(qty, buy_px, sell_px, lot_ts)]."""
        pnl = 0.0
        qty = 0.0
        for q, bp, sp, _ts_ in slices:
            pnl += q * (sp - bp) - fee_open * q * bp - fee_close * q * sp
            qty += q
        # gain/buy_px from the FIRST slice — the line's own lot (FIFO
        # head). Overshoot slices mix in pool-lot dust (amount rounding,
        # ~1e-6 qty); keeping the basis on the matched lot keeps gain_pct
        # comparable with the m1_reconcile oracle (line buy → line sell).
        wbp = slices[0][1]
        entered = slices[0][3]
        sid = (f"{source_id}#{tid}-L{line}" if line is not None
               else f"{source_id}#{tid}-anchor")
        return {
            "pnl_usd": round(pnl, 6),
            "close_ts": sell_ts or exit_ts or entered,
            "entered_at": entered,
            "strategy_id": sid,
            "line": line,
            "buy_px": round(wbp, 10),
            "sell_px": sell_px,
            "qty": round(qty, 12),
            "kind": kind,
            "exit_reason": exit_reason if kind == "full_exit" else "tp",
            "gain_pct": round((sell_px / wbp - 1.0) * 100.0, 6) if wbp else None,
            "synthetic": bool(trade.get("synthetic")),
            "slices": [[round(q, 12), round(bp, 10), round(sp, 10)]
                       for q, bp, sp, _ts_ in slices],
        }

    trips = []
    # --- tp trips: one per grid_sell order -----------------------------
    for line in sorted(sells):
        queue = line_queues.get(line, [])
        had_buys = bool(queue)
        for sell_o in sells[line]:
            sp = _order_px(sell_o)
            sq = _num(sell_o.get("filled", sell_o.get("amount")))
            sell_ts = _order_ts(sell_o, open_ts)
            if not had_buys:
                surprises.append(
                    f"trade#{tid} L{line}: grid_sell with no grid_buy on "
                    "the line — drained the fungible pool (state anomaly)")
            slices: list[tuple[float, float, float, float]] = []
            rem = sq
            # 1) the line's own lots, FIFO
            while rem > _QTY_EPS and queue:
                lot = queue[0]
                take = min(rem, lot["qty"])
                if take <= _QTY_EPS:
                    queue.pop(0)
                    continue
                lot["qty"] -= take
                rem -= take
                slices.append((take, lot["bp"], sp, lot["ts"]))
                partial = lot["qty"] > _QTY_EPS
                if not partial:
                    queue.pop(0)
                else:
                    break
            # 2) undersold: the line's front lot keeps a sliver — same
            #    lot, same journey: merge it into this trip at the exit
            if rem <= _QTY_EPS and queue and exit_px is not None:
                lot = queue[0]
                if lot["qty"] > _QTY_EPS:
                    slices.append((lot["qty"], lot["bp"], exit_px, lot["ts"]))
                    lot["qty"] = 0.0
                    queue.pop(0)
            # 3) overshoot: drain the trade-wide pool, oldest lot first
            #    (the position is fungible — amount-rounded sells can
            #    overshoot the line's own buys)
            while rem > _QTY_EPS and pool:
                lot = pool[0]
                take = min(rem, lot["qty"])
                if take <= _QTY_EPS:
                    pool.pop(0)
                    continue
                lot["qty"] -= take
                rem -= take
                slices.append((take, lot["bp"], sp, lot["ts"]))
                if lot["qty"] <= _QTY_EPS:
                    pool.pop(0)
                else:
                    break
            if rem > _QTY_EPS:
                surprises.append(
                    f"trade#{tid} L{line}: grid_sell qty {sq} exceeds the "
                    f"whole position by {rem:.10f} — remainder unbooked")
            if slices:
                trips.append(_trip(line, sp, sell_ts, slices, "tp"))

    # --- full-exit trips: one per remaining lot -------------------------
    for lot in pool:
        if lot["qty"] <= _QTY_EPS:
            continue
        if exit_px is None:
            surprises.append(
                f"trade#{tid} L{lot['line']}: lot never closed (no "
                "full-exit fill) — excluded")
            continue
        slices = [(lot["qty"], lot["bp"], exit_px, lot["ts"])]
        lot["qty"] = 0.0
        trips.append(_trip(lot["line"], exit_px, exit_ts, slices,
                           "full_exit"))

    trips.sort(key=lambda t: (t["close_ts"] or 0, t["strategy_id"]))
    return trips, surprises


def pair_trades(trades, source_id=""):
    """Many normalized trade rows → (trips, surprises); open rows skipped."""
    trips, surprises = [], []
    open_n = 0
    for tr in trades or []:
        if tr.get("is_open"):
            open_n += 1
            continue
        t, s = pair_trade(tr, source_id=source_id)
        trips.extend(t)
        surprises.extend(s)
    trips.sort(key=lambda t: (t["close_ts"] or 0, t["strategy_id"]))
    return trips, surprises


# ── source: freqtrade tradesv3.sqlite ──────────────────────────────────

_SQL_TRADES = """
    SELECT t.id, t.pair, t.is_open, t.is_short, t.open_date, t.close_date,
           t.exit_reason, t.enter_tag, t.open_rate, t.close_rate,
           t.fee_open, t.fee_close, t.leverage
      FROM trades t ORDER BY t.id
"""


def _sqlite_rows(conn):
    trades = []
    for row in conn.execute(_SQL_TRADES):
        tr = dict(zip(
            ("id", "pair", "is_open", "is_short", "open_date", "close_date",
             "exit_reason", "enter_tag", "open_rate", "close_rate",
             "fee_open", "fee_close", "leverage"), row))
        tr["orders"] = []
        trades.append(tr)
    by_id = {t["id"]: t for t in trades}
    for row in conn.execute(
            "SELECT ft_trade_id, id, ft_order_side, ft_order_tag, average,"
            " price, stop_price, ft_price, COALESCE(filled, amount),"
            " COALESCE(order_filled_date, order_date), NULL FROM orders"
            " ORDER BY id"):
        tid, oid, side, tag, avgp, price, stop_px, ft_px, qty, ts, _ = row
        t = by_id.get(tid)
        if t is None:
            continue
        t["orders"].append({
            "id": oid, "ft_order_side": side, "ft_order_tag": tag,
            "average": avgp, "price": price, "stop_price": stop_px,
            "ft_price": ft_px, "filled": qty, "order_filled_date": ts,
        })
    return trades


def from_sqlite(path, synthetic=False, source_id=None):
    """tradesv3.sqlite (or schema-subset DB) → (trips, report)."""
    path = str(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        trades = _sqlite_rows(conn)
    finally:
        conn.close()
    if synthetic:
        for t in trades:
            t["synthetic"] = True
    sid = source_id or Path(path).stem
    trips, surprises = pair_trades(trades, source_id=sid)
    report = {
        "source": path, "source_kind": "sqlite",
        "trades_closed": sum(1 for t in trades if not t.get("is_open")),
        "trades_open_skipped": sum(1 for t in trades if t.get("is_open")),
        "tp_trips": sum(1 for t in trips if t["kind"] == "tp"),
        "full_exit_trips": sum(1 for t in trips if t["kind"] == "full_exit"),
        "surprises": surprises,
        "fees_modeled": True,
    }
    return trips, report


# ── source: freqtrade backtesting export zip ───────────────────────────

def _find_result_json(zp):
    with zipfile.ZipFile(zp) as z:
        for n in z.namelist():
            if (n.endswith(".json") and "_config" not in n
                    and "meta" not in n and "market_change" not in n
                    and "_wallet" not in n):
                d = json.loads(z.read(n))
                if isinstance(d, dict) and "strategy" in d:
                    return d
    raise SystemExit(f"no backtest result inside {zp}")


def from_backtest_zip(path, synthetic=True, source_id=None):
    """A backtesting export zip → (trips, report).

    Backtest evidence is research data, not fleet evidence: it defaults to
    synthetic=True so it can never gate the sizing ladder (wt_reference
    §synthetic semantics). Pass synthetic=False for acceptance math.
    """
    path = str(path)
    d = _find_result_json(path)
    key = next((k for k in d["strategy"] if "Grid" in k),
               next(iter(d["strategy"])))
    st = d["strategy"][key]
    trades = []
    for t in st.get("trades", []):
        orders = []
        for o in t.get("orders", []):
            orders.append({
                "ft_order_side": o.get("ft_order_side"),
                "ft_order_tag": o.get("ft_order_tag"),
                "ft_is_entry": bool(o.get("ft_is_entry")),
                "safe_price": o.get("safe_price"),
                "filled": o.get("amount"),
                "order_filled_timestamp": o.get("order_filled_timestamp"),
            })
        trades.append({
            "id": t.get("trade_id", t.get("pair")), "pair": t.get("pair"),
            "is_open": bool(t.get("is_open")), "is_short":
                bool(t.get("is_short")),
            "open_date": t.get("open_date"),
            "open_timestamp": t.get("open_timestamp"),
            "close_date": t.get("close_date"),
            "close_timestamp": t.get("close_timestamp"),
            "exit_reason": t.get("exit_reason"),
            "open_rate": t.get("open_rate"),
            "close_rate": t.get("close_rate"),
            "fee_open": t.get("fee_open"), "fee_close": t.get("fee_close"),
            "orders": orders, "synthetic": synthetic,
        })
    sid = source_id or Path(path).stem
    trips, surprises = pair_trades(trades, source_id=sid)
    report = {
        "source": path, "source_kind": "backtest_zip",
        "strategy": key,
        "trades_closed": sum(1 for t in trades if not t.get("is_open")),
        "trades_open_skipped": sum(1 for t in trades if t.get("is_open")),
        "tp_trips": sum(1 for t in trips if t["kind"] == "tp"),
        "full_exit_trips": sum(1 for t in trips
                               if t["kind"] == "full_exit"),
        "surprises": surprises,
        "fees_modeled": True,
        "synthetic": synthetic,
    }
    return trips, report


# aliases mirroring wt_reference naming (bot_trades ↔ source loads)
load_sqlite = from_sqlite
load_backtest_zip = from_backtest_zip
