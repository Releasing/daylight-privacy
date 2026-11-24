import { ColorType, CrosshairMode, IChartApi, ISeriesApi, LineStyle, SeriesMarker, Time, UTCTimestamp, createChart } from "lightweight-charts";
import { useEffect, useMemo, useRef } from "react";
import { cents, price, timeOf } from "../format";
import { usePrefs } from "../state/prefs";
import { useStore } from "../state/store";

// One primary chart with selectable contract (YES/NO, cents) and underlying BTC reference ($) views (spec p.123, p.132).
// Units are never mixed on one axis. Overlays are display-only and never feed the model (p.125).
export type ChartMode = "yes" | "no" | "btc";
const TF_MIN: Record<string, number> = { "1m": 1, "5m": 5, "15m": 15, "1h": 60 };
const C = { blue: "#32A8FF", mint: "#38D9A9", coral: "#FF657D", cyan: "#67E8F9", violet: "#A78BFA", amber: "#FBBF24", muted: "#8EA2BA", line: "#223249" };

function aggregate(bars: any[], mins: number) {
  if (mins <= 1) return bars;
  const out: any[] = [];
  for (const b of bars) {
    const t = Math.floor(b.t / (mins * 60)) * mins * 60;
    const last = out[out.length - 1];
    if (last && last.t === t) { last.h = Math.max(last.h, b.h); last.l = Math.min(last.l, b.l); last.c = b.c; last.v += b.v; last.p = b.p; }
    else out.push({ ...b, t });
  }
  return out;
}

function ema(values: number[], n: number): (number | null)[] {
  const k = 2 / (n + 1); let e: number | null = null; const out: (number | null)[] = []; let seed: number[] = [];
  for (const v of values) {
    if (e === null) { seed.push(v); if (seed.length === n) { e = seed.reduce((a, b) => a + b, 0) / n; seed = []; } out.push(e); }
    else { e = k * v + (1 - k) * e; out.push(e); }
  }
  return out;
}

