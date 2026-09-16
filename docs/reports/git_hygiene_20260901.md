# X4｜git 卫生收尾：EOL 定调 + 假 diff 归零 + nightly 盯梢

> 状态：**完成**。提交 `4eec67e`（77 files, 48,288 insertions, 6 deletions）。
> 假 diff 归零：提交前 35 条脏 → 提交后 2 条（均属并行块提交后活动，非本块产物，留给 nightly）。
> 审计链自检：**链在 2026-08-16 历史断链（项目已知基线，340 条）**，铁证非 git 操作引入。

## §1 雷区排查：审计链哈希对行尾是否敏感？

**结论：不敏感 → 走路线 A（`text eol=lf` + renormalize）。**

代码原文（`app/audit.py`）：

写入端 `record()`（L140-148）：
```python
# 哈希链：prev_hash 参与本次哈希计算
payload = json.dumps(rec, ensure_ascii=False, sort_keys=True)
rec["hash"] = hashlib.sha256(
    (payload + "|" + prev).encode("utf-8")).hexdigest()[:16]
rec["prev"] = prev
line = json.dumps(rec, ensure_ascii=False)
...
f.write(line + "\n")       # 换行仅作行分隔，不参与哈希
```

校验端 `verify_chain()`（L184-208）：
```python
for line in f:
    line = line.strip()    # 先 strip 掉 \r\n 与 \n
    ...
    rec = json.loads(line)
    ...
    payload = {k: v for k, v in rec.items() if k not in ("hash", "prev")}
    expect = hashlib.sha256(
        (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "|" + prev)
        .encode("utf-8")).hexdigest()[:16]
```

**论证**：
1. 哈希输入 = `json.dumps(payload) + "|" + prev` 的 UTF-8 字节，**是 JSON 序列化字符串，不含任何换行符**；
2. 校验端先 `line.strip()` 再去 json，CRLF 与 LF 对 `json.loads` 无差别；
3. 换行符只作为**行分隔符**存在（`f.write(line + "\n")`），从不进入哈希计算。

**台账 jsonl 同机制确认**：`data/ml_scores_ledger.jsonl`、`data/forward_eval.jsonl`、`data/quality_alert.jsonl` 同为行分隔 JSONL，写入/校验路径一致（无独立哈希实现差异），行尾规范化同样无影响。

## §2 路线选择理由

- **路线 A（本块采用）**：`.gitattributes` 声明 `*.jsonl *.json *.md *.py … text eol=lf`，`git add --renormalize .` 把 index blob 统一规范化为 LF，工作区 CRLF 文件**按字节保持**（renormalize 只动 index，不 rewrite 工作区），git 比较"clean 后内容"→ 假 diff 消失。
- 路线 B（未采用）本可保字节，但哈希不敏感已排除其必要性；A 同时根治未来所有文本文件的 EOL 一致性。

## §3 前后 status 对比（35 → 2）

| 阶段 | porcelain 行数 | 说明 |
|---|---|---|
| 提交前 | 35 | 假 diff（account.json/audit.jsonl 整文件）+ 真改动（server/web/operations/shadow）+ pack6/7 交付物 + tmp 垃圾 |
| 提交后 | 2 | `M tools/sizing_replay.py`、`?? tmp/x3/probe5.py`——**均为提交后并行块（X2/X3）新产生**（sizing_replay 本次已入库被续改；probe5 为 X3 出网新脚本）。按红线"唯一一次提交"不再追 commit，留给 nightly |

**假 diff 归零证据**（staged diff 行数变化）：
- `data/audit/audit.jsonl`：提交前 diff 5950 行（整文件行尾）→ 提交后仅 57 行（真实运行时新增审计行）
- `data/account.json`：提交前 336 行 → 提交后 24 行（真实账户状态更新）
- 提交收编的 audit.jsonl 完整到最新（index 3004 行 = 工作区 3004 行，末行完整 JSON `2026-09-01 10:32:00 heartbeat`，无并发半行）

## §4 唯一提交

```
4eec67e  hygiene: eol policy + pack6/pack7 deliverables
          77 files changed, 48288 insertions(+), 6 deletions(-)
9aeb556  baseline 2026-09-01: post-incident full tree (code+docs+small data)
```

- 入库内容：`.gitattributes`（新建）+ pack6/7 交付物（docs/reports/、tools/、web/js/、data/bt_*.json 等）+ tmp/pack*/（W1 `.gitignore` 白名单 `!tmp/pack*/` 有意入库）+ tmp/w*/、tmp/x*（git add -A 契约）+ 真改动（server.py/web/operations.md/shadow/account/audit 新行）
- **未入库**：任何 .db/.png/.ico/.zst/.bak（`git diff --cached --name-only` 二进制过滤为空）；`.gitignore` 未被触碰（W1 资产）
- `tmp_cz.py`：**已不存在于项目树**（root 与递归均未找到）→ 删除动作 N/A，无残留

