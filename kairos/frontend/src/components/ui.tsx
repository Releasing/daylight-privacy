import { animate, useReducedMotion } from "motion/react";
import { ReactNode, useEffect, useRef, useState } from "react";
import { DASH, num } from "../format";

// Supplied Kairos outline icon set (Design Assets V11 legacy_reference/*.svg, 24 px, 1.7 px stroke).
const PATHS: Record<string, string> = {
  bell: "M5 17 H19 L17 14 V9 A5 5 0 0 0 7 9 V14 Z M10 21 H14",
  clock: "M12 3 A9 9 0 1 0 12 21 A9 9 0 1 0 12 3 M12 7 V12 L16 14",
  funds: "M3 6 H21 V19 H3 Z M16 11 H21 V15 H16 Z",
  home: "M3 10 L12 3 L21 10 V21 H15 V14 H9 V21 H3 Z",
  live: "M3 17 L8 12 L12 15 L21 5 M16 5 H21 V10",
  orders: "M5 3 H19 V21 H5 Z M8 8 H16 M8 12 H16 M8 16 H13",
  pause: "M8 5 V19 M16 5 V19",
  research: "M4 20 V4 H20 V20 Z M8 15 L11 11 L14 13 L17 8",
  settings: "M12 3 V6 M12 18 V21 M3 12 H6 M18 12 H21 M6 6 L8 8 M16 16 L18 18 M18 6 L16 8 M8 16 L6 18 M12 8 A4 4 0 1 0 12 16 A4 4 0 1 0 12 8",
  shield: "M12 3 L20 6 V12 Q20 18 12 22 Q4 18 4 12 V6 Z M8 12 L11 15 L16 9",
};

export function KIcon({ name, size = 20, className = "" }: { name: keyof typeof PATHS | string; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden className={className}>
      <path d={PATHS[name]} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function KMark({ size = 32 }: { size?: number }) {
  // Proposed K mark from the supplied app icon (kairos_app_icon.svg).
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" aria-label="Kairos">
      <rect width="100" height="100" rx="24" fill="#101D31" />
      <path d="M29 23 V77 M72 23 L44 50 L73 77 M44 50 H29" fill="none" stroke="#32A8FF" strokeWidth="9" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="72" cy="23" r="5" fill="#B4E8FF" />
    </svg>
  );
}

/** Animates from the currently rendered value to the newest confirmed value (100–250 ms), interruptible,
 *  with a subtle up/down flash; honors reduced motion; never animates random values (spec p.134). */
export function AnimatedNumber({ value, format, className = "", flash = true }: { value: unknown; format: (n: number) => string; className?: string; flash?: boolean }) {
  const target = num(value);
  const reduced = useReducedMotion();
  const [shown, setShown] = useState<number | null>(target);
  const [dir, setDir] = useState<0 | 1 | -1>(0);
  const cur = useRef<number | null>(target);
  useEffect(() => {
    if (target === null) { cur.current = null; setShown(null); return; }
    const from = cur.current;
    if (from === null || reduced) { cur.current = target; setShown(target); return; }
    if (from === target) return;
    if (flash) setDir(target > from ? 1 : -1);
    const ctl = animate(from, target, { duration: 0.18, ease: "easeOut", onUpdate: (v) => { cur.current = v; setShown(v); } });
    const t = window.setTimeout(() => setDir(0), 450);
    return () => { ctl.stop(); clearTimeout(t); };
  }, [target, reduced, flash]);
  const flashCls = dir === 1 ? "flash-up" : dir === -1 ? "flash-down" : "";
  return <span className={`tnum ${className} ${flashCls}`}>{shown === null ? DASH : format(shown)}</span>;
}

export function Card({ children, className = "", title, right }: { children: ReactNode; className?: string; title?: ReactNode; right?: ReactNode }) {
  return (
    <section className={`rounded-card border border-line bg-surface ${className}`}>
      {title !== undefined && (
        <header className="flex items-center justify-between gap-2 px-3 pt-2.5 pb-1.5">
          <h2 className="text-[13px] font-semibold text-text">{title}</h2>
          {right}
        </header>
      )}
      {children}
    </section>
  );
}

export function Pill({ children, tone = "blue" }: { children: ReactNode; tone?: "blue" | "mint" | "coral" | "amber" | "violet" | "cyan" | "muted" }) {
  const map = {
    blue: "text-blue border-blue/40 bg-blue/10", mint: "text-mint border-mint/40 bg-mint/10", coral: "text-coral border-coral/40 bg-coral/10",
    amber: "text-amber border-amber/40 bg-amber/10", violet: "text-violet border-violet/40 bg-violet/10", cyan: "text-cyan border-cyan/40 bg-cyan/10",
    muted: "text-muted border-line bg-surface2",
  } as const;
  return <span className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-2xs font-semibold uppercase tracking-wide ${map[tone]}`}>{children}</span>;
}

export function Stat({ label, children, sub }: { label: string; children: ReactNode; sub?: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-2xs text-muted">{label}</div>
      <div className="truncate text-[20px] font-semibold leading-7 tnum">{children}</div>
      {sub && <div className="truncate text-2xs text-muted">{sub}</div>}
    </div>
  );
}

export function Btn({ children, onClick, tone = "blue", disabled, className = "", type = "button" }: { children: ReactNode; onClick?: () => void; tone?: "blue" | "coral" | "ghost" | "mint"; disabled?: boolean; className?: string; type?: "button" | "submit" }) {
  const map = { blue: "bg-blue text-midnight hover:bg-blue/90", coral: "border border-coral/50 bg-coral/10 text-coral hover:bg-coral/20",
    ghost: "border border-line bg-surface2 text-text hover:border-blue/60", mint: "bg-mint text-midnight hover:bg-mint/90" } as const;
  return (
    <button type={type} disabled={disabled} onClick={onClick}
      className={`inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-lg px-3 text-[13px] font-semibold transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue disabled:cursor-not-allowed disabled:opacity-40 coarse:min-h-[44px] ${map[tone]} ${className}`}>
      {children}
    </button>
  );
}

export function Drawer({ open, onClose, title, children, wide = false }: { open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/50" role="dialog" aria-modal aria-label={title} onClick={onClose}>
      <aside onClick={(e) => e.stopPropagation()}
        className={`safe-b flex h-full w-full flex-col border-l border-line bg-surface ${wide ? "sm:max-w-[760px]" : "sm:max-w-[440px]"}`}>
        <header className="safe-t flex items-center justify-between border-b border-line px-4 py-3">
          <h2 className="text-[15px] font-semibold">{title}</h2>
          <button onClick={onClose} className="grid h-11 w-11 place-items-center rounded-lg text-muted hover:text-text" aria-label="Close">✕</button>
        </header>
        <div className="flex-1 overflow-y-auto p-4">{children}</div>
      </aside>
    </div>
  );
}

export function Row({ k, v, vc = "" }: { k: ReactNode; v: ReactNode; vc?: string }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1 text-[13px]">
      <span className="text-muted">{k}</span>
      <span className={`tnum text-right ${vc}`}>{v}</span>
    </div>
  );
}
