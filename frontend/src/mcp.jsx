import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowRight, Boxes, Check, CheckCircle2, ChevronDown, Clock3, Code2, Copy, Eye, EyeOff, FileSpreadsheet, FileText, KeyRound, Layers3, Link2, Loader2, LockKeyhole, Plug, Plus, RefreshCw, Search, ShieldCheck, Terminal, Unplug } from 'lucide-react';
import { api, post, dateTime } from './api';
import './mcp.css';

const list = value => Array.isArray(value) ? value : [];
const itemKey = item => `${item.kind}:${item.id}`;
const GROUPS = [
  { id: 'agent', name: '에이전트', icon: Boxes },
  { id: 'tool', name: '기본 도구', icon: Code2 },
  { id: 'app', name: '업무앱', icon: Layers3 },
  { id: 'document', name: '자료', icon: FileText },
  { id: 'file', name: 'Excel', icon: FileSpreadsheet },
];
const statusOf = publication => publication.revoked_at ? 'revoked' : new Date(publication.expires_at).getTime() <= Date.now() ? 'expired' : 'active';
const STATUS = { active: '사용 가능', revoked: '해제됨', expired: '만료됨' };
const formatTime = value => value ? dateTime(value) : '아직 사용 전';
const download = (content, filename, type = 'text/plain;charset=utf-8') => {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
async function copyText(value) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value);
  const input = document.createElement('textarea'); input.value = value; input.style.cssText = 'position:fixed;opacity:0';
  document.body.append(input); input.select(); const copied = document.execCommand('copy'); input.remove();
  if (!copied) throw Error('복사하지 못했습니다. 내용을 선택해 직접 복사해 주세요.');
}
function Button({ icon: Icon, busy, children, className = '', ...props }) {
  return <button {...props} disabled={busy || props.disabled} className={`mcp-button ${className}`}>{busy ? <Loader2 size={14} className="mcp-spin"/> : Icon ? <Icon size={14}/> : null}{children}</button>;
}
function Status({ publication }) { const status = statusOf(publication); return <span className={`mcp-status ${status}`}><i/>{STATUS[status]}</span>; }
function ScopeCounts({ selections }) {
  return <div className="mcp-scope-counts">{GROUPS.map(({ id, name, icon: Icon }) => {
    const count = list(selections).filter(item => item.kind === id).length;
    return count ? <span key={id}><Icon size={12}/>{name} <b>{count}</b></span> : null;
  })}</div>;
}

function ActivityLog({ publications }) {
  const activity = publications.flatMap(publication => list(publication.recent_activity).map(event => ({ ...event, publicationId: publication.id, publicationName: publication.name }))).sort((a, b) => new Date(b.at) - new Date(a.at)).slice(0, 8);
  return <section className="mcp-card mcp-activity"><header className="mcp-card-heading"><div><span className="mcp-eyebrow">ACTUAL MCP ACTIVITY</span><h2>외부 사용 기록</h2></div><Clock3 size={18}/></header>{activity.length ? <div className="mcp-activity-list">{activity.map((event, index) => <article key={`${event.publicationId}-${event.at}-${index}`}><span className={`mcp-activity-marker ${event.status}`}>{event.status === 'ok' ? <Check size={12}/> : <Unplug size={12}/>}</span><div><code>{event.operation}</code><p>{event.publicationName} · {dateTime(event.at)}</p></div><div><strong className={event.status}>{({ ok: '완료', rejected: '차단', failed: '실패' })[event.status] || event.status}</strong><span>{typeof event.duration_ms === 'number' ? `${Math.round(event.duration_ms)} ms` : ''}</span></div></article>)}</div> : <p className="mcp-activity-empty">외부 클라이언트가 도구나 자료를 사용하면 실제 호출 기록이 여기에 표시됩니다.</p>}<footer><ShieldCheck size={12}/>호출 이름과 처리 상태만 기록합니다. 키와 요청 본문은 표시하지 않습니다.</footer></section>;
}

