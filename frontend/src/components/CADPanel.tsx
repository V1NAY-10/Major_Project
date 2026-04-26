"use client";
import { useState, useRef, useCallback } from "react";
import {
  Upload, Send, Loader2, FileBox, AlertCircle, X, ChevronRight,
} from "lucide-react";
import PatternSummary from "./PatternSummary";
import IntentResponse from "./IntentResponse";
import CADViewer from "./CADViewer";
import { API_URL } from "@/lib/api";
import { ParsedData } from "./PatternSummary";

// ── Types ─────────────────────────────────────────────────────────────────────

interface IntentData {
  intents: Array<{
    target_pattern?: string;
    target_label?: string;
    action?: string;
    value?: number | string;
    reason?: string;
    [key: string]: unknown;
  }>;
  preview: Array<{
    pattern: string;
    before: any;
    after: any;
    count: number;
  }>;
  warnings: Array<{ warning: string }>;
}



// ── Sub-components ─────────────────────────────────────────────────────────────

function DropZone({
  onFile,
  uploading,
  fileName,
}: {
  onFile: (file: File) => void;
  uploading: boolean;
  fileName: string | null;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      const file = e.dataTransfer.files[0];
      if (file) onFile(file);
    },
    [onFile]
  );

  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      onClick={() => !uploading && inputRef.current?.click()}
      className={`relative flex flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed p-8 cursor-pointer transition-all
        ${dragging
          ? "border-primary bg-primary/10"
          : "border-border hover:border-primary/40 hover:bg-foreground/5 bg-foreground/[0.02]"
        }`}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".step,.stp,.STEP,.STP"
        className="hidden"
        onChange={(e) => { if (e.target.files?.[0]) onFile(e.target.files[0]); }}
      />

      {uploading ? (
        <>
          <Loader2 size={28} className="animate-spin text-primary" />
          <p className="text-sm text-foreground/60">Parsing CAD file…</p>
        </>
      ) : fileName ? (
        <>
          <FileBox size={28} className="text-primary" />
          <div className="text-center">
            <p className="text-sm font-semibold text-foreground truncate max-w-[200px]">{fileName}</p>
            <p className="text-[11px] text-foreground/40 mt-0.5">Click or drop to replace</p>
          </div>
        </>
      ) : (
        <>
          <div className="p-3 rounded-xl bg-foreground/5 border border-border">
            <Upload size={22} className="text-foreground/50" />
          </div>
          <div className="text-center">
            <p className="text-sm font-semibold text-foreground">Drop STEP file here</p>
            <p className="text-[11px] text-foreground/40 mt-0.5">or click to browse • .step / .stp</p>
          </div>
        </>
      )}
    </div>
  );
}

