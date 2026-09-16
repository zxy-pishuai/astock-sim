# 单实例锁缺陷修复提案（Y2）—— lock_fix_proposal

- 日期：2026-09-01
- 执行者：Y2（并发包）
- 上游输入：X1 处置报告 `docs/reports/dual_instance_fix_20260901.md` §1.5（6 缺陷，行号级）
- 交付形态：补丁草案 + 可复现单测 + 本报告 + 批准门后执行 SOP（**零生产改动**）
- 关联：X2 `docs/reports/sizing_wiring_proposal.md` §5 三段灰度（影子 1 周→单槽试用→全量）；本报告 §3 角色维度即为其灰度第一段提供锁机制支撑，两报告互相引用。

---

## §0 预注册 / 定性声明（先落盘后动手）

1. **取向：保守**。宁可"检测到可疑双主 → 拒绝启动 + 大声告警"，也不做复杂锁仲裁。本项目刚因"聪明逻辑"（watchdog 误判恢复）出过事，简单规则优先。
2. **生产零改动**：`main.py` / `tools/service_watchdog.py` / `app/*` 一行未动（mtime 证据见 §4）。本报告只给补丁 + 测试 + SOP。
3. **测试不起真服务第二实例**：单测为 `main.py` 锁逻辑的**独立拷贝**（`tmp/y2/unit_lock_test.py`），假锁文件 + 假端口状态驱动；用例 4 用真实 OS 锁（msvcrt）验证持句柄。避免重演 X1 双实例事故。
4. **语义自证**：修复前后行为并排展示（OLD 复现缺陷 / NEW 验证修复），测试输出即证据，不做"声称完成"。
5. **批准门**：生产应用（改 `main.py`）需人类批准；重启窗今晚 00:20–06:00。

---

## §1 背景：X1 定位的 6 缺陷 + 本批新增发现的第 7 缺陷

X1 双实例事故终态：**44104**（`main.py --no-window`，00:48 起，唯一持 8899）+ **53208**（桌面双击 `A股模拟盘.lnk`，09:39:34，持错位 app.lock）。已恢复三方一致（8899/lock/唯一 main.py 均=44104）。复发风险评级"中"：**任何人再双击桌面快捷方式即可复现**。

X1 §1.5 六缺陷（代码锚点以 `main.py` 实测为准，L66-114 锁段、L145-197 主流程）：

| # | 缺陷 | 现状代码引用 | 风险场景 |
|---|---|---|---|
| ① | 弱锁非持有型 | `_acquire_lock` L110-111 `open(_LOCK_FILE,"w")` 写后立即关闭 | 运行中不持句柄 → 任何后续进程可 `open("w")` 覆盖锁 |
| ② | alive 只验 PID 存活不验身份 | L84-95 `OpenProcess` 探测 | 存活但不一定是 main.py / 不持端口 → 误判 |
| ③ | healthy 裸 TCP connect | L97-101 `socket.create_connection` | 不验证归属者；且 alive=False 时（L104-109）直接删锁放行，不做端口二次核验 |
| ④ | "TCP 活+lock 异主"天然检测不到 | L102 只判 alive and healthy | 本案终态：8899=44104 但 lock=53208，无法识别端口主人≠锁主人 |
| ⑤ | 杀锁持有者后无自愈 | 锁只在启动时写一次 | 44104 运行中不重写锁 → stale 锁无人清理 |
| ⑥ | 检查顺序错位 | L171 `find_free_port` → L175 `_acquire_lock` 写锁 → L188 `_is_running` 才发现端口活 | "新实例持锁、旧实例持端口"错位，本案成因 |

**第 7 缺陷（本批新发现，X1 未列）**：`app/server.py` `find_free_port` L1183-1193 会从 prefer 起 +29 找可 bind 端口自动换端口。主流程 L171 拿 `find_free_port(args.port)` 的结果作实际端口，但 L175 `_acquire_lock(args.port)` 用的是**原始 args.port**——锁声明端口与实际服务端口解绑；若 8899 被任何进程占用，新实例可能起在 8900+ 却写同一个 app.lock，形成"锁/端口错配"。修复取向：**主实例固定端口，8899 被占即拒绝 + 告警**，不自动换端口。

