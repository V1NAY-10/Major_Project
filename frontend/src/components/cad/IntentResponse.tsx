"use client";
import React, { useState } from 'react';
import { CheckCircle, AlertTriangle, ChevronRight, Zap, Layers, GitBranch } from 'lucide-react';

interface IntentResponseProps {
  response: any; // Flexible - handles both old and new formats
  onConfirm: (selectedInterpretation?: string) => void;
  onCancel: () => void;
}

export default function IntentResponse({
  response,
  onConfirm,
  onCancel
}: IntentResponseProps) {
  const [selectedAlternative, setSelectedAlternative] = useState<string | undefined>();
  const [showJson, setShowJson] = useState(false);

  // Guard: if response is null/undefined, show nothing
  if (!response) return null;

  const status = response.status || 'ready_to_execute';
  const confidence = typeof response.confidence === 'number' ? response.confidence : 1.0;
  const intents = Array.isArray(response.intents) ? response.intents : [];
  const clusters = Array.isArray(response.clusters_detected) ? response.clusters_detected : [];
  const secondaryMods = Array.isArray(response.secondary_modifications) ? response.secondary_modifications : [];
  const alternatives = Array.isArray(response.alternative_interpretations) ? response.alternative_interpretations : [];

  const confidencePct = Math.round(confidence * 100);
  const confidenceColor = confidencePct >= 80 ? 'text-green-400' : confidencePct >= 60 ? 'text-yellow-400' : 'text-red-400';
  const confidenceBg = confidencePct >= 80 ? 'bg-green-500' : confidencePct >= 60 ? 'bg-yellow-500' : 'bg-red-500';

  // ── Needs Confirmation View ──────────────────────────────────────────────────
  if (status === 'needs_confirmation' && alternatives.length > 0) {
    return (
      <div className="space-y-4">
        {/* Header */}
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-xl bg-yellow-500/10 border border-yellow-500/20">
            <AlertTriangle size={16} className="text-yellow-400" />
          </div>
          <div>
            <p className="text-sm font-bold text-yellow-300">Needs Clarification</p>
            <p className="text-xs text-foreground/40">
              Confidence: <span className={confidenceColor}>{confidencePct}%</span> — please select an interpretation
            </p>
          </div>
        </div>

        {/* Confidence Bar */}
        <div className="h-1 bg-white/5 rounded-full overflow-hidden">
          <div className={`h-full ${confidenceBg} rounded-full transition-all`} style={{ width: `${confidencePct}%` }} />
        </div>

        {/* Alternatives */}
        <div className="space-y-2">
          {alternatives.map((alt: string, idx: number) => (
            <label key={idx} className={`flex items-center gap-3 p-3 rounded-xl border cursor-pointer transition-all ${
              selectedAlternative === alt
                ? 'bg-purple-500/20 border-purple-500/40'
                : 'bg-white/5 border-white/5 hover:bg-white/10'
            }`}>
              <input
                type="radio"
                name="alternative"
                value={alt}
                checked={selectedAlternative === alt}
                onChange={(e) => setSelectedAlternative(e.target.value)}
                className="accent-purple-500"
              />
              <span className="text-sm text-foreground/80">{alt}</span>
            </label>
          ))}
        </div>

        {/* JSON Dropdown */}
        <details
          open={showJson}
          onToggle={(e) => setShowJson((e.target as HTMLDetailsElement).open)}
          className="border border-white/5 rounded-xl overflow-hidden"
        >
          <summary className="flex items-center justify-between px-3 py-2 cursor-pointer select-none bg-white/[0.03] hover:bg-white/[0.06] transition-colors list-none">
            <span className="text-[9px] uppercase tracking-widest font-bold text-foreground/25">Raw JSON</span>
            <ChevronRight
              size={10}
              className={`text-foreground/25 transition-transform duration-200 ${showJson ? 'rotate-90' : ''}`}
            />
          </summary>
          <pre className="px-3 py-2 text-[9px] leading-relaxed font-mono text-foreground/40 overflow-x-auto max-h-40 bg-black/20">
            {JSON.stringify(response, null, 2)}
          </pre>
        </details>

        {/* Action Buttons */}
        <div className="flex gap-2 pt-1">
          <button
            onClick={() => onConfirm(selectedAlternative)}
            disabled={!selectedAlternative}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-purple-600 hover:bg-purple-700 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-bold rounded-xl transition-all shadow-lg shadow-purple-500/20"
          >
            <Zap size={14} />
            Confirm &amp; Apply
          </button>
          <button
            onClick={onCancel}
            className="px-4 py-2.5 bg-white/5 hover:bg-white/10 border border-white/10 text-foreground/60 text-sm rounded-xl transition-all"
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  // ── Ready to Execute View ────────────────────────────────────────────────────
  return (
    <div className="space-y-5">
      {/* Header with confidence */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-xl bg-green-500/10 border border-green-500/20">
            <CheckCircle size={16} className="text-green-400" />
          </div>
          <div>
            <p className="text-sm font-bold text-green-300">Ready to Apply</p>
            <p className="text-xs text-foreground/40">
              Confidence: <span className={confidenceColor}>{confidencePct}%</span>
            </p>
          </div>
        </div>
        {/* Confidence bar */}
        <div className="w-24 h-1.5 bg-white/5 rounded-full overflow-hidden">
          <div className={`h-full ${confidenceBg} rounded-full`} style={{ width: `${confidencePct}%` }} />
        </div>
      </div>

      {/* Detected Clusters */}
      {clusters.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Layers size={12} className="text-foreground/30" />
            <p className="text-[10px] uppercase tracking-widest font-bold text-foreground/30">Detected Components</p>
          </div>
          <div className="space-y-1.5">
            {clusters.map((cluster: any, idx: number) => (
              <div key={cluster.cluster_id || idx} className="bg-white/5 border border-white/5 rounded-xl p-3">
                <div className="flex items-center gap-2">
                  <div className="w-1.5 h-1.5 rounded-full bg-purple-500" />
                  <span className="text-xs font-bold text-foreground/70 capitalize">{cluster.type || 'Component'}</span>
                  {cluster.spatial_location && (
                    <span className="text-[9px] px-1.5 py-0.5 bg-purple-500/10 border border-purple-500/20 rounded text-purple-400">
                      {cluster.spatial_location}
                    </span>
                  )}
                </div>
                {Array.isArray(cluster.members) && cluster.members.length > 0 && (
                  <p className="text-[10px] text-foreground/30 font-mono mt-1 ml-3.5">
                    {cluster.members.join(' · ')}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Planned Changes */}
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Zap size={12} className="text-foreground/30" />
          <p className="text-[10px] uppercase tracking-widest font-bold text-foreground/30">Planned Changes</p>
        </div>
        <div className="space-y-1.5">
          {intents.length > 0 ? (
            intents.map((intent: any, idx: number) => (
              <div key={idx} className="bg-white/5 border border-white/5 rounded-xl p-3 space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-mono font-bold text-purple-300">
                    {intent.target_pattern || intent.target_label || `Target ${idx + 1}`}
                  </span>
                  <span className="text-[9px] px-1.5 py-0.5 bg-white/5 border border-white/5 rounded text-foreground/40 font-mono">
                    {intent.action}
                  </span>
                </div>
                {intent.reason && (
                  <p className="text-[10px] text-foreground/40 leading-relaxed">{intent.reason}</p>
                )}
                {typeof intent.confidence === 'number' && (
                  <p className="text-[9px] text-foreground/25">Intent confidence: {Math.round(intent.confidence * 100)}%</p>
                )}
              </div>
            ))
          ) : (
            <div className="bg-white/5 border border-white/5 rounded-xl p-3">
              <p className="text-xs text-foreground/50">
                Apply the modification described in the message above to the 3D model.
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Secondary Modifications */}
      {secondaryMods.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <GitBranch size={12} className="text-foreground/30" />
            <p className="text-[10px] uppercase tracking-widest font-bold text-foreground/30">Cascading Adjustments</p>
          </div>
          <div className="space-y-1.5">
            {secondaryMods.map((mod: any, idx: number) => (
              <div key={idx} className="bg-white/5 border border-white/5 rounded-xl p-3">
                <div className="flex items-center gap-2">
                  <ChevronRight size={10} className="text-foreground/30" />
                  <span className="text-xs font-mono text-foreground/50">
                    {mod.target_pattern || mod.target}: {mod.action}
                  </span>
                </div>
                {mod.reason && (
                  <p className="text-[10px] text-foreground/30 mt-1 ml-4">{mod.reason}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
      {/* JSON Dropdown */}
      <details
        open={showJson}
        onToggle={(e) => setShowJson((e.target as HTMLDetailsElement).open)}
        className="border border-white/5 rounded-xl overflow-hidden"
      >
        <summary className="flex items-center justify-between px-3 py-2 cursor-pointer select-none bg-white/[0.03] hover:bg-white/[0.06] transition-colors list-none">
          <span className="text-[9px] uppercase tracking-widest font-bold text-foreground/25">Raw JSON</span>
          <ChevronRight
            size={10}
            className={`text-foreground/25 transition-transform duration-200 ${showJson ? 'rotate-90' : ''}`}
          />
        </summary>
        <pre className="px-3 py-2 text-[9px] leading-relaxed font-mono text-foreground/40 overflow-x-auto max-h-40 bg-black/20">
          {JSON.stringify(response, null, 2)}
        </pre>
      </details>

      {/* Action Buttons */}
      <div className="flex gap-2 pt-1">
        <button
          onClick={() => onConfirm()}
          className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-purple-600 hover:bg-purple-700 text-white text-sm font-bold rounded-xl transition-all shadow-lg shadow-purple-500/30 hover:shadow-purple-500/50 active:scale-95"
        >
          <Zap size={14} fill="white" />
          Apply Changes
        </button>
        <button
          onClick={onCancel}
          className="px-4 py-3 bg-white/5 hover:bg-white/10 border border-white/10 text-foreground/50 text-sm rounded-xl transition-all"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
