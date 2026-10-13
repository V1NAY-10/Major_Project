import { Send, Loader2, Paperclip, Crosshair, X } from "lucide-react";
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
  /** Components selected in the viewer; the message applies to them. */
  selection?: { id: string; text: string }[];
  onRemoveSelection?: (id: string) => void;
  /** Lets the page focus the message box (e.g. after a quick action). */
  textareaRef?: React.RefObject<HTMLTextAreaElement | null>;
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
  selection = [],
  onRemoveSelection,
  textareaRef,
}: ChatInputProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  return (
    <div className="p-4 md:px-8 md:pb-6 shrink-0 bg-linear-to-t from-background via-background to-transparent">
      <form onSubmit={onSubmit} className="max-w-3xl mx-auto relative group">
        {hasCad && selection.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 mb-2 text-[11px]">
            <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400 font-semibold">
              <Crosshair size={12} /> Editing
            </span>
            {selection.map((s) => (
              <span
                key={s.id}
                className="flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-full border border-amber-500/40 bg-amber-500/10"
              >
                <span className="font-mono font-bold">{s.id}</span>
                {s.text && <span className="text-foreground/55 max-w-[180px] truncate">{s.text}</span>}
                {onRemoveSelection && (
                  <button
                    type="button"
                    onClick={() => onRemoveSelection(s.id)}
                    className="p-0.5 rounded-full hover:bg-foreground/10 text-foreground/40"
                    title="Remove from selection"
                  >
                    <X size={10} />
                  </button>
                )}
              </span>
            ))}
          </div>
        )}
        {hasCad && !prompt && !selection.length && suggestions.length > 0 && (
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
            ref={textareaRef}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSubmit();
              }
            }}
            placeholder={
              hasCad && selection.length
                ? `Describe the change to ${selection.map((s) => s.id).join(", ")}, e.g. “make it 2 mm bigger”…`
                : hasCad
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
