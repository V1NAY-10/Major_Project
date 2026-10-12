"use client";

import { useEffect, useState, useMemo } from "react";
import { Canvas } from "@react-three/fiber";
import { Bounds, Edges, GizmoHelper, GizmoViewport, OrbitControls } from "@react-three/drei";
import { STLLoader } from "three-stdlib";
import { BufferGeometry, MeshStandardMaterial } from "three";
import { Loader2 } from "lucide-react";
import { apiBlob } from "@/lib/api";

interface CADViewerProps {
  /** Backend path of the STL, e.g. /cad/model/<id>?v=3 (loaded with the auth header). */
  modelPath: string | null;
  /** Changing this re-frames the camera (e.g. a new file); version changes keep the current view. */
  fitKey?: string;
}

export default function CADViewer({ modelPath, fitKey }: CADViewerProps) {
  const [geometry, setGeometry] = useState<BufferGeometry | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!modelPath) {
      setGeometry(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    apiBlob(modelPath)
      .then(({ blob }) => blob.arrayBuffer())
      .then((buf) => {
        if (cancelled) return;
        const geo = new STLLoader().parse(buf);
        geo.computeVertexNormals();
        setGeometry(geo);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error("Error loading STL:", err);
        setError("Failed to load 3D model.");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [modelPath]);

  const material = useMemo(
    () => new MeshStandardMaterial({ color: "#8b5cf6", roughness: 0.45, metalness: 0.15 }),
    []
  );

  return (
    <div className="relative w-full h-full min-h-[300px] overflow-hidden flex items-center justify-center">
      {loading && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 z-10 bg-background/40 backdrop-blur-sm">
          <Loader2 size={24} className="animate-spin text-primary" />
          <p className="text-xs font-mono text-foreground/60">Loading mesh…</p>
        </div>
      )}
      {error && (
        <div className="absolute inset-0 flex items-center justify-center z-10">
          <p className="text-sm text-red-400">{error}</p>
        </div>
      )}
      {!modelPath && !loading && (
        <p className="text-xs uppercase tracking-widest font-bold text-foreground/20">No model</p>
      )}

      {geometry && (
        // FreeCAD is Z-up: give the camera a Z-up frame so the part appears as it does in FreeCAD
        <Canvas key={fitKey} camera={{ position: [1, -1.6, 1.1], up: [0, 0, 1], fov: 40, near: 0.01, far: 1e7 }}>
          <ambientLight intensity={0.55} />
          <directionalLight position={[1, -2, 3]} intensity={1.1} />
          <directionalLight position={[-2, 1, -1]} intensity={0.35} />
          <Bounds fit clip margin={1.3}>
            <mesh geometry={geometry} material={material}>
              <Edges threshold={25} color="#1e1b4b" />
            </mesh>
          </Bounds>
          <OrbitControls makeDefault />
          <GizmoHelper alignment="bottom-right" margin={[56, 56]}>
            <GizmoViewport axisColors={["#ef4444", "#22c55e", "#3b82f6"]} labelColor="white" />
          </GizmoHelper>
        </Canvas>
      )}
    </div>
  );
}
