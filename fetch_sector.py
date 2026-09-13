#!/usr/bin/env python3
"""Build the sector board: fetch DAILY bars for the ~666-name universe, fold them into
weekly bars, and splice them into a ready-to-publish HTML.

THIS FILE DELIBERATELY CONTAINS NO SCAN LOGIC.

It used to carry a Python port of the dashboard's scan(), "line for line". Four months
later the two had drifted into different rules entirely: the port still used a 13-week
support window and a 1.5% close-based reclaim, while the dashboard had moved to 52 weeks,
a breakout line derived from the break week's HIGH, tick-size alignment, a pullback band
and two daily confirmation steps. Nobody noticed, and the cloud board quietly published
signals the local board did not agree with. So the port is gone. The rule now lives in
exactly one place, sector_template.html, and the notification digest is produced by
running that same page (see signals()).

Why daily bars for a weekly board: Yahoo's own weekly series inherits its highs and lows
from zero-volume placeholder days (see the trap note in fetch.parse). Those cannot be
detected at weekly resolution, because a weekly bar carries no volume. Fetching daily,
dropping the placeholders and folding the survivors into weeks is the only way to get a
clean weekly high. Costs roughly 25x the bytes of the weekly endpoint; worth it.
"""

import html as htmlmod
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

import fetch
from fetch import MYT, ROOT, get, parse

OUT = os.path.join(ROOT, "sector")
TEMPLATE = os.path.join(ROOT, "sector_template.html")
KEEP_CORE = 540         # weeks of history for the names the sector grids render
KEEP_EXTRA = 260        # search-only tier: 5 years is plenty for a basing setup
MIN_BARS = 8            # below this the fetch is broken, not the stock (see note)
# MIN_BARS is a data-integrity floor, NOT a "can this stock produce a signal" floor.
# Those are three different numbers and conflating them cost the board 43 names:
#
#   8   weekly bars - below this, what came back is a broken fetch, not a young stock
#   28  weekly bars - the E rule's minimum (its WARM is 26: the break has to land after
#                     week 26 and the reversal the week after)
#   53  weekly bars - the A/C/D rule's minimum, because supportWeeks is 52
#
# This used to read 60, which is none of those. sector_symbols.json carries 709 names;
# 60 silently dropped 43 of them, every one a 2025-or-later listing, and the board went
# out as "666 names, market cap 100m and up, not one missing" while being exactly that
# minus every recent IPO. Two of the 43 are dead tickers (BPROP 4219, RSSB 9776 - Yahoo
# returns a single bar); the other 41 had 8 to 59 real weekly bars.
#
# A name with fewer than 53 bars still renders here - chart, sector grid, search - it
# just never produces a signal under the A rule, and its card says so. That is the
# correct outcome, not a gap to be papered over by excluding it.


def weekly_from_daily(js):
    """Daily Yahoo JSON -> clean weekly bars. parse() applies the null-close and
    zero-volume filters at daily resolution, then merge_weekly folds by ISO week."""
    daily = parse(js)                       # daily semantics: the live close IS filled in
    if not daily:
        return []
    return fetch.merge_weekly(daily, None)


