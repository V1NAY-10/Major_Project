"use client";
import { Check, CheckCircle2, ChevronRight, Copy, Crosshair, Eye, GitCommitHorizontal, Lock, MousePointerClick, TriangleAlert, XCircle, Zap } from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { Message, Operation, SideEffects, Verification } from "@/types/chat";
import { DesignOption } from "@/types/cad";
import { Units, useUnits } from "@/lib/units";

const TOOL_LABELS: Record<string, string> = {
  resize_hole: "Resize hole",
  move_hole: "Move hole",
  add_holes: "Add holes",
  remove_features: "Remove features",
  push_pull_face: "Push/pull face",
  set_face_distance: "Set distance",
  set_dimension: "Set dimension",
  set_hole_standard: "Standard hole",
  edit_slot: "Edit slot",
  edit_pocket: "Edit pocket",
  edit_chamfer: "Edit chamfer",
  mirror_object: "Mirror part",
  mirror_features: "Mirror features",
  pattern_feature: "Pattern",
  add_slot: "Add slot",
  add_pocket: "Add pocket",
  add_counterbore: "Add counterbore",
  add_countersink: "Add countersink",
  draft_faces: "Draft",
  split_object: "Split",
  shell_object: "Shell",
  edit_hole_pattern: "Edit pattern",
  list_dimensions: "Dimensions",
  fillet_edges: "Fillet",
  chamfer_edges: "Chamfer",
  add_primitive: "Add primitive",
  transform_object: "Transform",
  scale_object: "Scale",
  set_property: "Set parameter",
  set_sketch_constraint: "Sketch constraint",
  set_spreadsheet_cell: "Spreadsheet cell",
  delete_object: "Delete object",
  run_freecad_python: "Custom script",
};

function fmtValue(v: unknown, u?: Units, unit?: string): string {
  if (typeof v === "number") {
    if (u && unit === "mm") return u.len(v);
    if (u && unit === "deg") return u.ang(v);
    return Number.isInteger(v) ? `${v}` : v.toFixed(3);
  }
  if (Array.isArray(v)) return u && unit === "mm" ? `[${u.vec(v as number[])}]` : `[${v.map((x) => fmtValue(x)).join(", ")}]`;
  if (v && typeof v === "object")
    return Object.entries(v as Record<string, unknown>)
      .map(([k, x]) => `${k}=${fmtValue(x)}`)
      .join(", ");
  return String(v);
}

