"use client";
import Image from "next/image";

import { useState } from "react";

export default function Home() {
  const [prompt, setPrompt] = useState("");
  const [result, setResult] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

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
              <h2 className="text-sm font-medium text-white/50 uppercase tracking-wider">Generated Code</h2>
              <pre className="p-6 bg-black/40 border border-white/10 rounded-2xl overflow-x-auto text-sm font-mono text-purple-200 shadow-2xl max-h-96">
                <code>{result}</code>
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

