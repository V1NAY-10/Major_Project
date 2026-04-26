"""
End-to-end test: creates a STEP file, uploads it to /cad/upload,
and validates the response uses pythonOCC parser only.
"""
import cadquery as cq
import requests
import json
import sys

# Create a box with a hole
part = cq.Workplane("XY").box(100, 50, 20).faces(">Z").workplane().hole(10)
cq.exporters.export(part, "test_upload.step")

# Upload to running server
url = "http://127.0.0.1:8000/cad/upload"
with open("test_upload.step", "rb") as f:
    resp = requests.post(
        url,
        files={"file": ("test_upload.step", f, "application/octet-stream")},
        data={"session_id": "test-session-001"},
    )

print(f"Status: {resp.status_code}")
data = resp.json()
print(json.dumps(data, indent=2))

# Validate
parsed = data.get("parsed_data", {})
errors = []

if "legacy_text_scan" in json.dumps(parsed):
    errors.append("FAIL: legacy_text_scan found in response")

if parsed.get("parser") != "pythonOCC":
    errors.append(f"FAIL: parser field is '{parsed.get('parser')}', expected 'pythonOCC'")

summary = parsed.get("summary", {})
if summary.get("solids", 0) <= 0:
    errors.append(f"FAIL: solids={summary.get('solids')}, expected > 0")
if summary.get("faces", 0) <= 0:
    errors.append(f"FAIL: faces={summary.get('faces')}, expected > 0")

volume = parsed.get("physical_properties", {}).get("volume", 0)
if volume <= 0:
    errors.append(f"FAIL: volume={volume}, expected > 0")

# Check no raw STEP entities
raw_entities = ["ADVANCED_FACE", "CLOSED_SHELL", "MANIFOLD_SOLID_BREP", "AXIS2_PLACEMENT"]
response_str = json.dumps(parsed)
for ent in raw_entities:
    if ent in response_str:
        errors.append(f"FAIL: raw STEP entity '{ent}' found in response")

if errors:
    print("\n".join(errors))
    sys.exit(1)
else:
    print("\n✅ ALL VALIDATIONS PASSED")
    print(f"   parser:  {parsed.get('parser')}")
    print(f"   solids:  {summary.get('solids')}")
    print(f"   faces:   {summary.get('faces')}")
    print(f"   edges:   {summary.get('edges')}")
    print(f"   volume:  {volume}")
    print(f"   features: {[f['type'] for f in parsed.get('features', [])]}")
