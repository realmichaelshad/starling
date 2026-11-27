/**
 * Which skin Starling wears, and who gets to decide.
 *
 * The dock is the only surface that is entirely ours, so it is the only surface
 * a preference may override. The pill in a toolbar and the per-post card are
 * drawn INSIDE X's chrome and have to keep matching the page they sit on — a
 * dark chip in a white toolbar reads as a rendering bug, not as a choice.
 *
 * The default is "auto", because the honest answer to "what theme is Starling?"
 * is "whatever X is". But X ships THREE surfaces, not two — Default (#ffffff),
 * Dim (#15202b) and Lights out (#000000) — and collapsing Dim into Lights out
 * painted a pure-black panel onto a slate-blue page, which reads as a hole
 * punched in the feed rather than a card sitting on it. So a resolved theme
 * carries a TONE alongside the light/dark mode, and Dim gets Dim's slate.
 */

export type ThemePreference = "auto" | "light" | "dark";

/** The three surfaces X actually paints. */
export type SurfaceTone = "light" | "dim" | "dark";

export interface ResolvedTheme {
  /** Picks the token block. Only two modes exist; tone refines the dark one. */
  mode: "light" | "dark";
  tone: SurfaceTone;
}

/** Below this, a dark surface is Lights out's black rather than Dim's slate. */
const DIM_FLOOR = 0.05;
/** At or above this a surface is a light one — the pill's long-standing cut. */
const DARK_CEILING = 0.45;

/**
 * The same weighted average the pill has always used to read a surface. Not
 * gamma-corrected, and deliberately not: the numbers below are calibrated
 * against what it returns for X's own three backgrounds.
 */
export function surfaceLuminance(r: number, g: number, b: number): number {
  return (r * 0.2126 + g * 0.7152 + b * 0.0722) / 255;
}

/** Name the surface a background colour belongs to. */
export function toneFromRgb(r: number, g: number, b: number): SurfaceTone {
  const luminance = surfaceLuminance(r, g, b);
  if (luminance >= DARK_CEILING) return "light";
  return luminance < DIM_FLOOR ? "dark" : "dim";
}

export function resolveTheme(preference: ThemePreference, page: SurfaceTone): ResolvedTheme {
  if (preference === "light") return { mode: "light", tone: "light" };
  if (preference === "dark") {
    // Forcing dark ON a Dim page keeps Dim's slate: the ask is "go dark", not
    // "stop fitting in". From a light page there is no dark tone to inherit, so
    // it lands on Lights out — the decisive one.
    return { mode: "dark", tone: page === "dim" ? "dim" : "dark" };
  }
  return { mode: page === "light" ? "light" : "dark", tone: page };
}

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === "auto" || value === "light" || value === "dark";
}

/**
 * The line under the switch. Its whole job is to say whether X is still in
 * charge, because that is the one thing a two-position switch cannot show.
 */
export function themeHelp(preference: ThemePreference, page: SurfaceTone): string {
  const pageName = page === "light" ? "light" : page === "dim" ? "dim" : "dark";
  if (preference === "auto") return `Following X, which is ${pageName} right now.`;
  return `Set to ${preference}. X is ${pageName}.`;
}

// ---------------------------------------------------------------------------
// Contrast: the pill Starling draws inside X's own toolbar
// ---------------------------------------------------------------------------

/** X's three page backgrounds. */
export const X_SURFACES: Record<SurfaceTone, string> = {
  light: "#ffffff",
  dim: "#15202b",
  dark: "#000000",
};

export interface PillPalette {
  /** Label colour on the resting pill (transparent fill: the page shows through). */
  text: string;
  border: string;
  /** "✓ Done": text over a translucent green wash. */
  doneText: string;
  doneWash: string;
  /** "Try again" after a failure. */
  errorText: string;
  errorWash: string;
}

/**
 * The composer pill's colours for each theme. The CSS in the content script is built from these, so
 * the unit test that checks them against X's surfaces checks what ships. The light theme needs its
 * own state colours: the dark theme's mint "Done" (#6ee7b7) on a white toolbar is 1.5:1.
 */
export const PILL_PALETTE: Record<"light" | "dark", PillPalette> = {
  dark: {
    text: "#eff3f4",
    border: "#536471",
    doneText: "#6ee7b7",
    doneWash: "rgba(0,186,124,.12)",
    errorText: "#ff8a93",
    errorWash: "rgba(244,33,46,.1)",
  },
  light: {
    text: "#0f1419",
    border: "#cfd9de",
    doneText: "#00663f",
    doneWash: "rgba(0,186,124,.1)",
    errorText: "#b0141f",
    errorWash: "rgba(244,33,46,.08)",
  },
};

/** The pill skin for a surface: X's Dim is a dark page, and gets the dark pill. */
export function pillThemeFor(tone: SurfaceTone): "light" | "dark" {
  return tone === "light" ? "light" : "dark";
}

type Rgb = [number, number, number];

/** `#rrggbb`, or `rgba(r,g,b,a)` composited over `under`. */
export function parseColor(value: string, under: Rgb = [255, 255, 255]): Rgb {
  const hex = /^#([0-9a-f]{6})$/i.exec(value.trim());
  if (hex) {
    const n = Number.parseInt(hex[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const rgba = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(value.trim());
  if (!rgba) throw new Error(`not a colour: ${value}`);
  const alpha = rgba[4] === undefined ? 1 : Number(rgba[4]);
  return [1, 2, 3].map((index, channel) => Math.round(Number(rgba[index]) * alpha + under[channel] * (1 - alpha))) as Rgb;
}

/** WCAG 2.x relative luminance. */
export function relativeLuminance([r, g, b]: Rgb): number {
  const linear = (channel: number) => {
    const c = channel / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

/** WCAG contrast ratio of a foreground over a background (either may be translucent over `page`). */
export function contrastRatio(foreground: string, background: string, page = "#ffffff"): number {
  const base = parseColor(page);
  const bg = parseColor(background, base);
  const fg = parseColor(foreground, bg);
  const [light, dark] = [relativeLuminance(fg), relativeLuminance(bg)].sort((a, b) => b - a);
  return (light + 0.05) / (dark + 0.05);
}
