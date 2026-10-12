export type Vec3 = [number, number, number];

export interface BBox {
  min: Vec3;
  max: Vec3;
  size: Vec3;
}

export interface CadObject {
  name: string;
  label: string;
  type: string;
  volume: number;
  area: number;
  solids: number;
  faces: number;
  edges: number;
  bbox: BBox;
  center_of_mass: Vec3;
}

export interface Feature {
  id: string;
  kind: "hole" | "boss" | "round" | "plane" | "cone" | "sphere";
  object: string;
  diameter?: number;
  radius?: number;
  depth?: number;
  length?: number;
  through?: boolean;
  center?: Vec3;
  entry?: Vec3;
  normal?: Vec3;
  area?: number;
  label?: string;
  position?: string;
  is_cap?: boolean;
  countersinks?: { outer_diameter: number; angle_deg: number; at: Vec3 }[];
  [key: string]: unknown;
}

export interface ParametricObject {
  name: string;
  label: string;
  type: string;
  properties: Record<string, number | string | boolean>;
  expressions?: Record<string, string>;
}

export interface SketchInfo {
  name: string;
  label: string;
  constraints: { index: number; name: string; type: string; value: number; driving: boolean }[];
}

export interface ModelInfo {
  units: string;
  document: string;
  objects: CadObject[];
  bbox: BBox | null;
  features: Partial<Record<Feature["kind"], Feature[]>>;
  counts: Record<string, number>;
  parametric: ParametricObject[];
  sketches: SketchInfo[];
  spreadsheets: { name: string; label: string; cells: { cell: string; alias: string; value: unknown }[] }[];
  is_parametric: boolean;
}

export interface VersionEntry {
  v: number;
  description: string;
  created_at: string;
}

export interface CadState {
  session_id: string;
  filename: string;
  version: number;
  versions: VersionEntry[];
  can_undo: boolean;
  can_redo: boolean;
  dirty: boolean;
  info: ModelInfo;
  mesh_url: string;
}

export interface FreeCADStatus {
  connected: boolean;
  gui?: boolean;
  version?: string;
  error?: string;
}
