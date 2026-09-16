/* ===== J5 core/state.js — 共享状态（window 桥接，与 legacy app.js 同语义） ===== */
const state = {
  page: "market",
  code: "600519",
  period: "day",
  marketPage: 1,
  marketSize: 50,
  marketSort: "amount",
  marketKw: "",
  watchlist: [],
  liveTimer: null,
  marketTimer: null,
  posTimer: null,
  idxTimer: null,
  trTimer: null,
  healthTimer: null,
  klineCache: {},   // key -> {ts, data}
  kchart: null,
  mchart: null,
  chartMode: "kline",  // kline | minute
};

Object.assign(window, { state });
export { state };
