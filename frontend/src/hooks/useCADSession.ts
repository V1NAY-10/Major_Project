import { useState, useCallback, useEffect } from "react";
import { ApiError, apiBlob, apiFetch, postJson } from "@/lib/api";
import { CadState, FreeCADStatus } from "@/types/cad";

/** CAD state for the active chat session. The backend + FreeCAD own the truth; this mirrors it. */
export function useCADSession() {
  const [cad, setCad] = useState<CadState | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [busy, setBusy] = useState(false);

  const resetCadState = useCallback(() => setCad(null), []);

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

  /** Exports need the auth header, so they are fetched as a blob and saved via a temporary link. */
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
    /** Backend path of the current version's STL (fetched with auth by the viewer). */
    modelPath: cad ? cad.mesh_url : null,
    download,
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
