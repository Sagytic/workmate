# Workmate v0.8 implementation contract

This describes the implemented local and hosted application. Korean UI, personal workspaces, supplied fixtures, actual tool execution and persisted task outcomes. Local mode is a single user; hosted mode isolates each visitor session. Never turn an unverified scope, unavailable browser, or external model error into a fabricated success.

## v0.8 personal desk and Google Workspace contract

- `backend/personal_desk.py` exports `install_personal_desk(app, ctx)`, `tick(store, current=None)`, `writing_snapshot(store)`. Collections are `desk_briefings`, `desk_followups`, `desk_routines`; writing configuration is preferences/desk_writing. Every operation uses the supplied visitor store. No model/connector client is available in this module.
- `GET /api/personal-desk/state` returns preferences, active followups/routines and the latest 30 briefings. `POST /briefings` accepts `{project,document_ids,date?}`; empty document selection uses imported email/event snapshots from the project or this workspace. Tasks use actual noncompleted records in the project/workspace independently of document selection. Results carry counts, source IDs, excerpts, warnings and captured_at. Mail detail is capped at 20, events at 50, due tasks at 100 while counts retain the complete matched scope. Imported EML does not prove live read/unread state.
- Brief day membership uses KST interval overlap with exclusive event ends. Uncertain/unexpanded events are excluded with warnings. Google all-day original dates/timezone are retained separately from verified instants. Date-only task deadlines remain dates. Captures are static evidence, not current mailbox availability.
- `POST /followups` accepts `{title,document_id,direction:owed|waiting,due_at?,note?}`. The user establishes direction and completion; no whole-thread or reply-delivery inference occurs. Repeating an identical open item is idempotent. `PATCH /followups/{id}` supports status, snooze, title, due date and note. DELETE soft-archives. Capacity is 100 active records.
- `POST /routines` accepts `{name,project,document_ids,weekdays:[0..6],time:HH:mm,enabled}`; Monday is 0 and time is KST. PATCH edits settings, POST /{id}/run creates a manual briefing, DELETE soft-archives and clears next_run_at/enabled. Capacity is 30 active definitions. The existing scheduler calls tick approximately every 15 seconds. Locking and stable routine/slot identities prevent duplicate execution and notifications. Older missed calendar days are skipped; no offline replay, provider sync or automatic LLM invocation occurs. Missing selected documents fail closed without broadening scope. Snooze expiry produces one route=desk notification while active.
- `GET/PATCH /writing` reads/updates `{enabled,tone,language,length,signature,snippets:[{title,text}]}`. Limits: tone80/language40/length40/signature1000, 12 snippets with title80/text1200. Disabled settings return an empty analysis snapshot. Hub snapshots these settings only for inbox/followup runs; the system treats them as style guidance, never source evidence or executable permission.
- `backend/google_workspace.py` implements read-only Google authorization code with a registered Web client, fixed redirect URI, per-context one-time state and S256 challenge. Runtime scopes: openid, email, gmail.readonly, calendar.events.readonly. Credentials, code verifier and access/refresh tokens remain in connector memory; close clears them. Public results never return secret/token values. Client consent/real OAuth interoperability is not proven by provider mocks or file imports.
- Google paths: GET /api/google/status; POST /config {client_id,client_secret}; POST /auth/start; GET /auth/callback; DELETE /connection; GET /messages?limit=&q=; GET /events?days=7|14; POST /import {message_ids,event_ids,project}. Gmail search filters this fetched inbox page, not the whole mailbox. Calendar reads primary, expands instances and retains response calendar timezone for DATE boundaries. Results disclose pagination limits.
- Provider origins and supported read paths are fixed; redirect responses are not followed. MIME attachments, forwarded message parts, remote images and external links are not fetched. Only explicitly selected cached item IDs enter the store. Canonical import identity separates provider/account/project/content. Unchanged snapshots are reused and changed sources create another snapshot.
- Google gmail.readonly is restricted, and External Testing refresh tokens generally expire in seven days for these scopes. Bring-your-own registration does not imply verification exemption or organizational permission. No send/draft-save/label/calendar-write route is implemented. Model execution may transmit only selected excerpts to the user-configured provider after an explicit request.

