# DataMesh Warden -- demo script

A ~4-minute walkthrough of the incident war room, plus prep/troubleshooting
notes. Pairs with the architecture in [`architecture.md`](architecture.md)
(see section 5 for the UI layout and the Phase 6 note for the deployment
model this assumes).

## Before you start

Pick one of two setups:

- **Deployed (Cloud Run)** -- run `make deploy` (or `./scripts/deploy.ps1`)
  once ahead of time, then just open the printed `warden-ui` URL. Nothing
  to run locally; the audience can also open the URL themselves.
- **Local** -- `make run-api` + `make run-ui` in two terminals. Works with
  either `WARDEN_MODE=local` (no GCP, canned heuristics) or
  `WARDEN_MODE=cloud` (real Gemini/Firestore/BigQuery -- see the GCP setup
  walkthrough in `architecture.md`) depending on how much "this is really
  live" you want to show.

Either way, confirm before the audience shows up:

1. `GET /status` returns `{"status": "ok"}`.
2. The UI loads and the sidebar shows all three presets.
3. If demoing cloud mode: fire one throwaway incident yourself first, so
   Vertex AI/Firestore/BigQuery have "warmed up" (first real call of the
   day is sometimes a few seconds slower) and you've confirmed credentials
   are still valid.

## The 4-minute script

| Time | Action | Narration |
|---|---|---|
| 0:00-0:20 | Open the dashboard, let the dark theme land | "This is DataMesh Warden -- an autonomous agent fleet that triages, patches, and governs data incidents, with a human approving every write." |
| 0:20-0:40 | Click the **Schema drift** preset in the sidebar | "One click simulates a real alert -- in production this would come from a BigQuery audit log or a Cloud SQL insights alert." |
| 0:40-1:30 | Timeline tab streams in; switch to the Diagnosis tab as it populates | "The orchestrator (Gemini) is running a multi-turn loop, calling out to a log-triage sub-agent to form a hypothesis -- here, a dropped `email` column." |
| 1:30-2:30 | Patch Diff tab: show the sandbox vs. production SQL, before/after schema, sandbox stats | "A second sub-agent writes a DDL patch and actually runs it in an isolated sandbox clone of the table first -- nothing touches production yet." |
| 2:30-3:15 | Governance tab: point out the verdict and policy checks | "A third sub-agent audits the patch against org policy -- PII drop guards, data steward bindings, blast-radius checks -- and explains its verdict in plain English." |
| 3:15-3:45 | Click **Approve & execute** | "A human is always in the loop for the actual write. One click, and the approved patch runs for real." |
| 3:45-4:00 | Incident flips to Resolved; optionally show the Firestore console's incident tree | "Every step -- model turns, tool calls, the human decision -- is logged as a full audit trail." |

## Resetting between demo runs

**The scenarios are not idempotent in `WARDEN_MODE=cloud`.** "Schema
drift"'s whole premise is "the `email` column was dropped from
`orders`" -- approving its patch really does re-add that column via
`ALTER TABLE`. Replaying the exact same demo afterwards fails with a
genuine (not a bug) `Column already exists: email` error, since the
column really is there now. Before replaying "Schema drift", reset it:

```powershell
make reset-demo-data
# or: ./scripts/reset-demo-data.ps1 -ProjectId <your-project>
```

"Data quality anomaly" and "Broken pipeline job" don't mutate schema, so
they're safe to replay as-is.

## If something breaks mid-demo

- **Incident stuck in `DIAGNOSING`/`PATCHING` for a while**: real Gemini
  calls can occasionally take 20-60s end to end across three sub-agent
  turns; the UI auto-refreshes every ~1.5s so just keep talking. If it's
  been over ~90s, check the API logs for a stack trace.
- **Governance shows `BLOCK`**: that's not a bug -- it means a policy
  check genuinely failed (e.g. a PII-tagged column drop). Use it as a
  teaching moment: the Approve button is disabled on purpose.
- **API unreachable from the UI (Cloud Run)**: confirm the UI's service
  account still has `roles/run.invoker` on the API service (see
  `scripts/deploy.ps1`); this is the most common cause of a private API +
  public UI setup silently failing after a redeploy that recreates
  service accounts.
- **Fallback**: `WARDEN_MODE=local` always works offline with canned
  heuristics if live GCP/Gemini access is flaky right before a demo --
  the UI/timeline/tabs look identical either way.

## Appendix: firing an incident from the command line

Useful for a pre-demo warm-up run, or if the UI isn't available:

```powershell
curl -X POST <API_URL>/events/ingest `
  -H "Content-Type: application/json" `
  -d '{"source":"manual_demo","resource_uri":"bq://<project>.sales.orders","severity":"P1","raw_event":{"scenario":"schema_drift","table":"orders","dropped_column":"email"}}'
```

Against a private (Cloud Run) API, add an identity token:

```powershell
$token = gcloud auth print-identity-token
curl -X POST <API_URL>/events/ingest -H "Authorization: Bearer $token" -H "Content-Type: application/json" -d '...'
```
