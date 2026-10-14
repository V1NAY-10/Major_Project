"use client";
import { useEffect, useState } from "react";
import { AlertTriangle, Download, FileText, Loader2, Scale, ShieldCheck, Wrench } from "lucide-react";
import { BomRow, CadState, DfmFinding, MassRow, MaterialInfo } from "@/types/cad";
import { apiBlob, apiFetch, postJson } from "@/lib/api";
import { useUnits } from "@/lib/units";

interface AnalysisPanelProps {
  cad: CadState;
  onSelect: (id: string, additive: boolean) => void;
  /** Apply a tool call as a new version (DFM fixes). */
  onRunTool: (tool: string, args: Record<string, unknown>, label: string) => Promise<void>;
  setMaterial: (part: string, material: string) => Promise<void>;
  downloadFile: (path: string, name: string) => Promise<void>;
  onError: (msg: string) => void;
}

function Block({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-border p-3 space-y-2">
      <h3 className="flex items-center gap-1.5 text-xs font-semibold">
        {icon}
        {title}
      </h3>
      {children}
    </section>
  );
}

const btn = "text-[11px] font-semibold px-2.5 py-1 rounded-lg border border-border hover:border-purple-500/50 hover:bg-purple-500/10 disabled:opacity-40";
const sevStyle: Record<DfmFinding["severity"], string> = {
  error: "bg-red-500/15 text-red-600 dark:text-red-400",
  warning: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  info: "bg-foreground/10 text-foreground/60",
};

function useBusy() {
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (key: string, fn: () => Promise<void>, onError: (m: string) => void) => {
    setBusy(key);
    try {
      await fn();
    } catch (e) {
      onError((e as Error).message || "Request failed");
    } finally {
      setBusy(null);
    }
  };
  return { busy, run };
}

function Chip({ id, onSelect }: { id: string; onSelect: (id: string, additive: boolean) => void }) {
  return (
    <button
      onClick={(e) => onSelect(id, e.shiftKey || e.ctrlKey)}
      className="font-mono text-[10px] font-bold px-1 rounded bg-purple-500/15 text-purple-600 dark:text-purple-300 hover:bg-purple-500/30"
    >
      {id}
    </button>
  );
}

