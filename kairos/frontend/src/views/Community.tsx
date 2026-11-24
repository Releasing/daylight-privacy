import { useEffect, useState } from "react";
import { api } from "../api";
import { Btn, Card, Pill } from "../components/ui";
import { DASH, usd } from "../format";

// Opt-in verified performance (spec p.136). Pseudonymous, realized net after fees, losses included,
// paper and live separated, sample sizes shown. No balances, keys, identity, orders or positions.
export function CommunityView() {
  const [period, setPeriod] = useState("today");
  const [mode, setMode] = useState("paper");
  const [data, setData] = useState<any>(null);
  const [me, setMe] = useState<any>(null);
  const [alias, setAlias] = useState("");
  const [msg, setMsg] = useState("");
  const load = () => api(`/api/community/leaderboard?period=${period}&mode=${mode}`).then(setData).catch((e) => setMsg(e.message));
  useEffect(() => { load(); const t = window.setInterval(load, 15000); return () => clearInterval(t); }, [period, mode]); // eslint-disable-line
  useEffect(() => { api("/api/auth/me").then((m) => { setMe(m); setAlias(m.community_alias || ""); }); }, []);
  const toggle = async (enabled: boolean) => {
    setMsg("");
    try { await api("/api/community/opt-in", { body: { enabled, alias } }); setMe({ ...me, community_opt_in: enabled, community_alias: alias }); load(); }
    catch (e: any) { setMsg(e.message); }
  };
  return (
    <div className="mx-auto flex h-full max-w-[1200px] flex-col gap-2 p-2">
      <Card className="flex flex-wrap items-center gap-2 px-3 py-2">
        <div className="flex overflow-hidden rounded-lg border border-line text-[12px]">
          {["today", "7d", "30d", "all"].map((p) => <button key={p} onClick={() => setPeriod(p)} className={`min-h-[36px] px-3 font-semibold ${period === p ? "bg-blue text-midnight" : "text-muted"}`}>{p === "today" ? "Today" : p === "all" ? "All" : p}</button>)}
        </div>
        <div className="flex overflow-hidden rounded-lg border border-line text-[12px]">
          {["paper", "live"].map((m) => <button key={m} onClick={() => setMode(m)} className={`min-h-[36px] px-3 font-semibold ${mode === m ? "bg-violet/20 text-violet" : "text-muted"}`}>{m === "paper" ? "Paper" : "Live"}</button>)}
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <input value={alias} onChange={(e) => setAlias(e.target.value)} placeholder="Public alias" maxLength={32}
            className="h-9 w-40 rounded-lg border border-line bg-surface2 px-2 text-[13px]" aria-label="Public alias" />
          {me?.community_opt_in ? <Btn tone="ghost" onClick={() => toggle(false)}>Stop sharing</Btn> : <Btn onClick={() => toggle(true)}>Share my results</Btn>}
        </div>
      </Card>
      {msg && <div className="px-1 text-[12px] text-amber">{msg}</div>}
      <Card title="Verified performance" right={<span className="text-2xs text-muted">{data ? `${data.participants} participants · ${data.sample_trades} closed trades · ${mode} · day boundary ${data.day_boundary}` : DASH}</span>} className="min-h-0 flex-1 overflow-auto">
        {data && data.rows.length === 0 ? (
          <div className="p-8 text-center text-muted">
            <div className="text-[15px] text-text">No consenting users yet</div>
            <div className="mt-1 text-[12px]">Sharing is off by default. Only realized, reconciled results of users who opt in appear here — never balances, keys, orders or open positions.</div>
          </div>
        ) : (
          <table className="w-full text-[13px] tnum">
            <thead className="text-left text-2xs text-muted"><tr><th className="px-3 py-1">#</th><th>Alias</th><th>Net P&L (after fees)</th><th>Return</th><th>Wins</th><th>Losses</th><th>Breakeven</th><th>W/L</th><th>Trades</th></tr></thead>
            <tbody>
              {(data?.rows || []).map((r: any, i: number) => (
                <tr key={r.alias} className="border-t border-line">
                  <td className="px-3 py-1.5 text-muted">{i + 1}</td><td className="font-semibold">{r.alias}</td>
                  <td className={Number(r.net) >= 0 ? "text-mint" : "text-coral"}>{usd(r.net, true)}</td>
                  <td>{r.pct === null ? DASH : `${r.pct > 0 ? "+" : ""}${r.pct}%`}</td><td>{r.wins}</td><td>{r.losses}</td><td>{r.breakevens}</td>
                  <td>{r.wl ?? DASH}</td><td>{r.trades}{r.trades < 30 && <span className="ml-1"><Pill tone="amber">small sample</Pill></span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="px-3 py-3 text-2xs text-muted">A trade is a closed position episode (partial exits and fees aggregated). Percentage return uses a cash-flow-adjusted denominator. Past results — paper results especially — do not predict future returns. No copy trading.</p>
      </Card>
    </div>
  );
}
