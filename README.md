# bursa-data

马股 59 只（KLCI 30 + 科技 30）的行情数据，每个交易日自动抓取。

这个仓库**只是一个数据中转站**，没有界面。看板本身在别处：
https://claude.ai/code/artifact/a8d2f333-4a0a-4810-b46f-4b18f67ea0d4

## 为什么需要它

跑看板的 Claude 定时任务在一个沙盒里，**外网只通 `api.github.com`**（12 个行情源全部实测被挡）。
所以它自己抓不到 Yahoo。GitHub Actions 的跑机没有这个限制——由它抓数据、提交到这里，
定时任务再通过 GitHub API 把数据读走。

```
GitHub Actions (09:10 UTC / 17:10 马时)  →  data/*.json
                                              ↓  api.github.com
                          Claude 定时任务 (09:40 UTC / 17:40 马时)
                                              ↓
                                      重新发布看板 + 汇总推送
```

## 文件

| 路径 | 内容 |
|---|---|
| `symbols.json` | 59 只股票的代码、Yahoo 符号、名称、板块、分组 |
| `fetch.py` | 抓取脚本，只在 Actions 上跑 |
| `data/meta.json` | 快照日期、分片清单、失败与可疑名单、各股最新收盘价 |
| `data/weekly-*.json` | 周线，每股最多 540 根（10年） |
| `data/daily-*.json` | 日线，每股最多 1250 根（5年） |
| `data/hourly-*.json` | 小时线，每股最多 399 根（60天） |

数据分片是必须的：GitHub contents API 拒绝超过 1MB 的文件，所以每个分片压在 800KB 以内，
清单写在 `meta.json` 的 `chunks` 里。

每根K线是 `[日期, 开, 高, 低, 收]`，升序。周线/日线日期为 `YYYY-MM-DD`，小时线为 `YYYY-MM-DD HH:MM`，
时区一律 UTC+8。

## 两个已知的坑（脚本里已处理，别改掉）

1. **收盘价为 null** — 当天未收盘的那根K线有开高低但 `close` 是 null。日线和小时线要用
   `meta.regularMarketPrice` 补上最后一根，否则日线会被静默截断一个星期。周线**不能**补，
   因为 Yahoo 另外还会附一行实时报价，补了就重复。

2. **本周被拆成两根** — 周线里，进行中的那一周会返回「半周K线 + 一行日期是今天的实时行情」。
   不合并的话当周画成两根蜡烛；更糟的是那行的日期每天往前走，进行中的信号会天天被当成新信号推送。
   所以按 ISO 周合并成一根，日期取该周的**星期一**。

## 手动触发

Actions 页 → Fetch Bursa prices → Run workflow。