def weekly_range_pct(w):
    """Median (high-low)/close over the last year, in percent.

    THE PAGE DOES NOT USE THIS. render() recomputes it with wrOf() from the bars, so
    the authoritative implementation is the one in the template, same as the scan.
    It is still written into the data blob because the format has always carried a
    "wr" field, and a field that is present but wrong is worse than one that matches.

    First cut of this file got both halves of wrOf() wrong and nobody would have
    noticed without a side-by-side: it kept zero-range weeks (a week whose high equals
    its low) in the sample instead of dropping them, and it took the upper-middle
    element instead of averaging the middle two. 23 of 56 signals came out with a
    different wr; HIL alone read 3.0 against the page's 3.7, which moved ADB's
    breakout line from 0.61 to 0.615. wr feeds the breakout line, so that matters.
    """
    tail = w[-52:] if len(w) >= 52 else w
    v = sorted(x for x in ((b[2] - b[3]) / b[4] * 100.0 for b in tail if b[4] > 0) if x > 0)
    if not v:
        return 0.0
    n = len(v)
    mid = v[(n - 1) // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0
    return round(mid, 1)


def js_str(s):
    return json.dumps(s, ensure_ascii=False)


def find_chrome():
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        p = shutil.which(name)
        if p:
            return p
    return None


def signals(board_path):
    """Run the finished page under headless Chrome with ?signals=1 and read back the
    JSON it prints. The page computes it with the same scan() the reader sees, so the
    digest cannot disagree with the board. No network: the data is already inlined."""
    chrome = find_chrome()
    if not chrome:
        print("  ! no chrome found; skipping the digest", file=sys.stderr)
        return None
    url = "file://" + board_path.replace(os.sep, "/") + "?signals=1"
    with tempfile.TemporaryDirectory() as prof:
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--no-first-run", "--virtual-time-budget=120000",
               "--user-data-dir=" + prof, "--dump-dom", url]
        try:
            done = subprocess.run(cmd, capture_output=True, timeout=300)
        except Exception as e:                              # noqa: BLE001
            print("  ! chrome failed: %s" % e, file=sys.stderr)
            return None
    dom = done.stdout.decode("utf-8", "replace")
    m = re.search(r'<pre id="sigout">(.*?)</pre>', dom, re.S)
    if not m:
        print("  ! the page produced no #sigout block", file=sys.stderr)
        return None
    try:
        return json.loads(htmlmod.unescape(m.group(1)))
    except ValueError as e:
        print("  ! #sigout was not JSON: %s" % e, file=sys.stderr)
        return None


