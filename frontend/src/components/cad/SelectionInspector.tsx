"use client";
import { useState } from "react";
import { Anchor, Box, Crosshair, Loader2, MousePointerClick, Ruler, X } from "lucide-react";
import { CadObject, CadState, DatumLetter, Dimension, EdgeInfo, Feature, ModelInfo, Pattern, Vec3, WallThickness } from "@/types/cad";
import { apiFetch } from "@/lib/api";
import { Units, useUnits } from "@/lib/units";
import { datumText, dimValue, LockButton } from "./ModelPanel";

/** What a selected ID refers to in the parsed model. */
export type Entity =
  | { type: "part"; id: string; obj: CadObject }
  | { type: "feature"; id: string; f: Feature }
  | { type: "pattern"; id: string; p: Pattern }
  | { type: "dimension"; id: string; d: Dimension }
  | { type: "edge"; id: string; e: EdgeInfo }
  | { type: "unknown"; id: string };

const n = (v?: number, d = 3) => (v === undefined || v === null ? "—" : Number(v.toFixed(d)).toString());
const dir = (v?: Vec3) => (v ? `${n(v[0], 3)}, ${n(v[1], 3)}, ${n(v[2], 3)}` : "—");

export function entityIndex(info: ModelInfo, edges?: Map<string, EdgeInfo>): Map<string, Entity> {
  const m = new Map<string, Entity>();
  for (const [id, e] of edges ?? []) m.set(id, { type: "edge", id, e });
  for (const o of info.objects) if (o.id) m.set(o.id, { type: "part", id: o.id, obj: o });
  for (const fs of Object.values(info.features)) for (const f of fs ?? []) m.set(f.id, { type: "feature", id: f.id, f });
  for (const p of info.patterns ?? []) m.set(p.id, { type: "pattern", id: p.id, p });
  for (const d of info.dimensions ?? []) m.set(d.id, { type: "dimension", id: d.id, d });
  return m;
}

const KIND_NAME: Record<Feature["kind"], string> = {
  hole: "Hole",
  boss: "Boss / cylinder",
  round: "Fillet / round",
  plane: "Planar face",
  cone: "Cone",
  sphere: "Sphere",
  face: "Free-form face",
  slot: "Slot",
  pocket: "Pocket",
  chamfer: "Chamfer",
};

/** One-line summary, e.g. "Hole Ø6 through" (also used for the chips in the chat input). */
export function summary(e: Entity | undefined, u?: Units): string {
  if (!e) return "";
  const len = (v?: number) => (u ? u.len(v) : n(v));
  switch (e.type) {
    case "part":
      return `Part “${e.obj.label}”`;
    case "pattern":
      return `${e.p.type} pattern, ${e.p.count} holes`;
    case "dimension":
      return `${e.d.label} = ${u ? dimValue(e.d, u) : n(e.d.value)}`;
    case "edge":
      return `Edge (${e.e.kind}, ${e.e.convexity}) ${len(e.e.length)}`;
    case "unknown":
      return "not in the current model";
  }
  const f = e.f;
  if (f.kind === "hole") return `Hole Ø${len(f.diameter)} ${f.through ? "through" : `× ${len(f.depth)}`}`;
  if (f.kind === "boss") return `Boss Ø${len(f.diameter)} × ${len(f.length)}`;
  if (f.kind === "round") return `Fillet R${len(f.radius)}`;
  if (f.kind === "plane") return f.label ? `Face: ${f.label}` : "Planar face";
  if (f.kind === "sphere") return `Sphere R${len(f.radius)}`;
  if (f.kind === "slot") return `Slot ${len(f.width)} × ${len(f.length)}`;
  if (f.kind === "pocket") return `Pocket ${len(f.depth)} deep`;
  if (f.kind === "chamfer") return `Chamfer ${f.size ? len(f.size) : ""}`.trim();
  return KIND_NAME[f.kind];
}

type Row = [string, React.ReactNode];

