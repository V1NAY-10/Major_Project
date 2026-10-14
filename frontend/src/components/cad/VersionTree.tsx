"use client";
import { useMemo, useState } from "react";
import { GitBranch, GitCompare, Pencil, X } from "lucide-react";
import { CadState, CompareResult, VersionEntry } from "@/types/cad";
import { useUnits } from "@/lib/units";

interface VersionTreeProps {
  cad: CadState;
  onCheckout: (v: number) => void;
  onRename: (v: number, name: string) => void;
  onCompare: (a: number, b: number) => void;
  compare: CompareResult | null;
  onClearCompare: () => void;
}

type Row = { ver: VersionEntry; depth: number; branch: boolean };

/** Versions as a tree: each branch indented under the version it was made from (newest branch first). */
function layout(versions: VersionEntry[]): Row[] {
  const kids = new Map<number | null, VersionEntry[]>();
  for (const v of versions) {
    const p = v.parent ?? null;
    kids.set(p, [...(kids.get(p) ?? []), v]);
  }
  const out: Row[] = [];
  const walk = (v: VersionEntry, depth: number, branch: boolean) => {
    out.push({ ver: v, depth, branch });
    const ch = [...(kids.get(v.v) ?? [])].sort((a, b) => b.v - a.v);
    // the newest child continues the line; older children are branches
    ch.forEach((c, i) => walk(c, i === 0 ? depth : depth + 1, i > 0));
  };
  for (const root of kids.get(null) ?? []) walk(root, 0, false);
  return out.reverse();
}

export default function VersionTree({ cad, onCheckout, onRename, onCompare, compare, onClearCompare }: VersionTreeProps) {
  const u = useUnits();
  const rows = useMemo(() => layout(cad.versions), [cad.versions]);
  const [picked, setPicked] = useState<number[]>([]);
  const [editing, setEditing] = useState<number | null>(null);
  const [name, setName] = useState("");
  const toggle = (v: number) =>
    setPicked((p) => (p.includes(v) ? p.filter((x) => x !== v) : [...p, v].slice(-2)));
  const branches = rows.filter((r) => r.branch).length;

  return (
    <div className="space-y-2">
      <p className="text-[10px] text-foreground/45 px-1">
        Editing after an undo starts a new branch; older branches stay here. Tick two versions to compare them.
        {branches ? ` ${branches} branch${branches === 1 ? "" : "es"}.` : ""}
      </p>
      {picked.length === 2 && (
        <button
          onClick={() => onCompare(Math.min(...picked), Math.max(...picked))}
          className="w-full flex items-center justify-center gap-1.5 text-[11px] font-semibold py-1.5 rounded-lg bg-purple-600 text-white"
        >
          <GitCompare size={12} /> Compare v{Math.min(...picked)} → v{Math.max(...picked)}
        </button>
      )}
      {compare && (
        <div className="rounded-xl border border-border p-2 space-y-1 text-[11px]">
          <div className="flex items-center gap-2">
            <GitCompare size={12} className="text-purple-500" />
            <span className="font-semibold">v{compare.a.v} → v{compare.b.v}</span>
            <span className="flex items-center gap-2 text-[10px] text-foreground/50">
              <span className="inline-block w-2 h-2 rounded-sm bg-green-500" /> added
              <span className="inline-block w-2 h-2 rounded-sm bg-red-500" /> removed
            </span>
            <button onClick={onClearCompare} className="ml-auto text-foreground/40 hover:text-foreground" title="Close the comparison">
              <X size={12} />
            </button>
          </div>
          {compare.geometry.parts.map((p) => (
            <p key={p.object} className="font-mono text-foreground/70">
              {p.label}: +{p.added_volume} / −{p.removed_volume} mm³ {p.status !== "changed" ? `(${p.status})` : ""}
            </p>
          ))}
          {!compare.geometry.parts.length && <p className="text-foreground/50">No geometry difference.</p>}
          {compare.dimensions && compare.dimensions.total > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full font-mono text-[10px]">
                <tbody>
                  {compare.dimensions.rows.slice(0, 40).map((r) => (
                    <tr key={r.key} className="border-b border-border">
                      <td className="pr-2 py-0.5">{r.key}</td>
                      <td className="pr-2 text-right">{r.before === null ? "—" : u.value(r.before, r.kind as "length")}</td>
                      <td className="pr-2 text-right">{r.after === null ? "removed" : u.value(r.after, r.kind as "length")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {compare.dimensions.total > 40 && <p className="text-foreground/40">… {compare.dimensions.total - 40} more</p>}
            </div>
          )}
        </div>
      )}
      <ol className="space-y-1">
        {rows.map(({ ver, depth, branch }) => {
          const current = ver.v === cad.version;
          return (
            <li
              key={ver.v}
              style={{ marginLeft: depth * 14 }}
              className={`rounded-xl border px-2.5 py-1.5 text-[11px] ${current ? "border-purple-500/50 bg-purple-500/10" : "border-border"}`}
            >
              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  id={`cmp-${ver.v}`}
                  checked={picked.includes(ver.v)}
                  onChange={() => toggle(ver.v)}
                  title="Pick for comparison"
                  className="accent-purple-600"
                />
                {branch && <GitBranch size={11} className="text-foreground/40" />}
                <span className="font-mono font-bold">v{ver.v}</span>
                {editing === ver.v ? (
                  <input
                    autoFocus
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    onBlur={() => {
                      onRename(ver.v, name);
                      setEditing(null);
                    }}
                    onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                    className="flex-1 min-w-0 bg-background border border-border rounded px-1 text-[11px]"
                    placeholder="Name this version"
                  />
                ) : (
                  <span className="flex-1 min-w-0 truncate font-semibold">{ver.name ?? ""}</span>
                )}
                <button
                  onClick={() => {
                    setEditing(ver.v);
                    setName(ver.name ?? "");
                  }}
                  className="text-foreground/30 hover:text-foreground"
                  title="Name this version"
                >
                  <Pencil size={10} />
                </button>
                {current ? (
                  <span className="text-[10px] text-purple-600 dark:text-purple-300 font-semibold">current</span>
                ) : (
                  <button onClick={() => onCheckout(ver.v)} className="text-[10px] font-semibold text-foreground/50 hover:text-foreground">
                    go to
                  </button>
                )}
              </div>
              <p className="text-foreground/65 mt-0.5 break-words">{ver.description}</p>
              <p className="text-[10px] text-foreground/35 font-mono">{new Date(ver.created_at).toLocaleString()}</p>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
