"""Constrained business-app generation and evidence-backed browser UAT.

The generated application is real HTML/JS. Development is deliberately limited
to supported schema changes; natural-language/LLM output is never executed.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from datetime import datetime, timezone


FIELD_SPECS = [
    {"name": "title", "label": "업무명", "type": "text", "required": True},
    {"name": "owner", "label": "담당자", "type": "text", "required": False},
    {"name": "due_date", "label": "마감일", "type": "date", "required": False},
    {"name": "status", "label": "상태", "type": "select", "required": True,
     "options": ["대기", "진행", "완료"]},
    {"name": "completion_note", "label": "완료 메모", "type": "textarea", "required": False},
]


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def _safe_script_json(value):
    return _json(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


FIELD_TYPES = {"text", "textarea", "number", "date", "select", "email", "checkbox"}
RESERVED_FIELDS = {"id", "app_id", "created_at", "updated_at", "constructor", "prototype", "__proto__"}


def _finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def _normalise_schema(schema=None, warnings=None):
    schema = schema if isinstance(schema, dict) else {}
    warnings = warnings if warnings is not None else []
    raw_fields = schema.get("fields", [])
    if not isinstance(raw_fields, list):
        warnings.append("fields는 배열이어야 하므로 기본 필드를 사용했습니다.")
        raw_fields = []
    defaults = {f["name"]: f for f in FIELD_SPECS}
    sanitised = {}
    for raw in raw_fields[:64]:
        if not isinstance(raw, dict):
            warnings.append("객체가 아닌 필드 정의를 제외했습니다.")
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) or name in RESERVED_FIELDS:
            warnings.append("허용되지 않은 필드 이름을 제외했습니다.")
            continue
        default = defaults.get(name, {})
        kind = default.get("type") or raw.get("type", "text")
        if not isinstance(kind, str) or kind not in FIELD_TYPES:
            warnings.append(f"{name}: 지원하지 않는 입력 유형을 제외했습니다.")
            continue
        label = raw.get("label", default.get("label", name))
        if not isinstance(label, str) or not label.strip():
            label = default.get("label", name)
        field = {"name": name, "label": label.strip()[:40], "type": kind,
                 "required": raw.get("required") if isinstance(raw.get("required"), bool) else default.get("required", False)}
        if name in ("title", "status"):
            field["required"] = True
        if kind == "select":
            options = default.get("options") or raw.get("options")
            if not isinstance(options, list):
                warnings.append(f"{name}: 선택 항목이 없어 필드를 제외했습니다.")
                continue
            options = list(dict.fromkeys(x.strip()[:80] for x in options if isinstance(x, str) and x.strip()))[:30]
            if not options:
                warnings.append(f"{name}: 유효한 선택 항목이 없어 필드를 제외했습니다.")
                continue
            field["options"] = options
        if kind == "number":
            for bound in ("min", "max"):
                value = raw.get(bound)
                if _finite_number(value):
                    field[bound] = value
                elif value is not None:
                    warnings.append(f"{name}: 숫자가 아닌 {bound} 제약을 제외했습니다.")
            if "min" in field and "max" in field and field["min"] > field["max"]:
                warnings.append(f"{name}: 최솟값이 최댓값보다 커서 필드를 제외했습니다.")
                continue
        sanitised[name] = field
    fields = [sanitised.pop(f["name"], copy.deepcopy(f)) for f in FIELD_SPECS]
    if len(sanitised) > 8:
        warnings.append("사용자 정의 필드는 최대 8개까지 반영했습니다.")
    fields.extend(list(sanitised.values())[:8])
    return {"fields": fields, "require_completion_note": schema.get("require_completion_note") is True,
            "prevent_past_due_date": schema.get("prevent_past_due_date") is True}


def _apply_requirement(schema, requirement, plan=None):
    schema = _normalise_schema(schema)
    original = copy.deepcopy(schema)
    warnings = []
    changes = []
    text = str(requirement or "").lower()
    # A structured plan is authoritative: keywords elsewhere in the request
    # must not accidentally make unrelated base fields required.
    use_keyword_fallback = plan is None
    plan = plan if isinstance(plan, dict) else {}
    proposed = plan.get("schema") if isinstance(plan.get("schema"), dict) else plan
    planned_raw = proposed.get("fields", [])
    if isinstance(planned_raw, list):
        merged = {f["name"]: copy.deepcopy(f) for f in schema["fields"]}
        invalid = []
        for raw in planned_raw[:64]:
            if isinstance(raw, dict) and isinstance(raw.get("name"), str):
                prior = merged.get(raw["name"], {})
                clean_raw = dict(raw)
                if "required" in clean_raw and not isinstance(clean_raw["required"], bool):
                    warnings.append(f"{raw['name']}: required는 boolean이어야 하므로 기존 설정을 유지했습니다.")
                    clean_raw.pop("required")
                for bound in ("min", "max"):
                    if bound in clean_raw and clean_raw[bound] is not None:
                        value = clean_raw[bound]
                        if not _finite_number(value):
                            warnings.append(f"{raw['name']}: 잘못된 {bound} 제약을 무시했습니다.")
                            clean_raw.pop(bound)
                candidate = {**prior, **clean_raw}
                checked = _normalise_schema({"fields": [candidate]}, warnings)
                accepted = next((f for f in checked["fields"] if f["name"] == raw["name"]), None)
                if accepted is not None:
                    merged[raw["name"]] = accepted
            else:
                invalid.append(raw)
        schema = _normalise_schema({**schema, "fields": list(merged.values()) + invalid}, warnings)
    elif "fields" in proposed:
        warnings.append("fields는 배열이어야 하므로 변경하지 않았습니다.")
    features = plan.get("features", []) if isinstance(plan.get("features", []), list) else []
    completion = ((proposed.get("require_completion_note") is True) or "completion_note_required" in features or
                  (use_keyword_fallback and "완료" in text and any(w in text for w in ("메모", "사유", "내용", "비고"))
                   and any(w in text for w in ("필수", "반드시", "없으면", "없이는", "mandatory", "required"))) or
                  (use_keyword_fallback and "completion" in text and "required" in text))
    if isinstance(proposed.get("require_completion_note"), bool):
        schema["require_completion_note"] = proposed["require_completion_note"]
    elif completion:
        schema["require_completion_note"] = True
    planned_fields = {f["name"]: f for f in planned_raw if isinstance(f, dict) and isinstance(f.get("name"), str)} if isinstance(planned_raw, list) else {}
    for key, label, terms in (("owner", "담당자", ("담당자", "owner")),
                              ("due_date", "마감일", ("마감", "기한", "due"))):
        requested = ((proposed.get("require_" + key) is True) or (key + "_required" in features) or
                     (planned_fields.get(key, {}).get("required") is True) or
                     (use_keyword_fallback and key not in planned_fields and any(term in text for term in terms) and any(w in text for w in ("필수", "반드시", "required", "mandatory"))))
        field = next(f for f in schema["fields"] if f["name"] == key)
        if requested and not field["required"]:
            field["required"] = True
    past_due = (proposed.get("prevent_past_due_date") is True) or "prevent_past_due_date" in features or (
        use_keyword_fallback and any(w in text for w in ("과거", "지난")) and any(w in text for w in ("마감", "기한"))
        and any(w in text for w in ("금지", "불가", "안 되", "못", "막")))
    if isinstance(proposed.get("prevent_past_due_date"), bool):
        schema["prevent_past_due_date"] = proposed["prevent_past_due_date"]
    elif past_due:
        schema["prevent_past_due_date"] = True
    old_fields = {f["name"]: f for f in original["fields"]}
    for field in schema["fields"]:
        if field["name"] not in old_fields:
            changes.append(f"{field['label']} 입력 항목 추가 ({field['type']})")
        elif field != old_fields[field["name"]]:
            changes.append(f"{field['label']} 입력 설정 변경" + (" · 필수" if field["required"] else " · 선택"))
    for flag, label in (("require_completion_note", "완료 시 메모 필수"), ("prevent_past_due_date", "과거 마감일 차단")):
        if schema[flag] != original[flag]:
            changes.append(label + (" 적용" if schema[flag] else " 해제"))
    return schema, changes, warnings


APP_TEMPLATE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · Workmate</title><style>
*{box-sizing:border-box}body{margin:0;background:#f7f7f8;color:#26272b;font:14px/1.55 system-ui,-apple-system,"Malgun Gothic",sans-serif}button,input,select,textarea{font:inherit}button,a.button{cursor:pointer;border:1px solid #dedee2;border-radius:7px;padding:9px 13px;font-weight:600;background:#fff;color:#35363b;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;white-space:nowrap}button.primary{background:#292a30;color:white;border-color:#292a30}button:hover,a.button:hover{background:#f0f0f2}button.primary:hover{background:#44464e}button:disabled{opacity:.5;cursor:wait}button.danger{color:#b33d3d;background:white}header{background:#fff;border-bottom:1px solid #e3e3e7;padding:28px max(24px,calc((100vw - 1400px)/2))}.eyebrow{color:#85858d;font-size:11px;letter-spacing:.13em}h1{font-size:27px;letter-spacing:-.04em;margin:8px 0 5px;font-weight:650}header p{margin:0;color:#7c7d85;max-width:850px}.shell{max-width:1450px;margin:24px auto;padding:0 24px}.topline{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:18px}.toolbar{display:flex;gap:8px;align-items:center}#count{font-size:12px;color:#777880}.grid{display:grid;grid-template-columns:310px minmax(0,1fr);gap:20px;align-items:start}.card{background:#fff;border:1px solid #e3e3e8;border-radius:11px;padding:21px;box-shadow:0 2px 5px #25283302}h2{font-size:16px;margin:0 0 18px;letter-spacing:-.02em}.field{margin-bottom:14px}label{display:block;font-size:12px;font-weight:600;color:#55565e;margin-bottom:6px}input,select,textarea{width:100%;padding:9px 11px;border:1px solid #dedee5;border-radius:7px;color:#2a2b30;background:#fff;min-height:39px}input:focus,select:focus,textarea:focus{outline:3px solid #dfe1e64d;border-color:#9699a4}input[type=checkbox]{width:18px;min-height:18px;height:18px;accent-color:#35363f}textarea{min-height:75px;resize:vertical}.form-actions{display:flex;gap:8px;padding-top:6px}.feedback{margin-bottom:18px;padding:11px 14px;border-radius:7px;background:#edf3ef;color:#3c6651;font-size:13px}.feedback.error{background:#fcf0ed;color:#a54435}.feedback:empty{display:none}.list-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:15px;gap:10px}.list-head h2{margin:0}.filters{display:flex;gap:9px;margin-bottom:15px}.filters input{max-width:330px}.filters select{width:140px}.table-wrap{overflow:auto;max-height:680px}table{width:100%;border-collapse:collapse;font-size:12px;min-width:650px}th{position:sticky;top:0;text-align:left;font-size:11px;color:#8a8a94;background:#fafafb;border-bottom:1px solid #e3e3e8;padding:11px 10px;white-space:nowrap;font-weight:600}td{padding:14px 10px;border-bottom:1px solid #ededf0;vertical-align:middle;word-break:break-word;min-width:85px;max-width:260px}td:first-child{font-weight:600;min-width:150px}td.type-textarea{min-width:175px}td.type-email{min-width:170px}td:last-child,th:last-child{position:sticky;right:0;min-width:120px;background:#fff;z-index:2;box-shadow:-4px 0 5px #30303a05}th:last-child{background:#fafafb;z-index:3}.status{font-size:11px;white-space:nowrap;background:#f0f0f3;color:#6b6b76;padding:4px 9px;border-radius:5px}.status.done{background:#eaf2ec;color:#4b7558}.status.active{background:#eef0f8;color:#646c93}.row-actions{display:flex;gap:5px}.row-actions button{font-size:11px;padding:5px 8px;border-color:#ededf0}.empty{padding:44px 12px;text-align:center;color:#9898a2}.rules{margin-top:19px;padding-top:14px;border-top:1px solid #ededf0;font-size:11px;color:#8a8b94}.rules summary{cursor:pointer}.rules ul{padding-left:18px}.footer{font-size:11px;color:#a1a1a9;margin:22px 0}.field-hint{font-size:10px;color:#9898a2;margin:4px 0 0}@media(max-width:900px){.grid{grid-template-columns:1fr}.shell{padding:0 14px}.topline{align-items:start;flex-wrap:wrap}.toolbar{flex-wrap:wrap}header{padding:24px}.card{padding:17px}.filters input{max-width:none}.table-wrap{max-height:none}}
</style></head><body>
<header><div class="eyebrow">WORKSPACE</div><h1 id="app-title"></h1><p id="app-description"></p></header>
<main class="shell"><div class="topline"><span id="count">불러오는 중</span><div class="toolbar"><a id="export-csv" class="button" download>CSV 내보내기</a><button id="refresh" type="button">새로고침</button></div></div>
<div id="feedback" class="feedback" role="status" aria-live="polite"></div>
<div class="grid"><section class="card"><h2 id="form-title">새 업무 등록</h2><form id="record-form" novalidate><div id="form-fields"></div><div class="form-actions"><button type="submit" id="save-record" class="primary">저장</button><button type="button" id="reset-record">초기화</button></div></form><details class="rules"><summary>입력 규칙 보기</summary><ul id="rule-list"></ul></details></section>
<section class="card"><div class="list-head"><h2>업무 목록</h2><span id="filtered-count" class="field-hint"></span></div><div class="filters"><input id="search-records" type="search" aria-label="업무 검색" placeholder="업무, 담당자, 내용 검색"><select id="status-filter" aria-label="상태 필터"><option value="">모든 상태</option><option>대기</option><option>진행</option><option>완료</option></select></div><div class="table-wrap"><table><thead><tr id="record-headings"></tr></thead><tbody id="records"></tbody></table></div><div id="empty" class="empty" hidden>아직 등록한 업무가 없습니다.</div></section></div>
<p class="footer">Workmate Workspace</p></main>
<script id="app-config" type="application/json">__CONFIG__</script>
<script>
'use strict';
const CONFIG=JSON.parse(document.getElementById('app-config').textContent);
const APP_BASE='/api/apps/'+encodeURIComponent(CONFIG.id),API=APP_BASE+'/records';
const schema=CONFIG.schema;let records=[],editing=null,busy=false;const $=id=>document.getElementById(id);
$('app-title').textContent=CONFIG.name;$('app-description').textContent=CONFIG.description;$('export-csv').href=APP_BASE+'/export.csv';
for(const f of schema.fields){const wrap=document.createElement('div');wrap.className='field';const label=document.createElement('label');label.htmlFor='field-'+f.name;label.textContent=f.label+(f.required?' *':'');const el=document.createElement(f.type==='select'?'select':f.type==='textarea'?'textarea':'input');el.id='field-'+f.name;el.name=f.name;if(!['select','textarea'].includes(f.type))el.type=f.type;el.maxLength=f.type==='textarea'?2000:300;if(f.type==='number'){el.step='any';if(f.min!==undefined)el.min=String(f.min);if(f.max!==undefined)el.max=String(f.max)}if(f.type==='select'){if(f.name!=='status'){const empty=document.createElement('option');empty.value='';empty.textContent='선택';el.append(empty)}for(const option of f.options){const item=document.createElement('option');item.value=option;item.textContent=option;el.append(item)}}wrap.append(label,el);if(f.type==='number'&&(f.min!==undefined||f.max!==undefined)){const hint=document.createElement('p');hint.className='field-hint';hint.textContent=(f.min!==undefined?'최소 '+f.min:'')+(f.min!==undefined&&f.max!==undefined?' · ':'')+(f.max!==undefined?'최대 '+f.max:'');wrap.append(hint)}$('form-fields').append(wrap);const th=document.createElement('th');th.textContent=f.label;$('record-headings').append(th)}const actionHeader=document.createElement('th');actionHeader.textContent='관리';$('record-headings').append(actionHeader);
const rules=schema.fields.filter(f=>f.required).map(f=>f.label+' 필수');if(schema.require_completion_note)rules.push('완료 상태에서는 완료 메모 필수');if(schema.prevent_past_due_date)rules.push('과거 마감일 저장 불가');for(const rule of rules){const li=document.createElement('li');li.textContent=rule;$('rule-list').append(li)}
function message(text,error=false){$('feedback').textContent=text;$('feedback').className='feedback'+(error?' error':'')}
function values(){return Object.fromEntries(schema.fields.map(f=>{const el=$('field-'+f.name);return[f.name,f.type==='checkbox'?el.checked:f.type==='number'&&el.value.trim()!==''?Number(el.value):el.value.trim()]}))}
function validate(data){for(const f of schema.fields){const value=data[f.name],empty=value===''||value===null||value===undefined||(f.type==='checkbox'&&value!==true);if(f.required&&empty)return ({title:'업무명을',owner:'담당자를',due_date:'마감일을',status:'상태를',completion_note:'완료 메모를'}[f.name]||f.label)+' 입력해주세요.';if(!empty&&f.type==='number'){if(!Number.isFinite(value))return f.label+'에 숫자를 입력해주세요.';if(f.min!==undefined&&value<f.min)return f.label+'의 최솟값은 '+f.min+'입니다.';if(f.max!==undefined&&value>f.max)return f.label+'의 최댓값은 '+f.max+'입니다.'}if(!empty&&f.type==='email'&&!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value))return f.label+'의 이메일 형식을 확인해주세요.';if(!empty&&f.type==='select'&&!f.options.includes(value))return f.label+'의 허용값을 선택해주세요.'}if(schema.require_completion_note&&data.status==='완료'&&!data.completion_note)return '완료 상태에서는 완료 메모를 입력해주세요.';if(schema.prevent_past_due_date&&data.due_date){const now=new Date();const today=now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');if(data.due_date<today)return '마감일을 오늘 이후로 입력해주세요.'}return null}
function reset(){editing=null;$('record-form').reset();$('form-title').textContent='새 업무 등록';$('save-record').textContent='저장'}
function edit(record){editing=record.id;for(const f of schema.fields){const el=$('field-'+f.name);if(f.type==='checkbox')el.checked=record[f.name]===true;else el.value=record[f.name]??''}$('form-title').textContent='업무 수정';$('save-record').textContent='변경 저장';message('선택한 업무를 수정하고 저장해주세요.');$('field-title').focus()}
async function remove(record){if(busy||!window.confirm('이 업무를 삭제할까요? 삭제 후에는 되돌릴 수 없습니다.'))return;busy=true;try{await request(API+'/'+encodeURIComponent(record.id),{method:'DELETE'});if(editing===record.id)reset();await load();message('업무를 삭제했습니다.')}catch(error){message(error.message,true)}finally{busy=false}}
function render(){const query=$('search-records').value.trim().toLocaleLowerCase(),filter=$('status-filter').value,visible=records.filter(r=>(!filter||r.status===filter)&&(!query||schema.fields.some(f=>String(r[f.name]??'').toLocaleLowerCase().includes(query))));const body=$('records');body.replaceChildren();for(const record of visible){const row=document.createElement('tr');row.dataset.id=record.id;for(const f of schema.fields){const td=document.createElement('td');td.className='type-'+f.type;if(f.name==='status'){const span=document.createElement('span');span.className='status'+(record.status==='완료'?' done':record.status==='진행'?' active':'');span.textContent=record.status||'대기';td.append(span)}else{const value=record[f.name];td.textContent=f.type==='checkbox'?(value===true?'예':'아니요'):(value===null||value===undefined||value===''?'—':String(value))}row.append(td)}const td=document.createElement('td'),actions=document.createElement('div');actions.className='row-actions';const button=document.createElement('button');button.type='button';button.className='edit-record';button.textContent='수정';button.addEventListener('click',()=>edit(record));const del=document.createElement('button');del.type='button';del.className='delete-record danger';del.textContent='삭제';del.addEventListener('click',()=>remove(record));actions.append(button,del);td.append(actions);row.append(td);body.append(row)}$('empty').hidden=visible.length>0;$('empty').textContent=records.length?'검색 조건에 맞는 업무가 없습니다.':'아직 등록한 업무가 없습니다.';$('count').textContent=records.length+'개 업무 · 완료 '+records.filter(r=>r.status==='완료').length+'개';$('filtered-count').textContent=visible.length+'개 표시'}
async function request(url,options={}){const response=await fetch(url,{credentials:'same-origin',...options});if(response.status===204)return {};let data;try{data=await response.json()}catch{throw new Error('서버 응답을 읽을 수 없습니다.')}if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'요청을 처리하지 못했습니다.');return data}
async function load(){records=await request(API);if(!Array.isArray(records))throw new Error('업무 목록 형식이 올바르지 않습니다.');render()}
$('record-form').addEventListener('submit',async event=>{event.preventDefault();if(busy)return;const data=values(),error=validate(data);if(error){message(error,true);return}busy=true;$('save-record').disabled=true;try{await request(editing?API+'/'+encodeURIComponent(editing):API,{method:editing?'PATCH':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});await load();reset();message('업무를 저장했습니다.')}catch(error){message(error.message,true)}finally{busy=false;$('save-record').disabled=false}});
$('reset-record').addEventListener('click',()=>{reset();message('')});$('refresh').addEventListener('click',()=>load().then(()=>message('최신 목록을 불러왔습니다.')).catch(error=>message(error.message,true)));$('search-records').addEventListener('input',render);$('status-filter').addEventListener('change',render);load().catch(error=>message(error.message,true));
</script></body></html>'''


def _render_app(app_id, name, description, schema):
    from .app_theme import apply_app_theme
    config = {"id": str(app_id), "name": name, "description": description, "schema": _normalise_schema(schema)}
    return apply_app_theme(APP_TEMPLATE.replace("__TITLE__", html.escape(name)).replace("__CONFIG__", _safe_script_json(config)))


def create_app(prompt: str, app_id: str, output_dir: Path, plan: dict | None = None) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    requirement_plan = plan
    plan = plan if isinstance(plan, dict) else {}
    name = str(plan.get("name") or "업무 요청 관리")[:80]
    if not plan.get("name"):
        for keyword, suggested in (("견적", "견적 검수 관리"), ("경비", "경비 처리 관리"), ("고객", "고객 요청 관리"),
                                   ("프로젝트", "프로젝트 업무 관리"), ("휴가", "휴가 요청 관리"), ("이슈", "이슈 관리")):
            if keyword in prompt:
                name = suggested
                break
    description = str(plan.get("description") or "업무를 등록하고 담당자, 기한, 진행 상태를 한곳에서 관리합니다.")[:500]
    schema, _, warnings = _apply_requirement(_normalise_schema(), prompt, requirement_plan)
    source = _render_app(app_id, name, description, schema)
    (output_dir / "app.html").write_text(source, encoding="utf-8", newline="\n")
    source_sha = hashlib.sha256((output_dir / "app.html").read_bytes()).hexdigest()
    (output_dir / "app-schema.json").write_text(_json(schema), encoding="utf-8")
    (output_dir / "app-manifest.json").write_text(_json({
        "app_id": app_id, "name": name, "prompt": prompt, "source_sha256": source_sha,
        "generation": "allowlisted-schema-template", "capabilities": ["create", "read", "update", "delete", "search", "status-filter", "csv-export", "typed-fields", "validation"],
        "warnings": warnings,
        "limitations": ["기본 5개 및 사용자 정의 최대 8개 필드", "외부 시스템 연계 미지원", "생성만으로 테스트 통과를 의미하지 않음"]}), encoding="utf-8")
    return {"name": name, "description": description, "schema": schema, "source_filename": "app.html",
            "source_sha256": source_sha, "warnings": warnings,
            "artifacts": [{"name": "app.html", "kind": "app"}, {"name": "app-schema.json", "kind": "json"},
                          {"name": "app-manifest.json", "kind": "json"}]}


def _old_source(app):
    for key in ("source_path", "source_filename"):
        candidate = app.get(key)
        if candidate:
            path = Path(candidate)
            if path.is_file():
                return path.read_text(encoding="utf-8")
    return _render_app(app["id"], app.get("name", "업무 관리"), app.get("description", ""), app.get("schema"))


import threading
_BROWSER_SLOT = threading.BoundedSemaphore(1)

def _browser_test(source_path, schema, app_id, output_dir):
    with _BROWSER_SLOT:
        return _browser_test_serial(source_path, schema, app_id, output_dir)

def _browser_test_serial(source_path, schema, app_id, output_dir):
    node = shutil.which("node")
    result_path = output_dir / "browser-results.json"
    config = {"source_path": str(source_path.resolve()), "output_dir": str(output_dir.resolve()),
              "app_id": app_id, "schema": schema}
    (output_dir / "browser-config.json").write_text(_json(config), encoding="utf-8")
    if not node:
        return {"passed": False, "environment": "browser unavailable", "tests": [
            {"id": "ENV-001", "name": "실제 브라우저 실행", "status": "blocked", "expected": "Node + Playwright", "actual": "Node.js 실행파일을 찾을 수 없습니다."}]}
    try:
        process = subprocess.run([node, str(Path(__file__).with_name("builder_browser.cjs")),
                                  str(output_dir / "browser-config.json")], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=110)
        (output_dir / "browser-run.log").write_text(process.stdout + "\n" + process.stderr, encoding="utf-8")
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if process.returncode != 0:
                result["passed"] = False
            return result
        error = (process.stderr or process.stdout or "브라우저 결과파일이 생성되지 않았습니다.")[-1800:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = str(exc)
    return {"passed": False, "environment": "browser unavailable or interrupted", "tests": [
        {"id": "ENV-001", "name": "실제 브라우저 실행", "status": "blocked", "expected": "실행 결과 및 캡처 생성", "actual": error}]}


def _uat(app, requirement, schema, changes, result, source_sha):
    esc = lambda value: html.escape(str(value))
    tests = result.get("tests", [])
    rows = []
    for case in tests:
        evidence = case.get("screenshot")
        image = f'<a href="{esc(evidence)}"><img src="{esc(evidence)}" alt="{esc(case.get("name", "실행 캡처"))}"></a>' if evidence else "실행 기록 참조"
        rows.append(f'<tr><td>{esc(case.get("id", ""))}</td><td>{esc(case.get("name", ""))}</td><td>{esc(case.get("expected", ""))}</td><td>{esc(case.get("actual", ""))}</td><td class="{esc(case.get("status", "blocked"))}">{esc(case.get("status", "blocked").upper())}</td><td>{image}</td></tr>')
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>UAT 결과 — {esc(app.get("name", "업무 앱"))}</title><style>
    body{{font:14px/1.65 system-ui,"Malgun Gothic",sans-serif;color:#20354a;max-width:1200px;margin:40px auto;padding:0 25px}}h1{{font-size:29px}}h2{{font-size:19px;margin-top:30px}}.eyebrow{{color:#147b70;letter-spacing:.13em;font-size:12px}}.badge{{padding:6px 12px;border-radius:8px;background:#e3f1ef;font-weight:bold}}.note{{padding:16px;background:#f2f5f9;border-left:4px solid #62809c}}table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{border:1px solid #d6e0e9;padding:12px;text-align:left;vertical-align:top}}th{{background:#edf3f7}}td img{{width:150px;max-height:140px;object-fit:contain}}.passed{{color:#15714c;font-weight:700}}.failed,.blocked{{color:#aa4330;font-weight:700}}code{{word-break:break-all}}.meta{{display:grid;grid-template-columns:170px 1fr;gap:8px}}@media print{{body{{margin:15px;font-size:11px}}tr{{break-inside:avoid}}img{{max-width:120px}}}}</style></head><body>
    <div class="eyebrow">WORKMATE / USER ACCEPTANCE TEST EVIDENCE</div><h1>업무 앱 변경 · UAT 실행 결과</h1>
    <p><span class="badge">자동 실행 결과: {"PASS" if result.get("passed") else "검토 필요"}</span> &nbsp; 최종 사용자 인수판정: <strong>미승인</strong></p>
    <div class="meta"><strong>앱 / ID</strong><span>{esc(app.get("name", ""))} / {esc(app["id"])}</span><strong>요구사항 REQ-001</strong><span>{esc(requirement)}</span><strong>실행시각 UTC</strong><span>{esc(result.get("executed_at", datetime.now(timezone.utc).isoformat()))}</span><strong>제안 소스 SHA-256</strong><code>{esc(source_sha)}</code><strong>실행 환경</strong><span>{esc(result.get("environment", "미확인"))}</span></div>
    <h2>1. 변경 내용 및 범위</h2><ul>{''.join('<li>'+esc(change)+'</li>' for change in changes) or '<li>지원 가능한 변경을 확인하지 못했습니다.</li>'}</ul>
    <p class="note">테스트 대상은 실제 생성 HTML이며, 실행하지 못한 항목은 BLOCKED로 기록합니다. 업무 API는 테스트용 메모리 저장소로 대체하여 격리했습니다. 운영 API·인증·다중 사용자·데이터베이스 통합 검증을 의미하지 않습니다. 화면 캡처는 해당 브라우저 실행에서 수집하며 통과 여부는 검증식과 실제 결과를 기준으로 기록합니다.</p>
    <h2>2. 사전조건</h2><p>검증용 업무 데이터, 빈 테스트 저장소, 총 {len(schema['fields'])}개 입력 필드, 제안 소스 <a href="app.html">app.html</a>. 상태: 대기/진행/완료. 모든 테스트는 REQ-001 및 등록·조회·수정·삭제·검색·내보내기 회귀검증에 연결됩니다.</p>
    <h2>3. 테스트 결과</h2><table><thead><tr><th>ID</th><th>시나리오</th><th>기대결과</th><th>실제결과</th><th>상태</th><th>실제 증거</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
    <h2>4. 산출물 및 추적성</h2><p><a href="change.diff">소스 변경 내역</a> · <a href="browser-results.json">구조화된 실행 결과</a> · <a href="review.json">규칙 검토 결과</a> · <a href="app-schema.json">적용 스키마</a></p>
    <h2>5. 사용자 인수판정</h2><p>검토자: ____________________　검토일: ____________________</p><p>판정: □ 승인　□ 조건부 승인　□ 반려　　의견: ________________________________________</p>
    <p class="note">자동 테스트 PASS와 현업의 UAT 승인은 구분됩니다. 확인하지 않은 업무 적합성이나 운영환경의 동작을 승인한 것으로 표시하지 않습니다.</p></body></html>'''


def develop_app(app: dict, requirement: str, output_dir: Path,
                base_url: str = "http://127.0.0.1:8765", plan: dict | None = None) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    schema, changes, warnings = _apply_requirement(app.get("schema"), requirement, plan)
    source = _render_app(app["id"], app.get("name", "업무 관리"), app.get("description", ""), schema)
    original = _old_source(app)
    if source != original and "id=\"search-records\"" not in original and any(
            word in requirement.lower() for word in ("검색", "필터", "삭제", "csv", "내보내", "디자인", "화면")):
        changes.append("업무 목록 검색·상태 필터·삭제·CSV 내보내기 화면 적용")
    source_path = output_dir / "app.html"
    source_path.write_text(source, encoding="utf-8", newline="\n")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    (output_dir / "app-schema.json").write_text(_json(schema), encoding="utf-8")
    (output_dir / "change.diff").write_text("".join(difflib.unified_diff(original.splitlines(True), source.splitlines(True),
                                                       fromfile="before/app.html", tofile="proposed/app.html")), encoding="utf-8")
    review = {"review_type": "deterministic-template-checks", "independent_llm_review": False,
              "checks": [{"name": "사용자 입력은 textContent로 렌더링", "passed": "innerHTML" not in source},
                         {"name": "레코드 API는 same-origin", "passed": "const APP_BASE='/api/apps/'" in source},
                         {"name": "허용된 필드 이름·유형·개수", "passed": len(schema["fields"]) <= 13 and all(re.fullmatch(r"[a-z][a-z0-9_]{0,31}", f["name"]) and f["name"] not in RESERVED_FIELDS and f["type"] in FIELD_TYPES for f in schema["fields"])},
                         {"name": "임의 코드 실행 없음", "passed": "eval(" not in source and "new Function(" not in source}],
              "limitations": "규칙 검토이며 독립 AI 코드리뷰 또는 포괄적 보안 검증이 아닙니다."}
    (output_dir / "review.json").write_text(_json(review), encoding="utf-8")
    if changes:
        result = _browser_test(source_path, schema, app["id"], output_dir)
    else:
        result = {"passed": False, "environment": "not run — no supported change", "tests": [
            {"id": "REQ-001", "name": "지원 요구사항 확인", "status": "blocked", "expected": "새로운 지원 변경",
             "actual": "이미 적용되었거나 지원 범위 밖의 요구입니다. 필드 추가·입력 유형·필수값·숫자 범위·완료 메모 규칙을 지원합니다."}]}
    result["expected_source_sha256"] = source_sha
    if result.get("source_sha256") and result["source_sha256"] != source_sha:
        result.setdefault("tests", []).append({"id": "EVIDENCE-001", "name": "제안 소스와 실행 소스 일치",
            "expected": source_sha, "actual": result["source_sha256"], "status": "failed"})
        result["passed"] = False
    result.setdefault("source_sha256", source_sha)
    result["api_mode"] = "isolated stateful mock; production API not exercised"
    result.setdefault("executed_at", datetime.now(timezone.utc).isoformat())
    result["passed"] = (bool(result.get("passed")) and bool(result.get("tests"))
                        and all(t.get("status") == "passed" for t in result["tests"])
                        and all(check["passed"] for check in review["checks"]))
    (output_dir / "browser-results.json").write_text(_json(result), encoding="utf-8")
    (output_dir / "uat-report.html").write_text(_uat(app, requirement, schema, changes, result, source_sha), encoding="utf-8")
    artifact_names = [("app.html", "app"), ("app-schema.json", "json"), ("change.diff", "diff"),
                      ("review.json", "json"), ("browser-results.json", "json"), ("uat-report.html", "report")]
    artifact_names.extend((p.name, "image") for p in sorted(output_dir.glob("evidence-*.png")))
    if (output_dir / "browser-run.log").exists():
        artifact_names.append(("browser-run.log", "log"))
    if (output_dir / "evidence-export.csv").exists():
        artifact_names.append(("evidence-export.csv", "csv"))
    return {"schema": schema, "summary": (" / ".join(changes) + (". 실제 브라우저 검증 통과; 사용자 인수판정은 대기 중입니다." if result["passed"] else ". 검증 결과를 확인해주세요.")) if changes else "지원 가능한 새 변경을 확인해주세요.",
            "artifacts": [{"name": name, "kind": kind} for name, kind in artifact_names],
            "tests": result.get("tests", []), "passed": result["passed"], "source_filename": "app.html",
            "source_sha256": source_sha, "changes": changes, "warnings": warnings, "review": review,
            "test_scope": "actual generated HTML + isolated stateful API mock", "uat_approval": "pending"}