---

## §2 修复设计（逐缺陷）

### 2.1 锁内容升级：纯 PID 文本 → 四元组+角色（修复 ①②④）

锁文件内容从 `"<pid>"` 升级为 `"<pid>|<port>|<start>|<exe_path>|<role>"`（管道分隔，人类可读）：

```
44104|8899|2026-09-01 00:48:12|C:\...\main.py|main
```

- **为什么够**：身份核验所需的全部信息（PID/端口/启动时刻/可执行路径/角色）一次性落盘，任何一方读到锁即可交叉验证归属，无需额外状态文件。
- **兼容**：`_lock_meta_read` 对旧格式（纯 PID）降级解析（pid 有效、其余字段空），不破坏既有 `data/app.lock`。

### 2.2 持锁：OS 级独占句柄（修复 ①⑤，stale 根治）

`_lock_exclusive(path)`：`os.open(O_RDWR|O_CREAT)` 打开后不关闭，Windows 走 `msvcrt.locking(fd, LK_NBLCK, 1)` 锁文件首字节、Linux 走 `fcntl.flock(fd, LOCK_EX|LOCK_NB)`，返回 fd 存入全局 `_LOCK_G["fd"]`，进程存活期间保持打开。**进程死 → fd 关闭 → OS 锁自动释放 → 新实例可接管**，从机制上根治 stale 锁，无需任何"清理/自愈"代码。

- **为什么够**：这是 OS 保证的互斥，不是"读 PID 判存活"这种猜测。第二进程 `_lock_exclusive` 直接 OSError → 拒绝，无法覆盖。
- **保守性**：不引入锁仲裁/心跳刷新等复杂逻辑；持句柄即锁。

### 2.3 顺序重排：先端口探测，再拿锁（修复 ⑥）

新主流程：

```
main() → port = args.port（固定，不再 find_free_port 自动换端口）
       → _acquire_lock(port)
          ① _port_owner(port)   ← netstat 解析 LISTENING 该端口且 cmdline 含 main.py 的 PID（身份核验，修复②）
          ② _is_running(port)   ← HTTP GET /api/overview 200（健康核验，替代裸 TCP，修复③）
          ③ 分支判定（见 2.4）
          ④ 端口空闲 → _lock_exclusive 持句柄 → _lock_meta_write 四元组 → "ok"
```

- **为什么够**：先探测端口归属再写锁，"新实例先写锁、后见端口活"的错位路径被彻底截断。`_is_running`（HTTP 200）比旧 `socket.create_connection`（裸 TCP）更权威，且是 main.py 现成函数，零新增依赖。

### 2.4 "TCP 活 + lock 异主"检测（修复 ④）

`_acquire_lock` 分支判定：

| 现场 | 判定 | 动作 |
|---|---|---|
| 端口有真主 + overview 健康 + 锁同主 | `already` | 仅开窗口，不写锁 |
| 端口有真主 + overview 健康 + **锁异主** | `already` + **自愈** | 锁元数据重写为真主 PID（真主在跑，更新归属安全），告警"锁异主自愈" |
| 端口有真主 + overview 不健康 | `deny` | 半死僵尸 → 拒绝启动 + 告警，**不自动杀**（保守） |
| 端口被非 main.py 进程占用 | `deny` | 拒绝启动 + 告警（不自动换端口，避开第 7 缺陷） |
| 端口空闲 + 锁被其他实例持有（OS 锁） | `deny` | 拒绝启动 |
| 端口空闲 + 拿到 OS 锁 | `ok` | 写四元组，启动服务 |
| 任何异常 | `deny` | 拒绝启动 + 告警（旧版 `except: return True` 异常放行是隐患，一并修复） |

### 2.5 watchdog 恢复动作前置读锁归属（不改 watchdog 代码，仅建议）

