#!/usr/bin/env python3
"""Archive the current Weekly Bias rows into a permanent history table.

WHY THIS EXISTS
---------------
`/sd-sunday` REWRITES the weekly-bias table every Sunday. Only the current
week ever exists in the journal; every prior week's flagged setups are gone
unless recovered by git archaeology.

That silently destroyed the evidence base for two things:

  1. The FX suspension review, whose exit condition is "compare the FX setups
     that were flagged but not traded against what price actually did." Nothing
     was accumulating that comparison.
  2. Any assessment of whether the macro gate selects better-than-random
     setups -- the single most important open question about this system.
     Rejected setups are data too, and the gate rejects most of them.

Run this BEFORE /sd-sunday overwrites the table. Rows are keyed on
(Date, Instrument, BB_Zone_Type) -- one row per ZONE, not per instrument, since
2026-08-15. See key_of() below for why: the search now runs both directions, so
an instrument legitimately appears twice on one card and a 2-part key silently
dropped one of them. Running it twice on an unchanged card changes nothing, and
if a row HAS changed since it was archived the archived copy is updated to match.
Existing-row-wins was the original behaviour and was wrong -- a corrected or
re-run Sunday card could never reach the archive, while gate_outcomes.py went on
measuring the stale copy.

USAGE
  python archive_weekly_bias.py            # archive, report what was added
  python archive_weekly_bias.py --check    # report only, write nothing

Exit code 1 means nothing was archived when rows were present (i.e. something
is wrong), so it can be used as a guard in a script.
"""
import sys
from pathlib import Path
from bs4 import BeautifulSoup

JOURNAL = Path(__file__).with_name("index.html")
SRC_ID = "weekly-bias"
DST_ID = "weekly-bias-history"
DST_SECTION = "wbh"


BB_ZONE_TYPE_COL = 11  # Date,Instrument,W_Bias,Strength,Valuation,Seasonal,
                       # COT_Index_Value,COT_Verdict,Macro_Status,
                       # BB_Zone_Top,BB_Zone_Bot,BB_Zone_Type <- 11


def key_of(tr):
    """Dedup key: (Date, Instrument, BB_Zone_Type) -- one row per ZONE.

    ZONE, not instrument, since 2026-08-15. /sd-sunday now searches both
    directions and writes a row per zone, so an instrument can legitimately
    appear twice on one card -- once DEMAND, once SUPPLY.

    Keying on (Date, Instrument) silently archived whichever row came first
    and DROPPED the other, permanently. That is the exact failure this script
    exists to prevent: the second zone would never reach Bias History, and
    gate_outcomes.py would measure half the setups while looking complete.

    Rows predating the change carry a single BB_Zone_Type and key identically
    to before, so already-archived history is unaffected.
    """
    tds = tr.find_all("td")
    if len(tds) < 2:
        return None
    zone = (tds[BB_ZONE_TYPE_COL].get_text(strip=True)
            if len(tds) > BB_ZONE_TYPE_COL else "")
    return (tds[0].get_text(strip=True), tds[1].get_text(strip=True), zone)


def ensure_history_section(soup):
    """Create the nav tab + section + table if absent. Returns the table."""
    existing = soup.find("table", id=DST_ID)
    if existing:
        return existing, False

    src = soup.find("table", id=SRC_ID)
    if src is None:
        raise SystemExit(f"ERROR: no #{SRC_ID} table in {JOURNAL}")

    # --- nav tab, inserted directly after the Weekly Bias tab ---
    nav_tab = soup.find("div", attrs={"data-tab": "wb"})
    if nav_tab is None:
        raise SystemExit("ERROR: could not find the Weekly Bias nav tab")
    new_tab = soup.new_tag("div", attrs={"class": "tab", "data-tab": DST_SECTION})
    new_tab.append("Bias History ")
    cnt = soup.new_tag("span", attrs={"class": "tab-count", "id": f"tc-{DST_SECTION}"})
    new_tab.append(cnt)
    nav_tab.insert_after(new_tab)

    # --- section, inserted directly after the Weekly Bias section ---
    # Guarded like the nav-tab lookup above. Unguarded, a renamed or missing #wb
    # section surfaced as an AttributeError on .insert_after several lines below,
    # which reads as a script bug rather than "the journal structure changed".
    wb_section = soup.find("section", id="wb")
    if wb_section is None:
        raise SystemExit("ERROR: could not find the Weekly Bias section (#wb)")
    section = soup.new_tag("section", attrs={"class": "section", "id": DST_SECTION})

    hdr = soup.new_tag("div", attrs={"class": "section-header"})
    title = soup.new_tag("span", attrs={"class": "section-title"})
    title.string = "Bias History"
    sub = soup.new_tag("span", attrs={"class": "section-sub"})
    sub.string = ("Every Weekly Bias card ever written, including SKIP and [GATED] rows. "
                  "Appended before each /sd-sunday rewrite so flagged-but-not-traded "
                  "setups can be checked against what price did.")
    hdr.append(title)
    hdr.append(sub)
    section.append(hdr)

    wrap = soup.new_tag("div", attrs={"class": "table-wrap"})
    table = soup.new_tag("table", id=DST_ID)
    table.append(src.find("thead").__copy__())      # same columns as the live card
    table.append(soup.new_tag("tbody"))
    wrap.append(table)
    section.append(wrap)
    wb_section.insert_after(section)
    return table, True


