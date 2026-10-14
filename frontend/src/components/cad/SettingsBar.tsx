"use client";
import { useState } from "react";
import { Target } from "lucide-react";
import { SessionSettings } from "@/types/cad";

interface SettingsBarProps {
  settings: SessionSettings;
  onChange: (patch: { display_unit?: "mm" | "in"; precision?: number; tolerance_length?: number; tolerance_angle?: number; min_wall?: number }) => void;
}

/** Display unit, decimals and acceptance tolerance of the session (the model is always stored in mm). */
export default function SettingsBar({ settings, onChange }: SettingsBarProps) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[10px] text-foreground/60">
      <div className="flex rounded-lg bg-foreground/5 border border-border p-0.5 font-semibold" title="Display unit (edits are always made in mm)">
        {(["mm", "in"] as const).map((unit) => (
          <button
            key={unit}
            onClick={() => unit !== settings.display_unit && onChange({ display_unit: unit })}
            className={`px-2 py-0.5 rounded-md ${settings.display_unit === unit ? "bg-purple-600 text-white" : "hover:text-foreground"}`}
          >
            {unit}
          </button>
        ))}
      </div>
      <label className="flex items-center gap-1" title="Decimals shown (inch shows one more)">
        decimals
        <select
          value={settings.precision}
          onChange={(e) => onChange({ precision: Number(e.target.value) })}
          className="bg-background border border-border rounded px-1 py-0.5 font-mono"
        >
          {[2, 3, 4].map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </label>
      <span className="flex items-center gap-1" title="An edit whose measured result misses the requested value by more than this is rolled back">
        <Target size={11} /> tolerance ±
        <ToleranceInput value={settings.tolerance.length} onCommit={(v) => onChange({ tolerance_length: v })} />
        mm ±
        <ToleranceInput value={settings.tolerance.angle} onCommit={(v) => onChange({ tolerance_angle: v })} />°
      </span>
      <span className="flex items-center gap-1" title="Pre-edit check: warn when an edit makes a wall thinner than this (0 = off)">
        min wall
        <ToleranceInput value={settings.min_wall ?? 1} onCommit={(v) => onChange({ min_wall: v })} allowZero />
        mm
      </span>
    </div>
  );
}

function ToleranceInput({ value, onCommit, allowZero = false }: { value: number; onCommit: (v: number) => void; allowZero?: boolean }) {
  const [text, setText] = useState(String(value));
  const [shown, setShown] = useState(value);
  if (shown !== value) {
    // the backend changed it (e.g. another session state loaded): show the new value
    setShown(value);
    setText(String(value));
  }
  const commit = () => {
    const v = Number(text);
    if (Number.isFinite(v) && (v > 0 || (allowZero && v === 0)) && v <= (allowZero ? 50 : 10) && v !== value) onCommit(v);
    else setText(String(value));
  };
  return (
    <input
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      inputMode="decimal"
      className="w-14 bg-background border border-border rounded px-1 py-0.5 font-mono text-foreground"
    />
  );
}
