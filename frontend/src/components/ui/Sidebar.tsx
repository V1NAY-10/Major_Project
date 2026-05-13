"use client";
import { Plus, MessageSquare, Menu, X, Edit2, Check, Sun, Moon, Settings, LogOut } from "lucide-react";
import { UserButton, useUser, useClerk } from "@clerk/nextjs";
import { useState, useEffect, useRef } from "react";
import { useTheme } from "next-themes";
import { getApiUrl } from "@/lib/api";

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
    const { theme, setTheme, resolvedTheme } = useTheme();
    const { user } = useUser();
    const { signOut } = useClerk();
    const [mounted, setMounted] = useState(false);

    const profileMenuRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        setMounted(true);
    }, []);

    useEffect(() => {
        // Only fetch once the component has mounted (client-side) and we have a userId
        if (mounted && user?.id) {
            fetchSessions(user.id);
        }
    }, [currentSessionId, user?.id, mounted]);

    useEffect(() => {
        const handleClickOutside = (event: MouseEvent) => {
            if (profileMenuRef.current && !profileMenuRef.current.contains(event.target as Node)) {
                setShowProfileMenu(false);
            }
        };
        document.addEventListener("mousedown", handleClickOutside);
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, []);

    const fetchSessions = async (userId: string) => {
        if (!userId || userId === "null" || userId === "undefined") return;
        const apiUrl = getApiUrl();
        try {
            const response = await fetch(`${apiUrl}/sessions?user_id=${userId}`);
            if (!response.ok) throw new Error(`Server responded ${response.status}`);
            const sessionsData = await response.json();
            setSessions(Array.isArray(sessionsData) ? sessionsData : []);
        } catch (err) {
            // Only log real errors (not AbortError from component unmount)
            if ((err as Error)?.name !== "AbortError") {
                console.error("Failed to fetch sessions:", err);
            }
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
            const response = await fetch(`${getApiUrl()}/sessions/${editingId}`, {
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

    const handleSelectSession = async (id: string) => {
        if (id === currentSessionId) return;

        // Trigger sync before switching
        try {
            await fetch(`${getApiUrl()}/sessions/${id}/sync`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ previous_session_id: currentSessionId, user_id: user?.id }),
            });
        } catch (err) {
            console.error("Failed to sync session with FreeCAD", err);
        }

        onSelectSession(id);
    };

    const handleNewChatClick = async () => {
        // Trigger sync to "new" state before resetting
        try {
            await fetch(`${getApiUrl()}/sessions/new/sync`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ previous_session_id: currentSessionId, user_id: user?.id }),
            });
        } catch (err) {
            console.error("Failed to sync new chat with FreeCAD", err);
        }
        onNewChat();
    };

    return (
        <>
            <button
                onClick={() => setIsOpen(!isOpen)}
                className="fixed top-4 left-4 z-50 p-2 bg-white/10 backdrop-blur-md rounded-lg md:hidden text-white"
            >
                {isOpen ? <X size={20} /> : <Menu size={20} />}
            </button>

            <div className={`fixed inset-y-0 left-0 z-40 w-64 bg-sidebar backdrop-blur-2xl border-r border-sidebar-border transition-transform duration-300 transform ${isOpen ? "translate-x-0" : "-translate-x-full"} md:translate-x-0 flex flex-col`}>
                <div className="p-4 flex flex-col h-full">
                    <button
                        onClick={handleNewChatClick}
                        className="flex items-center gap-3 w-full p-4 mb-6 bg-background/5 hover:bg-background border border-border rounded-2xl text-foreground font-semibold shadow-xs hover:shadow-sm transition-all group"
                    >
                        <Plus size={18} className="group-hover:rotate-90 transition-transform" />
                        New Chat
                    </button>

                    <div className="flex-1 overflow-y-auto space-y-2 custom-scrollbar">
                        <h3 className="px-4 text-[10px] uppercase tracking-widest font-bold text-foreground/40 mb-2">History</h3>
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
                                            onClick={() => handleSelectSession(session.id)}
                                            className={`flex items-center gap-3 w-full p-3 rounded-xl text-left text-sm transition-all pr-10 border ${currentSessionId === session.id
                                                ? "bg-primary/10 text-primary border-primary/30 shadow-xs"
                                                : "text-foreground/70 border-transparent hover:bg-foreground/5 hover:text-foreground"
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

                    <div className="mt-auto pt-4 border-t border-border relative">
                        {showProfileMenu && (
                            <div
                                ref={profileMenuRef}
                                className="absolute bottom-16 left-2 right-2 bg-card border border-border rounded-2xl p-2 shadow-lg dark:shadow-2xl animate-in fade-in slide-in-from-bottom-2 duration-200 z-50"
                            >
                                <button
                                    onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
                                    className="flex items-center gap-3 w-full p-3 hover:bg-foreground/5 rounded-xl text-xs text-foreground/70 hover:text-foreground transition-all"
                                >
                                    {mounted && resolvedTheme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
                                    {mounted && resolvedTheme === "dark" ? "Light Mode" : "Dark Mode"}
                                </button>
                                <button className="flex items-center gap-3 w-full p-3 hover:bg-foreground/5 rounded-xl text-xs text-foreground/70 hover:text-foreground transition-all">
                                    <Settings size={14} />
                                    Settings
                                </button>
                                <div className="h-px bg-border my-1" />
                                <button 
                                    onClick={() => signOut({ redirectUrl: "/sign-in" })}
                                    className="flex items-center gap-3 w-full p-3 hover:bg-red-500/10 rounded-xl text-xs text-red-600 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300 transition-all font-medium"
                                >
                                    <LogOut size={14} />
                                    Log out
                                </button>
                            </div>
                        )}

                        <div
                            className="flex items-center justify-between w-full p-2 hover:bg-foreground/5 rounded-2xl transition-all"
                        >
                            <div className="flex items-center gap-3">
                                <UserButton appearance={{
                                    elements: {
                                        userButtonAvatarBox: 'w-8 h-8 rounded-full shadow-lg border border-border',
                                    }
                                }} />
                                <div className="flex flex-col text-left">
                                    <span className="text-xs font-semibold text-foreground truncate max-w-[120px]">
                                        {user?.fullName || user?.primaryEmailAddress?.emailAddress || "User"}
                                    </span>
                                    <span className="text-[10px] text-foreground/50 font-medium uppercase tracking-wider">Major Project</span>
                                </div>
                            </div>
                             <button 
                                onClick={() => setShowProfileMenu(!showProfileMenu)}
                                className="p-2 hover:bg-foreground/5 rounded-lg transition-colors"
                            >
                                <Menu size={16} className="text-foreground/40 hover:text-foreground transition-colors" />
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </>
    );
}
