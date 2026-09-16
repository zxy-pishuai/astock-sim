/* ===== J5 core/timer.js — 轮询降级：非交易时段 30s / 交易日盘中用基础间隔 =====
 * 判定依据：/api/calendar?date=（30 分钟缓存）+ 本地时间 09:15-15:05 窗口。
 * visibility 暂停/恢复由 main.js 挂接（隐藏 → clearTimers + abort 池；可见 → switchPage 重启）。
 */
import { fetchJson } from "./api.js";

const TimerHub = {
  _cal: null,
  _calTs: 0,
  async ensureCal() {
    if (Date.now() - this._calTs < 30 * 60 * 1000) return this._cal;
    try {
      const d = new Date();
      const ds = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      const r = await fetchJson("/api/calendar?date=" + ds);
      this._cal = r.is_trading === true;
      this._calTs = Date.now();
    } catch (e) { this._cal = null; }
    return this._cal;
  },
  isTradingNow() {
    const d = new Date();
    const hm = d.getHours() * 100 + d.getMinutes();
    return hm >= 915 && hm <= 1505;
  },
  /* 返回应使用的轮询间隔：非交易日或盘外 → 至少 30s */
  intervalFor(name, baseMs) {
    if (!this.isTradingNow() || this._cal === false) return Math.max(baseMs, 30000);
    return baseMs;
  },
};

Object.assign(window, { TimerHub });
export { TimerHub };
