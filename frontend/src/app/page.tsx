"use client";
import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { Zap, FileBox, X, ChevronRight, ChevronLeft, Download, Undo2, Redo2, Loader2, MonitorUp } from "lucide-react";

import Sidebar from "@/components/ui/Sidebar";
import ChatMessage from "@/components/chat/ChatMessage";
import ChatInput, { CAD_ACCEPT } from "@/components/chat/ChatInput";
import CADViewer from "@/components/cad/CADViewer";
import ModelPanel from "@/components/cad/ModelPanel";
import SelectionInspector, { entityIndex, summary } from "@/components/cad/SelectionInspector";
import SettingsBar from "@/components/cad/SettingsBar";

import { apiFetch, postJson } from "@/lib/api";
import { useCADSession, useFreeCADStatus } from "@/hooks/useCADSession";
import { ChatResponse, Message } from "@/types/chat";
import { CadState, CompareResult, DatumLetter, EdgeInfo, PartMesh, SelectMode } from "@/types/cad";
import { DEFAULT_SETTINGS, makeUnits, UnitsContext } from "@/lib/units";

const SUGGESTIONS = [
  "Summarise this part: size, holes, patterns and key dimensions",
  "Make the part 120 mm long but keep the holes centred",
  "Make the 6 mm holes M6 clearance holes",
  "Change the bolt circle to PCD 80",
  "Move the hole 5 mm further from the left edge",
  "Add a 1 mm chamfer to all hole entries",
];

