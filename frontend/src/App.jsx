import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowRight, ArrowUp, Bell, BookOpen, Boxes, Braces, Check, CheckCheck, CheckCircle2, ChevronDown, ChevronRight, Circle, Clock3, Code2, ExternalLink, FileCheck2, FileSpreadsheet, FileText, FolderOpen, GitBranch, History, Home, Layers3, ListChecks, Loader2, Menu, Moon, Sun, MoreHorizontal, Play, Plus, RefreshCw, Search, Settings2, ShieldCheck, SquarePen, Trash2, Upload, UserRound, Workflow, X, XCircle, AlertTriangle, Zap } from 'lucide-react';
import { api, post, asText, dateTime } from './api';
import { HomeDesk, DocumentsPage, AnalysisPage, ExamplesPage, DeskResult, TaskMetadata, GlobalSearch, TaskNotes } from './desk';
import { HomeV2, ConversationPage, ExcelV2, FilesPage, FilePicker as FileInput, AutomationV2, TaskControls, BatchResults, DynamicField, RegistrationForm } from './enhancements';
import {Companion,TodayPlan} from './companion';
import {TicketsPage} from './tickets';
import {ConnectionSettings} from './connection';
import {AgentHub} from './agenthub';
import './studio-integration.css';

const NAV = [
  ['agents', '에이전트 스튜디오', Boxes, '나만의 에이전트와 협업 미션'],
  ['conversation', '업무 요청', History, '요청과 답변 기록'],
  ['files', 'Excel 파일', FolderOpen, '업로드 파일과 예제'],
  ['documents', '자료실', BookOpen, '근거 자료'],
  ['analysis', '자료 분석', FileCheck2, '질의 · 비교 · 보고'],
  ['examples', '예제 모음', Layers3, '바로 실행할 업무 예제'],
  ['home', '오늘의 업무', Home, '진행 중인 업무'],
  ['excel', 'Excel 검증', FileSpreadsheet, '수식과 변경 사항을 확인합니다'],
  ['builder', '업무앱 제작', Boxes, '설명을 실제 업무앱으로 만듭니다'],
  ['developer', '개발 · UAT', Code2, '변경을 구현하고 검증합니다'],
  ['tickets', '개발 티켓 · 결과서', FileCheck2, '요청자의 인수 검토를 준비합니다'],
  ['automation', '업무 자동화', Workflow, '반복 업무를 실행 절차로 만듭니다'],
  ['memory', '내 업무 기억', BookOpen, '업무 맥락과 선호를 관리합니다'],
];
NAV.unshift(NAV.splice(NAV.findIndex(n=>n[0]==='home'),1)[0]);
const AGENTS = { assistant: ['개인 업무', ListChecks], excel: ['Excel 검증', FileSpreadsheet], builder: ['업무앱 제작', Boxes], developer: ['개발 · UAT', Code2], automation: ['업무 자동화', Workflow], knowledge: ['자료 질의', BookOpen], analyst: ['변경 분석', FileCheck2], reporter: ['보고서', FileText] };
const STATUS = { todo: ['할 일', 'neutral'], in_progress: ['진행 중', 'blue'], queued: ['대기', 'neutral'], running: ['실행 중', 'blue'], waiting_input: ['확인 필요', 'amber'], completed: ['완료', 'green'], failed: ['실패', 'red'] };
const emptyState = { documents: [], projects: [], examples: [], references: [], tasks: [], archived_tasks: [], uploads: [], chat_history: [], apps: [], workflows: [], memories: [], notifications: [], fixtures: [], stats: {}, agents: [] };
const initialTab = () => NAV.some(n => n[0] === window.location.hash.slice(1)) ? window.location.hash.slice(1) : 'home';
const resultObject = result => { if (typeof result === 'string') { try { return JSON.parse(result); } catch { return { summary: result }; } } return result || {}; };
const safeUrl = url => typeof url === 'string' && (url.startsWith('/api/') || /^https?:\/\//i.test(url)) ? url : '#';

function IconBox({ icon: Icon = ListChecks, color = 'teal', size = '' }) { return <span className={`icon-box ${color} ${size}`}><Icon size={size === 'small' ? 16 : 21}/></span>; }
function Badge({ status }) { const [label, color] = STATUS[status] || [status || '대기', 'neutral']; return <span className={`badge ${color}`}><span className={status === 'running' ? 'status-dot pulse' : 'status-dot'}/>{label}</span>; }
function Button({ children, icon: Icon, busy, className = '', ...props }) { return <button className={`button ${className}`} disabled={busy || props.disabled} {...props}>{busy ? <Loader2 size={16} className="spin"/> : Icon ? <Icon size={16}/> : null}{children}</button>; }
function Empty({ icon: Icon = FolderOpen, title, children }) { return <div className="empty"><Icon size={29}/><strong>{title}</strong>{children && <p>{children}</p>}</div>; }
function SectionHead({ eyebrow, title, description, right }) { return <div className="section-heading"><div><h1>{title}</h1>{description && <p>{description}</p>}</div>{right}</div>; }
function Modal({ title, subtitle, onClose, wide, children }) {
  useEffect(() => { const listener = event => { if (event.key === 'Escape' && !document.querySelector('.global-search-modal')) onClose(); }; document.addEventListener('keydown', listener); const prior = document.body.style.overflow; document.body.style.overflow = 'hidden'; return () => { document.removeEventListener('keydown', listener); document.body.style.overflow = prior; }; }, [onClose]);
  return <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><section className={`modal ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}><header className="modal-head"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="icon-button" onClick={onClose} aria-label="닫기"><X size={21}/></button></header><div className="modal-body">{children}</div></section></div>;
}

export default function App() {
  const [tab, setTab] = useState(initialTab);
  const [hubRunId,setHubRunId]=useState(()=>new URLSearchParams(window.location.search).get('hub_run'));
  const [theme,setTheme]=useState(()=>document.documentElement.dataset.theme==='light'?'light':'dark');
  useEffect(()=>{document.documentElement.dataset.theme=theme;document.querySelector('meta[name="theme-color"]')?.setAttribute('content',theme==='dark'?'#11151c':'#f7f8fa');try{localStorage.setItem('workmate-theme',theme);}catch{}},[theme]);
  useEffect(()=>{const sync=e=>{if(e.key==='workmate-theme')setTheme(e.newValue==='light'?'light':'dark');};window.addEventListener('storage',sync);return()=>window.removeEventListener('storage',sync);},[]);
  const [prepared, setPrepared] = useState(null);
  const [documentId, setDocumentId] = useState(() => new URLSearchParams(window.location.search).get('document'));
  const [searchOpen, setSearchOpen] = useState(false);
  useEffect(() => { const handler=e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();setSearchOpen(v=>!v);}};document.addEventListener('keydown',handler);return()=>document.removeEventListener('keydown',handler);},[]);
  const [state, setState] = useState(emptyState);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [connectionError, setConnectionError] = useState('');
  const [toast, setToast] = useState(null);
  const [selectedTask, setSelectedTask] = useState(null);
  const [taskDetail, setTaskDetail] = useState(null);
  const [recordsApp, setRecordsApp] = useState(null);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [busy, setBusy] = useState('');
  const stateRef = useRef(state);
  stateRef.current = state;
  const notify = useCallback((message, error = false) => setToast({ message, error, id: Date.now() }), []);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(null), 6500); return () => clearTimeout(timer); }, [toast]);
  const refresh = useCallback(async () => {
    try {
      const data = await api('/state');
      setState({ ...emptyState, ...data, tasks: data.tasks || [], apps: data.apps || [], workflows: data.workflows || [], memories: data.memories || [], notifications: data.notifications || [], fixtures: data.fixtures || [] });
      setConnected(true); setConnectionError('');
      return data;
    } catch (error) { setConnectionError(error.message); setConnected(false); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    let timer, cancelled = false;
    const poll = async () => { await refresh(); if (!cancelled) timer = setTimeout(poll, stateRef.current.tasks.some(t => ['queued', 'running'].includes(t.status)) ? 2000 : 10000); };
    poll(); return () => { cancelled = true; clearTimeout(timer); };
  }, [refresh]);
  useEffect(() => { const change = () => setTab(initialTab()); window.addEventListener('hashchange', change); return () => window.removeEventListener('hashchange', change); }, []);
  const navigate = next => { if(new URLSearchParams(window.location.search).get('companion')==='1'){window.open(`/#${next}`,'workmate-main');return;} setTab(next); window.location.hash = next; setSidebarOpen(false); window.scrollTo(0,0); };
  const perform = async (key, action, successMessage) => {
    setBusy(key);
    try { const data = await action(); await refresh(); if (successMessage) notify(successMessage); return data; }
    catch (error) { notify(error.message, true); return null; }
    finally { setBusy(''); }
  };
  const showTask = useCallback(async id => {
    if (!id) return;
    setSelectedTask(id); setTaskDetail(null);
    try { setTaskDetail(await api(`/tasks/${encodeURIComponent(id)}`)); } catch (error) { notify(error.message, true); }
  }, [notify]);
  useEffect(() => {
    if (!selectedTask) return;
    let valid = true;
    api(`/tasks/${encodeURIComponent(selectedTask)}`).then(data => { if (valid) setTaskDetail(data); }).catch(() => {});
    return () => { valid = false; };
  }, [selectedTask, state]);
  const startTask = async (agent, input, title) => {
    const task = await perform(`start-${agent}`, () => post('/tasks', { agent, input, ...(input.project ? { project: input.project } : {}), ...(title ? { title } : {}) }));
    if (task?.id) showTask(task.id);
    return task;
  };
  const closeTask = useCallback(() => { setSelectedTask(null); setTaskDetail(null); }, []);
  const unread = state.notifications.filter(n => !n.read && !n.read_at).length;
  const currentNav = NAV.find(n => n[0] === tab) || NAV[0];
  const openDocument = id => { if(new URLSearchParams(window.location.search).get('companion')==='1'){window.open(`/?document=${encodeURIComponent(id)}#documents`,'workmate-main');return;} setDocumentId(id); closeTask(); navigate('documents'); };
  const prepareExample = example => { setPrepared({agent:example.agent,input:example.input||{},project:example.project,title:example.title}); const target=({excel:'excel',builder:'builder',developer:'developer',automation:'automation',knowledge:'analysis',analyst:'analysis',reporter:'analysis',assistant:'conversation'})[example.agent]||'conversation'; navigate(target); notify('예제 입력을 준비했습니다. 내용을 확인하고 실행하세요.'); };
  const showHubRun = id => {if(new URLSearchParams(window.location.search).get('companion')==='1'){window.open(`/?hub_run=${encodeURIComponent(id)}#agents`,'workmate-main');return;} setHubRunId(id);closeTask();navigate('agents');};
  const props = { state, showHubRun, refresh, busy, perform, notify, showTask, startTask, navigate, connected, prepared, setPrepared, documentId, setDocumentId, openDocument, prepareExample };

  if(new URLSearchParams(window.location.search).get('companion')==='1')return <>
    <Companion {...props} mini/>
    {selectedTask && <TaskModal task={taskDetail || [...state.tasks, ...(state.archived_tasks || [])].find(t => t.id === selectedTask)} onClose={closeTask} {...props}/>}
    {toast && <div className={`toast ${toast.error ? 'error' : ''}`} role="status"><span>{toast.message}</span><button onClick={() => setToast(null)} aria-label="알림 닫기"><X size={17}/></button></div>}
  </>;

  return <div className="workspace">
    {sidebarOpen && <div className="sidebar-scrim" onClick={() => setSidebarOpen(false)}/>}
    <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
      <button className="brand" onClick={() => navigate('home')}><span className="brand-mark"><Layers3 size={23}/></span><span>workmate<span className="brand-dot">.</span></span></button>
      <div className="workspace-switch"><span className="workspace-avatar">W</span><div><strong>내 업무 워크스페이스</strong><span>개인 워크스페이스</span></div><ShieldCheck size={16}/></div>
      <span className="nav-caption">업무 공간</span>
      <nav>{NAV.map(([id, label, Icon]) => <button key={id} aria-label={label} aria-current={tab===id?"page":undefined} className={`nav-item ${tab === id ? 'active' : ''}`} onClick={() => navigate(id)}><Icon size={19}/><span>{label}</span>{id === 'home' && state.stats?.waiting > 0 && <span className="nav-count">{state.stats.waiting}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="local-note"><span className={`live-dot ${connected ? '' : 'offline'}`}/><strong>{connected ? (state.hosted?'온라인 워크스페이스 연결됨':'로컬 워크스페이스 연결됨') : '서버 연결 확인 중'}</strong><p>{state.hosted?'방문 세션별로 분리됩니다. 필요한 결과는 다운로드해 보관하세요.':'작업 이력과 결과를 이 기기에 보관합니다.'}</p></div><div className="profile"><span className="profile-avatar"><UserRound size={18}/></span><div><strong>내 업무 공간</strong><span>Workmate · v0.6</span></div></div></div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><div className="breadcrumb"><button className="icon-button mobile-menu" onClick={() => setSidebarOpen(true)} aria-label="메뉴 열기"><Menu size={21}/></button><span>워크스페이스</span><ChevronRight size={14}/><strong>{currentNav[1]}</strong></div><div className="topbar-actions"><button className="icon-button theme-toggle" aria-label="화면 테마 전환" title={theme==='dark'?'라이트 모드로 전환':'다크 모드로 전환'} aria-pressed={theme==='light'} onClick={()=>setTheme(theme==='dark'?'light':'dark')}>{theme==='dark'?<Sun size={15}/>:<Moon size={15}/>}</button><button className="global-search-trigger" onClick={() => setSearchOpen(true)} aria-label="전체 검색 열기"><Search size={15}/><span>검색</span><kbd>Ctrl K</kbd></button><button className={`mode-chip ${state.llm?.status === 'connected' ? 'live' : ''}`} onClick={() => setConnectionOpen(true)} title="연결 상태 확인"><span/>{!connected ? '연결 대기' : state.llm?.status === 'connected' ? 'API 연결됨' : state.llm?.status === 'error' ? '연결 오류' : state.llm_configured ? '모델 설정됨' : '규칙 기반'}</button><button className="icon-button bell-button" onClick={() => setNotificationsOpen(!notificationsOpen)} aria-label={`알림 ${unread}개`}><Bell size={20}/>{unread > 0 && <i>{unread}</i>}</button></div>
        {notificationsOpen && <div className="notification-panel"><div className="card-heading"><h3>업무 알림 {unread > 0 && <span className="subtle-count">{unread}</span>}</h3><Button icon={RefreshCw} className="text-button small" busy={busy === 'notifications'} onClick={() => perform('notifications', () => post('/notifications/check'), '현재 기한을 기준으로 알림을 확인했습니다.')}>지금 확인</Button></div>{state.notifications.length ? <div className="notification-list">{state.notifications.map(n => <button key={n.id} className={`notification-row ${!n.read && !n.read_at ? 'unread' : ''}`} onClick={async () => { await perform(`read-${n.id}`, () => post(`/notifications/${n.id}/read`)); if (n.task_id) { showTask(n.task_id); setNotificationsOpen(false); } }}><span className="notification-mark"/><div><strong>{asText(n.title || n.message || '업무 알림')}</strong>{n.title && n.message && <p>{asText(n.message)}</p>}<time>{dateTime(n.created_at || n.at)}</time></div>{!n.read && !n.read_at && <Check size={14}/>}</button>)}</div> : <Empty icon={Bell} title="새 알림이 없습니다">확인이 필요한 변화가 생기면 알려드려요.</Empty>}</div>}
      </header>
      <main className="main-content">
        {connectionError && <div className="connection-alert"><AlertTriangle size={17}/><span>{connectionError}</span><button onClick={refresh}>다시 연결</button></div>}
        {loading ? <div className="initial-loading"><Loader2 className="spin" size={26}/><p>내 업무 공간을 불러오고 있어요</p></div> : <>
          {tab === 'home' && <><TodayPlan {...props}/><section className="hub-home-strip"><div><span>AGENT STUDIO</span><h3>하나의 요청, 여러 전문 에이전트.</h3><p>회의 후속 조치 · 의사결정 · 프로젝트 레이더 · 주간 회고</p></div><button className="button secondary" onClick={()=>navigate('agents')}>에이전트에게 맡기기 <ArrowRight size={15}/></button></section><HomeDesk {...props}/></>}
          {tab === 'agents' && <AgentHub {...props} initialRunId={hubRunId}/>}
          {tab === 'excel' && <ExcelV2 {...props}/>}
          {tab === 'builder' && <BuilderPage {...props} openRecords={setRecordsApp}/>}
          {tab === 'developer' && <DeveloperPage {...props}/>}
          {tab === 'tickets' && <TicketsPage {...props}/>}
          {tab === 'automation' && <AutomationV2 {...props}/>}
          {tab === 'memory' && <MemoryPage {...props}/>}
          {tab === 'conversation' && <ConversationPage {...props}/>}
          {tab === 'files' && <FilesPage {...props}/>}
          {tab === 'documents' && <DocumentsPage {...props}/>}
          {tab === 'analysis' && <AnalysisPage {...props}/>}
          {tab === 'examples' && <ExamplesPage {...props}/>}
        </>}
        <footer className="page-footer"><span><ShieldCheck size={13}/> 실행 단계 · 근거 · 결과물을 함께 보관합니다</span><span>Workmate · v0.6</span></footer>
      </main>
    </div>
    <GlobalSearch open={searchOpen} onClose={() => setSearchOpen(false)} {...props}/>
    <Companion {...props}/>
    {selectedTask && <TaskModal task={taskDetail || [...state.tasks, ...(state.archived_tasks || [])].find(t => t.id === selectedTask)} onClose={closeTask} {...props}/>}
    {connectionOpen && <ConnectionSettings onClose={()=>setConnectionOpen(false)} onSaved={refresh}/>}
    {recordsApp && <RecordsModal app={recordsApp} onClose={() => setRecordsApp(null)} {...props}/>}
    {toast && <div className={`toast ${toast.error ? 'error' : ''}`} role="status">{toast.error ? <AlertTriangle size={19}/> : <CheckCircle2 size={19}/>}<span>{toast.message}</span><button onClick={() => setToast(null)} aria-label="알림 닫기"><X size={17}/></button></div>}
  </div>;
}

function TaskList({ tasks, showTask }) {
  if (!tasks.length) return <Empty icon={ListChecks} title="표시할 업무가 없습니다">새 업무를 맡기거나 다른 상태를 선택해 주세요.</Empty>;
  return <div className="task-list">{tasks.map(task => { const [label, Icon] = AGENTS[task.agent] || ['업무', ListChecks]; return <button className="task-row" key={task.id} onClick={() => showTask(task.id)}><IconBox icon={Icon} color={task.status === 'waiting_input' ? 'amber' : task.agent === 'developer' ? 'purple' : 'teal'} size="small"/><div className="task-row-copy"><strong>{task.title || label}</strong><p>{asText(task.summary || resultObject(task.result).summary || label)}</p><div className="task-meta"><span>{label}</span><i/> <time>{dateTime(task.updated_at || task.created_at)}</time>{task.artifacts?.length > 0 && <span className="attachment-count"><FileText size={12}/>{task.artifacts.length}</span>}</div></div><Badge status={task.status}/><ChevronRight className="task-chevron" size={16}/></button>; })}</div>;
}

function BuilderPage({ state, startTask, busy, showTask, openRecords, connected, prepared, setPrepared }) {
  const [prompt, setPrompt] = useState('프로젝트 산출물을 접수하고 검수하는 앱을 만들어줘. 산출물명, 담당자, 마감일, 상태, 완료 메모를 관리하고 싶어.');
  useEffect(()=>{if(prepared?.agent==='builder'){setPrompt(prepared.input?.prompt||'');setPrepared(null);}},[prepared,setPrepared]);
  return <><SectionHead eyebrow="BUSINESS APP BUILDER" title="업무앱 제작" description="필요한 입력 항목과 규칙을 설명해 사용할 앱을 만듭니다."/>
    <div className="builder-grid"><section className="card padded-card"><div className="card-heading flush"><div><h2>어떤 앱이 필요한가요?</h2><p>입력 항목과 처리 단계를 알려주세요.</p></div><IconBox icon={Boxes}/></div><label className="field-label" htmlFor="builder-prompt">업무 설명</label><textarea id="builder-prompt" className="text-area tall" value={prompt} onChange={e => setPrompt(e.target.value)}/><div className="muted-note"><Braces size={15}/><span>{state.llm_configured ? '연결된 LLM으로 앱 구성을 계획합니다.' : '규칙 기반으로 산출물 접수용 기본 구조의 앱을 생성합니다.'}</span></div><Button className="primary full-width" icon={Plus} busy={busy === 'start-builder'} disabled={!prompt.trim() || !connected} onClick={() => startTask('builder', { prompt })}>업무앱 만들기</Button></section><section className="builder-blueprint"><span className="eyebrow">FROM IDEA TO APP</span><h3>화면에서 데이터까지,<br/>한 번에 연결합니다.</h3><div className="blueprint-stack"><div><span>01</span><Boxes size={18}/><strong>업무 데이터 구조</strong><Check size={15}/></div><div><span>02</span><Layers3 size={18}/><strong>입력 · 목록 · 상태 화면</strong><Check size={15}/></div><div><span>03</span><ListChecks size={18}/><strong>실제로 저장되는 레코드</strong><Check size={15}/></div></div><p>생성한 앱은 별도 창에서 열고 데이터를 직접 등록할 수 있습니다.</p></section></div>
    <section className="section-block"><div className="block-heading"><div><h2>내 업무앱 <span className="subtle-count">{state.apps.length}</span></h2><p>생성된 화면과 저장된 데이터를 확인하세요.</p></div></div>{state.apps.length ? <div className="app-grid">{state.apps.map(app => <article className="card app-card" key={app.id}><div className="app-card-top"><IconBox icon={Boxes}/><span className="small-label">로컬 앱</span></div><h3>{app.name}</h3><p>{app.description}</p><div className="app-card-meta"><span><Layers3 size={14}/>{app.record_count ?? 0}개 레코드</span><span>{dateTime(app.created_at)}</span></div><div className="app-card-actions"><button className="button secondary" onClick={() => openRecords(app)}><ListChecks size={15}/>데이터 보기</button><a className="button primary" href={`/api/apps/${encodeURIComponent(app.id)}/view`} target="_blank" rel="noreferrer"><ExternalLink size={15}/>앱 열기</a></div></article>)}</div> : <div className="card"><Empty icon={Boxes} title="첫 업무앱을 만들어보세요">생성한 앱과 데이터가 이곳에 보관됩니다.</Empty></div>}</section>
    <section className="card"><div className="card-heading"><h2>앱 제작 이력</h2></div><TaskList tasks={state.tasks.filter(t => t.agent === 'builder')} showTask={showTask}/></section></>;
}

function DeveloperPage({ state, startTask, busy, showTask, navigate, connected, prepared, setPrepared }) {
  const [appId, setAppId] = useState('');
  const [requirement, setRequirement] = useState('상태를 완료로 변경할 때 완료 메모를 필수로 입력하도록 바꿔줘. 입력하지 않으면 저장을 막고 안내해줘.');
  useEffect(() => { if (!appId && state.apps.length) setAppId(state.apps[0].id); }, [state.apps, appId]);
  useEffect(()=>{if(prepared?.agent==='developer'){if(prepared.input?.app_id)setAppId(prepared.input.app_id);setRequirement(prepared.input?.requirement||'');setPrepared(null);}},[prepared,setPrepared]);
  return <><SectionHead eyebrow="DEVELOPMENT & ACCEPTANCE TEST" title="개발 · UAT" description="업무앱을 변경하고 검증 결과와 검증 산출물을 확인합니다."/>
    <div className="two-column-form"><section className="card padded-card"><div className="card-heading flush"><h2>개발 요청</h2><IconBox icon={Code2} color="purple"/></div><label className="field-label" htmlFor="dev-app">대상 업무앱</label><select id="dev-app" className="input" value={appId} onChange={e => setAppId(e.target.value)}><option value="">앱을 선택하세요</option>{state.apps.map(app => <option value={app.id} key={app.id}>{app.name}</option>)}</select>{!state.apps.length && <button className="inline-link space-top" onClick={() => navigate('builder')}>업무앱 먼저 만들기 <ArrowRight size={14}/></button>}<label className="field-label space-top" htmlFor="dev-requirement">변경 요구사항</label><textarea id="dev-requirement" className="text-area tall" value={requirement} onChange={e => setRequirement(e.target.value)}/><div className="muted-note"><Code2 size={15}/><span>지원 변경: 필수 입력과 완료 메모 규칙을 적용하고 검증합니다.</span></div><Button className="primary full-width" icon={Play} busy={busy === 'start-developer'} disabled={!appId || !requirement.trim() || !connected} onClick={() => startTask('developer', { app_id: appId, requirement })}>개발 · UAT 실행</Button></section><section className="card padded-card process-card"><span className="eyebrow">EVIDENCE, NOT ASSUMPTIONS</span><h3>실행한 검증만<br/>결과로 남깁니다.</h3><ol className="process-list"><li><span>1</span><div><strong>변경 요구사항 적용</strong><p>대상 앱의 동작과 소스 변경</p></div></li><li><span>2</span><div><strong>실제 화면 검증</strong><p>입력 · 저장 · 오류 처리 동작 확인</p></div></li><li><span>3</span><div><strong>UAT 증빙 생성</strong><p>검증 결과, 소스, 실행 근거 제공</p></div></li></ol><div className="notice"><ShieldCheck size={17}/><p>검증 환경이 없거나 실패한 경우, 통과로 표시하지 않습니다.</p></div></section></div>
    <section className="card"><div className="card-heading"><h2>개발 · 검증 이력</h2></div><TaskList tasks={state.tasks.filter(t => t.agent === 'developer')} showTask={showTask}/></section></>;
}

function MemoryPage({ state, busy, perform, connected }) {
  const [key, setKey] = useState(''); const [value, setValue] = useState(''); const [editing, setEditing] = useState(false); const [confirmDelete, setConfirmDelete] = useState(null);
  const save = async event => { event.preventDefault(); const result = await perform('save-memory', () => post('/memories', { key: key.trim(), value: value.trim() }), '업무 기억을 저장했습니다.'); if (result) { setKey(''); setValue(''); setEditing(false); } };
  return <><SectionHead eyebrow="PERSONAL CONTEXT" title="업무 기억" description="반복해서 설명하지 않도록 업무 맥락과 선호를 저장합니다."/>
    <div className="memory-layout"><section className="card padded-card memory-form"><div className="card-heading flush"><h2>{editing ? '업무 기억 수정' : '기억 추가하기'}</h2><IconBox icon={BookOpen}/></div><form onSubmit={save}><label className="field-label" htmlFor="memory-key">기억 이름</label><input id="memory-key" className="input" placeholder="예: 알림 선호" value={key} onChange={e => setKey(e.target.value)}/><label className="field-label space-top" htmlFor="memory-value">기억할 내용</label><textarea id="memory-value" className="text-area tall" placeholder="확인이 필요한 업무와 완료 결과만 간결하게 알려주세요." value={value} onChange={e => setValue(e.target.value)}/><div className="form-button-row"><Button type="submit" className="primary" icon={Check} busy={busy === 'save-memory'} disabled={!key.trim() || !value.trim() || !connected}>기억 저장</Button>{editing && <Button type="button" className="secondary" onClick={() => { setKey(''); setValue(''); setEditing(false); }}>취소</Button>}</div></form><div className="notice space-top"><BookOpen size={17}/><p>저장된 기억은 업무비서가 요청을 처리할 때 참고합니다. 언제든 삭제할 수 있습니다.</p></div></section><section className="memory-cards"><div className="block-heading compact"><h2>저장된 기억 <span className="subtle-count">{state.memories.length}</span></h2></div>{state.memories.length ? state.memories.map(memory => <article className="card memory-card" key={memory.id}><div className="memory-card-heading"><span className="memory-icon"><BookOpen size={17}/></span><h3>{memory.key}</h3><button className="icon-button" aria-label={`${memory.key} 수정`} onClick={() => { setKey(memory.key); setValue(asText(memory.value)); setEditing(true); document.getElementById('memory-key')?.focus(); }}><SquarePen size={16}/></button><button className="icon-button danger-hover" aria-label={`${memory.key} 삭제`} onClick={() => setConfirmDelete(memory.id)}><Trash2 size={16}/></button></div><p>{asText(memory.value)}</p><div className="memory-meta"><span>{memory.source === 'demo' || memory.source === 'seed' ? '기본 기억' : memory.source === 'user' ? '직접 저장' : asText(memory.source || '저장된 기억')}</span><time>{dateTime(memory.updated_at)}</time></div>{confirmDelete === memory.id && <div className="delete-confirm"><span>이 기억을 삭제할까요?</span><Button className="danger small" busy={busy === `delete-${memory.id}`} onClick={async () => { await perform(`delete-${memory.id}`, () => api(`/memories/${memory.id}`, { method: 'DELETE' }), '기억을 삭제했습니다.'); setConfirmDelete(null); }}>삭제</Button><Button className="secondary small" onClick={() => setConfirmDelete(null)}>취소</Button></div>}</article>) : <div className="card"><Empty icon={BookOpen} title="아직 저장된 기억이 없습니다">업무비서에게 알려주거나 직접 추가하세요.</Empty></div>}</section></div></>;
}

function TaskModal({ task, onClose, perform, busy, notify, state, navigate, refresh, showTask, openDocument }) {
  const [detailTab, setDetailTab] = useState('result');
  const [newCurrent, setNewCurrent] = useState('clean');
  const [showReplacement, setShowReplacement] = useState(false);
  const result = resultObject(task?.result);
  const findings = Array.isArray(result.findings) ? result.findings : Array.isArray(result.inspection?.findings) ? result.inspection.findings : [];
  const tests = Array.isArray(result.tests) ? result.tests : [];
  const counts = result.counts || result.inspection?.counts;
  const artifacts = task?.artifacts || [];
  const [agentLabel, AgentIcon] = AGENTS[task?.agent] || ['업무', ListChecks];
  const resume = async (action, input) => { await perform(`resume-${task.id}`, () => post(`/tasks/${task.id}/resume`, { action, ...(input ? { input } : {}) }), '작업을 재개했습니다. 실행 결과를 기다려주세요.'); };
  return <Modal title={task?.title || '업무 불러오는 중'} subtitle={task ? `${agentLabel} · ${dateTime(task.created_at)}` : ''} onClose={onClose} wide>{!task ? <div className="initial-loading"><Loader2 className="spin"/></div> : <>
    <TaskControls task={task} perform={perform} busy={busy} showTask={showTask} onClose={onClose}/><TaskMetadata task={task} state={state} perform={perform} busy={busy}/><div className="task-detail-top"><div><IconBox icon={AgentIcon}/><Badge status={task.status}/><span className="small-label">{task.input?.manual ? '직접 관리 업무' : task.mode === 'live' ? '모델 연결 환경' : '규칙 기반 환경'}</span></div><code>{task.id}</code></div>
    {(task.summary || result.summary) && <p className="task-summary">{asText(task.summary || result.summary)}</p>}
    {['running', 'queued'].includes(task.status) && <div className="running-notice"><Loader2 className="spin" size={18}/><span>작업 중입니다. 실행 기록을 자동으로 갱신합니다.</span></div>}
    {task.status === 'failed' && <div className="notice danger-notice"><AlertTriangle size={18}/><p>{asText(result.error || task.error || '작업을 완료하지 못했습니다. 실행 기록에서 원인을 확인해 주세요.')}</p></div>}
    {['todo', 'in_progress', 'waiting_input'].includes(task.status) && task.input?.manual && <section className="waiting-panel"><div><CheckCircle2 size={20}/><div><h3>업무를 마치셨나요?</h3><p>완료를 확인하면 업무 상태와 진행 기록에 반영됩니다.</p></div></div><div className="waiting-actions"><Button className="primary" icon={CheckCircle2} busy={busy === `resume-${task.id}`} onClick={() => resume('continue')}>완료 확인</Button></div></section>}
    {task.status === 'waiting_input' && result.checkpoint === 'registration' && <RegistrationForm task={task} perform={perform} busy={busy}/>}
    {task.status === 'waiting_input' && !task.input?.manual && result.checkpoint !== 'registration' && <section className="waiting-panel"><div><AlertTriangle size={20}/><div><h3>계속 진행하려면 확인이 필요합니다</h3><p>{asText(result.next_action || result.message || '검사 결과를 확인하고 복구 또는 보완 파일로 작업을 이어가세요.')}</p></div></div><div className="waiting-actions">{['excel', 'automation'].includes(task.agent) && <Button className="primary" icon={RefreshCw} busy={busy === `resume-${task.id}`} onClick={() => resume('repair')}>복구 가능한 수식 수정 후 계속</Button>}<Button className="secondary" icon={Upload} onClick={() => setShowReplacement(!showReplacement)}>보완 파일로 계속</Button></div>{showReplacement && <div className="replacement-file"><FileInput label="보완된 수정본" value={newCurrent} setValue={setNewCurrent} fixtures={state.fixtures} uploads={state.uploads || []} refresh={refresh} notify={notify}/><Button className="primary" busy={busy === `resume-${task.id}`} onClick={() => resume('continue', { current: newCurrent })}>이 파일로 재검증</Button></div>}</section>}
    {task.agent === 'excel' && !task.input?.currents?.length && task.status === 'completed' && (result.repairable_count > 0 || findings.some(f => f.repairable)) && <section className="waiting-panel"><div><RefreshCw size={20}/><div><h3>명확한 수식 오류를 사본에 복구할 수 있습니다</h3><p>기준 파일과 일치하는 수식만 복원합니다. 데이터 오류는 별도로 검토해야 합니다.</p></div></div><div className="waiting-actions"><Button className="primary" icon={RefreshCw} busy={busy === `resume-${task.id}`} onClick={() => resume('repair')}>검토한 수식 사본에 복구</Button></div></section>}
    <div className="detail-tabs tabs">{[['result', '결과 · 근거'], ['events', `실행 기록 ${(task.events || []).length}`], ['input', '입력 정보']].map(([id, label]) => <button key={id} className={detailTab === id ? 'active' : ''} onClick={() => setDetailTab(id)}>{label}</button>)}</div>
    {detailTab === 'result' && <>
      <DeskResult task={task} perform={perform} busy={busy} showTask={showTask} openDocument={openDocument} notify={notify}/><BatchResults result={result}/>
      {counts && <div className="result-counts">{[['errors', '오류', 'red'], ['warnings', '주의', 'amber'], ['changes', '변경', 'teal']].map(([key, label, color]) => <div key={key} className={color}><span>{label}</span><strong>{counts[key] ?? 0}</strong></div>)}</div>}
      {result.recalculated === false && <div className="subtle-notice"><FileSpreadsheet size={14}/> 수식 구조를 검사했습니다. Excel 계산 엔진의 재계산 결과는 포함하지 않습니다.</div>}
      {findings.length > 0 && <div className="findings"><h3>검사 결과 <span className="subtle-count">{findings.length}</span></h3>{findings.map((finding, index) => <article className="finding" key={finding.id || index}><div className="finding-top"><span className={`finding-severity ${finding.severity === 'error' || finding.severity === 'high' ? 'error' : 'warning'}`}>{finding.severity === 'error' || finding.severity === 'high' ? '오류' : '주의'}</span><code>{finding.sheet}{finding.cell ? `!${finding.cell}` : ''}</code><span>{asText(finding.rule)}</span>{finding.repairable && <span className="repairable"><RefreshCw size={11}/> 복구 가능</span>}</div><p>{asText(finding.message)}</p>{(finding.before !== undefined || finding.after !== undefined) && <div className="diff-block"><div><span>이전</span><code>{asText(finding.before) || '(비어 있음)'}</code></div><div><span>현재</span><code>{asText(finding.after) || '(비어 있음)'}</code></div></div>}{finding.suggested && <div className="suggested-value">제안 <code>{asText(finding.suggested)}</code></div>}</article>)}</div>}
      {tests.length > 0 && <section className="test-results"><h3>실제 검증 결과 {typeof result.passed === 'boolean' && <span className={`badge ${result.passed ? 'green' : 'red'}`}>{result.passed ? '통과' : '확인 필요'}</span>}</h3>{tests.map((test, i) => { const passed = test.passed === true || test.status === 'passed' || test.status === 'pass'; return <div className="test-row" key={test.id || i}>{passed ? <CheckCircle2 className="text-teal" size={19}/> : <AlertTriangle className="text-amber" size={19}/>}<div><strong>{asText(test.name || test.title || test.case || `검증 ${i + 1}`)}</strong><p>{asText(test.message || test.details || test.error || test.actual || test.status || '')}</p></div><span className={`badge ${passed ? 'green' : 'amber'}`}>{passed ? '통과' : '미통과 / 미검증'}</span></div>; })}</section>}
      {(result.app_id || result.app?.id) && <div className="result-app-link"><IconBox icon={Boxes} size="small"/><div><strong>업무앱 결과 확인</strong><p>실제 생성된 앱을 새 창에서 엽니다.</p></div><a className="button secondary" href={`/api/apps/${encodeURIComponent(result.app_id || result.app.id)}/view`} target="_blank" rel="noreferrer"><ExternalLink size={15}/>앱 열기</a></div>}
      {Array.isArray(result.applied) && <div className="notice"><CheckCircle2 size={17}/><p>{result.applied.length}개 항목의 복구를 사본에 적용했습니다.</p></div>}
      <section className="artifacts-section"><div className="block-heading compact"><h3>결과물 <span className="subtle-count">{artifacts.length}</span></h3></div>{artifacts.length ? <div className="artifact-grid">{artifacts.map((artifact, i) => <a key={artifact.id || i} className="artifact-card" href={safeUrl(artifact.url)} target="_blank" rel="noreferrer"><span className="artifact-icon">{String(artifact.name).endsWith('.xlsx') ? <FileSpreadsheet size={22}/> : <FileText size={22}/>}</span><span><strong>{artifact.name}</strong><small>{artifact.kind || '실행 산출물'}</small></span><ArrowDownToLine size={16}/></a>)}</div> : <p className="empty-inline">{['queued', 'running'].includes(task.status) ? '작업이 진행되면 생성된 결과물이 여기에 나타납니다.' : '이 작업에 첨부된 결과물이 없습니다.'}</p>}</section>
      {!result.sections?.length && !result.answer && !findings.length && !tests.length && !counts && task.status === 'completed' && !artifacts.length && <div className="notice"><CheckCircle2 size={18}/><p>{asText(result.reply || result.message || '작업이 완료되었습니다. 실행 기록에서 처리 내용을 확인할 수 있습니다.')}</p></div>}
    </>}
    {detailTab === 'events' && <div className="detail-timeline">{task.events?.length ? task.events.map((event, i) => <div key={i}><span className="timeline-index">{i + 1}</span><div><strong>{asText(event.message)}</strong><time>{dateTime(event.at)}</time></div></div>) : <Empty icon={History} title="아직 실행 기록이 없습니다"/>}</div>}
    {detailTab === 'input' && <><p className="helper-text">이 작업을 시작할 때 전달한 입력입니다.</p><pre className="json-view">{JSON.stringify(task.input || {}, null, 2)}</pre>{(result.source_sha256 || result.source_hash) && <p className="helper-text">검증 소스 해시: <code>{asText(result.source_sha256 || result.source_hash)}</code></p>}</>}
    <TaskNotes task={task} perform={perform} busy={busy}/>
  </>}</Modal>;
}

function RecordsModal({ app, onClose, notify, refresh }) {
  const [records, setRecords] = useState([]), [loading, setLoading] = useState(true), [saving, setSaving] = useState(false), [editing, setEditing] = useState(null), [values, setValues] = useState({}), [deleting, setDeleting] = useState(null), [query,setQuery] = useState('');
  const fields = app.schema?.fields || [];
  const load = useCallback(async () => { try { const data = await api('/apps/'+app.id+'/records'); setRecords(Array.isArray(data) ? data : data.records || []); } catch (error) { notify(error.message, true); } finally { setLoading(false); } }, [app.id, notify]);
  useEffect(() => { load(); }, [load]);
  const save = async event => {event.preventDefault();setSaving(true);try {const payload=Object.fromEntries(fields.map(f=>[f.name, values[f.name] ?? (['boolean','checkbox'].includes(f.type)?false:'')]));await api('/apps/'+app.id+'/records'+(editing?'/'+editing:''),{method:editing?'PATCH':'POST',body:payload});setValues({});setEditing(null);await load();await refresh();notify('레코드를 저장했습니다.');}catch(error){notify(error.message,true);}finally{setSaving(false);}};
  const remove = async id => {setSaving(true);try{await api('/apps/'+app.id+'/records/'+id,{method:'DELETE'});await load();await refresh();if(editing===id){setEditing(null);setValues({});}setDeleting(null);notify('레코드를 삭제했습니다.');}catch(error){notify(error.message,true);}finally{setSaving(false);}};
  const filtered = records.filter(r=>fields.some(f=>asText(r[f.name]).toLowerCase().includes(query.toLowerCase())));
  return <Modal title={app.name} subtitle="저장된 업무 데이터" onClose={onClose} wide><div className="record-toolbar"><span>{records.length}개 레코드</span><label className="compact-search"><Search size={14}/><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="데이터 검색" aria-label="데이터 검색"/></label><a className="button secondary small" href={'/api/apps/'+app.id+'/export.csv'} download><ArrowDownToLine size={14}/>CSV</a><a className="button secondary small" href={'/api/apps/'+app.id+'/view'} target="_blank" rel="noreferrer"><ExternalLink size={14}/>앱 열기</a></div>{loading?<div className="empty"><Loader2 className="spin"/></div>:filtered.length?<div className="table-scroll"><table className="record-table"><thead><tr>{fields.map(f=><th key={f.name}>{f.label||f.name}</th>)}<th>관리</th></tr></thead><tbody>{filtered.map(record=><tr key={record.id}>{fields.map(f=><td key={f.name}>{typeof record[f.name]==='boolean'?(record[f.name]?'예':'아니오'):asText(record[f.name])||'—'}</td>)}<td><div className="table-actions"><button className="icon-button" aria-label="레코드 수정" onClick={()=>{setEditing(record.id);setValues(Object.fromEntries(fields.map(f=>[f.name,record[f.name]??''])));}}><SquarePen size={15}/></button><button className="icon-button" aria-label="레코드 삭제" onClick={()=>setDeleting(record.id)}><Trash2 size={15}/></button></div></td></tr>)}</tbody></table></div>:<Empty icon={ListChecks} title={query?'검색 결과가 없습니다':'등록된 데이터가 없습니다'}/>} {deleting&&<div className="delete-confirm"><span>선택한 레코드를 삭제할까요?</span><Button className="danger small" busy={saving} onClick={()=>remove(deleting)}>삭제</Button><Button className="secondary small" onClick={()=>setDeleting(null)}>취소</Button></div>}<form className="record-form" onSubmit={save}><h3>{editing?'레코드 수정':'새 레코드 추가'}</h3><div className="record-fields">{fields.map(field=><DynamicField key={field.name} field={field} value={values[field.name]} onChange={value=>setValues({...values,[field.name]:value})}/>)}</div><div className="form-button-row"><Button type="submit" className="primary" busy={saving} icon={Check}>레코드 저장</Button>{editing&&<Button type="button" className="secondary" onClick={()=>{setEditing(null);setValues({});}}>수정 취소</Button>}</div></form></Modal>;
}
