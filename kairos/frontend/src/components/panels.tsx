import { Activity, Bot, ChevronDown, ChevronRight, Layers } from "lucide-react";
import { ReactNode, useEffect, useState } from "react";
import { DASH, cents, clock, num, pct, reason, timeOf, tone, usd } from "../format";
import { serverNow, useStore } from "../state/store";
import { AnimatedNumber, Card, KIcon, Pill } from "./ui";

// ---------------------------------------------------------------------------------------------
// KPI strip: active capital, realized daily net P&L, savings, W/L, auto-trading state (p.132)
export function KpiStrip({ compact = false, onFunds }: { compact?: boolean; onFunds?: () => void }) {
  const a = useStore((s) => s.account);
  const f = a?.funds;
  const today = a?.today;
  const wl = today ? `${today.wins}/${today.losses}` : DASH;
  const auto = a ? (a.controls.entries_paused ? "PAUSED" : a.mode) : DASH;
  const autoTone = !a ? "muted" : a.controls.entries_paused || a.mode === "PAUSED" ? "amber" : "mint";
  const items: [string, ReactNode, ReactNode?][] = [
    ["Active capital", <AnimatedNumber value={f?.active} format={(n) => usd(n)} />, `Spendable ${usd(a?.spendable)}`],
    ["Realized net P&L (today)", <AnimatedNumber value={today?.realized_net} format={(n) => usd(n, true)} className={tone(today?.realized_net)} />, "After fees · UTC day"],
    ["Protected savings", <AnimatedNumber value={f?.savings} format={(n) => usd(n)} />, `Reserve ${usd(f?.reserve)} · excluded from sizing`],
    ["W / L (today)", <span className="tnum">{wl}</span>, `${today?.trades ?? 0} closed episodes`],
  ];
  return (
    <div className={`grid gap-px overflow-hidden rounded-card border border-line bg-line ${compact ? "grid-cols-2" : "grid-cols-2 lg:grid-cols-5"}`}>
      {items.map(([k, v, sub]) => (
        <button key={k} onClick={onFunds} className="min-w-0 bg-surface px-3 py-2 text-left hover:bg-surface2">
          <div className="truncate text-2xs text-muted">{k}</div>
          <div className="truncate text-[20px] font-semibold leading-7">{v}</div>
          <div className="truncate text-2xs text-muted">{sub}</div>
        </button>
      ))}
      {!compact && (
        <div className="flex items-center justify-between gap-2 bg-surface px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            <Bot size={18} className="text-blue" />
            <div className="min-w-0">
              <div className="text-2xs text-muted">Auto trading</div>
              <div className="truncate text-[13px] font-semibold">{a?.paper === false ? "LIVE" : "PAPER"} · <span className={autoTone === "mint" ? "text-mint" : "text-amber"}>{auto}</span></div>
            </div>
          </div>
          <Pill tone={a?.paper === false ? "coral" : "blue"}>{a?.paper === false ? "Live" : "Paper"}</Pill>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Decision panel: calibrated probability, separate confidence, net edge after fees, action, reasons (p.132)
export function DecisionPanel({ onExpand }: { onExpand?: () => void }) {
  const m = useStore((s) => s.market);
  const a = useStore((s) => s.account);
  const est = m?.estimate;
  const up = est?.valid ? num(est.p_yes) : null;
  const cands = a?.candidates || {};
  const best = Object.values(cands).sort((x: any, y: any) => Number(y.ev_per_contract) - Number(x.ev_per_contract))[0] as any;
  const status = !a ? "CONNECTING" : a.controls.entries_paused ? "PAUSED" : a.mode === "PAUSED" ? "HALTED" : best?.decision === "enter" ? "ENTERING" : "EVALUATING";
  const stTone = status === "ENTERING" ? "mint" : status === "EVALUATING" ? "violet" : "amber";
  const reasons: string[] = best?.reasons || m?.market_reasons || [];
  return (
    <Card title={<span className="flex items-center gap-2"><Layers size={16} className="text-violet" />Decision engine</span>} right={<Pill tone={stTone as any}>{status}</Pill>} className="flex flex-col">
      <div className="px-3 pb-2">
        <div className="flex items-end justify-between">
          <div className="text-[13px] font-semibold text-blue">UP <AnimatedNumber value={up} format={(n) => pct(n)} className="text-[24px]" /></div>
          <div className="text-[13px] font-semibold text-coral">DOWN <AnimatedNumber value={up === null ? null : 1 - up} format={(n) => pct(n)} className="text-[24px]" /></div>
        </div>
        <div className="mt-1 h-2 overflow-hidden rounded-full bg-coral/70" aria-label="Settlement probability">
          <div className="h-full bg-blue transition-[width] duration-200" style={{ width: up === null ? "0%" : `${up * 100}%` }} />
        </div>
        <div className="mt-1 text-2xs text-muted">
          {est?.valid ? `Calibrated settlement model · MC ${est.n_paths} paths · band ${pct(est.p_low)}–${pct(est.p_high)} · ±${pct(est.mc_se, 2)} MC error` : `Probability unavailable${est?.reason ? ` (${est.reason})` : ""}`}
        </div>
        <div className="mt-2 grid grid-cols-3 gap-2">
          <MiniStat label="Net edge (cons.)" value={best ? cents(best.ev_per_contract, 1) : DASH} tone={tone(best?.ev_per_contract)} />
          <MiniStat label="Confidence" value={best?.confidence == null ? DASH : `${Number(best.confidence).toFixed(1)}`} sub="evidence, not prob." />
          <MiniStat label="Decision p95" value={m?.latency?.decision_ms?.p95 != null ? `${m.latency.decision_ms.p95.toFixed(1)} ms` : DASH} />
        </div>
        <div className="mt-2 text-[12px]">
          <span className="text-muted">Action: </span>
          <span className="font-semibold">{best ? `${best.decision === "enter" ? "Enter" : "Skip"} ${best.strategy_id} ${best.side?.toUpperCase()}` : "—"}</span>
          {best?.limit_price && <span className="text-muted"> @ {cents(best.limit_price)}</span>}
        </div>
        <div className="mt-1 flex flex-wrap gap-1">
          {reasons.slice(0, 4).map((r) => <Pill key={r} tone="muted">{reason(r)}</Pill>)}
        </div>
        {onExpand && (
          <button onClick={onExpand} className="mt-2 flex min-h-[32px] items-center gap-1 text-[12px] text-blue hover:underline">
            Strategy registry, missed opportunities, diagnostics <ChevronRight size={14} />
          </button>
        )}
      </div>
    </Card>
  );
}

function MiniStat({ label, value, sub, tone: t = "" }: { label: string; value: ReactNode; sub?: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-line bg-surface2 px-2 py-1.5">
      <div className="truncate text-2xs text-muted">{label}</div>
      <div className={`truncate text-[17px] font-semibold tnum ${t}`}>{value}</div>
      {sub && <div className="truncate text-[10px] text-muted">{sub}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Order book: YES bids and YES asks (asks derived from NO bids; p.133). Labels are explicit.
export function OrderBookPanel({ book, title = "Order book (Kalshi)", ticker }: { book?: any; title?: string; ticker?: string | null }) {
  const m = useStore((s) => s.market);
  const b = book ?? m?.book;
  const bids = (b?.yes_bids || []).slice(0, 6);
  const asks = (b?.yes_asks || []).slice(0, 6);
  const max = Math.max(1, ...bids.map((x: any) => Number(x[1])), ...asks.map((x: any) => Number(x[1])));
  const stale = b && !b.valid;
  return (
    <Card title={title} right={<span className="truncate text-2xs text-muted">{ticker ?? m?.ticker ?? DASH}{stale ? " · STALE" : ""}</span>} className="flex min-h-0 flex-col">
      <div className="grid grid-cols-2 gap-3 px-3 pb-2 text-[12px]">
        <BookSide title="YES bids" rows={bids} max={max} color="mint" />
        <BookSide title="YES asks (= 100 − NO bid)" rows={asks} max={max} color="coral" />
      </div>
      {!b?.valid && <div className="px-3 pb-2 text-2xs text-amber">Book unavailable: {b?.stale_reason || "no snapshot yet"} — no quotes are inferred.</div>}
    </Card>
  );
}

function BookSide({ title, rows, max, color }: { title: string; rows: any[]; max: number; color: "mint" | "coral" }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 flex justify-between text-2xs"><span className={color === "mint" ? "text-mint" : "text-coral"}>{title}</span><span className="text-muted">Size</span></div>
      {rows.length === 0 && <div className="py-2 text-muted">{DASH}</div>}
      {rows.map(([p, q]: any) => (
        <div key={p} className="relative flex justify-between py-[3px] tnum">
          <div className={`absolute inset-y-0 right-0 ${color === "mint" ? "bg-mint/15" : "bg-coral/15"}`} style={{ width: `${(Number(q) / max) * 100}%` }} />
          <span className={`relative ${color === "mint" ? "text-mint" : "text-coral"}`}>{cents(p)}</span>
          <span className="relative">{Number(q).toLocaleString()}</span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
export function useCountdown(closeTs: number | null | undefined): number | null {
  const [, tick] = useState(0);
  useEffect(() => { const t = window.setInterval(() => tick((x) => x + 1), 250); return () => clearInterval(t); }, []);
  const s = useStore((st) => st);
  if (!closeTs) return null;
  return closeTs - serverNow(s); // derived from server-synchronized clock (p.134)
}

export function PositionBar({ onActivity }: { onActivity: () => void }) {
  const a = useStore((s) => s.account);
  const m = useStore((s) => s.market);
  const left = useCountdown(m?.close_ts);
  const pos = (a?.positions || [])[0];
  const unrl = pos?.unrealized_at_bid;
  return (
    <div className="grid grid-cols-1 gap-2 lg:grid-cols-[1fr_200px_260px]">
      <Card className="flex items-center gap-3 px-3 py-2">
        <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-blue/10 text-blue"><Layers size={18} /></div>
        {pos ? (
          <div className="grid min-w-0 flex-1 grid-cols-2 gap-2 sm:grid-cols-5">
            <div className="col-span-2 min-w-0 sm:col-span-2"><div className="text-2xs text-muted">Active position</div>
              <div className="truncate text-[13px] font-semibold">{pos.side.toUpperCase()} · {pos.ticker}</div></div>
            <div><div className="text-2xs text-muted">Qty · avg cost</div><div className="tnum text-[13px]">{Number(pos.qty)} @ {cents(pos.avg_cost, 1)}</div></div>
            <div><div className="text-2xs text-muted">Exit bid now</div><div className="tnum text-[13px]">{cents(pos.bid)}</div></div>
            <div><div className="text-2xs text-muted">Unrealized @ bid</div><AnimatedNumber value={unrl} format={(n) => usd(n, true)} className={`text-[13px] font-semibold ${tone(unrl)}`} /></div>
          </div>
        ) : (
          <div className="text-[13px] text-muted">No open position · {a?.positions ? "bot is flat" : "awaiting account state"}</div>
        )}
      </Card>
      <Card className="flex items-center gap-3 px-3 py-2">
        <KIcon name="clock" className="text-amber" />
        <div><div className="text-2xs text-muted">Window closes</div><div className="tnum text-[20px] font-semibold text-amber">{clock(left)}</div></div>
      </Card>
      <button onClick={onActivity} className="flex items-center justify-between gap-2 rounded-card border border-line bg-surface px-3 py-2 text-left hover:border-blue/60">
        <span className="flex items-center gap-2 text-[13px] font-semibold"><Activity size={16} className="text-cyan" />Bot activity</span>
        <span className="flex items-center gap-2"><span className="truncate text-2xs text-muted">{a?.decisions?.[0] ? reason(a.decisions[0].reason_codes?.[0] || a.decisions[0].action) : DASH}</span><ChevronDown size={16} className="-rotate-90" /></span>
      </button>
    </div>
  );
}

export function StatusBar() {
  const m = useStore((s) => s.market);
  const a = useStore((s) => s.account);
  const conn = useStore((s) => s.connected);
  const feeds = m?.feeds || {};
  const ok = (v?: string) => (v === "ok" ? "text-mint" : v ? "text-amber" : "text-muted");
  const blockers: string[] = m?.health?.entry_blockers || [];
  return (
    <footer className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line px-4 py-1.5 text-2xs text-muted">
      <span className={conn === "live" ? "text-mint" : "text-amber"}>● Server {conn}</span>
      <span>Kalshi: <b className={ok(feeds.kalshi)}>{feeds.kalshi || DASH}</b>{feeds.kalshi_ws ? ` · WS ${feeds.kalshi_ws}` : " · REST polling"}</span>
      <span>Coinbase: <b className={ok(feeds.coinbase)}>{feeds.coinbase || DASH}</b></span>
      <span>Kraken: <b className={ok(feeds.kraken)}>{feeds.kraken || DASH}</b></span>
      <span>Risk: <b className={a?.halts?.length ? "text-amber" : "text-mint"}>{a?.halts?.length ? a.halts.map((h: any) => h.code).join(", ") : "active, no halts"}</b></span>
      <span>Entries: <b className={blockers.length ? "text-amber" : "text-mint"}>{blockers.length ? blockers.map(reason).join(", ") : "data OK"}</b></span>
      <span className="ml-auto">Mode: {a?.paper === false ? "LIVE" : "PAPER"} · policy {a?.risk?.policy_fingerprint || DASH}</span>
    </footer>
  );
}

export function DecisionsList({ rows }: { rows: any[] }) {
  return (
    <div className="divide-y divide-line">
      {rows.length === 0 && <div className="py-6 text-center text-muted">No decisions recorded yet.</div>}
      {rows.map((d, i) => (
        <div key={i} className="grid grid-cols-[64px_1fr_auto] gap-2 py-1.5 text-[12px]">
          <span className="tnum text-muted">{timeOf(d.ts)}</span>
          <span className="min-w-0 truncate">{d.side && d.side !== "-" ? `${d.side.toUpperCase()} · ` : ""}{d.strategy_id} · {(d.reason_codes || []).map(reason).join(", ")}</span>
          <span className={d.action === "enter" ? "text-mint" : d.action === "exit" ? "text-cyan" : "text-blue"}>{d.action}{d.qty ? ` ${d.qty}` : ""}</span>
        </div>
      ))}
    </div>
  );
}