export default function Home() {
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showCadPanel, setShowCadPanel] = useState(true);
  const [dragging, setDragging] = useState(false);
  // Components selected in the viewer / panel; the next message applies to them.
  const [selection, setSelection] = useState<string[]>([]);
  const [selectMode, setSelectMode] = useState<SelectMode>("feature");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const {
    cad,
    setCad,
    isUploading,
    busy,
    resetCadState,
    loadSessionCad,
    upload,
    undo,
    redo,
    modelPath,
    download,
    highlights,
    highlight,
    showInFreeCAD,
    pick,
    freecadSelection,
    updateSettings,
    setLock,
    setDatums,
    postAction,
    checkout,
    renameVersion,
    setMaterial,
    downloadFile,
  } = useCADSession();
  // Version compare overlay (added green / removed red) and the assembly view (visibility, explode).
  const [compare, setCompare] = useState<CompareResult | null>(null);
  const [assembly, setAssembly] = useState<{ version: number; meshes: PartMesh[] } | null>(null);
  const [assemblyOn, setAssemblyOn] = useState(false);
  const [hiddenParts, setHiddenParts] = useState<string[]>([]);
  const [explode, setExplode] = useState(0);
  const [showGhost, setShowGhost] = useState(true);
  // The alternative whose dry-run result is shown as the ghost (while its option set is still current).
  const [optionGhost, setOptionGhost] = useState<{ setId: string; key: number; url: string } | null>(null);
  const freecad = useFreeCADStatus();
  const messagesEndRef = useRef<HTMLDivElement>(null);
  // Session created by this page during upload/send: its data is already in local state, don't refetch.
  const createdHere = useRef<string | null>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  const fetchMessages = useCallback(async (sessionId: string) => {
    try {
      setMessages(await apiFetch<Message[]>(`/sessions/${sessionId}/messages`));
    } catch (err) {
      console.error("Failed to fetch messages", err);
    }
  }, []);

  useEffect(() => {
    if (currentSessionId && currentSessionId === createdHere.current) return;
    // (clearing for "no session" happens in handleNewChat)
    if (currentSessionId) {
      loadSessionCad(currentSessionId);
      // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch; state is set after the request resolves
      fetchMessages(currentSessionId);
    }
  }, [currentSessionId, loadSessionCad, fetchMessages]);

  const ensureSession = async (title: string) => {
    if (currentSessionId) return currentSessionId;
    const s = await postJson<{ id: string }>("/sessions", { title: title.slice(0, 40) });
    createdHere.current = s.id;
    setCurrentSessionId(s.id);
    return s.id;
  };

  const selectSession = (id: string) => {
    createdHere.current = null;
    setError("");
    setSelection([]);
    setCurrentSessionId(id);
  };

  const handleNewChat = () => {
    createdHere.current = null;
    setCurrentSessionId(null);
    setMessages([]);
    setPrompt("");
    setError("");
    setSelection([]);
    resetCadState();
  };

  const onFileUpload = async (file: File) => {
    setError("");
    try {
      const sessionId = await ensureSession(`CAD: ${file.name}`);
      await upload(file, sessionId);
      setSelection([]);
      await fetchMessages(sessionId);
      setShowCadPanel(true);
    } catch (err) {
      setError((err as Error).message || "Failed to upload CAD file");
    }
  };

  const handleSend = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const text = prompt.trim();
    if (!text || loading) return;
    setPrompt("");
    await sendText(text, cad ? selection : []);
  };

  const sendText = async (text: string, target: string[]) => {
    setError("");
    setLoading(true);
    setMessages((prev) => [...prev, { role: "user", content: text, selection: target }]);
    let sessionId: string | null = currentSessionId;
    try {
      sessionId = await ensureSession(text);
      const res = await postJson<ChatResponse>("/chat", {
        session_id: sessionId,
        message: text,
        selection: target,
      });
      setMessages((prev) => [...prev, res.message]);
      if (res.state) setCad(res.state);
      // Alternatives offered: show the first workable one as the ghost straight away.
      const first = res.message.options?.options.find((o) => o.ok && o.mesh_url);
      setOptionGhost(first && res.message.options ? { setId: res.message.options.id, key: first.key, url: first.mesh_url! } : null);
      // Keep showing the selection (IDs are stable across edits); otherwise show what the reply touched.
      if (target.length) highlight(target);
      else if (res.highlight?.length) highlight(res.highlight);
    } catch (err) {
      setError((err as Error).message || "Something went wrong");
      if (target === selection) setPrompt(text);
      // The backend stores the user message and the error; resync so the transcript matches it.
      if (sessionId) await fetchMessages(sessionId);
      else setMessages((prev) => prev.slice(0, -1));
    } finally {
      setLoading(false);
    }
  };

  const handleStep = async (dir: "undo" | "redo") => {
    if (!currentSessionId) return;
    setError("");
    try {
      await (dir === "undo" ? undo : redo)(currentSessionId);
      if (selection.length) highlight(selection);
      await fetchMessages(currentSessionId);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  /** Approve / cancel the pending plan (the preview shown as a ghost). */
  const planAction = async (what: "apply" | "cancel") => {
    if (!currentSessionId) return;
    setError("");
    setLoading(true);
    try {
      const res = await postAction<{ message: Message; state: CadState }>(`/cad/plan/{sid}/${what}`);
      if (res?.message) setMessages((prev) => [...prev, res.message]);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  /** Apply one of the offered alternatives as a new version. */
  const applyOption = async (setId: string, key: number) => {
    setError("");
    setLoading(true);
    try {
      const res = await postAction<{ message: Message; state: CadState }>(`/cad/options/{sid}/apply`, { key, set_id: setId });
      if (res?.message) setMessages((prev) => [...prev, res.message]);
      setOptionGhost(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  /** A clickable answer to the agent's question: select it and answer with it. */
  const onChoose = (id: string) => {
    select([id]);
    sendText(`Use ${id}`, [id]);
  };

  /** Apply one tool call as a new version (DFM fixes). */
  const runTool = async (tool: string, args: Record<string, unknown>, label: string) => {
    const res = await postAction<{ message: Message; state: CadState; highlight: string[] }>(`/cad/run-tool/{sid}`, { tool, args, label });
    if (res?.message) setMessages((prev) => [...prev, res.message]);
    if (res?.highlight?.length) highlight(res.highlight);
  };

  const onCompare = async (a: number, b: number) => {
    if (!currentSessionId) return;
    try {
      setCompare(await apiFetch<CompareResult>(`/cad/compare/${currentSessionId}?a=${a}&b=${b}`));
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const toggleAssembly = async () => {
    if (assemblyOn) {
      setAssemblyOn(false);
      return;
    }
    if (!cad) return;
    try {
      const res = await apiFetch<{ parts: PartMesh[] }>(`/cad/part-meshes/${cad.session_id}`);
      setAssembly({ version: cad.version, meshes: res.parts });
      setAssemblyOn(true);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const insertRef = (ref: string) => {
    const r = ref.trimEnd();
    setPrompt((p) => (p && !p.endsWith(" ") ? `${p} ${r} ` : `${p}${r} `));
  };

  const focusInput = () => setTimeout(() => textareaRef.current?.focus(), 0);

  /** Select components (replace, or toggle with Shift/Ctrl) and highlight them in the viewer and FreeCAD. */
  const select = (ids: string[], additive = false) => {
    let next = ids;
    if (additive) {
      next = [...selection];
      for (const id of ids) next = next.includes(id) ? next.filter((x) => x !== id) : [...next, id];
    }
    setSelection(next);
    highlight(next);
  };

  const onViewerPick = async (point: [number, number, number], additive: boolean) => {
    try {
      const res = await pick(point, selectMode);
      if (res?.id) select([res.id], additive);
      else if (!additive) select([]);
    } catch (err) {
      setError((err as Error).message || "Could not pick that point");
    }
  };

  const fromFreeCAD = async () => {
    try {
      const ids = await freecadSelection();
      if (ids.length) select(ids);
      else setError("Nothing is selected in the FreeCAD window. Click a face or part there first.");
    } catch (err) {
      setError((err as Error).message || "FreeCAD is not reachable");
    }
  };

  /** Dimension click: select it and start the message "P1-P2 = " so the user just types the value. */
  const onDimension = (id: string) => {
    select([id]);
    setPrompt(`${id} = `);
    focusInput();
  };

  const onAction = (text: string) => {
    setPrompt(text);
    focusInput();
  };

  /** Locks, datums and settings report backend errors in the banner. */
  const report = (p: Promise<unknown>) => p.catch((err) => setError((err as Error).message || "Request failed"));
  const onLock = (target: string, locked: boolean, mode: "value" | "centered" = "value") => report(setLock(target, locked, mode));
  const onDatum = (letter: DatumLetter, planeId: string) => report(setDatums({ [letter]: planeId }));

  const units = useMemo(() => makeUnits(cad?.settings ?? DEFAULT_SETTINGS), [cad?.settings]);
  // Edges aren't in the model info: keep the data of those picked / highlighted (cleared with a new version).
  const edges = useMemo(
    () => new Map<string, EdgeInfo>(highlights.filter((h) => h.edge).map((h) => [h.id, h.edge as EdgeInfo])),
    [highlights]
  );
  const index = useMemo(() => (cad ? entityIndex(cad.info, edges) : new Map()), [cad, edges]);
  const selectionChips = selection.map((id) => ({ id, text: summary(index.get(id), units) }));

  return (
    <UnitsContext.Provider value={units}>
      <div className="flex h-screen bg-background text-foreground overflow-hidden font-sans">
        <Sidebar currentSessionId={currentSessionId} onSelectSession={selectSession} onNewChat={handleNewChat} />

        <main className="flex-1 flex flex-row relative md:ml-64 transition-all duration-300 overflow-hidden h-full">
          {/* Chat */}
          <div
            className="flex-1 flex flex-col relative h-full min-w-0 overflow-hidden"
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              const file = e.dataTransfer.files[0];
              if (file) onFileUpload(file);
            }}
          >
            {dragging && (
              <div className="absolute inset-4 z-40 rounded-3xl border-2 border-dashed border-purple-500 bg-purple-500/10 flex items-center justify-center pointer-events-none">
                <p className="font-bold text-purple-400">Drop a CAD file ({CAD_ACCEPT.replaceAll(",", " ")})</p>
              </div>
            )}

            <header className="sticky top-0 z-30 px-4 py-3 border-b border-border bg-background/40 backdrop-blur-xl flex items-center justify-between gap-3 shrink-0">
              <div className="flex items-center gap-2 min-w-0">
                <div className="p-1.5 bg-purple-600 rounded-lg shadow-lg shadow-purple-500/20">
                  <Zap size={18} fill="white" className="text-white" />
                </div>
                <h1 className="font-bold tracking-tight">CAD Copilot</h1>
                {cad && (
                  <span className="text-xs bg-foreground/5 px-2 py-0.5 rounded border border-border text-foreground/50 flex items-center gap-1.5 font-mono truncate">
                    <FileBox size={12} /> {cad.filename} · v{cad.version}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {cad && (
                  <>
                    <button
                      onClick={() => handleStep("undo")}
                      disabled={!cad.can_undo || busy || loading}
                      title="Undo last change"
                      className="p-2 rounded-lg hover:bg-foreground/10 disabled:opacity-25"
                    >
                      <Undo2 size={16} />
                    </button>
                    <button
                      onClick={() => handleStep("redo")}
                      disabled={!cad.can_redo || busy || loading}
                      title="Redo"
                      className="p-2 rounded-lg hover:bg-foreground/10 disabled:opacity-25"
                    >
                      <Redo2 size={16} />
                    </button>
                  </>
                )}
                <span
                  title={freecad?.connected ? `FreeCAD ${freecad.version} (${freecad.gui ? "GUI" : "headless"})` : freecad?.error}
                  className={`flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest px-2 py-1 rounded-full border ${
                    freecad?.connected
                      ? "border-green-500/30 text-green-500 bg-green-500/10"
                      : "border-amber-500/30 text-amber-500 bg-amber-500/10"
                  }`}
                >
                  <span className={`w-1.5 h-1.5 rounded-full ${freecad?.connected ? "bg-green-500" : "bg-amber-500"}`} />
                  {freecad?.connected ? "FreeCAD" : "FreeCAD offline"}
                </span>
              </div>
            </header>

            <div className="flex-1 overflow-y-auto p-4 md:p-8 space-y-6 custom-scrollbar">
              {messages.length === 0 && !loading && (
                <div className="h-full flex flex-col items-center justify-center text-center space-y-4 max-w-md mx-auto">
                  <div className="w-20 h-20 bg-purple-500/10 rounded-3xl flex items-center justify-center border border-purple-500/20 mb-4">
                    <Zap size={40} className="text-purple-400" />
                  </div>
                  <h2 className="text-3xl font-black tracking-tight">Edit CAD by talking to it.</h2>
                  <p className="text-foreground/50 text-sm leading-relaxed">
                    Attach or drop a STEP, IGES, BREP or FreeCAD file. It opens in FreeCAD, and you can ask questions or
                    request precise changes. Every edit is measured, versioned and mirrored here and in FreeCAD.
                  </p>
                </div>
              )}

              {messages.map((msg, i) => (
                <ChatMessage
                  key={msg.id ?? i}
                  message={msg}
                  pendingPlanId={cad?.pending?.id ?? null}
                  onApplyPlan={() => planAction("apply")}
                  onCancelPlan={() => planAction("cancel")}
                  onChoose={onChoose}
                  onHoverChoice={(id) => highlight(id ? [id] : selection)}
                  activeOptionsId={cad?.options?.id ?? null}
                  previewOption={optionGhost && optionGhost.setId === cad?.options?.id ? optionGhost.key : null}
                  onPreviewOption={(o) =>
                    setOptionGhost(o?.mesh_url && msg.options ? { setId: msg.options.id, key: o.key, url: o.mesh_url } : null)
                  }
                  onApplyOption={(key) => msg.options && applyOption(msg.options.id, key)}
                  busy={loading}
                />
              ))}

              {(loading || isUploading) && (
                <div className="flex justify-start">
                  <div className="bg-foreground/5 border border-border rounded-2xl px-4 py-3 flex items-center gap-3">
                    <Loader2 size={14} className="animate-spin text-purple-500" />
                    <span className="text-xs font-bold uppercase tracking-widest text-foreground/50">
                      {isUploading ? "Opening in FreeCAD…" : "Working in FreeCAD…"}
                    </span>
                  </div>
                </div>
              )}

              {error && (
                <div className="mx-auto max-w-lg p-4 bg-red-500/10 border border-red-500/20 rounded-2xl text-red-400 text-xs font-medium flex items-start gap-2">
                  <X size={14} className="shrink-0 mt-0.5" /> <span className="flex-1">{error}</span>
                  <button onClick={() => setError("")} className="opacity-60 hover:opacity-100">
                    dismiss
                  </button>
                </div>
              )}
              <div ref={messagesEndRef} className="h-4" />
            </div>

            <ChatInput
              prompt={prompt}
              setPrompt={setPrompt}
              loading={loading}
              isUploadingCad={isUploading}
              onUpload={onFileUpload}
              onSubmit={handleSend}
              hasCad={!!cad}
              suggestions={messages.length <= 1 ? SUGGESTIONS : []}
              selection={selectionChips}
              onRemoveSelection={(id) => select([id], true)}
              textareaRef={textareaRef}
            />
          </div>

          {/* CAD workspace */}
          {cad && (
            <div
              className={`flex-none border-l border-border bg-card/30 backdrop-blur-3xl transition-all duration-500 flex flex-col h-full ${
                showCadPanel ? "w-[460px]" : "w-0 overflow-hidden border-none"
              }`}
            >
              <div className="px-5 py-3 border-b border-border flex items-center justify-between shrink-0">
                <div className="flex items-center gap-3">
                  <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                  <h2 className="text-[10px] font-black uppercase tracking-[0.4em] text-foreground/50">3D Workspace</h2>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => showInFreeCAD().catch((err) => setError(err.message || "FreeCAD is not reachable"))}
                    disabled={!freecad?.connected}
                    className="p-2 hover:bg-foreground/10 rounded-xl text-foreground/50 hover:text-foreground disabled:opacity-25"
                    title="Show this model in FreeCAD"
                  >
                    <MonitorUp size={16} />
                  </button>
                  <div className="relative group/dl">
                    <button className="p-2 hover:bg-foreground/10 rounded-xl text-foreground/50 hover:text-foreground" title="Download">
                      <Download size={16} />
                    </button>
                    <div className="absolute right-0 top-full z-50 hidden group-hover/dl:block bg-card border border-border rounded-xl shadow-xl py-1 min-w-[150px]">
                      {[
                        ["step", "STEP (.step)"],
                        ["fcstd", "FreeCAD (.FCStd)"],
                        ["stl", "Mesh (.stl)"],
                        ["iges", "IGES (.igs)"],
                      ].map(([fmt, label]) => (
                        <button
                          key={fmt}
                          onClick={() => download(fmt).catch((err) => setError(err.message || "Download failed"))}
                          className="block w-full text-left px-3 py-1.5 text-xs hover:bg-foreground/5"
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>
                  <button
                    onClick={() => setShowCadPanel(false)}
                    className="p-2 hover:bg-foreground/10 rounded-xl text-foreground/50 hover:text-foreground"
                  >
                    <ChevronRight size={18} />
                  </button>
                </div>
              </div>

              <div className="flex-1 overflow-y-auto p-5 space-y-5 custom-scrollbar">
                <SettingsBar settings={cad.settings ?? DEFAULT_SETTINGS} onChange={(patch) => report(updateSettings(patch))} />
                <div className="relative h-[340px] bg-black/5 dark:bg-black/40 rounded-[1.5rem] overflow-hidden border border-border">
                  <CADViewer
                    modelPath={modelPath}
                    fitKey={`${cad.session_id}:${cad.filename}`}
                    highlights={highlights}
                    onPick={onViewerPick}
                    ghostPath={
                      !showGhost
                        ? null
                        : optionGhost && optionGhost.setId === cad.options?.id
                          ? optionGhost.url
                          : cad.pending?.ok
                            ? cad.pending.mesh_url
                            : null
                    }
                    diff={compare?.geometry.meshes ?? []}
                    parts={
                      assemblyOn && assembly && assembly.version === cad.version
                        ? { meshes: assembly.meshes, hidden: hiddenParts, explode }
                        : null
                    }
                    overlay={
                      assemblyOn && assembly ? (
                        <div className="rounded-lg bg-background/85 backdrop-blur border border-border p-2 space-y-1 text-[10px]">
                          <label className="flex items-center gap-2" htmlFor="explode">
                            explode
                            <input id="explode" type="range" min={0} max={1.5} step={0.05} value={explode}
                              onChange={(e) => setExplode(Number(e.target.value))} />
                          </label>
                          <div className="flex flex-wrap gap-1 max-h-20 overflow-y-auto">
                            {assembly.meshes.map((m) => (
                              <label key={m.part} className="flex items-center gap-1" title={m.label}>
                                <input type="checkbox" checked={!hiddenParts.includes(m.part)}
                                  onChange={() => setHiddenParts((h) => (h.includes(m.part) ? h.filter((x) => x !== m.part) : [...h, m.part]))} />
                                <span className="font-mono">{m.part}</span>
                              </label>
                            ))}
                          </div>
                          {assembly.version !== cad.version && <p className="text-amber-600">model changed: toggle Assembly again</p>}
                        </div>
                      ) : null
                    }
                    toolbar={
                      <>
                        {(cad.pending?.ok || (optionGhost && optionGhost.setId === cad.options?.id)) && (
                          <button
                            onClick={() => setShowGhost((g) => !g)}
                            title="Show / hide the previewed result"
                            className={`rounded-lg border px-2 py-1 text-[10px] font-semibold ${showGhost ? "bg-cyan-500/20 border-cyan-500/50" : "bg-background/80 border-border"}`}
                          >
                            Preview
                          </button>
                        )}
                        {cad.info.objects.length > 1 && (
                          <button
                            onClick={toggleAssembly}
                            title="Assembly view: hide parts, explode the assembly"
                            className={`rounded-lg border px-2 py-1 text-[10px] font-semibold ${assemblyOn ? "bg-purple-600 text-white border-purple-600" : "bg-background/80 border-border"}`}
                          >
                            Assembly
                          </button>
                        )}
                        <div className="flex rounded-lg bg-background/80 backdrop-blur border border-border p-0.5 text-[10px] font-semibold">
                          {(["feature", "edge", "part"] as const).map((m) => (
                            <button
                              key={m}
                              onClick={() => setSelectMode(m)}
                              title={m === "feature" ? "Clicks select a feature (hole, face, slot, fillet…)" : m === "edge" ? "Clicks select the nearest edge (E#), e.g. to fillet or chamfer it" : "Clicks select a whole part"}
                              className={`px-2 py-0.5 rounded-md ${
                                selectMode === m ? "bg-purple-600 text-white" : "text-foreground/60 hover:text-foreground"
                              }`}
                            >
                              {m === "feature" ? "Feature" : m === "edge" ? "Edge" : "Part"}
                            </button>
                          ))}
                        </div>
                        <button
                          onClick={fromFreeCAD}
                          disabled={!freecad?.gui}
                          title="Select here what is selected in the FreeCAD window"
                          className="rounded-lg bg-background/80 backdrop-blur border border-border px-2 py-1 text-[10px] font-semibold text-foreground/60 hover:text-foreground disabled:opacity-30"
                        >
                          From FreeCAD
                        </button>
                      </>
                    }
                  />
                </div>
                <SelectionInspector
                  cad={cad}
                  selection={selection}
                  onSelect={(id, additive) => select([id], additive)}
                  edges={edges}
                  onRemove={(id) => select([id], true)}
                  onClear={() => select([])}
                  onAction={onAction}
                  onDimension={onDimension}
                  onLock={onLock}
                  onDatum={onDatum}
                />
                <ModelPanel
                  cad={cad}
                  selection={selection}
                  onSelect={(id, additive) => select([id], additive)}
                  onDimension={onDimension}
                  onInsert={insertRef}
                  onLock={onLock}
                  onDatum={onDatum}
                  onCheckout={(v) => report(checkout(v).then(async () => { if (currentSessionId) await fetchMessages(currentSessionId); }))}
                  onRename={(v, n) => report(renameVersion(v, n))}
                  onCompare={onCompare}
                  compare={compare}
                  onClearCompare={() => setCompare(null)}
                  onRunTool={runTool}
                  setMaterial={setMaterial}
                  downloadFile={downloadFile}
                  onError={setError}
                />
              </div>
            </div>
          )}

          {cad && !showCadPanel && (
            <button
              onClick={() => setShowCadPanel(true)}
              className="absolute right-0 top-1/2 -translate-y-1/2 p-3 bg-purple-600 text-white rounded-l-2xl hover:pr-5 transition-all shadow-2xl shadow-purple-500/40 z-50"
            >
              <ChevronLeft size={20} />
            </button>
          )}
        </main>
      </div>
    </UnitsContext.Provider>
  );
}
