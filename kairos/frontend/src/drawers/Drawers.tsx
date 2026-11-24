import { useEffect, useState } from "react";
import { api } from "../api";
import { DecisionsList } from "../components/panels";
import { Btn, Card, Pill, Row } from "../components/ui";
import { DASH, num, pct, reason, timeOf, usd } from "../format";
import { useStore } from "../state/store";
import { OrdersDrawerContent } from "../views/Trade";

export function ActivityDrawer() {
  const a = useStore((s) => s.account);
  const [tab, setTab] = useState<"all" | "trades" | "skipped" | "orders">("all");
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => {
    if (tab === "orders") return;
    api(`/api/decisions${tab === "all" ? "" : `?kind=${tab}`}`).then((d) => setRows(d.decisions.map((x: any) => ({ ...x, reason_codes: x.reasons, strategy_id: x.strategy }))));
  }, [tab, a?.decisions?.length]);
  return (
    <div>
      <div className="mb-2 flex gap-1">{(["all", "trades", "skipped", "orders"] as const).map((t) => (
        <button key={t} onClick={() => setTab(t)} className={`min-h-[36px] rounded-lg px-3 text-[12px] font-semibold ${tab === t ? "bg-blue/20 text-blue" : "text-muted"}`}>{t === "skipped" ? "Missed / skipped" : t[0].toUpperCase() + t.slice(1)}</button>))}</div>
      {tab === "orders" ? <OrdersDrawerContent rows={a?.recent_orders || []} /> : <DecisionsList rows={rows} />}
    </div>
  );
}

export function FundsDrawer() {
  const a = useStore((s) => s.account);
  const f = a?.funds;
  return (
    <div className="space-y-3">
      <Card title="Buckets" className="px-3 pb-2">
        <Row k="Active trading capital" v={usd(f?.active)} />
        <Row k="Reserved for open orders" v={usd(a?.reserved_orders)} />
        <Row k="Spendable now" v={usd(a?.spendable)} />
        <Row k="Protected savings" v={usd(f?.savings)} vc="text-blue" />
        <Row k="Restricted reserve" v={usd(f?.reserve)} vc="text-amber" />
        <Row k="Total (internal ledger)" v={usd(f?.total)} />
        <p className="pt-1 text-2xs text-muted">Savings and reserve are internal restrictions on this {a?.paper === false ? "Kalshi account" : "paper ledger"} — not separate bank funds. They are never used to size or fund orders.</p>
      </Card>
      <Card title="Profit policy" className="px-3 pb-2">
        <Row k="Current distribution policy" v={f?.policy || DASH} />
        <Row k="Cumulative realized net P&L" v={usd(f?.cumulative_realized, true)} />
        <Row k="High-water mark" v={usd(f?.high_water)} />
        <Row k="Distributions frozen" v={f?.frozen ? "yes (correction pending)" : "no"} />
        <p className="pt-1 text-2xs text-muted">Only realized, reconciled profit above the high-water mark is distributed. Unrealized marks never count.</p>
      </Card>
      <Card title="Risk headroom" className="px-3 pb-2">
        <Row k="Daily loss limit" v={usd(a?.risk?.daily_loss_limit)} />
        <Row k="Realized today" v={usd(a?.risk?.realized_today, true)} />
        <Row k="Loss headroom" v={usd(a?.risk?.loss_headroom)} />
        <Row k="Open worst-case exposure" v={usd(a?.risk?.portfolio_exposure)} />
        <Row k="Unrealized at executable bids" v={usd(a?.unrealized_at_bid, true)} />
      </Card>
    </div>
  );
}

