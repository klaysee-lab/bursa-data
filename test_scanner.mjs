import assert from "node:assert/strict";
import test from "node:test";
import { scanDaily, scanWeekly } from "./scanner.js";

const bar = (date, close, low = close - 0.1, high = close + 0.1, volume = 100) =>
  [date, close, high, low, close, volume];
const weeks = [
  ...Array.from({ length: 6 }, (_, i) => bar(`2024-0${i + 1}-01`, 10)),
  bar("2024-07-01", 8.8, 8.5, 9),
  bar("2024-08-01", 8.8, 8.7, 9),
  bar("2024-09-01", 9.0, 8.8, 9.1),
  bar("2024-10-01", 10.2, 9.8, 10.3)
];

test("requires a break, base and reclaim instead of one up bar", () => {
  const result = scanWeekly(weeks, { supportWeeks: 6, minBaseWeeks: 2, maxBaseWeeks: 3 });
  assert.equal(result.status, "reclaimed");
  assert.equal(result.breakDate, "2024-07-01");
  assert.equal(result.reclaimDate, "2024-10-01");
});

test("rejects a base that keeps making lower lows", () => {
  const broken = [...weeks];
  broken[7] = bar("2024-08-01", 8.4, 7.5, 8.8);
  assert.notEqual(scanWeekly(broken, { supportWeeks: 6 }).status, "reclaimed");
});

test("finds a dry-up pullback followed by a pressure breakout", () => {
  const daily = [
    ...Array.from({ length: 20 }, (_, i) => bar(`2024-10-${String(i + 1).padStart(2, "0")}`, 10, 9.8, 10.2, 100)),
    bar("2024-11-01", 10.0, 9.95, 10.1, 50),
    bar("2024-11-02", 10.5, 10.0, 10.6, 120)
  ];
  const result = scanDaily(daily, { reclaimDate: "2024-10-01", support: 9.5 }, {});
  assert.equal(result.status, "entry");
  assert.equal(result.pullback.date, "2024-11-01");
});
