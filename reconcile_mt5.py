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
import sys

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
    "pnl":    ("pnl", "p&l", "profit", "net", "net_$"),
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
        out.append({
            "id": tid,
            "symbol": pick(r, "symbol").upper(),
            "side": pick(r, "side").upper(),
            "open": pick(r, "open"),
            "close": pick(r, "close"),
            "entry": pick(r, "entry"),
            "exit": pick(r, "exit"),
            "qty": pick(r, "qty"),
            "fee": money(pick(r, "fee")) or 0.0,
            "swap": money(pick(r, "swap")) or 0.0,
            "pnl": money(pick(r, "pnl")),
            "tq": pick(r, "tq"),
        })
    return out


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
    """Journal rows carry the broker ticket inside Notes as '#<digits>'."""
    if "#" not in note:
        return None
    tail = note.split("#", 1)[1]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    return digits or None


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
    jtickets = {t: r for r in jrows if (t := ticket_of(r.get("Notes", "")))}

    missing = [d for d in deals if d["id"] not in jtickets]
    orphans = [r for r in jrows if ticket_of(r.get("Notes", "")) is None]
    mismatch = []
    for d in deals:
        jr = jtickets.get(d["id"])
        if jr and d["pnl"] is not None:
            jp = money(jr.get("Net_$", ""))
            if jp is not None and abs(jp - d["pnl"]) > 0.01:
                mismatch.append((d, jp))

    exp_pnl = sum(d["pnl"] for d in deals if d["pnl"] is not None)
    jrn_pnl = sum(v for r in jrows if (v := money(r.get("Net_$", ""))) is not None)

    print(f"export  : {len(deals)} deals   net ${exp_pnl:,.2f}")
    print(f"journal : {len(jrows)} rows    net ${jrn_pnl:,.2f}")
    print(f"delta   : ${exp_pnl - jrn_pnl:,.2f}\n")

    ok = True
    if missing:
        ok = False
        print(f"!! {len(missing)} DEAL(S) MISSING FROM THE JOURNAL")
        for d in missing:
            print(f"     #{d['id']}  {d['close']:16} {d['symbol']:7} {d['side']:4} "
                  f"${d['pnl'] if d['pnl'] is not None else 0:>9,.2f}")
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
