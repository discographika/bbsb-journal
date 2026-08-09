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

Run this BEFORE /sd-sunday overwrites the table. It is idempotent: rows are
keyed on (Date, Instrument), so running it twice changes nothing.

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


def key_of(tr):
    """Dedup key: (Date, Instrument) -- one row per instrument per card."""
    tds = tr.find_all("td")
    if len(tds) < 2:
        return None
    return (tds[0].get_text(strip=True), tds[1].get_text(strip=True))


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
    wb_section = soup.find("section", id="wb")
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
    have = {key_of(tr) for tr in dst_body.find_all("tr") if key_of(tr)}

    added, skipped = [], []
    for tr in src_rows:
        k = key_of(tr)
        if k is None:
            continue
        if k in have:
            skipped.append(k)
            continue
        if not check_only:
            dst_body.append(tr.__copy__())
        have.add(k)
        added.append(k)

    if created:
        print(f"Created #{DST_ID} section (first run).")
    print(f"Archived {len(added)} row(s); {len(skipped)} already present.")
    for d, inst in added:
        print(f"  + {d}  {inst}")
    if skipped and not added:
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
    return 0 if (added or skipped) else 1


if __name__ == "__main__":
    sys.exit(main())
