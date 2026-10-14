"use client";
import { useState } from "react";
import {
  Anchor,
  RectangleHorizontal,
  Scale,
  Scissors,
  SquareDashed,
  Box,
  Circle,
  Cylinder,
  Grid3x3,
  History,
  Layers,
  Lock,
  LockOpen,
  Ruler,
  Shapes,
  SlidersHorizontal,
  Square,
  TriangleAlert,
  X,
} from "lucide-react";
import { CadState, CompareResult, DatumLetter, Dimension, Feature, Lock as LockT, Pattern } from "@/types/cad";
import AnalysisPanel from "./AnalysisPanel";
import VersionTree from "./VersionTree";
import { Units, useUnits } from "@/lib/units";

interface ModelPanelProps {
  cad: CadState;
  /** Currently selected IDs (drawn highlighted). */
  selection: string[];
  /** Select a component (feature, part, pattern); `additive` = Shift/Ctrl held. */
  onSelect: (id: string, additive: boolean) => void;
  /** Dimension clicked: select it and put "ID = " in the message box. */
  onDimension: (id: string) => void;
  /** Insert text into the message box (parameters, sketch constraints). */
  onInsert: (text: string) => void;
  /** Lock / unlock a dimension or feature at its current value. */
  onLock: (target: string, locked: boolean, mode?: "value" | "centered", axis?: "X" | "Y" | "Z") => void;
  /** Reset a datum letter to its default face. */
  onDatum: (letter: DatumLetter, planeId: string) => void;
  /** Version tree. */
  onCheckout: (v: number) => void;
  onRename: (v: number, name: string) => void;
  onCompare: (a: number, b: number) => void;
  compare: CompareResult | null;
  onClearCompare: () => void;
  /** Analysis tab. */
  onRunTool: (tool: string, args: Record<string, unknown>, label: string) => Promise<void>;
  setMaterial: (part: string, material: string) => Promise<void>;
  downloadFile: (path: string, name: string) => Promise<void>;
  onError: (msg: string) => void;
}

type Pick = (id: string, additive: boolean) => void;

const n = (v?: number, d = 2) => (v === undefined || v === null ? "—" : Number(v.toFixed(d)).toString());

