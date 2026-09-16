#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天玑量化终端 TianjiQuant — A股实战派量化终端（原 A股模拟盘 Pro）
用法：
  python main.py            # 桌面独立窗口（默认，pywebview + WebView2）
  python main.py --browser  # 浏览器模式
  python main.py --cli      # 自检模式
  python main.py --port 8899
"""
import argparse
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# Windows 控制台/重定向 UTF-8（★ G4 2026-09-13：stderr 同样 reconfigure——重定向日志
# 若按 locale(GBK) 编码，含中文 traceback 全部 mojibake；PYTHONUTF8 供子进程继承）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.environ.setdefault("PYTHONUTF8", "1")
# pythonw 静默模式
if sys.stdout is None:
    class _NullOut:
        def write(self, *a, **k): pass
        def flush(self): pass
    sys.stdout = _NullOut()

from app import server as srv
from app import datafeed as df


def _is_running(port):
    """检测端口是否已有本服务实例"""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_ready(port, timeout=30):
    """★ 4.5 修复：等待 HTTP 服务真正就绪（否则窗口打开后立即请求会被拒/K线刷不出）。
    服务线程在 serve_forever 启动后有短暂窗口期不可用，这里轮询到 /api/overview 返回 200。
    """
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ★ 4.5 单实例锁：PID 文件 + 端口健康双重校验，防多实例/僵死进程
# ★ F5（2026-09-08）重构：端口健康为唯一权威；锁文件只在真正 serve 前写，
#   根治"锁被纯 UI 进程劫持"（旧逻辑在锁 PID 已死但端口健康时清锁写自己 PID，
#   随后 _is_running 分支直接开窗口 return → app.lock 指向无服务进程）。
_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.lock")


def _pid_alive(pid):
    """Windows: OpenProcess 探测进程存活。"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def _acquire_lock(port):
    """尝试获取单实例锁。返回 (ok, msg)。
    ★ F5：端口健康是唯一权威——
      - 端口健康 → (False, 已有实例)，绝不触碰锁文件；
      - 端口不健康 → (True, 可接管)，仅当旧 PID 已死才清旧锁；
      本函数不写锁文件（写锁只在真正 serve 前由 _write_lock 进行）。"""
    try:
        import socket
        # 端口健康检查（唯一权威：有服务在跑就是已有实例）
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5):
                return False, "已有实例运行中(端口 %d 健康)" % port
        except OSError:
            pass  # 端口不健康 → 本进程可接管
        old_pid = 0
        try:
            with open(_LOCK_FILE, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
        except (ValueError, IOError):
            old_pid = 0
        if old_pid > 0 and not _pid_alive(old_pid):
            # 旧 PID 已死：清锁（不重写——写锁移到真正 serve 前）
            try:
                os.remove(_LOCK_FILE)
            except OSError:
                pass
            return True, "清理已死实例锁(PID %d)" % old_pid
        if old_pid > 0:
            # 旧 PID 存活但端口不健康（僵死）：接管但不删锁，serve 前覆盖写锁
            return True, "旧 PID %d 存活但端口不健康(僵死)，将接管" % old_pid
        return True, ""
    except Exception:
        return True, ""


def _write_lock():
    """★ F5：只有确定要 serve 时才写 app.lock（写锁时机 = serve 前）。"""
    try:
        with open(_LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


def run_browser(port, url):
    import webbrowser
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()


def run_desktop(port, url):
    """pywebview 独立窗口；失败回退浏览器"""
    try:
        import webview
    except ImportError:
        srv.log("pywebview 不可用，回退浏览器模式")
        run_browser(port, url)
        return False
    try:
        win = webview.create_window(
            "天玑量化终端 TianjiQuant", url,
            width=1520, height=940, min_size=(1180, 760),
            background_color="#0b0e14",
        )
        webview.start()
        srv.log("窗口已关闭，退出服务")
        return True
    except Exception as e:
        srv.log(f"桌面窗口启动失败({e})，回退浏览器模式")
        run_browser(port, url)
        return False


def main():
    ap = argparse.ArgumentParser(description="天玑量化终端 TianjiQuant v4.5")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--browser", action="store_true", help="浏览器模式")
    ap.add_argument("--no-window", action="store_true", help="仅启动服务不打开窗口")
    ap.add_argument("--cli", action="store_true", help="自检后退出")
    args = ap.parse_args()

    srv.log("=" * 46)
    srv.log("  天玑量化终端 TianjiQuant v4.5 — A股实战派量化终端")
    srv.log("=" * 46)

    if args.cli:
        try:
            idx = df.fetch_indices()
            srv.log(f"指数行情 OK: {len(idx)} 个")
        except Exception as e:
            srv.log(f"指数行情失败: {e}")
        try:
            k = df.fetch_kline("600519", "day", 60)
            srv.log(f"K线接口 OK: 贵州茅台 {len(k)} 根")
        except Exception as e:
            srv.log(f"K线接口失败: {e}")
        srv.log("自检完成")
        return

    # ★ F5：先做单实例锁检查（端口健康=唯一权威），再定端口
    ok_lock, lock_msg = _acquire_lock(args.port)
    if not ok_lock:
        srv.log(lock_msg)
        if not args.no_window:
            if args.browser:
                run_browser(args.port, f"http://127.0.0.1:{args.port}/")
            else:
                run_desktop(args.port, f"http://127.0.0.1:{args.port}/")
        return
    if lock_msg:
        srv.log(lock_msg)

    # 单实例：服务已在跑则只打开窗口/浏览器（防竞态：检查后端口被其他实例接管）
    if _is_running(args.port):
        srv.log(f"检测到已有实例(端口{args.port})，直接打开")
        if not args.no_window:
            if args.browser:
                run_browser(args.port, f"http://127.0.0.1:{args.port}/")
            else:
                run_desktop(args.port, f"http://127.0.0.1:{args.port}/")
        return

    # ★ F5：find_free_port 移到取锁成功之后（端口决策基于当前真实状态）
    port = srv.find_free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    # ★ F5：只有确定要 serve 才写 app.lock
    if not _write_lock():
        srv.log("警告: app.lock 写入失败，继续启动（守护方将以端口反查为准）")

    httpd = srv.serve("127.0.0.1", port)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    srv.log(f"服务已启动: {url}")
    srv.log("数据源: 腾讯行情+K线 / 新浪备用 | 缓存: SQLite磁盘+内存TTL")
    srv.log("关闭窗口或 Ctrl+C 退出")
    # ★ F5 启动自检：本进程必须持有锁且端口已监听，否则主动退出不当孤儿
    try:
        import socket as _socket
        with open(_LOCK_FILE, "r", encoding="utf-8") as f:
            lock_pid = int(f.read().strip())
        if lock_pid != os.getpid():
            srv.log(f"自检失败：app.lock 属主 PID={lock_pid}≠本进程 {os.getpid()}，主动退出")
            sys.exit(1)
        with _socket.create_connection(("127.0.0.1", port), timeout=1.5):
            pass
    except OSError:
        srv.log(f"自检失败：端口 {port} 未监听，主动退出")
        sys.exit(1)
    except Exception as e:
        srv.log(f"自检失败：{e!r}，主动退出")
        sys.exit(1)
    # ★ 4.5 修复：等服务真正就绪再开窗口，避免"K线刷不出来"
    if not _wait_ready(port, timeout=30):
        srv.log("警告: 服务 30s 内未就绪，仍继续启动窗口")
    # ★ 4.5：后台自动增量更新数据（min5 + 日K，不阻塞启动）
    try:
        from app import updater
        updater.start_background_update()
        srv.log("后台增量更新已启动（min5/日K 自动补最新数据）")
    except Exception as e:
        srv.log(f"增量更新启动失败: {e}")
    # ★ 开盘自动开启交易引擎（交易日 9:15 自动启动；用户手动停止后当天不再自动重启）
    try:
        from app import trader
        trader.engine.enable_auto_start()
        srv.log("开盘自动开启已启用（交易日 9:15 自动启动交易引擎）")
    except Exception as e:
        srv.log(f"开盘自动开启启动失败: {e}")
    # ★ P65 影子日更调度（盘中 record + 收盘 settle，只记账不碰账户）
    try:
        from app import shadow_scheduler as _shd
        _shd.start()
        srv.log("影子日更调度已启动（盘中记账/收盘结算，影子账不碰账户）")
    except Exception as e:
        srv.log(f"影子调度启动失败: {e}")
    # ★ 接口加速：后台预热股票列表（避免开盘第一次扫描卡 16 秒拉列表）
    try:
        from app import datafeed as _df
        def _warm_list():
            try:
                _df.get_stock_list(force=True)
            except Exception:
                pass
        threading.Thread(target=_warm_list, daemon=True).start()
        srv.log("股票列表后台预热已启动")
    except Exception as e:
        srv.log(f"列表预热失败: {e}")
    # ★ M1a A1-b（2026-09-06）：全宇宙预热改懒加载 + 交易日定时。
    # 非交易日/夜间只做股票列表最小预热（上方已启动）；资金面全宇宙预热
    # （fetch_all_stocks + top30 资金面信号，峰值 ~1.6GB）挪到交易日 09:05 触发，
    # 避免开机/自愈后无条件抢 1.6GB 7 分钟。
    try:
        from app import trading_calendar as _tc
        _today = time.strftime("%Y-%m-%d")
        _hm = time.localtime().tm_hour * 100 + time.localtime().tm_min
        if _tc.is_trading_day(_today) and 830 <= _hm <= 1530:
            # 交易日盘中时段：立即预热
            from app import updater as _upd
            _upd.warm_moneyflow_top(limit=30)
            srv.log("资金面缓存预热已启动（交易日盘中，top30 成交额）")
        elif _tc.is_trading_day(_today) and _hm < 830:
            # 交易日盘前：后台线程等到 09:05 触发预热
            def _wait_and_warm():
                try:
                    _target = time.mktime(time.strptime(_today + " 09:05:00", "%Y-%m-%d %H:%M:%S"))
                    _sleep = max(1, _target - time.time())
                    time.sleep(_sleep)
                    from app import updater as _upd2
                    _upd2.warm_moneyflow_top(limit=30)
                    srv.log("资金面缓存预热已触发（交易日 09:05 定时，top30 成交额）")
                except Exception:
                    pass
            threading.Thread(target=_wait_and_warm, daemon=True).start()
            srv.log("资金面缓存预热已排程（交易日 09:05 定时触发，当前盘前）")
        else:
            # 非交易日或盘后：跳过全宇宙预热（懒加载，首次真实请求时按需计算）
            srv.log("资金面缓存预热跳过（非交易日/盘后，懒加载模式）")
    except Exception as e:
        srv.log(f"资金面预热排程失败: {e}")

    try:
        if args.no_window:
            while True:
                time.sleep(3600)
        elif args.browser:
            run_browser(port, url)
            while True:
                time.sleep(3600)
        else:
            ok = run_desktop(port, url)
            if not ok:
                # 已回退浏览器，保持服务运行
                while True:
                    time.sleep(3600)
    except KeyboardInterrupt:
        srv.log("已退出")
        httpd.shutdown()


if __name__ == "__main__":
    main()
