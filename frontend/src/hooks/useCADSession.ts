import { useState, useCallback } from "react";
import { API_URL } from "@/lib/api";
import { CADState, ParsedData } from "@/types/cad";

export function useCADSession() {
  const [cadState, setCadState] = useState<CADState>({
    fileId: null,
    parsedData: null,
    modelUrl: null,
    fileName: null,
    isUploading: false,
  });

  const resetCadState = useCallback(() => {
    setCadState({
      fileId: null,
      parsedData: null,
      modelUrl: null,
      fileName: null,
      isUploading: false,
    });
  }, []);

  const fetchSessionDetails = useCallback(async (sessionId: string, userId: string) => {
    try {
      const res = await fetch(`${API_URL}/sessions/${sessionId}?user_id=${userId}`);
      if (res.ok) {
        const data = await res.json();
        if (data.cad_file_id) {
          setCadState({
            fileId: data.cad_file_id,
            fileName: data.cad_filename,
            parsedData: data.cad_parsed_data,
            modelUrl: `${API_URL}/cad/model/${data.cad_file_id}`,
            isUploading: false,
          });
        } else {
          resetCadState();
        }
      }
    } catch (err) {
      console.error("Failed to fetch session details", err);
    }
  }, [resetCadState]);

  const handleUpload = useCallback(async (file: File, sessionId: string | null, userId: string) => {
    setCadState(prev => ({ ...prev, isUploading: true }));

    const formData = new FormData();
    formData.append("file", file);
    if (sessionId) formData.append("session_id", sessionId);

    try {
      const res = await fetch(`${API_URL}/cad/upload`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) throw new Error("Upload failed");
      
      const data = await res.json();
      setCadState({
        fileId: data.file_id,
        fileName: data.filename,
        parsedData: data.parsed_data,
        modelUrl: `${API_URL}/cad/model/${data.file_id}`,
        isUploading: false,
      });
      return data;
    } catch (err) {
      setCadState(prev => ({ ...prev, isUploading: false }));
      throw err;
    }
  }, []);

  const updateModelUrl = useCallback((fileId: string) => {
    setCadState(prev => ({
      ...prev,
      modelUrl: `${API_URL}/cad/model/${fileId}?t=${Date.now()}`
    }));
  }, []);

  /**
   * Update parsedData in state — called after a successful modification so the
   * Feature Map panel reflects the latest component parameters (including XYZ).
   */
  const updateParsedData = useCallback((newParsedData: ParsedData) => {
    setCadState(prev => ({ ...prev, parsedData: newParsedData }));
  }, []);

  /**
   * Fetch the latest parsed component data from the backend for a given fileId
   * and push it into state. Used after modify-model to refresh the Feature Map.
   */
  const refreshParsedData = useCallback(async (fileId: string) => {
    try {
      const res = await fetch(`${API_URL}/cad/parsed-data/${fileId}`);
      if (res.ok) {
        const data = await res.json();
        if (data.parsed_data) {
          setCadState(prev => ({ ...prev, parsedData: data.parsed_data as ParsedData }));
        }
      }
    } catch (err) {
      console.error("Failed to refresh parsed data", err);
    }
  }, []);

  return {
    ...cadState,
    setCadState,
    resetCadState,
    fetchSessionDetails,
    handleUpload,
    updateModelUrl,
    updateParsedData,
    refreshParsedData,
  };
}