function featureRows(f: Feature, info: ModelInfo, u: Units): Row[] {
  const rows: Row[] = [];
  const add = (label: string, v: React.ReactNode) => {
    if (v !== undefined && v !== null && v !== "" && v !== "—") rows.push([label, v]);
  };
  add("Diameter", f.diameter !== undefined ? `Ø ${u.len(f.diameter)}` : undefined);
  if (f.diameter === undefined) add("Radius", f.radius !== undefined ? `R ${u.len(f.radius)}` : undefined);
  if (f.kind === "hole" || f.kind === "slot")
    add("Depth", f.through ? `through (${u.len(f.depth)})` : u.len(f.depth));
  if (f.kind === "pocket") add("Depth", u.len(f.depth));
  add("Width", f.width !== undefined ? u.len(f.width) : undefined);
  if (f.kind !== "hole") add("Length", f.length !== undefined ? u.len(f.length) : undefined);
  add("Outline", f.shape);
  add("Corner radius", f.kind === "pocket" ? (f.corner_radius ? `R ${u.len(f.corner_radius)}` : "sharp") : undefined);
  if (f.kind === "chamfer") {
    add("Size", f.size !== null && f.size !== undefined ? u.len(f.size) : `${u.len(f.distances?.[0])} × ${u.len(f.distances?.[1])}`);
    add("Angle", u.ang(f.angle_deg));
    add("Between", (f.faces ?? []).join(" / "));
  }
  add("Opens on", f.face);
  add("Thread", f.thread ? `${f.thread.spec}${f.thread.depth ? ` × ${u.len(f.thread.depth)}` : ""}${f.thread.note ? ` ⚠ ${f.thread.note}` : ""}` : undefined);
  add("Fillet chain", f.chain_faces ? `${f.chain_faces} tangent faces` : undefined);
  add("Ring fillet", f.toroidal ? "toroidal" : undefined);
  add("Standard", f.standard);
  add("Drill tip", f.drill_tip ? "yes" : undefined);
  if (f.countersinks?.length)
    add("Countersink", `Ø ${u.len(f.countersinks[0].outer_diameter)} × ${u.ang(f.countersinks[0].angle_deg)}`);
  if (f.counterbore) add("Counterbore", `Ø ${u.len(f.counterbore.diameter)} × ${u.len(f.counterbore.depth)} deep`);
  add("Counterbore of", f.counterbore_of);
  add("Half-angle", f.half_angle_deg !== undefined ? u.ang(f.half_angle_deg as number) : undefined);
  add("Area", f.area !== undefined ? `${n(f.area, 2)} mm²` : undefined);
  add("Face", f.label);
  add("Surface", f.surface);
  add("Normal", f.normal ? dir(f.normal) : undefined);
  add("Axis", f.axis ? dir(f.axis as Vec3) : f.direction ? dir(f.direction as Vec3) : undefined);
  add("From datums", datumText(f, u));
  add("Entry", f.entry ? u.vec(f.entry) : undefined);
  add("Centre", f.center ? u.vec(f.center) : undefined);
  add("Position", f.position);
  add("Pattern", f.pattern);
  const datum = (Object.entries(info.datums ?? {}) as [DatumLetter, { id: string }][]).find(([, d]) => d.id === f.id);
  add("Datum", datum ? `${datum[0]}` : undefined);
  const obj = info.objects.find((o) => o.name === f.object);
  if (obj && info.objects.length > 1) add("Part", `${obj.id ?? ""} ${obj.label}`.trim());
  return rows;
}

function partRows(o: CadObject, info: ModelInfo, u: Units): Row[] {
  const counts: Record<string, number> = {};
  for (const fs of Object.values(info.features))
    for (const f of fs ?? []) if (f.object === o.name && !f.is_cap) counts[f.kind] = (counts[f.kind] ?? 0) + 1;
  return [
    ["Label", o.label],
    ["FreeCAD name", <span key="n" className="font-mono">{o.name}</span>],
    ["Type", o.type],
    ...(o.parent ? ([["Assembly", o.parent]] as Row[]) : []),
    ["Size X × Y × Z", `${u.len(o.bbox.size[0], true)} × ${u.len(o.bbox.size[1], true)} × ${u.len(o.bbox.size[2])}`],
    ["Min corner", u.vec(o.bbox.min)],
    ["Volume", `${n(o.volume, 1)} mm³`],
    ["Area", `${n(o.area, 1)} mm²`],
    ["Topology", `${o.solids} solid · ${o.faces} faces · ${o.edges} edges`],
    ["Features", Object.entries(counts).map(([k, c]) => `${c} ${k}`).join(", ") || "—"],
    ...(o.unrecognized_faces ? ([["Free-form faces", `${o.unrecognized_faces}`]] as Row[]) : []),
  ];
}

