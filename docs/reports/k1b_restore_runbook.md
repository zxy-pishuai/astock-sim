# K1b｜重建手册（三场景可照抄执行）

- 日期：2026-09-05
- 适用：极小关键包 `data/keypack/keypack_*.tar.gz`（同盘，防误删/写坏，不防盘坏）
- 前置：所有操作前先 `Set-Location C:\Users\26838\A股模拟盘`；`$env:PYTHONIOENCODING='utf-8'`
- 铁律：还原到 `tmp/restore_test/` 先验证，**禁直接覆盖生产路径**；确认无误后再人工拷贝覆盖

---

## 场景 A：account.json 被写坏 / 误删

**症状**：服务启动报错 `JSON decode error` / 持仓丢失 / 现金异常 / 审计链与账户不一致。

### 步骤

**A1. 确认损坏范围（只读，1 分钟）**
```powershell
# 检查 account.json 是否可解析
py -3.13 -c "import json; d=json.load(open('data/account.json',encoding='utf-8')); print('cash=',d.get('cash'),'trades=',len(d.get('trades',[])),'positions=',len(d.get('positions',{})))"
# 若报错 → 文件损坏；若数值异常 → 逻辑损坏
```

**A2. 从 keypack 还原到临时目录（2 分钟）**
```powershell
# 找最新归档
$arc = (Get-ChildItem 'data\keypack\keypack_*.tar.gz' | Sort-Object Name -Descending | Select-Object -First 1).FullName
Write-Output "using: $arc"
# 解包到临时目录
New-Item -ItemType Directory -Force 'tmp\restore_test' | Out-Null
py -3.13 -c "
import tarfile, sys
tarfile.open(r'$arc','r:gz').extractall('tmp/restore_test', filter='data')
print('extracted')
"
# 验证还原件 SHA256
py -3.13 tools\backup_key_pack.py --verify $arc
```

**A3. 与 git nightly 历史交叉核对（3 分钟）**
```powershell
# git nightly 兜住了 account.json（.gitignore 未排除它）
git log --oneline -5 -- data/account.json
# 找最后正常版本（在损坏时间之前）
git log -p --since="2026-09-01" -- data/account.json | Select-Object -First 50
# 检出历史版本到临时文件对比
git show HEAD~1:data/account.json > tmp\restore_test\account_git.json
py -3.13 -c "
import json
a=json.load(open('tmp/restore_test/data/account.json',encoding='utf-8'))
b=json.load(open('tmp/restore_test/account_git.json',encoding='utf-8'))
print('keypack: cash=%s trades=%d positions=%d' % (a.get('cash'),len(a.get('trades',[])),len(a.get('positions',{}))))
print('git:      cash=%s trades=%d positions=%d' % (b.get('cash'),len(b.get('trades',[])),len(b.get('positions',{}))))
"
```

**A4. 对账判据（必须通过，5 分钟）**
```powershell
py -3.13 -c "
import json
d=json.load(open('tmp/restore_test/data/account.json',encoding='utf-8'))
cash=d.get('cash',0)
positions=d.get('positions',{})
trades=d.get('trades',[])
# 判据1：现金 >= 0
print('cash>=0:', cash>=0, 'cash=%.2f'%cash)
# 判据2：每笔交易 qty>0, price>0
bad=[t for t in trades if t.get('qty',0)<=0 or t.get('price',0)<=0]
print('trades valid:', len(bad)==0, 'bad=%d'%len(bad))
# 判据3：持仓 qty>0
bad_pos=[k for k,v in positions.items() if v.get('qty',0)<=0]
print('positions valid:', len(bad_pos)==0, 'bad=%d'%len(bad_pos))
# 判据4：与审计链当日事件核对（最后一笔交易时间应在审计链中有对应事件）
if trades:
    print('last trade:', trades[-1].get('time'), trades[-1].get('code'), trades[-1].get('side'))
"
```
**通过标准**：cash>=0、所有 trades qty/price>0、所有 positions qty>0、最后一笔交易时间与审计链 `data/audit/audit.jsonl` 当日事件可对应。

