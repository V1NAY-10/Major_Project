"use client";
import { useState, useEffect, useRef, useCallback } from "react";
import { Send, Zap, Loader2, MessageSquare, FileBox, Paperclip, X, ChevronRight, ChevronLeft } from "lucide-react";
import Sidebar from "@/components/Sidebar";
import ChatMessage from "@/components/ChatMessage";
import CADViewer from "@/components/CADViewer";
import PatternSummary, { ParsedData } from "@/components/PatternSummary";
import IntentResponse from "@/components/IntentResponse";
import { API_URL } from "@/lib/api";
import { useUser } from "@clerk/nextjs";

interface Message {
  id?: string;
  role: "user" | "assistant";
  content: string;
  code?: string;
  intents?: any[];
  preview?: any[];
}

export default function Home() {
  const { user } = useUser();
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  
  // CAD State
  const [cadFileId, setCadFileId] = useState<string | null>(null);
  const [cadParsedData, setCadParsedData] = useState<ParsedData | null>(null);
  const [cadModelUrl, setCadModelUrl] = useState<string | null>(null);
  const [cadFileName, setCadFileName] = useState<string | null>(null);
  const [isUploadingCad, setIsUploadingCad] = useState(false);
  const [showCadPanel, setShowCadPanel] = useState(true);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

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
  }, [currentSessionId, user?.id]);

  const resetCadState = () => {
    setCadFileId(null);
    setCadParsedData(null);
    setCadModelUrl(null);
    setCadFileName(null);
  };

  const fetchSessionDetails = async (sessionId: string, userId: string) => {
    try {
      const res = await fetch(`${API_URL}/sessions/${sessionId}?user_id=${userId}`);
      if (res.ok) {
        const data = await res.json();
        if (data.cad_file_id) {
          setCadFileId(data.cad_file_id);
          setCadFileName(data.cad_filename);
          setCadParsedData(data.cad_parsed_data);
          setCadModelUrl(`${API_URL}/cad/model/${data.cad_file_id}`);
        } else {
          resetCadState();
        }
      }
    } catch (err) {
      console.error("Failed to fetch session details", err);
    }
  };

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

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !user?.id) return;

    setIsUploadingCad(true);
    setError("");

    let sessionId = currentSessionId;
    // Ensure session exists
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

    const formData = new FormData();
    formData.append("file", file);
    if (sessionId) formData.append("session_id", sessionId);

    try {
      const res = await fetch(`${API_URL}/cad/upload`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) throw new Error("Upload failed");
      
      const data = await res.json();
      setCadFileId(data.file_id);
      setCadFileName(data.filename);
      setCadParsedData(data.parsed_data);
      setCadModelUrl(`${API_URL}/cad/model/${data.file_id}`);
      
      setMessages(prev => [...prev, { role: "assistant", content: `I've uploaded and parsed **${data.filename}**. You can now ask questions about it or request modifications.` }]);
      setSuccessMsg("CAD Uploaded!");
      setTimeout(() => setSuccessMsg(""), 3000);
    } catch (err: any) {
      setError(err.message || "Failed to upload CAD");
    } finally {
      setIsUploadingCad(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
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

    setError(""); // Clear error immediately
    let sessionId = currentSessionId;
    if (!sessionId) {
      try {
        const title = prompt.slice(0, 30);
        const res = await fetch(`${API_URL}/sessions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, user_id: user?.id }),
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
        preview: data.preview
      }]);
    } catch (err: any) {
      setError(err.message || "An error occurred");
      setPrompt(currentPrompt);
    } finally {
      setLoading(false);
    }
  };

  // Clear error when typing
  useEffect(() => {
    if (prompt) setError("");
  }, [prompt]);

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

            {messages.map((msg: any, i) => (
              <div key={i} className="space-y-4 max-w-full">
                <ChatMessage
                  role={msg.role}
                  content={msg.content}
                  code={msg.code}
                  onRunInFreeCAD={handleRunInFreeCAD}
                />
                {msg.intents && msg.intents.length > 0 && (
                  <div className="ml-4 md:ml-12 mr-4 bg-card/50 backdrop-blur-md border border-white/5 rounded-3xl p-6 shadow-2xl animate-in fade-in slide-in-from-left-4 duration-700">
                    <div className="flex items-center gap-2 mb-4">
                        <div className="w-1.5 h-1.5 rounded-full bg-purple-500 animate-pulse" />
                        <p className="text-[10px] uppercase tracking-[0.3em] font-black text-foreground/30">
                            Geometric Intelligence
                        </p>
                    </div>
                    <IntentResponse 
                        intent={{ intents: msg.intents, preview: msg.preview || [], warnings: [] }} 
                        onApply={async () => {
                            if (!cadFileId) return;
                            try {
                                const res = await fetch(`${API_URL}/cad/modify-model`, {
                                    method: "POST",
                                    headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify({ file_id: cadFileId, intents: msg.intents }),
                                });
                                const data = await res.json();
                                if (data.mesh_url) {
                                    setCadModelUrl(`${API_URL}${data.mesh_url}`);
                                    setSuccessMsg("Model Updated!");
                                    setTimeout(() => setSuccessMsg(""), 3000);
                                }
                            } catch (err) {
                                console.error("Apply failed", err);
                            }
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

          {/* Input Area */}
          <div className="p-4 md:p-8 shrink-0 bg-linear-to-t from-background via-background to-transparent">
            <form onSubmit={handleGenerate} className="max-w-3xl mx-auto relative group">
              <input
                type="file"
                ref={fileInputRef}
                onChange={handleFileUpload}
                accept=".step,.stp"
                className="hidden"
              />
              <div className="flex items-end gap-2 bg-foreground/5 hover:bg-foreground/10 focus-within:bg-foreground/10 border border-white/5 focus-within:border-purple-500/40 rounded-3xl p-2 transition-all shadow-2xl backdrop-blur-xl">
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={isUploadingCad}
                  className="p-4 text-foreground/30 hover:text-purple-400 transition-all transform hover:scale-110 disabled:opacity-30"
                  title="Attach CAD File"
                >
                  {isUploadingCad ? <Loader2 size={20} className="animate-spin text-purple-400" /> : <Paperclip size={20} />}
                </button>
                <textarea
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      handleGenerate();
                    }
                  }}
                  placeholder={cadFileId ? "How should we modify this model?" : "Generate a 3D model or upload a file..."}
                  className="flex-1 bg-transparent border-none focus:ring-0 p-4 text-sm text-foreground placeholder-foreground/20 resize-none min-h-[56px] max-h-32 scrollbar-none"
                />
                <button
                  type="submit"
                  disabled={loading || !prompt.trim()}
                  className="p-4 bg-purple-600 hover:bg-purple-500 disabled:opacity-20 rounded-2xl transition-all shadow-lg shadow-purple-500/20 transform hover:scale-105 active:scale-95 group-disabled:hover:scale-100"
                >
                  {loading ? <Loader2 size={20} className="animate-spin text-white" /> : <Send size={20} className="text-white" />}
                </button>
              </div>
            </form>
          </div>
        </div>

        {/* CAD Preview Panel (Fixed Right Side) */}
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

        {/* Re-open CAD panel button (Floating) */}
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

