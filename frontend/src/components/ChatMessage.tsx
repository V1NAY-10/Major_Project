"use client";
import { Check, Copy, Play } from "lucide-react";
import { useState } from "react";

interface ChatMessageProps {
    role: "user" | "assistant";
    content: string;
    onRunInFreeCAD?: (code: string) => void;
}

export default function ChatMessage({ role, content, onRunInFreeCAD }: ChatMessageProps) {
    const [copied, setCopied] = useState(false);

    const handleCopy = async () => {
        try {
            await navigator.clipboard.writeText(content);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch (err) {
            console.error("Failed to copy text: ", err);
        }
    };

    const isAssistant = role === "assistant";

    return (
        <div className={`flex ${isAssistant ? "justify-start" : "justify-end"} mb-6 animate-in fade-in slide-in-from-bottom-2 duration-300`}>
            <div className={`max-w-[85%] rounded-2xl p-4 shadow-lg ${isAssistant
                    ? "bg-white/10 backdrop-blur-md border border-white/20 text-white"
                    : "bg-purple-600 text-white"
                }`}>
                <div className="flex items-center justify-between mb-2">
                    <span className="text-[10px] uppercase tracking-widest font-bold opacity-50">
                        {isAssistant ? "Assistant" : "You"}
                    </span>
                    {isAssistant && (
                        <div className="flex gap-2">
                            <button
                                onClick={handleCopy}
                                className="p-1 hover:bg-white/10 rounded transition-colors"
                                title="Copy code"
                            >
                                {copied ? <Check size={14} className="text-green-400" /> : <Copy size={14} />}
                            </button>
                            {onRunInFreeCAD && (
                                <button
                                    onClick={() => onRunInFreeCAD(content)}
                                    className="p-1 hover:bg-green-500/20 rounded transition-colors text-green-400"
                                    title="Run in FreeCAD"
                                >
                                    <Play size={14} fill="currentColor" />
                                </button>
                            )}
                        </div>
                    )}
                </div>

                {isAssistant ? (
                    <pre className="text-sm font-mono bg-black/30 p-3 rounded-lg overflow-x-auto border border-white/5">
                        <code>{content}</code>
                    </pre>
                ) : (
                    <p className="text-sm leading-relaxed">{content}</p>
                )}
            </div>
        </div>
    );
}
