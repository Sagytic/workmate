# Workmate v0.4 implementation contract

This describes the implemented local application, not a roadmap. Korean UI, a single local user, synthetic fixtures, actual tool execution and persisted task outcomes. Never turn an unverified scope, unavailable browser, or external model error into a fabricated success.

## Runtime and persistence

- FastAPI at `127.0.0.1:8765`; production serves `frontend/dist`. Vite development runs at `127.0.0.1:5173` and proxies `/api`.
- SQLite defaults to `data/workmate.db`. `WORKMATE_DATA_DIR` changes the DB directory; upload and artifact paths remain under the project `data/` directory.
- Persisted collections: tasks, messages, memories, apps, records, workflows, notifications, uploads, documents, projects, examples, meta.
- Artifacts: `data/artifacts/{task_id}/attempt-{NN}/`; repair attempts use a separate child directory. Artifact names are relative to the task directory and retain nested paths.
- Timestamps use Korea Standard Time. The in-process scheduler checks about every 15 seconds. It requires a running app server, does not backfill every missed interval, and is not an OS/background service.
- On restart, interrupted queued/running tasks are marked failed. Completed state and files persist. No automatic retention/deletion policy is implemented.
- Bind locally. Host/origin restrictions are not multi-user authentication. There is no company mail/calendar/ERP/financial-system integration.

## Shared entities

```text
Task {
  id, title, agent, status, created_at, updated_at, due_at,
  priority: low|normal|high, archived, mode: demo|live,
  input, result, summary, attempt, parent_id?,
  events: [{at,message}], artifacts: [{id,name,url,kind}]
}
agent = assistant|excel|builder|developer|automation|knowledge|analyst|reporter
status = todo|in_progress|queued|running|waiting_input|completed|failed

Memory {id,key,value,source,updated_at}
App {id,name,description,schema,created_at,version,url,record_count?}
Record {id,app_id,...schema_field_values,created_at,updated_at?}
Workflow {
  id,name,description,steps:[{tool,label}],enabled,
  trigger:manual|upload|interval,interval_minutes,baseline,current,app_id?,
  next_run_at,last_run_at,created_at
}
Message {id,role,content,created_at,task_id?}
```

Internal App storage includes `source_path`; public app data omits it. The actual view is `/api/apps/{id}/view`. Task completion describes the tool run, not universal business validity. Excel findings and unsupported coverage must be read from `result`; manual task completion is explicitly a user confirmation.

A resume preserves the task ID, earlier events and artifacts and increments its execution attempt. Retry creates a new task with `parent_id`, old inputs/deadline and priority. Running/queued tasks and manual personal tasks cannot be retried. Running/queued tasks cannot be archived.

## API

All paths below begin with `/api`. Validation errors use `detail` (a string for domain errors; FastAPI validation can return structured details). Data IDs must resolve to stored objects. Client-provided file paths are not accepted as uploaded-file IDs.

| Method and path | Input / output |
| --- | --- |
| GET `/health` | `{ok,mode,llm_configured,version}` |
| GET `/state` | `{mode,llm_configured,llm,agents,tasks,archived_tasks,chat_history,uploads,memories,apps,workflows,notifications,fixtures,stats,server_time}` |
| POST `/connection/test` | Small real model request; returns public connection diagnostics |
| POST `/chat` | `{message}` → `{reply,mode,history,task_id?,memory?,workflow_id?}`; async tasks are polled |
| GET / DELETE `/chat/history` | Latest 100 messages, chronological / clear stored messages; clearing does not clear tasks or memories |
| POST `/tasks` | `{agent,title?,input?,due_at?}` → Task |
| GET / PATCH `/tasks/{id}` | Read / edit `{title?,due_at?,priority?,archived?}` |
| POST `/tasks/{id}/resume` | `{action:repair|continue,input?:{...}}` → same Task |
| POST `/tasks/{id}/retry` | New Task linked to original |
| GET `/tasks/{id}/bundle` | ZIP with `task.json` and registered artifacts; total artifact size <=100 MiB |
| POST `/memories` | `{key,value}` → Memory; same key updates existing memory |
| DELETE `/memories/{id}` | `{ok:true}` |
| POST `/uploads` | Multipart `file` → upload metadata |
| GET `/uploads/{id}` | Original uploaded XLSX download |
| GET `/fixtures/{id}` | Synthetic XLSX; IDs `baseline|changed|clean|reordered` |
| POST `/apps` | `{prompt}` → builder Task |
| GET `/apps/{id}` | Public App with record count |
| GET `/apps/{id}/view` | Actual generated HTML, no-store |
| GET / POST `/apps/{id}/records` | List records / create from allowed schema fields |
| PATCH / DELETE `/apps/{id}/records/{record_id}` | Merge and validate / delete; record must belong to that app |
| GET `/apps/{id}/export.csv` | Schema-ordered records, UTF-8 BOM CSV |
| POST `/workflows` | `{name,prompt?,app_id?,trigger?,interval_minutes?,baseline?,current?}` → Workflow |
| PATCH `/workflows/{id}` | Edit same configuration plus `enabled` |
| DELETE `/workflows/{id}` | Delete definition; preserve previous runs; reject while queued/running |
| POST `/workflows/{id}/run` | Optional `{baseline,current,app_id}` overrides → Task; disabled workflow rejects execution |
| POST `/notifications/check` | Check actual due times → `{created}` |
| POST `/notifications/{id}/read` | Mark read |
| GET `/artifacts/{task_id}/{filename:path}` | Only registered files inside that task's artifact directory |

