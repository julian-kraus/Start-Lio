import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Tests run only against a local throwaway Postgres. A managed or pooled URL (Supabase, a connection pooler)
# would mean a test is about to hit shared infrastructure, so refuse to collect at all.
_FORBIDDEN = ("supabase", "pooler")
for _var in ("TEST_DATABASE_URL", "DATABASE_URL"):
    _url = os.environ.get(_var, "").lower()
    if any(bad in _url for bad in _FORBIDDEN):
        raise RuntimeError(
            f"{_var} points at a managed or pooled database; tests run only against a local throwaway Postgres")


def make_pdf(lines: list[str]) -> bytes:
    """Minimal one-page PDF with a text layer (no lines = page without text, like a scan)."""
    ops = " ".join(f"({line}) Tj 0 -16 Td" for line in lines)
    content = f"BT /F1 12 Tf 72 720 Td {ops} ET" if lines else ""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out
