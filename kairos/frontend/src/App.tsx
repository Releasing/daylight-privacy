import { ArrowLeftRight, LineChart, Users } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { api } from "./api";
import { StatusBar } from "./components/panels";
import { Btn, Drawer, KIcon, KMark, Pill } from "./components/ui";
import { ActivityDrawer, FundsDrawer, NotificationsDrawer, ResearchDrawer, SettingsDrawer } from "./drawers/Drawers";
import { connect, dataAgeMs, disconnect, useStore } from "./state/store";
import { CommunityView } from "./views/Community";
import { MarketsDesktop, MarketsMobile, OverlaySheet } from "./views/Markets";
import { TradeView } from "./views/Trade";

// Exactly three primary destinations (spec p.129, p.137, p.138). Settings and notifications are utility icons.
type Tab = "markets" | "trade" | "community";
const TABS: { id: Tab; label: string; Icon: any }[] = [
  { id: "markets", label: "Markets", Icon: LineChart },
  { id: "trade", label: "Trade", Icon: ArrowLeftRight },
  { id: "community", label: "Community", Icon: Users },
];

function useIsMobile() {
  const q = "(max-width: 900px)";
  const [m, setM] = useState(() => window.matchMedia(q).matches);
  useEffect(() => { const mq = window.matchMedia(q); const h = () => setM(mq.matches); mq.addEventListener("change", h); return () => mq.removeEventListener("change", h); }, []);
  return m;
}

function Connection() {
  const conn = useStore((s) => s.connected);
  const st = useStore((s) => s);
  const age = dataAgeMs(st);
  const p95 = st.market?.latency?.lane_fast?.p95;
  const stale = conn !== "live" || (age !== null && age > 5000);
  const label = conn === "live" ? (stale ? "STALE" : "LIVE") : conn === "offline" ? "OFFLINE" : "RECONNECTING";
  return (
    <div className="flex items-center overflow-hidden rounded-lg border border-line text-[12px]" title={`Last server update ${age === null ? "never" : `${Math.round(age / 100) / 10}s ago`}`}>
      <span className={`flex items-center gap-1.5 px-2 py-1.5 font-semibold ${stale ? "text-amber" : "text-mint"}`}><span className={`h-2 w-2 rounded-full ${stale ? "bg-amber" : "bg-mint"}`} />{label}</span>
      <span className="hidden border-l border-line px-2 py-1.5 tnum text-muted sm:inline">{p95 != null ? `${p95.toFixed(0)} ms` : "—"}</span>
    </div>
  );
}

function Header({ tab, setTab, mobile, open }: { tab: Tab; setTab: (t: Tab) => void; mobile: boolean; open: (d: string) => void }) {
  const a = useStore((s) => s.account);
  return (
    <header className="safe-t border-b border-line bg-midnight">
      <div className="safe-x flex h-14 items-center gap-3">
        <div className="flex items-center gap-2"><KMark size={30} /><span className="text-[17px] font-bold tracking-[0.28em]">KAIROS</span></div>
        {!mobile && (
          <nav className="ml-6 flex h-full items-stretch gap-1" aria-label="Primary">
            {TABS.map(({ id, label, Icon }) => (
              <button key={id} onClick={() => setTab(id)} aria-current={tab === id ? "page" : undefined}
                className={`flex items-center gap-2 px-5 text-[14px] font-semibold transition-colors ${tab === id ? "nav-glow text-text" : "text-muted hover:text-text"}`}>
                <Icon size={17} className={tab === id ? "text-blue" : ""} />{label}
              </button>
            ))}
          </nav>
        )}
        <div className="ml-auto flex items-center gap-2">
          <Pill tone={a?.paper === false ? "coral" : "blue"}>{a?.paper === false ? "LIVE MODE" : "PAPER MODE"}</Pill>
          <Connection />
          <button onClick={() => open("notifications")} aria-label="Notifications" className="grid h-10 w-10 place-items-center rounded-lg border border-line text-muted hover:text-text"><KIcon name="bell" /></button>
          <button onClick={() => open("settings")} aria-label="Settings" className="grid h-10 w-10 place-items-center rounded-lg border border-line text-muted hover:text-text"><KIcon name="settings" /></button>
        </div>
      </div>
    </header>
  );
}