`stats` contains completed/active/waiting/artifacts for non-archived tasks. CSV exporters escape formula-like text and preserve numeric values. Artifact serving and ZIP export resolve paths inside the expected task directory; the ZIP preserves relative links between UAT HTML and images.

### Task inputs

```json
{"agent":"excel","input":{"baseline":"baseline","current":"changed"}}
{"agent":"excel","input":{"baseline":"baseline","currents":["changed","clean","reordered"]}}
{"agent":"builder","input":{"prompt":"고객 요청 관리 앱"}}
{"agent":"developer","input":{"app_id":"app_...","requirement":"완료 메모와 담당자 필수"}}
{"agent":"automation","input":{"workflow_id":"workflow_...","baseline":"baseline","current":"clean","app_id":"app_..."}}
{"agent":"assistant","input":{"manual":true,"message":"제안서 확인"}}
```

File inputs accept fixture IDs or stored upload IDs. Batch `currents` contains 1–10 files. Assistant manual tasks wait for user completion. Automation captures a `workflow_snapshot` in task input so a waiting run remains meaningful after definition changes/deletion.

## App schema and generation

Base fields always exist in order: `title` (text, required), `owner` (text), `due_date` (date), `status` (select, required; 대기/진행/완료), `completion_note` (textarea). Base types and status options are fixed. Optional custom fields follow, maximum 8, maximum 13 fields total.

```json
{
  "fields":[
    {"name":"budget","label":"예산","type":"number","required":true,"min":0,"max":1000000},
    {"name":"customer_email","label":"고객 이메일","type":"email","required":false},
    {"name":"category","label":"구분","type":"select","required":true,"options":["문의","변경"]}
  ],
  "require_completion_note":true,
  "prevent_past_due_date":false
}
```

The example shows a partial plan; normalized output also includes the five base fields. Field names match `[a-z][a-z0-9_]{0,31}`; reserved storage/prototype keys are rejected. Labels are at most 40 characters. Allowed types: `text|textarea|number|date|select|email|checkbox`. Select has 1–30 unique string options, each at most 80 characters. Numeric bounds must be finite and ordered. Malformed edits are rejected or reported in warnings without removing an existing valid field/constraint.

Accepted plan forms: `{name?,description?,fields?,require_completion_note?,prevent_past_due_date?}` or nested `{schema:{fields,...flags}}`. Partial fields merge into existing fields. Legacy `features` entries `completion_note_required|owner_required|due_date_required|prevent_past_due_date` remain accepted. A structured plan is authoritative: only its explicit fields/flags/features apply; request-text keyword fallback is used only when `plan=None` (demo). A live plan listing `unsupported` requirements is rejected.

Record validation is authoritative in the API: allowed schema fields, required values, finite numbers/bounds, dates, email shape, select options and boolean checkbox values. Required checkbox means checked/true. Completed records require a nonempty completion note when configured. Past-due checks use KST. Schema changes retain old records; a later record edit must meet the updated schema.

Generated code uses same-origin records/CSV endpoints, dynamic form/table fields, CRUD, text search and status filter. Treat model text and record values as data: escaped JSON literals and DOM `textContent`; no arbitrary model JavaScript, `eval`, or host code execution.

### Builder module