function ErrorBanner({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  return (
    <div className="flex items-center gap-3 p-3 bg-red-500/10 border border-red-500/25 rounded-xl text-red-400 text-sm animate-in fade-in slide-in-from-top-1 duration-200">
      <AlertCircle size={15} className="shrink-0" />
      <span className="flex-1">{message}</span>
      <button onClick={onDismiss} className="p-0.5 hover:bg-red-500/10 rounded-lg transition-colors">
        <X size={14} />
      </button>
    </div>
  );
}

// ── Main panel ─────────────────────────────────────────────────────────────────

export default function CADPanel() {
  // Upload state
  const [uploading, setUploading] = useState(false);
  const [fileName, setFileName] = useState<string | null>(null);
  const [fileId, setFileId] = useState<string | null>(null);
  const [modelUrl, setModelUrl] = useState<string | null>(null);
  const [parsedData, setParsedData] = useState<ParsedData | null>(null);

  // Prompt state
  const [prompt, setPrompt] = useState("");
  const [interpreting, setInterpreting] = useState(false);
  const [intentResponse, setIntentResponse] = useState<IntentData | null>(null);

  // Error state
  const [uploadError, setUploadError] = useState("");
  const [intentError, setIntentError] = useState("");

  // ── Upload handler ────────────────────────────────────────────────────────
  const handleFile = useCallback(async (file: File) => {
    setUploading(true);
    setUploadError("");
    setParsedData(null);
    setFileId(null);
    setModelUrl(null);
    setIntentResponse(null);
    setFileName(file.name);

    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await fetch(`${API_URL}/cad/upload`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({ detail: "Upload failed" }));
        throw new Error(detail.detail ?? "Upload failed");
      }
      const data = await res.json();
      // The backend returns { file_id, filename, parsed_data: {...} }
      if (data.file_id) {
        setFileId(data.file_id);
        setModelUrl(`/cad/model/${data.file_id}`);
      }
      if (data.parsed_data) {
        setParsedData(data.parsed_data as ParsedData);
      } else {
        // Fallback if the API shape changes or returns it directly
        setParsedData(data as ParsedData);
      }
    } catch (err: unknown) {
      setUploadError(err instanceof Error ? err.message : "Failed to parse CAD file");
      setFileName(null);
    } finally {
      setUploading(false);
    }
  }, []);

  // ── Intent handler ────────────────────────────────────────────────────────
  const handleInterpret = useCallback(
    async (e?: React.FormEvent) => {
      if (e) e.preventDefault();
      if (!prompt.trim() || !parsedData || interpreting) return;

      setInterpreting(true);
      setIntentError("");
      setIntentResponse(null);

      try {
        const res = await fetch(`${API_URL}/cad/interpret`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt: prompt.trim(), parsed_data: parsedData }),
        });
        if (!res.ok) {
          const detail = await res.json().catch(() => ({ detail: "Interpretation failed" }));
          throw new Error(detail.detail ?? "Failed to interpret prompt");
        }
        const data: IntentData = await res.json();
        setIntentResponse(data);

        // Chain the request to physically modify the model geometry and regenerate STL
        if (data.intents && data.intents.length > 0 && fileId) {
          const modRes = await fetch(`${API_URL}/cad/modify-model`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ file_id: fileId, intents: data.intents }),
          });
          
          if (modRes.ok) {
            const modData = await modRes.json();
            
            if (modData.error) {
              setIntentError("Modification failed. Try smaller values.");
              console.error("Modify error:", modData.reason);
            }
            
            if (modData.mesh_url) {
              setModelUrl(modData.mesh_url);
            }
          } else {
            setIntentError("Modification failed. Try smaller values.");
          }
        }
      } catch (err: unknown) {
        setIntentError(err instanceof Error ? err.message : "Failed to interpret prompt");
      } finally {
        setInterpreting(false);
      }
    },
    [prompt, parsedData, interpreting]
  );

  const hasParsedData = parsedData !== null;
  const highlightText = intentResponse?.intents?.[0]?.target_label || null;

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="w-full max-w-6xl mx-auto p-4 md:p-6 flex flex-col gap-4">
      {/* Section heading */}
      <div className="flex items-center gap-2">
        <FileBox size={18} className="text-primary" />
        <h2 className="text-base font-bold text-foreground tracking-tight">CAD Intent Tester</h2>
        <ChevronRight size={14} className="text-foreground/30" />
        <span className="text-xs text-foreground/40">Upload → Parse → Interpret</span>
      </div>

      {/* Main split layout */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* ── LEFT: Upload + Parsed Data ─────────────────────────────────── */}
        <div className="flex flex-col gap-3">
          <DropZone onFile={handleFile} uploading={uploading} fileName={fileName} />

          {uploadError && (
            <ErrorBanner message={uploadError} onDismiss={() => setUploadError("")} />
          )}

          {hasParsedData && (
            <div className="bg-card border border-border rounded-2xl p-4 shadow-sm flex-1 flex flex-col gap-4">
              <div>
                <p className="text-[10px] uppercase tracking-widest font-bold text-foreground/40 mb-3">
                  Parsed Patterns
                </p>
                <div className="max-h-[300px] overflow-y-auto pr-2">
                  <PatternSummary data={parsedData!} />
                </div>
              </div>
              
              <div className="flex-1 flex flex-col min-h-[300px]">
                <p className="text-[10px] uppercase tracking-widest font-bold text-foreground/40 mb-3">
                  3D Viewer
                </p>
                <CADViewer modelUrl={modelUrl} highlightText={highlightText} />
              </div>
            </div>
          )}

          {!hasParsedData && !uploading && !uploadError && (
            <div className="bg-card border border-border rounded-2xl p-4 flex flex-col items-center justify-center text-center gap-2 min-h-[120px]">
              <p className="text-sm text-foreground/30">Upload a STEP file to see parsed patterns here.</p>
            </div>
          )}
        </div>

        {/* ── RIGHT: Prompt + Intent Response ───────────────────────────── */}
        <div className="flex flex-col gap-3">
          {/* Prompt input */}
          <div className="bg-card border border-border rounded-2xl p-4 shadow-sm">
            <p className="text-[10px] uppercase tracking-widest font-bold text-foreground/40 mb-3">
              Test Prompt
            </p>
            <form onSubmit={handleInterpret} className="relative">
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleInterpret();
                  }
                }}
                disabled={!hasParsedData}
                placeholder={
                  hasParsedData
                    ? "e.g. increase small hole diameter, add support cylinders…"
                    : "Upload a CAD file first…"
                }
                rows={3}
                className="w-full bg-foreground/5 hover:bg-foreground/[0.07] focus:bg-foreground/[0.07]
                  border border-border focus:border-primary/50
                  rounded-xl p-3 pr-12 text-foreground placeholder-foreground/25
                  focus:outline-none transition-all resize-none text-sm
                  disabled:opacity-40 disabled:cursor-not-allowed"
              />
              <button
                type="submit"
                disabled={interpreting || !hasParsedData || !prompt.trim()}
                className="absolute right-2.5 bottom-2.5 p-2 bg-purple-600 hover:bg-purple-500
                  disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-purple-600
                  rounded-xl transition-all active:scale-95 shadow-lg"
              >
                {interpreting
                  ? <Loader2 size={16} className="animate-spin" />
                  : <Send size={16} />
                }
              </button>
            </form>

            {/* Suggestion chips */}
            {hasParsedData && !intentResponse && (
              <div className="flex flex-wrap gap-1.5 mt-3">
                {[
                  "Increase small hole diameter",
                  "Remove all support cylinders",
                  "Change main surface height",
                ].map((s) => (
                  <button
                    key={s}
                    onClick={() => setPrompt(s)}
                    className="text-[11px] px-2.5 py-1 bg-foreground/5 hover:bg-foreground/10
                      border border-border rounded-full text-foreground/60 hover:text-foreground
                      transition-all"
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Intent error */}
          {intentError && (
            <ErrorBanner message={intentError} onDismiss={() => setIntentError("")} />
          )}

          {/* Loading skeleton */}
          {interpreting && (
            <div className="bg-card border border-border rounded-2xl p-4 shadow-sm animate-pulse">
              <div className="h-3 w-24 bg-foreground/10 rounded mb-4" />
              {[80, 60, 70, 90].map((w, i) => (
                <div key={i} className="h-2.5 bg-foreground/10 rounded mb-2.5" style={{ width: `${w}%` }} />
              ))}
            </div>
          )}

          {/* Intent response */}
          {intentResponse && !interpreting && (
            <div className="bg-card border border-border rounded-2xl p-4 shadow-sm">
              <p className="text-[10px] uppercase tracking-widest font-bold text-foreground/40 mb-3">
                Interpreted Intent
              </p>
              <IntentResponse intent={intentResponse} />
            </div>
          )}

          {/* Empty state */}
          {!intentResponse && !interpreting && !intentError && (
            <div className="bg-card border border-border rounded-2xl p-4 flex flex-col items-center justify-center text-center gap-2 min-h-[120px]">
              <p className="text-sm text-foreground/30">
                {hasParsedData
                  ? "Type a prompt above to interpret intent."
                  : "Waiting for a CAD file upload…"}
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
