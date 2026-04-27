"use client";

import { useEffect, useState, useMemo } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Stage } from "@react-three/drei";
import { STLLoader } from "three-stdlib";
import { BufferGeometry, MeshStandardMaterial } from "three";
import { API_URL } from "@/lib/api";
import { Loader2 } from "lucide-react";

interface CADViewerProps {
  modelUrl: string | null;
  highlightText?: string | null;
}

export default function CADViewer({ modelUrl, highlightText }: CADViewerProps) {
  const [geometry, setGeometry] = useState<BufferGeometry | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!modelUrl) {
      setGeometry(null);
      return;
    }

    setLoading(true);
    setError("");

    const loader = new STLLoader();
    loader.load(
      modelUrl.startsWith('http') ? modelUrl : `${API_URL}${modelUrl}`,
      (geo) => {
        setGeometry(geo);
        setLoading(false);
      },
      undefined,
      (err) => {
        console.error("Error loading STL:", err);
        setError("Failed to load 3D model.");
        setLoading(false);
      }
    );
  }, [modelUrl]);

  const material = useMemo(() => {
    return new MeshStandardMaterial({
      color: "#8b5cf6", // subtle purple
      roughness: 0.4,
      metalness: 0.2,
    });
  }, []);

  return (
    <div className="relative w-full h-full min-h-[300px] bg-foreground/[0.02] border border-border rounded-xl overflow-hidden flex items-center justify-center">
      {/* Subtle grid background via CSS */}
      <div 
        className="absolute inset-0 pointer-events-none opacity-[0.03]" 
        style={{ backgroundImage: 'linear-gradient(to right, currentColor 1px, transparent 1px), linear-gradient(to bottom, currentColor 1px, transparent 1px)', backgroundSize: '20px 20px' }}
      />

      {loading && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 z-10 bg-background/50 backdrop-blur-sm">
          <Loader2 size={24} className="animate-spin text-primary" />
          <p className="text-sm font-mono text-foreground/60">Loading 3D Mesh...</p>
        </div>
      )}

      {error && (
        <div className="absolute inset-0 flex items-center justify-center z-10">
          <p className="text-sm text-red-400">{error}</p>
        </div>
      )}

      {!modelUrl && !loading && (
        <div className="absolute inset-0 flex items-center justify-center z-10">
          <p className="text-xs uppercase tracking-widest font-bold text-foreground/20">3D Viewer Disabled</p>
        </div>
      )}

      {/* Overlay Text for Highlight */}
      {highlightText && geometry && !loading && (
        <div className="absolute top-4 left-4 z-10 bg-amber-500/10 border border-amber-500/20 text-amber-500 text-xs px-3 py-1.5 rounded-lg font-mono font-bold animate-in fade-in slide-in-from-top-2">
          Modifying: {highlightText}
        </div>
      )}

      {geometry && (
        <Canvas shadows camera={{ position: [50, 50, 50], fov: 45 }}>
          <Stage environment="city" intensity={0.5} adjustCamera>
            <mesh geometry={geometry} material={material} castShadow receiveShadow />
          </Stage>
          <OrbitControls makeDefault autoRotate autoRotateSpeed={0.5} />
        </Canvas>
      )}
    </div>
  );
}