function MobileNav({ tab, setTab }: { tab: Tab; setTab: (t: Tab) => void }) {
  return (
    <nav className="safe-b fixed inset-x-0 bottom-0 z-30 border-t border-line bg-midnight/95 backdrop-blur" aria-label="Primary">
      <div className="grid grid-cols-3">
        {TABS.map(({ id, label, Icon }) => (
          <button key={id} onClick={() => setTab(id)} aria-current={tab === id ? "page" : undefined}
            className={`flex min-h-[56px] flex-col items-center justify-center gap-0.5 text-[11px] font-semibold ${tab === id ? "text-blue" : "text-muted"}`}>
            <Icon size={22} />{label}
          </button>
        ))}
      </div>
    </nav>
  );
}

function Login({ onDone }: { onDone: () => void }) {
  const [f, setF] = useState({ username: "", password: "", totp: "" });
  const [err, setErr] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr("");
    try { await api("/api/auth/login", { body: f }); onDone(); } catch (x: any) { setErr(x.status === 429 ? "Too many attempts — wait a minute." : "Sign-in failed."); }
  };
  return (
    <main className="safe-t safe-b grid min-h-full place-items-center p-4">
      <form onSubmit={submit} className="w-full max-w-[360px] space-y-3 rounded-card border border-line bg-surface p-5">
        <div className="flex items-center gap-2"><KMark size={34} /><span className="text-[18px] font-bold tracking-[0.28em]">KAIROS</span></div>
        <p className="text-2xs text-muted">Private application. Sign in with password and authenticator code.</p>
        <input autoComplete="username" required placeholder="Username" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} className="h-11 w-full rounded-lg border border-line bg-surface2 px-3" />
        <input type="password" autoComplete="current-password" required placeholder="Password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} className="h-11 w-full rounded-lg border border-line bg-surface2 px-3" />
        <input inputMode="numeric" autoComplete="one-time-code" required pattern="[0-9]{6}" placeholder="6-digit code" value={f.totp} onChange={(e) => setF({ ...f, totp: e.target.value })} className="h-11 w-full rounded-lg border border-line bg-surface2 px-3 tnum" />
        <Btn type="submit" className="w-full">Sign in</Btn>
        {err && <div className="text-[12px] text-coral">{err}</div>}
      </form>
    </main>
  );
}

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [tab, setTab] = useState<Tab>(() => (location.hash.slice(1) as Tab) || "markets");
  const [drawer, setDrawer] = useState<string | null>(null);
  const mobile = useIsMobile();

  useEffect(() => { api("/api/auth/me").then(() => setAuthed(true)).catch(() => setAuthed(false)); }, []);
  useEffect(() => { if (authed) connect(); return () => disconnect(); }, [authed]);
  useEffect(() => { const h = () => setAuthed(false); window.addEventListener("kairos:unauth", h); return () => window.removeEventListener("kairos:unauth", h); }, []);
  useEffect(() => { if (TABS.some((t) => t.id === tab)) history.replaceState(null, "", `#${tab}`); }, [tab]);

  if (authed === null) return <div className="grid h-full place-items-center text-muted">Loading…</div>;
  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  const logout = async () => { try { await api("/api/auth/logout", { method: "POST" }); } finally { disconnect(); setAuthed(false); } };
  const titles: Record<string, string> = { activity: "Bot activity", funds: "Funds", research: "Research & diagnostics", settings: "Settings", notifications: "Notifications", overlays: "Chart overlays" };

  return (
    <div className={`flex flex-col ${mobile ? "min-h-full pb-[72px]" : "h-[100dvh] overflow-hidden"}`}>
      <Header tab={tab} setTab={setTab} mobile={mobile} open={setDrawer} />
      <main className={`min-h-0 flex-1 ${mobile ? "" : "overflow-hidden"}`}>
        {tab === "markets" && (mobile ? <MarketsMobile openDrawer={setDrawer} /> : <MarketsDesktop openDrawer={setDrawer} />)}
        {tab === "trade" && <TradeView mobile={mobile} />}
        {tab === "community" && <CommunityView />}
      </main>
      {!mobile && <StatusBar />}
      {mobile && <MobileNav tab={tab} setTab={setTab} />}
      <Drawer open={!!drawer} onClose={() => setDrawer(null)} title={drawer ? titles[drawer] : ""} wide={drawer === "research" || drawer === "activity"}>
        {drawer === "activity" && <ActivityDrawer />}
        {drawer === "funds" && <FundsDrawer />}
        {drawer === "research" && <ResearchDrawer />}
        {drawer === "settings" && <SettingsDrawer onLogout={logout} />}
        {drawer === "notifications" && <NotificationsDrawer />}
        {drawer === "overlays" && <OverlaySheet onClose={() => setDrawer(null)} />}
      </Drawer>
    </div>
  );
}
