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

  /** Display unit / precision / tolerance of the session. */
  const updateSettings = useCallback(
    async (patch: { display_unit?: "mm" | "in"; precision?: number; tolerance_length?: number; tolerance_angle?: number }) => {
      if (!sessionId) return;
      const state = await apiFetch<CadState>(`/cad/settings/${sessionId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      setCad(state);
    },
    [sessionId]
  );

  /** Lock (or unlock) a dimension / feature at its current value; mode "centered" keeps a feature centred. */
  const setLock = useCallback(
    async (target: string, locked: boolean, mode: "value" | "centered" = "value", axis?: "X" | "Y" | "Z") => {
      if (!sessionId) return;
      const state = await postJson<CadState>(`/cad/locks/${sessionId}${locked ? "" : "/remove"}`, { target, mode, axis });
      setCad(state);
    },
    [sessionId]
  );

  /** Choose datum faces (plane IDs); "default" resets a letter. */
  const setDatums = useCallback(
    async (datums: Partial<Record<"A" | "B" | "C", string>>) => {
      if (!sessionId) return;
      const state = await apiFetch<CadState>(`/cad/datums/${sessionId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(datums),
      });
      setCad(state);
    },
    [sessionId]
  );

  /** GET a JSON analysis endpoint of this session, e.g. getJson("/cad/mass"). */
  const getJson = useCallback(
    async <T,>(path: string, params: Record<string, string | number> = {}): Promise<T | null> => {
      if (!sessionId) return null;
      const q = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])).toString();
      return apiFetch<T>(`${path}/${sessionId}${q ? `?${q}` : ""}`);
    },
    [sessionId]
  );

  /** POST to a session endpoint that returns {message, state}; the state replaces the current one. */
  const postAction = useCallback(
    async <T extends { state?: CadState },>(path: string, body?: unknown): Promise<T | null> => {
      if (!sessionId) return null;
      const res = await postJson<T>(path.replace("{sid}", sessionId), body);
      if (res?.state) setCad(res.state);
      return res;
    },
    [sessionId]
  );

  /** Version tree: make version v current (the next edit branches from it). */
  const checkout = useCallback(
    async (v: number) => {
      if (!sessionId) return;
      setCad(await postJson<CadState>(`/cad/checkout/${sessionId}/${v}`));
    },
    [sessionId]
  );

  const renameVersion = useCallback(
    async (v: number, name: string) => {
      if (!sessionId) return;
      setCad(
        await apiFetch<CadState>(`/cad/versions/${sessionId}/${v}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        })
      );
    },
    [sessionId]
  );

  const setMaterial = useCallback(
    async (part: string, material: string) => {
      if (!sessionId) return;
      setCad(
        await apiFetch<CadState>(`/cad/materials/${sessionId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ part, material }),
        })
      );
    },
    [sessionId]
  );

  /** Save a file the backend generates (BOM CSV / XLSX, drawing PDF / SVG). */
  const downloadFile = useCallback(async (path: string, fallbackName: string) => {
    const { blob, filename } = await apiBlob(path);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename ?? fallbackName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
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
    updateSettings,
    setLock,
    setDatums,
    getJson,
    postAction,
    checkout,
    renameVersion,
    setMaterial,
    downloadFile,
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