`tools/service_watchdog.py` `recover()`（L271-306）恢复前应增加一步只读比对：`read_lock_pid()` vs `pid_on_port(PORT)`。**锁 PID ≠ 端口 PID** → 记"锁异主"告警、**不动作**、升级人类核验（正是 09-01 事故终态形态，watchdog 若恢复会误杀/误拉）。同主健康 → 维持 `skip`（防双实例，现状逻辑正确）。此建议写入本报告与 SOP，**不改 watchdog 脚本**（G0 产物，隔离面外）。

### 2.6 角色维度：`--role=shadow`（与 X2 §5 灰度互相引用）

X2 灰度第一段"影子 1 周"需要跑一个**不占 8899、不交易、只记账**的影子进程。修完的强锁若"全机仅一个 main.py"会把它一起锁死。设计：

- `main.py --role=shadow [--role-key=<键>]`：影子角色。
  - **允许多影子**：每个影子持独立锁文件 `data/app.lock.shadow.<role_key>`（`_lock_exclusive` 独占持句柄），不同 role_key 可并行。
  - **仍禁双主**：主锁 `data/app.lock` 唯一，影子不碰主锁。
  - **须主实例在跑**：`_acquire_lock_shadow` 先 `_is_running(port)`，无主实例 → 拒绝（"影子不可独立运行"）。
  - **不写 account/不发单**：由 X2 影子记账工具（`tools/shadow_board.py` 等）保证业务侧不落单；本 patch 只做角色锁隔离。
- **隔离标记**：锁文件分离（`app.lock` vs `app.lock.shadow.<key>`）+ 锁内容 role 字段，双重标记。
- **互引**：X2 报告 §5 灰度第一段的影子进程建议以 `main.py --role=shadow --role-key=sizing` 方式启动；本报告 §3 补充其锁机制约束。

---

## §3 补丁清单（tmp/y2/lock_fix.patch）

- 来源：`main.py`（原，CRLF，269 行）→ `tmp/y2/main.py.fixed`（LF 副本，398 行，+129 行）。
- **patch 为 difflib unified diff**（`a/main.py` / `b/main.py`），**新增 192 行 / 删除 63 行**，改动范围仅锁段 + main() 主流程 + CLI 参数，未触碰 run_browser/run_desktop/_is_running/_wait_ready 与其余逻辑。
- **patch 可应用性已验证**：3 个 hunk（@-62/+173、@-148/+9、@-168/+42）按标准逆序算法应用到原 `main.py` 后**逐位等于** `main.py.fixed`（398 行一致）。
- 行尾说明：生产 `main.py` 为 CRLF；`patch` 与 `fixed` 均为 LF。应用 patch 后统一为 LF（项目约定，步骤 3 校验 CRLF=0）。

改动明细：

| 区块 | 内容 |
|---|---|
| 锁段 L65-114 | 整体替换：新增 `_lock_exclusive` / `_pid_alive` / `_pid_cmdline_has` / `_port_pids` / `_port_owner` / `_lock_meta_read` / `_lock_meta_write` / `_acquire_lock_shadow`；重写 `_acquire_lock`（返回协议 ok/already/deny） |
| CLI L147-151 | 新增 `--role`（main/shadow）、`--role-key` |
| 主流程 L171-197 | 固定端口（弃 find_free_port）+ 先 `_acquire_lock` 再按 action 分支 + shadow 分支 |

---

## §4 单测与验证（可复现）

运行：`C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe tmp/y2/unit_lock_test.py`（输出已存 `tmp/y2/unit_test_output.txt`）。**15/15 全部通过**。

| 用例 | OLD（复现缺陷） | NEW（验证修复） |
|---|---|---|
| C1 正常启动（无锁+端口空闲） | 放行并写锁 ✓ | action=ok 放行 ✓ |
| C2 双启拒绝 | 正确拒绝（PID 44104）✓ | already ✓ |
| C2 锁异主 | **指向错误 PID 53208**（缺陷④复现）| 识别归属 44104 + "锁异主自愈" ✓ |
| C3 stale（锁 PID 死） | 放行但**可被 open("w") 覆盖**（缺陷①复现）| 强锁持句柄，第二持有被 OS 拒绝 ✓ |
| C4 真实 OS 锁持句柄 | — | fdA 持有、fdB 被拒；fd 关闭后重新可持有（stale 自愈）✓ |
| C5 顺序错位回归（端口已有真主+无锁） | 先写锁、留下错位锁（缺陷⑥复现）| 先探测端口 → already，不写锁 ✓ |
| C6 watchdog 误拉场景 | — | 锁异主→alert 不重启；同主健康→skip；端口死→restart ✓ |