**A5. 人工批准后覆盖生产（需人类确认）**
```powershell
# 备份当前损坏件
Copy-Item 'data\account.json' 'data\account.json.corrupted_20260905'
# 覆盖
Copy-Item 'tmp\restore_test\data\account.json' 'data\account.json' -Force
# 验证
py -3.13 -c "import json; d=json.load(open('data/account.json',encoding='utf-8')); print('RESTORED OK: cash=%.2f trades=%d positions=%d'%(d['cash'],len(d['trades']),len(d['positions'])))"
```

**耗时**：A1-A4 约 11 分钟（不含人工批准等待）。
**需人类批准**：A5 覆盖生产路径。

---

## 场景 B：market.db 日K段损坏 / 快照全丢

**症状**：服务启动报 `database disk image is malformed` / 查询 kline 报错 / 回测结果异常 / `data/snapshots/` 目录为空。

### 步骤

**B1. 确认损坏范围（只读，3 分钟）**
```powershell
# 检查 market.db 完整性
py -3.13 -c "
import sqlite3
c=sqlite3.connect('file:data/market.db?mode=ro&immutable=1',uri=True,timeout=180)
try:
    r=c.execute('PRAGMA integrity_check').fetchone()
    print('integrity:', r[0])
    print('kline rows:', c.execute('SELECT COUNT(*) FROM kline').fetchone()[0])
    print('max date:', c.execute('SELECT MAX(date) FROM kline').fetchone()[0])
except Exception as e:
    print('ERROR:', e)
c.close()
"
# 检查快照
Get-ChildItem 'data\snapshots' -Directory | Select-Object Name
```

**B2. 最小可跑通路径：空库 + stale_first 逐日回补（需人类批准启动）**
```powershell
# 备份损坏库（若还能读）
Copy-Item 'data\market.db' 'data\market.db.corrupted_20260905' -ErrorAction SilentlyContinue

# 建空库（只建表结构，不写数据）
py -3.13 -c "
import sqlite3
c=sqlite3.connect('data/market.db')
c.execute('''CREATE TABLE IF NOT EXISTS kline(
    code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
    volume INTEGER, amount REAL, period TEXT,
    PRIMARY KEY(code,date,period))''')
c.commit(); c.close()
print('empty market.db created')
"

# 触发 stale_first 回补（通过 updater 路径，禁手工 INSERT）
# 方法1：重启服务（updater 启动时自动检测 stale 并回补）
# 方法2：手动调用（若服务在线）
py -3.13 -c "
from app import updater
updater.update_daily(stale_first=True)
"
```

**B3. 回补耗时估算（基于 E1 实测吞吐）**

| 阶段 | 吞吐（E1 实测） | 数据量 | 预估耗时 |
|---|---|---|---|
| stale_first 当日覆盖 | ~300-858 票/480s 预算 | 2526 票 | 约 3-5 个收盘周期（每日 480s 预算） |
| 历史段回补（2019-至今） | backfill_daily 实测 51min/6线程（全量补数） | ~884 万行 | 约 1-2 小时（全速）/ 约 1 周（按每日 480s 预算） |
| min5.db 重建 | 未实测（546 万行） | 546 万行 | 约 2-4 小时（估） |

**结论**：若走 stale_first 每日预算，**回到今天可用水平（当日覆盖≥0.9）约需 3-5 个交易日**；全量历史回补约 1 周（验收方结论"重建成本以周计"）。

**B4. 期间影响（让使用者心里有数）**
- 扫描会吃多旧的 bar：回补初期 kline 只有最近几日数据，板块强度/选股结果严重失真
- 板块红条会亮：`daily_coverage` 低于 0.8 → CRITICAL，前端板块红条持续亮
- 回测不可用：钉快照回测无快照可用（快照全丢），需等快照重新生成（收盘后 auto_snapshot）
- ML 预测不可用：ml_pred 表丢失，需等 rolling_train 重新训练（无自动触发，需人工）

