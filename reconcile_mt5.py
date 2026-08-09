#!/usr/bin/env python3
"""Reconcile the journal's Closed Trades against an MT5 / broker export.

WHY THIS EXISTS
    On 2026-08-02 the journal was found to contain 13 trades, all winners,
    while the account had actually taken 42 (22W/20L). Every loss had been
    omitted, overstating P/L by $849.41 and showing a 100% win rate against
    an actual 52%. A month of analysis built on that table was void.

    Nothing structural prevented that, so this does: run it every Friday and
    the journal cannot silently drift again.

USAGE
    python reconcile_mt5.py <export.csv>            # report only, changes nothing
    python reconcile_mt5.py <export.csv> --apply    # also insert missing trades

    The export needs one row per closed deal. Column names are matched loosely
    (see FIELD_ALIASES) so both the broker dashboard export and a hand-made CSV
    work. A ticket/ID column is REQUIRED — it is the only reliable join key,
    since same-symbol same-minute trades are otherwise indistinguishable.
"""
import argparse
import csv
import os
import re
import sys
from datetime import datetime

from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL = os.path.join(HERE, "index.html")

FIELD_ALIASES = {
    "id":     ("id", "ticket", "deal", "order", "position"),
    "symbol": ("symbol", "instrument", "pair"),
    "side":   ("side", "direction", "type"),
    "open":   ("open_dt", "open date", "opendate", "open time", "date_open"),
    "close":  ("close_dt", "close date", "closedate", "close time", "date_close"),
    "entry":  ("entry", "open price", "price open"),
    "exit":   ("exit", "close price", "price close"),
    "qty":    ("qty", "lots", "volume", "size"),
    "fee":    ("fee", "commission", "comm"),
    "swap":   ("swap", "rollover"),
    # GROSS vs NET must stay separate. MT5's `profit` field (and mt5_export_deals.py,
    # which writes it straight through at :102) is GROSS — commission and swap sit in
    # their own columns. Folding "profit" into the net aliases made this script read a
    # gross figure as net, so every comparison was wrong by (commission + swap) per
    # trade, and the delta check below then failed on a difference it had invented.
    "pnl":    ("pnl", "p&l", "net", "net_$"),          # already net of costs
    "gross":  ("profit", "gross", "gross_$"),          # needs + fee + swap
    "tq":     ("tq", "trade quality", "quality"),
}


def pick(row, key):
    """Fetch a value from a CSV row by any of the aliases for `key`."""
    lowered = {k.strip().lower(): v for k, v in row.items() if k}
    for alias in FIELD_ALIASES[key]:
        if alias in lowered:
            return (lowered[alias] or "").strip()
    return ""


