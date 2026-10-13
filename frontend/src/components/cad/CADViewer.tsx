"use client";

import { useEffect, useState, useMemo } from "react";
import { Canvas } from "@react-three/fiber";
import { Bounds, Edges, GizmoHelper, GizmoViewport, OrbitControls } from "@react-three/drei";
import { STLLoader } from "three-stdlib";
import { BufferAttribute, BufferGeometry, DoubleSide, MeshStandardMaterial } from "three";
import { Loader2 } from "lucide-react";
import { apiBlob } from "@/lib/api";
import { HighlightMesh } from "@/types/cad";

interface CADViewerProps {
  /** Backend path of the STL, e.g. /cad/model/<id>?v=3. */
  modelPath: string | null;
  /** Changing this re-frames the camera (e.g. a new file); version changes keep the current view. */
  fitKey?: string;
  /** Feature faces to draw highlighted on top of the model (same global frame as the STL). */
  highlights?: HighlightMesh[];
  /** Click on the model: the clicked point (global mm) and whether Shift/Ctrl was held (add to selection). */
  onPick?: (point: [number, number, number], additive: boolean) => void;
  /** Controls drawn over the top-right corner. */
  toolbar?: React.ReactNode;
}

// A click that moved the pointer more than this (px) was an orbit/pan drag, not a pick.
const CLICK_SLOP = 4;
const noRaycast = () => null;

function useHighlightGeometries(highlights: HighlightMesh[]) {
  const geos = useMemo(
    () =>
      highlights
        .filter((h) => h.positions.length && h.indices.length)
        .map((h) => {
          const g = new BufferGeometry();
          g.setAttribute("position", new BufferAttribute(new Float32Array(h.positions), 3));
          g.setIndex(h.indices);
          g.computeVertexNormals();
          return { id: h.id, geometry: g };
        }),
    [highlights]
  );
  useEffect(() => () => geos.forEach((g) => g.geometry.dispose()), [geos]);
  return geos;
}

export default function CADViewer({ modelPath, fitKey, highlights = [], onPick, toolbar }: CADViewerProps) {
  // Result of the last finished load; anything for another path is stale (a new version is loading).
  const [loaded, setLoaded] = useState<{ path: string; geometry: BufferGeometry | null; error: string } | null>(null);

  useEffect(() => {
    if (!modelPath) return;
    let cancelled = false;
    apiBlob(modelPath)
      .then(({ blob }) => blob.arrayBuffer())
      .then((buf) => {
        if (cancelled) return;
        const geo = new STLLoader().parse(buf);
        geo.computeVertexNormals();
        setLoaded({ path: modelPath, geometry: geo, error: "" });
      })
      .catch((err) => {
        if (cancelled) return;
        console.error("Error loading STL:", err);
        setLoaded({ path: modelPath, geometry: null, error: "Failed to load 3D model." });
      });
    return () => {
      cancelled = true;
    };
  }, [modelPath]);

  const current = loaded && loaded.path === modelPath ? loaded : null;
  const loading = !!modelPath && !current;
  const error = current?.error ?? "";
  // Keep showing the previous version's mesh while the new one loads.
  const geometry = modelPath ? (current ? current.geometry : loaded?.geometry ?? null) : null;

  const material = useMemo(
    () => new MeshStandardMaterial({ color: "#8b5cf6", roughness: 0.45, metalness: 0.15 }),
    []
  );
  // Drawn over the model's own faces: polygonOffset pulls it towards the camera so it wins the depth test.
  const highlightMaterial = useMemo(
    () =>
      new MeshStandardMaterial({
        color: "#f59e0b",
        emissive: "#f59e0b",
        emissiveIntensity: 0.6,
        roughness: 0.4,
        polygonOffset: true,
        polygonOffsetFactor: -2,
        polygonOffsetUnits: -2,
        side: DoubleSide, // hole walls are seen from inside
      }),
    []
  );
  const highlightGeos = useHighlightGeometries(highlights);

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
      {toolbar && <div className="absolute top-2 right-2 z-10 flex items-center gap-1">{toolbar}</div>}
      {highlightGeos.length > 0 && (
        <div className="absolute top-2 left-2 z-10 rounded-md bg-amber-500/15 border border-amber-500/30 px-2 py-0.5 text-[10px] font-mono text-amber-600 dark:text-amber-300">
          {highlightGeos.map((g) => g.id).join(", ")}
        </div>
      )}

      {geometry && (
        // FreeCAD is Z-up: give the camera a Z-up frame so the part appears as it does in FreeCAD
        <Canvas key={fitKey} camera={{ position: [1, -1.6, 1.1], up: [0, 0, 1], fov: 40, near: 0.01, far: 1e7 }}>
          <ambientLight intensity={0.55} />
          <directionalLight position={[1, -2, 3]} intensity={1.1} />
          <directionalLight position={[-2, 1, -1]} intensity={0.35} />
          <Bounds fit clip margin={1.3}>
            <mesh
              geometry={geometry}
              material={material}
              onClick={(e) => {
                if (!onPick || e.delta > CLICK_SLOP) return;
                e.stopPropagation();
                const ne = e.nativeEvent;
                onPick([e.point.x, e.point.y, e.point.z], ne.shiftKey || ne.ctrlKey || ne.metaKey);
              }}
              onPointerOver={() => onPick && (document.body.style.cursor = "crosshair")}
              onPointerOut={() => (document.body.style.cursor = "")}
            >
              <Edges threshold={25} color="#1e1b4b" />
            </mesh>
          </Bounds>
          {highlightGeos.map((g) => (
            // clicks go through the highlight to the model underneath
            <mesh key={g.id} geometry={g.geometry} material={highlightMaterial} renderOrder={1} raycast={noRaycast} />
          ))}
          <OrbitControls makeDefault />
          <GizmoHelper alignment="bottom-right" margin={[56, 56]}>
            <GizmoViewport axisColors={["#ef4444", "#22c55e", "#3b82f6"]} labelColor="white" />
          </GizmoHelper>
        </Canvas>
      )}
    </div>
  );
}