**mtime / 行数证据**：

| 文件 | mtime | 说明 |
|---|---|---|
| `main.py` | 2026-08-26 10:29:40 | **生产文件未动** ✓ |
| `tools/service_watchdog.py` | 2026-08-31 23:48:25 | 未动 ✓ |
| `data/app.lock` | 2026-09-01 10:35:15（5B）| X1 手动归位内容，Y2 未动 ✓（红线"不动 data/"）|
| `tmp/y2/main.py.fixed` | 2026-09-01 11:13:44（398 行）| 修复副本，py_compile OK，CRLF=0 |
| `tmp/y2/lock_fix.patch` | 2026-09-01 11:14:01 | +192/-63，CRLF=0 |
| `tmp/y2/unit_lock_test.py` | 2026-09-01 11:15:19 | py_compile OK，CRLF=0 |

---

## §5 批准门后执行 SOP（生产应用，待人类批准）

### 5.1 批准门
本 SOP 所述改动**必须**经批准后执行；重启窗今晚 **00:20–06:00**（无交易无更新任务，最安全）。09:15–15:00 禁重启（红线）。

### 5.2 执行步骤

1. **备份**：`Copy-Item main.py tmp/y2/backup_main_<ts>.py`（保留原 CRLF 版本）。
2. **打补丁**：
   - 方式 A（推荐，可审阅）：读 `tmp/y2/main.py.fixed` 全文 → 人工对照 `tmp/y2/lock_fix.patch` 逐块替换进 `main.py`，保持 LF 行尾。
   - 方式 B：若环境有 `patch`：`patch -p1 < tmp/y2/lock_fix.patch`（注意先统一行尾为 LF）。
3. **语法校验**：`python -m py_compile main.py` 必须通过；确认 CRLF=0；记录改后行数（应为 398）与 mtime。
4. **00:20 重启**：停 8899 服务 → 启动 `main.py --no-window` → 等就绪 → 跑 `startup_check.py`。
5. **双实例自杀式验证**（关键）：重启后**故意再双击桌面 `A股模拟盘.lnk`**，预期：
   - 新进程被拒（日志出现"已有实例运行中(PID 44104)"或"锁被其他实例持有"），**不开第二窗口、不覆盖锁**；
   - 杀 44104 后立即再双击 → 新实例应**自动接管**（OS 锁随进程死释放，无 stale）。
6. **角色验证（X2 联动，可选）**：`main.py --role=shadow --role-key=sizing` 应能启动影子且不与主实例冲突；重复同 role_key 应被拒；无主实例时影子应拒绝。
7. **回滚路径**：停服务 → `Copy-Item tmp/y2/backup_main_<ts>.py main.py -Force` → 重启 → 复跑 startup_check.py。lock 文件残留由新锁机制自动处理（OS 锁随进程死释放；旧纯 PID 锁格式由 `_lock_meta_read` 降级兼容）。

### 5.3 常识条款（db 类文件不入 git）
本仓库 db 类文件（`data/snapshots/`、`data/market.db*`、`data/min5.db*`、`data/backups/`）已由 `.gitignore` 排除，`git ls-files` 实测 0 个 `.db`；**git 回滚永不触碰数据库**。

### 5.4 真实近亲警告（SOP 必须含）
`git reset --hard` / `git restore .` 在**服务运行中**会把**被跟踪**的 `data/account.json`、`data/audit/audit.jsonl` 回滚到最近提交（X4 10:35 / nightly 粒度），丢失其后账户状态与审计行——账户有 `account_history` 可救、**审计链丢尾不可再生**。
**禁令：本仓库永远禁止全量 hard-restore；需要回看基线用 `git show <rev>:<path>` 只读。**