export function ResearchDrawer() {
  const a = useStore((s) => s.account);
  const [r, setR] = useState<any>(null);
  useEffect(() => { api("/api/research").then(setR); }, []);
  const reg = a?.registry || {};
  const cands = a?.candidates || {};
  return (
    <div className="space-y-3">
      <p className="text-2xs text-amber">{r?.disclaimer || "Simulated results are not realized earnings."}</p>
      <Card title="Current candidates (both sides, all strategies)" className="px-3 pb-2">
        {Object.keys(cands).length === 0 && <div className="py-2 text-muted">No candidates evaluated yet.</div>}
        {Object.entries(cands).map(([k, c]: any) => (
          <div key={k} className="border-t border-line py-1.5 text-[12px]">
            <div className="flex justify-between"><b>{k}</b><Pill tone={c.decision === "enter" ? "mint" : "muted"}>{c.decision}</Pill></div>
            <div className="tnum text-muted">p {pct(c.p_point)} · cons. {pct(c.p_conservative)} · EV/contract {usd(c.ev_per_contract, true)} · conf {c.confidence == null ? DASH : Number(c.confidence).toFixed(1)} · kelly {pct(c.kelly_fraction)} · cap {usd(c.size_cap)}</div>
            <div className="text-muted">{(c.reasons || []).map(reason).join(" · ")}</div>
          </div>
        ))}
      </Card>
      <Card title="Strategy registry (edge lifecycle)" className="px-3 pb-2">
        {Object.values(reg).map((e: any) => (
          <div key={e.strategy} className="flex items-center justify-between border-t border-line py-1.5 text-[12px]">
            <span>{e.strategy} <span className="text-muted">{e.version}</span></span>
            <span className="tnum text-muted">{e.samples} samples · {e.windows} windows · LCB {usd(e.ev_lcb, true)}</span>
            <Pill tone={e.status === "active" ? "mint" : e.status === "suspended" ? "coral" : "violet"}>{e.status}</Pill>
          </div>
        ))}
      </Card>
      <Card title="Calibration" className="px-3 pb-2">
        {r?.calibration ? (<>
          <Row k="Settled windows scored" v={r.calibration.n} />
          <Row k="Brier score" v={num(r.calibration.brier)?.toFixed(4) ?? DASH} />
          <Row k="Expected calibration error" v={r.calibration.ece == null ? "needs ≥10 samples" : pct(r.calibration.ece, 2)} />
          <div className="mt-2 grid grid-cols-10 items-end gap-0.5" style={{ height: 80 }} aria-label="Reliability bins">
            {r.calibration.bins.map((b: any, i: number) => (
              <div key={i} title={`${pct(b.lo, 0)}–${pct(b.hi, 0)}: n=${b.n}, observed ${b.obs == null ? "—" : pct(b.obs)}`} className="bg-violet/60" style={{ height: `${(b.obs ?? 0) * 100}%`, opacity: b.n ? 1 : 0.15 }} />
            ))}
          </div>
          <div className="text-2xs text-muted">Observed frequency per predicted-probability decile (sparse bins faded).</div>
        </>) : <div className="py-2 text-muted">No settled windows with recorded forecasts yet. Calibration appears after official settlements.</div>}
      </Card>
      <Card title="Skipped opportunities (24 h)" className="px-3 pb-2">
        {r && Object.keys(r.skip_reasons_24h || {}).length === 0 && <div className="py-2 text-muted">None recorded.</div>}
        {Object.entries(r?.skip_reasons_24h || {}).sort((x: any, y: any) => y[1] - x[1]).slice(0, 10).map(([k, n]: any) => <Row key={k} k={reason(k)} v={n} />)}
        <Row k="Windows discovered / analyzed" v={r ? `${r.coverage_24h.discovered} / ${r.coverage_24h.analyzed}` : DASH} />
      </Card>
      <Card title="Execution & latency" className="px-3 pb-2">
        {Object.entries(r?.latency || {}).map(([k, v]: any) => <Row key={k} k={k} v={v.p50 == null ? DASH : `p50 ${v.p50.toFixed(1)} · p95 ${v.p95.toFixed(1)} · p99 ${v.p99.toFixed(1)} ms (n=${v.n})`} />)}
        <Row k="Proxy→index basis samples" v={r?.basis?.samples ?? DASH} />
        <Row k="Replay" v={r?.replay?.status ?? DASH} />
      </Card>
      <Card title="Optional Claude research" className="px-3 pb-2">
        <Row k="Status" v={r?.claude?.enabled ? "enabled" : "disabled (default)"} />
        <Row k="Monthly cap / cutoff" v={r ? `$${r.claude.cap_usd} / $${r.claude.cutoff_usd}` : DASH} />
        <Row k="Spent this month" v={r ? `$${r.claude.spent_usd}` : DASH} />
        <p className="text-2xs text-muted">Research only — it cannot place orders, change limits or see keys. Trading runs identically without it.</p>
      </Card>
    </div>
  );
}

