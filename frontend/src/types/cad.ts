export interface Topology {
  solids: number;
  faces: number;
  edges: number;
}

export interface BoundingBox {
  xmin: number;
  ymin: number;
  zmin: number;
  xmax: number;
  ymax: number;
  zmax: number;
  length: number;
  width: number;
  height: number;
}

export interface Component {
  id: string;
  type: string;
  role?: string;
  count: number;
  editable: boolean;
  radius?: number;
  diameter?: number;
  height?: number;
  center?: [number, number, number];
  axis?: [number, number, number];
  semantic_label?: string[];
  [key: string]: any;
}

export interface Relationship {
  source: string;
  relation: string;
  target: string;
}

export interface ParsedData {
  filename: string;
  format: string;
  summary: {
    topology?: Topology;
    bounding_box?: BoundingBox;
    volume?: number;
    component_count?: number;
    total_feature_instances?: number;
  };
  components: Component[];
  relationships: Relationship[];
}

export interface CADState {
  fileId: string | null;
  parsedData: ParsedData | null;
  modelUrl: string | null;
  fileName: string | null;
  isUploading: boolean;
}
