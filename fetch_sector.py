#!/usr/bin/env python3
"""Build the sector board: fetch weekly bars for the Top-20-per-sector universe,
run the basing scan, and write a ready-to-publish HTML into sector/.

The universe is Bursa's OWN 13 sectors (Consumer / Industrial Products & Services,
Construction, Technology, Financial Services, Property, Plantation, REIT, Energy,
Health Care, Telecommunications & Media, Transportation & Logistics, Utilities), taken
from KLSE Screener. It replaced TradingView's 20-sector global taxonomy, which classified
Bursa names into buckets like "Producer Manufacturing" and "Miscellaneous" that mean
nothing to a Malaysian trader. The Yahoo symbol is the Bursa stock code + ".KL", so
symbol resolution is exact - no search-and-guess, no wrong-company matches.

Why this one builds the whole page while fetch.py only writes data: the sector
universe is ~250 stocks (~4.1 MB of bars). The Claude routine cannot assemble that
-- it would have to pull 5 MB through its own context. So the runner does the
assembly and the routine's entire job becomes "publish this file".

Imports fetch.py rather than copying its parser: the null-close and running-week
traps are fixed there, and they must not drift between the two boards.
"""

import json
import os
import sys
from datetime import datetime

import fetch
from fetch import MYT, ROOT, get, parse

OUT = os.path.join(ROOT, "sector")
TEMPLATE = os.path.join(ROOT, "sector_template.html")
KEEP = 540              # 10 years of weekly bars, same depth as the KLCI board
MIN_BARS = 60           # below this there is not enough history for a basing setup
RECENT_WEEKS = 4        # the "just happened" window the notification uses

# Defaults must match the dashboard's own inputs, or the page recomputes something
# different from what the notification announced.
PARAMS = dict(supportWeeks=13, minBase=3, maxBase=12, tolPct=4.0, minReboundPct=1.5)


def scan(w, p):
    """Port of scan() in the dashboard, line for line. Returns the LATEST qualifying
    reversal, or None. Verified against the page: same signals on the same bars."""
    n = len(w)
    low_of = [b[3] for b in w]
    close_of = [b[4] for b in w]
    best = None
    sw = p["supportWeeks"]
    for b in range(sw, n):
        sup = min(low_of[b - sw:b])
        if low_of[b] >= sup:
            continue                                    # must break below the prior low
        bl = low_of[b]
        floor = bl * (1 - p["tolPct"] / 100.0)
        for length in range(p["minBase"], p["maxBase"] + 1):
            end, r = b + length - 1, b + length
            if r >= n:
                break
            lo, li, ok = float("inf"), b, True
            for k in range(b, end + 1):
                if low_of[k] < lo:
                    lo, li = low_of[k], k
                if k > b and low_of[k] < floor:
                    ok = False                          # a meaningful new low breaks the base
                    break
            if not ok:
                continue
            if close_of[r] < sup * (1 + p["minReboundPct"] / 100.0):
                continue                                # the reversal week must clearly reclaim
            cand = {
                "sup": sup, "low": lo, "rev": close_of[r], "revIdx": r,
                "base": length, "ago": n - 1 - r,
                "lowDate": w[li][0], "revDate": w[r][0], "breakDate": w[b][0],
            }
            if best is None or cand["revIdx"] > best["revIdx"]:
                best = cand
            break
    return best


def weekly_range_pct(w):
    """Median (high-low)/close over the last year, in percent. This is the number the
    card shows: a 2% reclaim means one thing on a stock that moves 2% a week and
    something else entirely on one that moves 9%."""
    tail = w[-52:] if len(w) >= 52 else w
    v = sorted((b[2] - b[3]) / b[4] * 100.0 for b in tail if b[4] > 0)
    return round(v[len(v) // 2], 1) if v else 0.0


def js_str(s):
    return json.dumps(s, ensure_ascii=False)


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
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
               "?range=10y&interval=1wk" % sym)
        try:
            bars = parse(get(url), weekly=True)
        except Exception as e:                          # noqa: BLE001 - report, don't abort
            print("  ! %s: %s" % (sym, e), file=sys.stderr)
            bars = []
        bars = bars[-KEEP:] if bars else []

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
            "wr": weekly_range_pct(bars), "w": bars,
        })

    if len(rows) < len(stocks) / 2:
        print("FATAL: only %d/%d symbols fetched" % (len(rows), len(stocks)), file=sys.stderr)
        return 1

    snap = max(r["w"][-1][0] for r in rows)
    os.makedirs(OUT, exist_ok=True)

    # --- signals, and what is new since the last run ------------------------
    sigs = {}
    for r in rows:
        s = scan(r["w"], PARAMS)
        if s and s["ago"] <= RECENT_WEEKS:
            sigs[r["t"]] = {
                "sym": r["sym"], "n": r["n"], "s": r["s"], "i": r["i"], "wr": r["wr"],
                "revDate": s["revDate"], "ago": s["ago"], "base": s["base"],
                "sup": round(s["sup"], 4), "rev": round(s["rev"], 4),
                "reclaim": round((s["rev"] - s["sup"]) / s["sup"] * 100, 1),
            }
    # "new" means a reversal week this ticker did not already have. Comparing the
    # revDate (not just presence) is what stops a signal re-announcing as it ages.
    fresh = {t: v for t, v in sigs.items()
             if prev_sigs.get(t, {}).get("revDate") != v["revDate"]}

    # --- the page ----------------------------------------------------------
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    for marker in ("__DATA__", "__SNAP__"):
        if marker not in html:
            print("FATAL: %s missing from sector_template.html" % marker, file=sys.stderr)
            return 1

    parts = []
    for r in rows:
        bars = ",".join("[%s,%s,%s,%s,%s]" % (js_str(b[0]), b[1], b[2], b[3], b[4])
                        for b in r["w"])
        parts.append('{"t":%s,"sym":%s,"n":%s,"s":%s,"i":%s,"wr":%s,"w":[%s]}' % (
            js_str(r["t"]), js_str(r["sym"]), js_str(r["n"]), js_str(r["s"]),
            js_str(r["i"]), r["wr"], bars))
    html = html.replace("__DATA__", "[" + ",".join(parts) + "]").replace("__SNAP__", snap)
    with open(os.path.join(OUT, "board.html"), "w", encoding="utf-8") as f:
        f.write(html)

    with open(sig_path, "w", encoding="utf-8") as f:
        json.dump(sigs, f, separators=(",", ":"), ensure_ascii=False)
    with open(os.path.join(OUT, "new_signals.json"), "w", encoding="utf-8") as f:
        json.dump(fresh, f, separators=(",", ":"), ensure_ascii=False)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "snap": snap,
            "generated_at": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S +08:00"),
            "count": len(rows),
            "recent": len(sigs),
            "new": len(fresh),
            "failed": failed,
            "suspect": suspect,
            "last_close": {r["sym"]: r["w"][-1][4] for r in rows},
        }, f, separators=(",", ":"), ensure_ascii=False)

    size = os.path.getsize(os.path.join(OUT, "board.html"))
    print("\n%d/%d symbols, snap %s, board.html %.2f MB"
          % (len(rows), len(stocks), snap, size / 1048576.0))
    print("recent(<=%dw): %d, new since last run: %d" % (RECENT_WEEKS, len(sigs), len(fresh)))
    if fresh:
        print("new: " + ", ".join("%s(%s)" % (t, v["revDate"]) for t, v in fresh.items()))
    if failed:
        print("failed: " + ", ".join(failed))
    if suspect:
        print("suspect (kept out): " + ", ".join(suspect))
    return 0


if __name__ == "__main__":
    sys.exit(main())
