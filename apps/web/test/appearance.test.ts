// Appearance engine (ported from the ui-demo prototype): only known values survive,
// custom colours are per mode, and the variables the shell reads follow the choice.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ACCENTS,
  cleanAppearance,
  contrastRatio,
  currentColors,
  DEFAULT_APPEARANCE,
  INK,
  softAccent,
  THEMES,
  themeVariables,
  type Appearance,
} from "../src/appearance.js";

test("unknown or malformed values fall back to the defaults", () => {
  const v = cleanAppearance({
    mode: "neon",
    theme: "__proto__",
    accent: "chartreuse",
    font: "comic",
    radius: "blob",
    density: "tiny",
    custom: { light: { bg: "red", fg: "#12345" }, dark: { bg: "#abcdef" } },
  });
  assert.ok(v);
  assert.equal(v.mode, DEFAULT_APPEARANCE.mode);
  assert.equal(v.theme, "niletech");
  assert.equal(v.accent, "theme");
  assert.equal(v.font, "plex");
  assert.equal(v.radius, "rounded");
  assert.equal(v.density, "comfortable");
  assert.deepEqual(v.custom, { light: {}, dark: { bg: "#ABCDEF" } });
  assert.equal(cleanAppearance("nope"), null);
});

test("system mode follows the device; custom colours apply only to their mode", () => {
  const a = cleanAppearance({ mode: "system", theme: "sand", custom: { dark: { bg: "#101010" } } });
  assert.ok(a);
  assert.deepEqual(currentColors(a, false), { mode: "light", bg: "#FAF6EF", fg: "#2B2118", accent: "#C46A1E" });
  assert.equal(currentColors(a, true).bg, "#101010");
  assert.equal(currentColors({ ...a, mode: "light" }, true).mode, "light");
});

test("accent, font, corners and accent contrast reach the CSS variables", () => {
  const a = cleanAppearance({ mode: "dark", theme: "contrast", accent: "theme", font: "mono", radius: "square" });
  assert.ok(a);
  const vars = themeVariables(a, false);
  assert.equal(vars["--bg"], "#000000");
  assert.equal(vars["--accent"], "#FFD400");
  assert.equal(vars["--accent-fg"], "#0B0F12"); // bright accent gets dark text
  assert.equal(vars["--r"], "4px");
  assert.match(vars["--font"] ?? "", /IBM Plex Mono/);
  assert.equal(vars["--ok"], "#5CC98D"); // dark background uses the light status colours
});

test("contrast ratio matches the WCAG formula", () => {
  assert.equal(contrastRatio("#FFFFFF", "#000000").toFixed(1), "21.0");
  assert.equal(contrastRatio("#777777", "#777777"), 1);
});

function pairs(vars: Record<string, string>) {
  const accent = vars["--accent"] ?? "";
  const bg = vars["--bg"] ?? "";
  const text = vars["--accent-text"] ?? "";
  return {
    onAccent: contrastRatio(vars["--accent-fg"] ?? "", accent),
    textOnSoft: contrastRatio(text, softAccent(accent, bg)),
    textOnBg: contrastRatio(text, bg),
  };
}

test("default dark preset: text on the accent and accent-coloured text both reach 4.5:1", () => {
  const vars = themeVariables({ ...DEFAULT_APPEARANCE, mode: "dark" }, false);
  assert.equal(vars["--accent"], "#2FB3BD");
  // Was white (#FFFFFF) on #2FB3BD = 2.5:1; the dark ink reads at about 7.6:1.
  assert.equal(vars["--accent-fg"], INK);
  const c = pairs(vars);
  assert.ok(c.onAccent >= 4.5, `accent-fg on accent ${c.onAccent.toFixed(2)}`);
  assert.ok(c.textOnSoft >= 4.5, `accent text on soft accent ${c.textOnSoft.toFixed(2)}`);
  assert.ok(c.textOnBg >= 4.5, `accent text on background ${c.textOnBg.toFixed(2)}`);
  // System mode on a dark device resolves to the same values.
  assert.deepEqual(themeVariables(DEFAULT_APPEARANCE, true), vars);
});

test("every preset, mode and accent keeps accent text at 4.5:1 or better", () => {
  const failures: string[] = [];
  for (const theme of Object.keys(THEMES)) {
    for (const mode of ["light", "dark"] as const) {
      for (const accent of Object.keys(ACCENTS)) {
        const a: Appearance = { ...DEFAULT_APPEARANCE, theme, mode, accent };
        const c = pairs(themeVariables(a, false));
        for (const [name, ratio] of Object.entries(c)) {
          if (ratio < 4.5) failures.push(`${theme}/${mode}/${accent} ${name} ${ratio.toFixed(2)}`);
        }
      }
    }
  }
  assert.deepEqual(failures, []);
});
