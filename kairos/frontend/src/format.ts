// Formatting helpers. Missing values render as an em dash — never as zero (spec p.133).
export const DASH = "—";

export function num(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function usd(v: unknown, signed = false): string {
  const n = num(v);
  if (n === null) return DASH;
  const s = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${n < 0 ? "−" : signed && n > 0 ? "+" : ""}$${s}`;
}

export function cents(v: unknown, digits = 0): string {
  const n = num(v);
  if (n === null) return DASH;
  return `${(n * 100).toFixed(digits)}¢`;
}

export function pct(v: unknown, digits = 1, signed = false): string {
  const n = num(v);
  if (n === null) return DASH;
  return `${signed && n > 0 ? "+" : ""}${(n * 100).toFixed(digits)}%`;
}

export function price(v: unknown, digits = 2): string {
  const n = num(v);
  if (n === null) return DASH;
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function clock(sec: number | null): string {
  if (sec === null || !Number.isFinite(sec)) return DASH;
  const s = Math.max(0, Math.floor(sec));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function timeOf(ts: unknown): string {
  const n = num(ts);
  if (n === null) return DASH;
  return new Date(n * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function tone(v: unknown): string {
  const n = num(v);
  if (n === null || n === 0) return "text-text";
  return n > 0 ? "text-mint" : "text-coral";
}

export const REASON_LABELS: Record<string, string> = {
  nonpositive_conservative_ev: "Edge below cost",
  insufficient_depth: "Insufficient depth",
  rules_template_unverified: "Rules not yet verified",
  book_stale: "Book stale",
  btc_proxy_stale: "BTC feed stale",
  quote_stale: "Quote stale",
  mode_paused: "Paused",
  entries_paused_by_operator: "Entries paused",
  confidence_below_reject: "Weak evidence",
  daily_loss_limit: "Daily loss limit",
  after_entry_cutoff: "After cutoff",
  account_unreconciled: "Unreconciled",
  market_context_incomplete: "Market data incomplete",
  no_active_market: "No active window",
  strike_missing: "Strike missing",
  continuation_value_higher: "Hold value higher",
  liquidation_beats_continuation: "Exit beats hold",
  model_stale_fallback_hold: "Model stale — hold",
};

export function reason(r: string): string {
  const base = r.split(":")[0];
  if (base.startsWith("size_below_minimum")) return "Size cap below 1 contract";
  if (base === "halt") return `Halt: ${r.split(":")[1]}`;
  if (base === "approved") return "Approved";
  if (base === "binding") return `Bound by ${r.split(":")[1]}`;
  return REASON_LABELS[base] || base.replace(/_/g, " ");
}