function IdChip({ id, onPick, selected }: { id: string; onPick: Pick; selected?: boolean }) {
  return (
    <button
      onClick={(e) => onPick(id, e.shiftKey || e.ctrlKey || e.metaKey)}
      title={`Select ${id} (Shift+click adds to the selection)`}
      className={`font-mono text-[11px] font-bold px-1.5 py-0.5 rounded transition-colors shrink-0 ${
        selected
          ? "bg-amber-500/25 text-amber-700 dark:text-amber-300 ring-1 ring-amber-500/60"
          : "bg-purple-500/15 text-purple-600 dark:text-purple-300 hover:bg-purple-500/30"
      }`}
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

function Row({ f, onPick, selected, children }: { f: Feature; onPick: Pick; selected: Set<string>; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2 text-[11px] leading-snug py-0.5">
      <IdChip id={f.id} onPick={onPick} selected={selected.has(f.id)} />
      <div className="min-w-0 text-foreground/70">{children}</div>
    </div>
  );
}

function patternText(p: Pattern, u: Units) {
  const holes = `${p.count} × Ø${u.len(p.hole_diameter, true)}`;
  if (p.type === "circular")
    return `${holes} on PCD ${u.len(p.pcd)}${p.full_circle ? "" : `, ${u.ang(p.angle_step_deg)} apart`}`;
  if (p.type === "grid") return `${holes} grid ${p.count_u}×${p.count_v}, ${u.len(p.spacing_u, true)} × ${u.len(p.spacing_v)}`;
  return `${holes} in a row, ${u.len(p.spacing)} apart`;
}

/** "A 10.000 · B 15.000 · C 20.000" (distances from the datum faces). */
export function datumText(f: Feature, u: Units) {
  const d = f.datum_position;
  if (!d) return "";
  return (["A", "B", "C"] as const)
    .filter((k) => d[k] !== undefined)
    .map((k) => `${k} ${u.len(d[k], true)}`)
    .join(" · ");
}

function FeaturesTab({ cad, selection, onSelect }: ModelPanelProps) {
  const u = useUnits();
  const f = cad.info.features;
  const planes = (f.plane ?? []).filter((p) => !p.is_cap).sort((a, b) => (b.area ?? 0) - (a.area ?? 0));
  // Counterbores are shown on the hole they belong to.
  const holes = (f.hole ?? []).filter((h) => !h.counterbore_of);
  const patterns = cad.info.patterns ?? [];
  const unrecognized = cad.info.objects.filter((o) => o.unrecognized_faces);
  const pick = onSelect;
  const selected = new Set(selection);
  return (
    <div className="space-y-3">
      {unrecognized.length > 0 && (
        <p className="flex items-start gap-1.5 text-[10px] text-amber-600 dark:text-amber-400 px-1">
          <TriangleAlert size={12} className="shrink-0 mt-px" />
          <span>
            {unrecognized.map((o) => `${o.label}: ${o.unrecognized_faces}`).join(", ")} free-form face(s) not classified;
            they can still be measured, but not edited as features.
          </span>
        </p>
      )}
      <Section title="Parts" icon={<Box size={13} />} count={cad.info.objects.length}>
        {cad.info.objects.map((o) => (
          <div key={o.name} className="flex items-start gap-2 text-[11px] leading-snug py-0.5">
            {o.id ? <IdChip id={o.id} onPick={pick} selected={selected.has(o.id)} /> : null}
            <div className="min-w-0 text-foreground/70">
              <span className="text-foreground">{o.label}</span>
              {o.parent ? <span className="text-foreground/40"> · in {o.parent}</span> : null}
              <span className="block text-foreground/40 font-mono">
                {u.len(o.bbox.size[0], true)} × {u.len(o.bbox.size[1], true)} × {u.len(o.bbox.size[2])} · {o.faces} faces
              </span>
            </div>
          </div>
        ))}
      </Section>
      <Section title="Hole patterns" icon={<Grid3x3 size={13} />} count={patterns.length}>
        {patterns.map((p) => (
          <div key={p.id} className="flex items-start gap-2 text-[11px] leading-snug py-0.5">
            <IdChip id={p.id} onPick={pick} selected={selected.has(p.id)} />
            <div className="min-w-0 text-foreground/70">
              <span className="text-foreground">{patternText(p, u)}</span>
              <span className="block text-foreground/40 font-mono">{p.holes.join(" ")}</span>
            </div>
          </div>
        ))}
      </Section>
      <Section title="Holes" icon={<Circle size={13} />} count={holes.length}>
        {holes.map((h) => (
          <Row key={h.id} f={h} onPick={pick} selected={selected}>
            <span className="font-semibold text-foreground">Ø{u.len(h.diameter)}</span>{" "}
            {h.through ? "through" : `× ${u.len(h.depth)} deep`}
            {h.countersinks?.length ? ` · csk Ø${u.len(h.countersinks[0].outer_diameter, true)}` : ""}
            {h.counterbore && (
              <>
                {" · cbore "}
                <button
                  onClick={(e) => h.counterbore?.feature && pick(h.counterbore.feature, e.shiftKey)}
                  className="hover:underline"
                  title={h.counterbore.feature ? `Counterbore ${h.counterbore.feature}` : undefined}
                >
                  Ø{u.len(h.counterbore.diameter, true)} × {u.len(h.counterbore.depth, true)}
                </button>
              </>
            )}
            {h.pattern ? <span className="text-foreground/40"> · {h.pattern}</span> : null}
            {h.thread ? (
              <span className="text-sky-700 dark:text-sky-400" title={h.thread.note ?? h.thread.source}>
                {" "}· thread {h.thread.spec}
                {h.thread.depth ? ` × ${u.len(h.thread.depth, true)}` : ""}
              </span>
            ) : null}
            {h.standard ? (
              <span className="block text-emerald-700 dark:text-emerald-400" title={(h.standards ?? []).join("\n")}>
                = {h.standard}
              </span>
            ) : null}
            <span className="block text-foreground/40 font-mono">
              {datumText(h, u) || `@ ${u.vec(h.entry)}`} · {h.position}
            </span>
          </Row>
        ))}
      </Section>
      <Section title="Slots" icon={<RectangleHorizontal size={13} />} count={f.slot?.length ?? 0}>
        {f.slot?.map((x) => (
          <Row key={x.id} f={x} onPick={pick} selected={selected}>
            <span className="font-semibold text-foreground">
              {u.len(x.width, true)} × {u.len(x.length)}
            </span>{" "}
            {x.shape} · {x.through ? "through" : `${u.len(x.depth)} deep`}
            <span className="block text-foreground/40 font-mono">
              @ {u.vec(x.center)} · on {x.face}
            </span>
          </Row>
        ))}
      </Section>
      <Section title="Pockets" icon={<SquareDashed size={13} />} count={f.pocket?.length ?? 0}>
        {f.pocket?.map((x) => (
          <Row key={x.id} f={x} onPick={pick} selected={selected}>
            <span className="font-semibold text-foreground">
              {u.len(x.length, true)} × {u.len(x.width, true)} × {u.len(x.depth)} deep
            </span>
            {x.corner_radius ? ` · corners R${u.len(x.corner_radius, true)}` : " · sharp corners"}
            <span className="block text-foreground/40 font-mono">
              @ {u.vec(x.center)} · on {x.face}
            </span>
          </Row>
        ))}
      </Section>
      <Section title="Chamfers" icon={<Scissors size={13} />} count={f.chamfer?.length ?? 0}>
        {f.chamfer?.map((x) => (
          <Row key={x.id} f={x} onPick={pick} selected={selected}>
            <span className="font-semibold text-foreground">
              {x.size !== null && x.size !== undefined
                ? u.len(x.size)
                : `${u.len(x.distances?.[0], true)} × ${u.len(x.distances?.[1])}`}
            </span>{" "}
            × {u.ang(x.angle_deg)} · {u.len(x.length)} long
            <span className="block text-foreground/40 font-mono">between {(x.faces ?? []).join(" / ")}</span>
          </Row>
        ))}
      </Section>
      <Section title="Bosses / cylinders" icon={<Cylinder size={13} />} count={f.boss?.length ?? 0}>
        {f.boss?.map((c) => (
          <Row key={c.id} f={c} onPick={pick} selected={selected}>
            <span className="font-semibold text-foreground">Ø{u.len(c.diameter)}</span> × {u.len(c.length)} long
            <span className="block text-foreground/40 font-mono">@ {u.vec(c.center)}</span>
          </Row>
        ))}
      </Section>
      <Section title="Fillets / rounds" icon={<Circle size={13} className="opacity-60" />} count={f.round?.length ?? 0}>
        {f.round?.map((r) => (
          <Row key={r.id} f={r} onPick={pick} selected={selected}>
            <span className="font-semibold text-foreground">R{u.len(r.radius)}</span>
            {r.chain_faces ? <span className="text-foreground/50"> · chain of {r.chain_faces} faces</span> : null}
            {r.toroidal ? <span className="text-foreground/50"> · ring (toroidal)</span> : null}
            <span className="block text-foreground/40 font-mono">@ {u.vec(r.center)}</span>
          </Row>
        ))}
      </Section>
      <Section title="Planar faces" icon={<Square size={13} />} count={planes.length}>
        {planes.slice(0, 60).map((p) => (
          <Row key={p.id} f={p} onPick={pick} selected={selected}>
            <span className="text-foreground">{p.label}</span> · {n(p.area, 1)} mm²
            <span className="block text-foreground/40 font-mono">n = {(p.normal ?? []).map((x) => n(x, 3)).join(", ")}</span>
          </Row>
        ))}
      </Section>
      <Section title="Free-form faces" icon={<Shapes size={13} />} count={f.face?.length ?? 0}>
        {f.face?.map((x) => (
          <Row key={x.id} f={x} onPick={pick} selected={selected}>
            <span className="text-foreground">{x.surface ?? "surface"}</span> · {n(x.area, 1)} mm²
            <span className="block text-foreground/40 font-mono">@ {u.vec(x.center)}</span>
          </Row>
        ))}
      </Section>
      {!!f.cone?.length && (
        <Section title="Cones" icon={<Layers size={13} />} count={f.cone.length}>
          {f.cone.map((k) => (
            <Row key={k.id} f={k} onPick={pick} selected={selected}>
              half-angle {u.ang(k.half_angle_deg as number)}
              <span className="block text-foreground/40 font-mono">@ {u.vec(k.center)}</span>
            </Row>
          ))}
        </Section>
      )}
    </div>
  );
}

const DIM_GROUPS: { type: Dimension["type"]; title: string }[] = [
  { type: "extent", title: "Overall sizes" },
  { type: "thickness", title: "Thicknesses" },
  { type: "gap", title: "Gaps / slots" },
  { type: "edge_distance", title: "Hole positions" },
  { type: "center_distance", title: "Hole to hole" },
  { type: "angle", title: "Angles" },
  { type: "diameter", title: "Diameters" },
  { type: "radius", title: "Radii" },
  { type: "coaxial", title: "Coaxiality" },
];

export const dimValue = (d: Dimension, u: Units) => (d.unit === "deg" ? u.ang(d.value) : u.len(d.value, true));

export function LockButton({ locked, onClick, title }: { locked: boolean; onClick: () => void; title?: string }) {
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      title={title ?? (locked ? "Locked: edits may not change it. Click to unlock" : "Lock: keep this value through every edit")}
      className={`p-0.5 rounded shrink-0 ${locked ? "text-amber-600 dark:text-amber-400" : "text-foreground/25 hover:text-foreground/60"}`}
    >
      {locked ? <Lock size={11} /> : <LockOpen size={11} />}
    </button>
  );
}

function LocksAndDatums({ cad, onSelect, onLock, onDatum }: ModelPanelProps) {
  const locks: LockT[] = cad.locks ?? [];
  const datums = cad.info.datums ?? {};
  return (
    <div className="rounded-2xl border border-border bg-foreground/[0.02] px-3 py-2 space-y-1.5 text-[11px]">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="flex items-center gap-1 text-foreground/50 font-semibold" title="Hole positions are measured from these faces">
          <Anchor size={12} /> Datums
        </span>
        {(["A", "B", "C"] as const).map((L) =>
          datums[L] ? (
            <span key={L} className="flex items-center gap-0.5" title={`${datums[L]!.label}${datums[L]!.explicit ? "" : " (default)"}`}>
              <span className="font-mono font-black text-foreground/70">{L}</span>
              <IdChip id={datums[L]!.id} onPick={onSelect} />
              {datums[L]!.explicit && (
                <button
                  onClick={() => onDatum(L, "default")}
                  className="text-foreground/30 hover:text-foreground"
                  title={`Reset datum ${L} to the default face`}
                >
                  <X size={10} />
                </button>
              )}
            </span>
          ) : null
        )}
        {!Object.keys(datums).length && <span className="text-foreground/40">none (no planar faces)</span>}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="flex items-center gap-1 text-foreground/50 font-semibold">
          <Lock size={12} /> Locks
        </span>
        {locks.length === 0 && <span className="text-foreground/40">none: click a lock icon to keep a value</span>}
        {locks.map((lk) => (
          <span
            key={`${lk.target}:${lk.mode}:${lk.axis ?? ""}`}
            className="flex items-center gap-1 rounded-full bg-amber-500/10 text-amber-700 dark:text-amber-300 px-2 py-0.5 font-mono text-[10px]"
          >
            {lk.label}
            <button onClick={() => onLock(lk.target, false, lk.mode)} title="Unlock" className="hover:text-foreground">
              <X size={10} />
            </button>
          </span>
        ))}
      </div>
    </div>
  );
}

function DimensionsTab(props: ModelPanelProps) {
  const { cad, selection, onDimension, onLock } = props;
  const u = useUnits();
  const dims = cad.info.dimensions ?? [];
  const objects = new Map(cad.info.objects.map((o) => [o.name, o.label]));
  const multi = cad.info.objects.length > 1;
  const locked = new Set((cad.locks ?? []).filter((l) => l.mode === "value").map((l) => l.target));
  return (
    <div className="space-y-3">
      <LocksAndDatums {...props} />
      {!dims.length ? (
        <p className="text-xs text-foreground/40 p-2">
          No editable dimensions were found (they come from parallel planar faces and holes).
        </p>
      ) : (
        <p className="text-[10px] text-foreground/40 px-1">
          Click a dimension to insert <span className="font-mono">P1-P2 =</span> into the message, then type the new value
          ({u.unit}, or with a unit like <span className="font-mono">1/4&quot;</span>). Values in {u.unit}; angles in
          degrees. The lock icon keeps a value through every edit.
        </p>
      )}
      {DIM_GROUPS.map(({ type, title }) => {
        const rows = dims.filter((d) => d.type === type);
        return (
          <Section key={type} title={title} icon={<Ruler size={13} />} count={rows.length}>
            {rows.map((d) => (
              <div
                key={d.id}
                className={`w-full flex items-baseline gap-1.5 text-[11px] leading-snug py-0.5 px-1 rounded ${
                  selection.includes(d.id) ? "bg-amber-500/15 ring-1 ring-amber-500/50" : "hover:bg-foreground/5"
                }`}
              >
                <LockButton locked={locked.has(d.id)} onClick={() => onLock(d.id, !locked.has(d.id))} />
                <button
                  onClick={() => onDimension(d.id)}
                  title={`Set ${d.id} (${d.label})`}
                  className="flex-1 min-w-0 flex items-baseline gap-2 text-left"
                >
                  <span className="font-mono text-[11px] font-bold text-purple-600 dark:text-purple-300 shrink-0">{d.id}</span>
                  <span className="min-w-0 flex-1 text-foreground/60 truncate">
                    {d.label}
                    {multi ? ` · ${objects.get(d.object) ?? d.object}` : ""}
                  </span>
                  <span className="font-mono font-semibold text-foreground shrink-0">{dimValue(d, u)}</span>
                </button>
              </div>
            ))}
          </Section>
        );
      })}
    </div>
  );
}

function ParamsTab({ cad, onInsert }: ModelPanelProps) {
  const { parametric, sketches, spreadsheets } = cad.info;
  if (!parametric.length && !sketches.length && !spreadsheets.length) {
    return (
      <p className="text-xs text-foreground/40 p-2">
        No parametric data. This is a plain B-rep model (STEP/IGES/BREP), so edits work directly on faces and features
        (see the Dimensions tab).
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
                onClick={() => onInsert(`${o.name}.${k}`)}
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
              onClick={() => onInsert(`${s.name} constraint ${c.name || c.index}`)}
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

export default function ModelPanel(props: ModelPanelProps) {
  const { cad } = props;
  const u = useUnits();
  const [tab, setTab] = useState<"features" | "dimensions" | "params" | "history" | "analysis">("features");
  const bb = cad.info.bbox;
  const volume = cad.info.objects.reduce((s, o) => s + o.volume, 0);
  const tabs = [
    { key: "features" as const, label: "Features", icon: <Layers size={12} /> },
    { key: "dimensions" as const, label: "Dimensions", icon: <Ruler size={12} /> },
    { key: "params" as const, label: "Params", icon: <SlidersHorizontal size={12} /> },
    { key: "history" as const, label: "History", icon: <History size={12} /> },
    { key: "analysis" as const, label: "Analysis", icon: <Scale size={12} /> },
  ];

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-2 text-center">
        {bb &&
          (["X", "Y", "Z"] as const).map((axis, i) => (
            <div key={axis} className="rounded-xl border border-border py-2">
              <p className="text-[9px] uppercase tracking-widest text-foreground/40">
                {axis} size ({u.unit})
              </p>
              <p className="font-mono text-sm font-bold">{u.len(bb.size[i], true)}</p>
            </div>
          ))}
      </div>
      <p className="text-[10px] text-foreground/40 font-mono text-center">
        {cad.info.objects.length} object{cad.info.objects.length === 1 ? "" : "s"} · volume{" "}
        {u.unit === "in" ? `${n(volume / 16387.064, 3)} in³` : `${n(volume, 1)} mm³`} · stored in mm
      </p>

      <div className="flex gap-1 p-1 rounded-xl bg-foreground/5">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex-1 min-w-0 flex items-center justify-center gap-1 text-[10px] font-semibold py-1.5 rounded-lg transition-colors ${
              tab === t.key ? "bg-background shadow text-foreground" : "text-foreground/50 hover:text-foreground"
            }`}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      {tab === "features" && <FeaturesTab {...props} />}
      {tab === "dimensions" && <DimensionsTab {...props} />}
      {tab === "params" && <ParamsTab {...props} />}
      {tab === "history" && (
        <VersionTree
          cad={cad}
          onCheckout={props.onCheckout}
          onRename={props.onRename}
          onCompare={props.onCompare}
          compare={props.compare}
          onClearCompare={props.onClearCompare}
        />
      )}
      {tab === "analysis" && (
        <AnalysisPanel
          cad={cad}
          onSelect={props.onSelect}
          onRunTool={props.onRunTool}
          setMaterial={props.setMaterial}
          downloadFile={props.downloadFile}
          onError={props.onError}
        />
      )}
    </div>
  );
}