function ConfirmDialog({ action, busy, onClose, onConfirm }) {
  const element = useRef(null);
  useEffect(() => {
    const previous = document.activeElement;
    element.current?.querySelector('button')?.focus();
    return () => previous?.focus?.();
  }, []);
  useEffect(() => {
    const handler = event => {
      if (event.key === 'Escape' && !busy) onClose();
      if (event.key !== 'Tab') return;
      const buttons = [...element.current.querySelectorAll('button:not(:disabled)')];
      if (!buttons.length) { event.preventDefault(); return; }
      const first = buttons[0], last = buttons.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [busy, onClose]);
  const revoke = action.kind === 'revoke';
  return <div className="mcp-modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}><section ref={element} className="mcp-modal" role="dialog" aria-modal="true" aria-labelledby="mcp-confirm-title" aria-describedby="mcp-confirm-description"><span className="mcp-modal-icon">{revoke ? <Unplug size={24}/> : <KeyRound size={24}/>}</span><h2 id="mcp-confirm-title">{revoke ? '이 연결을 해제할까요?' : '연결 키를 재발급할까요?'}</h2><p id="mcp-confirm-description"><strong>{action.publication.name}</strong>{revoke ? '의 키가 즉시 무효화됩니다. 이 연결을 사용하는 외부 AI 도구는 더 이상 접근할 수 없습니다.' : '의 이전 키가 즉시 무효화됩니다. 새 키를 복사한 뒤 외부 AI 도구의 연결 설정을 바꿔 주세요.'}</p>{!revoke && <p className="mcp-small">선택 범위와 만료일은 그대로 유지됩니다.</p>}<footer><Button onClick={onClose} disabled={!!busy}>취소</Button><Button className={revoke ? 'danger' : 'primary'} busy={!!busy} icon={revoke ? Unplug : RefreshCw} onClick={onConfirm}>{revoke ? '연결 해제' : '새 키 발급'}</Button></footer></section></div>;
}

function ConnectionExamples({ endpoint, config, onCopy }) {
  const [tab, setTab] = useState('json');
  const generic = JSON.stringify({ mcpServers: config?.mcpServers || { workmate: { url: endpoint, headers: { Authorization: 'Bearer <WORKMATE_MCP_KEY>' } } } }, null, 2);
  const curl = `# WORKMATE_MCP_KEY 환경 변수에 발급받은 키를 넣으세요.\ncurl ${JSON.stringify(endpoint)} \\\n  -H "Authorization: Bearer $WORKMATE_MCP_KEY" \\\n  -H "Content-Type: application/json" \\\n  -H "Accept: application/json, text/event-stream" \\\n  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"workmate-client","version":"1.0"}}}'`;
  const python = `# pip install "mcp==2.3.0"
# WORKMATE_MCP_KEY 환경 변수에 발급받은 키를 넣으세요.
import asyncio
import os
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": f"Bearer {os.environ['WORKMATE_MCP_KEY']}"}
    async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
        transport = streamable_http_client(
            ${JSON.stringify(endpoint)}, http_client=http
        )
        async with Client(transport, mode="legacy") as client:
            tools = await client.list_tools()
            resources = await client.list_resources()
            print([tool.name for tool in tools.tools])
            print([str(resource.uri) for resource in resources.resources])

asyncio.run(main())`;
  const examples = { json: { text: generic, file: 'workmate-mcp.json', type: 'application/json' }, curl: { text: curl, file: 'workmate-mcp.sh' }, python: { text: python, file: 'workmate-mcp.py' } };
  const current = examples[tab];
  return <section className="mcp-card mcp-examples"><header className="mcp-card-heading"><div><span className="mcp-eyebrow">CONNECT YOUR CLIENT</span><h2>다른 AI 도구에서 연결하기</h2></div><Terminal size={19}/></header><div className="mcp-connection-steps"><div><b>1</b><span>연결 키 발급</span></div><ArrowRight size={13}/><div><b>2</b><span>서버 URL · 헤더 입력</span></div><ArrowRight size={13}/><div><b>3</b><span>도구 · 자료 목록 확인</span></div></div><div className="mcp-endpoint"><span>MCP 서버 URL</span><div><code>{endpoint}</code><Button icon={Copy} onClick={() => onCopy(endpoint, '서버 URL을 복사했습니다.')}>복사</Button></div></div><p className="mcp-client-note">Streamable HTTP와 사용자 지정 Authorization 헤더를 지원하는 클라이언트에서 연결하세요. 현재 Bearer 키를 직접 입력하는 방식이며 OAuth 로그인은 지원하지 않습니다.</p><div className="mcp-code-toolbar"><div className="mcp-code-tabs" role="tablist" aria-label="MCP 연결 예제">{[['json', 'JSON 설정'], ['curl', 'cURL'], ['python', 'Python']].map(([id, label]) => <button key={id} role="tab" id={`mcp-example-tab-${id}`} aria-controls="mcp-example-code" aria-selected={tab === id} onClick={() => setTab(id)} className={tab === id ? 'active' : ''}>{label}</button>)}</div><div><Button icon={Copy} aria-label={`${tab} 예제 복사`} onClick={() => onCopy(current.text, '키가 포함되지 않은 예제를 복사했습니다.')}/><Button icon={ArrowDownToLine} aria-label={`${tab} 예제 다운로드`} onClick={() => download(current.text, current.file, current.type)}/></div></div><pre id="mcp-example-code" role="tabpanel" aria-labelledby={`mcp-example-tab-${tab}`} tabIndex={0}><code>{current.text}</code></pre><p className="mcp-code-note"><LockKeyhole size={12}/>예제에는 실제 키가 들어가지 않습니다. 클라이언트에서 발급받은 키로 바꿔 주세요.</p></section>;
}

function NewConnection({ catalog, initialAgentId, onAgentConsumed, busy, blocked, onCreate }) {
  const [name, setName] = useState(''), [days, setDays] = useState(30), [selected, setSelected] = useState([]), [includeContext, setIncludeContext] = useState(false), [group, setGroup] = useState('agent'), [search, setSearch] = useState('');
  const consumed = useRef('');
  const items = list(catalog.items), limit = catalog.limits?.selections || 40;
  useEffect(() => {
    if (!initialAgentId || consumed.current === initialAgentId) return;
    const agent = items.find(item => item.kind === 'agent' && item.id === initialAgentId);
    if (!agent) return;
    consumed.current = initialAgentId; setSelected([itemKey(agent)]); setName(`${agent.name} 연결`); setGroup('agent'); onAgentConsumed?.();
  }, [initialAgentId, items, onAgentConsumed]);
  const chosen = items.filter(item => selected.includes(itemKey(item)));
  const needsExcelFiles = chosen.some(item => item.kind === 'tool' && item.id === 'excel') && chosen.filter(item => item.kind === 'file').length < 2;
  const documentCount = chosen.filter(item => item.kind === 'document').length;
  const needsKnowledgeDocument = chosen.some(item => item.kind === 'tool' && item.id === 'knowledge') && documentCount < 1;
  const needsComparisonDocuments = chosen.some(item => item.kind === 'tool' && item.id === 'analyst') && documentCount < 2;
  const missingRequiredScope = needsExcelFiles || needsKnowledgeDocument || needsComparisonDocuments;
  const visible = items.filter(item => item.kind === group && `${item.name} ${item.description || ''}`.toLowerCase().includes(search.toLowerCase()));
  const toggle = item => setSelected(current => current.includes(itemKey(item)) ? current.filter(key => key !== itemKey(item)) : current.length < limit ? [...current, itemKey(item)] : current);
  return <section className="mcp-card mcp-create" id="mcp-create"><header className="mcp-card-heading"><div><span className="mcp-eyebrow">NEW CONNECTION</span><h2>새 MCP 연결 만들기</h2></div><Plus size={19}/></header><form onSubmit={async event => { event.preventDefault(); if (missingRequiredScope || blocked) return; const created = await onCreate({ name: name.trim(), expires_days: days, selections: chosen.map(({ kind, id }) => ({ kind, id })), include_context: includeContext }); if (created) { setName(''); setSelected([]); setIncludeContext(false); } }}><div className="mcp-form-top"><label htmlFor="mcp-name">연결 이름<input id="mcp-name" placeholder="예: 개발 도구의 Workmate" maxLength={80} required value={name} onChange={event => setName(event.target.value)} disabled={busy || blocked}/></label><label htmlFor="mcp-expiry">키 유효 기간<select id="mcp-expiry" value={days} onChange={event => setDays(Number(event.target.value))} disabled={busy || blocked}>{[7, 30, 90].filter(value => value <= (catalog.limits?.expires_days || 90)).map(value => <option key={value} value={value}>{value}일</option>)}</select></label></div><div className="mcp-selection-heading"><div><h3>외부에 제공할 항목</h3><p>선택한 항목만 이 연결에서 사용할 수 있습니다.</p></div><span>{selected.length}<small> / {limit}</small></span></div><div className="mcp-kind-tabs" role="tablist" aria-label="공개 항목 종류">{GROUPS.map(({ id, name: label, icon: Icon }) => <button type="button" key={id} role="tab" aria-selected={group === id} aria-controls="mcp-catalog-items" id={`mcp-kind-${id}`} className={group === id ? 'active' : ''} onClick={() => { setGroup(id); setSearch(''); }}><Icon size={13}/>{label}<span>{items.filter(item => item.kind === id).length}</span></button>)}</div><label className="mcp-search"><Search size={14}/><input value={search} onChange={event => setSearch(event.target.value)} placeholder={`${GROUPS.find(item => item.id === group)?.name} 검색`} aria-label="공개할 항목 검색"/></label><div className="mcp-catalog" id="mcp-catalog-items" role="tabpanel" aria-labelledby={`mcp-kind-${group}`}>{visible.map(item => <label key={itemKey(item)} className={selected.includes(itemKey(item)) ? 'selected' : ''}><input type="checkbox" checked={selected.includes(itemKey(item))} disabled={busy || blocked || (!selected.includes(itemKey(item)) && selected.length >= limit)} onChange={() => toggle(item)}/><span><strong>{item.name}{item.kind === 'agent' && <em>{item.builtin ? '기본' : '내 에이전트'}</em>}</strong><small>{item.description || (item.kind === 'document' ? '선택한 자료의 현재 내용에 읽기 접근' : item.kind === 'app' ? '선택한 업무앱의 현재 데이터에 읽기 접근' : '외부 MCP 클라이언트에서 사용')}</small></span></label>)}{!visible.length && <p className="mcp-catalog-empty">{search ? '검색 결과가 없습니다.' : `연결할 ${GROUPS.find(item => item.id === group)?.name} 항목이 없습니다.`}</p>}</div>{chosen.length > 0 && <div className="mcp-selected-summary"><ScopeCounts selections={chosen}/><button type="button" onClick={() => setSelected([])} disabled={busy || blocked}>선택 지우기</button></div>}{missingRequiredScope&&<p className="mcp-prerequisite" role="status">{needsExcelFiles&&<>Excel 검증을 실행하려면 Excel 탭에서 기준 파일과 비교할 파일을 2개 이상 선택하세요.</>}{needsExcelFiles&&(needsKnowledgeDocument||needsComparisonDocuments)&&<br/>}{needsComparisonDocuments?<>자료 비교 도구를 사용하려면 자료 탭에서 비교할 문서를 2개 이상 선택하세요.</>:needsKnowledgeDocument&&<>자료 질의 도구를 사용하려면 자료 탭에서 문서를 함께 선택하세요.</>}</p>}<label className="mcp-context-optin"><input type="checkbox" checked={includeContext} onChange={event => setIncludeContext(event.target.checked)} disabled={busy || blocked}/><span><strong>에이전트의 저장된 업무 맥락도 포함</strong><small>프로필에 연결된 자료·업무·기억·작성 선호를 발급 시점의 사본으로 포함합니다. 민감한 내용이 있을 수 있으므로 필요한 경우에만 선택하세요.</small></span></label><div className="mcp-scope-note"><ShieldCheck size={15}/><p>{catalog.context_policy || '에이전트의 저장된 맥락은 기본으로 제외됩니다. 선택한 자료와 업무앱은 현재 내용을 읽을 수 있습니다.'}</p></div><footer><span>읽기 · 분석 범위<br/><small>업무앱 데이터 쓰기 · 자동화 실행 제외</small></span><Button type="submit" icon={KeyRound} className="primary" busy={busy} disabled={blocked || !name.trim() || !chosen.length || missingRequiredScope}>연결 키 발급</Button></footer></form></section>;
}

export function McpPage({ notify = () => {}, initialAgentId = '', onAgentConsumed, navigate }) {
  const [catalog, setCatalog] = useState(null), [publications, setPublications] = useState([]), [config, setConfig] = useState(null), [loading, setLoading] = useState(true), [error, setError] = useState(''), [busy, setBusy] = useState(''), [action, setAction] = useState(null), [issued, setIssued] = useState(null), [showKey, setShowKey] = useState(false);
  const mounted = useRef(true), issuedRef = useRef(null), actionLock = useRef(false);
  const reload = useCallback(async () => {
    const results = await Promise.allSettled([api('/mcp/catalog'), api('/mcp/publications'), api('/mcp/config')]);
    if (!mounted.current) return;
    if (results[0].status === 'fulfilled') setCatalog(results[0].value);
    if (results[1].status === 'fulfilled') setPublications(list(results[1].value.publications));
    if (results[2].status === 'fulfilled') setConfig(results[2].value);
    const failure = results.find(result => result.status === 'rejected');
    if (failure) throw failure.reason;
    setError('');
  }, []);
  useEffect(() => { mounted.current = true; reload().catch(reason => { if (mounted.current) setError(reason.message); }).finally(() => { if (mounted.current) setLoading(false); }); return () => { mounted.current = false; }; }, [reload]);
  const copy = async (value, message) => { try { await copyText(value); notify(message); } catch (reason) { setError(reason.message); } };
  const updatePublication = publication => setPublications(current => [publication, ...current.filter(item => item.id !== publication.id)]);
  const revealIssued = result => { updatePublication(result.publication); setIssued(result); setShowKey(false); setTimeout(() => issuedRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50); };
  const create = async body => {
    if (actionLock.current) return false;
    actionLock.current = true; setBusy('create'); setError('');
    try { const result = await post('/mcp/publications', body); if (mounted.current) { revealIssued(result); notify('MCP 연결 키를 발급했습니다. 지금 복사해 보관해 주세요.'); } return true; }
    catch (reason) { if (mounted.current) setError(reason.message); return false; }
    finally { actionLock.current = false; if (mounted.current) setBusy(''); }
  };
  const confirm = async () => {
    if (!action || actionLock.current) return;
    actionLock.current = true; setBusy(`${action.kind}-${action.publication.id}`); setError('');
    try {
      const result = await post(`/mcp/publications/${encodeURIComponent(action.publication.id)}/${action.kind}`);
      if (!mounted.current) return;
      if (action.kind === 'rotate') { revealIssued(result); notify('새 키를 발급했습니다. 외부 도구의 키도 변경해 주세요.'); }
      else { updatePublication(result.publication); if (issued?.publication.id === action.publication.id) setIssued(null); notify('MCP 연결을 해제했습니다.'); }
      setAction(null);
    } catch (reason) { if (mounted.current) { setAction(null); setError(reason.message); } }
    finally { actionLock.current = false; if (mounted.current) setBusy(''); }
  };
  const endpoint = config?.mcpServers?.workmate?.url || new URL(config?.endpoint || catalog?.endpoint || '/mcp/', window.location.origin).href;
  const active = publications.filter(publication => statusOf(publication) === 'active');
  const limitReached = active.length >= (catalog?.limits?.publications || 20);
  return <div className="mcp-page"><header className="mcp-page-heading"><div><span className="mcp-eyebrow">WORKMATE · MODEL CONTEXT PROTOCOL</span><h1>내 에이전트를 다른 AI 도구에서도.</h1><p>Workmate에서 만든 에이전트와 자료를 필요한 범위만 골라 연결하세요.</p></div><span className="mcp-header-symbol"><Plug size={27}/></span></header><div className="mcp-overview"><div><span>사용 가능한 연결</span><strong>{active.length}<small> / {catalog?.limits?.publications || 20}</small></strong></div><div><span>연결 방식</span><strong>Streamable HTTP</strong></div><div><span>접근 제어</span><strong><LockKeyhole size={15}/>개별 키 · 선택 범위</strong></div></div>{error && <div className="mcp-error" role="alert"><span>{error}</span><Button icon={RefreshCw} busy={busy === 'reload'} disabled={!!busy} onClick={async () => { setBusy('reload'); try { await reload(); } catch (reason) { setError(reason.message); } finally { setBusy(''); } }}>다시 불러오기</Button></div>}{loading ? <div className="mcp-loading" role="status"><Loader2 className="mcp-spin" size={21}/>MCP 연결 설정을 불러오고 있습니다.</div> : catalog ? <>{catalog.hosted && <div className="mcp-session-note"><Clock3 size={16}/><p>온라인 MCP 연결은 현재 방문 세션에 속합니다. 1시간 동안 활동이 없거나 서버가 다시 시작되면 만료일 전에도 종료됩니다. 지속적인 연결은 직접 실행한 Workmate 서버에서 사용하세요.</p></div>}{issued && <section className="mcp-issued" ref={issuedRef} aria-label="새로 발급한 MCP 연결 키"><header><div><CheckCircle2 size={21}/><div><h2>연결 키가 준비됐습니다</h2><p>{issued.publication.name} · 이 화면에서만 다시 볼 수 있습니다.</p></div></div><span className="mcp-once">한 번만 표시</span></header><label htmlFor="mcp-access-key">연결 키</label><div className="mcp-key-input"><input id="mcp-access-key" type={showKey ? 'text' : 'password'} value={issued.access_key} readOnly autoComplete="off" spellCheck={false}/><Button icon={showKey ? EyeOff : Eye} aria-label={showKey ? '연결 키 숨기기' : '연결 키 표시'} onClick={() => setShowKey(value => !value)}/><Button icon={Copy} onClick={() => copy(issued.access_key, '연결 키를 복사했습니다.')}>키 복사</Button></div><p className="mcp-issued-note">외부 AI 도구의 Authorization 헤더에 <code>Bearer 발급받은키</code> 형식으로 넣으세요. 화면을 떠나거나 보관 완료를 누르면 키를 다시 조회할 수 없습니다.</p>{issued.publication.include_context && <div className="mcp-context-summary">함께 포함한 맥락: {Object.entries(issued.publication.context_summary || {}).map(([key, value]) => `${({ documents: '자료', tasks: '업무', memories: '기억', writing_preferences: '작성 선호' })[key] || key} ${typeof value === 'number' ? value : Array.isArray(value) ? value.length : value ? 1 : 0}개`).join(' · ')}</div>}<footer><span><ShieldCheck size={13}/>키를 가진 도구는 아래 선택 범위에 접근할 수 있습니다.</span><Button icon={Check} onClick={() => { setIssued(null); setShowKey(false); }}>키 보관 완료</Button></footer></section>}<div className="mcp-main-grid"><div className="mcp-main-column"><section className="mcp-card mcp-publications"><header className="mcp-card-heading"><div><span className="mcp-eyebrow">YOUR CONNECTIONS</span><h2>연결 관리 <span>{publications.length}</span></h2></div><Button icon={RefreshCw} busy={busy === 'reload'} disabled={!!busy} aria-label="MCP 연결 목록 새로고침" onClick={async () => { setBusy('reload'); try { await reload(); } catch (reason) { setError(reason.message); } finally { setBusy(''); } }}/></header>{!publications.length ? <div className="mcp-empty"><span><Link2 size={26}/></span><h3>업무 맥락이 이어지는 연결</h3><p>새 연결에서 에이전트나 자료를 선택하고<br/>발급받은 키를 외부 AI 도구에 등록하세요.</p><div><Boxes size={17}/><span>Workmate</span><span className="mcp-flow-line"/><Plug size={17}/><span>다른 AI 도구</span></div></div> : <div className="mcp-publication-list">{publications.map(publication => <article key={publication.id}><header><div className="mcp-publication-icon"><Plug size={18}/></div><div><h3>{publication.name}</h3><span><code>{publication.key_prefix ? `${publication.key_prefix}…` : '개별 연결 키'}</code> · {dateTime(publication.created_at)} 생성</span></div><Status publication={publication}/></header><ScopeCounts selections={publication.selections}/><dl><div><dt>마지막 사용</dt><dd>{formatTime(publication.last_used_at)}</dd></div><div><dt>키 만료일</dt><dd>{dateTime(publication.expires_at)}</dd></div><div><dt>요청 횟수</dt><dd>{publication.request_count || 0}회</dd></div></dl><details><summary>공개 범위 확인 <ChevronDown size={13}/></summary><ul>{list(publication.selections).map(item => <li key={itemKey(item)}><span>{GROUPS.find(group => group.id === item.kind)?.name || item.kind}</span>{item.name || item.id}</li>)}</ul><p>{publication.include_context ? '에이전트의 저장된 업무 맥락 사본 포함' : '에이전트의 저장된 업무 맥락 제외'} · 선택한 자료·업무앱은 현재 내용에 읽기 접근</p></details><footer><span><ShieldCheck size={12}/>{publication.include_context ? '맥락 포함' : '선택 범위만'}</span><div><Button icon={RefreshCw} disabled={!!busy || !!issued || statusOf(publication) !== 'active'} onClick={() => setAction({ kind: 'rotate', publication })}>키 재발급</Button><Button icon={Unplug} className="quiet" disabled={!!busy || statusOf(publication) !== 'active'} onClick={() => setAction({ kind: 'revoke', publication })}>해제</Button></div></footer></article>)}</div>}</section><ActivityLog publications={publications}/><ConnectionExamples endpoint={endpoint} config={config} onCopy={copy}/></div><div className="mcp-side-column">{limitReached ? <section className="mcp-card mcp-limit"><ShieldCheck size={23}/><h2>연결 수 한도에 도달했습니다</h2><p>사용하지 않는 연결을 해제하면 새 연결을 만들 수 있습니다.</p></section> : <NewConnection catalog={catalog} initialAgentId={initialAgentId} onAgentConsumed={onAgentConsumed} busy={!!busy} blocked={!!issued} onCreate={create}/>}<aside className="mcp-mini-note"><span><Boxes size={17}/></span><div><strong>작업하는 화면 곁에는 Workmate Mini</strong><p>Windows 데스크톱에서도 에이전트와 함께 일하세요.</p><a href="https://sagytic.github.io/workmate/#desktop-mini" target="_blank" rel="noreferrer">Mini 살펴보기 <ArrowRight size={12}/></a></div></aside></div></div></> : <div className="mcp-unavailable"><Plug size={24}/><h2>MCP 설정을 불러오지 못했습니다</h2><p>서버 연결 상태를 확인하고 다시 불러와 주세요.</p></div>}{action && <ConfirmDialog action={action} busy={busy} onClose={() => setAction(null)} onConfirm={confirm}/>}</div>;
}