- Administrators may explicitly preconfigure WORKMATE_GOOGLE_CLIENT_ID and WORKMATE_GOOGLE_CLIENT_SECRET on the server. The client identifies the app, not a shared mailbox: every visitor still has independent consent/token state. Per-visitor BYO configuration can override the default. Current public deployment has no configured shared Google app; do not claim one-click consent without the prerequisite registration.

## Mail, calendar and specialist extensions

- `backend/agent_specialists.py` adds `inbox`, `meeting_prep`, `followup`, `focus`. They read selected imported source documents through the existing scoped Hub pipeline. Mail/calendar content is untrusted evidence, not a tool/permission instruction. Selected excerpts may be sent to the configured user LLM when analysis is requested.
- Reply draft results retain source evidence and a bounded subject/body. Recipient headers are stripped of line breaks; addresses without source support are cleared. `GET /api/agent-hub/runs/{run_id}/reply-drafts/{index}.eml` returns a completed run's draft as a message file. Workmate does not send it.
- A focus run accepts `{focus:{date,start,end,block_minutes,break_minutes}}`; times use KST. Time blocks are computed deterministically from selected event metadata and scoped incomplete tasks, independently of model suggestions. Canceled/free events do not occupy time. Recurrence, timezone ambiguity, or source-count/text-budget omissions that prevent reliable collision checks block a definite schedule instead of assuming availability. The result discloses that only selected snapshots were checked. `GET /api/agent-hub/runs/{run_id}/schedule.ics` exports proposed blocks for manual calendar import; no external event is created.
- The Microsoft connector uses a configurable, user-owned/approved Entra public-client ID and tenant with delegated device authorization. Runtime scopes are `User.Read`, `Mail.Read`, `Calendars.Read`, `offline_access`; no application permission, Mail.Send, Mail.ReadWrite or Calendars.ReadWrite is requested. Microsoft/tenant consent is external and is not fabricated by local fixtures or tests.
- Device login and token state must be per visitor context, memory only, bounded and cleared on disconnect/session disposal/restart. Public status may expose account/configuration metadata and the current user code/verification URI while pending; it must never expose device_code, access_token, refresh_token or sensitive raw error responses. Token and Graph hosts are fixed Microsoft origins. Honor pending interval/expiry and prevent stale/concurrent login results from reconnecting a canceled session.
- User-triggered Graph reads create a reviewable item list; only selected IDs are imported. An explicitly selected `.eml`/`.ics` file imports its parsed messages/events (at most 100 ICS events). Imported documents retain source metadata and stable identities; agent profiles separately select which documents to analyze. No arbitrary URLs, remote images or executable attachments are followed. Imported document retention is separate from connector token lifetime. Re-imports must not silently create duplicate items.
- ICS recurrence/timezone limits are explicit. A parsed recurrence rule without expanded occurrences is not evidence that later times are free. Imported timestamps preserve known offsets; floating/unsupported timezone values require an uncertainty marker or a supported resolution.
- The hosted application remains an isolated visitor workspace, not durable user signup. Google or Microsoft connector identity does not establish Workmate login, account recovery, cross-device access or long-term storage. Account signup and user-scoped durable database integration remain future extension scope; current free-host persistence limits are disclosed.

## v0.8 specialist studio contract

`backend/agent_hub.py` provides `seed_agent_hub(store)`, `install_agent_hub(app, ctx)`, and the shared chat/API entry point `start_run(ctx, agent_id, request)`. Collections are `hub_agents`, `hub_runs`, and `hub_missions`. Eight built-in kinds are `meeting`, `decision`, `radar`, `reflection`, `inbox`, `meeting_prep`, `followup`, `focus`; they use a fixed visible pipeline, not unrestricted ReAct execution.