**B5. 验证回补进度**
```powershell
# 每日检查覆盖
py -3.13 -c "
import sqlite3
c=sqlite3.connect('file:data/market.db?mode=ro&immutable=1',uri=True,timeout=180)
latest=c.execute('SELECT MAX(date) FROM kline').fetchone()[0]
n=c.execute('SELECT COUNT(DISTINCT code) FROM kline WHERE date=?',(latest,)).fetchone()[0]
print('latest=%s covered_codes=%d/2526 ratio=%.3f'%(latest,n,n/2526))
c.close()
"
```

**耗时**：B1-B2 约 10 分钟（不含回补等待）；回补本身 3-5 交易日（当日覆盖）/ 1 周（全量）。
**需人类批准**：B2 建空库覆盖生产 + 启动回补。

---

## 场景 C：整盘失效（盘坏 / 掉电 / 系统无法启动）

**结论：当前无解，需外接介质。**

同盘 keypack 在盘坏时与源同灭，无法恢复。git nightly 若推送到远程（当前未配置 remote）可恢复代码，但数据全灭。

### 外接介质落地后的第一步（引用 K1 §2 现成命令）

人类提供第二介质（外接 USB/NVMe 盘或 NAS 映射盘，挂载为 D:/E: 或网络路径）后：

```powershell
# 首次异盘备份（SQLite 在线备份 API，禁止 copy 正在写的 .db）
py -3.13 tools\backup_data.py --dest D:\tianji_backup
# 产物：<D:\tianji_backup>\<时间戳>\market.db + 全部 JSON + SHA256SUMS.txt + manifest.json
```

`tools/backup_data.py` 已内置 sqlite `backup` API（非文件 copy，规避撕裂副本）；`--big` 同时备份 min5.db（~725MB，较慢）。首建备份目录路径需人类确认。

### 恢复演练（介质就绪后必须做一次）

RESTORE 到 `tmp/pack20/restore_test/`，只读比对：
- `kline` 行数 / `COUNT(DISTINCT code)` / 最近 5 个交易日逐日期覆盖数
- 随机 20 票×5 日收盘价
全一致才通过；**禁把副本写回任何生产路径**。

### 外接方案建议（供人类决策）
- 最小：1TB 外接 USB SSD（约 ¥400-600），每周手动备份
- 推荐：2TB NVMe 移动硬盘 + 每日自动备份（注册 `TianjiDataBackup_Weekly`，每周日 10:00）
- 进阶：NAS 映射盘 + 异地备份（防火灾/盗窃）

**耗时**：场景 C 当前不可执行；介质就绪后首次备份约 30 分钟（market.db 1.4GB + JSON），恢复演练约 1 小时。

---

## 通用：keypack 还原命令速查

```powershell
# 列出所有归档
Get-ChildItem 'data\keypack\*.tar.gz' | Sort-Object Name | Select-Object Name,@{N='MB';E={[math]::Round($_.Length/1MB,2)}},LastWriteTime

# 校验归档
py -3.13 tools\backup_key_pack.py --verify 'data\keypack\keypack_YYYYMMDD_HHMMSS.tar.gz'

# 解包到临时目录（禁直接解到生产路径）
New-Item -ItemType Directory -Force 'tmp\restore_test' | Out-Null
py -3.13 -c "import tarfile; tarfile.open(r'归档路径','r:gz').extractall('tmp/restore_test', filter='data')"

# 查看 manifest（成员清单 + SHA256 + account 摘要）
py -3.13 -c "
import tarfile,json
t=tarfile.open(r'归档路径','r:gz')
m=json.load(t.extractfile('manifest.json'))
print('generated_at:',m['generated_at'])
print('members:',m['member_count'],'total_bytes:',m['total_bytes'])
for x in m['members']:
    if x['path']=='data/account.json':
        print('account:',x.get('account_summary'))
"
```
