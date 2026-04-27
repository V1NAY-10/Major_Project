"use client";
import { Layers, Circle, Box, Minus, Triangle } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

import { ParsedData, Component } from "@/types/cad";

interface PatternSummaryProps {
  data: ParsedData;
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function humanLabel(type?: string): string {
  if (!type) return "Unknown";
  const map: Record<string, string> = {
    hole_pattern: "Small Holes",
    hole: "Hole",
    cylinder_pattern: "Cylinders",
    cylinder: "Cylinder",
    sphere_pattern: "Spheres",
    sphere: "Sphere",
    torus_pattern: "Fillets / Tori",
    torus: "Fillet / Torus",
    cone_pattern: "Cones",
    cone: "Cone",
    body: "Main Body",
    plane: "Flat Surface",
    plane_pattern: "Flat Surfaces",
  };
  return map[type] ?? type.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}



function TypeIcon({ type = "" }: { type?: string }) {
  const cls = "shrink-0 opacity-60";
  if (type.includes("hole") || type.includes("cylinder") || type.includes("sphere"))
    return <Circle size={14} className={cls} />;
  if (type.includes("body"))
    return <Box size={14} className={cls} />;
  if (type.includes("plane"))
    return <Minus size={14} className={cls} />;
  if (type.includes("cone"))
    return <Triangle size={14} className={cls} />;
  return <Layers size={14} className={cls} />;
}

function DimensionChips({ comp }: { comp: Component }) {
  const chips: { label: string; value: string }[] = [];

  if (comp.diameter != null)
    chips.push({ label: "Ø", value: `${comp.diameter.toFixed(2)} mm` });
  else if (comp.radius != null)
    chips.push({ label: "r", value: `${comp.radius.toFixed(2)} mm` });

  if (comp.length != null)
    chips.push({ label: "L", value: `${comp.length.toFixed(2)} mm` });
  if (comp.width != null)
    chips.push({ label: "W", value: `${comp.width.toFixed(2)} mm` });
  if (comp.height != null)
    chips.push({ label: "H", value: `${comp.height.toFixed(2)} mm` });

  if (comp.major_radius != null)
    chips.push({ label: "R maj", value: `${comp.major_radius.toFixed(2)} mm` });
  if (comp.minor_radius != null)
    chips.push({ label: "R min", value: `${comp.minor_radius.toFixed(2)} mm` });

  if (comp.semi_angle_deg != null)
    chips.push({ label: "∠", value: `${comp.semi_angle_deg.toFixed(1)}°` });

  if (comp.orientation)
    chips.push({ label: "ori", value: comp.orientation });

  return (
    <div className="flex flex-wrap gap-1.5 mt-2">
      {chips.map(({ label, value }) => (
        <span
          key={label}
          className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-foreground/5 border border-border text-[10px] font-mono text-foreground/70"
        >
          <span className="text-foreground/40">{label}</span>
          {value}
        </span>
      ))}
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function PatternSummary({ data }: PatternSummaryProps) {
  const { summary, components = [] } = data;

  return (
    <div className="flex flex-col gap-4 h-full overflow-y-auto pr-1">
      {/* Summary bar */}
      {summary && (
        <div className="grid grid-cols-3 gap-2 text-center">
          {[
            { label: "Components", value: summary.component_count ?? components.length },
            { label: "Instances", value: summary.total_feature_instances ?? "—" },
            { label: "Compression", value: summary.compression_ratio ? `${summary.compression_ratio}×` : "—" },
          ].map(({ label, value }) => (
            <div
              key={label}
              className="bg-foreground/5 border border-border rounded-xl p-2"
            >
              <p className="text-lg font-bold text-foreground">{value}</p>
              <p className="text-[10px] uppercase tracking-wider text-foreground/40 mt-0.5">{label}</p>
            </div>
          ))}
        </div>
      )}

      {/* Topology row */}
      {summary?.topology && (
        <div className="flex items-center gap-2 flex-wrap">
          {Object.entries(summary.topology).map(([k, v]) => (
            <span
              key={k}
              className="text-[10px] px-2 py-0.5 rounded-full bg-foreground/5 border border-border text-foreground/50 font-mono"
            >
              {k}: <span className="text-foreground/80 font-semibold">{v}</span>
            </span>
          ))}
          {summary.volume != null && (
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-foreground/5 border border-border text-foreground/50 font-mono">
              vol: <span className="text-foreground/80 font-semibold">{summary.volume.toFixed(2)}</span>
            </span>
          )}
        </div>
      )}

      {/* Component cards */}
      {components.length === 0 ? (
        <p className="text-sm text-foreground/40 text-center py-6">No components to display.</p>
      ) : (
        <div className="flex flex-col gap-2">
          {components.map((comp) => (
            <div
              key={comp.id}
              className="bg-foreground/[0.03] border border-border rounded-xl p-3 transition-all hover:border-border/80 hover:bg-foreground/5"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <TypeIcon type={comp.type} />
                  <span className="text-sm font-semibold text-foreground truncate">
                    {humanLabel(comp.type)}
                  </span>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  {comp.semantic_label?.map((label) => (
                    <span
                      key={label}
                      className="text-[10px] px-2 py-0.5 rounded-full border font-medium capitalize text-blue-400 bg-blue-500/10 border-blue-500/20"
                    >
                      {label.replace(/_/g, " ")}
                    </span>
                  ))}
                </div>
              </div>
              <DimensionChips comp={comp} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
