// Authoritative client state = last server snapshot + sequenced deltas (spec p.134, p.142).
// The client never fabricates values: anything missing from the server stays null and renders as "—".
import { useSyncExternalStore } from "react";

export type Dict = Record<string, any>;

export interface State {
  connected: "connecting" | "live" | "reconnecting" | "offline";
  lastEventAt: number | null; // client clock ms when last message arrived
  serverOffsetMs: number; // server_time − client time (for countdowns)
  seq: number;
  market: Dict | null;
  account: Dict | null;
  chart: Dict | null;
  system: Dict | null;
  notices: Dict[];
  fills: Dict[];
}

let state: State = {
  connected: "connecting", lastEventAt: null, serverOffsetMs: 0, seq: -1,
  market: null, account: null, chart: null, system: null, notices: [], fills: [],
};
const listeners = new Set<() => void>();
let pending = false;

function emit() {
  // batch UI updates per animation frame (not trading decisions — those are server-side)
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => { pending = false; listeners.forEach((l) => l()); });
}

export function setState(patch: Partial<State>) { state = { ...state, ...patch }; emit(); }
export function getState() { return state; }
export function useStore<T>(sel: (s: State) => T): T {
  return useSyncExternalStore((cb) => { listeners.add(cb); return () => listeners.delete(cb); }, () => sel(state));
}

let ws: WebSocket | null = null;
let backoff = 500;
let timer: number | undefined;

function handle(msg: Dict) {
  const now = Date.now();
  if (msg.kind === "snapshot") {
    const d = msg.data;
    setState({ seq: msg.seq, market: d.market, account: d.account, chart: d.chart, system: d.system,
      serverOffsetMs: d.server_time * 1000 - now, lastEventAt: now, connected: "live" });
    return;
  }
  if (state.seq >= 0 && msg.seq !== state.seq + 1) { // sequence gap → fresh snapshot
    reconnect(true);
    return;
  }
  const patch: Partial<State> = { seq: msg.seq, lastEventAt: now, connected: "live" };
  switch (msg.kind) {
    case "market": patch.market = msg.data; patch.serverOffsetMs = msg.data.server_time * 1000 - now; break;
    case "account": patch.account = msg.data; break;
    case "chart": patch.chart = msg.data; break;
    case "fill": patch.fills = [msg.data, ...state.fills].slice(0, 50); break;
    case "notice": case "settlement": case "market_event": case "order":
      patch.notices = [{ ...msg.data, kind: msg.kind, at: now }, ...state.notices].slice(0, 50); break;
    case "heartbeat": patch.serverOffsetMs = msg.data.server_time * 1000 - now; break;
  }
  setState(patch);
}

export function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/api/stream`);
  setState({ connected: state.lastEventAt ? "reconnecting" : "connecting" });
  ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); backoff = 500; } catch { /* ignore malformed */ } };
  ws.onclose = (e) => {
    ws = null;
    if (e.code === 4401) { setState({ connected: "offline" }); window.dispatchEvent(new Event("kairos:unauth")); return; }
    setState({ connected: "reconnecting" });
    clearTimeout(timer);
    timer = window.setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 15000);
  };
}

export function reconnect(_gap = false) {
  try { ws?.close(); } catch { /* noop */ }
  ws = null;
  state = { ...state, seq: -1 };
  connect();
}

export function disconnect() { clearTimeout(timer); try { ws?.close(); } catch { /* noop */ } ws = null; }

// Browser suspension / resume (p.116, p.143): refresh authoritative state on foreground.
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") reconnect(); });
  window.addEventListener("online", () => reconnect());
  window.addEventListener("offline", () => setState({ connected: "offline" }));
}

export function dataAgeMs(s: State): number | null { return s.lastEventAt ? Date.now() - s.lastEventAt : null; }
export function serverNow(s: State): number { return (Date.now() + s.serverOffsetMs) / 1000; }
