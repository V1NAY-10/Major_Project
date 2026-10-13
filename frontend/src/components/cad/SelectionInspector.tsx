"use client";
import { Box, Crosshair, MousePointerClick, Ruler, X } from "lucide-react";
import { CadObject, CadState, Dimension, Feature, ModelInfo, Pattern, Vec3 } from "@/types/cad";

/** What a selected ID refers to in the parsed model. */
export type Entity =
  | { type: "part"; id: string; obj: CadObject }
  | { type: "feature"; id: string; f: Feature }
  | { type: "pattern"; id: string; p: Pattern }
  | { type: "dimension"; id: string; d: Dimension }
  | { type: "unknown"; id: string };

const n = (v?: number, d = 3) => (v === undefined || v === null ? "—" : Number(v.toFixed(d)).toString());
const vec = (v?: Vec3) => (v ? `${n(v[0], 2)}, ${n(v[1], 2)}, ${n(v[2], 2)}` : "—");

export function entityIndex(info: ModelInfo): Map<string, Entity> {
  const m = new Map<string, Entity>();
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
};

/** One-line summary, e.g. "Hole Ø6 through" (also used for the chips in the chat input). */
export function summary(e: Entity | undefined): string {
  if (!e) return "";
  switch (e.type) {
    case "part":
      return `Part “${e.obj.label}”`;
    case "pattern":
      return `${e.p.type} pattern, ${e.p.count} holes`;
    case "dimension":
      return `${e.d.label} = ${n(e.d.value)}`;
    case "unknown":
      return "not in the current model";
  }
  const f = e.f;
  if (f.kind === "hole") return `Hole Ø${n(f.diameter)} ${f.through ? "through" : `× ${n(f.depth)}`}`;
  if (f.kind === "boss") return `Boss Ø${n(f.diameter)} × ${n(f.length)}`;
  if (f.kind === "round") return `Fillet R${n(f.radius)}`;
  if (f.kind === "plane") return f.label ? `Face: ${f.label}` : "Planar face";
  if (f.kind === "sphere") return `Sphere R${n(f.radius)}`;
  return KIND_NAME[f.kind];
}

type Row = [string, React.ReactNode];

function featureRows(f: Feature, info: ModelInfo): Row[] {
  const rows: Row[] = [];
  const add = (label: string, v: React.ReactNode) => {
    if (v !== undefined && v !== null && v !== "" && v !== "—") rows.push([label, v]);
  };
  add("Diameter", f.diameter !== undefined ? `Ø ${n(f.diameter)} mm` : undefined);
  if (f.diameter === undefined) add("Radius", f.radius !== undefined ? `R ${n(f.radius)} mm` : undefined);
  if (f.kind === "hole") add("Depth", f.through ? `through (${n(f.depth)} mm)` : `${n(f.depth)} mm`);
  if (f.kind !== "hole") add("Length", f.length !== undefined ? `${n(f.length)} mm` : undefined);
  add("Drill tip", f.drill_tip ? "yes" : undefined);
  if (f.countersinks?.length)
    add("Countersink", `Ø ${n(f.countersinks[0].outer_diameter)} × ${n(f.countersinks[0].angle_deg, 1)}°`);
  if (f.counterbore) add("Counterbore", `Ø ${n(f.counterbore.diameter)} × ${n(f.counterbore.depth)} deep`);
  add("Counterbore of", f.counterbore_of);
  add("Half-angle", f.half_angle_deg !== undefined ? `${n(f.half_angle_deg as number, 2)}°` : undefined);
  add("Area", f.area !== undefined ? `${n(f.area, 2)} mm²` : undefined);
  add("Face", f.label);
  add("Surface", f.surface);
  add("Normal", f.normal ? vec(f.normal) : undefined);
  add("Axis", f.axis ? vec(f.axis as Vec3) : f.direction ? vec(f.direction as Vec3) : undefined);
  add("Entry", f.entry ? vec(f.entry) : undefined);
  add("Centre", f.center ? vec(f.center) : undefined);
  add("Position", f.position);
  add("Pattern", f.pattern);
  const obj = info.objects.find((o) => o.name === f.object);
  if (obj && info.objects.length > 1) add("Part", `${obj.id ?? ""} ${obj.label}`.trim());
  return rows;
}

function partRows(o: CadObject, info: ModelInfo): Row[] {
  const counts: Record<string, number> = {};
  for (const fs of Object.values(info.features))
    for (const f of fs ?? []) if (f.object === o.name && !f.is_cap) counts[f.kind] = (counts[f.kind] ?? 0) + 1;
  return [
    ["Label", o.label],
    ["FreeCAD name", <span key="n" className="font-mono">{o.name}</span>],
    ["Type", o.type],
    ...(o.parent ? ([["Assembly", o.parent]] as Row[]) : []),
    ["Size X × Y × Z", `${n(o.bbox.size[0], 2)} × ${n(o.bbox.size[1], 2)} × ${n(o.bbox.size[2], 2)} mm`],
    ["Min corner", vec(o.bbox.min)],
    ["Volume", `${n(o.volume, 1)} mm³`],
    ["Area", `${n(o.area, 1)} mm²`],
    ["Topology", `${o.solids} solid · ${o.faces} faces · ${o.edges} edges`],
    ["Features", Object.entries(counts).map(([k, c]) => `${c} ${k}`).join(", ") || "—"],
    ...(o.unrecognized_faces ? ([["Free-form faces", `${o.unrecognized_faces}`]] as Row[]) : []),
  ];
}