/** "✓ P5-P6 = 120.000 mm  Δ 0.0000" chip: green within tolerance, amber outside or without a target. */
function VerifiedChip({ v }: { v: Verification }) {
  const u = useUnits();
  const field = fmtField(v.field);
  const hasTarget = v.deviation !== undefined && v.deviation !== null;
  const tol = v.unit === "deg" ? u.settings.tolerance.angle : u.settings.tolerance.length;
  const dev = !hasTarget
    ? ""
    : v.unit === "deg"
      ? ` Δ ${u.ang(v.deviation)}`
      : typeof v.deviation === "number"
        ? ` Δ ${u.len(v.deviation, true)}`
        : "";
  const bad = hasTarget && v.within_tolerance === false;
  return (
    <span
      title={
        hasTarget
          ? `requested ${fmtValue(v.requested, u, v.unit)} · achieved ${fmtValue(v.achieved, u, v.unit)} · tolerance ±${tol}${v.unit === "deg" ? "°" : " mm"}`
          : "measured after the edit"
      }
      className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${
        bad ? "bg-amber-500/15 text-amber-700 dark:text-amber-300" : "bg-green-500/10 text-green-600 dark:text-green-400"
      }`}
    >
      {bad ? "⚠" : "✓"} {v.id}
      {field ? ` ${field}` : ""} = {fmtValue(v.achieved ?? v.value, u, v.unit)}
      {dev && <span className="opacity-60">{dev}</span>}
    </span>
  );
}

/** Values that changed besides the target, collapsible; changes on untouched features are amber. */
function SideEffectsTable({ se }: { se: SideEffects }) {
  const u = useUnits();
  const [open, setOpen] = useState(se.unexpected > 0);
  if (!se.total) return null;
  return (
    <div className="mt-1.5">
      <button
        onClick={() => setOpen(!open)}
        className={`flex items-center gap-1 text-[10px] ${se.unexpected ? "text-amber-600 dark:text-amber-400" : "text-foreground/45"} hover:underline`}
      >
        <ChevronRight size={11} className={`transition-transform ${open ? "rotate-90" : ""}`} />
        {se.unexpected ? <TriangleAlert size={10} /> : null}
        {se.total} other value{se.total === 1 ? "" : "s"} changed
        {se.unexpected ? ` · ${se.unexpected} unexpected` : " · all on the edited features"}
      </button>
      {open && (
        <table className="mt-1 w-full text-[10px] font-mono">
          <thead>
            <tr className="text-foreground/40 text-left">
              <th className="font-normal pr-2">value</th>
              <th className="font-normal pr-2 text-right">before</th>
              <th className="font-normal pr-2 text-right">after</th>
              <th className="font-normal text-right">Δ</th>
            </tr>
          </thead>
          <tbody>
            {se.rows.map((r) => (
              <tr
                key={r.key}
                title={r.label + (r.expected ? " (involves a feature the edit changed)" : " (unexpected: not on an edited feature)")}
                className={r.expected ? "text-foreground/60" : "text-amber-700 dark:text-amber-300 bg-amber-500/10"}
              >
                <td className="pr-2 truncate max-w-[9rem]">{r.key}</td>
                <td className="pr-2 text-right">{u.value(r.before, r.kind)}</td>
                <td className="pr-2 text-right">{r.after === null ? "removed" : u.value(r.after, r.kind)}</td>
                <td className="text-right">
                  {r.delta === null ? "" : `${r.delta > 0 ? "+" : ""}${r.kind === "count" ? r.delta : r.kind === "angle" ? u.ang(r.delta) : u.len(r.delta, true)}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && se.total > se.rows.length && (
        <p className="text-[10px] text-foreground/40">… and {se.total - se.rows.length} more</p>
      )}
    </div>
  );
}

function fmtField(field: string): string {
  if (field.startsWith("distance_to:")) return field.replace(/^distance_to:/, "↔ ");
  if (field === "dimension") return "";
  if (field === "pattern") return "pattern";
  if (field.startsWith("datum:")) return `from datum ${field.slice(6)}`;
  return field.replace(/_/g, " ");
}