export default function AnalysisPanel({ cad, onSelect, onRunTool, setMaterial, downloadFile, onError }: AnalysisPanelProps) {
  const u = useUnits();
  const sid = cad.session_id;
  const { busy, run } = useBusy();
  const [library, setLibrary] = useState<MaterialInfo[]>([]);
  const [mass, setMass] = useState<{ parts: MassRow[]; total?: { mass_g: number; center_of_mass: number[] } } | null>(null);
  const [process, setProcess] = useState<"cnc" | "print3d" | "sheet">("cnc");
  const [dfm, setDfm] = useState<{ findings: DfmFinding[]; counts: Record<string, number> } | null>(null);
  const [bom, setBom] = useState<{ rows: BomRow[]; parts: number; total_mass_g: number | null } | null>(null);
  const [clearance, setClearance] = useState(1);
  const [clash, setClash] = useState<{ pairs: { parts: string[]; labels: string[]; status: string; overlap_volume: number; distance: number }[] } | null>(null);
  const [drawing, setDrawing] = useState<{ svg: string; scale: string; callouts: string[] } | null>(null);
  const [costProc, setCostProc] = useState<"cnc" | "print3d">("cnc");
  const [qty, setQty] = useState(1);
  const [cost, setCost] = useState<{ parts: { part: string; label: string; per_part: number; total: number; material: string; assumed_material?: boolean }[]; total: number; note: string } | null>(null);

  useEffect(() => {
    apiFetch<MaterialInfo[]>("/cad/materials").then(setLibrary).catch(() => {});
  }, []);
  // results describe one version of the model: clear them when it changes
  const versionKey = `${sid}:${cad.version}`;
  const [shownFor, setShownFor] = useState(versionKey);
  if (shownFor !== versionKey) {
    setShownFor(versionKey);
    setMass(null);
    setDfm(null);
    setBom(null);
    setClash(null);
    setDrawing(null);
    setCost(null);
  }

  const parts = cad.info.objects;
  const multi = parts.length > 1;

  return (
    <div className="space-y-3">
      <Block title="Material and mass" icon={<Scale size={13} />}>
        <div className="space-y-1">
          {parts.map((o) => (
            <div key={o.name} className="flex items-center gap-2 text-[11px]">
              {o.id && <Chip id={o.id} onSelect={onSelect} />}
              <span className="flex-1 min-w-0 truncate text-foreground/70">{o.label}</span>
              <select
                id={`mat-${o.id}`}
                value={(cad.materials ?? {})[o.id ?? ""] ?? ""}
                onChange={(e) => run("mat", () => setMaterial(o.id ?? "all", e.target.value), onError)}
                className="bg-background border border-border rounded px-1 py-0.5 text-[11px] max-w-[11rem]"
              >
                <option value="" disabled>
                  material…
                </option>
                {library.map((m) => (
                  <option key={m.key} value={m.key}>
                    {m.name} ({m.density} g/cm³)
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
        <button className={btn} disabled={!!busy} onClick={() => run("mass", async () => setMass(await apiFetch(`/cad/mass/${sid}`)), onError)}>
          {busy === "mass" ? <Loader2 size={11} className="animate-spin inline" /> : "Compute mass properties"}
        </button>
        {mass && (
          <div className="overflow-x-auto">
            <table className="w-full text-[10px] font-mono">
              <thead>
                <tr className="text-foreground/40 text-left">
                  <th className="font-normal pr-2">part</th>
                  <th className="font-normal pr-2 text-right">mass</th>
                  <th className="font-normal pr-2 text-right">volume mm³</th>
                  <th className="font-normal">centre of mass</th>
                </tr>
              </thead>
              <tbody>
                {mass.parts.map((r) => (
                  <tr key={r.part} className="border-b border-border" title={r.principal_moments_kg_mm2 ? `principal moments ${r.principal_moments_kg_mm2.join(", ")} kg·mm²` : "set a material for mass and inertia"}>
                    <td className="pr-2">{r.part}</td>
                    <td className="pr-2 text-right">{r.mass_g !== undefined ? `${r.mass_g.toFixed(2)} g` : "no material"}</td>
                    <td className="pr-2 text-right">{r.volume.toFixed(1)}</td>
                    <td>{u.vec(r.center_of_mass)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {mass.total && <p className="text-[11px] mt-1">Total {mass.total.mass_g.toFixed(2)} g · centre of mass {u.vec(mass.total.center_of_mass)}</p>}
          </div>
        )}
      </Block>

      <Block title="Design for manufacturing" icon={<ShieldCheck size={13} />}>
        <div className="flex flex-wrap items-center gap-2">
          <select id="dfm-process" value={process} onChange={(e) => setProcess(e.target.value as typeof process)} className="bg-background border border-border rounded px-1 py-0.5 text-[11px]">
            <option value="cnc">CNC machining</option>
            <option value="print3d">3D printing (FDM)</option>
            <option value="sheet">Sheet metal</option>
          </select>
          <button className={btn} disabled={!!busy} onClick={() => run("dfm", async () => setDfm(await apiFetch(`/cad/dfm/${sid}?process=${process}`)), onError)}>
            {busy === "dfm" ? <Loader2 size={11} className="animate-spin inline" /> : "Check"}
          </button>
          {dfm && (
            <span className="text-[10px] text-foreground/50">
              {dfm.counts.error} error(s) · {dfm.counts.warning} warning(s)
            </span>
          )}
        </div>
        {dfm && !dfm.findings.length && <p className="text-[11px] text-green-600 dark:text-green-400">No problems found.</p>}
        {dfm?.findings.map((f, i) => (
          <div key={i} className="text-[11px] space-y-1 border-b border-border pb-1">
            <div className="flex items-start gap-1.5">
              <span className={`shrink-0 text-[9px] font-bold uppercase px-1 rounded ${sevStyle[f.severity]}`}>{f.severity}</span>
              <span className="text-foreground/80">{f.message}</span>
            </div>
            <div className="flex flex-wrap items-center gap-1">
              {f.features.map((id) => (
                <Chip key={id} id={id} onSelect={onSelect} />
              ))}
              {f.fix && (
                <button
                  className={`${btn} flex items-center gap-1`}
                  disabled={!!busy}
                  onClick={() =>
                    run("fix", async () => {
                      await onRunTool(f.fix!.tool, f.fix!.args, f.fix!.label);
                      setDfm(await apiFetch(`/cad/dfm/${sid}?process=${process}`));
                    }, onError)
                  }
                  title={`${f.fix.tool} ${JSON.stringify(f.fix.args)}`}
                >
                  <Wrench size={10} /> {f.fix.label}
                </button>
              )}
            </div>
          </div>
        ))}
      </Block>

      <Block title="Bill of materials" icon={<FileText size={13} />}>
        <div className="flex flex-wrap gap-2">
          <button className={btn} disabled={!!busy} onClick={() => run("bom", async () => setBom(await apiFetch(`/cad/bom/${sid}`)), onError)}>
            {busy === "bom" ? <Loader2 size={11} className="animate-spin inline" /> : "Build BOM"}
          </button>
          {(["csv", "xlsx"] as const).map((f) => (
            <button key={f} className={`${btn} flex items-center gap-1`} onClick={() => run("dl", () => downloadFile(`/cad/bom/${sid}?format=${f}`, `BOM.${f}`), onError)}>
              <Download size={10} /> {f.toUpperCase()}
            </button>
          ))}
        </div>
        {bom && (
          <div className="overflow-x-auto">
            <table className="w-full text-[10px] font-mono">
              <thead>
                <tr className="text-foreground/40 text-left">
                  <th className="font-normal pr-2">#</th>
                  <th className="font-normal pr-2">part</th>
                  <th className="font-normal pr-2 text-right">qty</th>
                  <th className="font-normal pr-2">material</th>
                  <th className="font-normal text-right">mass</th>
                </tr>
              </thead>
              <tbody>
                {bom.rows.map((r) => (
                  <tr key={r.item} className="border-b border-border" title={`${r.parts.join(" ")} · ${r.size} mm`}>
                    <td className="pr-2">{r.item}</td>
                    <td className="pr-2">{r.description}</td>
                    <td className="pr-2 text-right">{r.quantity}</td>
                    <td className="pr-2">{r.material}</td>
                    <td className="text-right">{r.total_mass_g === null ? "—" : `${r.total_mass_g.toFixed(1)} g`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="text-[11px] mt-1">
              {bom.parts} parts{bom.total_mass_g !== null ? ` · ${bom.total_mass_g.toFixed(1)} g` : ""}
            </p>
          </div>
        )}
      </Block>

      {multi && (
        <Block title="Interference" icon={<AlertTriangle size={13} />}>
          <div className="flex items-center gap-2 text-[11px]">
            <label htmlFor="clearance">clearance</label>
            <input id="clearance" type="number" min={0} step={0.5} value={clearance} onChange={(e) => setClearance(Number(e.target.value))} className="w-16 bg-background border border-border rounded px-1" />
            mm
            <button className={btn} disabled={!!busy} onClick={() => run("clash", async () => setClash(await apiFetch(`/cad/interference/${sid}?clearance=${clearance}`)), onError)}>
              Check
            </button>
          </div>
          {clash && !clash.pairs.length && <p className="text-[11px] text-green-600 dark:text-green-400">No overlaps, nothing closer than {clearance} mm.</p>}
          {clash?.pairs.map((p, i) => (
            <p key={i} className={`text-[11px] flex flex-wrap items-center gap-1 ${p.status === "interference" ? "text-red-600 dark:text-red-400" : "text-foreground/70"}`}>
              <Chip id={p.parts[0]} onSelect={onSelect} /> <Chip id={p.parts[1]} onSelect={onSelect} />
              {p.status === "interference" ? `overlap ${p.overlap_volume} mm³` : p.status === "touching" ? "touching" : `${u.len(p.distance)} apart`}
            </p>
          ))}
        </Block>
      )}

      <Block title="2D drawing" icon={<FileText size={13} />}>
        <div className="flex flex-wrap gap-2">
          <button
            className={btn}
            disabled={!!busy}
            onClick={() =>
              run("draw", async () => {
                const r = await postJson<{ scale: string; callouts: string[] }>(`/cad/drawing/${sid}`);
                const { blob } = await apiBlob(`/cad/drawing/${sid}?format=svg`);
                setDrawing({ svg: URL.createObjectURL(blob), scale: r.scale, callouts: r.callouts });
              }, onError)
            }
          >
            {busy === "draw" ? <Loader2 size={11} className="animate-spin inline" /> : "Make drawing"}
          </button>
          {(["pdf", "svg"] as const).map((f) => (
            <button key={f} className={`${btn} flex items-center gap-1`} onClick={() => run("dl", () => downloadFile(`/cad/drawing/${sid}?format=${f}`, `drawing.${f}`), onError)}>
              <Download size={10} /> {f.toUpperCase()}
            </button>
          ))}
        </div>
        {drawing && (
          <>
            {/* eslint-disable-next-line @next/next/no-img-element -- a generated SVG blob, not a static asset */}
            <img src={drawing.svg} alt="Generated drawing" className="w-full rounded border border-border bg-white" />
            <p className="text-[10px] text-foreground/50">
              Scale {drawing.scale} · {drawing.callouts.join(" · ")}
            </p>
          </>
        )}
      </Block>

      <Block title="Cost estimate" icon={<Scale size={13} />}>
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          <select id="cost-process" value={costProc} onChange={(e) => setCostProc(e.target.value as typeof costProc)} className="bg-background border border-border rounded px-1 py-0.5">
            <option value="cnc">CNC</option>
            <option value="print3d">3D print</option>
          </select>
          <label htmlFor="qty">qty</label>
          <input id="qty" type="number" min={1} value={qty} onChange={(e) => setQty(Math.max(1, Number(e.target.value)))} className="w-14 bg-background border border-border rounded px-1" />
          <button className={btn} disabled={!!busy} onClick={() => run("cost", async () => setCost(await apiFetch(`/cad/cost/${sid}?process=${costProc}&quantity=${qty}`)), onError)}>
            Estimate
          </button>
        </div>
        {cost && (
          <div className="text-[11px] space-y-0.5">
            {cost.parts.map((p) => (
              <p key={p.part} className="font-mono">
                {p.part} {p.label}: ≈ {p.per_part.toFixed(2)} USD each ({p.material}
                {p.assumed_material ? ", assumed" : ""})
              </p>
            ))}
            <p className="font-semibold">Total ≈ {cost.total.toFixed(2)} USD</p>
            <p className="text-[10px] text-foreground/45">{cost.note}</p>
          </div>
        )}
      </Block>
    </div>
  );
}
