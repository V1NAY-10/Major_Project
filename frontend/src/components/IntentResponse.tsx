"use client";
import { useState } from "react";
import { CheckCircle2, Target, Zap, Hash, FileText, Loader2 } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface IntentData {
  target_pattern?: string;
  action?: string;
  value?: number | string;
  reason?: string;
  [key: string]: unknown;
}

interface IntentResponseProps {
  intent: {
    intents?: Array<any>;
    preview?: Array<any>;
    warnings?: Array<{ warning: string }>;
  };
  onApply?: () => Promise<void>;
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function actionColor(action?: string) {
  if (!action) return "text-foreground/50";
  if (action.includes("increase") || action.includes("add") || action.includes("create"))
    return "text-emerald-400";
  if (action.includes("decrease") || action.includes("remove") || action.includes("reduce"))
    return "text-red-400";
  if (action.includes("change") || action.includes("modify") || action.includes("update"))
    return "text-amber-400";
  return "text-blue-400";
}

const FIELD_META = [
  { key: "target_label", label: "Target", icon: <Target size={14} className="text-purple-400" />, valueClass: "text-purple-300 font-semibold" },
  { key: "action", label: "Action", icon: <Zap size={14} className="text-amber-400" /> },
  { key: "value", label: "Value", icon: <Hash size={14} className="text-blue-400" />, valueClass: "text-blue-300 font-semibold" },
  { key: "reason", label: "Reason", icon: <FileText size={14} className="text-foreground/40" />, valueClass: "text-foreground/60 italic" },
];

export default function IntentResponse({ intent, onApply }: IntentResponseProps) {
  const [applying, setApplying] = useState(false);
  const intentsList = intent.intents || [];
  const previews = intent.preview || [];
  const warnings = intent.warnings || [];

  const handleApply = async () => {
    if (!onApply) return;
    setApplying(true);
    try {
        await onApply();
    } finally {
        setApplying(false);
    }
  };

  return (
    <div className="flex flex-col gap-4 animate-in fade-in slide-in-from-bottom-2 duration-300">
      
      {/* Status badge */}
      <div className="flex items-center gap-2">
        <CheckCircle2 size={16} className="text-emerald-400" />
        <span className="text-xs font-semibold text-emerald-400 uppercase tracking-wider">
          Intent Interpreted ({intentsList.length} action{intentsList.length !== 1 ? 's' : ''})
        </span>
      </div>

      {/* Warnings */}
      {warnings.length > 0 && (
        <div className="flex flex-col gap-2">
          {warnings.map((w, i) => (
            <div key={i} className="px-3 py-2 bg-amber-500/10 border border-amber-500/20 text-amber-500/90 text-xs rounded-xl">
              ⚠️ {w.warning}
            </div>
          ))}
        </div>
      )}

      {/* Intents */}
      {intentsList.map((singleIntent, idx) => {
        // Collect any extra fields not in standard set
        const standardKeys = new Set([...FIELD_META.map(f => f.key), "target_pattern"]);
        const extraEntries = Object.entries(singleIntent).filter(([k]) => !standardKeys.has(k));
        const prev = previews[idx];

        return (
          <div key={idx} className="flex flex-col gap-2 p-3 bg-card border border-border rounded-xl shadow-sm">
            
            <div className="flex flex-col gap-2">
              {FIELD_META.map(({ key, label, icon, valueClass }) => {
                const val = singleIntent[key];
                if (val === undefined || val === null) return null;
                return (
                  <div key={key} className="flex items-start gap-3 bg-foreground/[0.03] border border-border rounded-lg p-2.5">
                    <div className="mt-0.5 shrink-0">{icon}</div>
                    <div className="flex-1 min-w-0">
                      <p className="text-[10px] uppercase tracking-widest text-foreground/40 font-bold mb-0.5">
                        {label}
                      </p>
                      <p className={`text-sm font-mono break-all ${key === "action" ? actionColor(String(val)) : valueClass ?? "text-foreground"}`}>
                        {String(val)}
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Preview Banner */}
            {prev && (
              <div className="mt-2 bg-blue-500/5 border border-blue-500/10 rounded-lg p-3 flex flex-col gap-1">
                <p className="text-[10px] uppercase tracking-widest text-blue-400/70 font-bold">Simulation Preview</p>
                <div className="flex items-center gap-2 text-xs font-mono">
                  <span className="text-foreground/50">{prev.before}</span>
                  <span className="text-blue-400">→</span>
                  <span className="text-emerald-400 font-bold">{prev.after}</span>
                  <span className="text-foreground/40 ml-auto">x{prev.count} instances</span>
                </div>
              </div>
            )}

            {/* Extra fields */}
            {extraEntries.length > 0 && (
              <div className="mt-2 bg-foreground/[0.03] border border-border rounded-lg overflow-hidden">
                <p className="text-[10px] uppercase tracking-widest text-foreground/40 font-bold px-3 pt-3 pb-1">
                  Additional Fields
                </p>
                {extraEntries.map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between px-3 py-2 border-t border-border first:border-t-0">
                    <span className="text-xs text-foreground/50 font-mono">{k}</span>
                    <span className="text-xs text-foreground/80 font-mono truncate max-w-[60%] text-right">
                      {JSON.stringify(v)}
                    </span>
                  </div>
                ))}
              </div>
            )}

          </div>
        );
      })}

      {/* Apply Button */}
      {onApply && (
        <button
          onClick={handleApply}
          disabled={applying}
          className="w-full mt-2 py-3 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white rounded-xl font-bold text-sm shadow-lg shadow-purple-500/20 transition-all flex items-center justify-center gap-2"
        >
          {applying ? <Loader2 size={18} className="animate-spin" /> : <Zap size={18} fill="white" />}
          {applying ? "Modifying Geometry..." : "Apply Changes to 3D Model"}
        </button>
      )}

      {/* Raw JSON toggle */}
      <details className="group mt-2">
        <summary className="text-[10px] uppercase tracking-wider text-foreground/30 hover:text-foreground/60 cursor-pointer select-none transition-colors">
          View raw JSON
        </summary>
        <pre className="mt-2 text-[11px] font-mono bg-black/30 border border-border rounded-xl p-3 overflow-x-auto text-foreground/70 leading-relaxed">
          {JSON.stringify(intent, null, 2)}
        </pre>
      </details>
    </div>
  );
}
