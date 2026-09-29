#!/bin/sh
# One command for judges: clean rebuild, acceptance checker, isolation probes.
# Usage: make verify   (or: sh scripts/verify.sh)
set -eu
BASE=${BASE:-http://localhost:8080}
ORG="Cookie: session=org_7f2a"; JA="Cookie: session=jdg_a_91bc"; JB="Cookie: session=jdg_b_44de"; PRT="Cookie: session=prt_2e88"

echo "== 1. clean rebuild (wipes the demo database volume)"
docker compose down -v >/dev/null 2>&1 || true
docker compose up --build -d
printf "waiting for %s/healthz " "$BASE"
i=0; until curl -sf -o /dev/null "$BASE/healthz"; do i=$((i+1)); [ $i -gt 90 ] && { echo "portal did not start"; docker compose logs --tail 40; exit 1; }; printf .; sleep 1; done; echo " up"

echo; echo "== 2. acceptance checker (run.py)"
python3 run.py .dogfood.toml | tee acceptance-report.txt

echo; echo "== 3. role isolation probes (expected -> got)"
fail=0
probe() { # expected method path header
  got=$(curl -s -o /dev/null -w "%{http_code}" -X "$2" ${4:+-H "$4"} -H "Content-Type: application/json" ${5:+-d "$5"} "$BASE$3")
  if [ "$got" = "$1" ]; then r=ok; else r=FAIL; fail=1; fi
  printf "  %-4s %-6s %-48s %-12s %s -> %s\n" "$r" "$2" "$3" "${6:-}" "$1" "$got"
}
probe 403 GET  "/api/judge/scores?judge=jdg_01" "$JB" "" "judge_b"
probe 403 GET  "/api/judge/scores?judge=no_such_judge" "$JA" "" "judge_a"
probe 200 GET  "/api/judge/scores" "$JA" "" "judge_a"
probe 403 GET  "/api/judge/scores" "$PRT" "" "participant"
probe 401 GET  "/api/judge/scores" "" "" "anonymous"
probe 403 GET  "/api/export.csv" "$JA" "" "judge_a"
probe 403 GET  "/api/events/evt_01/results.csv" "$JA" "" "judge_a"
probe 403 GET  "/api/events/evt_01/progress" "$PRT" "" "participant"
probe 403 GET  "/events/evt_01/results" "$JB" "" "judge_b"
probe 403 GET  "/events/evt_01/dashboard" "$JA" "" "judge_a"
probe 403 POST "/api/projects" "$PRT" '{"title":"late"}' "participant"
probe 200 GET  "/events/evt_01/results" "$ORG" "" "organizer"

echo
if [ -d .venv ]; then echo "== 4. unit and integration tests"; .venv/bin/pytest -q | tail -1
else echo "== 4. tests: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest"; fi
[ $fail -eq 0 ] && echo "verify: all probes passed" || { echo "verify: probe failures above"; exit 1; }
