/* Bursa break-and-reclaim scanner. Bars are [date, open, high, low, close, volume?]. */

export const DEFAULT_PARAMS = {
  supportWeeks: 26,
  minBaseWeeks: 2,
  maxBaseWeeks: 12,
  baseTolerancePct: 3,
  reclaimPct: 1,
  pullbackPct: 3,
  volumeDryUpRatio: 0.8,
  breakoutVolumeRatio: 1,
  recentWeeks: 6
};

const median = values => {
  const a = values.filter(Number.isFinite).sort((x, y) => x - y);
  if (!a.length) return null;
  const m = Math.floor(a.length / 2);
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
};

const weekOf = date => {
  const d = new Date(`${date.slice(0, 10)}T00:00:00`);
  const monday = new Date(d);
  monday.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return monday.toISOString().slice(0, 10);
};

export function parseBars(value) {
  if (!Array.isArray(value)) return [];
  return value.map(row => {
    if (Array.isArray(row)) {
      const [date, open, high, low, close, volume] = row;
      return [String(date), +open, +high, +low, +close, volume == null ? null : +volume];
    }
    const date = row.date ?? row.datetime ?? row.time;
    return [String(date), +(row.open ?? row.Open), +(row.high ?? row.High),
      +(row.low ?? row.Low), +(row.close ?? row.Close), row.volume == null ? null : +(row.volume ?? row.Volume)];
  }).filter(b => b[0] && [1, 2, 3, 4].every(Number.isFinite))
    .sort((a, b) => a[0].localeCompare(b[0]));
}

export function scanWeekly(bars, params = {}) {
  const p = { ...DEFAULT_PARAMS, ...params };
  const w = parseBars(bars);
  if (w.length < p.supportWeeks + p.minBaseWeeks + 1) {
    return { status: "insufficient", score: 0, reason: "周线少于扫描所需长度" };
  }
  let candidate = null;
  for (let breakIndex = p.supportWeeks; breakIndex < w.length - p.minBaseWeeks; breakIndex += 1) {
    const support = Math.min(...w.slice(breakIndex - p.supportWeeks, breakIndex).map(b => b[3]));
    if (!(w[breakIndex][3] < support)) continue;
    const breakLow = w[breakIndex][3];
    for (let baseWeeks = p.minBaseWeeks; baseWeeks <= p.maxBaseWeeks; baseWeeks += 1) {
      const reclaimIndex = breakIndex + baseWeeks;
      if (reclaimIndex >= w.length) break;
      const base = w.slice(breakIndex, reclaimIndex);
      if (base.some((bar, i) => i > 0 && bar[3] < breakLow * (1 - p.baseTolerancePct / 100))) continue;
      if (w[reclaimIndex][4] < support * (1 + p.reclaimPct / 100)) continue;
      candidate = {
        support, breakLow, breakIndex, reclaimIndex, baseWeeks,
        breakDate: w[breakIndex][0], lowDate: base.reduce((x, b) => b[3] < x[3] ? b : x, base[0])[0],
        reclaimDate: w[reclaimIndex][0], weeksAgo: w.length - 1 - reclaimIndex
      };
    }
  }
  if (!candidate) return { status: "none", score: 0, reason: "暂无满足周线破底、打底、收复的组合" };
  const latest = w[w.length - 1][4];
  const badTrend = w.length >= 4 && w.slice(-3).every((b, i, a) => i === 0 || (b[2] < a[i - 1][2] && b[3] < a[i - 1][3]));
  const invalid = latest < candidate.breakLow;
  return {
    status: invalid ? "invalid" : "reclaimed",
    score: invalid || badTrend ? -1 : 1,
    ...candidate,
    badTrend,
    invalid,
    reason: invalid ? "最新收盘跌回破底低点以下" : badTrend ? "最近高点与低点连续下移" : "周线结构通过"
  };
}

export function scanDaily(bars, weekly, params = {}) {
  const p = { ...DEFAULT_PARAMS, ...params };
  const d = parseBars(bars);
  const volumeAvailable = d.some(b => Number.isFinite(b[5]) && b[5] > 0);
  if (!weekly || !weekly.reclaimDate || d.length < 25) return { status: "insufficient", score: 0, reason: "日线不足以确认回踩与突破" };
  const after = d.filter(b => b[0] >= weekly.reclaimDate);
  if (after.length < 3) return { status: "waiting", score: 0, reason: "等待翻转后的日线形成回踩" };
  const resistance = Math.max(...d.slice(-Math.min(10, d.length)).map(b => b[2]));
  const recentVol = median(d.slice(-21).map(b => b[5]));
  let pullback = null;
  let entry = null;
  for (let i = 1; i < after.length; i += 1) {
    const bar = after[i];
    const dry = recentVol == null || bar[5] == null || bar[5] <= recentVol * p.volumeDryUpRatio;
    const holds = bar[3] >= weekly.support * (1 - p.pullbackPct / 100);
    if (holds && dry && !pullback) pullback = { date: bar[0], index: i, volumeDry: dry };
    if (pullback && i > pullback.index) {
      const priorHigh = Math.max(...after.slice(Math.max(0, i - 5), i).map(b => b[2]));
      const breakout = bar[4] > priorHigh;
      const volumeOk = recentVol == null || bar[5] == null || bar[5] >= recentVol * p.breakoutVolumeRatio;
      if (breakout && volumeOk) { entry = { date: bar[0], price: bar[4], resistance: priorHigh }; break; }
    }
  }
  const latest = d[d.length - 1][4];
  if (latest < weekly.support * (1 - p.pullbackPct / 100)) {
    return { status: "invalid", score: -1, reason: "日线回踩失守周线支撑", pullback };
  }
  const volumeNote = volumeAvailable ? "" : "（该数据源没有成交量，量能条件未验证）";
  if (entry) return { status: "entry", score: volumeAvailable ? 2 : 1, pullback, entry, volumeAvailable, reason: `守支撑后突破短线压力${volumeNote}` };
  if (pullback) return { status: "pullback", score: 1, pullback, volumeAvailable, reason: `已回踩守住支撑，等待短线压力突破${volumeNote}` };
  return { status: "waiting", score: 0, volumeAvailable, reason: `周线已翻，日线尚未出现合格回踩${volumeNote}` };
}

export function scanStock(stock, weekly, daily, params = {}) {
  const week = scanWeekly(weekly, params);
  const day = scanDaily(daily, week.status === "reclaimed" ? week : null, params);
  const score = week.score + (week.status === "reclaimed" ? day.score : 0);
  const status = week.status === "insufficient" || day.status === "insufficient" ? "insufficient" :
    week.status === "invalid" || day.status === "invalid" ? "invalid" :
    day.status === "entry" ? "entry" : day.status === "pullback" ? "pullback" :
    week.status === "reclaimed" ? "reclaimed" : week.status;
  return { ...stock, week, day, score, status, confidence: Math.max(0, Math.min(100, 50 + score * 15)) };
}

export { weekOf };
