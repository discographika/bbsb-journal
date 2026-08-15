#!/usr/bin/env python3
"""Measure what price DID after each flagged setup.

    #### 2026-08-10 -- THE GATE QUESTION THIS WAS BUILT FOR IS CLOSED. ####

    This script was written to answer "does the macro gate select better-than-
    random setups?" by accumulating ~20 forward rows in Bias History. That plan
    is retired: the question was answered a different way, and the forward
    method turned out to be incapable of answering it at all.

    WHAT HAPPENED
        COT history IS retrievable after all -- via Pine, not the MCP (raw CFTC
        data is free through TradingView/LibraryCOT). So the gate was applied
        historically across 161 backtest trades instead of waiting ~5 Sundays.
        Result: ungated PF 0.954 -> gated 1.021, but nothing significant, the
        gate helped only 4 of 10 instruments, and it loses to its own inverse.
        Full report: pine/COT_GATE_VALIDATION_2026-08-10.md

    WHY 20 ROWS WAS NEVER GOING TO WORK
        Detecting an edge of the observed size needs ~700-3,000 trades PER ARM
        at 80% power. Twenty rows is two orders of magnitude short. Any
        "gated vs flagged" summary this script prints on a handful of rows is
        noise -- do not report it as a finding, in either direction.

        The same applies to the FX resumption trigger, which is the identical
        20-setup counter (CLAUDE.md § FX SUSPENSION).

    WHAT THIS SCRIPT IS STILL GOOD FOR -- and it is genuinely useful
        - Does ZONE SCORE predict outcome? (8/8 vs 7/8 vs 6/8). This is the
          live question and it is NOT affected by the gate finding.
        - Did the forward move favour the recorded W_Bias?
        - Sanity-checking that recorded zones behave the way the method claims.

        Those need a large sample too, so treat everything here as descriptive
        until the row count is in the hundreds. Keep accumulating; just stop
        expecting a verdict at 20.

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
          SKIP rows dated 2026-08-09 onward ARE measurable.

          SUPERSEDED 2026-08-10: this used to say the gated-vs-flagged
          comparison "becomes real once ~20 of them accumulate -- roughly 5
          Sundays". That was wrong by two orders of magnitude. See the header.
          Recording the zones is still right -- it costs ~4 BB searches a
          Sunday and it is the only counterfactual there will ever be -- but do
          not expect it to settle the gate.

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

# A zone-less row is ambiguous: was the search run and empty, or never run?
# The marker is the Notes string, NOT the date. /sd-sunday writes
# "NO ZONE (searched 120 bars)" when the search ran and found nothing.
#
# Do NOT use a date cutoff here. Step 3G changed ON 2026-08-09, partway through
# that day, so the card already written that morning has zone-less gated rows
# that were never searched while carrying the same date. A date test reports
# them as "searched, found nothing" -- the exact conflation this is meant to
# prevent, just moved somewhere harder to notice.
SEARCHED_MARKER = "NO ZONE (SEARCHED"

# watchlist name -> FTMO execution ticker
TICKER = {
    "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "AUDUSD": "AUDUSD",
    "USDJPY": "USDJPY", "GBPJPY": "GBPJPY", "EURGBP": "EURGBP",
    "XAUUSD": "XAUUSD", "US30": "US30.cash", "NAS100": "US100.cash",
    "US500": "US500.cash", "GER40": "GER40.cash",
}


def bias_dir(text):
    """Extract a direction from free-text W_Bias. Returns +1 / -1 / 0.

    RANGING is checked FIRST and wins. W_Bias is free text and routinely reads
    like 'RANGING (was bullish)' or 'BULLISH -> ranging' -- substring-matching
    BULLISH/BEARISH before ruling out RANGING would sign a directionless week
    and feed a fake forward return into the macro-status averages.
    """
    t = (text or "").upper()
    if "RANGING" in t or "RANGE" in t or "NEUTRAL" in t:
        return 0
    has_bull, has_bear = "BULLISH" in t, "BEARISH" in t
    if has_bull and has_bear:
        return 0      # e.g. 'bearish, turning bullish' -- ambiguous, do not sign
    if has_bull:
        return 1
    if has_bear:
        return -1
    return 0          # unknown -- forward return is not signable


def num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def load_rows():
    soup = BeautifulSoup(JOURNAL.read_text(encoding="utf-8"), "html.parser")
    out = []
    # LIVE CARD FIRST, history second -- order matters, see the dedupe below.
    for tid in ("weekly-bias", "weekly-bias-history"):
        t = soup.find("table", id=tid)
        if not t:
            continue
        hdr = [th.get_text(strip=True) for th in t.find_all("th")]
        for tr in t.find("tbody").find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if cells:
                out.append(dict(zip(hdr, cells)))
    # Dedupe on (Date, Instrument, BB_Zone_Type), FIRST WINS -- live card beats history.
    #
    # BB_Zone_Type joined the key on 2026-08-15, when /sd-sunday started searching
    # both directions and writing one row per ZONE rather than per instrument. An
    # instrument can now appear twice on a card, once DEMAND and once SUPPLY, with
    # DIFFERENT Macro_Status and Trade_Y_N -- the gate is evaluated per zone. On the
    # old two-part key the second zone was dropped as a duplicate, so this study
    # would have measured half the setups while reporting a full sample.
    # In normal operation the two never overlap (history holds past weeks, the
    # card holds this one). They overlap only when a Sunday is re-run or a row
    # is corrected: Step 0 archives the OLD card, then rewrites the live one.
    # History-first would make the stale archived copy win and silently discard
    # the correction -- which would have quietly defeated the 2026-08-09 Step 3G
    # fix if the card were re-run to pick up zones on gated rows.
    seen, uniq = set(), []
    for r in out:
        k = (r.get("Date"), r.get("Instrument"), r.get("BB_Zone_Type", ""))
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
           "reached": None, "r_mult": None, "has_zone": False,
           "notes": row.get("Notes", "")}
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

    # Select into Market Watch first. CLAUDE.md § EXECUTION TICKERS: only 11 of 166
    # symbols are visible by default and gold and every index are hidden, so
    # copy_rates_range on an unselected symbol returns empty. That degraded into a
    # generic "no rates" count indistinguishable from a genuine data gap -- it works
    # today only because the watchlist happens to be aligned.
    if not mt5.symbol_select(tk, True):
        res["status"] = "symbol_select failed"
        return res

    rates = mt5.copy_rates_range(tk, mt5.TIMEFRAME_H1, start, end)
    if rates is None or len(rates) == 0:
        res["status"] = "no rates (symbol selected, so this is a real data gap)"
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
    # Split by date rather than asserting. Before 2026-08-09 Step 3G skipped the
    # zone search on gated rows, so a blank there means NEVER SEARCHED and cannot
    # be backfilled. From 2026-08-09 the BB search always runs, so a blank means
    # SEARCHED AND FOUND NOTHING -- a real result, and a different thing entirely.
    # Conflating the two is the exact distinction this fix exists to preserve.
    searched = [r for r in blind if SEARCHED_MARKER in (r["notes"] or "").upper()]
    never = [r for r in blind if SEARCHED_MARKER not in (r["notes"] or "").upper()]
    if searched:
        print(f"  {len(searched)} searched, no qualifying zone found -- a real result")
    if never:
        print(f"  {len(never)} never searched -- cannot be backfilled, the zone had")
        print("     to be read off the chart at the time. Step 3G skipped the search")
        print("     on gated rows until 2026-08-09; these predate the fix.")

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
        # Fixed field list, not results[0].keys() -- that raised IndexError on an
        # empty journal, i.e. it failed exactly when you were checking whether
        # anything had been recorded at all.
        cols = ["date", "instrument", "ticker", "macro", "bb_score", "sb_score",
                "trade", "bias", "status", "fwd_pct", "reached", "r_mult", "has_zone"]
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in results:
                # fwd_5d is the in-memory name; the column is fwd_pct because the
                # window is --days (default 10), so "5d" was wrong for every
                # invocation that did not explicitly pass --days 5.
                row = dict(r)
                row["fwd_pct"] = row.pop("fwd_5d", None)
                w.writerow(row)
        print(f"\ndetail ({len(results)} rows, window {args.days}d) -> {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