def patch_js(html):
    """Wire row tinting + the tab count for the new table."""
    changed = False
    anchor = "  const ctCount = tintRows('closed-trades',  13, resultMap);"
    if anchor in html and "weekly-bias-history'" not in html:
        html = html.replace(
            anchor,
            anchor + "\n  const wbhCount = tintRows('weekly-bias-history', 2, biasMap);",
            1)
        changed = True
    anchor2 = "  setCount('tc-ct', ctCount);"
    if anchor2 in html and "tc-wbh'" not in html:
        html = html.replace(anchor2, anchor2 + "\n  setCount('tc-wbh', wbhCount);", 1)
        changed = True
    return html, changed


def main():
    check_only = "--check" in sys.argv
    soup = BeautifulSoup(JOURNAL.read_text(encoding="utf-8"), "html.parser")

    src = soup.find("table", id=SRC_ID)
    if src is None:
        raise SystemExit(f"ERROR: no #{SRC_ID} table in {JOURNAL}")
    src_rows = [tr for tr in src.find("tbody").find_all("tr") if tr.find("td")]
    if not src_rows:
        print("Weekly Bias table is empty -- nothing to archive.")
        return 0

    dst, created = ensure_history_section(soup)
    dst_body = dst.find("tbody")
    # Map key -> existing row, so a re-archived key can be UPDATED rather than
    # dropped. Previously this was a set and an existing key simply won: a Sunday
    # card that was corrected or re-run could never reach the archive, and because
    # gate_outcomes.py reads history, the stale copy is what got measured. The
    # freshly-written card is by definition the more correct one.
    existing = {}
    for tr in dst_body.find_all("tr"):
        k = key_of(tr)
        if k:
            existing[k] = tr

    added, updated, unchanged = [], [], []
    for tr in src_rows:
        k = key_of(tr)
        if k is None:
            continue
        old = existing.get(k)
        if old is not None:
            if str(old) == str(tr):
                unchanged.append(k)
                continue
            if not check_only:
                old.replace_with(tr.__copy__())
            updated.append(k)
            continue
        if not check_only:
            dst_body.append(tr.__copy__())
        existing[k] = tr
        added.append(k)

    if created:
        print(f"Created #{DST_ID} section (first run).")
    print(f"Archived {len(added)} new, {len(updated)} updated, "
          f"{len(unchanged)} already identical.")
    # Keys are 3-tuples (Date, Instrument, BB_Zone_Type) since 2026-08-15 -- the
    # zone type is printed so a two-row instrument is legible in the report.
    for d, inst, zone in added:
        print(f"  + {d}  {inst:<8} {zone}")
    for d, inst, zone in updated:
        print(f"  ~ {d}  {inst:<8} {zone}  (row changed since it was archived)")
    if unchanged and not added and not updated:
        print("  (nothing new -- already archived)")

    if check_only:
        print("\n--check: no changes written.")
        return 0

    html = str(soup)
    html, js_changed = patch_js(html)
    if js_changed:
        print("Patched JS: row tinting + tab count for the history table.")
    JOURNAL.write_text(html, encoding="utf-8")
    print(f"\nWrote {JOURNAL}")
    print("Now commit and push the journal repo so GitHub Pages picks it up.")
    return 0 if (added or updated or unchanged) else 1


if __name__ == "__main__":
    sys.exit(main())
