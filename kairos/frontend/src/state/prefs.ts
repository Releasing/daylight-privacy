// Display preferences are stored separately from strategy configuration and never change model inputs (p.128).
import { useEffect, useState } from "react";

export interface Prefs {
  levels: boolean; zones: boolean; strike: boolean; ema: boolean; volume: boolean; patterns: boolean;
  fills: boolean; entries: boolean; targets: boolean;
}
const DEFAULTS: Prefs = { levels: true, zones: false, strike: true, ema: true, volume: true, patterns: true, fills: true, entries: true, targets: true };
const KEY = "kairos.display.v1";
const subs = new Set<(p: Prefs) => void>();

function load(): Prefs {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch { return DEFAULTS; }
}
let current = load();

export function usePrefs(): [Prefs, (p: Partial<Prefs>) => void] {
  const [p, setP] = useState(current);
  useEffect(() => { subs.add(setP); return () => { subs.delete(setP); }; }, []);
  const update = (patch: Partial<Prefs>) => {
    current = { ...current, ...patch };
    try { localStorage.setItem(KEY, JSON.stringify(current)); } catch { /* private mode */ }
    subs.forEach((s) => s(current));
  };
  return [p, update];
}