def money(text):
    t = str(text).replace("$", "").replace(",", "").replace("+", "").strip()
    if t in ("", "-", "—"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def load_export(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        tid = pick(r, "id")
        if not tid:
            continue
        fee = money(pick(r, "fee")) or 0.0
        swap = money(pick(r, "swap")) or 0.0
        # Prefer a genuine net column; otherwise derive it from gross + costs.
        # MT5 signs commission and swap NEGATIVE for costs, so this is a sum, not
        # a subtraction -- matching mt5_export_deals.py:120.
        net = money(pick(r, "pnl"))
        if net is None:
            g = money(pick(r, "gross"))
            net = None if g is None else round(g + fee + swap, 2)
        out.append({
            "id": tid,
            "symbol": pick(r, "symbol").upper(),
            "side": pick(r, "side").upper(),
            "open": pick(r, "open"),
            "close": pick(r, "close"),
            "entry": pick(r, "entry"),
            "exit": pick(r, "exit"),
            "qty": pick(r, "qty"),
            "fee": fee,
            "swap": swap,
            "pnl": net,
            "tq": pick(r, "tq"),
        })
    return out


def as_date(text):
    """Parse a journal or export timestamp to a date. None if unparseable."""
    t = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(t[:len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    return None


def load_journal():
    with open(JOURNAL, encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")
    table = soup.find("table", id="closed-trades")
    hdr = [th.get_text(strip=True) for th in table.find_all("th")]
    rows = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if tds:
            rows.append(dict(zip(hdr, [td.get_text(strip=True) for td in tds])))
    return soup, table, hdr, rows


def ticket_of(note):
    """Journal rows carry the broker ticket inside Notes as 'Ticket #<digits>'.

    Anchored on the literal 'Ticket #' where possible. The old version split on
    the FIRST '#' anywhere in Notes and took the digits after it -- Notes is long
    free text (the 2026-08-04 row is a multi-paragraph post-mortem), so any other
    '#' earlier in the string silently yielded the wrong key or None, and the
    ticket is the ONLY reliable join for reconciliation.
    """
    if not note:
        return None
    m = re.search(r"[Tt]icket\s*#\s*(\d+)", note)
    if m:
        return m.group(1)
    m = re.search(r"#(\d{6,})", note)      # fallback: a plausibly-ticket-length run
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export", help="broker/MT5 export CSV")
    ap.add_argument("--apply", action="store_true",
                    help="insert missing trades into the journal")
    args = ap.parse_args()

    if not os.path.exists(args.export):
        sys.exit(f"export not found: {args.export}")

    deals = load_export(args.export)
    soup, table, hdr, jrows = load_journal()
    # Detect duplicates BEFORE collapsing. Building this dict silently kept the
    # last row for a repeated ticket, so a trade logged twice -- the natural result
    # of running --apply against overlapping exports -- vanished from the count and
    # double-counted in the P&L sum, which is drift of exactly the kind this script
    # exists to surface.
    jtickets, dup_tickets = {}, {}
    for r in jrows:
        t = ticket_of(r.get("Notes", ""))
        if not t:
            continue
        if t in jtickets:
            dup_tickets.setdefault(t, 1)
            dup_tickets[t] += 1
        jtickets[t] = r

    missing = [d for d in deals if d["id"] not in jtickets]
    orphans = [r for r in jrows if ticket_of(r.get("Notes", "")) is None]
    mismatch = []
    for d in deals:
        jr = jtickets.get(d["id"])
        if jr and d["pnl"] is not None:
            jp = money(jr.get("Net_$", ""))
            if jp is not None and abs(jp - d["pnl"]) > 0.01:
                mismatch.append((d, jp))

    # --- SCOPE THE COMPARISON TO THE EXPORT'S OWN DATE WINDOW -----------------
    # An export covers a finite range; the journal covers all history. Comparing
    # a windowed export against the whole journal guarantees a non-zero delta,
    # which after the 2026-08-09 hardening made this script exit 1 unconditionally
    # -- and a check that always fails gets ignored, which is the same end state
    # as the check that always passed.
    #
    # Acute after the FTMO migration: the journal holds 43 The5ers-era rows whose
    # tickets can NEVER appear in an FTMO export. Those are out of scope, not drift.
    exp_dates = [d for d in (as_date(x["close"]) for x in deals) if d]
    lo, hi = (min(exp_dates), max(exp_dates)) if exp_dates else (None, None)

    def in_window(r):
        if lo is None:
            return False
        d = as_date(r.get("Date_Close", ""))
        return d is not None and lo <= d <= hi

    scoped = [r for r in jrows if in_window(r)]
    outside = len(jrows) - len(scoped)

    exp_pnl = sum(d["pnl"] for d in deals if d["pnl"] is not None)
    jrn_pnl = sum(v for r in scoped if (v := money(r.get("Net_$", ""))) is not None)

    win = f"{lo} .. {hi}" if lo else "(no parseable dates)"
    print(f"window  : {win}")
    print(f"export  : {len(deals)} deals   net ${exp_pnl:,.2f}")
    print(f"journal : {len(scoped)} rows in window   net ${jrn_pnl:,.2f}")
    if outside:
        print(f"          ({outside} journal row(s) outside the window — not compared)")
    print(f"delta   : ${exp_pnl - jrn_pnl:,.2f}\n")

    # Reverse direction. Added 2026-08-09 after this script reported
    # "OK — journal matches the export exactly" against an EMPTY export while
    # the journal held 43 rows and the delta read $1,125.53.
    #
    # The original check was one-directional: it only asked "is any EXPORT deal
    # missing from the journal?" An empty export has nothing to be missing, so
    # it passed. A truncated export (say, only the last month downloaded) passed
    # the same way, silently leaving every older row unverified.
    #
    # That is the exact failure this script exists to prevent — a reconciliation
    # that reports OK when it has actually checked nothing.
    #
    # Scoped to the export window (see above): a row the export never claimed to
    # cover is unverified-by-this-export, not evidence of drift. Widen the export
    # range to verify older rows.
    export_ids = {d["id"] for d in deals}
    unmatched_journal = [(t, r) for r in scoped
                         if (t := ticket_of(r.get("Notes", ""))) and t not in export_ids]

    delta = exp_pnl - jrn_pnl

    ok = True
    if not deals and jrows:
        ok = False
        print("!! EXPORT IS EMPTY but the journal has rows — nothing was verified.")
        print("   Check the export actually pulled data, and that the terminal is")
        print("   on the right account. Do NOT read this as a clean result.")
    if missing:
        ok = False
        print(f"!! {len(missing)} DEAL(S) MISSING FROM THE JOURNAL")
        for d in missing:
            print(f"     #{d['id']}  {d['close']:16} {d['symbol']:7} {d['side']:4} "
                  f"${d['pnl'] if d['pnl'] is not None else 0:>9,.2f}")
    if unmatched_journal:
        ok = False
        print(f"\n!! {len(unmatched_journal)} JOURNAL ROW(S) NOT IN THE EXPORT — unverified")
        print("   Either the export range is too short, or these rows are wrong.")
        for t, r in unmatched_journal[:10]:
            print(f"     #{t}  {r.get('Date_Close','?')}  {r.get('Instrument','?')}")
        if len(unmatched_journal) > 10:
            print(f"     ... and {len(unmatched_journal) - 10} more")
    if dup_tickets:
        ok = False
        print(f"\n!! {len(dup_tickets)} DUPLICATE TICKET(S) IN THE JOURNAL")
        print("   The same trade is logged more than once — P&L is double-counted.")
        for t, n in dup_tickets.items():
            print(f"     #{t}  x{n}")
    if orphans:
        ok = False
        print(f"\n!! {len(orphans)} JOURNAL ROW(S) WITH NO TICKET — cannot be verified")
        for r in orphans:
            print(f"     {r.get('Date_Close','?')}  {r.get('Instrument','?')}")
    if mismatch:
        ok = False
        print(f"\n!! {len(mismatch)} P&L MISMATCH(ES)")
        for d, jp in mismatch:
            print(f"     #{d['id']}  journal ${jp:,.2f}  vs export ${d['pnl']:,.2f}")
    if abs(delta) > 0.01:
        ok = False
        print(f"\n!! NET P&L DELTA ${delta:,.2f} — the two sides do not agree.")
    if ok:
        print("OK — journal matches the export exactly.")

    if missing and args.apply:
        body = table.find("tbody") or table
        for d in missing:
            gross = (d["pnl"] or 0) - d["fee"] - d["swap"]
            note = f"Ticket #{d['id']}."
            if d["tq"]:
                note += f" Trade Quality {d['tq']}."
            note += " Added by reconcile_mt5.py."
            vals = [d["open"], d["close"], d["symbol"], d["side"], d["entry"],
                    d["exit"], "—", "—", d["qty"], f"{gross:+,.2f}",
                    f"{d['fee'] + d['swap']:+,.2f}", f"{d['pnl']:+,.2f}", "—",
                    "WIN" if (d["pnl"] or 0) > 0 else "LOSS", note]
            tr = soup.new_tag("tr")
            for v in vals:
                td = soup.new_tag("td")
                td.string = str(v)
                tr.append(td)
            body.append(tr)
        with open(JOURNAL, "w", encoding="utf-8") as f:
            f.write(str(soup))
        print(f"\nAPPLIED — inserted {len(missing)} row(s). "
              f"Commit and push, then re-run to confirm clean.")
    elif missing:
        print("\n(report only — re-run with --apply to insert)")

    return 1 if not ok else 0


if __name__ == "__main__":
    sys.exit(main())
