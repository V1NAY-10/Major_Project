"use client";
import { Check, CheckCircle2, Copy, GitCommitHorizontal, XCircle, Zap } from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { Message, Operation } from "@/types/chat";

const TOOL_LABELS: Record<string, string> = {
  resize_hole: "Resize hole",
  move_hole: "Move hole",
  add_holes: "Add holes",
  remove_features: "Remove features",
  push_pull_face: "Push/pull face",
  set_face_distance: "Set distance",
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

function fmtValue(v: unknown): string {
  if (typeof v === "number") return Number.isInteger(v) ? `${v}` : v.toFixed(3);
  if (Array.isArray(v)) return `[${v.map(fmtValue).join(", ")}]`;
  return String(v);
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
                <span key={i} className="rounded bg-green-500/10 px-1.5 py-0.5 font-mono text-[10px] text-green-600 dark:text-green-400">
                  ✓ {v.id} {v.field.replace(/^distance_to:/, "↔ ")} = {fmtValue(v.value)}
                </span>
              ))}
            </div>
          )}
          {missing.map((v, i) => (
            <p key={i} className="mt-1 text-[10px] text-amber-500">⚠ {v.id}: {v.note ?? "could not verify"}</p>
          ))}
          {!!op.new_features?.length && (
            <p className="mt-1 text-[10px] text-foreground/40">New: {op.new_features.join(", ")}</p>
          )}
        </div>
      </div>
    </div>
  );
}

export default function ChatMessage({ message }: { message: Message }) {
  const [copied, setCopied] = useState(false);
  const { role, content, operations, version, kind } = message;
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
          <p className="text-sm leading-relaxed font-medium whitespace-pre-wrap">{content}</p>
        )}

        {!!operations?.length && (
          <div className="mt-3 space-y-1.5">
            {operations.map((op, i) => (
              <OperationCard key={i} op={op} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
