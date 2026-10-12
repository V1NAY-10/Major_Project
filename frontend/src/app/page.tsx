"use client";
import { useState, useEffect, useRef, useCallback } from "react";
import { Zap, FileBox, X, ChevronRight, ChevronLeft, Download, Undo2, Redo2, Loader2 } from "lucide-react";
import { useAuth, useUser } from "@clerk/nextjs";

import Sidebar from "@/components/ui/Sidebar";
import ChatMessage from "@/components/chat/ChatMessage";
import ChatInput, { CAD_ACCEPT } from "@/components/chat/ChatInput";
import CADViewer from "@/components/cad/CADViewer";
import ModelPanel from "@/components/cad/ModelPanel";

import { apiFetch, postJson, setAuthTokenGetter } from "@/lib/api";
import { useCADSession, useFreeCADStatus } from "@/hooks/useCADSession";
import { Message } from "@/types/chat";
import { CadState } from "@/types/cad";

const SUGGESTIONS = [
  "Summarise this part: size, holes and main faces",
  "List all holes with diameters and positions",
  "Make the overall length 10 mm longer",
  "Add a 1 mm chamfer to all hole entries",
];

export default function Home() {
  const { user } = useUser();
  const { getToken } = useAuth();
  // Every backend request carries the Clerk session token; the backend derives the user from it.
  setAuthTokenGetter(getToken);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showCadPanel, setShowCadPanel] = useState(true);
  const [dragging, setDragging] = useState(false);

  const { cad, setCad, isUploading, busy, resetCadState, loadSessionCad, upload, undo, redo, modelPath, download } =
    useCADSession();
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
    if (currentSessionId && user?.id) {
      loadSessionCad(currentSessionId);
      fetchMessages(currentSessionId);
    } else if (!currentSessionId) {
      setMessages([]);
      resetCadState();
    }
  }, [currentSessionId, user?.id, loadSessionCad, fetchMessages, resetCadState]);

  const ensureSession = async (title: string) => {
    if (currentSessionId) return currentSessionId;
    if (!user?.id) throw new Error("Please sign in first");
    const s = await postJson<{ id: string }>("/sessions", { title: title.slice(0, 40) });
    createdHere.current = s.id;
    setCurrentSessionId(s.id);
    return s.id;
  };

  const selectSession = (id: string) => {
    createdHere.current = null;
    setError("");
    setCurrentSessionId(id);
  };

  const handleNewChat = () => {
    createdHere.current = null;
    setCurrentSessionId(null);
    setMessages([]);
    setPrompt("");
    setError("");
    resetCadState();
  };

  const onFileUpload = async (file: File) => {
    setError("");
    try {
      const sessionId = await ensureSession(`CAD: ${file.name}`);
      await upload(file, sessionId);
      await fetchMessages(sessionId);
      setShowCadPanel(true);
    } catch (err: any) {
      setError(err.message || "Failed to upload CAD file");
    }
  };

  const handleSend = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const text = prompt.trim();
    if (!text || loading) return;
    setError("");
    setLoading(true);
    setPrompt("");
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    let sessionId: string | null = currentSessionId;
    try {
      sessionId = await ensureSession(text);
      const res = await postJson<{ message: Message; state: CadState | null }>("/chat", {
        session_id: sessionId,
        message: text,
      });
      setMessages((prev) => [...prev, res.message]);
      if (res.state) setCad(res.state);
    } catch (err: any) {
      setError(err.message || "Something went wrong");
      setPrompt(text);
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
      await fetchMessages(currentSessionId);
    } catch (err: any) {
      setError(err.message);
    }
  };

  const insertRef = (ref: string) => setPrompt((p) => (p && !p.endsWith(" ") ? `${p} ${ref} ` : `${p}${ref} `));

  return (
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
              <ChatMessage key={msg.id ?? i} message={msg} />
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
              <div className="relative h-[340px] bg-black/5 dark:bg-black/40 rounded-[1.5rem] overflow-hidden border border-border">
                <CADViewer modelPath={modelPath} fitKey={`${cad.session_id}:${cad.filename}`} />
              </div>
              <ModelPanel cad={cad} onPick={insertRef} />
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
  );
}