---

## §6 附带给 X5（今晚观察块）的衔接说明

### 6.1 修复前今日（09-01）残余风险清单

| # | 风险 | 现状 | 观察要点 |
|---|---|---|---|
| R1 | 任何人双击桌面快捷方式 → 第二实例 | 高（中评级的直接来源）| 观察是否有新 main.py 进程出现、app.lock 是否被覆盖 |
| R2 | watchdog 恢复路径在"锁异主"时可能误拉 | 中（recover 只读 lock pid，不比对端口 pid）| 若 watch 到 restart 动作，立即核对端口 PID |
| R3 | `find_free_port` 自动换端口 → 锁/端口错配 | 中（第 7 缺陷）| 8899 若被占，观察是否有进程起在 8900+ |
| R4 | 服务运行中 `git restore .` 回滚 account/audit | 中（被跟踪文件）| 观察 git 操作痕迹；禁止全量 restore |
| R5 | 14:55 前哨检查（X1 已排）| — | Y2 不干扰，X5 可核对其结果 |

### 6.2 X5 观察建议
- 修复前：每 30 分钟核对 8899 归属 PID == app.lock PID == 唯一 main.py PID（三方一致才健康）。
- 若出现任何一方不一致：记"锁异主"事件，**不要自动恢复**，升级人工（保守原则）。

---

## §7 假警报教训（本批自采，任务书附带已证伪）

曾怀疑"X4 把 4.15GB 快照 market.db 纳入 git 跟踪 → 误 `git reset --hard`/`git restore .` 会毁当前工作库"。实测证伪：`git ls-files | grep '\.db$'` = 0 个、`.gitignore` 明载 `data/snapshots/`、`data/market.db*`、`data/min5.db*`、`data/backups/` 全排除、size-pack 仅 4.8MB。**该风险不存在**，`.gitattributes` 的 `*.db -text` 只是双保险声明（规则先于跟踪存在）。

**教训自采**：给下游下隐患判断前，先跑那条 10 秒的验证命令——本项目第三次因"看起来显然"的假设差点制造恐慌。

---

## §8 遗留风险 / 未做项（诚实披露）

1. **生产未应用**：本批只交付补丁+测试+报告；`main.py` 真改需批准门 + 00:20 重启，未执行。
2. **watchdog 未改**：`recover()` 的"锁异主→告警不动作"前置比对只是建议（写入 §2.5），watchdog 属 G0 产物隔离面，改它需另立任务。
3. **单测覆盖**：C2/C3/C5 的 OLD 复现基于逻辑模拟（注入探测器），C4 是真实 OS 锁验证；未做"真双进程"集成测试（红线禁止，避免重演事故）——真实场景依赖 §5.2 步骤 5 自杀式验证补足。
4. **`_port_owner` 依赖 `wmic`**：Windows 11 新装机 wmic 可能弃用；若 wmic 不可用，`_pid_cmdline_has` 返回 False → 本服务进程识别会退化（降级为"any_port→deny"保守拒绝，不会误放行）。建议批准执行时现场验证 wmic 可用性。
5. **影子记账业务侧**：`--role=shadow` 只保证锁隔离；实际记账逻辑（`tools/shadow_board.run_shadow`）由 X2 提供，本 patch 中为占位 ImportError 捕获，不阻塞主流程。
6. **并发协调**：与 G1（act12 大回测）无资源冲突（本批仅小文件+单测）；未触发 act12 错峰让步。

---

## §9 结论

- 六缺陷（+第 7 缺陷 find_free_port 错配）均有对应修复设计，取向保守（拒绝+告警优先于自动仲裁）。
- 补丁 `tmp/y2/lock_fix.patch`（+192/-63）已生成，`main.py` 生产文件零改动；单测 15/15 通过。
- 生产应用待批准门；批准后按 §5 SOP 在 00:20 窗口执行，自杀式验证是最后一道真实场景门。
- **只出建议，零落地**：本批未注册任务、未改 config、未动任何生产文件。
