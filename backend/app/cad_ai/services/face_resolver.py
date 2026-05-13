import re

class FaceResolver:
    """Resolves user references to specific faces."""
    
    def resolve(self, prompt: str, components: list) -> list[dict]:
        """
        Returns list of {face_id, assembly_label, component_id, confidence}
        
        Handles:
        - "face 14" → direct face_id lookup
        - "the top of the nosecone" → semantic label match
        - "the flat part near the nozzle" → spatial + type match
        """
        references = []
        
        # 1. Direct face number reference
        face_num = self._extract_face_number(prompt)
        if face_num is not None:
            face = self._find_face_by_id(face_num, components)
            if face:
                references.append({
                    "face_id": face_num,
                    "assembly_label": face.get("assembly_label", f"face_{face_num}"),
                    "component_id": face.get("id"),
                    "confidence": 1.0,
                    "match_type": "direct_id"
                })
        
        # 2. Assembly label match ("top cap of nosecone")
        label_matches = self._fuzzy_label_match(prompt, components)
        references.extend(label_matches)
        
        return sorted(references, key=lambda x: x["confidence"], reverse=True)
    
    def _extract_face_number(self, prompt: str) -> int | None:
        m = re.search(r'\bface[_\s#]?(\d+)\b', prompt, re.IGNORECASE)
        return int(m.group(1)) if m else None

    def _find_face_by_id(self, face_num: int, components: list) -> dict | None:
        for comp in components:
            if comp.get("face_id") == face_num:
                return comp
        return None

    def _fuzzy_label_match(self, prompt: str, components: list) -> list[dict]:
        # Simple keyword matching for assembly labels
        matches = []
        prompt_lower = prompt.lower()
        for comp in components:
            label = comp.get("assembly_label", "").lower()
            if not label:
                continue
            # Break label into words
            words = label.replace("_", " ").split()
            match_count = sum(1 for w in words if w in prompt_lower)
            if match_count > 0:
                confidence = match_count / len(words)
                matches.append({
                    "face_id": comp.get("face_id"),
                    "assembly_label": comp.get("assembly_label"),
                    "component_id": comp.get("id"),
                    "confidence": confidence,
                    "match_type": "fuzzy_label"
                })
        return matches
