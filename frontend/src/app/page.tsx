"use client";
import { useState, useEffect, useRef } from "react";
import { Send, Zap, Loader2 } from "lucide-react";
import Sidebar from "@/components/Sidebar";
import ChatMessage from "@/components/ChatMessage";

interface Message {
  id?: string;
  role: "user" | "assistant";
  content: string;
}

export default function Home() {
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [successMsg, setSuccessMsg] = useState("");

  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, loading]);

  useEffect(() => {
    if (currentSessionId) {
      fetchMessages(currentSessionId);
    } else {
      setMessages([]);
    }
  }, [currentSessionId]);

  const fetchMessages = async (sessionId: string) => {
    try {
      const response = await fetch(`http://127.0.0.1:8000/sessions/${sessionId}/messages`);
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
  };

  const handleRunInFreeCAD = async (code: string) => {
    setRunning(true);
    setError("");
    setSuccessMsg("");
    try {
      const response = await fetch("http://127.0.0.1:8000/run-in-freecad", {
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
    } finally {
      setRunning(false);
    }
  };

  const handleGenerate = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!prompt.trim() || loading) return;

    let sessionId = currentSessionId;

    // 1. Create a session if it doesn't exist
    if (!sessionId) {
      try {
        const title = prompt.slice(0, 30) + (prompt.length > 30 ? "..." : "");
        const res = await fetch("http://127.0.0.1:8000/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title }),
        });
        const sessionData = await res.json();
        sessionId = sessionData.id;
        setCurrentSessionId(sessionId);

        // Trigger sync for the first time
        await fetch(`http://127.0.0.1:8000/sessions/${sessionId}/sync`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ previous_session_id: null }),
        });
      } catch (err) {
        console.error("Failed to create session", err);
        // Continue anyway if backend supabase is not configured
      }
    }

    const userMsg: Message = { role: "user", content: prompt };
    setMessages(prev => [...prev, userMsg]);
    const currentPrompt = prompt;
    setPrompt("");
    setLoading(true);
    setError("");

    try {
      const response = await fetch("http://127.0.0.1:8000/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: currentPrompt, session_id: sessionId }),
      });

      if (!response.ok) throw new Error("Failed to generate code");

      const data = await response.json();
      setMessages(prev => [...prev, { role: "assistant", content: data.code }]);
    } catch (err: any) {
      setError(err.message || "An error occurred");
      setPrompt(currentPrompt); // Restore prompt on error
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex h-screen bg-[#050505] text-white overflow-hidden font-sans">
      <Sidebar
        currentSessionId={currentSessionId}
        onSelectSession={setCurrentSessionId}
        onNewChat={handleNewChat}
      />

      <main className="flex-1 flex flex-col relative md:ml-64 transition-all duration-300">
        {/* Background Gradient */}
        <div className="absolute inset-0 bg-linear-to-b from-purple-900/10 via-transparent to-transparent pointer-events-none" />

        {/* Header */}
        <header className="sticky top-0 z-30 p-4 border-b border-white/5 bg-black/20 backdrop-blur-xl flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="p-1.5 bg-purple-600 rounded-lg">
              <Zap size={18} fill="white" />
            </div>
            <h1 className="font-bold tracking-tight">FreeCAD AI</h1>
          </div>
          {successMsg && (
            <div className="px-3 py-1 bg-green-500/20 border border-green-500/30 rounded-full text-green-400 text-xs animate-pulse">
              {successMsg}
            </div>
          )}
        </header>

        {/* Chat Area */}
        <div className="flex-1 overflow-y-auto p-4 md:p-8 space-y-4 custom-scrollbar">
          {messages.length === 0 && !loading && (
            <div className="h-full flex flex-col items-center justify-center text-center space-y-4 max-w-md mx-auto">
              <div className="w-16 h-16 bg-white/5 rounded-3xl flex items-center justify-center mb-4 border border-white/10">
                <Zap size={32} className="text-purple-400" />
              </div>
              <h2 className="text-2xl font-bold">What are we building?</h2>
              <p className="text-white/40 text-sm">
                Describe a 3D object, and I'll generate the FreeCAD Python code for you.
              </p>
              <div className="grid grid-cols-1 gap-2 w-full mt-8">
                {["Create a 10x10 cube with a hole", "Generate a gear with 20 teeth", "Build a parametric table frame"].map((suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => setPrompt(suggestion)}
                    className="p-3 bg-white/5 hover:bg-white/10 border border-white/10 rounded-xl text-left text-xs transition-all"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg, i) => (
            <ChatMessage
              key={i}
              role={msg.role}
              content={msg.content}
              onRunInFreeCAD={handleRunInFreeCAD}
            />
          ))}

          {loading && (
            <div className="flex justify-start mb-6">
              <div className="bg-white/5 backdrop-blur-md border border-white/10 rounded-2xl p-4 flex items-center gap-3">
                <Loader2 size={16} className="animate-spin text-purple-400" />
                <span className="text-sm text-white/60">Generating code...</span>
              </div>
            </div>
          )}

          {error && (
            <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-2xl text-red-400 text-sm max-w-md mx-auto text-center">
              {error}
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input Area */}
        <div className="p-4 md:p-8 bg-linear-to-t from-black via-black/80 to-transparent">
          <form
            onSubmit={handleGenerate}
            className="max-w-3xl mx-auto relative group"
          >
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  handleGenerate();
                }
              }}
              placeholder="Ask me to generate a 3D model..."
              className="w-full bg-white/5 hover:bg-white/10 focus:bg-white/10 border border-white/10 focus:border-purple-500/50 rounded-2xl p-4 pr-14 text-white placeholder-white/20 focus:outline-hidden transition-all resize-none shadow-2xl min-h-[60px] max-h-32"
            />
            <button
              type="submit"
              disabled={loading || !prompt.trim()}
              className="absolute right-3 bottom-3 p-2 bg-purple-600 hover:bg-purple-500 disabled:opacity-30 disabled:hover:bg-purple-600 rounded-xl transition-all active:scale-95 shadow-lg"
            >
              {loading ? <Loader2 size={20} className="animate-spin" /> : <Send size={20} />}
            </button>
          </form>
          <p className="text-[10px] text-center mt-3 text-white/20 uppercase tracking-widest">
            FreeCAD Python Code Generator • Powered by Gemini Flash
          </p>
        </div>
      </main>
    </div>
  );
}

