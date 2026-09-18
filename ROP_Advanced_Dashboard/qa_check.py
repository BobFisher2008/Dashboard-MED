"""Star-schema integrity report (writes QA_REPORT.json). Same as `python warehouse.py check`."""
import json

from warehouse import BASE_DIR, connect, integrity_checks

con = connect()
checks = integrity_checks(con)
con.close()
(BASE_DIR / "QA_REPORT.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(json.dumps(checks, ensure_ascii=False, indent=2, default=str))
raise SystemExit(0 if checks["all_ok"] else 1)