function patternRows(p: Pattern): Row[] {
  const rows: Row[] = [
    ["Type", p.type],
    ["Holes", `${p.count} × Ø ${n(p.hole_diameter)}`],
  ];
  if (p.type === "circular") {
    rows.push(["PCD", `${n(p.pcd)} mm`], ["Angle step", `${n(p.angle_step_deg, 2)}°`]);
    rows.push(["Start angle", `${n(p.start_angle_deg, 2)}°`], ["Centre", vec(p.center)]);
  } else if (p.type === "grid") {
    rows.push(["Grid", `${p.count_u} × ${p.count_v}`], ["Spacing", `${n(p.spacing_u)} × ${n(p.spacing_v)} mm`]);
  } else {
    rows.push(["Spacing", `${n(p.spacing)} mm`], ["Direction", vec(p.direction)]);
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
  switch (e.f.kind) {
    case "hole":
      return [["Diameter", "Ø"], ["Move", "Move it by [x, y, z] mm: "], ["Chamfer", "Add a chamfer of  mm to its entry"], ["Remove", "Remove this hole"]];
    case "plane":
      return [["Push / pull", "Push this face out by  mm"], ["Fillet edges", "Fillet the edges of this face with R "]];
    case "boss":
      return [["Diameter", "Change its diameter to "], ["Length", "Make it  mm long"], ["Remove", "Remove this boss"]];
    case "round":
      return [["Radius", "Change the radius to "], ["Remove", "Remove this fillet"]];
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
  onSelect: (id: string, additive: boolean) => void;
  onRemove: (id: string) => void;
  onClear: () => void;
  /** Put a prompt starter in the message box (the selection stays the target). */
  onAction: (text: string) => void;
  /** Dimension clicked: select it and put "ID = " in the message box. */
  onDimension: (id: string) => void;
}

function Card({ e, cad, onSelect, onRemove, onAction, onDimension }: { e: Entity } & Omit<InspectorProps, "selection" | "onClear">) {
  const info = cad.info;
  const rows: Row[] =
    e.type === "feature" ? featureRows(e.f, info)
    : e.type === "part" ? partRows(e.obj, info)
    : e.type === "pattern" ? patternRows(e.p)
    : e.type === "dimension" ? [["Value", `${n(e.d.value)} mm`], ["Type", e.d.type.replace("_", " ")], ["Between", e.d.refs.join(" ↔ ")]]
    : [["Status", "Not in the current model data (it may have been removed by an edit)."]];
  const title =
    e.type === "feature" ? KIND_NAME[e.f.kind] + (e.f.kind === "hole" ? (e.f.through ? " · through" : " · blind") : "")
    : e.type === "part" ? "Part"
    : e.type === "pattern" ? "Hole pattern"
    : e.type === "dimension" ? e.d.label
    : "Unknown ID";
  const members: string[] =
    e.type === "pattern" ? e.p.holes
    : e.type === "dimension" ? e.d.refs
    : e.type === "feature" ? [e.f.part, e.f.counterbore?.feature, e.f.counterbore_of, e.f.pattern].filter((x): x is string => !!x)
    : [];
  const dims = relatedDims(e, info).slice(0, 10);

  return (
    <div className="rounded-2xl border border-amber-500/30 bg-amber-500/[0.04] p-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs font-black px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-700 dark:text-amber-300">{e.id}</span>
        <span className="text-xs font-semibold truncate flex-1">{title}</span>
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
            <button
              key={d.id}
              onClick={() => onDimension(d.id)}
              title={`Select ${d.id} and type its new value`}
              className="w-full flex items-baseline gap-2 text-[11px] px-1 rounded hover:bg-foreground/5 text-left"
            >
              <span className="font-mono font-bold text-purple-600 dark:text-purple-300 shrink-0">{d.id}</span>
              <span className="flex-1 truncate text-foreground/55">{d.label}</span>
              <span className="font-mono font-semibold">{n(d.value)}</span>
            </button>
          ))}
        </div>
      )}

      {actions(e).length > 0 && (
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
        </div>
      )}
    </div>
  );
}

export default function SelectionInspector({ cad, selection, onSelect, onRemove, onClear, onAction, onDimension }: InspectorProps) {
  if (!selection.length) {
    return (
      <p className="flex items-center gap-2 text-[11px] text-foreground/45 px-1">
        <MousePointerClick size={13} className="shrink-0" />
        Click the model (or any ID below) to select a component and see its data. Shift+click adds to the selection.
        Your next message then applies to it.
      </p>
    );
  }
  const index = entityIndex(cad.info);
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
        />
      ))}
    </div>
  );
}
