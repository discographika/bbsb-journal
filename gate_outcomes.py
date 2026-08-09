#!/usr/bin/env python3
"""Measure what price DID after each flagged setup, so the macro gate can be judged.

THE QUESTION
    The 2026-08-09 backtest put the entry model at PF 1.12 -- and 0.97 excluding
    gold -- measured UNGATED, because COT/Valuation/Seasonality have no
    retrievable history. So the system's central component is unmeasured:

        Does the macro gate select better-than-random setups?

    Three decisions hang on the answer: whether FX trading resumes, whether the
    B2-B6 indicator rebuild gets built, and whether the gate is worth keeping.

WHAT THIS CAN AND CANNOT ANSWER  -- read before trusting a summary
    CAN:  does ZONE SCORE predict outcome?  (8/8 vs 7/8 vs 6/8)
          does the forward move favour the recorded W_Bias?

    CANNOT (for rows dated before 2026-08-09): whether SKIP was the right call.
          Step 3G used to short-circuit the whole zone search for gated
          instruments to save tokens, so every SKIP row from that era has NO
          ZONE. That optimisation destroyed the counterfactual -- there is
          nothing to measure on those skipped setups, and no way to backfill
          them, since the zone had to be read off the chart at the time.

          FIXED 2026-08-09: Step 3G now runs the BB search on gated instruments
          and records it (SB is still skipped; Trade_Y_N still forces NO). So
          SKIP rows dated 2026-08-09 onward ARE measurable. The
          gated-vs-flagged comparison becomes real once ~20 of them accumulate
          -- roughly 5 Sundays at ~4 gated instruments per card.

    Also: the gate has only ever emitted SKIP or WEAK. MODERATE/HIGH/A+ MAX have
          never fired (see the NEUTRAL-bottleneck note in CLAUDE.md), so there is
          no tier variation to measure among the setups that DO get zones.

OUTCOME MEASURES
    fwd_5d   -- % move over ~5 trading days, SIGNED to the recorded W_Bias.
                Positive = price went the way the bias said. Crude, but it is
                the only measure available for SKIP rows.
    reached  -- did price touch the zone proximal inside the window?
    r_mult   -- once touched, did price reach +2R before -1R, where R = zone
                height (entry ~ proximal, stop ~ distal)? +2 / -1 / 0 if neither.

READ-ONLY. copy_rates_range only. No order_send in this file.

USAGE
    python gate_outcomes.py                 # evaluate + print summary
    python gate_outcomes.py --days 10       # forward window (default 10)
    python gate_outcomes.py --csv out.csv   # also write per-row detail
"""
import argparse
import csv
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup
import MetaTrader5 as mt5

TERMINAL = r"C:\Program Files\FTMO Global Markets MT5 Terminal\terminal64.exe"
JOURNAL = Path(__file__).with_name("index.html")

# watchlist name -> FTMO execution ticker
TICKER = {
    "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "AUDUSD": "AUDUSD",
    "USDJPY": "USDJPY", "GBPJPY": "GBPJPY", "EURGBP": "EURGBP",
    "XAUUSD": "XAUUSD", "US30": "US30.cash", "NAS100": "US100.cash",
    "US500": "US500.cash", "GER40": "GER40.cash",
}


def bias_dir(text):
    """Extract a direction from free-text W_Bias. Returns +1 / -1 / 0."""
    t = (text or "").upper()
    if "BULLISH" in t:
        return 1
    if "BEARISH" in t:
        return -1
    return 0          # RANGING / unknown -- forward return is not signable


