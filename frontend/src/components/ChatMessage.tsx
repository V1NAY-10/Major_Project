"use client";
import { Check, Copy, Play, Zap } from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";

interface ChatMessageProps {
    role: "user" | "assistant";
    content: string;
    code?: string;
    onRunInFreeCAD?: (code: string) => void;
}

export default function ChatMessage({ role, content, code, onRunInFreeCAD }: ChatMessageProps) {
    const [copied, setCopied] = useState(false);

    const handleCopy = async (text: string) => {
        try {
            await navigator.clipboard.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch (err) {
            console.error("Failed to copy text: ", err);
        }
    };

    const isAssistant = role === "assistant";

    return (
        <div className={`flex ${isAssistant ? "justify-start" : "justify-end"} mb-8 animate-in fade-in slide-in-from-bottom-4 duration-500`}>
            <div className={`max-w-[80%] md:max-w-[70%] rounded-2xl p-5 shadow-2xl transition-all duration-300 hover:shadow-primary/5 ${isAssistant
                    ? "bg-foreground/[0.03] backdrop-blur-xl border border-white/5 text-foreground ring-1 ring-white/5"
                    : "bg-linear-to-br from-purple-600 to-indigo-600 text-white shadow-purple-500/20"
                }`}>
                <div className="flex items-center justify-between mb-3 border-b border-white/5 pb-2">
                    <div className="flex items-center gap-2">
                        {isAssistant && (
                           <div className="w-5 h-5 rounded-full bg-purple-500 flex items-center justify-center">
                               <Zap size={10} fill="white" className="text-white" />
                           </div>
                        )}
                        <span className="text-[10px] uppercase tracking-[0.2em] font-black opacity-40">
                            {isAssistant ? "AI Assistant" : "User"}
                        </span>
                    </div>
                    {isAssistant && (
                        <div className="flex gap-2">
                            <button
                                onClick={() => handleCopy(content)}
                                className="p-1.5 hover:bg-white/10 rounded-lg transition-colors text-foreground/40 hover:text-foreground"
                                title="Copy all"
                            >
                                {copied ? <Check size={14} className="text-green-400" /> : <Copy size={14} />}
                            </button>
                        </div>
                    )}
                </div>

                {isAssistant ? (
                    <div className="text-sm leading-relaxed prose prose-invert max-w-none prose-p:my-2 prose-code:text-purple-300">
                        <ReactMarkdown
                            components={{
                                code({ node, inline, className, children, ...props }: any) {
                                    const match = /language-(\w+)/.exec(className || "");
                                    const codeString = String(children).replace(/\n$/, "");
                                    
                                    if (!inline && match && match[1] === "python") {
                                        return (
                                            <div className="relative my-6 group/code">
                                                <div className="absolute right-3 top-3 z-10 opacity-0 group-hover/code:opacity-100 transition-all transform translate-y-2 group-hover/code:translate-y-0 flex gap-2">
                                                    <button
                                                        onClick={() => handleCopy(codeString)}
                                                        className="p-2 bg-black/60 hover:bg-black/80 rounded-lg text-white/80 border border-white/10 backdrop-blur-md"
                                                        title="Copy Code"
                                                    >
                                                        <Copy size={14} />
                                                    </button>
                                                    {onRunInFreeCAD && (
                                                        <button
                                                            onClick={() => onRunInFreeCAD(codeString)}
                                                            className="p-2 bg-purple-600 hover:bg-purple-500 rounded-lg text-white border border-purple-400/30 shadow-lg shadow-purple-500/20"
                                                            title="Run in FreeCAD"
                                                        >
                                                            <Play size={14} fill="currentColor" />
                                                        </button>
                                                    )}
                                                </div>
                                                <div className="absolute left-4 -top-3 px-2 py-0.5 bg-purple-900/50 border border-purple-500/30 rounded text-[10px] font-bold text-purple-300 uppercase tracking-widest z-10 backdrop-blur-sm">
                                                    Python Script
                                                </div>
                                                <pre className="bg-black/40 p-5 rounded-2xl border border-white/5 overflow-x-auto font-mono text-[13px] leading-relaxed scrollbar-thin scrollbar-thumb-white/10">
                                                    <code>{children}</code>
                                                </pre>
                                            </div>
                                        );
                                    }
                                    return <code className={`${className} bg-purple-500/20 px-1.5 py-0.5 rounded text-purple-300`} {...props}>{children}</code>;
                                }
                            }}
                        >
                            {content}
                        </ReactMarkdown>
                    </div>
                ) : (
                    <p className="text-sm leading-relaxed font-medium">{content}</p>
                )}
            </div>
        </div>
    );
}
