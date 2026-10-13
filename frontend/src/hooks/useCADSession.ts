import { useState, useCallback, useEffect, useRef } from "react";
import { ApiError, apiBlob, apiFetch, postJson } from "@/lib/api";
import { CadState, FreeCADStatus, HighlightMesh, PickResult, SelectMode } from "@/types/cad";

/** CAD state for the active chat session. The backend + FreeCAD own the truth; this mirrors it. */
export function useCADSession() {
  const [cad, setCad] = useState<CadState | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [highlights, setHighlights] = useState<HighlightMesh[]>([]);
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const highlightSeq = useRef(0);

  const resetCadState = useCallback(() => {
    setCad(null);
    setHighlights([]);
  }, []);

  // Feature geometry belongs to one version of one model: drop it when either changes.
  // (adjusting state during render instead of in an effect avoids a cascading re-render)
  const sessionId = cad?.session_id;
  const modelKey = `${sessionId}:${cad?.version}`;
  const [highlightKey, setHighlightKey] = useState(modelKey);
  if (highlightKey !== modelKey) {
    setHighlightKey(modelKey);
    setHighlights([]);
  }

  /**
   * Highlight features (H3, P1-P2, PAT1…) in the web viewer and select them in FreeCAD.
   * Debounced; failures are ignored (highlighting is a convenience).
   */
  const highlight = useCallback(
    (ids: string[]) => {
      if (!sessionId) return;
      if (highlightTimer.current) clearTimeout(highlightTimer.current);
      const seq = ++highlightSeq.current;
      if (!ids.length) {
        setHighlights([]);
        return;
      }
      highlightTimer.current = setTimeout(() => {
        postJson<{ meshes: HighlightMesh[]; missing: string[] }>(`/cad/highlight/${sessionId}`, { ids, select: true })
          .then((res) => {
            if (seq === highlightSeq.current) setHighlights(res.meshes ?? []);
          })
          .catch(() => {});
      }, 150);
    },
    [sessionId]
  );

  /** The component under a point clicked in the viewer (global mm). */
  const pick = useCallback(
    async (point: [number, number, number], mode: SelectMode): Promise<PickResult | null> => {
      if (!sessionId) return null;
      return postJson<PickResult>(`/cad/pick/${sessionId}`, { point, mode });
    },
    [sessionId]
  );

  /** IDs of what is currently selected in the FreeCAD window. */
  const freecadSelection = useCallback(async (): Promise<string[]> => {
    if (!sessionId) return [];
    const res = await apiFetch<{ ids: string[] }>(`/cad/freecad-selection/${sessionId}`);
    return res.ids ?? [];
  }, [sessionId]);

  /** Bring FreeCAD (with this session's document) to the front. */
  const showInFreeCAD = useCallback(async () => {
    if (!sessionId) return;
    await postJson(`/cad/show/${sessionId}`);
  }, [sessionId]);

  /** Load the session's CAD state (404 = no model yet) and bring its document to front in FreeCAD. */
  const loadSessionCad = useCallback(async (sessionId: string) => {
    try {
      const state = await apiFetch<CadState>(`/cad/state/${sessionId}`);
      setCad(state);
      postJson<CadState>(`/cad/activate/${sessionId}`).then(setCad).catch(() => {});
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setCad(null);
      else console.error("Failed to load CAD state", err);
    }
  }, []);

  const upload = useCallback(async (file: File, sessionId: string) => {
    setIsUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("session_id", sessionId);
      const state = await apiFetch<CadState>("/cad/upload", { method: "POST", body: form });
      setCad(state);
      return state;
    } finally {
      setIsUploading(false);
    }
  }, []);

  const step = useCallback(async (sessionId: string, dir: "undo" | "redo") => {
    setBusy(true);
    try {
      const state = await postJson<CadState>(`/cad/${dir}/${sessionId}`);
      setCad(state);
      return state;
    } finally {
      setBusy(false);
    }
  }, []);

  /** Exports are fetched as a blob and saved via a temporary link (keeps the backend error message on failure). */
  const download = useCallback(
    async (format: string) => {
      if (!cad) return;
      const { blob, filename } = await apiBlob(`/cad/download/${cad.session_id}?format=${format}`);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename ?? `${cad.filename.replace(/\.[^.]+$/, "")}_v${cad.version}.${format}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
    [cad]
  );

  return {
    cad,
    setCad,
    isUploading,
    busy,
    resetCadState,
    loadSessionCad,
    upload,
    undo: (sessionId: string) => step(sessionId, "undo"),
    redo: (sessionId: string) => step(sessionId, "redo"),
    /** Backend path of the current version's STL. */
    modelPath: cad ? cad.mesh_url : null,
    download,
    highlights,
    highlight,
    showInFreeCAD,
    pick,
    freecadSelection,
  };
}

/** Polls the FreeCAD bridge so the header can show whether edits will reach FreeCAD. */
export function useFreeCADStatus(intervalMs = 10000) {
  const [status, setStatus] = useState<FreeCADStatus | null>(null);
  useEffect(() => {
    let alive = true;
    const poll = () =>
      apiFetch<FreeCADStatus>("/freecad/status")
        .then((s) => alive && setStatus(s))
        .catch((e) => alive && setStatus({ connected: false, error: String(e.message ?? e) }));
    poll();
    const id = setInterval(poll, intervalMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [intervalMs]);
  return status;
}
