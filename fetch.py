#!/usr/bin/env python3
"""Fetch Bursa Malaysia OHLC from Yahoo Finance and write chunked JSON into data/.

Runs on a GitHub Actions runner, which has unrestricted outbound network access.
The Claude routine that republishes the dashboard cannot reach Yahoo (its sandbox
allows only api.github.com), so this job is what actually gets the prices; the
routine then reads the committed files through the GitHub contents API.

Chunking matters: the contents API refuses files over 1 MB, so every emitted file
is kept under CHUNK_LIMIT and data/meta.json lists them.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

MYT = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "data")

# Depths the dashboard's range buttons rely on. Shortening any of these makes
# 周线10年 / 日线5年 / 小时线60天 collapse into the shorter option next to them.
SERIES = [
    ("w", "10y", "1wk", 540),
    ("d", "5y", "1d", 1250),
    ("h", "60d", "60m", 399),
]
CHUNK_LIMIT = 800_000          # bytes per output file, safely under the 1 MB API cap
UA = "Mozilla/5.0 (compatible; bursa-data/1.0)"


def get(url, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as e:
            if attempt == tries - 1:
                raise
            # Yahoo rate-limits bursts; back off rather than hammering.
            time.sleep(2 * (attempt + 1))
    return None


def wk_monday(dt):
    return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")


def parse(js, hourly=False, weekly=False):
    """Yahoo chart JSON -> ascending [[date, o, h, l, c], ...]."""
    res = (js or {}).get("chart", {}).get("result")
    if not res:
        return []
    r = res[0]
    ts = r.get("timestamp") or []
    q = (r.get("indicators", {}).get("quote") or [{}])[0]
    o, h, l, c = (q.get(k) or [] for k in ("open", "high", "low", "close"))
    live = (r.get("meta") or {}).get("regularMarketPrice")

    bars = []
    n = len(ts)
    for i in range(n):
        if i >= len(o) or i >= len(h) or i >= len(l) or i >= len(c):
            break
        if o[i] is None or h[i] is None or l[i] is None:
            continue
        close = c[i]
        if close is None:
            # NULL-CLOSE TRAP: the in-progress session has open/high/low but a null
            # close. Dropping it silently truncated the daily series by up to a week.
            # Fill from the live quote -- but never on weekly, where Yahoo already
            # appends a separate live row that would then duplicate the week.
            if weekly or i != n - 1 or live is None:
                continue
            close = live
        dt = datetime.fromtimestamp(ts[i], MYT)
        stamp = dt.strftime("%Y-%m-%d %H:%M") if hourly else dt.strftime("%Y-%m-%d")
        bars.append([stamp, round(o[i], 4), round(h[i], 4), round(l[i], 4), round(close, 4)])

    if weekly:
        bars = merge_weekly(bars, ts)
    return bars


def merge_weekly(bars, ts):
    """RUNNING-WEEK SPLIT TRAP: Yahoo returns the in-progress week as two rows --
    a part-week bar plus a live row dated today. Left alone the current week draws
    as two candles, and the live row's date advances daily, so a reversal in the
    running week looks brand new on every run and notifies repeatedly. Fold every
    bar sharing an ISO week into one, dated that week's Monday."""
    out = []
    for b in bars:
        key = wk_monday(datetime.strptime(b[0], "%Y-%m-%d"))
        if out and out[-1][0] == key:
            prev = out[-1]
            prev[2] = max(prev[2], b[2])
            prev[3] = min(prev[3], b[3])
            prev[4] = b[4]
        else:
            out.append([key, b[1], b[2], b[3], b[4]])
    return out


def check_weekly(out):
    seen = set()
    for d, *_ in out:
        if d in seen:
            return "duplicate weekly date %s" % d
        seen.add(d)
        if datetime.strptime(d, "%Y-%m-%d").weekday() != 0:
            return "weekly date %s is not a Monday" % d
    return None


def chunk(payload, prefix, names):
    """Split {symbol: bars} into files under CHUNK_LIMIT, recording their names."""
    cur, idx = {}, 0
    for sym, bars in payload.items():
        cur[sym] = bars
        if len(json.dumps(cur, separators=(",", ":"))) > CHUNK_LIMIT:
            del cur[sym]
            if cur:
                names.append(write(prefix, idx, cur))
                idx += 1
            cur = {sym: bars}
    if cur:
        names.append(write(prefix, idx, cur))


def write(prefix, idx, obj):
    name = "%s-%d.json" % (prefix, idx)
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    return name


def main():
    with open(os.path.join(ROOT, "symbols.json"), encoding="utf-8") as f:
        stocks = json.load(f)

    prev = {}
    meta_path = os.path.join(OUT, "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            prev = json.load(f).get("last_close", {})

    series = {k: {} for k, _, _, _ in SERIES}
    failed, suspect = [], []

    for st in stocks:
        sym = st["sym"]
        got = {}
        for key, rng, iv, keep in SERIES:
            url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
                   "?range=%s&interval=%s" % (sym, rng, iv))
            try:
                bars = parse(get(url), hourly=(key == "h"), weekly=(key == "w"))
            except Exception as e:                       # noqa: BLE001 - report, don't abort
                print("  ! %s %s: %s" % (sym, key, e), file=sys.stderr)
                bars = []
            got[key] = bars[-keep:] if bars else []

        if len(got["w"]) < 40:
            failed.append(sym)
            print("  ! %s: only %d weekly bars, skipped" % (sym, len(got["w"])), file=sys.stderr)
            continue

        bad = check_weekly(got["w"])
        if bad:
            failed.append(sym)
            print("  ! %s: %s" % (sym, bad), file=sys.stderr)
            continue

        # A >40% jump against the last run usually means the symbol resolved to a
        # different company, not a real move. Keep it out rather than corrupt the chart.
        last = got["w"][-1][4]
        was = prev.get(sym)
        if was and was > 0 and abs(last - was) / was > 0.40:
            suspect.append("%s (%.4f -> %.4f)" % (sym, was, last))
            continue

        for key in series:
            series[key][sym] = got[key]
        print("  %-10s w=%-4d d=%-5d h=%-4d  %s" % (sym, len(got["w"]), len(got["d"]),
                                                    len(got["h"]), got["w"][-1][0]))

    ok = len(series["w"])
    if ok < len(stocks) / 2:
        print("FATAL: only %d/%d symbols fetched" % (ok, len(stocks)), file=sys.stderr)
        return 1

    for f in os.listdir(OUT):
        if f.endswith(".json"):
            os.remove(os.path.join(OUT, f))

    chunks = {}
    for key, prefix in (("w", "weekly"), ("d", "daily"), ("h", "hourly")):
        names = []
        chunk(series[key], prefix, names)
        chunks[key] = names

    snap = max(b[-1][0] for b in series["w"].values())
    meta = {
        "snap": snap,
        "generated_at": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S +08:00"),
        "count": ok,
        "chunks": chunks,
        "failed": failed,
        "suspect": suspect,
        "last_close": {s: b[-1][4] for s, b in series["w"].items()},
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"))

    print("\n%d/%d symbols, snap %s" % (ok, len(stocks), snap))
    print("chunks: " + ", ".join("%s=%d" % (k, len(v)) for k, v in chunks.items()))
    if failed:
        print("failed: " + ", ".join(failed))
    if suspect:
        print("suspect (kept out): " + ", ".join(suspect))
    return 0


if __name__ == "__main__":
    sys.exit(main())