- Profile fields: `id,name,description,instructions,kind,document_ids,project,use_memory,builtin,archived,created_at,updated_at`. Names max 80 characters, instructions 4,000, selected document IDs 20; no more than 40 unarchived profiles. Archive hides a profile without deleting its old runs.
- `POST /api/agent-hub/agents` creates a profile; `PATCH` and `DELETE /api/agent-hub/agents/{id}` update/archive it. IDs must resolve. A selected document must match an explicitly selected project.
- `POST /api/agent-hub/agents/{id}/run` accepts `{request}` (1–12,000 characters); `GET /api/agent-hub/runs/{id}` returns full state. Chat dispatch uses the same validation/capacity path and returns `hub_run_id`.
- `GET /api/agent-hub` returns `{agents,runs,missions,kinds}`. Runs are compact summaries, capped at 100; missions are capped at 30. Full evidence is available through run detail.
- `POST /api/agent-hub/missions` accepts `{name?,request,agent_ids:[1..4]}`. Every selected profile and its context is validated before any run is queued. A mission contains `{id,name,request,agent_ids,run_ids,created_at}`. Read with `GET /api/agent-hub/missions/{id}`. Its derived status is `queued|running|completed|partial|failed`, with actual child summaries and completion/failure counts. No coordinator occupies a pool worker waiting for other workers. Each child retains its own result and import endpoints.
- Each run snapshots the profile, request, allowed sources and metrics before entering `ctx.engine.pool`. Steps are `자료 수집`, `전문 분석`, `근거 확인`, `결과 전달`, with `pending|running|completed|failed`, timestamps and actual events. Run status is `queued|running|completed|failed`; `mode` is `llm|rules`. The connected Planner performs one structured `_complete` call per specialist; with no configured API, deterministic extraction/aggregation runs instead. API errors fail honestly. Startup marks interrupted runs failed.
- Document selection is explicit, or the selected project's documents when IDs are empty. Blank project plus no documents reads no library documents. Task records are included for the selected project, or all non-archived tasks for unscoped radar/reflection/followup/focus. Meeting/decision without a project read no tasks. Memory is opt-in, includes at most 20 records (global or matching project). Document excerpts are capped at 6,000 characters, detailed tasks at 100, total context text at 64,000 characters; truncation is reported. Metrics count the full scoped task set. Recent completion uses actual `completed_at` within the captured seven-day interval, never `updated_at`.
- Source records contain `{id,title,type,excerpt}` with optional frozen metadata. Result has `summary`, `decisions`, `actions`, `insights`, `risks`, `memory_suggestions`, `alternatives`, `followup_questions`, `reply_drafts`, `time_blocks`, and `focus_plan`. Evidence-bearing items carry `evidence_id,evidence_quote,evidence_verified`. Unresolvable IDs or quotations are cleared and recorded in `verification.warnings`; unsupported owners/deadlines are cleared. Exact quote presence is not a semantic entailment check. Date-only deadlines stay dates and become overdue on the next local day.
- `POST /api/agent-hub/runs/{id}/actions` accepts `{indices}` and returns `{created,existing,tasks}`. Only selected completed-run results create manual `todo` tasks. Stable run/index identities make repeated import idempotent.
- `POST /api/agent-hub/runs/{id}/memory` accepts `{indices}` and returns `{created,existing,conflicts,memories}`. Unverified quotes cannot be imported. Same-name/same-value memory is reused; different values preserve the existing memory and return a conflict. No silent overwrite and no import during analysis.
- `GET /api/agent-hub/runs/{id}/report.html` is a self-contained HTML attachment. It escapes user/model/source text and sends a restrictive CSP. A completed run is required. Existing reports do not reread edited/deleted source records.
- Capacity counts active tasks, UAT runs and hub runs: 4 hosted / 12 local. A mission must fit all requested slots before starting. Specialist execution has no host-code, network browsing, outbound messaging or arbitrary write tools. Only the configured LLM call may leave the workspace; scoped source excerpts are sent to that provider when enabled.

## v0.5 personal API, hosted service and desktop Mini