def num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def load_rows():
    soup = BeautifulSoup(JOURNAL.read_text(encoding="utf-8"), "html.parser")
    out = []
    for tid in ("weekly-bias-history", "weekly-bias"):
        t = soup.find("table", id=tid)
        if not t:
            continue
        hdr = [th.get_text(strip=True) for th in t.find_all("th")]
        for tr in t.find("tbody").find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if cells:
                out.append(dict(zip(hdr, cells)))
    # dedupe on (Date, Instrument) -- the live card duplicates history
    seen, uniq = set(), []
    for r in out:
        k = (r.get("Date"), r.get("Instrument"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def evaluate(row, days):
    inst = row.get("Instrument", "")
    tk = TICKER.get(inst)
    res = {"date": row.get("Date"), "instrument": inst, "ticker": tk,
           "macro": row.get("Macro_Status"), "bb_score": row.get("BB_Score"),
           "sb_score": row.get("SB_Score"), "trade": row.get("Trade_Y_N"),
           "bias": row.get("W_Bias", "")[:24], "status": "", "fwd_5d": None,
           "reached": None, "r_mult": None, "has_zone": False}
    # Set from the row itself, BEFORE any early return -- coverage reporting must
    # count rows that are merely too recent to evaluate, not just evaluated ones.
    res["has_zone"] = bool(
        num(row.get("BB_Zone_Top")) and num(row.get("BB_Zone_Bot"))
        and (row.get("BB_Zone_Type") or "").upper() in ("DEMAND", "SUPPLY"))
    if not tk:
        res["status"] = "unknown ticker"
        return res
    try:
        start = datetime.strptime(row["Date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        res["status"] = "bad date"
        return res

    end = start + timedelta(days=days)
    if end > datetime.now(timezone.utc):
        res["status"] = f"too recent (needs {days}d)"
        return res

    rates = mt5.copy_rates_range(tk, mt5.TIMEFRAME_H1, start, end)
    if rates is None or len(rates) == 0:
        res["status"] = "no rates"
        return res

    open0 = float(rates[0]["open"])
    close_n = float(rates[-1]["close"])
    d = bias_dir(row.get("W_Bias"))
    if d:
        res["fwd_5d"] = round((close_n - open0) / open0 * 100 * d, 2)

    top, bot = num(row.get("BB_Zone_Top")), num(row.get("BB_Zone_Bot"))
    ztype = (row.get("BB_Zone_Type") or "").upper()
    if res["has_zone"]:
        demand = ztype == "DEMAND"
        prox, dist = (top, bot) if demand else (bot, top)
        R = abs(prox - dist)
        touched = False
        for r_ in rates:
            lo, hi = float(r_["low"]), float(r_["high"])
            if not touched:
                if (demand and lo <= prox) or ((not demand) and hi >= prox):
                    touched = True
                    res["reached"] = True
                continue
            if demand:
                if lo <= prox - R:                      # -1R first
                    res["r_mult"] = -1; break
                if hi >= prox + 2 * R:
                    res["r_mult"] = 2; break
            else:
                if hi >= prox + R:
                    res["r_mult"] = -1; break
                if lo <= prox - 2 * R:
                    res["r_mult"] = 2; break
        if not touched:
            res["reached"] = False
        elif res["r_mult"] is None:
            res["r_mult"] = 0                            # touched, neither hit
    res["status"] = "ok"
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    if not mt5.initialize(path=TERMINAL):
        print("INITIALIZE FAILED:", mt5.last_error())
        return 2
    try:
        rows = load_rows()
        results = [evaluate(r, args.days) for r in rows]
    finally:
        mt5.shutdown()

    done = [r for r in results if r["status"] == "ok"]
    pend = [r for r in results if r["status"] != "ok"]

    print(f"rows: {len(results)}   evaluated: {len(done)}   pending/skipped: {len(pend)}")
    if pend:
        reasons = {}
        for r in pend:
            reasons[r["status"]] = reasons.get(r["status"], 0) + 1
        print("  " + ", ".join(f"{v}x {k}" for k, v in reasons.items()))

    if not done:
        print("\nNothing evaluable yet. Setups need ~%dd of forward data." % args.days)
        print("This is expected while the record is young -- re-run weekly.")
    else:
        print("\n=== BY ZONE SCORE (only rows that HAVE a zone) ===")
        buckets = {}
        for r in done:
            if r["r_mult"] is None:
                continue
            buckets.setdefault(r["bb_score"], []).append(r["r_mult"])
        if buckets:
            for k in sorted(buckets):
                v = buckets[k]
                print(f"  BB {k:5} n={len(v):3}  avg R {sum(v)/len(v):+.2f}  "
                      f"reached+won {sum(1 for x in v if x == 2)}/{len(v)}")
        else:
            print("  none reached their zone yet")

        print("\n=== FORWARD MOVE vs W_BIAS, by macro status ===")
        print("    (positive = price went the way the bias said)")
        fb = {}
        for r in done:
            if r["fwd_5d"] is not None:
                fb.setdefault(r["macro"], []).append(r["fwd_5d"])
        for k in sorted(fb):
            v = fb[k]
            print(f"  {k:6} n={len(v):3}  avg {sum(v)/len(v):+.2f}%  "
                  f"positive {sum(1 for x in v if x > 0)}/{len(v)}")

    # --- THE ACTUAL QUESTION: was SKIP the right call? ------------------------
    # Only answerable on rows where a gated setup ALSO has a recorded zone, so
    # its r_mult can be compared against the setups the gate let through.
    # Step 3G recorded no zone for gated rows before 2026-08-09 (see module
    # docstring) -- those are permanently unanswerable, hence the coverage line.
    print("\n=== WAS SKIP THE RIGHT CALL? ===")
    skips = [r for r in results if (r["macro"] or "").upper().startswith("SKIP")]
    blind = [r for r in skips if not r["has_zone"]]
    print(f"  SKIP rows: {len(skips)}   with a zone (measurable): "
          f"{len(skips) - len(blind)}   without (blind): {len(blind)}")
    if blind:
        print(f"  {len(blind)} blind row(s) are pre-2026-08-09 and cannot be")
        print("  backfilled -- the zone had to be read off the chart at the time.")

    cmp_ = {}
    for r in done:
        if r["r_mult"] is None:
            continue
        key = "GATED (skipped)" if (r["macro"] or "").upper().startswith("SKIP") \
            else "LET THROUGH"
        cmp_.setdefault(key, []).append(r["r_mult"])
    if len(cmp_) == 2:
        for k in ("LET THROUGH", "GATED (skipped)"):
            v = cmp_[k]
            print(f"  {k:16} n={len(v):3}  avg R {sum(v)/len(v):+.2f}")
        gap = (sum(cmp_["LET THROUGH"]) / len(cmp_["LET THROUGH"])
               - sum(cmp_["GATED (skipped)"]) / len(cmp_["GATED (skipped)"]))
        n = min(len(cmp_["LET THROUGH"]), len(cmp_["GATED (skipped)"]))
        print(f"  gap: {gap:+.2f} R in favour of the gate"
              if gap > 0 else f"  gap: {gap:+.2f} R -- the gate skipped the BETTER setups")
        if n < 20:
            print(f"  NOT YET DECISIVE -- smaller arm has n={n}, want ~20.")
    else:
        print("  Not comparable yet: need evaluable rows on BOTH sides")
        print("  (gated-with-zone AND let-through). Re-run weekly.")

    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
        print(f"\ndetail -> {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
