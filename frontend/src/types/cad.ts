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
  kind: "hole" | "boss" | "round" | "plane" | "cone" | "sphere" | "face" | "slot" | "pocket" | "chamfer";
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
  /** Best-matching standard size, e.g. "M6 clearance (normal)"; all matches in `standards`. */
  standard?: string;
  standards?: string[];
  /** Holes / bosses: distance (mm) from each datum face, measured into the material. */
  datum_position?: Partial<Record<DatumLetter, number>>;
  /** Thread annotation on a hole (session note or a threaded hole feature of an FCStd file). */
  thread?: { spec: string; depth?: number | null; source?: string; note?: string };
  /** Slots / pockets. */
  shape?: "obround" | "rectangular";
  width?: number;
  corner_radius?: number;
  /** Slot / pocket: the face it opens on. */
  face?: string;
  /** Chamfer: equal size (null when the two distances differ), both distances, angle to the first face. */
  size?: number | null;
  distances?: [number, number];
  angle_deg?: number;
  faces?: string[];
  /** Fillet chains: number of tangent faces in the chain. */
  chain_faces?: number;
  toroidal?: boolean;
  [key: string]: unknown;
}

/** An edge (E#) of a part: picked in the viewer or listed by find_edges. */
export interface EdgeInfo {
  id: string;
  kind: string;
  length: number;
  mid: Vec3;
  ends: Vec3[];
  /** IDs of the features whose faces meet at the edge. */
  faces: string[];
  convexity: "convex" | "concave" | "tangent" | "seam" | "free";
  part?: string;
}

export interface WallThickness {
  part: string;
  object: string;
  label: string;
  min_thickness: number;
  at: [Vec3, Vec3];
  faces: (string | null)[];
  thin: { face: string | null; thickness: number; at: Vec3 }[];
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

export type DimensionType =
  | "extent"
  | "thickness"
  | "gap"
  | "edge_distance"
  | "center_distance"
  | "angle"
  | "diameter"
  | "radius"
  | "coaxial";

export interface Dimension {
  /** Feature IDs: "P1-P2", "H3-P5", "H1-H2", angle "P3∠P7", coaxiality "H1◎H2", or radial "C1.Ø" / "R2.R". */
  id: string;
  type: DimensionType;
  /** mm, or degrees when unit is "deg". */
  value: number;
  unit?: "deg";
  refs: string[];
  axis?: Vec3;
  object: string;
  label: string;
}

export type DatumLetter = "A" | "B" | "C";

export interface DatumRef {
  id: string;
  label: string;
  normal: Vec3;
  part?: string;
  /** Chosen by the user (otherwise a default). */
  explicit: boolean;
}

export interface SessionSettings {
  display_unit: "mm" | "in";
  /** Decimals shown for mm (inch gets one more). */
  precision: number;
  /** Acceptance tolerance of edits: mm and degrees. */
  tolerance: { length: number; angle: number };
  /** Pre-edit check: warn when an edit makes a wall thinner than this (mm, 0 = off). */
  min_wall?: number;
}

export interface Lock {
  target: string;
  mode: "value" | "centered";
  axis?: "X" | "Y" | "Z";
  value: number | Record<string, number> | null;
  unit?: "mm" | "deg" | null;
  label: string;
  created_at?: string;
}

export interface HighlightMesh {
  id: string;
  kind: string;
  /** Flat xyz in global mm (Z-up, same frame as the STL). */
  positions: number[];
  indices: number[];
  /** Edges: line segments as flat xyz pairs. */
  lines?: number[];
  edge?: EdgeInfo;
}

export interface PickResult {
  /** The feature (or, in part mode, the part) under the clicked point; null when nothing is there. */
  id: string | null;
  feature: string | null;
  part: string | null;
  face?: number;
  distance?: number;
  edge?: EdgeInfo;
}

export type SelectMode = "feature" | "part" | "edge";

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
  datums?: Partial<Record<DatumLetter, DatumRef>>;
  /** Threads read from threaded hole features of an FCStd file. */
  threads?: { hole: string; spec: string; depth: number; source: string }[];
  parametric: ParametricObject[];
  sketches: SketchInfo[];
  spreadsheets: { name: string; label: string; cells: { cell: string; alias: string; value: unknown }[] }[];
  is_parametric: boolean;
}

export interface VersionEntry {
  v: number;
  description: string;
  created_at: string;
  /** Version this one was made from (null for the upload): versions form a tree. */
  parent?: number | null;
  name?: string;
}

/** A previewed edit / plan waiting for approval. */
export interface PendingPlan {
  id: string;
  summary: string;
  steps: { tool: string; args: Record<string, unknown>; label: string }[];
  records: ({ tool: string; ok: boolean; message?: string; error?: string; verified?: unknown[]; warnings?: string[] } | null)[];
  ok: boolean;
  error?: string | null;
  base_version: number;
  mesh_url: string | null;
}

/** One alternative for an open-ended request, dry-run like a plan (not possible when the dry run failed). */
export interface DesignOption {
  key: number;
  title: string;
  description: string;
  steps: { tool: string; args: Record<string, unknown>; label: string }[];
  records: PendingPlan["records"];
  ok: boolean;
  error?: string | null;
  mesh_url: string | null;
}

export interface OptionSet {
  id: string;
  question: string;
  options: DesignOption[];
  base_version: number;
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
  settings?: SessionSettings;
  locks?: Lock[];
  /** Datum faces chosen explicitly by the user (letter → plane ID). */
  datums?: Partial<Record<DatumLetter, string>>;
  /** Part ID → material key. */
  materials?: Record<string, string>;
  pending?: PendingPlan | null;
  /** Alternatives waiting for the user's pick (only while the model is still at their base version). */
  options?: { id: string; base_version: number } | null;
}

export interface FreeCADStatus {
  connected: boolean;
  gui?: boolean;
  version?: string;
  error?: string;
}

// ── Phase 3 / 4 analysis results ─────────────────────────────────────────

export interface DiffMesh {
  kind: "added" | "removed";
  object: string;
  positions: number[];
  indices: number[];
}

export interface CompareResult {
  a: VersionEntry;
  b: VersionEntry;
  dimensions: { rows: { key: string; label: string; before: number | null; after: number | null; delta: number | null; kind: string }[]; total: number } | null;
  geometry: { parts: { object: string; label: string; added_volume: number; removed_volume: number; status: string }[]; meshes: DiffMesh[] };
}

export interface PartMesh {
  part: string;
  label: string;
  center: Vec3;
  positions: number[];
  indices: number[];
}

export interface MaterialInfo {
  key: string;
  name: string;
  density: number;
  price_per_kg: number;
  category: string;
}

export interface MassRow {
  part: string;
  label: string;
  volume: number;
  area: number;
  center_of_mass: Vec3;
  material?: string | null;
  density?: number;
  mass_g?: number;
  principal_moments_kg_mm2?: number[];
  inertia_kg_mm2?: number[][];
}

export interface DfmFinding {
  severity: "error" | "warning" | "info";
  rule: string;
  message: string;
  features: string[];
  value?: number;
  limit?: number;
  fix?: { tool: string; args: Record<string, unknown>; label: string };
}

export interface BomRow {
  item: number;
  parts: string[];
  description: string;
  quantity: number;
  material: string;
  unit_mass_g: number | null;
  total_mass_g: number | null;
  size: string;
}
