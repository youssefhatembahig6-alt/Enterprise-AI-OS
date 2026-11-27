/**
 * Appearance (theme) engine, ported from the NileTech Assistant prototype
 * (ui-demo/eaios-ui-demo.html): mode, theme preset, accent, custom background and
 * foreground per mode, font, corners and density. The pure functions run under
 * node:test; applyAppearance and the storage helpers touch the browser.
 *
 * Preferences are visual only and contain nothing about the user or the session, so they
 * are remembered on this device. The session token never leaves memory (ADR 0101).
 */

export type Mode = "system" | "light" | "dark";
export type Radius = "square" | "rounded" | "round";
export type Density = "comfortable" | "compact";

interface Palette {
  bg: string;
  fg: string;
  accent: string;
}

const NILETECH = { name: "NileTech", light: { bg: "#F7F8F8", fg: "#15212B", accent: "#0F8A94" }, dark: { bg: "#0E1418", fg: "#E6EEF2", accent: "#2FB3BD" } };
const PLEX_STACK = '"IBM Plex Sans",system-ui,sans-serif';

export const THEMES: Record<string, { name: string; light: Palette; dark: Palette }> = {
  niletech: NILETECH,
  midnight: { name: "Midnight", light: { bg: "#FFFFFF", fg: "#111111", accent: "#2563EB" }, dark: { bg: "#000000", fg: "#FFFFFF", accent: "#3B82F6" } },
  sand: { name: "Desert sand", light: { bg: "#FAF6EF", fg: "#2B2118", accent: "#C46A1E" }, dark: { bg: "#1B1612", fg: "#F3EBE0", accent: "#E08A3C" } },
  nile: { name: "Nile green", light: { bg: "#F4F8F4", fg: "#132016", accent: "#2E8B57" }, dark: { bg: "#0D1510", fg: "#E3EFE5", accent: "#4FBF7E" } },
  contrast: { name: "High contrast", light: { bg: "#FFFFFF", fg: "#000000", accent: "#0033CC" }, dark: { bg: "#000000", fg: "#FFFFFF", accent: "#FFD400" } },
};

export const ACCENTS: Record<string, { name: string; hex: string | null }> = {
  theme: { name: "Theme default", hex: null },
  teal: { name: "Teal", hex: "#14A3AD" },
  blue: { name: "Blue", hex: "#3B82F6" },
  green: { name: "Green", hex: "#2FA866" },
  orange: { name: "Orange", hex: "#E07A1F" },
  purple: { name: "Purple", hex: "#8B5CF6" },
  pink: { name: "Pink", hex: "#E0458B" },
  red: { name: "Red", hex: "#D9433A" },
};

export const FONTS: Record<string, { name: string; stack: string }> = {
  system: { name: "System", stack: 'system-ui,-apple-system,"Segoe UI",sans-serif' },
  plex: { name: "IBM Plex Sans", stack: PLEX_STACK },
  inter: { name: "Inter", stack: '"Inter",system-ui,sans-serif' },
  manrope: { name: "Manrope", stack: '"Manrope",system-ui,sans-serif' },
  mono: { name: "Mono", stack: '"IBM Plex Mono",ui-monospace,monospace' },
};

export interface Appearance {
  mode: Mode;
  theme: string;
  accent: string;
  font: string;
  radius: Radius;
  density: Density;
  custom: { light: { bg?: string; fg?: string }; dark: { bg?: string; fg?: string } };
}

export const DEFAULT_APPEARANCE: Appearance = {
  mode: "system",
  theme: "niletech",
  accent: "theme",
  font: "plex",
  radius: "rounded",
  density: "comfortable",
  custom: { light: {}, dark: {} },
};

const HEX = /^#[0-9a-fA-F]{6}$/;
const has = (o: object, k: unknown) => typeof k === "string" && Object.prototype.hasOwnProperty.call(o, k);