function OperationCard({ op }: { op: Operation }) {
  const verified = (op.verified ?? []).filter((v) => v.status === "ok");
  const missing = (op.verified ?? []).filter((v) => v.status !== "ok");
  return (
    <div
      className={`rounded-xl border px-3 py-2 text-xs ${
        op.ok ? "border-green-500/20 bg-green-500/5" : "border-red-500/25 bg-red-500/5"
      }`}
    >
      <div className="flex items-start gap-2">
        {op.ok ? (
          <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-green-500" />
        ) : (
          <XCircle size={14} className="mt-0.5 shrink-0 text-red-400" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-semibold">{TOOL_LABELS[op.tool] ?? op.tool}</span>
            <span className="font-mono text-foreground/60 break-words">{op.ok ? op.message?.split("\n")[0] : op.error}</span>
          </div>
          {verified.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1.5">
              {verified.map((v, i) => (
                <VerifiedChip key={i} v={v} />
              ))}
            </div>
          )}
          {missing.map((v, i) => (
            <p key={i} className="mt-1 text-[10px] text-amber-500">⚠ {v.id}: {v.note ?? "could not verify"}</p>
          ))}
          {(op.warnings ?? []).map((w, i) => (
            <p key={`w${i}`} className="mt-1 text-[10px] text-amber-600 dark:text-amber-400">⚠ {w}</p>
          ))}
          {op.lock_resolution && (
            <p className="mt-1 flex items-center gap-1 text-[10px] text-foreground/50">
              <Lock size={10} /> A lock was kept: the edit used {op.lock_resolution}.
            </p>
          )}
          {!!op.new_features?.length && (
            <p className="mt-1 text-[10px] text-foreground/40">New: {op.new_features.join(", ")}</p>
          )}
          {op.side_effects && <SideEffectsTable se={op.side_effects} />}
        </div>
      </div>
    </div>
  );
}

/** One alternative: title, trade-off, previewed values; Preview shows its ghost, Apply makes it a version. */
function OptionCard({
  opt,
  active,
  previewing,
  busy,
  onPreview,
  onApply,
}: {
  opt: DesignOption;
  active: boolean;
  previewing: boolean;
  busy?: boolean;
  onPreview?: () => void;
  onApply?: () => void;
}) {
  const verified = opt.records.flatMap((r) => (r?.verified ?? []) as Verification[]).filter((v) => v.status === "ok");
  const warnings = opt.records.flatMap((r) => r?.warnings ?? []);
  return (
    <div
      className={`rounded-xl border px-3 py-2 text-xs ${
        !opt.ok
          ? "border-border opacity-60"
          : previewing
            ? "border-cyan-500/60 bg-cyan-500/10"
            : "border-border hover:border-purple-500/50"
      }`}
    >
      <div className="flex items-start gap-2">
        <span
          className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full font-mono text-[10px] font-bold ${
            opt.ok ? "bg-purple-600 text-white" : "bg-foreground/10 text-foreground/50"
          }`}
        >
          {opt.key}
        </span>
        <div className="min-w-0 flex-1">
          <p className="font-semibold">{opt.title}</p>
          <p className="text-foreground/60">{opt.description}</p>
          {opt.ok ? (
            <>
              {verified.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {verified.map((v, i) => (
                    <VerifiedChip key={i} v={v} />
                  ))}
                </div>
              )}
              {warnings.map((w, i) => (
                <p key={i} className="mt-1 text-[10px] text-amber-600 dark:text-amber-400">⚠ {w}</p>
              ))}
              <p className="mt-1 font-mono text-[10px] text-foreground/40 break-words">
                {opt.steps.map((st) => st.label).join(" → ")}
              </p>
            </>
          ) : (
            <p className="mt-1 text-[10px] text-red-500/80">Not possible: {opt.error}</p>
          )}
        </div>
        {opt.ok && (
          <div className="flex shrink-0 flex-col gap-1">
            <button
              onClick={onPreview}
              disabled={!active}
              title="Show this result as a ghost in the 3D view"
              className={`flex items-center gap-1 rounded-lg border px-2 py-0.5 text-[11px] font-semibold disabled:opacity-30 ${
                previewing ? "border-cyan-500/60 bg-cyan-500/20" : "border-border"
              }`}
            >
              <Eye size={11} /> Preview
            </button>
            <button
              onClick={onApply}
              disabled={!active || busy}
              className="rounded-lg bg-purple-600 px-2 py-0.5 text-[11px] font-semibold text-white disabled:opacity-30"
            >
              Apply
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

interface ChatMessageProps {
  message: Message;
  /** ID of the plan currently waiting for approval (its buttons are active only then). */
  pendingPlanId?: string | null;
  onApplyPlan?: () => void;
  onCancelPlan?: () => void;
  /** A clickable answer was chosen / hovered (highlight it in the 3D view). */
  onChoose?: (id: string, label: string) => void;
  onHoverChoice?: (id: string | null) => void;
  /** ID of the option set still waiting for a pick (its buttons are active only then). */
  activeOptionsId?: string | null;
  /** Key of the option whose ghost is shown in the 3D view. */
  previewOption?: number | null;
  onPreviewOption?: (opt: DesignOption | null) => void;
  onApplyOption?: (key: number) => void;
  busy?: boolean;
}

export default function ChatMessage({
  message,
  pendingPlanId,
  onApplyPlan,
  onCancelPlan,
  onChoose,
  onHoverChoice,
  activeOptionsId,
  previewOption,
  onPreviewOption,
  onApplyOption,
  busy,
}: ChatMessageProps) {
  const [copied, setCopied] = useState(false);
  const { role, content, operations, version, kind, selection, plan, choices, options } = message;
  const optionsActive = !!options && options.id === activeOptionsId;
  const planActive = !!plan && plan.ok && plan.id === pendingPlanId;
  const isAssistant = role === "assistant";

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy text: ", err);
    }
  };

  return (
    <div className={`flex ${isAssistant ? "justify-start" : "justify-end"} animate-in fade-in slide-in-from-bottom-4 duration-500`}>
      <div
        className={`max-w-[85%] md:max-w-[75%] rounded-2xl p-4 shadow-lg ${
          isAssistant
            ? kind === "error"
              ? "bg-red-500/5 border border-red-500/20 text-foreground"
              : "bg-foreground/[0.03] backdrop-blur-xl border border-border text-foreground"
            : "bg-linear-to-br from-purple-600 to-indigo-600 text-white shadow-purple-500/20"
        }`}
      >
        {isAssistant && (
          <div className="flex items-center justify-between mb-2 pb-2 border-b border-border">
            <div className="flex items-center gap-2">
              <div className="w-5 h-5 rounded-full bg-purple-500 flex items-center justify-center">
                <Zap size={10} fill="white" className="text-white" />
              </div>
              <span className="text-[10px] uppercase tracking-[0.2em] font-black opacity-40">AetherCAD</span>
              {version !== undefined && version !== null && (
                <span className="flex items-center gap-1 rounded-full border border-border px-1.5 py-0.5 text-[9px] font-mono text-foreground/50">
                  <GitCommitHorizontal size={10} /> v{version}
                </span>
              )}
            </div>
            <button
              onClick={handleCopy}
              className="p-1 hover:bg-foreground/10 rounded-lg transition-colors text-foreground/40 hover:text-foreground"
              title="Copy"
            >
              {copied ? <Check size={13} className="text-green-400" /> : <Copy size={13} />}
            </button>
          </div>
        )}

        {isAssistant ? (
          <div className="chat-md text-sm leading-relaxed">
            <ReactMarkdown>{content}</ReactMarkdown>
          </div>
        ) : (
          <>
            {!!selection?.length && (
              <p className="mb-1 flex flex-wrap items-center gap-1 text-[10px] text-white/70">
                <Crosshair size={10} /> on
                {selection.map((id) => (
                  <span key={id} className="rounded bg-white/15 px-1 font-mono font-bold text-white">
                    {id}
                  </span>
                ))}
              </p>
            )}
            <p className="text-sm leading-relaxed font-medium whitespace-pre-wrap">{content}</p>
          </>
        )}

        {!!operations?.length && (
          <div className="mt-3 space-y-1.5">
            {operations.map((op, i) => (
              <OperationCard key={i} op={op} />
            ))}
          </div>
        )}

        {plan && plan.ok && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className="flex items-center gap-1 text-[10px] text-cyan-700 dark:text-cyan-300">
              <Eye size={11} /> {planActive ? "preview shown as a ghost in the 3D view" : "no longer pending"}
            </span>
            <button
              onClick={onApplyPlan}
              disabled={!planActive || busy}
              className="ml-auto text-xs font-semibold px-3 py-1 rounded-lg bg-purple-600 text-white disabled:opacity-30"
            >
              Apply
            </button>
            <button
              onClick={onCancelPlan}
              disabled={!planActive || busy}
              className="text-xs font-semibold px-3 py-1 rounded-lg border border-border disabled:opacity-30"
            >
              Cancel
            </button>
          </div>
        )}

        {!!options?.options.length && (
          <div className="mt-3 space-y-1.5">
            {options.options.map((o) => (
              <OptionCard
                key={o.key}
                opt={o}
                active={optionsActive}
                previewing={optionsActive && previewOption === o.key}
                busy={busy}
                onPreview={() => onPreviewOption?.(previewOption === o.key ? null : o)}
                onApply={() => onApplyOption?.(o.key)}
              />
            ))}
            {!optionsActive && <p className="text-[10px] text-foreground/40">These options are no longer current.</p>}
          </div>
        )}

        {!!choices?.length && (
          <div className="mt-3 flex flex-wrap gap-1.5" onMouseLeave={() => onHoverChoice?.(null)}>
            {choices.map((c) => (
              <button
                key={c.id}
                onClick={() => onChoose?.(c.id, c.label)}
                onMouseEnter={() => onHoverChoice?.(c.id)}
                onFocus={() => onHoverChoice?.(c.id)}
                disabled={busy}
                title={`Choose ${c.id} (hover to see it in the 3D view)`}
                className="flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-lg border border-border hover:border-amber-500/60 hover:bg-amber-500/10 disabled:opacity-40"
              >
                <MousePointerClick size={11} />
                <span className="font-mono font-bold text-purple-600 dark:text-purple-300">{c.id}</span>
                <span className="text-foreground/70">{c.label}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