function patternRows(p: Pattern, u: Units): Row[] {
  const rows: Row[] = [
    ["Type", p.type],
    ["Holes", `${p.count} × Ø ${u.len(p.hole_diameter)}`],
  ];
  if (p.type === "circular") {
    rows.push(["PCD", u.len(p.pcd)], ["Angle step", u.ang(p.angle_step_deg)]);
    rows.push(["Start angle", u.ang(p.start_angle_deg)], ["Centre", u.vec(p.center)]);
  } else if (p.type === "grid") {
    rows.push(["Grid", `${p.count_u} × ${p.count_v}`], ["Spacing", `${u.len(p.spacing_u, true)} × ${u.len(p.spacing_v)}`]);
  } else {
    rows.push(["Spacing", u.len(p.spacing)], ["Direction", dir(p.direction)]);
  }
  return rows;
}

/** Prompt starters for the selected component: [button label, text put in the message box]. */
function actions(e: Entity): [string, string][] {
  if (e.type === "part") return [["Move", "Move it by [x, y, z] mm: "], ["Scale", "Scale it by "], ["Delete", "Delete this part"]];
  if (e.type === "dimension") return [["Set value", "= "]];
  if (e.type === "pattern") {
    if (e.p.type === "circular") return [["PCD", "pcd "], ["Hole count", "count "], ["Start angle", "start_angle "]];
    if (e.p.type === "grid") return [["Spacing X", "spacing_u "], ["Spacing Y", "spacing_v "]];
    return [["Spacing", "spacing "], ["Hole count", "count "]];
  }
  if (e.type === "unknown") return [];
  if (e.type === "edge") return [["Fillet", "R"], ["Chamfer", "C"]];
  switch (e.f.kind) {
    case "slot":
      return [["Width", "width "], ["Length", "length "], ...(e.f.through ? [] : ([["Depth", "depth "]] as [string, string][])),
        ["Move", "Move it by [x, y, z] mm: "], ["Remove", "Remove this slot"]];
    case "pocket":
      return [["Depth", "depth "], ["Corner radius", "corner R"], ["Remove", "Remove this pocket"]];
    case "chamfer":
      return [["Size", "size "], ["To fillet", "to fillet R"], ["Remove", "Remove this chamfer"]];
    case "hole":
      return [
        ["Diameter", "Ø"],
        ["Clearance", "M clearance"],
        ["Tap", "M tap"],
        ["Move", "Move it by [x, y, z] mm: "],
        ["From datums", "Move it to  from datum B and  from datum C"],
        ["Chamfer", "Add a chamfer of  mm to its entry"],
        ["Thread", "thread M"],
        ["Remove", "Remove this hole"],
      ];
    case "plane":
      return [["Push / pull", "Push this face out by  mm"], ["Fillet edges", "Fillet the edges of this face with R "]];
    case "boss":
      return [["Diameter", "Ø"], ["Length", "Make it  mm long"], ["Remove", "Remove this boss"]];
    case "round":
      return [["Radius", "R"], ["Remove", "Remove this fillet"]];
    default:
      return [["Remove", "Remove this feature"]];
  }
}

function relatedDims(e: Entity, info: ModelInfo): Dimension[] {
  const dims = info.dimensions ?? [];
  if (e.type === "feature") return dims.filter((d) => d.refs.includes(e.id));
  if (e.type === "part") return dims.filter((d) => d.object === e.obj.name && d.type === "extent");
  if (e.type === "pattern") return dims.filter((d) => d.refs.some((r) => e.p.holes.includes(r)));
  return [];
}

function Chip({ id, onClick }: { id: string; onClick: (e: React.MouseEvent) => void }) {
  return (
    <button
      onClick={onClick}
      className="font-mono text-[10px] font-bold px-1 py-px rounded bg-purple-500/15 text-purple-600 dark:text-purple-300 hover:bg-purple-500/30"
    >
      {id}
    </button>
  );
}

interface InspectorProps {
  cad: CadState;
  selection: string[];
  /** Data of edges (E#) seen in picks and highlights (edges aren't part of the model info). */
  edges?: Map<string, EdgeInfo>;
  onSelect: (id: string, additive: boolean) => void;
  onRemove: (id: string) => void;
  onClear: () => void;
  /** Put a prompt starter in the message box (the selection stays the target). */
  onAction: (text: string) => void;
  /** Dimension clicked: select it and put "ID = " in the message box. */
  onDimension: (id: string) => void;
  /** Lock / unlock a target at its current value (mode centered keeps a feature centred). */
  onLock: (target: string, locked: boolean, mode?: "value" | "centered") => void;
  /** Use a planar face as datum A, B or C. */
  onDatum: (letter: DatumLetter, planeId: string) => void;
}

