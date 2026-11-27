"""One-shot SAFE STATE resume requested through git (CI has no shell for the operator).

Put a new id on the first line of ops/resume.txt and push; the next Actions run clears the SAFE STATE once
and records the id, so the same request is never applied twice. Paper ledger only.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.core.safety import SafeState  # noqa: E402
from app.database.db import Database  # noqa: E402


def main() -> int:
    f = ROOT / "ops" / "resume.txt"
    if not f.exists():
        return 0
    rid = (f.read_text().strip().splitlines() or [""])[0].strip()
    s = load_settings()
    if not rid or not Path(s.db_path).exists():
        return 0
    db = Database(s.db_path)
    try:
        if db.get_state("resume_request_applied") == rid:
            print(f"resume request {rid}: already applied")
            return 0
        safe = SafeState(db)
        st = safe.status()
        if st.halted:
            print(f"resume request {rid}: clearing SAFE STATE ({st.reason})")
            safe.resume("github", f"resume request {rid}")
        else:
            print(f"resume request {rid}: not halted, nothing to do")
        db.set_state("resume_request_applied", rid)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
