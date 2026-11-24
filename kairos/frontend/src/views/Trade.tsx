import { Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { ChartMode, MarketChart } from "../components/MarketChart";
import { OrderBookPanel } from "../components/panels";
import { Btn, Card, Drawer, Pill, Row } from "../components/ui";
import { DASH, cents, clock, timeOf, usd } from "../format";
import { serverNow, useStore } from "../state/store";
import { ChartToolbar, OverlaySheet } from "./Markets";

// Manual Kalshi orders (spec p.135): explicit, user-initiated, reviewed, server-side submission.
export function TradeView({ mobile }: { mobile: boolean }) {
  const market = useStore((s) => s.market);
  const account = useStore((s) => s.account);
  const [list, setList] = useState<any[]>([]);
  const [filter, setFilter] = useState("");
  const [ticker, setTicker] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [err, setErr] = useState("");
  const [mode, setMode] = useState<ChartMode>("yes");
  const [tf, setTf] = useState("1m");
  const [sheet, setSheet] = useState(false);
  const [overlays, setOverlays] = useState(false);
  const active = ticker ?? market?.ticker ?? null;

  useEffect(() => {
    api("/api/trade/markets").then((d) => setList(d.markets)).catch((e) => setErr(e.message));
  }, []);
  useEffect(() => {
    if (!active) return;
    let stop = false;
    const load = () => api(`/api/trade/book/${encodeURIComponent(active)}`).then((d) => !stop && setDetail(d)).catch((e) => !stop && setErr(e.message));
    load();
    const t = window.setInterval(load, 2000);
    return () => { stop = true; clearInterval(t); };
  }, [active]);

  const shown = useMemo(() => list.filter((m) => !filter || `${m.ticker} ${m.title}`.toLowerCase().includes(filter.toLowerCase())).slice(0, 60), [list, filter]);
  const isActive = active === market?.ticker;
  const ticket = <OrderTicket ticker={active} detail={detail} supported={!!detail?.market?.model_supported} onDone={() => setSheet(false)} />;

  const picker = (
    <Card title="Markets" right={<span className="text-2xs text-muted">{list.length} open</span>} className="flex min-h-0 flex-col">
      <div className="px-3 pb-2">
        <label className="flex min-h-[36px] items-center gap-2 rounded-lg border border-line bg-surface2 px-2">
          <Search size={14} className="text-muted" />
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Search ticker or title" className="w-full bg-transparent text-[13px] outline-none" />
        </label>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-1 pb-2">
        {err && <div className="px-2 text-2xs text-amber">{err}</div>}
        {shown.map((m) => (
          <button key={m.ticker} onClick={() => { setTicker(m.ticker); if (mobile) setSheet(false); }}
            className={`flex w-full min-h-[44px] items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-surface2 ${m.ticker === active ? "bg-blue/10" : ""}`}>
            <span className="min-w-0"><span className="block truncate text-[12px] font-semibold">{m.title || m.ticker}</span>
              <span className="block truncate text-2xs text-muted">{m.ticker}{m.model_supported ? " · model supported" : ""}</span></span>
            <span className="tnum text-right text-2xs"><span className="text-mint">{cents(m.yes_bid)}</span> / <span className="text-coral">{cents(m.yes_ask)}</span></span>
          </button>
        ))}
      </div>
    </Card>
  );

  const chart = (
    <Card className="flex min-h-0 flex-col">
      {isActive ? <ChartToolbar mode={mode} setMode={setMode} tf={tf} setTf={setTf} onOverlays={() => setOverlays(true)} />
        : <div className="px-3 pt-2.5 text-[15px] font-semibold">{detail?.market?.title || active || DASH}</div>}
      <div className="min-h-[260px] flex-1 px-1 pb-1">
        {isActive ? <MarketChart mode={mode} tf={tf} /> : (
          <div className="grid h-full place-items-center p-4 text-center text-[13px] text-muted">
            Live chart history is recorded for the active BTC window. For other markets the official book is shown at right and refreshes every 2 s.
          </div>
        )}
      </div>
    </Card>
  );

  const bottom = <OrdersDrawerContent rows={account?.recent_orders || []} />;

  if (mobile) {
    return (
      <div className="space-y-2 p-2">
        {picker}
        {chart}
        <OrderBookPanel book={detail?.book} ticker={active} />
        <Btn className="w-full" onClick={() => setSheet(true)} disabled={!active}>Open order ticket</Btn>
        <Card title="My orders">{bottom}</Card>
        <BottomSheet open={sheet} onClose={() => setSheet(false)} title="Order ticket">{ticket}</BottomSheet>
        <Drawer open={overlays} onClose={() => setOverlays(false)} title="Chart overlays"><OverlaySheet onClose={() => setOverlays(false)} /></Drawer>
      </div>
    );
  }
  return (
    <div className="grid h-full min-h-0 grid-rows-[minmax(0,1fr)_200px] gap-2 p-2">
      <div className="grid min-h-0 grid-cols-[260px_minmax(0,1fr)_340px] gap-2">
        {picker}
        {chart}
        <div className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-2">
          <Card title="Order ticket">{ticket}</Card>
          <OrderBookPanel book={detail?.book} ticker={active} />
        </div>
      </div>
      <Card title="Positions & order history" className="min-h-0 overflow-y-auto">{bottom}</Card>
      <Drawer open={overlays} onClose={() => setOverlays(false)} title="Chart overlays"><OverlaySheet onClose={() => setOverlays(false)} /></Drawer>
    </div>
  );
}

function OrderTicket({ ticker, detail, supported, onDone }: { ticker: string | null; detail: any; supported: boolean; onDone: () => void }) {
  const s = useStore((st) => st);
  const [side, setSide] = useState<"yes" | "no">("yes");
  const [action, setAction] = useState<"buy" | "sell">("buy");
  const [qty, setQty] = useState(1);
  const [limit, setLimit] = useState("");
  const [preview, setPreview] = useState<any>(null);
  const [review, setReview] = useState(false);
  const [msg, setMsg] = useState("");
  const book = detail?.book;
  const bestAsk = side === "yes" ? book?.yes_asks?.[0]?.[0] : book?.no_asks?.[0]?.[0];
  const bestBid = side === "yes" ? book?.yes_bids?.[0]?.[0] : book?.no_bids?.[0]?.[0];
  useEffect(() => { setPreview(null); setReview(false); }, [ticker, side, action, qty, limit]);
  const closeTs = detail?.market?.close_ts;
  const body = { ticker, side, action, quantity: qty, limit_price: limit || String(action === "buy" ? bestAsk ?? "" : bestBid ?? "") };
  const doPreview = async () => {
    setMsg("");
    try { setPreview(await api("/api/trade/preview", { body })); setReview(true); } catch (e: any) { setMsg(e.message); }
  };
  const submit = async () => {
    try {
      const r = await api("/api/trade/order", { body: { ...body, confirm: true } });
      setMsg(r.ok ? `Order ${r.order?.state}: filled ${r.order?.filled} of ${r.order?.quantity}` : `Rejected: ${(r.reasons || []).join(", ")}`);
      setReview(false);
      if (r.ok) onDone();
    } catch (e: any) { setMsg(e.message); }
  };
  if (!ticker) return <div className="p-3 text-muted">Select a market.</div>;
  return (
    <div className="space-y-2 px-3 pb-3">
      <div className="truncate text-[12px] text-muted">{detail?.market?.title || ticker}</div>
      <div className="text-2xs text-muted">Ticker <b className="text-text">{ticker}</b> · expires {timeOf(closeTs)} ({clock(closeTs ? closeTs - serverNow(s) : null)})</div>
      <div className="text-2xs">{supported ? <Pill tone="violet">Model analysis available</Pill> : <Pill tone="muted">Model analysis unavailable for this market</Pill>}</div>
      <div className="grid grid-cols-2 gap-1">
        {(["buy", "sell"] as const).map((a) => <button key={a} onClick={() => setAction(a)} className={`min-h-[40px] rounded-lg border text-[13px] font-semibold ${action === a ? "border-blue bg-blue/15 text-blue" : "border-line text-muted"}`}>{a.toUpperCase()}</button>)}
        {(["yes", "no"] as const).map((x) => <button key={x} onClick={() => setSide(x)} className={`min-h-[40px] rounded-lg border text-[13px] font-semibold ${side === x ? (x === "yes" ? "border-mint bg-mint/10 text-mint" : "border-coral bg-coral/10 text-coral") : "border-line text-muted"}`}>{x.toUpperCase()}</button>)}
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="text-2xs text-muted">Quantity<input type="number" min={1} value={qty} onChange={(e) => setQty(Math.max(1, Math.floor(Number(e.target.value) || 1)))}
          className="mt-1 h-10 w-full rounded-lg border border-line bg-surface2 px-2 text-[14px] text-text tnum" /></label>
        <label className="text-2xs text-muted">Limit ($, 0.01–0.99)<input inputMode="decimal" placeholder={String(action === "buy" ? bestAsk ?? "" : bestBid ?? "")} value={limit} onChange={(e) => setLimit(e.target.value)}
          className="mt-1 h-10 w-full rounded-lg border border-line bg-surface2 px-2 text-[14px] text-text tnum" /></label>
      </div>
      <Row k="Best bid / ask" v={`${cents(bestBid)} / ${cents(bestAsk)}`} />
      {!review && <Btn className="w-full" onClick={doPreview}>Review order</Btn>}
      {review && preview && (
        <div className="space-y-1 rounded-lg border border-blue/40 bg-blue/5 p-2">
          <div className="text-[12px] font-semibold">Review — {action.toUpperCase()} {qty} {side.toUpperCase()} @ ≤ {cents(body.limit_price)}</div>
          <Row k="Fillable now" v={preview.fillable_now} />
          <Row k="Avg price" v={cents(preview.vwap, 1)} />
          <Row k="Estimated fee" v={usd(preview.est_fee)} />
          {action === "buy" ? (<><Row k="Maximum loss" v={usd(preview.max_loss)} vc="text-coral" /><Row k="Payout if correct" v={usd(preview.payout_if_win)} /><Row k="Net if correct" v={usd(preview.net_if_win, true)} vc="text-mint" /></>)
            : <Row k="Proceeds" v={usd(preview.proceeds)} />}
          <div className="text-[10px] text-muted">{preview.note} Fee schedule: {preview.fee_schedule?.type} ({preview.fee_schedule?.source}). Paper/live per account mode.</div>
          <div className="grid grid-cols-2 gap-2 pt-1"><Btn tone="ghost" onClick={() => setReview(false)}>Edit</Btn><Btn tone="mint" onClick={submit}>Confirm order</Btn></div>
        </div>
      )}
      {msg && <div className="text-[12px] text-amber">{msg}</div>}
    </div>
  );
}

export function OrdersDrawerContent({ rows }: { rows: any[] }) {
  if (!rows.length) return <div className="p-4 text-center text-muted">No orders yet.</div>;
  return (
    <div className="overflow-x-auto px-2 pb-2">
      <table className="w-full text-[12px] tnum">
        <thead className="text-left text-2xs text-muted"><tr><th className="py-1">Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Limit</th><th>Filled</th><th>Avg</th><th>Fees</th><th>State</th><th>Origin</th></tr></thead>
        <tbody>{rows.map((o) => (
          <tr key={o.client_order_id} className="border-t border-line">
            <td className="py-1">{timeOf(o.created_ts)}</td><td className="max-w-[160px] truncate">{o.ticker}</td><td>{o.action} {o.side}</td><td>{o.quantity}</td>
            <td>{cents(o.limit)}</td><td>{o.filled}</td><td>{cents(o.avg_price, 1)}</td><td>{usd(o.fees)}</td>
            <td><Pill tone={o.state === "filled" ? "mint" : o.state === "rejected" || o.state === "unknown" ? "coral" : o.state === "partial" ? "amber" : "muted"}>{o.state}</Pill></td>
            <td>{o.origin}{o.paper ? " · paper" : ""}</td>
          </tr>))}
        </tbody>
      </table>
    </div>
  );
}

function BottomSheet({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: React.ReactNode }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex items-end bg-black/50" onClick={onClose} role="dialog" aria-modal aria-label={title}>
      <div onClick={(e) => e.stopPropagation()} className="safe-b max-h-[88dvh] w-full overflow-y-auto rounded-t-2xl border-t border-line bg-surface">
        <div className="flex items-center justify-between px-4 py-2"><span className="text-[15px] font-semibold">{title}</span>
          <button onClick={onClose} className="grid h-11 w-11 place-items-center text-muted" aria-label="Close">✕</button></div>
        {children}
      </div>
    </div>
  );
}