function Card({ e, cad, onSelect, onRemove, onAction, onDimension, onLock, onDatum }: { e: Entity } & Omit<InspectorProps, "selection" | "onClear" | "edges">) {
  const u = useUnits();
  const info = cad.info;
  const locks = cad.locks ?? [];
  const isLocked = (target: string, mode: "value" | "centered" = "value") =>
    locks.some((l) => l.target === target && l.mode === mode);
  const rows: Row[] =
    e.type === "feature" ? featureRows(e.f, info, u)
    : e.type === "part" ? partRows(e.obj, info, u)
    : e.type === "pattern" ? patternRows(e.p, u)
    : e.type === "dimension" ? [["Value", dimValue(e.d, u) + (e.d.unit === "deg" ? "" : ` ${u.unit}`)], ["Type", e.d.type.replace("_", " ")], ["Between", e.d.refs.join(" ↔ ")]]
    : e.type === "edge" ? [["Type", e.e.kind], ["Length", u.len(e.e.length)], ["Corner", e.e.convexity], ["Midpoint", u.vec(e.e.mid)], ["Part", e.e.part ?? "—"]]
    : [["Status", "Not in the current model data (it may have been removed by an edit)."]];
  const title =
    e.type === "feature" ? KIND_NAME[e.f.kind] + (e.f.kind === "hole" ? (e.f.through ? " · through" : " · blind") : "")
    : e.type === "part" ? "Part"
    : e.type === "pattern" ? "Hole pattern"
    : e.type === "dimension" ? e.d.label
    : e.type === "edge" ? "Edge"
    : "Unknown ID";
  const members: string[] =
    e.type === "pattern" ? e.p.holes
    : e.type === "dimension" ? e.d.refs
    : e.type === "edge" ? e.e.faces
    : e.type === "feature" ? [e.f.part, e.f.counterbore?.feature, e.f.counterbore_of, e.f.pattern].filter((x): x is string => !!x)
    : [];
  const dims = relatedDims(e, info).slice(0, 10);
  const lockedDims = new Set(locks.filter((l) => l.mode === "value").map((l) => l.target));
  const centrable = e.type === "feature" && ["hole", "boss"].includes(e.f.kind);

  return (
    <div className="rounded-2xl border border-amber-500/30 bg-amber-500/[0.04] p-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs font-black px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-700 dark:text-amber-300">{e.id}</span>
        <span className="text-xs font-semibold truncate flex-1">{title}</span>
        {e.type !== "unknown" && e.type !== "edge" && (
          <LockButton
            locked={isLocked(e.id)}
            onClick={() => onLock(e.id, !isLocked(e.id))}
            title={isLocked(e.id) ? "Locked: edits may not change it. Click to unlock" : `Lock ${e.id}: no edit may change its size or position`}
          />
        )}
        <button onClick={() => onRemove(e.id)} className="p-1 rounded hover:bg-foreground/10 text-foreground/40" title="Deselect">
          <X size={12} />
        </button>
      </div>

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px]">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-foreground/45">{k}</dt>
            <dd className="font-mono text-foreground/85 break-words">{v}</dd>
          </div>
        ))}
      </dl>

      {members.length > 0 && (
        <div className="flex flex-wrap items-center gap-1 text-[10px] text-foreground/45">
          <span>Related:</span>
          {members.map((id) => (
            <Chip key={id} id={id} onClick={(ev) => onSelect(id, ev.shiftKey || ev.ctrlKey)} />
          ))}
        </div>
      )}

      {dims.length > 0 && (
        <div className="space-y-0.5">
          <p className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-foreground/40">
            <Ruler size={10} /> Dimensions
          </p>
          {dims.map((d) => (
            <div key={d.id} className="flex items-baseline gap-1.5 text-[11px] px-1 rounded hover:bg-foreground/5">
              <LockButton locked={lockedDims.has(d.id)} onClick={() => onLock(d.id, !lockedDims.has(d.id))} />
              <button
                onClick={() => onDimension(d.id)}
                title={`Select ${d.id} and type its new value`}
                className="flex-1 min-w-0 flex items-baseline gap-2 text-left"
              >
                <span className="font-mono font-bold text-purple-600 dark:text-purple-300 shrink-0">{d.id}</span>
                <span className="flex-1 truncate text-foreground/55">{d.label}</span>
                <span className="font-mono font-semibold">{dimValue(d, u)}</span>
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-wrap gap-1 pt-1">
        {actions(e).map(([label, text]) => (
          <button
            key={label}
            onClick={() => onAction(text)}
            className="text-[10px] font-semibold px-2 py-1 rounded-full border border-border hover:border-purple-500/50 hover:bg-purple-500/10"
          >
            {label}
          </button>
        ))}
        {centrable && (
          <button
            onClick={() => onLock(e.id, !isLocked(e.id, "centered"), "centered")}
            title="Keep it at the centre of its part through every edit (e.g. when the part is resized)"
            className={`text-[10px] font-semibold px-2 py-1 rounded-full border ${
              isLocked(e.id, "centered") ? "border-amber-500/60 bg-amber-500/15 text-amber-700 dark:text-amber-300" : "border-border hover:border-amber-500/50"
            }`}
          >
            {isLocked(e.id, "centered") ? "Centred (locked)" : "Keep centred"}
          </button>
        )}
        {e.type === "part" && <WallThicknessButton cad={cad} part={e.id} onSelect={onSelect} />}
        {e.type === "feature" && e.f.kind === "plane" && !e.f.is_cap && (
          <span className="flex items-center gap-1 text-[10px] text-foreground/50">
            <Anchor size={10} /> Datum
            {(["A", "B", "C"] as const).map((L) => (
              <button
                key={L}
                onClick={() => onDatum(L, e.id)}
                title={`Use ${e.id} as datum ${L}`}
                className={`font-mono font-bold px-1.5 py-0.5 rounded border ${
                  info.datums?.[L]?.id === e.id ? "border-purple-500 bg-purple-500/20" : "border-border hover:border-purple-500/50"
                }`}
              >
                {L}
              </button>
            ))}
          </span>
        )}
      </div>
    </div>
  );
}

/** Measures the part's thinnest wall on demand and shows where it is. */
function WallThicknessButton({ cad, part, onSelect }: { cad: CadState; part: string; onSelect: (id: string, additive: boolean) => void }) {
  const u = useUnits();
  const [state, setState] = useState<{ busy: boolean; res?: WallThickness; error?: string }>({ busy: false });
  const run = async () => {
    setState({ busy: true });
    try {
      const r = await apiFetch<{ parts: WallThickness[] }>(`/cad/wall-thickness/${cad.session_id}?object=${encodeURIComponent(part)}`);
      setState({ busy: false, res: r.parts[0] });
    } catch (err) {
      setState({ busy: false, error: (err as Error).message });
    }
  };
  return (
    <span className="flex flex-wrap items-center gap-1 text-[10px]">
      <button
        onClick={run}
        disabled={state.busy}
        className="font-semibold px-2 py-1 rounded-full border border-border hover:border-purple-500/50 hover:bg-purple-500/10 disabled:opacity-50"
        title="Ray-cast the part to find its thinnest wall"
      >
        {state.busy ? <Loader2 size={10} className="animate-spin inline" /> : "Wall thickness"}
      </button>
      {state.res && (
        <span className="text-foreground/70">
          min <span className="font-mono font-bold">{u.len(state.res.min_thickness)}</span> between{" "}
          {state.res.faces.filter((x): x is string => !!x).map((id) => (
            <Chip key={id} id={id} onClick={(ev) => onSelect(id, ev.shiftKey || ev.ctrlKey)} />
          ))}
        </span>
      )}
      {state.error && <span className="text-red-400">{state.error}</span>}
    </span>
  );
}

export default function SelectionInspector({ cad, selection, edges, onSelect, onRemove, onClear, onAction, onDimension, onLock, onDatum }: InspectorProps) {
  if (!selection.length) {
    return (
      <p className="flex items-center gap-2 text-[11px] text-foreground/45 px-1">
        <MousePointerClick size={13} className="shrink-0" />
        Click the model (or any ID below) to select a component and see its data. Shift+click adds to the selection.
        Your next message then applies to it.
      </p>
    );
  }
  const index = entityIndex(cad.info, edges);
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 px-1">
        <Crosshair size={13} className="text-amber-500" />
        <span className="text-[10px] font-black uppercase tracking-[0.25em] text-foreground/50">
          Selected ({selection.length})
        </span>
        <span className="text-[10px] text-foreground/40 flex items-center gap-1">
          <Box size={10} /> your next message applies to {selection.length === 1 ? "it" : "these"}
        </span>
        <button onClick={onClear} className="ml-auto text-[10px] text-foreground/40 hover:text-foreground">
          clear
        </button>
      </div>
      {selection.map((id) => (
        <Card
          key={id}
          e={index.get(id) ?? { type: "unknown", id }}
          cad={cad}
          onSelect={onSelect}
          onRemove={onRemove}
          onAction={onAction}
          onDimension={onDimension}
          onLock={onLock}
          onDatum={onDatum}
        />
      ))}
    </div>
  );
}
