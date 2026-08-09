#!/usr/bin/env python3
"""Export closed positions from FTMO MT5 to a CSV that reconcile_mt5.py accepts.

WHY THIS EXISTS
    reconcile_mt5.py is the Friday guardrail. It caught the journal holding 13
    trades when the account had taken 42 -- every loss omitted, P/L overstated
    by $849.41. It works, and its logic is NOT being rewritten.

    What was fragile was its INPUT: a CSV you had to remember to download from
    the broker dashboard. A reconciliation you can forget to feed is a
    reconciliation that silently stops running -- the same failure mode it
    exists to catch.

    This removes the manual step. It reads closed positions straight from the
    running MT5 terminal and writes the CSV reconcile_mt5.py already knows how
    to parse.

FRIDAY WORKFLOW
    cd C:\\Users\\disco\\Documents\\BBSB_Trading\\journal
    python mt5_export_deals.py                     # writes mt5_deals_<date>.csv
    python reconcile_mt5.py mt5_deals_<date>.csv   # report only
    python reconcile_mt5.py mt5_deals_<date>.csv --apply

READ-ONLY. Uses history_deals_get only. No order_send anywhere in this file.
"""
import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

TERMINAL = r"C:\Program Files\FTMO Global Markets MT5 Terminal\terminal64.exe"

# Column names chosen to hit reconcile_mt5.py's FIELD_ALIASES on the nose.
COLUMNS = ["ticket", "symbol", "direction", "date_open", "date_close",
           "entry", "exit", "lots", "commission", "swap", "profit"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400,
                    help="how far back to pull (default 400)")
    ap.add_argument("-o", "--out", default=None,
                    help="output path (default mt5_deals_<today>.csv here)")
    args = ap.parse_args()

    if not mt5.initialize(path=TERMINAL):
        print("INITIALIZE FAILED:", mt5.last_error())
        print("Is the FTMO MT5 terminal running and logged in?")
        return 2

    try:
        acct = mt5.account_info()
        if acct is None:
            print("account_info() returned None -- terminal running but not logged in.")
            return 2
        print(f"account {acct.login} @ {acct.server}  balance {acct.balance:,.2f}")

        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        deals = mt5.history_deals_get(since, datetime.now(timezone.utc) + timedelta(days=1))
        if deals is None:
            print("history_deals_get returned None:", mt5.last_error())
            return 2

        # Group by position_id. A position has one IN deal and one or more OUT
        # deals (partial closes). Balance/credit deals carry position_id 0 and
        # are skipped -- they are not trades.
        by_pos = defaultdict(list)
        for d in deals:
            if d.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
                continue          # skips DEAL_TYPE_BALANCE etc.
            if not d.position_id:
                continue
            by_pos[d.position_id].append(d)

        rows = []
        for pos_id, legs in by_pos.items():
            legs.sort(key=lambda x: x.time)
            entries = [d for d in legs if d.entry == mt5.DEAL_ENTRY_IN]
            exits = [d for d in legs if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
            if not entries or not exits:
                continue          # still open, or malformed -- not a closed trade
            first, last = entries[0], exits[-1]
            rows.append({
                "ticket": pos_id,
                "symbol": first.symbol,
                # IN deal type is the direction the POSITION was opened in
                "direction": "BUY" if first.type == mt5.DEAL_TYPE_BUY else "SELL",
                "date_open": datetime.fromtimestamp(first.time, timezone.utc)
                                     .strftime("%Y-%m-%d %H:%M:%S"),
                "date_close": datetime.fromtimestamp(last.time, timezone.utc)
                                      .strftime("%Y-%m-%d %H:%M:%S"),
                "entry": first.price,
                "exit": last.price,
                "lots": sum(d.volume for d in entries),
                # commission can sit on either leg; swap normally on the close
                "commission": round(sum(d.commission for d in legs), 2),
                "swap": round(sum(d.swap for d in legs), 2),
                "profit": round(sum(d.profit for d in legs), 2),
            })

        rows.sort(key=lambda r: r["date_close"])

        out = Path(args.out) if args.out else \
            Path(__file__).with_name(f"mt5_deals_{datetime.now():%Y-%m-%d}.csv")
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(rows)

        print(f"{len(rows)} closed position(s) -> {out}")
        if not rows:
            print("No closed trades in range. That is correct for a fresh account,")
            print("but on an account that HAS traded it means something is wrong --")
            print("check --days and that the terminal is on the right account.")
        else:
            net = sum(r["profit"] + r["commission"] + r["swap"] for r in rows)
            print(f"net across export: {net:,.2f}")
            print(f"\nnext: python reconcile_mt5.py {out.name}")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
