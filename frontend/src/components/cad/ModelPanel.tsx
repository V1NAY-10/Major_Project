"use client";
import { useState } from "react";
import { Circle, Cylinder, History, Layers, SlidersHorizontal, Square } from "lucide-react";
import { CadState, Feature, Vec3 } from "@/types/cad";

interface ModelPanelProps {
  cad: CadState;
  onPick: (ref: string) => void;
}

const n = (v?: number, d = 2) => (v === undefined || v === null ? "—" : Number(v.toFixed(d)).toString());
const vec = (v?: Vec3) => (v ? `${n(v[0], 1)}, ${n(v[1], 1)}, ${n(v[2], 1)}` : "—");

function IdChip({ id, onPick }: { id: string; onPick: (id: string) => void }) {
  return (
    <button
      onClick={() => onPick(id)}
      title={`Insert ${id} into the message`}
      className="font-mono text-[11px] font-bold px-1.5 py-0.5 rounded bg-purple-500/15 text-purple-600 dark:text-purple-300 hover:bg-purple-500/30 transition-colors"
    >
      {id}
    </button>
  );
}

function Section({ title, icon, count, children }: { title: string; icon: React.ReactNode; count: number; children: React.ReactNode }) {
  const [open, setOpen] = useState(true);
  if (!count) return null;
  return (
    <div className="rounded-2xl border border-border bg-foreground/[0.02]">
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-3 py-2 text-xs font-semibold">
        {icon}
        <span>{title}</span>
        <span className="ml-auto text-foreground/40 font-mono">{count}</span>
      </button>
      {open && <div className="px-3 pb-3 space-y-1">{children}</div>}
    </div>
  );
}

function Row({ f, onPick, children }: { f: Feature; onPick: (id: string) => void; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2 text-[11px] leading-snug py-0.5">
      <IdChip id={f.id} onPick={onPick} />
      <div className="min-w-0 text-foreground/70">{children}</div>
    </div>
  );
}

function FeaturesTab({ cad, onPick }: ModelPanelProps) {
  const f = cad.info.features;
  const planes = (f.plane ?? []).filter((p) => !p.is_cap).sort((a, b) => (b.area ?? 0) - (a.area ?? 0));
  return (
    <div className="space-y-3">
      <Section title="Holes" icon={<Circle size={13} />} count={f.hole?.length ?? 0}>
        {f.hole?.map((h) => (
          <Row key={h.id} f={h} onPick={onPick}>
            <span className="font-semibold text-foreground">Ø{n(h.diameter, 3)}</span>{" "}
            {h.through ? "through" : `× ${n(h.depth)} deep`}
            {h.countersinks?.length ? ` · csk Ø${n(h.countersinks[0].outer_diameter)}` : ""}
            <span className="block text-foreground/40 font-mono">@ {vec(h.entry)} · {h.position}</span>
          </Row>
        ))}
      </Section>
      <Section title="Bosses / cylinders" icon={<Cylinder size={13} />} count={f.boss?.length ?? 0}>
        {f.boss?.map((c) => (
          <Row key={c.id} f={c} onPick={onPick}>
            <span className="font-semibold text-foreground">Ø{n(c.diameter, 3)}</span> × {n(c.length)} long
            <span className="block text-foreground/40 font-mono">@ {vec(c.center)}</span>
          </Row>
        ))}
      </Section>
      <Section title="Fillets / rounds" icon={<Circle size={13} className="opacity-60" />} count={f.round?.length ?? 0}>
        {f.round?.map((r) => (
          <Row key={r.id} f={r} onPick={onPick}>
            <span className="font-semibold text-foreground">R{n(r.radius, 3)}</span>
            <span className="block text-foreground/40 font-mono">@ {vec(r.center)}</span>
          </Row>
        ))}
      </Section>
      <Section title="Planar faces" icon={<Square size={13} />} count={planes.length}>
        {planes.slice(0, 60).map((p) => (
          <Row key={p.id} f={p} onPick={onPick}>
            <span className="text-foreground">{p.label}</span> · {n(p.area, 1)} mm²
            <span className="block text-foreground/40 font-mono">n = {vec(p.normal)}</span>
          </Row>
        ))}
      </Section>
      {!!f.cone?.length && (
        <Section title="Cones" icon={<Layers size={13} />} count={f.cone.length}>
          {f.cone.map((k) => (
            <Row key={k.id} f={k} onPick={onPick}>
              half-angle {n(k.half_angle_deg as number)}°<span className="block text-foreground/40 font-mono">@ {vec(k.center)}</span>
            </Row>
          ))}
        </Section>
      )}
    </div>
  );
}

