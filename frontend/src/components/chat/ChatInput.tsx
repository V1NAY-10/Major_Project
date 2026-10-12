import { Send, Loader2, Paperclip } from "lucide-react";
import { useRef } from "react";

export const CAD_ACCEPT = ".step,.stp,.iges,.igs,.brep,.brp,.fcstd";

interface ChatInputProps {
  prompt: string;
  setPrompt: (val: string) => void;
  loading: boolean;
  isUploadingCad: boolean;
  onUpload: (file: File) => void;
  onSubmit: (e?: React.FormEvent) => void;
  hasCad: boolean;
  suggestions?: string[];
}

export default function ChatInput({
  prompt,
  setPrompt,
  loading,
  isUploadingCad,
  onUpload,
  onSubmit,
  hasCad,
  suggestions = [],
}: ChatInputProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  return (
    <div className="p-4 md:px-8 md:pb-6 shrink-0 bg-linear-to-t from-background via-background to-transparent">
      <form onSubmit={onSubmit} className="max-w-3xl mx-auto relative group">
        {hasCad && !prompt && suggestions.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mb-2">
            {suggestions.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setPrompt(s)}
                className="text-[11px] px-2.5 py-1 bg-foreground/5 hover:bg-foreground/10 border border-border rounded-full text-foreground/60 hover:text-foreground transition-all"
              >
                {s}
              </button>
            ))}
          </div>
        )}
        <input
          type="file"
          ref={fileInputRef}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onUpload(file);
            if (fileInputRef.current) fileInputRef.current.value = "";
          }}
          accept={CAD_ACCEPT}
          className="hidden"
        />
        <div className="flex items-end gap-2 bg-foreground/5 hover:bg-foreground/10 focus-within:bg-foreground/10 border border-border focus-within:border-purple-500/40 rounded-3xl p-2 transition-all shadow-2xl backdrop-blur-xl">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={isUploadingCad || loading}
            className="p-4 text-foreground/30 hover:text-purple-400 transition-all transform hover:scale-110 disabled:opacity-30"
            title={hasCad ? "Replace CAD file" : "Attach CAD file (STEP, IGES, BREP, FCStd)"}
          >
            {isUploadingCad ? <Loader2 size={20} className="animate-spin text-purple-400" /> : <Paperclip size={20} />}
          </button>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSubmit();
              }
            }}
            placeholder={
              hasCad
                ? "Ask about the part or describe a change, e.g. “make H2 Ø8 mm”…"
                : "Attach a CAD file to start, or ask a CAD question…"
            }
            className="flex-1 bg-transparent border-none outline-none focus:ring-0 p-4 text-sm text-foreground placeholder-foreground/30 resize-none min-h-[56px] max-h-32 scrollbar-none"
          />
          <button
            type="submit"
            disabled={loading || !prompt.trim()}
            className="p-4 bg-purple-600 hover:bg-purple-500 disabled:opacity-20 rounded-2xl transition-all shadow-lg shadow-purple-500/20 transform hover:scale-105 active:scale-95"
          >
            {loading ? <Loader2 size={20} className="animate-spin text-white" /> : <Send size={20} className="text-white" />}
          </button>
        </div>
      </form>
    </div>
  );
}
