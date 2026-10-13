export type Vec3 = [number, number, number];

export interface BBox {
  min: Vec3;
  max: Vec3;
  size: Vec3;
}

export interface CadObject {
  /** Stable part ID (B1, B2…). */
  id?: string;
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
  parent?: string;
  /** Faces the engine could not classify (B-spline free-form etc.). */
  unrecognized_faces?: number;
}

export interface Feature {
  id: string;
  kind: "hole" | "boss" | "round" | "plane" | "cone" | "sphere" | "face";
  object: string;
  /** Part ID (B#) of the object the feature is on. */
  part?: string;
  /** Free-form faces: the surface type FreeCAD reports. */
  surface?: string;
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
  /** Hole sitting in a counterbore: the counterbore's size and feature ID. */
  counterbore?: { diameter: number; depth: number; feature?: string };
  /** This hole is the counterbore of the given hole. */
  counterbore_of?: string;
  /** ID of the hole pattern this hole belongs to. */
  pattern?: string;
  [key: string]: unknown;
}

export interface Pattern {
  id: string;
  type: "circular" | "grid" | "linear";
  count: number;
  hole_diameter: number;
  holes: string[];
  object: string;
  axis: Vec3;
  // circular
  pcd?: number;
  center?: Vec3;
  angle_step_deg?: number;
  start_angle_deg?: number;
  full_circle?: boolean;
  // linear
  spacing?: number;
  direction?: Vec3;
  start?: Vec3;
  // grid
  spacing_u?: number;
  spacing_v?: number;
  count_u?: number;
  count_v?: number;
}

export interface Dimension {
  /** Two feature IDs, e.g. "P1-P2" or "H3-P5". */
  id: string;
  type: "extent" | "thickness" | "gap" | "edge_distance";
  value: number;
  refs: [string, string];
  axis: Vec3;
  object: string;
  label: string;
}

export interface HighlightMesh {
  id: string;
  kind: string;
  /** Flat xyz in global mm (Z-up, same frame as the STL). */
  positions: number[];
  indices: number[];
}

export interface PickResult {
  /** The feature (or, in part mode, the part) under the clicked point; null when nothing is there. */
  id: string | null;
  feature: string | null;
  part: string | null;
  face?: number;
  distance?: number;
}

export type SelectMode = "feature" | "part";

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
  patterns?: Pattern[];
  dimensions?: Dimension[];
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
