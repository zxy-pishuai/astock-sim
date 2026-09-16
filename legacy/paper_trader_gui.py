#!/usr/bin/env python3
"""
A股模拟盘交易系统 — 图形界面 v2.1
双击 paper_trader_gui.py 或在项目目录运行 python paper_trader_gui.py
"""
import json
import os
import sys
import threading
import time
import queue
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from datetime import datetime

# 确保能 import 同目录的 paper_trader
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import paper_trader as pt

# ============================================================
# 线程安全的日志重定向
# ============================================================
class GuiLogHandler:
    """把 pt.log() 和 stdout 重定向到 GUI Text 控件（线程安全）"""
    def __init__(self, text_widget, root):
        self.text = text_widget
        self.root = root
        self._original_log = None
        self._log_queue = queue.Queue()
        self._processing = False

    def _safe_insert(self, line):
        """在主线程中安全写入"""
        try:
            self.text.insert(tk.END, line)
            self.text.see(tk.END)
        except Exception:
            pass

    def _process_queue(self):
        """定时从队列取日志写入 Text（主线程安全）"""
        try:
            while True:
                line = self._log_queue.get_nowait()
                self._safe_insert(line)
        except queue.Empty:
            pass
        if self._processing:
            self.root.after(100, self._process_queue)

    def write(self, msg):
        """stdout 重定向（可能来自后台线程）"""
        if msg.strip():
            self._log_queue.put(msg)

    def flush(self):
        pass

    def attach(self):
        self._original_log = pt.log
        def gui_log(msg, level="INFO"):
            emoji = {"INFO": "  ", "OK": "[OK]", "WARN": "[!]", "ERROR": "[X]",
                     "BUY": "[买入]", "SELL": "[卖出]"}
            prefix = emoji.get(level, "  ")
            line = f"[{pt.fmt_time()}] {prefix} {msg}\n"
            self._log_queue.put(line)
        pt.log = gui_log
        self._old_stdout = sys.stdout
        sys.stdout = self
        # 启动队列处理（主线程定时取日志）
        self._processing = True
        self.root.after(100, self._process_queue)

    def detach(self):
        self._processing = False
        if self._original_log:
            pt.log = self._original_log
        if hasattr(self, '_old_stdout') and self._old_stdout:
            sys.stdout = self._old_stdout


