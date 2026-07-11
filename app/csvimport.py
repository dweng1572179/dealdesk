"""One robust CSV reader for every importer (lenders, comps, rates, deals, contacts).
Folds together the row-normalization those importers each copy-pasted, and hardens the
two ways csv crashes on real-world files:

  • a single cell over csv's 131072-char default limit -> _csv.Error (raise the ceiling)
  • malformed bytes mid-file (a stray NUL, a bare CR) -> _csv.Error (stop, don't 500)

Every importer decodes utf-8-sig (Excel BOM), lowercases + trims header keys, drops
DictReader's restkey bucket (the LIST of surplus cells on an over-wide row, which would
crash .strip()), and trims string values."""
import csv
import io

# A generous cell ceiling — a pasted "notes" blob or a base64 cell can exceed 128 KB.
# ponytail: a flat 4 MB per field, not streaming — importers already buffer the whole
# upload; a CSV cell bigger than this is a malformed file, not data.
_FIELD_LIMIT = 4 * 1024 * 1024


def rows(raw: bytes):
    """Yield each CSV row as a {lowercased-header: trimmed-value} dict. Robust to
    oversized cells and malformed content — a parse error ends iteration cleanly rather
    than 500-ing the request. Rows that are entirely blank are skipped."""
    try:
        csv.field_size_limit(_FIELD_LIMIT)
    except (OverflowError, ValueError):  # pragma: no cover - platform-dependent cap
        pass
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
    try:
        for row in reader:
            norm = {(k or "").strip().lower(): (v if isinstance(v, str) else "").strip()
                    for k, v in row.items() if k is not None}
            if any(norm.values()):
                yield norm
    except csv.Error:
        return  # a malformed row aborts the rest; the rows before it still imported


def demo() -> None:
    got = list(rows(b"Name,Notes\nAcme, hi \n\n , \nBeta,x\n"))
    assert [r["name"] for r in got] == ["Acme", "Beta"], got   # blank row dropped, values kept
    assert list(got[0]) == ["name", "notes"], got[0]           # header keys lowercased
    assert got[0]["notes"] == "hi", got[0]                     # values trimmed

    # an over-wide row: extra cells land in the restkey LIST, which must be dropped
    # (not .strip()'d) — and the named columns still parse.
    wide = list(rows(b"a,b\n1,2,3,4\n"))
    assert wide == [{"a": "1", "b": "2"}], wide

    # a cell far larger than csv's 131072 default must not raise
    big = b"name,notes\nAcme," + b"x" * 200_000 + b"\n"
    out = list(rows(big))
    assert out and out[0]["name"] == "Acme" and len(out[0]["notes"]) == 200_000

    # a NUL byte mid-file raises _csv.Error internally; we stop cleanly, keeping the
    # good rows parsed before it rather than 500-ing.
    bad = b"name\nGood\nBa\x00d\nAfter\n"
    res = list(rows(bad))
    assert res and res[0]["name"] == "Good", res
    print("csvimport.demo OK")


if __name__ == "__main__":
    demo()