def main():
    with open(os.path.join(ROOT, "sector_symbols.json"), encoding="utf-8") as f:
        stocks = json.load(f)

    prev_close, prev_sigs = {}, {}
    meta_path = os.path.join(OUT, "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            prev_close = json.load(f).get("last_close", {})
    sig_path = os.path.join(OUT, "signals.json")
    if os.path.exists(sig_path):
        with open(sig_path, encoding="utf-8") as f:
            prev_sigs = json.load(f)

    rows, failed, suspect = [], [], []
    for st in stocks:
        sym = st["sym"]
        # 10y daily for everyone. The weekly history kept is trimmed afterwards, but the
        # placeholder filter has to see every day in order to rebuild a week correctly.
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
               "?range=10y&interval=1d" % sym)
        try:
            bars = weekly_from_daily(get(url))
        except Exception as e:                              # noqa: BLE001 - report, do not abort
            print("  ! %s: %s" % (sym, e), file=sys.stderr)
            bars = []
        keep = KEEP_EXTRA if st.get("x") else KEEP_CORE
        bars = bars[-keep:] if bars else []

        if len(bars) < MIN_BARS:
            failed.append(sym)
            print("  ! %s: only %d weekly bars" % (sym, len(bars)), file=sys.stderr)
            continue
        bad = fetch.check_weekly(bars)
        if bad:
            failed.append(sym)
            print("  ! %s: %s" % (sym, bad), file=sys.stderr)
            continue
        # A >40% move against the last run usually means the ticker resolved to a
        # different company, not a real move. Keep it out rather than corrupt the chart.
        was = prev_close.get(sym)
        if was and was > 0 and abs(bars[-1][4] - was) / was > 0.40:
            suspect.append("%s (%.4f -> %.4f)" % (sym, was, bars[-1][4]))
            continue

        rows.append({
            "t": st["t"], "sym": sym, "n": st["n"], "s": st["s"], "i": st.get("i", ""),
            "x": int(st.get("x", 0)),
            "wr": weekly_range_pct(bars), "w": bars,
        })

    if len(rows) < len(stocks) / 2:
        print("FATAL: only %d/%d symbols fetched" % (len(rows), len(stocks)), file=sys.stderr)
        return 1

    snap = max(r["w"][-1][0] for r in rows)
    os.makedirs(OUT, exist_ok=True)

    # --- the page ----------------------------------------------------------
    with open(TEMPLATE, encoding="utf-8") as f:
        page = f.read()
    for marker in ("__DATA__", "__SNAP__"):
        if marker not in page:
            print("FATAL: %s missing from sector_template.html" % marker, file=sys.stderr)
            return 1

    parts = []
    for r in rows:
        bars = ",".join("[%s,%s,%s,%s,%s]" % (js_str(b[0]), b[1], b[2], b[3], b[4])
                        for b in r["w"])
        parts.append('{"t":%s,"sym":%s,"n":%s,"s":%s,"i":%s%s,"wr":%s,"w":[%s]}' % (
            js_str(r["t"]), js_str(r["sym"]), js_str(r["n"]), js_str(r["s"]),
            js_str(r["i"]), ',"x":1' if r["x"] else '', r["wr"], bars))
    page = page.replace("__DATA__", "[" + ",".join(parts) + "]").replace("__SNAP__", snap)
    board = os.path.join(OUT, "board.html")
    with open(board, "w", encoding="utf-8") as f:
        f.write(page)

    # --- the digest, computed BY the page just written ---------------------
    dump = signals(os.path.abspath(board))
    sigs = {}
    if dump:
        for s in dump.get("signals", []):
            sigs[s["t"]] = {
                "sym": s["sym"], "n": s["n"], "s": s["s"], "i": s["i"], "wr": s["wr"],
                "core": bool(s["core"]),
                "revDate": s["revDate"], "breakDate": s["breakDate"],
                "ago": s["ago"], "base": s["base"],
                "sup": round(s["sup"], 4), "rev": round(s["rev"], 4),
                "lvl": round(s["lvl"], 4), "bh": round(s["bh"], 4), "bl": round(s["bl"], 4),
                "band": [round(s["band"][0], 4), round(s["band"][1], 4)],
                "reclaim": s["reclaim"],
            }
    # "new" means a reversal week this ticker did not already have. Comparing revDate
    # (not just presence) is what stops a signal re-announcing as it ages.
    fresh = {t: v for t, v in sigs.items()
             if prev_sigs.get(t, {}).get("revDate") != v["revDate"]}

    with open(sig_path, "w", encoding="utf-8") as f:
        json.dump(sigs, f, separators=(",", ":"), ensure_ascii=False)
    with open(os.path.join(OUT, "new_signals.json"), "w", encoding="utf-8") as f:
        json.dump(fresh, f, separators=(",", ":"), ensure_ascii=False)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "snap": snap,
            "generated_at": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S +08:00"),
            "count": len(rows),
            "core": sum(1 for r in rows if not r["x"]),
            "recent": len(sigs),
            "new": len(fresh),
            "digest_ok": bool(dump),
            "params": (dump or {}).get("params"),
            "failed": failed,
            "suspect": suspect,
            "last_close": {r["sym"]: r["w"][-1][4] for r in rows},
        }, f, separators=(",", ":"), ensure_ascii=False)

    size = os.path.getsize(board)
    print("\n%d/%d symbols, snap %s, board.html %.2f MB"
          % (len(rows), len(stocks), snap, size / 1048576.0))
    if dump:
        print("recent: %d (core %d), new since last run: %d"
              % (len(sigs), sum(1 for v in sigs.values() if v["core"]), len(fresh)))
        if fresh:
            print("new: " + ", ".join("%s(%s)" % (t, v["revDate"]) for t, v in fresh.items()))
    else:
        print("!! digest unavailable - the page did not produce signals")
    if failed:
        print("failed: " + ", ".join(failed))
    if suspect:
        print("suspect (kept out): " + ", ".join(suspect))
    return 0


if __name__ == "__main__":
    sys.exit(main())
