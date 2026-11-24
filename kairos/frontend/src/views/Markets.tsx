import { ChevronDown, Maximize2, SlidersHorizontal } from "lucide-react";
import { ReactNode, useState } from "react";
import { ChartMode, MarketChart } from "../components/MarketChart";
import { DecisionPanel, KpiStrip, OrderBookPanel, PositionBar, useCountdown } from "../components/panels";
import { Card, Pill } from "../components/ui";
import { cents, clock, price } from "../format";
import { usePrefs } from "../state/prefs";
import { useStore } from "../state/store";

export function ChartToolbar({ mode, setMode, tf, setTf, onOverlays, onFull }: { mode: ChartMode; setMode: (m: ChartMode) => void; tf: string; setTf: (t: string) => void; onOverlays: () => void; onFull?: () => void }) {
  const m = useStore((s) => s.market);
  const q = m?.quotes;
  return (
    <div className="flex flex-wrap items-center gap-2 px-3 pt-2.5">
      <div className="mr-auto flex min-w-0 items-center gap-2">
        {mode === "btc" ? (
          <>
            <span className="grid h-7 w-7 place-items-center rounded-full bg-btc text-[13px] font-bold text-midnight">₿</span>
            <span className="text-[15px] font-semibold">BTC / USD</span>
            <span className="tnum text-[18px] font-semibold">{price(m?.spot)}</span>
            <Pill tone="muted">exchange proxy</Pill>
          </>
        ) : (
          <>
            <span className="truncate text-[15px] font-semibold">{m?.ticker || "No active window"}</span>
            <span className="tnum text-[13px] text-muted">{mode.toUpperCase()} bid <b className="text-mint">{cents(mode === "yes" ? q?.yes_bid : q?.no_bid)}</b> · ask <b className="text-coral">{cents(mode === "yes" ? q?.yes_ask : q?.no_ask)}</b></span>
          </>
        )}
      </div>
      <div className="flex overflow-hidden rounded-lg border border-line text-[12px]" role="tablist" aria-label="Chart view">
        {(["yes", "no", "btc"] as ChartMode[]).map((x) => (
          <button key={x} role="tab" aria-selected={mode === x} onClick={() => setMode(x)}
            className={`min-h-[32px] px-2.5 font-semibold ${mode === x ? "bg-blue text-midnight" : "text-muted hover:text-text"}`}>{x === "btc" ? "BTC ref" : `${x.toUpperCase()} ¢`}</button>
        ))}
      </div>
      {mode === "btc" && (
        <div className="flex overflow-hidden rounded-lg border border-line text-[12px]" aria-label="Timeframe">
          {["1m", "5m", "15m", "1h"].map((t) => (
            <button key={t} onClick={() => setTf(t)} className={`min-h-[32px] px-2 font-semibold ${tf === t ? "bg-blue/20 text-blue" : "text-muted hover:text-text"}`}>{t.toUpperCase()}</button>
          ))}
        </div>
      )}
      <button onClick={onOverlays} aria-label="Chart overlays" className="grid h-8 w-8 place-items-center rounded-lg border border-line text-muted hover:text-text"><SlidersHorizontal size={15} /></button>
      {onFull && <button onClick={onFull} aria-label="Expand chart" className="grid h-8 w-8 place-items-center rounded-lg border border-line text-muted hover:text-text"><Maximize2 size={15} /></button>}
    </div>
  );
}

export function OverlaySheet({ onClose }: { onClose: () => void }) {
  const [p, set] = usePrefs();
  const items: [keyof typeof p, string][] = [["levels", "Support / resistance (S1/S2, R1/R2)"], ["zones", "Level zones"], ["strike", "Settlement strike (price to beat)"],
    ["ema", "EMA 9 / 21"], ["volume", "Volume"], ["patterns", "Breakout / retest markers"], ["fills", "My fill markers"], ["entries", "Average entry"], ["targets", "Dynamic targets"]];
  return (
    <div className="space-y-1">
      <p className="mb-2 text-2xs text-muted">Display only — hiding an overlay never changes model inputs or decisions.</p>
      {items.map(([k, label]) => (
        <label key={k} className="flex min-h-[44px] items-center justify-between rounded-lg border border-line bg-surface2 px-3">
          <span className="text-[13px]">{label}</span>
          <input type="checkbox" className="h-5 w-5 accent-[#32A8FF]" checked={p[k]} onChange={(e) => set({ [k]: e.target.checked })} />
        </label>
      ))}
    </div>
  );
}

export function MarketsDesktop({ openDrawer }: { openDrawer: (d: string) => void }) {
  const [mode, setMode] = useState<ChartMode>("yes");
  const [tf, setTf] = useState("1m");
  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)_auto] gap-2 p-2">
      <KpiStrip onFunds={() => openDrawer("funds")} />
      <div className="grid min-h-0 grid-cols-[minmax(0,65fr)_minmax(320px,35fr)] gap-2">
        <Card className="flex min-h-0 flex-col">
          <ChartToolbar mode={mode} setMode={setMode} tf={tf} setTf={setTf} onOverlays={() => openDrawer("overlays")} />
          <div className="min-h-0 flex-1 px-1 pb-1"><MarketChart mode={mode} tf={tf} /></div>
        </Card>
        <div className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-2">
          <DecisionPanel onExpand={() => openDrawer("research")} />
          <OrderBookPanel />
        </div>
      </div>
      <PositionBar onActivity={() => openDrawer("activity")} />
    </div>
  );
}

function Section({ title, children, defaultOpen = true, right }: { title: string; children: ReactNode; defaultOpen?: boolean; right?: ReactNode }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="rounded-card border border-line bg-surface">
      <button onClick={() => setOpen(!open)} aria-expanded={open} className="flex min-h-[44px] w-full items-center justify-between px-3 text-left">
        <span className="text-[13px] font-semibold">{title}</span>
        <span className="flex items-center gap-2">{right}<ChevronDown size={16} className={`transition-transform ${open ? "" : "-rotate-90"}`} /></span>
      </button>
      {open && <div className="pb-2">{children}</div>}
    </section>
  );
}

export function MarketsMobile({ openDrawer }: { openDrawer: (d: string) => void }) {
  const [mode, setMode] = useState<ChartMode>("yes");
  const [tf, setTf] = useState("1m");
  const m = useStore((s) => s.market);
  const left = useCountdown(m?.close_ts);
  return (
    <div className="space-y-2 p-2">
      <KpiStrip compact onFunds={() => openDrawer("funds")} />
      <Section title="Chart" right={<span className="tnum text-[12px] text-amber">{clock(left)} left</span>}>
        <ChartToolbar mode={mode} setMode={setMode} tf={tf} setTf={setTf} onOverlays={() => openDrawer("overlays")} />
        <div className="h-[300px] px-1"><MarketChart mode={mode} tf={tf} /></div>
      </Section>
      <DecisionPanel onExpand={() => openDrawer("research")} />
      <Section title="Order book" defaultOpen={false}><OrderBookPanel title="" /></Section>
      <PositionBar onActivity={() => openDrawer("activity")} />
    </div>
  );
}