```text
create_app(prompt:str, app_id:str, output_dir:Path, plan:dict|None=None)
  -> {name,description,schema,source_filename,source_sha256,warnings,artifacts}

develop_app(app:dict, requirement:str, output_dir:Path,
            base_url:str='http://127.0.0.1:8765', plan:dict|None=None)
  -> {schema,summary,artifacts,tests,passed,source_filename,source_sha256,
      changes,warnings,review,test_scope,uat_approval}
```

`create_app` writes runnable `app.html` and schema artifacts. The orchestrator stores the source path and schema. `develop_app` writes proposed source, diff and deterministic template review; `builder_browser.cjs` tests that exact source in a real browser against an isolated local memory API. It captures real PNGs and verifies source SHA-256. The `base_url` parameter remains for compatibility; this isolated test does not assert production API/SQLite behavior.

Use project Playwright or configured/local runtime fallback. Browser order is Edge, Chrome, bundled Chromium, with optional `WORKMATE_BROWSER_CHANNEL`/`WORKMATE_PLAYWRIGHT_MODULE`. Missing browser/runtime, unsupported/no-op change, test failure or source-hash mismatch must not pass. Review is `deterministic-template-checks`, not independent LLM review.

UAT is fixed-template `uat-report.html`, with requirement, preconditions, expected/actual results, real screenshots, source hash, links to evidence and pending human acceptance. DOCX is not implemented. Production adoption requires `passed:true` and the app version unchanged since testing; otherwise no overwrite. Successful adoption increments app version and marks `applied:true`.

## Excel module

```text
inspect_workbook(baseline:Path, current:Path, output_dir:Path) -> Inspection
inspect_workbooks(baseline:Path, currents:list[Path], output_dir:Path) -> BatchInspection
repair_workbook(baseline:Path, current:Path, output_dir:Path) -> Repair
```

Inspection includes `summary,status,verification_status,findings,changes,counts,coverage,support_coverage,supported_schema,formula_scope,formula_scope_supported,limitations,baseline_sha256,current_sha256,repairable_count,artifacts,recalculated:false`. Finding has `id,rule,severity,sheet,cell,message,before,after,suggested,repairable`. Artifacts include HTML, JSON, findings CSV and changes CSV.

Batch compares each of 1–10 files independently with one baseline. It adds `batches,batch_counts`, per-file results/errors and aggregate coverage; failed files are unverified, not clean. There is no merge or batch automatic repair.

Upload gate: XLSX <=10 MiB, ZIP uncompressed <=50 MiB, <=2,000 entries, workbook present, no VBA project. Direct Excel module additionally caps its input expansion at 100 MiB and sheet-cell range at 200,000 total.

Matching requires the same sheet name and unique typed IDs. Full business rules require ID/title/owner/quantity/unit_price/total/status headers (accepted aliases are in `ALIASES`). Missing/duplicate/ambiguous keys or layouts narrow coverage and block automatic restoration. The supported static formula grammar is same-row relative-row references and bounded functions SUM/PRODUCT/ROUND family/ABS/MIN/MAX/AVERAGE/IF. Unsupported formulas remain unverified even if unchanged.

Data-validation definitions are compared; arbitrary validation formulas are not evaluated. The module does not recalculate Excel, verify cached-value freshness, resolve all circular references, or guarantee business correctness. Restoration copies only unambiguous baseline formulas into a separate workbook, logs and reinspects it, and requests recalculation on open. Reject rich/unsupported workbook features whose preservation is not guaranteed. Never overwrite originals or claim recalculation occurred.

## Automation and personal assistance

The supported workflow is fixed: `validate_excel` then `register_result`. It is not general browser-action teaching/replay or arbitrary tool orchestration.

- `manual`: explicit run with saved file inputs or overrides.
- `upload`: a successful Workmate XLSX upload triggers enabled workflows against their saved baseline.
- `interval`: 1–10,080 minutes; no overlapping scheduled run while that workflow has queued/running/waiting_input tasks.
- Any Excel finding or unsupported schema stops registration at `waiting_input`, `checkpoint:validation`. Continue with a replacement current file or request an eligible copy repair; never bypass the findings.
- If target app validation requires additional values, use `checkpoint:registration` and `registration:{app_id,fields,values,error}`. Resume with `input:{record_values:{...}}`; validate again.
- Register a real record, deduplicated by workflow ID + app ID + source-file hash, or write a receipt when no target app is selected. Receipt explicitly states `recalculated:false`.
- Disabling stops future workflow runs. Deleting a definition preserves snapshots/history and is blocked only while queued/running; waiting work can resume from its snapshot.