export function SettingsDrawer({ onLogout }: { onLogout: () => void }) {
  const a = useStore((s) => s.account);
  const [st, setSt] = useState<any>(null);
  const [msg, setMsg] = useState("");
  const [reauth, setReauth] = useState({ password: "", totp: "" });
  const [confirm, setConfirm] = useState("");
  useEffect(() => { api("/api/settings").then(setSt); }, []);
  const call = async (path: string, body: any = {}) => {
    setMsg("");
    try { const r = await api(path, { body }); setMsg(JSON.stringify(r.results ?? r.note ?? (r.ok ? "done" : r))); }
    catch (e: any) { setMsg(e.status === 428 ? "Re-authenticate below first (sensitive action)." : e.message); }
  };
  const doReauth = async () => { try { await api("/api/auth/reauth", { body: reauth }); setMsg("Re-authenticated for 5 minutes."); setReauth({ password: "", totp: "" }); } catch (e: any) { setMsg(e.message); } };
  const enablePush = async () => {
    try {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) return setMsg("Push not supported here. On iPhone, install to Home Screen first (iOS 16.4+).");
      if (!st?.push_public_key) return setMsg("Server push key not configured (see runbook).");
      const perm = await Notification.requestPermission();
      if (perm !== "granted") return setMsg("Notification permission not granted.");
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: st.push_public_key });
      const j = sub.toJSON();
      await api("/api/push/subscribe", { body: { endpoint: j.endpoint, keys: j.keys } });
      setMsg("Push notifications enabled.");
    } catch (e: any) { setMsg(e.message); }
  };
  const lim = st?.immutable_limits;
  return (
    <div className="space-y-3">
      <Card title="Mode" className="px-3 pb-2">
        <Row k="Account mode" v={<Pill tone={st?.mode === "live" ? "coral" : "blue"}>{st?.mode || DASH}</Pill>} />
        <Row k="Live authorization" v={st?.live_authorized ? `recorded ${timeOf(st.live_authorization.ts)}` : "not authorized (administrative step on the server)"} />
        <p className="text-2xs text-muted">Live enablement is an administrative setup step performed on the server after documented validation — not a routine trade approval.</p>
      </Card>
      <Card title="Emergency controls" className="space-y-2 px-3 pb-3">
        <p className="text-2xs text-muted">Pause stops new entries only; exit management continues. Cancel removes pending entry orders. Flatten attempts to sell inventory at available bids — liquidation is not guaranteed.</p>
        <div className="grid grid-cols-2 gap-2">
          {a?.controls?.entries_paused ? <Btn tone="mint" onClick={() => call("/api/controls/resume")}>Resume entries</Btn> : <Btn tone="coral" onClick={() => call("/api/controls/pause")}>Pause entries</Btn>}
          <Btn tone="ghost" onClick={() => call("/api/controls/cancel-entries")}>Cancel pending entries</Btn>
        </div>
        <div className="flex gap-2"><input value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder="Type FLATTEN" className="h-10 flex-1 rounded-lg border border-line bg-surface2 px-2" />
          <Btn tone="coral" disabled={confirm !== "FLATTEN"} onClick={() => call("/api/controls/flatten", { confirm })}>Emergency flatten</Btn></div>
        {(a?.halts || []).map((h: any) => (
          <div key={h.code} className="flex items-center justify-between rounded-lg border border-amber/40 p-2 text-[12px]"><span>{h.code} <span className="text-muted">({h.kind})</span></span>
            {h.kind === "latched" && <Btn tone="ghost" onClick={() => call("/api/controls/acknowledge", { code: h.code, cause_cleared: true })}>Acknowledge</Btn>}</div>
        ))}
      </Card>
      <Card title="Immutable risk boundaries" className="px-3 pb-2">
        {lim ? (<>
          <Row k="Per-entry ceiling" v={pct(lim.max_entry_fraction, 0)} /><Row k="Per-market exposure" v={pct(lim.max_market_exposure_fraction, 0)} />
          <Row k="Portfolio exposure" v={pct(lim.max_portfolio_exposure_fraction, 0)} /><Row k="Daily loss limit" v={pct(lim.daily_loss_fraction, 0)} />
          <Row k="Max drawdown" v={pct(lim.max_drawdown_fraction, 0)} /><Row k="Concurrent positions" v={lim.max_concurrent_positions} />
          <Row k="Aggressive mode" v={lim.aggressive_enabled ? "enabled" : "disabled"} /><Row k="Tradable series" v={lim.allowed_series.join(", ")} />
          <Row k="Policy fingerprint" v={lim.fingerprint} />
        </>) : DASH}
        <p className="text-2xs text-muted">Read-only here. Changing absolute limits requires an administrative release; research and AI cannot modify them.</p>
      </Card>
      <Card title="Manual positions" className="px-3 pb-2">
        <label className="flex min-h-[44px] items-center justify-between"><span className="text-[13px]">Let the bot manage my manual positions</span>
          <input type="checkbox" className="h-5 w-5" checked={!!st?.bot_manages_manual} onChange={(e) => call("/api/settings/bot-manual", { enabled: e.target.checked }).then(() => api("/api/settings").then(setSt))} /></label>
      </Card>
      <Card title="Notifications" className="px-3 pb-3"><Btn tone="ghost" onClick={enablePush}>Enable push notifications</Btn></Card>
      <Card title="Security" className="space-y-2 px-3 pb-3">
        <div className="text-2xs text-muted">Sensitive changes require re-authentication (password + authenticator code).</div>
        <input type="password" autoComplete="current-password" value={reauth.password} onChange={(e) => setReauth({ ...reauth, password: e.target.value })} placeholder="Password" className="h-10 w-full rounded-lg border border-line bg-surface2 px-2" />
        <input inputMode="numeric" autoComplete="one-time-code" value={reauth.totp} onChange={(e) => setReauth({ ...reauth, totp: e.target.value })} placeholder="6-digit code" className="h-10 w-full rounded-lg border border-line bg-surface2 px-2" />
        <div className="flex gap-2"><Btn onClick={doReauth}>Re-authenticate</Btn><Btn tone="ghost" onClick={onLogout}>Sign out</Btn></div>
        <div className="max-h-40 overflow-y-auto text-2xs text-muted">{(st?.audit || []).map((x: any) => <div key={x.id}>{timeOf(x.ts)} · {x.action}</div>)}</div>
      </Card>
      {msg && <div className="break-words text-[12px] text-amber">{msg}</div>}
    </div>
  );
}

export function NotificationsDrawer() {
  const notices = useStore((s) => s.notices);
  const sys = useStore((s) => s.system);
  return (
    <div className="space-y-1 text-[12px]">
      {notices.length === 0 && (sys?.events || []).length === 0 && <div className="py-6 text-center text-muted">No notifications.</div>}
      {notices.map((n, i) => <div key={i} className="border-b border-line py-1.5"><span className="text-muted">{new Date(n.at).toLocaleTimeString()}</span> · {n.kind} {n.ticker || n.kind === "notice" ? JSON.stringify(n.credits || n.ticker || "") : ""} {n.state || n.result || ""}</div>)}
      {(sys?.events || []).slice().reverse().map((e: any, i: number) => <div key={`e${i}`} className="border-b border-line py-1.5 text-muted">{timeOf(e.ts)} · {e.kind} {e.ticker || ""}</div>)}
    </div>
  );
}
