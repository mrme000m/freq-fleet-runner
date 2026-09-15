# Containment report — LLM + PocketBase vendoring (port 8290)

## Copied (verified vs grid-autonomy sources)
- grid/pbclient.py, grid/llm/provider.py, grid/.pocketbase/{pocketbase,pb.env(600),
  pb_data/,pb_hooks/,pb_migrations/} — byte-identical (sha256/diff -r); journal
  rows = 21482; auxiliary.db intentionally absent (PB recreates it)

## Modified
- grid/.pocketbase/pb.env — ONLY PB_URL: 8090 → http://127.0.0.1:8290
- grid/dev — pb lifecycle (_pb_is_ours/pb_running/pb_start/pb_stop/cmd_pb), `pb
  [start|stop|status]`, pb BEFORE console in start/restart, stops in stop (--keep-pb),
  `logs pb` + status line. serve --dir pb_data --http 127.0.0.1:8290; pidfile
  state/pids/pb.pid; log state/logs/pb.log; /api/health check; foreign-listener guard.
  console_start exports PB_URL=...:8290 (server.py seeds setdefault → explicit wins).
- .gitignore — added grid/.pocketbase/; grid/llm/ = pure code, no secrets → committable

## Not modified
- server.py, static/app.js, console-dataflow-audit.md, grid-autonomy (read-only).

## Verification
- grid/dev pb → READY; :8290/api/health → 200; stop/start cycle clean; left RUNNING
- Superuser auth via pb.env (creds never printed): journal 21482, recs 26, decisions 28,
  reliability 7, slots 15, bots 14 — all match
- /usr/bin/python3 -m pytest grid/tests -q → 76 passed (baseline)
- grid/dev status → pocketbase RUNNING · :8290 · health ok; pb left RUNNING; engine untouched
## Deviation
- Console :8798 (pid 21927, pre-wiring, no PB_URL env) restarted → pid 51794 with
  PB_URL=...:8290 (ps eww verified); the only way the wiring takes effect.
- At 05:25 an EXTERNAL full stop (not this worker: engine SIGINT + pidfile wipe)
  took console+engine+pb down; pb re-started per this task.