## §5 ls-files --eol 复检表

```
i/lf    w/lf    attr/                  .gitattributes
i/lf    w/crlf  attr/text eol=lf       data/account.json      ← 假 diff 源，已收编（status clean）
i/lf    w/crlf  attr/text eol=lf       data/audit/audit.jsonl ← 假 diff 源，已收编（status clean）
i/lf    w/lf    attr/text eol=lf       data/ml_scores_ledger.jsonl / forward_eval.jsonl
i/lf    w/lf    attr/text eol=lf       docs/operations.md / app/audit.py
```

全库汇总：531 文件 `i/lf w/lf`（文本，含新增）、109 文件 `i/lf w/crlf`（**全部带 `attr/text eol=lf`**，已被 .gitattributes 收编、不再报脏）、76 个无扩展名/默认、16 个 mixed、3 个 `-text`（二进制）。

## §6 审计链自检（提交后）

**结论：链断裂点 = 2026-08-16 19:07:19（manual_buy），项目已知历史基线；git 操作零责任。**

- 双路校验（运行时 `app.audit.verify_chain()` + 独立 hashlib 逐行重算）均在 `2026-08-16 19:07:19` 断（checked=8）；
- **铁证**：`git show HEAD:data/audit/audit.jsonl`（本次提交收编的版本）同样断在 8/16 同一位置（checked=8）→ 断链在 git 卫生**之前**已存在于工作区，git 操作（renormalize/add/commit 只动 index/objects，从不改写工作区字节）无引入任何新断裂；
- **已知基线确认**：`data/audit_chain_breaks.json` 存档 `breaks_total=340`，首条即 `line 9, 2026-08-16 19:07:19, manual_buy, prev_mismatch+hash_mismatch`——与本次校验断点完全一致；
- 断链根因（档案性质）：8/16 多条 `manual_buy` 测试单（price 10.0/20.0 交替）写入时链首异常重置（`prev=""` 出现在非当日首条），历史测试/并发写遗留；
- ⚠ 因此任务书预设的"2978 行链 OK"**在既有基线口径下不成立**（文件当前 3004 行，含 8/16 前即存在的 340 条断链档案）；本块如实报告：**git 操作前后链状态一致（无新增断裂）**，不断链修补、不伪造 OK。链修复属既有红线外事项，另立任务。

## §7 nightly 首次盯梢与明日验收命令

- 首次触发：今晚 2026-09-01 23:50（`TianjiGit_Nightly` → `tools/git_nightly.ps1`）；
- 本块已把验收命令写进 `docs/operations.md` §⑥（新增小节），明日（9/2）上午执行：
  - `git log --since=yesterday --oneline` → 应见 `auto 2026-09-01 23:50` 提交；
  - 未见 auto 提交 → `schtasks /query /tn TianjiGit_Nightly /fo LIST /v` + 日志 `tmp/git_nightly.log`（git_nightly.ps1 每次把末条提交追加到该文件）；
  - 行为基准：`git add -A` + `git commit -m "auto <时间>"`，无变更吞非零退出不算失败。

## §8 echarts.min.js（1MB）处置建议

- 现状：`web/vendor/echarts.min.js`（1,030,855 B）**已在库**（基线 9aeb556 入库，非本次），未被 .gitignore 排除；
- 建议：**保持现状（keep）**——1MB 单文件在无 remote 的纯本地库可接受，且它是 web UI 运行时依赖，移出会导致 checkout 后 UI 缺依赖；
- 备选（下次批次再做，**本次不动**）：若后续要瘦库，在 `.gitignore` 加 `web/vendor/` 并用 `git rm --cached` 移出跟踪（红线禁止本次执行）。

## §9 合规记录（红线核查）

- 写入面仅 4 项：`.gitattributes`（新建）、`docs/operations.md`（追加 §⑤⑥）、唯一一次 commit（4eec67e）、`docs/reports/git_hygiene_20260901.md`（本报告）；
- 未 `git rm --cached`、未改 .gitignore（W1 资产）、未 push/remote、未 amend/rebase、未 tag；
- `market.db` 等数据文件字节零触碰（git 只动 index/objects，工作区 CRLF 文件未顺手转换）；
- 未碰进程/服务（X1 在处置双实例）、未出网（X3 在出网）——本块纯 git 操作；
- 哈希雷区结论贴代码原文为证（§1），非凭感觉选路线。

## 附录：写入面文件

| 文件 | 操作 |
|---|---|
| `.gitattributes` | 新建（EOL 政策：文本 eol=lf / 二进制 -text） |
| `docs/operations.md` | 追加 §⑤ EOL 政策 + §⑥ nightly 首次验收 |
| `git` 提交 `4eec67e` | 唯一一次 hygiene 提交（77 files） |
| `docs/reports/git_hygiene_20260901.md` | 本报告 |
