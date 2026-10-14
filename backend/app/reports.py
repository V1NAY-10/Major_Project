"""
Report exports (pure Python): bill of materials as rows, CSV and XLSX (written directly, no extra dependency).
"""

import csv
import io
import zipfile
from xml.sax.saxutils import escape

BOM_COLUMNS = ["Item", "Part IDs", "Description", "Quantity", "Material", "Unit mass (g)", "Total mass (g)",
               "Size (mm)"]


def bom_rows(groups, materials, masses):
    """groups: engine bom_groups; materials: {part ID: material name}; masses: {part ID: g}. One row per group."""
    rows = []
    for i, g in enumerate(sorted(groups, key=lambda g: (-g["volume"], g["labels"][0])), 1):
        pid = g["parts"][0]
        mats = {materials.get(p) for p in g["parts"]}
        unit = masses.get(pid)
        rows.append({"item": i, "parts": g["parts"], "description": _common_label(g["labels"]), "quantity": g["quantity"],
                     "material": " / ".join(sorted(m for m in mats if m)) or "—",
                     "unit_mass_g": None if unit is None else round(unit, 3),
                     "total_mass_g": None if unit is None else round(unit * g["quantity"], 3),
                     "size": " × ".join(f"{x:g}" for x in g["size"])})
    return rows


def _common_label(labels):
    """'LEG-1', 'LEG-2' → 'LEG'; a single label stays as it is."""
    if len(labels) == 1:
        return labels[0]
    p = labels[0]
    for l in labels[1:]:
        while p and not l.startswith(p):
            p = p[:-1]
    p = p.rstrip(" -_.0123456789")
    return p or labels[0]


def _values(r):
    return [r["item"], " ".join(r["parts"]), r["description"], r["quantity"], r["material"],
            "" if r["unit_mass_g"] is None else r["unit_mass_g"], "" if r["total_mass_g"] is None else r["total_mass_g"],
            r["size"]]


def bom_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(BOM_COLUMNS)
    for r in rows:
        w.writerow(_values(r))
    return buf.getvalue().encode("utf-8-sig")


def _col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx(sheet_name, header, rows):
    """A minimal valid .xlsx workbook with one sheet (inline strings, bold header)."""
    def cell(ref, v, style=0):
        st = f' s="{style}"' if style else ""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return f'<c r="{ref}"{st}><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"{st}><is><t>{escape(str(v))}</t></is></c>'
    lines = []
    for ri, row in enumerate([header] + rows, 1):
        cells = "".join(cell(f"{_col(ci)}{ri}", v, 1 if ri == 1 else 0) for ci, v in enumerate(row))
        lines.append(f'<row r="{ri}">{cells}</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<sheetData>{"".join(lines)}</sheetData></worksheet>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name[:31])}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>',
        "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
        '<cellXfs count="2"><xf/><xf fontId="1" applyFont="1"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": sheet,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def bom_xlsx(rows):
    return xlsx("BOM", BOM_COLUMNS, [_values(r) for r in rows])
