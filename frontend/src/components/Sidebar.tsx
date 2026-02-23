"use client";
import { Plus, MessageSquare, Menu, X, Edit2, Check, Sun, Moon, Settings, LogOut } from "lucide-react";
import { useState, useEffect, useRef } from "react";

interface Session {
    id: string;
    title: string;
}

interface SidebarProps {
    currentSessionId: string | null;
    onSelectSession: (id: string) => void;
    onNewChat: () => void;
}

export default function Sidebar({ currentSessionId, onSelectSession, onNewChat }: SidebarProps) {
    const [sessions, setSessions] = useState<Session[]>([]);
    const [isOpen, setIsOpen] = useState(true);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editValue, setEditValue] = useState("");
    const [showProfileMenu, setShowProfileMenu] = useState(false);
    const [isDarkMode, setIsDarkMode] = useState(true);

    const profileMenuRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        fetchSessions();
    }, [currentSessionId]);

    useEffect(() => {
        const handleClickOutside = (event: MouseEvent) => {
            if (profileMenuRef.current && !profileMenuRef.current.contains(event.target as Node)) {
                setShowProfileMenu(false);
            }
        };
        document.addEventListener("mousedown", handleClickOutside);
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, []);

    const fetchSessions = async () => {
        try {
            const response = await fetch("http://127.0.0.1:8000/sessions");
            if (!response.ok) throw new Error("Failed to fetch sessions");
            const sessionsData = await response.json();
            setSessions(Array.isArray(sessionsData) ? sessionsData : []);
        } catch (err) {
            console.error("Failed to fetch sessions", err);
        }
    };

    const handleStartRename = (e: React.MouseEvent, session: Session) => {
        e.stopPropagation();
        setEditingId(session.id);
        setEditValue(session.title);
    };

    const handleSaveRename = async (e?: React.FormEvent) => {
        if (e) e.preventDefault();
        if (!editingId || !editValue.trim()) {
            setEditingId(null);
            return;
        }

        try {
            const response = await fetch(`http://127.0.0.1:8000/sessions/${editingId}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ title: editValue.trim() }),
            });
            if (response.ok) {
                setSessions(prev => prev.map(s => s.id === editingId ? { ...s, title: editValue.trim() } : s));
            }
        } catch (err) {
            console.error("Failed to rename session", err);
        } finally {
            setEditingId(null);
        }
    };

    return (
        <>
            <button
                onClick={() => setIsOpen(!isOpen)}
                className="fixed top-4 left-4 z-50 p-2 bg-white/10 backdrop-blur-md rounded-lg md:hidden text-white"
            >
                {isOpen ? <X size={20} /> : <Menu size={20} />}
            </button>

            <div className={`fixed inset-y-0 left-0 z-40 w-64 bg-black/40 backdrop-blur-2xl border-r border-white/10 transition-transform duration-300 transform ${isOpen ? "translate-x-0" : "-translate-x-full"} md:translate-x-0 flex flex-col`}>
                <div className="p-4 flex flex-col h-full">
                    <button
                        onClick={onNewChat}
                        className="flex items-center gap-3 w-full p-4 mb-6 bg-white/5 hover:bg-white/10 border border-white/10 rounded-2xl text-white font-medium transition-all group"
                    >
                        <Plus size={18} className="group-hover:rotate-90 transition-transform" />
                        New Chat
                    </button>

                    <div className="flex-1 overflow-y-auto space-y-2 custom-scrollbar">
                        <h3 className="px-4 text-[10px] uppercase tracking-widest font-bold text-white/40 mb-2">History</h3>
                        {sessions.map((session) => (
                            <div key={session.id} className="relative group/item">
                                {editingId === session.id ? (
                                    <form
                                        onSubmit={handleSaveRename}
                                        className="flex items-center gap-2 px-3 py-2 bg-purple-600/20 border border-purple-500/30 rounded-xl"
                                    >
                                        <input
                                            autoFocus
                                            className="bg-transparent border-none focus:outline-hidden text-sm text-white w-full"
                                            value={editValue}
                                            onChange={(e) => setEditValue(e.target.value)}
                                            onBlur={() => handleSaveRename()}
                                        />
                                        <button type="submit" className="text-purple-400 hover:text-white">
                                            <Check size={14} />
                                        </button>
                                    </form>
                                ) : (
                                    <div className="relative">
                                        <button
                                            onClick={() => onSelectSession(session.id)}
                                            className={`flex items-center gap-3 w-full p-3 rounded-xl text-left text-sm transition-all pr-10 ${currentSessionId === session.id
                                                ? "bg-purple-600/30 text-purple-200 border border-purple-500/30"
                                                : "text-white/70 hover:bg-white/5 hover:text-white"
                                                }`}
                                        >
                                            <MessageSquare size={16} className="shrink-0" />
                                            <span className="truncate">{session.title}</span>
                                        </button>
                                        <button
                                            onClick={(e) => handleStartRename(e, session)}
                                            className="absolute right-3 top-1/2 -translate-y-1/2 p-1 text-white/20 hover:text-white opacity-0 group-hover/item:opacity-100 transition-all"
                                        >
                                            <Edit2 size={12} />
                                        </button>
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>

                    <div className="mt-auto pt-4 border-t border-white/10 relative">
                        {showProfileMenu && (
                            <div
                                ref={profileMenuRef}
                                className="absolute bottom-16 left-2 right-2 bg-[#1a1a1a] border border-white/10 rounded-2xl p-2 shadow-2xl animate-in fade-in slide-in-from-bottom-2 duration-200 z-50"
                            >
                                <button
                                    onClick={() => setIsDarkMode(!isDarkMode)}
                                    className="flex items-center gap-3 w-full p-3 hover:bg-white/5 rounded-xl text-xs text-white/70 hover:text-white transition-all"
                                >
                                    {isDarkMode ? <Sun size={14} /> : <Moon size={14} />}
                                    {isDarkMode ? "Light Mode" : "Dark Mode"}
                                </button>
                                <button className="flex items-center gap-3 w-full p-3 hover:bg-white/5 rounded-xl text-xs text-white/70 hover:text-white transition-all">
                                    <Settings size={14} />
                                    Settings
                                </button>
                                <div className="h-px bg-white/5 my-1" />
                                <button className="flex items-center gap-3 w-full p-3 hover:bg-red-500/10 rounded-xl text-xs text-red-400 hover:text-red-300 transition-all">
                                    <LogOut size={14} />
                                    Log out
                                </button>
                            </div>
                        )}

                        <button
                            onClick={() => setShowProfileMenu(!showProfileMenu)}
                            className="flex items-center justify-between w-full p-2 hover:bg-white/5 rounded-2xl transition-all"
                        >
                            <div className="flex items-center gap-3">
                                <div className="w-8 h-8 rounded-full bg-linear-to-br from-indigo-500 to-purple-600 shadow-lg" />
                                <div className="flex flex-col text-left">
                                    <span className="text-xs font-medium text-white">FreeCAD AI</span>
                                    <span className="text-[10px] text-white/40 uppercase">Major Project</span>
                                </div>
                            </div>
                            <Menu size={14} className="text-white/20" />
                        </button>
                    </div>
                </div>
            </div>
        </>
    );
}