export function MarketChart({ mode, tf, height }: { mode: ChartMode; tf: string; height?: number }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<Record<string, ISeriesApi<any>>>({});
  const lines = useRef<any[]>([]);
  const chartData = useStore((s) => s.chart);
  const market = useStore((s) => s.market);
  const account = useStore((s) => s.account);
  const fills = useStore((s) => s.fills);
  const [prefs] = usePrefs();

  useEffect(() => {
    if (!host.current) return;
    const c = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: C.muted, fontFamily: "Inter, system-ui, sans-serif", fontSize: 11 },
      grid: { vertLines: { color: "rgba(34,50,73,0.35)" }, horzLines: { color: "rgba(34,50,73,0.35)" } },
      rightPriceScale: { borderColor: C.line },
      timeScale: { borderColor: C.line, timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      handleScale: { pinch: true, mouseWheel: true, axisPressedMouseMove: true },
      handleScroll: { pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    });
    chart.current = c;
    return () => { c.remove(); chart.current = null; series.current = {}; };
  }, []);

  // (Re)build series when the mode changes
  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    Object.values(series.current).forEach((s) => c.removeSeries(s));
    series.current = {};
    if (mode === "btc") {
      series.current.candles = c.addCandlestickSeries({ upColor: C.mint, downColor: C.coral, borderVisible: false, wickUpColor: C.mint, wickDownColor: C.coral,
        priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
      series.current.vol = c.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
      c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      series.current.ema9 = c.addLineSeries({ color: C.cyan, lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
      series.current.ema21 = c.addLineSeries({ color: C.violet, lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
    } else {
      const fmt = { type: "custom" as const, formatter: (p: number) => `${p.toFixed(0)}¢`, minMove: 1 };
      series.current.ask = c.addLineSeries({ color: C.coral, lineWidth: 1, lineStyle: LineStyle.Solid, priceFormat: fmt, title: "ask" });
      series.current.bid = c.addLineSeries({ color: C.mint, lineWidth: 2, priceFormat: fmt, title: "bid" });
      series.current.last = c.addLineSeries({ color: C.blue, lineWidth: 1, lineStyle: LineStyle.Dotted, priceFormat: fmt, lastValueVisible: false, priceLineVisible: false });
    }
  }, [mode]);

  const btcBars = useMemo(() => aggregate((chartData?.btc || []).map((b: any) => ({ ...b })), TF_MIN[tf] || 1), [chartData, tf]);

  // Data + overlays
  useEffect(() => {
    const c = chart.current;
    if (!c || !chartData) return;
    const s = series.current;
    lines.current.forEach(([ser, l]) => { try { ser.removePriceLine(l); } catch { /* series replaced */ } });
    lines.current = [];
    const addLine = (ser: ISeriesApi<any>, p: number, color: string, title: string, style = LineStyle.Dashed, width: 1 | 2 = 1) => {
      lines.current.push([ser, ser.createPriceLine({ price: p, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title })]);
    };
    if (mode === "btc" && s.candles) {
      const bars = btcBars.filter((b: any) => Number.isFinite(b.o));
      s.candles.setData(bars.map((b: any) => ({ time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c })));
      s.vol.setData(prefs.volume ? bars.map((b: any) => ({ time: b.t as UTCTimestamp, value: b.v, color: b.c >= b.o ? "rgba(56,217,169,0.35)" : "rgba(255,101,125,0.35)" })) : []);
      const closes = bars.map((b: any) => b.c);
      const e9 = ema(closes, 9), e21 = ema(closes, 21);
      s.ema9.setData(prefs.ema ? bars.map((b: any, i: number) => (e9[i] === null ? null : { time: b.t as UTCTimestamp, value: e9[i] })).filter(Boolean) as any : []);
      s.ema21.setData(prefs.ema ? bars.map((b: any, i: number) => (e21[i] === null ? null : { time: b.t as UTCTimestamp, value: e21[i] })).filter(Boolean) as any : []);
      if (chartData.strike && prefs.strike) addLine(s.candles, chartData.strike, C.blue, "Strike", LineStyle.Dashed, 2);
      if (prefs.levels) {
        (chartData.levels?.support || []).forEach((l: any, i: number) => {
          addLine(s.candles, l.center, C.mint, `S${i + 1}`, LineStyle.Dashed);
          if (prefs.zones) { addLine(s.candles, l.lower, "rgba(56,217,169,0.35)", "", LineStyle.Dotted); addLine(s.candles, l.upper, "rgba(56,217,169,0.35)", "", LineStyle.Dotted); }
        });
        (chartData.levels?.resistance || []).forEach((l: any, i: number) => {
          addLine(s.candles, l.center, C.coral, `R${i + 1}`, LineStyle.Dashed);
          if (prefs.zones) { addLine(s.candles, l.lower, "rgba(255,101,125,0.35)", "", LineStyle.Dotted); addLine(s.candles, l.upper, "rgba(255,101,125,0.35)", "", LineStyle.Dotted); }
        });
      }
      const pm: SeriesMarker<Time>[] = prefs.patterns ? (chartData.patterns || []).filter((p: any) => p.at).map((p: any) => ({
        time: (Math.floor(p.at / ((TF_MIN[tf] || 1) * 60)) * (TF_MIN[tf] || 1) * 60) as UTCTimestamp,
        position: p.kind.includes("break") && !p.kind.includes("down") ? "aboveBar" : "belowBar",
        color: p.kind === "false_breakout" ? C.amber : p.kind.includes("bounce") || p.kind === "breakout" ? C.mint : C.coral,
        shape: "circle", text: p.kind.replace("_", " "),
      })) : [];
      s.candles.setMarkers(pm.sort((a, b) => (a.time as number) - (b.time as number)));
    } else if (s.bid) {
      const pts = (chartData.contract || []) as any[];
      const flip = mode === "no";
      const conv = (bid: number | null, ask: number | null) => flip ? [ask === null ? null : 100 - ask * 100, bid === null ? null : 100 - bid * 100] : [bid === null ? null : bid * 100, ask === null ? null : ask * 100];
      const bids: any[] = [], asks: any[] = [];
      for (const p of pts) {
        const [b, a] = conv(p.bid, p.ask);
        if (b !== null) bids.push({ time: p.t as UTCTimestamp, value: b });
        if (a !== null) asks.push({ time: p.t as UTCTimestamp, value: a });
      }
      s.bid.setData(bids);
      s.ask.setData(asks);
      const trades = (chartData.contract_trades || []).map((t: any) => ({ time: Math.floor(t.t) as UTCTimestamp, value: flip ? 100 - t.p * 100 : t.p * 100 }));
      const dedup = new Map<number, any>(); trades.forEach((t: any) => dedup.set(t.time, t));
      s.last.setData([...dedup.values()].sort((a, b) => a.time - b.time));
      const side = mode;
      const pos = (account?.positions || []).find((p: any) => p.ticker === chartData.ticker && p.side === side);
      if (pos && prefs.entries && pos.avg_cost) addLine(s.bid, Number(pos.avg_cost) * 100, C.blue, "Avg entry", LineStyle.Dashed, 2);
      if (pos && prefs.targets && account?.candidates?.[`scalp:${side}`]?.target && pos.strategy === "scalp") {
        addLine(s.bid, Number(account.candidates[`scalp:${side}`].target) * 100, C.amber, "Target", LineStyle.Dotted);
      }
      const myFills: SeriesMarker<Time>[] = prefs.fills ? fills.filter((f: any) => f.ticker === chartData.ticker && f.side === side).map((f: any) => ({
        time: Math.floor(f.ts) as UTCTimestamp, position: f.action === "buy" ? "belowBar" : "aboveBar", color: f.action === "buy" ? C.mint : C.coral,
        shape: f.action === "buy" ? "arrowUp" : "arrowDown", text: `${f.action} ${Number(f.qty)}@${(Number(f.price) * 100).toFixed(0)}¢`,
      })) : [];
      s.bid.setMarkers(myFills.sort((a, b) => (a.time as number) - (b.time as number)));
    }
  }, [chartData, mode, btcBars, prefs, account, fills, tf]);

  const src = mode === "btc"
    ? `BTC/USD ${tf} · ${chartData?.btc_source?.venues?.join("+") || "no venue"} median (exchange proxy, not CF Benchmarks index)`
    : `${chartData?.ticker || "—"} · Kalshi ${mode.toUpperCase()} executable bid/ask (¢)`;
  const lastQuote = market?.quotes;
  const empty = mode === "btc" ? !btcBars.length : !(chartData?.contract || []).length;
  return (
    <div className="relative h-full w-full" style={height ? { height } : undefined}>
      <div ref={host} className="absolute inset-0" />
      {empty && (
        <div className="pointer-events-none absolute inset-0 grid place-items-center text-center text-[13px] text-muted">
          <div>{mode === "btc" ? "Waiting for BTC trade data from exchange feeds" : "Waiting for Kalshi contract data"}<br />
            <span className="text-2xs">No values are shown until verified data arrives.</span></div>
        </div>
      )}
      <div className="pointer-events-none absolute bottom-1 left-2 text-2xs text-muted/90">
        {src} · updated {timeOf(market?.server_time)}
        {mode !== "btc" && lastQuote ? ` · last ${cents(lastQuote.last)}` : ""}
        {mode === "btc" && chartData?.strike ? ` · strike ${price(chartData.strike)}` : ""}
      </div>
    </div>
  );
}
