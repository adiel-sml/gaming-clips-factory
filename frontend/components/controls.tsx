"use client";
import { useEffect, useState } from "react";

/* ---------- temps ---------- */
export function fmtTime(s: number, withHours = true): string {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return withHours || h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

export function parseTime(text: string): number | null {
  const t = text.trim();
  if (!t) return null;
  if (/^\d+(\.\d+)?$/.test(t)) return Number(t);
  const parts = t.split(":").map(Number);
  if (parts.some((p) => Number.isNaN(p))) return null;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

/* ---------- contrôle segmenté ---------- */
export function Segmented<T extends string | number>({ value, options, onChange }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void;
}) {
  return (
    <div className="segmented" role="radiogroup">
      {options.map((o) => (
        <button key={String(o.value)} type="button" role="radio" aria-checked={o.value === value}
          className={o.value === value ? "active" : ""} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ---------- puces ---------- */
export function ChipGroup<T extends string | number>({ value, options, onChange }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void;
}) {
  return (
    <div className="chips">
      {options.map((o) => (
        <button key={String(o.value)} type="button" className={`chip ${o.value === value ? "active" : ""}`} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ---------- cartes de choix ---------- */
export function ChoiceCards<T extends string>({ value, options, onChange, columns }: {
  value: T;
  options: { value: T; title: string; sub?: string; emoji?: string; visual?: React.ReactNode }[];
  onChange: (v: T) => void;
  columns?: 3;
}) {
  return (
    <div className={`choices ${columns === 3 ? "choices-3" : ""}`}>
      {options.map((o) => (
        <button key={o.value} type="button" className={`choice ${o.value === value ? "active" : ""}`} onClick={() => onChange(o.value)}>
          {o.visual}
          {o.emoji && <span className="choice-emoji">{o.emoji}</span>}
          <span className="choice-title">{o.title}</span>
          {o.sub && <span className="choice-sub">{o.sub}</span>}
        </button>
      ))}
    </div>
  );
}

/* ---------- curseur double ---------- */
export function RangeSlider({ min, max, start, end, step = 1, onChange }: {
  min: number; max: number; start: number; end: number; step?: number; onChange: (s: number, e: number) => void;
}) {
  const span = Math.max(1e-6, max - min);
  const left = ((start - min) / span) * 100;
  const right = ((end - min) / span) * 100;
  return (
    <div className="range">
      <div className="range-track" />
      <div className="range-fill" style={{ left: `${left}%`, width: `${Math.max(0, right - left)}%` }} />
      <input type="range" aria-label="Début" min={min} max={max} step={step} value={start}
        onChange={(e) => onChange(Math.min(Number(e.target.value), end - step), end)} />
      <input type="range" aria-label="Fin" min={min} max={max} step={step} value={end}
        onChange={(e) => onChange(start, Math.max(Number(e.target.value), start + step))} />
    </div>
  );
}

/* ---------- champ d'heure avec boutons ---------- */
export function TimeField({ label, value, onChange, min = 0, max, steps = [-60, -10, 10, 60], precise = false }: {
  label: string; value: number; onChange: (v: number) => void; min?: number; max?: number; steps?: number[]; precise?: boolean;
}) {
  // précis (bornes d'un clip) : m:ss.d sans les heures sauf si nécessaire → tient sur mobile
  const show = (v: number) => (precise ? `${fmtTime(Math.floor(v), false)}.${Math.floor((v % 1) * 10)}` : fmtTime(v));
  const [text, setText] = useState(show(value));
  useEffect(() => setText(show(value)), [value]); // eslint-disable-line react-hooks/exhaustive-deps
  const clamp = (v: number) => Math.min(max ?? Infinity, Math.max(min, v));
  const commit = () => {
    const parsed = parseTime(text.split(".")[0]) ;
    const frac = precise && text.includes(".") ? Number("0." + text.split(".")[1]) || 0 : 0;
    if (parsed == null) setText(show(value));
    else onChange(clamp(parsed + frac));
  };
  const fmtStep = (d: number) => {
    const a = Math.abs(d);
    const unit = a >= 3600 ? `${a / 3600}h` : a >= 60 ? `${a / 60}m` : `${a}s`;
    return `${d > 0 ? "+" : "−"}${unit}`;
  };
  return (
    <div className="timefield">
      <span className="label" style={{ marginBottom: 0 }}>{label}</span>
      <div className="timefield-box">
        <button type="button" onClick={() => onChange(clamp(value + steps[0]))} aria-label={fmtStep(steps[0])}>{fmtStep(steps[0])}</button>
        <input inputMode="numeric" value={text} onChange={(e) => setText(e.target.value)} onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
        <button type="button" onClick={() => onChange(clamp(value + steps[steps.length - 1]))} aria-label={fmtStep(steps[steps.length - 1])}>
          {fmtStep(steps[steps.length - 1])}
        </button>
      </div>
      {steps.length > 2 && (
        <div className="nudges">
          {steps.slice(1, -1).map((d) => (
            <button type="button" key={d} onClick={() => onChange(clamp(value + d))}>{fmtStep(d)}</button>
          ))}
        </div>
      )}
    </div>
  );
}
