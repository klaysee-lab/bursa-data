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

## template.html

看板的**干净底稿**，数据位置留了四个占位符：`__DATA__`、`__DH__`、`__SNAP__`、`__PREVSIG__`。

定时任务必须从这个模板重建页面，**不能拿读回来的成品当底稿**。原因是发布平台会在页面顶部注入约 36KB 的
frame-runtime 前导码；用成品当底稿会把前导码一起烤进源码，于是：

`<title>` 会被挤出发布时扫描标题的 8KB 窗口，于是看板在画廊里被改名成 "new_artifact"。
2026-09-02 的首次真实运行就是这样被改名的，已修。

（实测前导码**不会**累积：平台在发布时会剥掉旧的 frame-runtime 块再重新注入，两次读回的页面大小
分别是 4490640 和 4490642 字节，基本相同。所以后果只有改名这一项——因为扫描标题发生在剥离之前。
仍然要用模板重建，这是最省事也最不会出错的做法。）

## 板块看板（sector/）

`fetch_sector.py` 和 `fetch.py` 的分工**故意不一样**，改动前先看懂原因：

- `fetch.py`（KLCI 59 只）只提交数据，页面由云端任务用 `template.html` 拼。
- `fetch_sector.py`（322 只）在 runner 上**直接把成品页面建好**提交（`sector/board.html`），云端任务只负责原样发布。

原因：322 只周线约 5MB，云端任务要拼页面就得把这 5MB 读进自己的上下文，这条路走不通。所以谁有算力谁干活——runner 有完整的网络和内存，云端任务只做它非做不可的那件事（发布 artifact）。

`fetch_sector.py` **import** `fetch.py`，不是复制。null-close 和 running-week 两个 Yahoo 陷阱的修复只能有一份，两个看板不能各改各的。

它还自己跑一遍扫描，写出 `sector/new_signals.json`——比对的是 **revDate** 而不是「有没有信号」，所以一个信号变旧不会天天重复通知。云端任务那边另有一道哨兵：新增超过 25 只就当作基线重置，只报数字不列清单。

改页面的流程：编辑 `bursa-sector/shell.html` → 跑 `assemble.pl`（本地那份）→ 跑 `mktemplate.pl`（更新本仓库的 `sector_template.html`）。**两个都要跑**，只跑前者的话 runner 明天还是建旧页面。
