/* Actual browser verification of the proposed generated HTML. No fabricated PASS. */
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const http = require('http');
const assert = require('assert/strict');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8').replace(/^\uFEFF/, ''));
const output = config.output_dir;
const source = fs.readFileSync(config.source_path, 'utf8');
const tests = [];
const futureDate = new Date(Date.now()+7*86400000).toISOString().slice(0,10);
const results = { passed: false, executed_at: new Date().toISOString(), environment: '',
  source_sha256: crypto.createHash('sha256').update(source).digest('hex'),
  api_mode: 'isolated stateful mock; production API not exercised', tests };
let browser, page, mockServer;
function loadPlaywright() {
  const candidates = [process.env.WORKMATE_PLAYWRIGHT_MODULE, 'playwright',
    path.join(process.env.USERPROFILE || '', '.cache', 'codex-runtimes', 'codex-primary-runtime', 'dependencies', 'node', 'node_modules', 'playwright')].filter(Boolean);
  for (const candidate of candidates) {try {return require(candidate)} catch {}}
  throw new Error('Playwright 모듈을 찾을 수 없습니다. npm install playwright 후 다시 실행해주세요.');
}
async function test(id, name, expected, action, screenshot) {
  const steps = {'TC-001':'앱 열기 → 입력 필드와 빈 목록 확인','TC-002':'업무명 비우기 → 저장 → 오류 메시지 확인','TC-003':'업무명·담당자·기한 입력 → 저장 → 목록 확인','TC-004':'담당자 비우기 → 저장 → 오류 확인 → 담당자 복구','TC-005':'마감일 비우기 → 저장 → 오류 확인 → 마감일 복구','TC-006':'지난 날짜 입력 → 저장 → 차단 확인 → 유효 날짜 복구','TC-007':'완료 상태 선택 → 메모 비우기 → 저장 → 차단 확인','TC-008':'담당자·기한·완료 메모 입력 → 저장 → 완료 상태 확인','TC-009':'페이지 새로고침 → 저장값 재조회 → 오류 확인','TC-010':'일치·불일치 검색 → 대기·완료 필터 → 결과 확인','TC-011':'CSV 내보내기 → 다운로드 → 필드와 저장값 확인','TC-012':'삭제 취소 → 데이터 유지 → 삭제 확인 → 빈 목록 확인'};
  const item = { id, name, expected, status: 'running', actual: '', started_at: new Date().toISOString(), steps: steps[id] || '필드의 필수·유형·범위 제약을 입력하고 저장 동작 확인' };
  tests.push(item);
  try {
    item.actual = await action() || '기대결과와 일치';
    screenshot = screenshot || 'evidence-'+id.toLowerCase()+'.png';
    await page.screenshot({path:path.join(output, screenshot), fullPage:true});item.screenshot=screenshot;
    item.status='passed';
  } catch (error) {
    item.status='failed';item.actual=error.message;
    try {item.screenshot='evidence-failure-'+id+'.png';await page.screenshot({path:path.join(output,item.screenshot),fullPage:true})} catch {delete item.screenshot}
    throw error;
  } finally {item.completed_at = new Date().toISOString();}
}
async function main() {
  const { chromium } = loadPlaywright();
  const errors=[];
  for (const channel of [process.env.WORKMATE_BROWSER_CHANNEL, 'msedge', 'chrome', null].filter((v,i,a)=>v!==undefined && a.indexOf(v)===i)) {
    try {browser=await chromium.launch({headless:true,...(channel?{channel}:{}),timeout:15000});results.environment='Playwright / Chromium '+(channel||'bundled')+' / headless / isolated API mock';break} catch(e){errors.push(e.message)}
  }
  if(!browser)throw new Error(errors.join('\n').slice(-2500));
  page=await browser.newPage({viewport:{width:1400,height:1000},locale:'ko-KR',timezoneId:'Asia/Seoul'});
  page.setDefaultTimeout(8000);
  const pageErrors=[];page.on('pageerror',error=>pageErrors.push(error.message));
  let records=[];let seq=1;const requests=[];
  // Isolated in-memory HTTP service. A real attachment response is needed to
  // exercise browser downloads; the production API/database is never used.
  const api='/api/apps/'+encodeURIComponent(config.app_id)+'/records';
  mockServer=http.createServer(async(request,response)=>{try{
    const url=new URL(request.url,'http://127.0.0.1');
    const send=(status,body,type='application/json; charset=utf-8',headers={})=>{response.writeHead(status,{'Content-Type':type,...headers});response.end(typeof body==='string'?body:JSON.stringify(body));};
    if(url.pathname==='/app'){send(200,source,'text/html; charset=utf-8');return}
    if(url.pathname==='/api/apps/'+encodeURIComponent(config.app_id)+'/export.csv'){
      const escape=value=>'"'+String(value??'').replace(/"/g,'""')+'"';
      const csv='\uFEFF'+[config.schema.fields.map(f=>escape(f.label)).join(','),...records.map(r=>config.schema.fields.map(f=>escape(r[f.name])).join(','))].join('\r\n');
      send(200,csv,'text/csv; charset=utf-8',{'Content-Disposition':'attachment; filename="records.csv"'});return;
    }
    if(url.pathname===api||url.pathname.startsWith(api+'/')){
      const method=request.method;requests.push(method+' '+url.pathname);
      if(method==='GET'){send(200,records);return}
      if(method==='DELETE'){const id=decodeURIComponent(url.pathname.split('/').pop());records=records.filter(r=>r.id!==id);send(200,{ok:true});return}
      const body=await new Promise((resolve,reject)=>{let value='';request.on('data',chunk=>{value+=chunk;if(value.length>100000){reject(new Error('test body too large'));request.destroy()}});request.on('end',()=>resolve(value));request.on('error',reject)});
      const data=JSON.parse(body||'{}');
      if(method==='POST'){const record={id:'test-'+seq++,...data};records.push(record);send(201,record);return}
      if(method==='PATCH'){const id=decodeURIComponent(url.pathname.split('/').pop()),record=records.find(r=>r.id===id);if(!record){send(404,{detail:'not found'});return}Object.assign(record,data);send(200,record);return}
    }
    send(404,'isolated test: unsupported route','text/plain');
  }catch(error){response.writeHead(500,{'Content-Type':'application/json'});response.end(JSON.stringify({detail:error.message}));}
  });
  await new Promise((resolve,reject)=>{mockServer.once('error',reject);mockServer.listen(0,'127.0.0.1',resolve)});
  const base='http://127.0.0.1:'+mockServer.address().port;
  const field=name=>page.locator('#field-'+name);
  const save=()=>page.locator('#save-record').click();
  const stored=()=>page.getByText('업무를 저장했습니다.',{exact:true}).waitFor();
  const baseNames=['title','owner','due_date','status','completion_note'];
  const custom=config.schema.fields.filter(f=>!baseNames.includes(f.name));
  const sampleValue=f=>f.type==='number'?Math.max(f.min??-Infinity,Math.min(f.max??Infinity,42)):f.type==='email'?'tester@example.test':f.type==='date'?futureDate:f.type==='select'?f.options[0]:f.type==='checkbox'?true:''+f.label;
  async function setValue(f,value){if(f.type==='checkbox')await field(f.name).setChecked(value===true);else if(f.type==='select')await field(f.name).selectOption(String(value));else await field(f.name).fill(String(value));}
  async function fillCustom(){for(const f of custom)await setValue(f,sampleValue(f));if(config.schema.fields.find(f=>f.name==='completion_note').required)await field('completion_note').fill('검수 메모');}
  await test('TC-001','생성 앱 초기 렌더링','실제 HTML이 로드되고 정의된 모든 필드와 빈 목록 표시',async()=>{
    await page.goto(base+'/app');await page.locator('#empty').waitFor({state:'visible'});assert.equal(await page.locator('#form-fields .field').count(),config.schema.fields.length);assert.equal(await page.locator('#record-headings th').count(),config.schema.fields.length+1);assert.equal(pageErrors.length,0);return '앱 제목·'+config.schema.fields.length+'개 입력 필드·동적 테이블·빈 목록 확인';
  },'evidence-01-initial.png');
  await test('TC-002','필수 업무명 검증','빈 업무명은 저장 요청 없이 차단',async()=>{
    await save();await page.getByText('업무명을 입력해주세요.',{exact:true}).waitFor();assert.equal(records.length,0);return '필수 입력 오류 표시, 레코드 0건';
  });
  await test('TC-003','업무 생성 및 API 연동','업무 1건이 POST 후 목록에 표시',async()=>{
    await field('title').fill('구매 요청 검수 <확인>');await field('owner').fill('김서연');await field('due_date').fill(futureDate);await fillCustom();await save();await stored();assert.equal(records.length,1);assert.equal(records[0].title,'구매 요청 검수 <확인>');for(const f of custom)assert.deepEqual(records[0][f.name],sampleValue(f));assert.equal(await page.locator('#records tr').count(),1);assert.ok(requests.some(r=>r.startsWith('POST')));assert.equal(await page.locator('#records td').first().textContent(),'구매 요청 검수 <확인>');return 'POST 호출·동적 필드 값 저장·1건 조회 성공, 특수문자를 텍스트로 렌더링';
  },'evidence-02-created.png');
  await page.locator('.edit-record').first().click();
  for(const [index,f] of custom.entries()){
    await test('CUSTOM-'+String(index+1).padStart(2,'0'),f.label+' 동적 입력 검증','필수·유형·범위 제약을 검증하고 기존 데이터 유지',async()=>{
      const attempts=[];
      if(f.required)attempts.push(f.type==='checkbox'?false:'');
      if(f.type==='number'&&f.min!==undefined)attempts.push(f.min-Math.max(1,Math.abs(f.min)*.01));
      if(f.type==='number'&&f.max!==undefined)attempts.push(f.max+Math.max(1,Math.abs(f.max)*.01));
      if(f.type==='email')attempts.push('invalid-address');
      for(const invalid of attempts){await setValue(f,invalid);const before=requests.filter(r=>r.startsWith('PATCH')).length;await save();await page.locator('#feedback.error').waitFor({state:'visible'});assert.equal(requests.filter(r=>r.startsWith('PATCH')).length,before);assert.deepEqual(records[0][f.name],sampleValue(f));await setValue(f,sampleValue(f));}
      return attempts.length?attempts.length+'개 잘못된 입력 차단, 기존 저장값 유지':'선택 필드의 '+f.type+' 값 저장 및 재편집 확인';
    },index===0?'evidence-03-custom-validation.png':undefined);
  }
  if(config.schema.fields.find(f=>f.name==='owner').required){
    await test('TC-004','담당자 필수 변경 검증','담당자가 없으면 변경 저장 차단',async()=>{await field('owner').fill('');await save();await page.getByText('담당자를 입력해주세요.',{exact:true}).waitFor();assert.equal(records[0].owner,'김서연');await field('owner').fill('김서연');return '담당자 누락 차단, 기존 값 유지';},'evidence-03-owner-validation.png');
  }
  if(config.schema.fields.find(f=>f.name==='due_date').required){
    await test('TC-005','마감일 필수 변경 검증','마감일이 없으면 변경 저장 차단',async()=>{await field('due_date').fill('');await save();await page.getByText('마감일을 입력해주세요.',{exact:true}).waitFor();assert.equal(records[0].due_date,futureDate);await field('due_date').fill(futureDate);return '마감일 누락 차단, 기존 값 유지';},'evidence-04-date-validation.png');
  }
  if(config.schema.prevent_past_due_date){
    await test('TC-006','과거 마감일 검증','과거 날짜는 저장 차단',async()=>{await field('due_date').fill('2000-01-01');await save();await page.getByText('마감일을 오늘 이후로 입력해주세요.',{exact:true}).waitFor();assert.equal(records[0].due_date,futureDate);await field('due_date').fill(futureDate);return '과거 마감일 차단, 기존 값 유지';},'evidence-05-past-validation.png');
  }
  if(config.schema.require_completion_note){
    await test('TC-007','완료 메모 필수 변경 검증','완료 상태에서 메모가 비어 있으면 저장 차단',async()=>{await field('status').selectOption('완료');await field('completion_note').fill('');const before=requests.filter(r=>r.startsWith('PATCH')).length;await save();await page.locator('#feedback.error').waitFor({state:'visible'});assert.ok((await page.locator('#feedback').textContent()).includes('메모'));assert.equal(records[0].status,'대기');assert.equal(requests.filter(r=>r.startsWith('PATCH')).length,before);return '완료 메모 오류 표시, PATCH 미발생, 기존 상태 대기 유지';},'evidence-06-completion-validation.png');
  }
  await test('TC-008','유효한 변경 저장','담당자·마감일·완료 메모를 입력하면 완료 저장',async()=>{await field('owner').fill('박지훈');await field('due_date').fill(futureDate);await field('status').selectOption('완료');await field('completion_note').fill('구매 요청의 항목 및 금액 검수 완료');await save();await stored();assert.equal(records[0].owner,'박지훈');assert.equal(records[0].status,'완료');assert.ok(records[0].completion_note);assert.ok(requests.some(r=>r.startsWith('PATCH')));return 'PATCH 후 담당자 변경·완료 상태·메모가 목록에 표시';},'evidence-07-completed.png');
  await test('TC-009','페이지 재조회 및 JavaScript 오류 확인','재로드 후 저장값 유지, 브라우저 JavaScript 오류 없음',async()=>{await page.reload();await page.locator('#records tr').first().waitFor();assert.equal(await page.locator('#records tr').count(),1);assert.ok((await page.locator('#records').textContent()).includes('구매 요청의 항목 및 금액 검수 완료'));assert.equal(pageErrors.length,0);return '동일 검증 API 저장소에서 값 재조회 성공, JavaScript 오류 0건';},'evidence-08-reloaded.png');
  await test('TC-010','검색 및 상태 필터','텍스트 검색·상태 필터가 목록을 정확히 제한',async()=>{await page.locator('#search-records').fill('없는 업무');assert.equal(await page.locator('#records tr').count(),0);await page.locator('#search-records').fill('구매 요청');assert.equal(await page.locator('#records tr').count(),1);await page.locator('#status-filter').selectOption('대기');assert.equal(await page.locator('#records tr').count(),0);await page.locator('#status-filter').selectOption('완료');assert.equal(await page.locator('#records tr').count(),1);await page.locator('#search-records').fill('');await page.locator('#status-filter').selectOption('');return '일치·불일치 검색 및 완료 상태 필터 확인';},'evidence-09-filtered.png');
  await test('TC-011','CSV 내보내기','CSV 다운로드에 전체 필드와 저장된 레코드 포함',async()=>{const downloadEvent=page.waitForEvent('download');await page.locator('#export-csv').click();const download=await downloadEvent;const target=path.join(output,'evidence-export.csv');await download.saveAs(target);const csv=fs.readFileSync(target,'utf8');assert.ok(csv.includes('구매 요청 검수 <확인>'));for(const f of config.schema.fields)assert.ok(csv.includes(f.label));return '실제 다운로드 완료, 모든 필드명과 저장값 확인 (검증 API CSV)';});
  await test('TC-012','삭제 확인 및 삭제 실행','취소 시 유지, 확인 시 DELETE 후 목록과 저장소에서 제거',async()=>{page.once('dialog',dialog=>dialog.dismiss());await page.locator('.delete-record').first().click();assert.equal(records.length,1);page.once('dialog',dialog=>dialog.accept());await page.locator('.delete-record').first().click();await page.getByText('업무를 삭제했습니다.',{exact:true}).waitFor();assert.equal(records.length,0);assert.equal(await page.locator('#records tr').count(),0);assert.ok(requests.some(r=>r.startsWith('DELETE')));assert.equal(pageErrors.length,0);return '삭제 취소 시 유지, 확인 시 DELETE 및 빈 목록 확인';},'evidence-10-deleted.png');
  results.requests=requests;results.passed=tests.length>0&&tests.every(t=>t.status==='passed');
}
main().catch(error=>{results.passed=false;if(!tests.some(t=>t.status==='failed'))tests.push({id:'ENV-001',name:'실제 브라우저 실행',expected:'브라우저 실행 및 증거 생성',actual:error.message,status:'blocked'});results.error=error.message;process.exitCode=1}).finally(async()=>{
  fs.writeFileSync(path.join(output,'browser-results.json'),JSON.stringify(results,null,2),'utf8');
  if(browser)try{await browser.close()}catch{}
  if(mockServer)await new Promise(resolve=>{mockServer.close(resolve);mockServer.closeAllConnections?.()});
  console.log(JSON.stringify({passed:results.passed,tests:tests.length,environment:results.environment}));
});