- `/api/connection/config` GET returns public endpoint/model/status metadata without keys; POST validates a candidate with an actual model request before applying it; DELETE clears the running connection. Local optional persistence writes `.env`; hosted sessions disallow persistence. A key is not reused when changing endpoint. Hosted destinations are allowlisted public OpenAI-compatible providers. Local custom HTTPS and loopback HTTP providers are supported.
- `backend.cloud:app` serves isolated visitor modules, SQLite stores, files and memory-only API settings. Owner `.env`, DB and private work history are excluded from the deployment. Secure HttpOnly SameSite cookies identify public sessions; idle sessions expire after one hour. Hosting is ephemeral across server restarts/redeploys. Public limits are eight visitor sessions with per-session request/model-call throttles.
- Electron Windows Mini runs independently of the browser, with an owl window, chat panel, tray, speech bubbles, always-on-top option, optional Windows login startup and `Ctrl+Alt+Space`. Closing a panel hides it; tray Quit exits. It reuses an existing local server or starts/owns a local server. The renderer is sandboxed, context isolated and has no Node integration; a narrow preload exposes validated companion actions. It does not turn off-PC time into running background work.

## Runtime and persistence

- FastAPI at `127.0.0.1:8765`; production serves `frontend/dist`. Vite development runs at `127.0.0.1:5173` and proxies `/api`.
- SQLite defaults to `data/workmate.db`. `WORKMATE_DATA_DIR` changes the DB directory; upload and artifact paths remain under the project `data/` directory.
- Persisted collections: tasks, messages, memories, apps, records, workflows, notifications, uploads, documents, projects, examples, meta.
- Artifacts: `data/artifacts/{task_id}/attempt-{NN}/`; repair attempts use a separate child directory. Artifact names are relative to the task directory and retain nested paths.
- Timestamps use Korea Standard Time. The in-process scheduler checks about every 15 seconds. It requires a running app server, does not backfill every missed interval, and is not an OS/background service.
- On restart, interrupted queued/running tasks are marked failed. Completed state and files persist. Local data has no automatic retention policy; hosted visitor sessions follow the one-hour idle expiry described above.
- Bind locally. Host/origin restrictions are not multi-user authentication. Optional delegated Microsoft Graph and Google read access imports selected mail/calendar snapshots. There is no external ERP/financial transaction execution or external mail/calendar write action.

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

Use a clean Korean workspace with dark mode by default and an optional light theme. Screens: 오늘의 업무, 업무 요청, 파일 보관함, Excel 검증, 업무앱 제작, 개발·UAT, 업무 자동화, 내 업무 기억. Include actual connection diagnostics, loading/error/empty states, task detail/events/artifacts, batch file results, registration-input checkpoint, archive/retry/ZIP and dynamic app fields. Poll every 2 seconds while tasks run, otherwise 10 seconds. Connection mode is described as LLM or rule-based; no fake result buttons.

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

Priority ranking uses actual due times, priority and waiting/failure states. The planner excludes queued/running tasks and does not read external calendar availability. Focus and plan completion do not imply business task completion. Timer completion requires the app process to be running or a subsequent state read. The original v0.4 frontend notifications are in-page speech bubbles with an optional separate browser window. The v0.5 desktop Mini adds independent native windows and a tray, as described above; notifications still require a running local app/server.

Ticket criteria map to executed supported browser cases. Missing or failed coverage prevents a passing acceptance summary. UAT runs use an isolated in-memory API around a frozen source copy; they do not mutate the business app's persisted records. Source SHA-256, app version, request snapshot, executor and KST times are saved. Every actual browser check has a PNG artifact and execution timestamp. Existing run output remains stable after ticket or app changes. Interrupted runs are marked failed on process startup.

UAT output: self-contained HTML with embedded PNG evidence, plain delivery text, `EmailMessage` EML with `X-Unsent: 1` and HTML attachment, JSON, individual screenshots and ZIP. The recipient is validated and comes from the ticket snapshot. Mail delivery is performed by the user's mail client; no delivery claim or mail connector exists. Human acceptance starts pending and changes only through an explicit reviewer decision. The report records the state at generation time; later decisions are recorded separately in the workspace audit.

The formal result layout is original, informed by the European Commission PM² deliverables acceptance structures. No proprietary third-party template is bundled.