# ============================================================
# 主窗口
# ============================================================
class PaperTraderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("A股模拟盘交易系统 v2.1")
        self.root.geometry("820x680")
        self.root.minsize(720, 520)

        style = ttk.Style()
        style.theme_use("clam")

        # 循环/自动交易控制
        self._loop_running = False
        self._auto_trade = tk.BooleanVar(value=pt.CONFIG.get("auto_trade", False))
        self._market_logged = False
        self._pos_refreshing = False

        self._build_ui()
        self._attach_log()

        # 启动时刷新，如果开启自动交易则自动开始循环
        self._refresh_status()
        self._start_position_refresh()
        if self._auto_trade.get():
            self.root.after(2000, self._auto_start)

    def _auto_start(self):
        """自动开始交易循环"""
        now = datetime.now()
        if pt.is_trading_time(now) or pt.is_auction_time(now):
            self._threaded_loop()
            tag = "竞价打板+交易" if pt.is_auction_time(now) else "交易时段"
            self._safe_log(f"[INFO] 自动交易模式已启动（{tag}）\n")
        else:
            self._safe_log(f"[INFO] 自动交易模式已开启，等待竞价/交易时段...\n")
            # 延迟到竞价/交易时段开始
            self.root.after(30000, self._check_auto_start)

    def _check_auto_start(self):
        """检查是否到竞价/交易时间，如果是则自动开始"""
        if not self._auto_trade.get() or self._loop_running:
            return
        now = datetime.now()
        if pt.is_trading_time(now) or pt.is_auction_time(now):
            self._threaded_loop()
            tag = "竞价打板" if pt.is_auction_time(now) else "交易时段"
            self._safe_log(f"[INFO] 进入{tag}，自动开始扫描\n")
        else:
            self.root.after(30000, self._check_auto_start)

    def _safe_log(self, line):
        """线程安全写日志"""
        self.log_area.insert(tk.END, line)
        self.log_area.see(tk.END)

    def _build_ui(self):
        # --- 顶部：账户概览 ---
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill=tk.X)

        self.cash_var = tk.StringVar(value="...")
        self.asset_var = tk.StringVar(value="...")
        self.pnl_var = tk.StringVar(value="...")
        self.pos_var = tk.StringVar(value="...")

        ttk.Label(top_frame, text="现金余额:", font=("", 9)).grid(row=0, column=0, sticky=tk.W, padx=(0,5))
        ttk.Label(top_frame, textvariable=self.cash_var, font=("", 10, "bold")).grid(row=0, column=1, sticky=tk.W, padx=(0,20))
        ttk.Label(top_frame, text="总资产:", font=("", 9)).grid(row=0, column=2, sticky=tk.W, padx=(0,5))
        ttk.Label(top_frame, textvariable=self.asset_var, font=("", 10, "bold")).grid(row=0, column=3, sticky=tk.W, padx=(0,20))
        ttk.Label(top_frame, text="累计盈亏:", font=("", 9)).grid(row=0, column=4, sticky=tk.W, padx=(0,5))
        ttk.Label(top_frame, textvariable=self.pnl_var, font=("", 10, "bold")).grid(row=0, column=5, sticky=tk.W, padx=(0,20))
        ttk.Label(top_frame, text="持仓:", font=("", 9)).grid(row=0, column=6, sticky=tk.W, padx=(0,5))
        ttk.Label(top_frame, textvariable=self.pos_var, font=("", 10, "bold")).grid(row=0, column=7, sticky=tk.W)

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10)

        # --- 中部：持仓表格 ---
        mid_frame = ttk.Frame(self.root, padding=10)
        mid_frame.pack(fill=tk.BOTH, expand=False)
        ttk.Label(mid_frame, text="当前持仓", font=("", 10, "bold")).pack(anchor=tk.W)
        columns = ("代码", "名称", "持仓(股)", "成本", "现价", "盈亏", "盈亏%", "最高", "天数")
        self.pos_tree = ttk.Treeview(mid_frame, columns=columns, show="headings", height=5)
        for col in columns:
            self.pos_tree.heading(col, text=col)
            w = 60 if col in ("代码", "名称") else 80
            self.pos_tree.column(col, width=w, anchor=tk.CENTER)
        self.pos_tree.pack(fill=tk.X, pady=5)

        # --- 竞价打板公示面板 ---
        auction_frame = ttk.Frame(self.root, padding=10)
        auction_frame.pack(fill=tk.BOTH, expand=False)
        ttk.Label(auction_frame, text="🎯 竞价打板候选 (9:25-9:30 公示·实盘参考)",
                  font=("", 10, "bold")).pack(anchor=tk.W)
        auction_cols = ("排名", "代码", "名称", "高开%", "量比", "评分", "连板", "板块")
        self.auction_tree = ttk.Treeview(auction_frame, columns=auction_cols,
                                         show="headings", height=6)
        for i, col in enumerate(auction_cols):
            self.auction_tree.heading(col, text=col)
            w = 50 if col in ("排名", "代码", "评分", "连板") else 80
            if col == "高开%": w = 55
            if col == "量比": w = 55
            self.auction_tree.column(col, width=w, anchor=tk.CENTER)
        self.auction_tree.pack(fill=tk.X, pady=5)
        self.auction_hint = ttk.Label(
            auction_frame,
            text="等待 9:25 竞价窗口...（开启「持续循环」或「自动交易」后自动扫描）",
            foreground="#888")
        self.auction_hint.pack(anchor=tk.W)

        # --- 按钮区 ---
        btn_frame = ttk.Frame(self.root, padding=10)
        btn_frame.pack(fill=tk.X)

        self.btn_refresh = ttk.Button(btn_frame, text="刷新持仓", command=self._refresh_status)
        self.btn_refresh.pack(side=tk.LEFT, padx=3)

        self.btn_scan = ttk.Button(btn_frame, text="扫描交易", command=self._threaded_scan)
        self.btn_scan.pack(side=tk.LEFT, padx=3)

        self.btn_signal = ttk.Button(btn_frame, text="信号扫描(不交易)", command=self._threaded_signal)
        self.btn_signal.pack(side=tk.LEFT, padx=3)

        self.btn_loop = ttk.Button(btn_frame, text="持续循环", command=self._threaded_loop)
        self.btn_loop.pack(side=tk.LEFT, padx=3)

        self.btn_stop = ttk.Button(btn_frame, text="停止循环", command=self._stop_loop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=3)

        self.btn_watchlist = ttk.Button(btn_frame, text="⭐自选股池", command=self._show_watchlist)
        self.btn_watchlist.pack(side=tk.LEFT, padx=3)

        self.btn_analyze = ttk.Button(btn_frame, text="🔍自选分析", command=self._threaded_analyze)
        self.btn_analyze.pack(side=tk.LEFT, padx=3)

        self.btn_history = ttk.Button(btn_frame, text="📜历史交割单", command=self._show_history)
        self.btn_history.pack(side=tk.LEFT, padx=3)

        self.btn_wechat = ttk.Button(btn_frame, text="🔔微信提醒", command=self._show_wechat_settings)
        self.btn_wechat.pack(side=tk.LEFT, padx=3)

        # 自动交易复选框
        self.auto_cb = ttk.Checkbutton(
            btn_frame, text="自动交易(交易时段自动运行)",
            variable=self._auto_trade, command=self._on_auto_toggle
        )
        self.auto_cb.pack(side=tk.LEFT, padx=10)

        self.btn_reset = ttk.Button(btn_frame, text="重置账户", command=self._reset_account)
        self.btn_reset.pack(side=tk.RIGHT, padx=3)

        self.progress = ttk.Progressbar(btn_frame, mode="indeterminate", length=100)

        # --- 日志区域 ---
        log_frame = ttk.Frame(self.root, padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(log_frame, text="交易日志", font=("", 10, "bold")).pack(anchor=tk.W)
        self.log_area = scrolledtext.ScrolledText(
            log_frame, height=14, font=("Consolas", 9), wrap=tk.WORD,
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white"
        )
        self.log_area.pack(fill=tk.BOTH, expand=True, pady=5)

        # 状态栏
        self.status_bar = ttk.Label(self.root, text="就绪", relief=tk.SUNKEN, anchor=tk.W, padding=3)
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _on_auto_toggle(self):
        """自动交易复选框切换"""
        pt.CONFIG["auto_trade"] = self._auto_trade.get()
        if self._auto_trade.get():
            self._safe_log(f"[INFO] 自动交易模式: 开启 (交易时段自动运行)\n")
            self._check_auto_start()
        else:
            self._safe_log(f"[INFO] 自动交易模式: 关闭\n")
            if self._loop_running:
                self._stop_loop()

    def _attach_log(self):
        self.log_handler = GuiLogHandler(self.log_area, self.root)
        self.log_handler.attach()

    # ================================================================
    # 行情刷新
    # ================================================================
    def _start_position_refresh(self):
        """启动持仓行情定时刷新（每10秒更新浮盈浮亏）"""
        self.root.after(10000, self._tick_position_refresh)

    def _tick_position_refresh(self):
        """定时刷新持仓实时浮盈浮亏"""
        if not self._pos_refreshing:
            self._refresh_positions_only()
        self.root.after(10000, self._tick_position_refresh)

    def _refresh_positions_only(self):
        """只拉持仓代码行情，更新浮盈浮亏（后台线程，轻量）"""
        state = pt.load_state()
        codes = list(state["positions"].keys())
        if not codes:
            return
        self._pos_refreshing = True
        def _fetch():
            try:
                quotes = pt.fetch_quotes(codes)
            except Exception:
                quotes = {}
            self.root.after(0, lambda: self._finish_positions_refresh(quotes))
        threading.Thread(target=_fetch, daemon=True).start()

    def _finish_positions_refresh(self, quotes):
        self._update_account_and_positions(quotes)
        self._pos_refreshing = False

    def _refresh_status(self):
        """刷新顶部概览和持仓表格"""
        self.status_bar.config(text="刷新行情中...")
        def _fetch():
            try:
                stocks = pt.fetch_all_stocks()
            except Exception:
                stocks = {}
            self.root.after(0, lambda: self._update_ui(stocks))
        threading.Thread(target=_fetch, daemon=True).start()

    def _update_account_and_positions(self, quotes):
        """用实时行情更新账户概览 + 持仓表格（轻量，只需持仓行情）"""
        state = pt.load_state()
        if "trades" not in state:
            state["trades"] = []

        for code, pos in state["positions"].items():
            cp = quotes.get(code, {}).get("price", 0) or pos["entry_price"]
            pos["current_price"] = cp
            pos["pnl_pct"] = (cp - pos["entry_price"]) / pos["entry_price"]
            pos["pnl_value"] = (cp - pos["entry_price"]) * pos["qty"]

        total_market = sum(
            p.get("current_price", p["entry_price"]) * p["qty"]
            for p in state["positions"].values()
        )
        total_asset = state["cash"] + total_market
        total_pnl = total_asset - pt.CONFIG["initial_capital"]

        self.cash_var.set(f"¥{state['cash']:,.0f}")
        self.asset_var.set(f"¥{total_asset:,.0f}")
        self.pnl_var.set(f"¥{total_pnl:+,.0f} ({total_pnl/pt.CONFIG['initial_capital']:+.1%})")
        self.pos_var.set(f"{len(state['positions'])}/{pt.CONFIG['max_positions']}只")

        # 持仓表格
        for row in self.pos_tree.get_children():
            self.pos_tree.delete(row)
        for code, pos in state["positions"].items():
            cp = pos.get("current_price", pos["entry_price"])
            display_name = f"[板]{pos['name']}" if pos.get("board_trade") else pos["name"]
            self.pos_tree.insert("", tk.END, values=(
                code, display_name, pos["qty"],
                f"{pos['entry_price']:.2f}", f"{cp:.2f}",
                f"{pos.get('pnl_value', 0):+,.0f}",
                f"{pos.get('pnl_pct', 0):+.1%}",
                f"{pos.get('peak', pos['entry_price']):.2f}",
                pos.get("days", 0),
            ))

    def _update_ui(self, stocks):
        # 账户概览 + 持仓表格
        self._update_account_and_positions(stocks)

        state = pt.load_state()
        # 状态栏 + 市场分析
        if not stocks:
            self.status_bar.config(text="⚠ 行情获取失败，显示缓存数据")
        else:
            breadth = pt.calc_market_breadth(stocks)
            regime, max_pos, threshold, pos_pct = pt.get_market_regime(breadth)
            regime_icons = {"强势": "🟢", "中性": "🟡", "弱势(抱团)": "🟠", "崩溃(防守)": "🔴"}
            icon = regime_icons.get(regime, "")
            auto_tag = " [自动]" if self._auto_trade.get() else ""
            board_count = sum(1 for p in state["positions"].values() if p.get("board_trade"))
            board_tag = f" | 🎯打板{board_count}只" if board_count > 0 or pt.CONFIG.get("board_trade_enabled", True) else ""
            self.status_bar.config(text=f"{icon} {regime}{auto_tag} | 上涨{breadth:.1%} | {max_pos}仓/阈值{threshold}分{board_tag} | {len(stocks)}只")

            # 首次加载输出市场+板块分析
            if not self._market_logged:
                self._market_logged = True
                self._safe_log(f"\n{'─'*60}\n")
                self._safe_log(f"  {icon} 市场环境: {regime}\n")
                self._safe_log(f"  上涨占比: {breadth:.1%} | 允许{max_pos}仓 | "
                              f"买入阈值{threshold}分 | 单仓{pos_pct:.0%}\n")
                try:
                    sector_strength = pt.calc_sector_strength(stocks)
                    top_sectors = pt.get_top_sectors(sector_strength, pt.CONFIG["sector_top_n"])
                    if top_sectors:
                        self._safe_log(f"  强势板块:\n")
                        for i, (s_name, s_info) in enumerate(top_sectors):
                            leaders = s_info.get("leaders", [])
                            ldr = f" | 龙头: {', '.join(n[:4] for _,n,_ in leaders[:3])}" if leaders else ""
                            up_n = int(s_info['up_ratio'] * s_info['count'])
                            self._safe_log(
                                f"    {i+1}. {s_name} 均涨{s_info['avg_pct']:+.2f}% "
                                f"上涨{up_n}/{s_info['count']}({s_info['up_ratio']:.0%}){ldr}\n")
                except Exception:
                    pass
                # 自动交易状态提示
                if self._auto_trade.get():
                    self._safe_log(f"  ⚡ 自动交易已开启，交易时段将自动扫描买卖\n")
                self._safe_log(f"{'─'*60}\n")

    # ================================================================
    # 竞价打板面板刷新（主线程调用）
    # ================================================================
    def _update_auction_panel(self, results, scan_time):
        """更新竞价打板公示面板"""
        for row in self.auction_tree.get_children():
            self.auction_tree.delete(row)
        if not results:
            if scan_time:
                self.auction_hint.config(
                    text=f"✅ {scan_time} 扫描完成，暂无符合条件候选（评分<阈值{pt.CONFIG['auction_score_threshold']}）",
                    foreground="#888")
            return
        for i, r in enumerate(results):
            s = r.get("streak", 0)
            streak_text = {0: "首板", 1: "二板", 2: "三板"}.get(s, f"{s}板")
            self.auction_tree.insert("", tk.END, values=(
                i + 1, r["code"], r["name"],
                f"{r['auction_pct']:+.1f}%", f"{r['vol_ratio']:.0%}",
                r["score"], streak_text, r["sector"],
            ))
        top = results[0]
        self.auction_hint.config(
            text=f"✅ {scan_time} | 共{len(results)}只候选，首位 {top['name']} "
                 f"高开{top['auction_pct']:+.1f}% 评分{top['score']}分 | 建议预留仓位 "
                 f"¥{pt.CONFIG['initial_capital']*pt.CONFIG['auction_reserve_pct']:,.0f} "
                 f"({pt.CONFIG['auction_reserve_pct']:.0%})",
            foreground="#0a0")

    # ================================================================
    # 按钮状态管理
    # ================================================================
    def _set_buttons(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        for btn in [self.btn_refresh, self.btn_scan, self.btn_signal,
                     self.btn_loop, self.btn_reset]:
            btn.config(state=state)

    def _task_start(self, msg):
        self._set_buttons(False)
        self.progress.pack(side=tk.LEFT, padx=5)
        self.progress.start()
        self.status_bar.config(text=msg)

    def _task_done(self, error=None):
        """后台任务完成——总是在主线程调用"""
        self.progress.stop()
        self.progress.pack_forget()
        self._set_buttons(True)
        self.btn_stop.config(state=tk.DISABLED)
        self._refresh_status()
        self.status_bar.config(text="就绪")
        if error:
            messagebox.showerror("错误", f"任务失败:\n{error}")

    # ================================================================
    # 后台任务
    # ================================================================
    def _threaded_scan(self):
        if self._loop_running:
            return  # 循环中不重复扫描
        self._task_start("扫描交易中...")
        def _run():
            err = None
            try:
                pt.run_once(verbose=True)
            except Exception as e:
                err = str(e)
            self.root.after(0, lambda: self._task_done(err))
        threading.Thread(target=_run, daemon=True).start()

    def _threaded_signal(self):
        if self._loop_running:
            return
        self._task_start("信号扫描中（不交易）...")
        def _run():
            err = None
            try:
                pt.run_backtest()
            except Exception as e:
                err = str(e)
            self.root.after(0, lambda: self._task_done(err))
        threading.Thread(target=_run, daemon=True).start()

    def _threaded_loop(self):
        if self._loop_running:
            return
        self._loop_running = True
        self._set_buttons(False)
        self.btn_stop.config(state=tk.NORMAL)
        self.progress.pack(side=tk.LEFT, padx=5)
        self.progress.start()
        self.status_bar.config(text="🔁 持续循环+打板轮询运行中...")
        def _run():
            while self._loop_running:
                now = datetime.now()

                # ★★ 竞价打板窗口（9:25-9:30）优先
                if pt.is_auction_time(now):
                    try:
                        results = pt.run_auction_scan(verbose=True)
                        scan_time = pt.get_auction_results()[1]
                        self.root.after(0, lambda r=results, t=scan_time:
                                        self._update_auction_panel(r, t))
                        if results:
                            self.root.after(0, lambda r=results: self.status_bar.config(
                                text=f"🎯 竞价打板: {len(r)}只候选，首位{r[0]['name']} 评分{r[0]['score']}分"))
                        else:
                            self.root.after(0, lambda: self.status_bar.config(
                                text="🎯 竞价打板扫描中..."))
                    except Exception as e:
                        self.root.after(0, lambda e=str(e): self._safe_log(
                            f"[WARN] 竞价打板异常: {e}\n"))
                    time.sleep(pt.CONFIG.get("auction_scan_interval", 5))
                    continue

                if pt.CONFIG.get("trade_only_hours", True) and not pt.is_trading_time(now):
                    t = now.strftime("%H:%M")
                    self.root.after(0, lambda t=t: self.status_bar.config(
                        text=f"⏰ 非交易时段({t})，等待中..."))
                    time.sleep(30)
                    continue
                try:
                    # 完整扫描
                    pt.run_once(verbose=True)
                except Exception as e:
                    self.root.after(0, lambda e=str(e): self._safe_log(
                        f"[ERROR] 扫描异常: {e}\n"))

                # ★ 打板秒级轮询
                board_interval = pt.CONFIG.get("board_scan_interval", 5)
                board_rounds = pt.CONFIG.get("loop_interval", 60) // board_interval
                for _ in range(board_rounds):
                    if not self._loop_running:
                        break
                    now2 = datetime.now()
                    if not pt.is_trading_time(now2):
                        break
                    try:
                        result = pt.run_board_scan()
                        if result:
                            self.root.after(0, lambda r=result: self._safe_log(
                                f"🎯 打板轮询: 买入{r}笔\n"))
                    except Exception as e:
                        self.root.after(0, lambda e=str(e): self._safe_log(
                            f"[WARN] 打板轮询异常: {e}\n"))
                    time.sleep(board_interval)
            self.root.after(0, self._task_done)
        threading.Thread(target=_run, daemon=True).start()

    def _stop_loop(self):
        self._loop_running = False
        self.status_bar.config(text="正在停止循环...")

    def _show_history(self):
        """弹出历史交割单窗口（按天统计，含买入日内浮盈）"""
        win = tk.Toplevel(self.root)
        win.title("📜 历史交割单（按天统计）")
        win.geometry("980x620")

        loading = ttk.Label(win, text="⏳ 加载交割单与日内浮盈中...", font=("", 11), padding=24)
        loading.pack()

        def _load():
            err = None
            try:
                daily = pt.get_daily_trades()
            except Exception as e:
                daily = []
                err = str(e)
            self.root.after(0, lambda: self._finish_history(win, loading, daily, err))
        threading.Thread(target=_load, daemon=True).start()

    def _finish_history(self, win, loading, daily, err):
        loading.destroy()
        if err:
            ttk.Label(win, text=f"加载失败: {err}", foreground="#c0392b",
                      padding=12).pack()
            return

        # 顶部汇总：已实现 + 买入浮盈
        total_realized = 0.0
        total_intraday = 0.0
        for _, trades, buy_pnl in daily:
            total_realized += sum(t.get("pnl", 0) for t in trades if t["side"] == "sell")
            total_intraday += buy_pnl
        sell_count = sum(1 for _, trades, _ in daily
                         for t in trades if t["side"] == "sell")
        summary = ttk.Label(
            win,
            text=(f"共 {len(daily)} 个交易日 | 已平仓 {sell_count} 笔 | "
                  f"已实现盈亏 {total_realized:+,.0f} + 买入浮盈 {total_intraday:+,.0f}"
                  f" = 合计 {total_realized + total_intraday:+,.0f} 元"),
            font=("", 10, "bold"), padding=8)
        summary.pack(anchor=tk.W)

        # 交割单表格（树形：日期为父节点，交易为子节点）
        cols = ("时间", "方向", "代码", "名称", "价格", "数量", "成交额", "盈亏", "理由")
        tree = ttk.Treeview(win, columns=cols, show="tree headings", height=22)
        tree.heading("#0", text="日期 / 汇总")
        tree.column("#0", width=300, anchor=tk.W)
        widths = {"时间": 70, "方向": 45, "代码": 70, "名称": 90, "价格": 65,
                  "数量": 70, "成交额": 90, "盈亏": 90, "理由": 320}
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=widths.get(col, 80),
                        anchor=tk.W if col == "理由" else tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        for date, trades, buy_pnl in daily:
            day_realized = sum(t.get("pnl", 0) for t in trades if t["side"] == "sell")
            day_total = day_realized + buy_pnl
            buy_n = sum(1 for t in trades if t["side"] == "buy")
            sell_n = sum(1 for t in trades if t["side"] == "sell")
            parent = tree.insert(
                "", tk.END,
                text=(f"{date}   买{buy_n} 卖{sell_n}   "
                      f"已实现{day_realized:+,.0f}  买入浮盈{buy_pnl:+,.0f}  "
                      f"当日合计{day_total:+,.0f} 元"),
                open=True, tags=("date",))
            for t in trades:
                side = "买入" if t["side"] == "buy" else "卖出"
                amount = t["price"] * t["qty"]
                if t["side"] == "sell":
                    pnl = t.get("pnl")
                    pnl_s = f"{pnl:+,.0f}" if pnl is not None else ""
                else:
                    ip = t.get("intraday_pnl")
                    pnl_s = f"浮{ip:+,.0f}" if ip is not None else "—"
                tree.insert(parent, tk.END, text="", values=(
                    (t.get("time", "") or "")[11:16], side, t["code"], t["name"],
                    f"{t['price']:.2f}", t["qty"], f"{amount:,.0f}",
                    pnl_s, t.get("reason", ""),
                ), tags=("sell",) if t["side"] == "sell" else ("buy",))
        tree.tag_configure("date", background="#e8e8e8", font=("", 9, "bold"))
        tree.tag_configure("sell", foreground="#c0392b")
        tree.tag_configure("buy", foreground="#1e7d32")

    def _show_wechat_settings(self):
        """弹出微信提醒设置窗口（Server酱 SendKey 配置）"""
        win = tk.Toplevel(self.root)
        win.title("🔔 微信异动提醒设置")
        win.geometry("560x360")

        cfg = pt.load_notify_config()

        info = ttk.Label(win, text=(
            "使用 Server酱 把异动提醒推送到你的微信：\n"
            "1. 用微信扫码登录 sct.ftqq.com 注册\n"
            "2. 复制你的 SendKey（形如 SCTxxxx 或 sctp...）\n"
            "3. 粘贴到下方 → 点「测试推送」验证 → 保存\n"
            "之后触发 清仓/加仓/主力出货/做T 等信号时，会自动推送到你微信。"),
            justify=tk.LEFT, padding=12)
        info.pack(anchor=tk.W)

        key_frame = ttk.Frame(win, padding=(12, 0))
        key_frame.pack(fill=tk.X)
        ttk.Label(key_frame, text="SendKey:").pack(side=tk.LEFT)
        key_var = tk.StringVar(value=cfg.get("sendkey", ""))
        key_entry = ttk.Entry(key_frame, textvariable=key_var, width=44, show="*")
        key_entry.pack(side=tk.LEFT, padx=6)
        ttk.Button(key_frame, text="显示",
                   command=lambda: key_entry.config(
                       show="" if key_entry.cget("show") == "*" else "*")
                   ).pack(side=tk.LEFT)

        enabled_var = tk.BooleanVar(value=bool(cfg.get("enabled", False)))
        ttk.Checkbutton(win, text="启用微信提醒", variable=enabled_var,
                        padding=(12, 8)).pack(anchor=tk.W)

        btn_frame = ttk.Frame(win, padding=12)
        btn_frame.pack(fill=tk.X)

        def do_test():
            key = key_var.get().strip()
            if not key:
                messagebox.showwarning("提示", "请先填写 SendKey", parent=win)
                return
            pt.set_wechat(key, True)
            enabled_var.set(True)
            def _send():
                ok = pt.wechat_notify("A股模拟盘测试",
                                      "微信推送已连通！之后有清仓/加仓/出货/做T等异动会这样提醒你。")
                def done():
                    if ok:
                        messagebox.showinfo("成功", "测试消息已发送，请查看微信（配置已保存）", parent=win)
                    else:
                        messagebox.showerror("失败", "发送失败，请检查 SendKey 是否完整（含 SCT/sctp 前缀）", parent=win)
                self.root.after(0, done)
            threading.Thread(target=_send, daemon=True).start()

        ttk.Button(btn_frame, text="📤 测试推送并保存", command=do_test).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="💾 保存", command=lambda: self._wl_save_wechat(key_var.get(), enabled_var.get(), win)).pack(side=tk.LEFT, padx=4)

    def _wl_save_wechat(self, key, enabled, win):
        if enabled and not key.strip():
            messagebox.showwarning("提示", "启用提醒需填写 SendKey")
            return
        pt.set_wechat(key, enabled)
        messagebox.showinfo("已保存", "微信提醒配置已保存")
        win.destroy()

    # ================================================================
    # 自选股深度分析（纯本地算法，无额外API）
    # ================================================================
    def _threaded_analyze(self):
        if self._loop_running:
            return
        self._task_start("深度分析自选股中...")
        def _run():
            err = None
            result = None
            try:
                result = pt.analyze_watchlist(include_market=True)
            except Exception as e:
                err = str(e)
            self.root.after(0, lambda: self._finish_analyze(result, err))
        threading.Thread(target=_run, daemon=True).start()

    def _finish_analyze(self, result, err):
        self.progress.stop()
        self.progress.pack_forget()
        self._set_buttons(True)
        self.btn_stop.config(state=tk.DISABLED)
        self.status_bar.config(text="就绪")
        if err:
            messagebox.showerror("错误", f"分析失败:\n{err}")
            return
        if not result or not result.get("stocks"):
            messagebox.showinfo("提示", result.get("message", "自选股池为空"))
            return
        self._show_analysis(result)

    @staticmethod
    def _fmt(v, fmt="{:.2f}"):
        return fmt.format(v) if v is not None else "—"

    def _show_analysis(self, result):
        win = tk.Toplevel(self.root)
        win.title("🔍 自选股深度分析")
        win.geometry("1080x660")

        # 顶部：市场环境
        m = result.get("market") or {}
        top_frame = ttk.Frame(win, padding=10)
        top_frame.pack(fill=tk.X)
        regime = m.get("regime", "—")
        breadth = m.get("breadth", 0) or 0
        icons = {"强势": "🟢", "中性": "🟡", "弱势(抱团)": "🟠", "崩溃(防守)": "🔴"}
        top_text = f"{icons.get(regime, '')} 市场环境: {regime} | 上涨占比 {breadth:.1%}"
        tops = m.get("top_sectors", []) or []
        if tops:
            top_text += " | 最强板块: " + ", ".join(f"{s[0]}(+{s[1]:.2f}%)" for s in tops[:5])
        ttk.Label(top_frame, text=top_text, font=("", 10, "bold")).pack(anchor=tk.W)

        # 表格
        cols = ("代码", "名称", "板块", "现价", "涨跌%", "5日%", "评分", "均线", "RSI", "洗盘/出货")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=8)
        widths = {"代码": 70, "名称": 90, "板块": 95, "现价": 60, "涨跌%": 60,
                  "5日%": 60, "评分": 45, "均线": 90, "RSI": 45, "洗盘/出货": 100}
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=widths.get(col, 70), anchor=tk.CENTER)
        tree.pack(fill=tk.X, padx=10, pady=5)

        # 详情区
        detail = scrolledtext.ScrolledText(win, height=12, font=("Consolas", 10),
                                           wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4")
        detail.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        bh_cn = {"washout": "洗盘", "distribution": "出货", "neutral": "中性"}

        def fill_row(a):
            if a.get("error"):
                return (a["code"], a["name"], a["sector"], "—", "—", "—", "—", "—", "—", a["error"])
            bh = bh_cn.get(a["behavior"], "—")
            bh_txt = f"{bh}({a['conf']:.0%})" if a["behavior"] != "neutral" else "中性"
            return (a["code"], a["name"], a["sector"],
                    self._fmt(a["price"]), self._fmt(a["pct_chg"], "{:+.2f}"),
                    self._fmt(a["chg5"], "{:+.1%}"), a["score"], a["ma_state"],
                    self._fmt(a["rsi"], "{:.0f}"), bh_txt)

        for a in result["stocks"]:
            tree.insert("", tk.END, values=fill_row(a), iid=a["code"])

        def on_select(_e=None):
            sel = tree.selection()
            if not sel:
                return
            code = sel[0]
            a = next((x for x in result["stocks"] if x["code"] == code), None)
            if not a or a.get("error"):
                detail.delete("1.0", tk.END)
                detail.insert(tk.END, a.get("error", "无数据") if a else "无数据")
                return
            bh = bh_cn.get(a["behavior"], "—")
            bh_txt = f"{bh}({a['conf']:.0%})" if a["behavior"] != "neutral" else "中性"
            lines = [
                f"【{a['name']} {a['code']}】  板块: {a['sector']}",
                f"现价 {self._fmt(a['price'])} | 涨跌 {self._fmt(a['pct_chg'], '{:+.2f}')}% | "
                f"成交额 {self._fmt(a['amount']/1e8, '{:.2f}')}亿" if a["amount"] else f"现价 {self._fmt(a['price'])}",
                f"均线: MA5={self._fmt(a['ma5'])} MA10={self._fmt(a['ma10'])} "
                f"MA20={self._fmt(a['ma20'])} → {a['ma_state']}",
                f"MACD: {a['macd_state']} | RSI: {self._fmt(a['rsi'], '{:.0f}')}({a['rsi_state']}) "
                f"| RSRS: {self._fmt(a['rsrs'])}",
                f"量比 {self._fmt(a['vol_ratio'])} | 5日 {self._fmt(a['chg5'], '{:+.1%}')} "
                f"| 20日 {self._fmt(a['chg20'], '{:+.1%}')} | 距20日高点 {self._fmt(a['dist_high'], '{:.1%}')}",
                f"洗盘/出货: {bh_txt}" + (f" | 连板: {a['streak']}" if a.get("streak") else ""),
                f"综合评分: {a['score']}分",
            ]
            if a["signals"]:
                lines.append("信号: " + "、".join(a["signals"]))
            if a.get("sector_in_top"):
                lines.append(f"板块「{a['sector']}」今日均涨 +{a['sector_avg_pct']:.2f}%（Top板块）")
            else:
                lines.append(f"板块「{a['sector']}」今日未进Top{pt.CONFIG['sector_top_n']}")
            lines.append("")
            lines.append(f"💡 建议: {a['advice']}")
            detail.delete("1.0", tk.END)
            detail.insert(tk.END, "\n".join(lines))

        tree.bind("<<TreeviewSelect>>", on_select)
        if result["stocks"]:
            tree.selection_set(result["stocks"][0]["code"])
            on_select()

    def _show_watchlist(self):
        """弹出自选股池 + 记忆好票管理窗口"""
        win = tk.Toplevel(self.root)
        win.title("⭐ 我的自选股票池 + 记忆好票")
        win.geometry("820x560")

        # --- 顶部：添加自选股 ---
        add_frame = ttk.Frame(win, padding=10)
        add_frame.pack(fill=tk.X)
        ttk.Label(add_frame, text="代码:").pack(side=tk.LEFT)
        code_entry = ttk.Entry(add_frame, width=10)
        code_entry.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(add_frame, text="名称(可空,自动补):").pack(side=tk.LEFT)
        name_entry = ttk.Entry(add_frame, width=14)
        name_entry.pack(side=tk.LEFT, padx=(4, 10))
        add_btn = ttk.Button(add_frame, text="➕ 加入自选")
        add_btn.pack(side=tk.LEFT, padx=4)
        hint = ttk.Label(add_frame, text="自选股会着重扫描(+10分)并触发桌面提醒",
                         foreground="#888")
        hint.pack(side=tk.LEFT, padx=12)

        # --- 中部：左右两栏 ---
        cols_frame = ttk.Frame(win, padding=10)
        cols_frame.pack(fill=tk.BOTH, expand=True)

        # 左栏：自选股
        left = ttk.Frame(cols_frame)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        ttk.Label(left, text="自选股票池（手动添加）", font=("", 10, "bold")).pack(anchor=tk.W)
        wl_cols = ("代码", "名称", "加入日期")
        wl_tree = ttk.Treeview(left, columns=wl_cols, show="headings", height=14)
        for c in wl_cols:
            wl_tree.heading(c, text=c)
            wl_tree.column(c, width=90, anchor=tk.CENTER)
        wl_tree.pack(fill=tk.BOTH, expand=True, pady=5)

        # 右栏：记忆好票
        right = ttk.Frame(cols_frame)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))
        ttk.Label(right, text="记忆好票（扫描自动标记）", font=("", 10, "bold")).pack(anchor=tk.W)
        mem_cols = ("代码", "名称", "命中", "最高分", "最近见")
        mem_tree = ttk.Treeview(right, columns=mem_cols, show="headings", height=14)
        for c in mem_cols:
            mem_tree.heading(c, text=c)
            mem_tree.column(c, width=88, anchor=tk.CENTER)
        mem_tree.pack(fill=tk.BOTH, expand=True, pady=5)

        # --- 底部按钮 ---
        bot_frame = ttk.Frame(win, padding=10)
        bot_frame.pack(fill=tk.X)
        ttk.Button(bot_frame, text="❌ 删除选中自选", command=lambda: self._wl_remove(wl_tree, mem_tree)).pack(side=tk.LEFT, padx=3)
        ttk.Button(bot_frame, text="🧹 清空记忆好票", command=lambda: self._wl_clear_memory(wl_tree, mem_tree)).pack(side=tk.LEFT, padx=3)
        ttk.Button(bot_frame, text="🔄 刷新", command=lambda: self._wl_refresh(wl_tree, mem_tree)).pack(side=tk.LEFT, padx=3)

        def do_add():
            code = code_entry.get().strip()
            if not code:
                messagebox.showwarning("提示", "请输入股票代码", parent=win)
                return
            name = name_entry.get().strip()
            def _work():
                resolved = name
                if not resolved:
                    try:
                        q = pt.fetch_quotes([code])
                        if code in q and q[code].get("name"):
                            resolved = q[code]["name"]
                    except Exception:
                        pass
                ok, msg = pt.add_to_watchlist(code, resolved)
                self.root.after(0, lambda ok=ok, msg=msg: self._wl_add_done(ok, msg, code_entry, name_entry, wl_tree, mem_tree))
            threading.Thread(target=_work, daemon=True).start()

        add_btn.config(command=do_add)
        self._wl_refresh(wl_tree, mem_tree)

    def _wl_refresh(self, wl_tree, mem_tree):
        data = pt.load_watchlist()
        for row in wl_tree.get_children():
            wl_tree.delete(row)
        for w in data.get("watchlist", []):
            wl_tree.insert("", tk.END, values=(
                w.get("code"), w.get("name", ""), w.get("added", "")))
        for row in mem_tree.get_children():
            mem_tree.delete(row)
        for m in sorted(data.get("memory_stocks", []),
                        key=lambda x: -x.get("max_score", 0)):
            mem_tree.insert("", tk.END, values=(
                m.get("code"), m.get("name", ""),
                m.get("hits", 1), m.get("max_score", 0),
                m.get("last_seen", "")))

    def _wl_add_done(self, ok, msg, code_entry, name_entry, wl_tree, mem_tree):
        if ok:
            code_entry.delete(0, tk.END)
            name_entry.delete(0, tk.END)
            self._wl_refresh(wl_tree, mem_tree)
        else:
            messagebox.showinfo("提示", msg)

    def _wl_remove(self, wl_tree, mem_tree):
        sel = wl_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中要删除的自选股")
            return
        for item in sel:
            code = wl_tree.item(item, "values")[0]
            pt.remove_from_watchlist(code)
        self._wl_refresh(wl_tree, mem_tree)

    def _wl_clear_memory(self, wl_tree, mem_tree):
        ok = messagebox.askyesno("确认", "清空所有记忆好票标记？")
        if ok:
            data = pt.load_watchlist()
            data["memory_stocks"] = []
            pt.save_watchlist(data)
            self._wl_refresh(wl_tree, mem_tree)

    def _reset_account(self):
        ok = messagebox.askyesno("确认重置", "将清空所有持仓和交易记录，确定吗？")
        if ok:
            pt.save_state({"cash": pt.CONFIG["initial_capital"], "positions": {}, "trades": []})
            self._refresh_status()
            self._safe_log("[INFO] 账户已重置\n")

    def on_close(self):
        self._loop_running = False
        self.log_handler.detach()
        self.root.destroy()


# ============================================================
# 启动
# ============================================================
def main():
    root = tk.Tk()
    app = PaperTraderApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()

if __name__ == "__main__":
    main()
