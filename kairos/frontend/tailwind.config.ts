import type { Config } from "tailwindcss";

// V11 palette (spec p.117, p.131; v11/palette.json). Colors carry meaning: blue = focus/actions, cyan = live
// feeds, violet = model intelligence, amber = expiry/warnings, orange = BTC identity, mint = profit/support,
// coral = loss/resistance.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        midnight: "#080D16",
        surface: "#101927",
        surface2: "#0C1420",
        line: "#223249",
        blue: "#32A8FF",
        cyan: "#67E8F9",
        violet: "#A78BFA",
        amber: "#FBBF24",
        btc: "#F7931A",
        mint: "#38D9A9",
        coral: "#FF657D",
        text: "#EDF4FF",
        muted: "#8EA2BA",
      },
      fontFamily: { sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"] },
      borderRadius: { card: "12px" },
      fontSize: { "2xs": ["11px", "14px"] },
    },
  },
  plugins: [],
} satisfies Config;