/** Accepts only known keys and #RRGGBB colours; anything else falls back to the default. */
export function cleanAppearance(value: unknown): Appearance | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  const o: Appearance = { ...DEFAULT_APPEARANCE, custom: { light: {}, dark: {} } };
  if (v.mode === "system" || v.mode === "light" || v.mode === "dark") o.mode = v.mode;
  if (has(THEMES, v.theme)) o.theme = v.theme as string;
  if (has(ACCENTS, v.accent)) o.accent = v.accent as string;
  if (has(FONTS, v.font)) o.font = v.font as string;
  if (v.radius === "square" || v.radius === "rounded" || v.radius === "round") o.radius = v.radius;
  if (v.density === "comfortable" || v.density === "compact") o.density = v.density;
  const custom = v.custom as Record<string, Record<string, unknown>> | undefined;
  for (const m of ["light", "dark"] as const) {
    for (const k of ["bg", "fg"] as const) {
      const c = custom?.[m]?.[k];
      if (typeof c === "string" && HEX.test(c)) o.custom[m][k] = c.toUpperCase();
    }
  }
  return o;
}

export function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((x) => {
    const c = x / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(a: string, b: string): number {
  const x = luminance(a);
  const y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

export function resolvedMode(a: Appearance, systemDark: boolean): "light" | "dark" {
  return a.mode === "system" ? (systemDark ? "dark" : "light") : a.mode;
}

export function currentColors(a: Appearance, systemDark: boolean): Palette & { mode: "light" | "dark" } {
  const mode = resolvedMode(a, systemDark);
  const preset = (THEMES[a.theme] ?? NILETECH)[mode];
  const custom = a.custom[mode];
  return {
    mode,
    bg: custom.bg ?? preset.bg,
    fg: custom.fg ?? preset.fg,
    accent: ACCENTS[a.accent]?.hex ?? preset.accent,
  };
}

/** The CSS custom properties the shell reads (prototype applyTheme). */
export function themeVariables(a: Appearance, systemDark: boolean): Record<string, string> {
  const c = currentColors(a, systemDark);
  const dark = luminance(c.bg) < 0.2;
  return {
    "--bg": c.bg,
    "--fg": c.fg,
    "--accent": c.accent,
    "--accent-fg": luminance(c.accent) > 0.45 ? "#0B0F12" : "#FFFFFF",
    "--ok": dark ? "#5CC98D" : "#1F7A4A",
    "--warn": dark ? "#E7A54D" : "#A35A00",
    "--crit": dark ? "#F08A82" : "#B3261E",
    "--font": FONTS[a.font]?.stack ?? PLEX_STACK,
    "--r": a.radius === "square" ? "4px" : a.radius === "round" ? "18px" : "12px",
  };
}

// ---- browser side -------------------------------------------------------------------

const KEY = "eaios-appearance";

function storage(): Storage | null {
  try {
    return window.localStorage; // visual preferences only; never tokens (ADR 0101)
  } catch {
    return null;
  }
}

export function loadAppearance(): Appearance {
  try {
    return cleanAppearance(JSON.parse(storage()?.getItem(KEY) ?? "null")) ?? DEFAULT_APPEARANCE;
  } catch {
    return DEFAULT_APPEARANCE;
  }
}

export function saveAppearance(a: Appearance): void {
  try {
    storage()?.setItem(KEY, JSON.stringify(a));
  } catch {
    /* storage unavailable: the choice still applies for this page */
  }
}

export function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/** Sets the variables on <html> through the CSSOM (allowed by the CSP's style-src 'self'). */
export function applyAppearance(a: Appearance): void {
  const root = document.documentElement;
  const dark = systemPrefersDark();
  for (const [k, v] of Object.entries(themeVariables(a, dark))) root.style.setProperty(k, v);
  root.style.colorScheme = luminance(currentColors(a, dark).bg) < 0.2 ? "dark" : "light";
  root.classList.toggle("compact", a.density === "compact");
}