Personal task reminders check deadlines within two hours/overdue and deduplicate by task/deadline/phase. They are in-app notifications only. Memories upsert by key; conversations and actual task context ground briefings. No fabricated proactive action or implied mailbox access.

## LLM boundary and diagnostics

Configure server-only `WORKMATE_LLM_API_KEY`, `WORKMATE_LLM_BASE_URL`, `WORKMATE_LLM_MODEL`. All three are required for live mode; otherwise deterministic demo mode. Never expose keys in UI, diagnostics, logs, ZIPs or documentation.

The development environment has confirmed OpenRouter connectivity using `deepseek/deepseek-v4.1-flash`. For this model, optional `WORKMATE_LLM_REASONING_EFFORT=none` is recommended and sent only to OpenRouter: default reasoning can exhaust the constrained output budget before usable JSON content. Do not disable TLS verification; `httpx` uses `ssl.create_default_context()` including OS trust roots.

Public diagnostics distinguish `not_configured|configured|connected|error` and report model/provider, last check, latency, sanitized errors and process-local usage counters. `configured` means settings exist but a connection has not yet been confirmed. Connection test performs an actual small request. Live errors do not silently fall back to demo. Usage counters reset on restart and are not account billing; cost is accumulated only when the provider reports it.

External prompts may include current request, recent conversation, memories, app names/schema, task summaries/deadlines and uploaded filenames. Current code does not send raw Excel bytes/full cells. LLM returns allowlisted intent or schema JSON, not executable host code. No enterprise connectors are installed by this application.

## UI and verification status

Use a clean light charcoal/white Korean workspace. Screens: 오늘의 업무, 업무 요청, 파일 보관함, Excel 검증, 업무앱 제작, 개발·UAT, 업무 자동화, 내 업무 기억. Include actual connection diagnostics, loading/error/empty states, task detail/events/artifacts, batch file results, registration-input checkpoint, archive/retry/ZIP and dynamic app fields. Poll every 2 seconds while tasks run, otherwise 10 seconds. Connection mode is described as LLM or rule-based; no fake result buttons.

Python regression tests, generated-source real-browser UAT and real-server API/SQLite browser integration are separate checks. `WORKMATE_BROWSER_TEST=1` enables browser regression tests; `npm run test:browser` requires a running server and creates synthetic data. v0.2 validation on 2026-10-22: **64 regression tests passed; 11 real API/browser scenarios; 5 live LLM checks; 4 dynamic live pipeline stages including 12 browser UAT scenarios**. See verification/summary.json and separate reports; these scopes overlap and must not be summed. Use recorded evidence for each run; do not reuse v0.1 test counts.


## v0.3 desk and project contract

This is a central Planner/router plus a deterministic Engine and role modules, not autonomous ReAct loops. Direct UI execution bypasses routing. `run_desk(agent,inputs,root,store,planner,folder)` serves knowledge/analyst/reporter. Source documents are untrusted data, never instructions.

- Knowledge input: `{question,document_ids?:string[],project?:string}`. Select up to 20 documents; retrieve matching paragraphs without a vector database.
- Analyst input: `{before_id,after_id,question?}`. Exact paragraph diff is factual; LLM impact sections are labelled interpretation.
- Reporter input: `{project?,period_days:1..90,report_type:weekly|handover|meeting_prep}`. Exclude archived tasks and the reporting task itself. Period completion uses `completed_at` or explicit completed events, never `updated_at`. Unknown completion timing remains separate.
- All produce `{summary,answer,sections,sources,actions,metrics?,generation,generation_label,warnings,evidence_validation,synthetic,artifacts}` with HTML/JSON/TXT. Document hashes and source snapshots preserve evidence after edits/deletion. Model citations require an existing paragraph ID and exact nonempty substring <=800 characters. This confirms source existence/excerpts, not all semantic claims. Invalid sources are rejected. If no citations validate, model interpretation is not adopted and the generation label says so. Provider errors fail the task; they do not silently become demo output.
- Source context is capped at 60 paragraphs/48,000 characters; selected document bodies are sent to the configured provider. Synthetic provenance is retained as internal metadata; presentation has no demo badges.

`GET/POST /api/documents`, `GET/PATCH/DELETE /api/documents/{id}`, `POST /api/documents/upload` (TXT/MD <=1 MiB, UTF-8/BOM or CP949), `GET /api/documents/{id}/download`. Documents have id/title/body/category/project/created_at/updated_at/synthetic. Body cap is 100,000 characters. No PDF/DOCX parsing.

