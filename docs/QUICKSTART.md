# QUICKSTART｜五分钟跑起来（面向新下载用户）

> 系统主 README 只记录研究演进；本文是"下载后怎么跑"的完整路径。若卡在任何一步，先看文末自检。

## 0. 前置

- Windows 10/11 x64（其他平台未验证，协议源是通达信/东财的公开接口，Mac/Linux 自行摸索）
- Python 3.13.x （3.12 也应可，数值结果可能浮动）
- GitHub 账号一（拉这个仓库需可见权限：本仓库默认 **Private**，联系作者加 collaborator）

## 1. 下载

```bash
git clone https://github.com/zxy-pishuai/astock-sim.git
cd astock-sim
```

（没有 git 的话，网页 Code→Download ZIP 亦可。）

## 2. 装依赖

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

要求见 `requirements.txt`（numpy/pandas/scipy + akshare/mootdx/tdxpy/baostock + requests…）。

## 3. 起一个服务（三种模式）

```bash
python main.py --cli          # 自检模式，看系统是否装好了
python main.py --browser      # 打开浏览器，Web 界面（本机 http://127.0.0.1:8xxx）
python main.py                # 默认桌面窗（pywebview + WebView2）
python main.py --port 8899    # 指定端口
```

## 4. 首次数据建库（重要：仓库不带你的真实行情缓存）

我先删了两个最大的文件再上传：

- `data/market.db`（~1.9 GB 日线/账户）
- `data/min5.db`（~0.7 GB；5 分钟线）
- `data/snapshots/`、`data/backups/`、`data/bt_cache/`、`data/keypack/`、`data/account.json`

因此你看到的是**从未有数据的状态**（`app/datafeed._init_db` 会自动建空库结构）。
数据有两种获取方式：

- **自建（有耐心）**：`python tools/daily_backfill.py`（时间/网络依赖大，全 A 股），加 `tools/fetch_index_kline.py` 取指数；
- **拷快照（快，推荐）**：联系作者拿 `2026-09-16` 打包快照（约 6 GB），解压到 `data/market.db` + `data/min5.db` 即可。

## 5. 常见入口

| 你想… | 去哪儿 |
|---|---|
| 看盘 | `main.py --browser` 打开 Web 面板 |
| 核对今天的数据落库对不对 | `data/audit/audit.jsonl`（每轮 append） |
| 手动跑一次盘后数据更新 | `python tools/daily_backfill.py` |
| 跑一次完整自检 | `python tools/selfcheck.py` 或 `python tools/preflight.py` |
| 建快照 | `python tools/snapshot_archive.py` |
| 引擎直接回测 A/B | `python` 里 `app/engine.py`（README §阶段2 例子） |

## 6. 自检排错

| 现象 | 定位 |
|---|---|
| `pip install` 某包编不过 | 严格按 `requirements.lock` 装那一个即可 |
| `main.py --cli` 中文乱码 | Windows 下用 `.py` 首行 UTF8 工具；`PYTHONUTF8=1` 兜底 |
| 打不开 Web | 端口占用：`--port 8899` 或防火墙 |
| 数据始终空 | 看上面 §4；
`tools/selfcheck.py` 里能查到当前 db 是否可写 |
| TDX 夜档 | 免费源"作息"，白天再拉；盘后拉数据最好别卡 23:30 后 |

## 7. 更新

```bash
git pull
```

没有其他依赖性变更；`data/` 目录你本地的运行时数据不会被覆盖（仓库不含）。

## 8. 未随仓库发布的内容 & 已知取舍

- `data/keypack/`、`data/account.json`：**未随库**（含账户/持仓状态），仅作者本机保留。
- `docs/` 里有 272 个研究报告——是作者的研究档案，读不解，可从 `docs/reports/phase0…` 起按序读。
- 运行流程长changlog见 `README.md`（这是作者的工作日志，不是说明书）。
