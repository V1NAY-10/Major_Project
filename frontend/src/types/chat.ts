export interface Verification {
  id: string;
  field: string;
  value?: unknown;
  status: "ok" | "missing";
  note?: string;
}

export interface Operation {
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  message?: string;
  error?: string;
  verified?: Verification[];
  new_features?: string[];
  removed_features?: string[];
}

export interface Message {
  id?: string;
  role: "user" | "assistant";
  content: string;
  operations?: Operation[];
  version?: number;
  kind?: "upload" | "undo" | "redo" | "error";
  created_at?: string;
}