`GET /api/projects` returns current project counts; `GET /api/search?q=&project=` searches tasks/documents/apps/records/memories and returns at most 60 results. `GET /api/examples`, `POST /api/examples/{id}/run`, `POST /api/demo/seed` support reproducible synthetic scenarios. Seeding is insert-if-absent, preserves edits, and keeps sample workflows disabled.

Task metadata adds project/owner/tags/description/completed_at/notes/synthetic. Only manual tasks can be moved through todo/in_progress/waiting_input/completed; automatic execution state cannot be overwritten through TaskPatch. Preserve completed_at on repeated completed updates. Reopening clears it.

`POST /api/tasks/{id}/actions/import` accepts selected action_ids and optional project. Parent must be complete; IDs must exist in its result. Stable parent+action hashes prevent duplicate import. Imported tasks start at todo and retain source_task_id/source_action_id/source_ids. `POST /api/tasks/{id}/notes` and `DELETE /api/tasks/{id}/notes/{note_id}` store/remove notes. No external messages are sent.

Home provides project/risk context, list/kanban/week views, filters, recent artifacts and example links. Ctrl+K provides shared search. v0.3 evidence is stored under verification/v0.3 separately from v0.2 reports.


## v0.4 personal companion and ticket acceptance

Personal companion data use persisted collections `preferences` (companion key), `plans`, and `focus`; ticket acceptance uses `tickets`, `uat_runs` and `uat_decisions`. This is single-user local state, not organization identity or access control. Routes use the same local host/origin restrictions as the workspace.

| Method / route | Behavior |
| --- | --- |
| GET `/companion` | Preferences, grounded priorities, current date plan, focus state/history and contextual nudges |
| PATCH `/companion/preferences` | Name (1–20), enabled, quiet, quiet_until; validated time |
| POST `/companion/plan` | Available minutes 15–480, optional project/owner/start time; deterministic priority and capacity allocation |
| PATCH `/companion/plan/{day}/{block}` | Mark block done/undone; never marks the linked actual task completed |
| GET `/companion/plan/{day}/calendar.ics` | Portable calendar events with stable block UIDs and UTC event dates |
| POST `/companion/focus` | Task or goal, 1–180 minutes; reject another active/paused focus |
| POST `/companion/focus/{id}/pause`, `/resume`, `/complete` | Persist focus transitions; expired sessions create one completion notice |
| GET / POST `/tickets` | List tickets/runs/criteria / create a ticket |
| PATCH `/tickets/{id}` | Edit current ticket without changing earlier run snapshots |
| POST `/tickets/{id}/uat` | Executor name; snapshot current source, schema and ticket, queue actual browser evidence |
| GET `/uat/runs/{id}` | Read run status, criteria coverage, actual checks and registered artifacts |
| GET `/uat/runs/{id}/files/{filename}` | Serve only whitelisted artifacts under the run folder |
| POST `/uat/runs/{id}/decision` | Explicit reviewer decision and comment; preserve audit history |

Priority ranking uses actual due times, priority and waiting/failure states. The planner excludes queued/running tasks and does not read external calendar availability. Focus and plan completion do not imply business task completion. Timer completion requires the app process to be running or a subsequent state read. Frontend notifications are in-page speech bubbles; the mini companion is a separate browser window. Neither is an OS-wide agent or native push service.

Ticket criteria map to executed supported browser cases. Missing or failed coverage prevents a passing acceptance summary. UAT runs use an isolated in-memory API around a frozen source copy; they do not mutate the business app's persisted records. Source SHA-256, app version, request snapshot, executor and KST times are saved. Every actual browser check has a PNG artifact and execution timestamp. Existing run output remains stable after ticket or app changes. Interrupted runs are marked failed on process startup.

UAT output: self-contained HTML with embedded PNG evidence, plain delivery text, `EmailMessage` EML with `X-Unsent: 1` and HTML attachment, JSON, individual screenshots and ZIP. The recipient is validated and comes from the ticket snapshot. Mail delivery is performed by the user's mail client; no delivery claim or mail connector exists. Human acceptance starts pending and changes only through an explicit reviewer decision. The report records the state at generation time; later decisions are recorded separately in the workspace audit.

The formal result layout is original, informed by the European Commission PM² deliverables acceptance structures. No proprietary third-party template is bundled.
