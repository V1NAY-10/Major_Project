"use client";
import { useState, useEffect, useRef } from "react";
import { Zap, MessageSquare, FileBox, X, ChevronRight, ChevronLeft, Download } from "lucide-react";
import { useUser } from "@clerk/nextjs";

// Components
import Sidebar from "@/components/ui/Sidebar";
import ChatMessage from "@/components/chat/ChatMessage";
import ChatInput from "@/components/chat/ChatInput";
import CADViewer from "@/components/cad/CADViewer";
import PatternSummary from "@/components/cad/PatternSummary";
import IntentResponse from "@/components/cad/IntentResponse";

// Hooks & Utils
import { API_URL } from "@/lib/api";
import { useCADSession } from "@/hooks/useCADSession";
import { Message } from "@/types/chat";

export default function Home() {
  const { user } = useUser();
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [showCadPanel, setShowCadPanel] = useState(true);

  const { 
    fileId: cadFileId, 
    parsedData: cadParsedData, 
    modelUrl: cadModelUrl, 
    fileName: cadFileName, 
    isUploading: isUploadingCad,
    resetCadState,
    fetchSessionDetails,
    handleUpload,
    updateModelUrl,
    updateParsedData,
    refreshParsedData,
  } = useCADSession();

  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, loading]);

  // Load session data when session ID changes
  useEffect(() => {
    if (currentSessionId && user?.id) {
      fetchSessionDetails(currentSessionId, user.id);
      fetchMessages(currentSessionId, user.id);
    } else if (!currentSessionId) {
      setMessages([]);
      resetCadState();
    }
  }, [currentSessionId, user?.id, fetchSessionDetails, resetCadState]);

  const fetchMessages = async (sessionId: string, userId: string) => {
    try {
      const response = await fetch(`${API_URL}/sessions/${sessionId}/messages?user_id=${userId}`);
      const data = await response.json();
      setMessages(data);
    } catch (err) {
      console.error("Failed to fetch messages", err);
    }
  };

  const handleNewChat = () => {
    setCurrentSessionId(null);
    setMessages([]);
    setPrompt("");
    resetCadState();
  };

  const onFileUpload = async (file: File) => {
    if (!user?.id) return;
    setError("");

    let sessionId = currentSessionId;
    if (!sessionId) {
      try {
        const title = `CAD: ${file.name}`;
        const res = await fetch(`${API_URL}/sessions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, user_id: user.id }),
        });
        const sessionData = await res.json();
        sessionId = sessionData.id;
        setCurrentSessionId(sessionId);
      } catch (err) {
        console.error("Session creation failed", err);
      }
    }

    try {
      const data = await handleUpload(file, sessionId, user.id);
      setMessages(prev => [...prev, { 
        role: "assistant", 
        content: `I've uploaded and parsed **${data.filename}**. You can now ask questions about it or request modifications.` 
      }]);
      setSuccessMsg("CAD Uploaded!");
      setTimeout(() => setSuccessMsg(""), 3000);
    } catch (err: any) {
      setError(err.message || "Failed to upload CAD");
    }
  };

  const handleRunInFreeCAD = async (code: string) => {
    setError("");
    setSuccessMsg("");
    try {
      const response = await fetch(`${API_URL}/run-in-freecad`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: code }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Failed to run in FreeCAD");
      setSuccessMsg("Sent to FreeCAD!");
      setTimeout(() => setSuccessMsg(""), 3000);
    } catch (err: any) {
      setError(err.message || "An error occurred");
    }
  };

  const handleGenerate = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!prompt.trim() || loading) return;

    setError("");
    let sessionId = currentSessionId;
    if (!sessionId && user?.id) {
      try {
        const title = prompt.slice(0, 30);
        const res = await fetch(`${API_URL}/sessions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, user_id: user.id }),
        });
        const sessionData = await res.json();
        sessionId = sessionData.id;
        setCurrentSessionId(sessionId);
      } catch (err) {}
    }

    const currentPrompt = prompt;
    setMessages(prev => [...prev, { role: "user", content: currentPrompt }]);
    setPrompt("");
    setLoading(true);

    try {
      const response = await fetch(`${API_URL}/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: currentPrompt, session_id: sessionId }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || "Failed to generate response");
      }

      const data = await response.json();
      setMessages(prev => [...prev, { 
        role: "assistant", 
        content: data.content, 
        code: data.code,
        intents: data.intents,
        preview: data.preview,
        intent_response: data.intent_response
      }]);
    } catch (err: any) {
      setError(err.message || "An error occurred");
      setPrompt(currentPrompt);
    } finally {
      setLoading(false);
    }
  };

  const handleApplyModifications = async (intents: any[], code?: string) => {
    if (!cadFileId) return;
    setSuccessMsg("Applying...");
    try {
      // ── Fire requests sequentially to avoid race condition ────────────────
      // 1. FreeCAD live update (socket → listener). Must complete FIRST
      //    so that the active document is modified.
      let fcRes = null;
      let fcOk = true;
      if (code && code.trim()) {
        fcRes = await fetch(`${API_URL}/run-in-freecad`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt: code }),
        });
        fcOk = fcRes.ok;
      }

      // 2. modify-model: runs its own parametric script, recomputes, 
      //    and exports the NEW STL and STEP from the active document.
      const modRes = await fetch(`${API_URL}/cad/modify-model`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_id: cadFileId, intents }),
      });

      const modData = modRes.ok ? await modRes.json() : null;
      const modOk = !!modData?.mesh_url;

      if (modOk) {
        // Update 3D viewer URL (cache-bust)
        updateModelUrl(cadFileId);

        // ── Update Feature Map parameters immediately ──────────────────────
        if (modData.updated_parsed_data) {
          updateParsedData(modData.updated_parsed_data);
        } else {
          // Fallback: fetch fresh parsed data from backend
          refreshParsedData(cadFileId);
        }

        setSuccessMsg(fcOk ? "✓ Updated in FreeCAD + Viewer!" : "✓ Viewer Updated!");
        setTimeout(() => setSuccessMsg(""), 4000);
        setMessages(prev => [...prev, {
          role: "assistant",
          content: `✅ Modifications applied simultaneously to **both** the 3D web viewer and FreeCAD. The Feature Map parameters have been refreshed. What else would you like to change?`
        }]);
      } else if (fcOk && code) {
        setSuccessMsg("✓ Applied in FreeCAD!");
        setTimeout(() => setSuccessMsg(""), 4000);
        setMessages(prev => [...prev, {
          role: "assistant",
          content: "Changes applied to FreeCAD. The web 3D preview may need a moment to refresh."
        }]);
      } else {
        setSuccessMsg("");
        console.warn("Apply partially failed", modData);
      }
    } catch (err) {
      setSuccessMsg("");
      console.error("Apply failed", err);
    }
  };

  return (
    <div className="flex h-screen bg-background text-foreground overflow-hidden font-sans">
      <Sidebar
        currentSessionId={currentSessionId}
        onSelectSession={setCurrentSessionId}
        onNewChat={handleNewChat}
      />

      <main className="flex-1 flex flex-row relative md:ml-64 transition-all duration-300 overflow-hidden h-full">
        {/* Chat Section */}
        <div className="flex-1 flex flex-col relative h-full min-w-0 overflow-hidden">
          <div className="absolute inset-0 bg-linear-to-b from-purple-900/5 via-transparent to-transparent pointer-events-none" />
          
          <header className="sticky top-0 z-30 p-4 border-b border-border bg-background/40 backdrop-blur-xl flex items-center justify-between shrink-0">
            <div className="flex items-center gap-2">
              <div className="p-1.5 bg-purple-600 rounded-lg shadow-lg shadow-purple-500/20">
                <Zap size={18} fill="white" className="text-white" />
              </div>
              <h1 className="font-bold tracking-tight">CAD Copilot</h1>
              {cadFileName && (
                <span className="text-xs bg-foreground/5 px-2 py-0.5 rounded border border-white/5 text-foreground/40 flex items-center gap-1.5 font-mono">
                  <FileBox size={12} /> {cadFileName}
                </span>
              )}
            </div>
            {successMsg && (
              <div className="px-3 py-1 bg-green-500/20 border border-green-500/30 rounded-full text-green-400 text-[10px] font-bold uppercase tracking-widest animate-pulse">
                {successMsg}
              </div>
            )}
          </header>

          <div className="flex-1 overflow-y-auto p-4 md:p-8 space-y-8 custom-scrollbar">
            {messages.length === 0 && !loading && (
              <div className="h-full flex flex-col items-center justify-center text-center space-y-4 max-w-md mx-auto">
                <div className="w-20 h-20 bg-purple-500/10 rounded-3xl flex items-center justify-center border border-purple-500/20 mb-4 animate-pulse">
                    <Zap size={40} className="text-purple-400" />
                </div>
                <h2 className="text-3xl font-black tracking-tight bg-linear-to-r from-white to-white/40 bg-clip-text text-transparent">Design Anything.</h2>
                <p className="text-foreground/40 text-sm leading-relaxed">
                  Upload a CAD file to analyze, explain, or modify it conversationally. I'll handle the geometry, you handle the vision.
                </p>
              </div>
            )}

            {messages.map((msg, i) => (
              <div key={i} className="space-y-4 max-w-full">
                <ChatMessage
                  role={msg.role}
                  content={msg.content}
                  code={msg.code}
                  onRunInFreeCAD={handleRunInFreeCAD}
                />
                {msg.intent_response && (
                  <div className="ml-4 md:ml-12 mr-4 bg-card/50 backdrop-blur-md border border-white/5 rounded-3xl p-6 shadow-2xl animate-in fade-in slide-in-from-left-4 duration-700">
                    <div className="flex items-center gap-2 mb-4">
                        <div className="w-1.5 h-1.5 rounded-full bg-purple-500 animate-pulse" />
                        <p className="text-[10px] uppercase tracking-[0.3em] font-black text-foreground/30">
                            Geometric Intelligence
                        </p>
                    </div>
                    <IntentResponse 
                        response={msg.intent_response} 
                        onConfirm={(selectedInterpretation) => {
                          if (selectedInterpretation) {
                            // Can add logic for handling interpretation if needed, currently we just apply the intent
                            console.log("User selected alternative:", selectedInterpretation);
                          }
                          handleApplyModifications(msg.intents!, msg.code);
                        }}
                        onCancel={() => {
                          setMessages(prev => [...prev, { role: "assistant", content: "Modification cancelled." }]);
                        }}
                    />
                  </div>
                )}
              </div>
            ))}

            {loading && (
              <div className="flex justify-start">
                <div className="bg-foreground/5 border border-white/5 backdrop-blur-md rounded-2xl p-4 flex items-center gap-4 animate-pulse">
                  <div className="relative">
                    <div className="w-3 h-3 bg-purple-500 rounded-full animate-ping opacity-75" />
                    <div className="absolute inset-0 w-3 h-3 bg-purple-500 rounded-full" />
                  </div>
                  <span className="text-xs font-bold uppercase tracking-widest text-foreground/40">Processing Intent...</span>
                </div>
              </div>
            )}

            {error && (
              <div className="mx-auto max-w-lg p-4 bg-red-500/10 border border-red-500/20 rounded-2xl text-red-400 text-xs font-medium text-center flex items-center justify-center gap-2 animate-in shake duration-500">
                <X size={14} /> {error}
              </div>
            )}
            <div ref={messagesEndRef} className="h-4" />
          </div>

          <ChatInput 
            prompt={prompt}
            setPrompt={setPrompt}
            loading={loading}
            isUploadingCad={isUploadingCad}
            onUpload={onFileUpload}
            onSubmit={handleGenerate}
            hasCad={!!cadFileId}
          />
        </div>

        {/* CAD Preview Panel */}
        {cadFileId && (
          <div className={`flex-none border-l border-white/5 bg-card/30 backdrop-blur-3xl transition-all duration-500 flex flex-col h-full ${showCadPanel ? "w-[450px]" : "w-0 overflow-hidden border-none"}`}>
            <div className="p-5 border-b border-white/5 flex items-center justify-between shrink-0">
              <div className="flex items-center gap-3">
                  <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                  <h2 className="text-[10px] font-black uppercase tracking-[0.4em] text-foreground/40">3D Workspace</h2>
              </div>
              <button 
                onClick={() => setShowCadPanel(false)} 
                className="p-2 hover:bg-white/5 rounded-xl transition-colors text-foreground/30 hover:text-foreground"
              >
                <ChevronRight size={18} />
              </button>
            </div>
            
            <div className="flex-1 overflow-y-auto p-6 space-y-8 custom-scrollbar">
              <div className="relative h-[350px] bg-black/40 rounded-[2rem] overflow-hidden border border-white/5 group/viewer shadow-inner">
                <div className="absolute top-4 left-4 z-10 px-3 py-1 bg-black/60 backdrop-blur-md border border-white/10 rounded-full text-[9px] font-bold text-white/50 uppercase tracking-widest opacity-0 group-hover/viewer:opacity-100 transition-opacity">
                    Interactive Preview
                </div>

                <a
                  href={`${API_URL}/cad/download/${cadFileId}`}
                  download
                  className="absolute top-4 right-4 z-10 p-2 bg-purple-600/80 hover:bg-purple-600 backdrop-blur-md border border-purple-500/30 rounded-xl text-white opacity-0 group-hover/viewer:opacity-100 transition-all shadow-lg hover:shadow-purple-500/50"
                  title="Download Modified CAD (STL)"
                >
                  <Download size={16} />
                </a>

                <CADViewer modelUrl={cadModelUrl} />
              </div>
              
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                    <h3 className="text-[10px] font-black uppercase tracking-[0.4em] text-foreground/30">Feature Map</h3>
                    <div className="h-px flex-1 mx-4 bg-white/5" />
                </div>
                {cadParsedData && <PatternSummary data={cadParsedData} />}
              </div>
            </div>
          </div>
        )}

        {/* Re-open CAD panel button */}
        {cadFileId && !showCadPanel && (
          <button
            onClick={() => setShowCadPanel(true)}
            className="absolute right-0 top-1/2 -translate-y-1/2 p-3 bg-purple-600 text-white rounded-l-2xl hover:pr-5 transition-all shadow-2xl shadow-purple-500/40 z-50 group"
          >
            <ChevronLeft size={20} className="group-hover:scale-125 transition-transform" />
          </button>
        )}
      </main>
    </div>
  );
}
