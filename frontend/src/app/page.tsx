"use client";
import Image from "next/image";

import { useState } from "react";

export default function Home() {
  const [prompt, setPrompt] = useState("");
  const [result, setResult] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [running, setRunning] = useState(false);
  const [successMsg, setSuccessMsg] = useState("");

  const handleCopy = async () => {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy text: ", err);
    }
  };

  const handleRunInFreeCAD = async () => {
    if (!result) return;
    setRunning(true);
    setError("");
    setSuccessMsg("");
    try {
      const response = await fetch("http://localhost:8000/run-in-freecad", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: result }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Failed to run in FreeCAD");
      setSuccessMsg(data.message);
      setTimeout(() => setSuccessMsg(""), 3000);
    } catch (err: any) {
      setError(err.message || "An error occurred");
    } finally {
      setRunning(false);
    }
  };

  const handleGenerate = async () => {
    if (!prompt.trim()) return;

    setLoading(true);
    setError("");
    setResult("");

    try {
      const response = await fetch("http://localhost:8000/generate", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ prompt }),
      });

      if (!response.ok) {
        throw new Error("Failed to generate code");
      }

      const data = await response.json();
      setResult(data.code);
    } catch (err: any) {
      setError(err.message || "An error occurred");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-linear-to-br from-indigo-500 via-purple-500 to-pink-500 flex items-center justify-center p-4 font-sans">
      <div className="max-w-2xl w-full bg-white/10 backdrop-blur-xl border border-white/20 rounded-3xl p-8 shadow-2xl text-white">
        <header className="mb-8 text-center">
          <h1 className="text-4xl font-bold tracking-tight mb-2">FreeCAD AI Generator</h1>
          <p className="text-white/70">Enter a prompt to generate FreeCAD Python code using Gemini Flash</p>
        </header>

        <div className="space-y-6">
          <div className="relative group">
            <textarea
              placeholder="e.g., Create a 10x10 cube with a 3mm hole in the center"
              className="w-full h-40 bg-black/20 border border-white/10 rounded-2xl p-4 text-white placeholder-white/30 focus:outline-hidden focus:ring-2 focus:ring-purple-400 transition-all resize-none shadow-inner"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
          </div>

          <button
            onClick={handleGenerate}
            disabled={loading || !prompt.trim()}
            className="w-full py-4 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-2xl font-semibold text-lg transition-all transform active:scale-95 shadow-lg flex items-center justify-center gap-2"
          >
            {loading ? (
              <div className="w-6 h-6 border-2 border-white/30 border-t-white rounded-full animate-spin" />
            ) : (
              "Generate Code"
            )}
          </button>

          {error && (
            <div className="p-4 bg-red-500/20 border border-red-500/30 rounded-2xl text-red-200 text-sm">
              {error}
            </div>
          )}

          {result && (
            <div className="space-y-2 mt-8 animate-in fade-in slide-in-from-bottom-4 duration-500">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-medium text-white/50 uppercase tracking-wider">Generated Code</h2>
                <button
                  onClick={handleCopy}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg text-xs font-semibold transition-all active:scale-95"
                >
                  {copied ? (
                    <>
                      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                      Copied!
                    </>
                  ) : (
                    <>
                      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2" /><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" /></svg>
                      Copy Script
                    </>
                  )}
                </button>
                <button
                  onClick={handleRunInFreeCAD}
                  disabled={running}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600/20 hover:bg-green-600/30 border border-green-500/30 rounded-lg text-xs font-semibold transition-all active:scale-95 disabled:opacity-50"
                >
                  {running ? (
                    <div className="w-3 h-3 border border-white/30 border-t-white rounded-full animate-spin" />
                  ) : (
                    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3" /></svg>
                  )}
                  Run in FreeCAD
                </button>
              </div>
              {successMsg && (
                <div className="p-2 bg-green-500/20 border border-green-500/30 rounded-xl text-green-200 text-xs text-center animate-pulse">
                  {successMsg}
                </div>
              )}
              <pre className="p-6 bg-black/40 border border-white/10 rounded-2xl overflow-x-auto text-sm font-mono text-purple-200 shadow-2xl max-h-96 relative group">
                <code>{result}</code>
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

