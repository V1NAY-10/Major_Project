import { Send, Zap, Loader2, Paperclip } from "lucide-react";
import { useRef } from "react";

interface ChatInputProps {
  prompt: string;
  setPrompt: (val: string) => void;
  loading: boolean;
  isUploadingCad: boolean;
  onUpload: (file: File) => void;
  onSubmit: (e?: React.FormEvent) => void;
  hasCad: boolean;
}

export default function ChatInput({ 
  prompt, 
  setPrompt, 
  loading, 
  isUploadingCad, 
  onUpload, 
  onSubmit,
  hasCad 
}: ChatInputProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  return (
    <div className="p-4 md:p-8 shrink-0 bg-linear-to-t from-background via-background to-transparent">
      <form onSubmit={onSubmit} className="max-w-3xl mx-auto relative group">
        <input
          type="file"
          ref={fileInputRef}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onUpload(file);
            if (fileInputRef.current) fileInputRef.current.value = "";
          }}
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
                onSubmit();
              }
            }}
            placeholder={hasCad ? "How should we modify this model?" : "Generate a 3D model or upload a file..."}
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
  );
}