function ParamsTab({ cad, onPick }: ModelPanelProps) {
  const { parametric, sketches, spreadsheets } = cad.info;
  if (!parametric.length && !sketches.length && !spreadsheets.length) {
    return (
      <p className="text-xs text-foreground/40 p-2">
        No parametric data. This is a plain B-rep model (STEP/IGES/BREP), so edits work directly on faces and features.
      </p>
    );
  }
  return (
    <div className="space-y-3">
      {parametric.map((o) => (
        <div key={o.name} className="rounded-2xl border border-border p-3">
          <p className="text-xs font-semibold mb-1">
            {o.label} <span className="text-foreground/40 font-mono text-[10px]">{o.type}</span>
          </p>
          <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
            {Object.entries(o.properties).map(([k, v]) => (
              <button
                key={k}
                onClick={() => onPick(`${o.name}.${k}`)}
                className="flex justify-between text-[11px] hover:bg-foreground/5 rounded px-1 text-left"
              >
                <span className="text-foreground/50">{k}{o.expressions?.[k] ? " ƒ" : ""}</span>
                <span className="font-mono">{String(v)}</span>
              </button>
            ))}
          </div>
        </div>
      ))}
      {sketches.map((s) => (
        <div key={s.name} className="rounded-2xl border border-border p-3">
          <p className="text-xs font-semibold mb-1">{s.label} <span className="text-foreground/40 text-[10px]">sketch</span></p>
          {s.constraints.length === 0 && <p className="text-[11px] text-foreground/40">No dimensional constraints</p>}
          {s.constraints.map((c) => (
            <button
              key={c.index}
              onClick={() => onPick(`${s.name} constraint ${c.name || c.index}`)}
              className="w-full flex justify-between text-[11px] hover:bg-foreground/5 rounded px-1"
            >
              <span className="text-foreground/50">#{c.index} {c.name} {c.type}</span>
              <span className="font-mono">{c.value}{c.type === "Angle" ? "°" : ""}</span>
            </button>
          ))}
        </div>
      ))}
      {spreadsheets.map((s) => (
        <div key={s.name} className="rounded-2xl border border-border p-3">
          <p className="text-xs font-semibold mb-1">{s.label} <span className="text-foreground/40 text-[10px]">spreadsheet</span></p>
          {s.cells.filter((c) => c.alias).map((c) => (
            <div key={c.cell} className="flex justify-between text-[11px] px-1">
              <span className="text-foreground/50">{c.alias} ({c.cell})</span>
              <span className="font-mono">{String(c.value)}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function HistoryTab({ cad }: { cad: CadState }) {
  return (
    <ol className="space-y-1.5">
      {[...cad.versions].reverse().map((v) => (
        <li
          key={v.v}
          className={`rounded-xl border px-3 py-2 text-[11px] ${v.v === cad.version ? "border-purple-500/40 bg-purple-500/10" : "border-border"}`}
        >
          <div className="flex justify-between font-mono text-foreground/50">
            <span className="font-bold text-foreground">v{v.v}{v.v === cad.version ? " (current)" : ""}</span>
            <span>{new Date(v.created_at).toLocaleTimeString()}</span>
          </div>
          <p className="text-foreground/70 mt-0.5">{v.description}</p>
        </li>
      ))}
    </ol>
  );
}

export default function ModelPanel({ cad, onPick }: ModelPanelProps) {
  const [tab, setTab] = useState<"features" | "params" | "history">("features");
  const bb = cad.info.bbox;
  const volume = cad.info.objects.reduce((s, o) => s + o.volume, 0);
  const tabs = [
    { key: "features" as const, label: "Features", icon: <Layers size={12} /> },
    { key: "params" as const, label: "Parameters", icon: <SlidersHorizontal size={12} /> },
    { key: "history" as const, label: "History", icon: <History size={12} /> },
  ];

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-2 text-center">
        {bb &&
          (["X", "Y", "Z"] as const).map((axis, i) => (
            <div key={axis} className="rounded-xl border border-border py-2">
              <p className="text-[9px] uppercase tracking-widest text-foreground/40">{axis} size</p>
              <p className="font-mono text-sm font-bold">{n(bb.size[i], 3)}</p>
            </div>
          ))}
      </div>
      <p className="text-[10px] text-foreground/40 font-mono text-center">
        {cad.info.objects.length} object{cad.info.objects.length === 1 ? "" : "s"} · volume {n(volume, 1)} mm³ · mm
      </p>

      <div className="flex gap-1 p-1 rounded-xl bg-foreground/5">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex-1 flex items-center justify-center gap-1.5 text-[11px] font-semibold py-1.5 rounded-lg transition-colors ${
              tab === t.key ? "bg-background shadow text-foreground" : "text-foreground/50 hover:text-foreground"
            }`}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      {tab === "features" && <FeaturesTab cad={cad} onPick={onPick} />}
      {tab === "params" && <ParamsTab cad={cad} onPick={onPick} />}
      {tab === "history" && <HistoryTab cad={cad} />}
    </div>
  );
}
