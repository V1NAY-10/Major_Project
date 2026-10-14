export interface Verification {
  id: string;
  field: string;
  value?: unknown;
  status: "ok" | "missing";
  note?: string;
  /** When the edit had a target value: what was asked, what was measured, and the difference. */
  requested?: unknown;
  achieved?: unknown;
  deviation?: number;
  within_tolerance?: boolean;
  unit?: "mm" | "deg";
  /** Pre-formatted line in the session's unit, e.g. "✓ P5-P6 = 120.000 mm (Δ 0.0000 mm)". */
  display?: string;
}

/** A value that changed although it was not the target of the edit. */
export interface SideEffectRow {
  key: string;
  label: string;
  before: number | null;
  after: number | null;
  delta: number | null;
  kind: "length" | "angle" | "count";
  /** It involves a feature the edit moved or rebuilt; unexpected changes are flagged. */
  expected: boolean;
}

export interface SideEffects {
  rows: SideEffectRow[];
  total: number;
  unexpected: number;
  added: number;
  removed: number;
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
  side_effects?: SideEffects;
  /** Pre-edit check warnings (e.g. a wall thinner than the minimum). */
  warnings?: string[];
  /** A dry run (preview): nothing was changed. */
  preview?: boolean;
  /** How a lock was respected, e.g. "anchor 'symmetric'". */
  lock_resolution?: string;
}

export interface Message {
  id?: string;
  role: "user" | "assistant";
  content: string;
  operations?: Operation[];
  version?: number;
  kind?: "upload" | "undo" | "redo" | "error";
  /** IDs selected in the viewer when the message was sent (the components it applies to). */
  selection?: string[];
  /** A previewed plan proposed in this message (approve / cancel while it is the pending one). */
  plan?: import("./cad").PendingPlan;
  /** Clickable answers to a question in this message. */
  choices?: { id: string; label: string }[];
  created_at?: string;
}

export interface ChatResponse {
  message: Message;
  state: import("./cad").CadState | null;
  /** Feature / dimension / pattern IDs the reply refers to; shown highlighted. */
  highlight?: string[];
}
