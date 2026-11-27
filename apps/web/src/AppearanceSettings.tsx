/** Settings › Appearance, as in the NileTech Assistant prototype. */

import { useEffect, useState } from "react";
import {
  ACCENTS,
  cleanAppearance,
  contrastRatio,
  currentColors,
  DEFAULT_APPEARANCE,
  FONTS,
  systemPrefersDark,
  THEMES,
  type Appearance,
  type Density,
  type Mode,
  type Radius,
} from "./appearance";

function Thumb({ dark, half }: { dark?: boolean; half?: boolean }) {
  return (
    <span className={`thumb ${dark ? "d" : "l"}${half ? " half" : ""}`}>
      <i className="sb" />
      <span className="lines">
        <i className="acc" />
        <i />
        <i className="w70" />
      </span>
    </span>
  );
}

function Swatch({ color }: { color: string }) {
  return <span className="swatch" style={{ background: color }} />;
}

export function AppearanceSettings({
  appearance,
  onChange,
}: {
  appearance: Appearance;
  onChange: (next: Appearance) => void;
}) {
  const [advanced, setAdvanced] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const c = currentColors(appearance, systemPrefersDark());
  const custom = appearance.custom[c.mode];
  const ratio = contrastRatio(c.bg, c.fg);
  const [bgText, setBgText] = useState(c.bg);
  const [fgText, setFgText] = useState(c.fg);
  useEffect(() => {
    setBgText(c.bg);
    setFgText(c.fg);
  }, [c.bg, c.fg]);

  const set = (patch: Partial<Appearance>) => onChange({ ...appearance, ...patch });
  const setColor = (key: "bg" | "fg", value: string): boolean => {
    if (!/^#[0-9a-fA-F]{6}$/.test(value)) return false;
    onChange({
      ...appearance,
      custom: { ...appearance.custom, [c.mode]: { ...appearance.custom[c.mode], [key]: value.toUpperCase() } },
    });
    return true;
  };
  const copy = async () => {
    const text = JSON.stringify(appearance);
    try {
      await navigator.clipboard.writeText(text);
      setMessage("Theme copied. Paste it on another device with Import.");
    } catch {
      setImportOpen(true);
      setImportText(text);
      setMessage("Copy the text in the box.");
    }
  };
  const applyImport = () => {
    try {
      const raw: unknown = JSON.parse(importText);
      const theme = raw && typeof raw === "object" ? (raw as { theme?: unknown }).theme : undefined;
      const v = typeof theme === "string" && theme in THEMES ? cleanAppearance(raw) : null;
      if (!v) throw new Error("invalid");
      onChange(v);
      setImportOpen(false);
      setMessage("Theme applied.");
    } catch {
      setMessage("That isn't a theme copied from this page.");
    }
  };

  return (
    <div className="appearance">
      <h2>Appearance</h2>
      <div className="card">
        <div className="r">
          <div className="k">
            Mode<small>System follows your device</small>
          </div>
          <div className="modes">
            {(["system", "light", "dark"] as Mode[]).map((m) => (
              <button
                key={m}
                type="button"
                className="mode"
                aria-label={m === "system" ? "System" : m === "light" ? "Light" : "Dark"}
                aria-pressed={appearance.mode === m}
                onClick={() => set({ mode: m })}
              >
                {m === "dark" ? <Thumb dark /> : <Thumb />}
                {m === "system" ? <Thumb dark half /> : null}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="r">
          <div className="k">Theme</div>
          <button type="button" className="iconbtn" title="Import theme" aria-label="Import theme" onClick={() => setImportOpen(true)}>
            <svg className="i" viewBox="0 0 24 24" aria-hidden>
              <path d="M12 4v11M7 10l5 5 5-5" />
              <path d="M5 20h14" />
            </svg>
          </button>
          <button type="button" className="iconbtn" title="Copy theme" aria-label="Copy theme" onClick={() => void copy()}>
            <svg className="i" viewBox="0 0 24 24" aria-hidden>
              <rect x="9" y="9" width="11" height="11" rx="2" />
              <path d="M5 15V5a1 1 0 0 1 1-1h10" />
            </svg>
          </button>
          <label className="pill">
            <span className="aa">Aa</span>
            <select
              aria-label="Theme"
              value={appearance.theme}
              onChange={(e) => set({ theme: e.target.value, custom: { light: {}, dark: {} } })}
            >
              {Object.entries(THEMES).map(([k, t]) => (
                <option key={k} value={k}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        {importOpen ? (
          <div className="import">
            <label className="note" htmlFor="impText">
              Paste a theme you copied from another device
            </label>
            <textarea id="impText" spellCheck={false} value={importText} onChange={(e) => setImportText(e.target.value)} />
            <div className="btns">
              <button type="button" className="btn primary" onClick={applyImport}>
                Apply
              </button>
              <button type="button" className="btn" onClick={() => setImportOpen(false)}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}
        <div className="r">
          <div className="k">Accent</div>
          <label className="pill">
            <Swatch color={c.accent} />
            <select aria-label="Accent" value={appearance.accent} onChange={(e) => set({ accent: e.target.value })}>
              {Object.entries(ACCENTS).map(([k, a]) => (
                <option key={k} value={k}>
                  {a.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        {(["bg", "fg"] as const).map((key) => (
          <div className="r" key={key}>
            <div className="k">
              {key === "bg" ? "Background" : "Foreground"}
              <small>{custom[key] ? `Custom for ${c.mode} mode` : `From theme, ${c.mode} mode`}</small>
            </div>
            <label className="pill">
              <span className="swatch" style={{ background: c[key] }}>
                <input
                  type="color"
                  aria-label={key === "bg" ? "Pick background" : "Pick foreground"}
                  value={c[key].toLowerCase()}
                  onChange={(e) => setColor(key, e.target.value)}
                />
              </span>
              <input
                type="text"
                aria-label={key === "bg" ? "Background hex" : "Foreground hex"}
                maxLength={7}
                value={key === "bg" ? bgText : fgText}
                onChange={(e) => (key === "bg" ? setBgText : setFgText)(e.target.value)}
                onBlur={(e) => {
                  if (!setColor(key, e.target.value.trim())) {
                    setMessage("Use a hex colour like #0E1418.");
                    (key === "bg" ? setBgText : setFgText)(c[key]);
                  }
                }}
              />
            </label>
          </div>
        ))}
        <div className="r">
          <div className="k">Font</div>
          <label className="pill">
            <select aria-label="Font" value={appearance.font} onChange={(e) => set({ font: e.target.value })}>
              {Object.entries(FONTS).map(([k, f]) => (
                <option key={k} value={k}>
                  {f.name}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <button type="button" className="adv" aria-expanded={advanced} onClick={() => setAdvanced((a) => !a)}>
        Advanced{" "}
        <svg className="i" viewBox="0 0 24 24" aria-hidden>
          <path d="M9 6l6 6-6 6" />
        </svg>
      </button>
      {advanced ? (
        <div className="card">
          <div className="r">
            <div className="k">Corners</div>
            <label className="pill">
              <select aria-label="Corners" value={appearance.radius} onChange={(e) => set({ radius: e.target.value as Radius })}>
                <option value="square">Square</option>
                <option value="rounded">Rounded</option>
                <option value="round">Extra round</option>
              </select>
            </label>
          </div>
          <div className="r">
            <div className="k">Density</div>
            <label className="pill">
              <select aria-label="Density" value={appearance.density} onChange={(e) => set({ density: e.target.value as Density })}>
                <option value="comfortable">Comfortable</option>
                <option value="compact">Compact</option>
              </select>
            </label>
          </div>
          <div className="r">
            <div className="k">
              Reset appearance<small>Back to the NileTech theme, following your device</small>
            </div>
            <button
              type="button"
              className="btn"
              onClick={() => {
                onChange(DEFAULT_APPEARANCE);
                setMessage("Appearance reset.");
              }}
            >
              Reset
            </button>
          </div>
        </div>
      ) : null}
      <p className="note" role="status">
        Changes apply instantly and are remembered on this device. Text contrast:{" "}
        <b>
          {ratio.toFixed(1)}:1{ratio < 4.5 ? " (too low to read comfortably)" : ""}
        </b>
        {message ? ` · ${message}` : ""}
      </p>
    </div>
  );
}
