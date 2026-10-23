import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.dirname(ROOT);
const WORK = path.join(ROOT, 'data/examples/excel_showcase');
const bundledPython = process.env.WORKMATE_PYTHON || path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe');
const require = createRequire(path.join(WORK, 'loader.mjs'));
const { Workbook, SpreadsheetFile, FileBlob } = await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
await fs.mkdir(WORK, { recursive: true });
const FORMAT_WORK = path.resolve(ROOT, '../../work/excel-format-v2');
await fs.mkdir(path.join(FORMAT_WORK,'originals'), {recursive:true});
for(const filename of ['baseline.xlsx','changed.xlsx','clean.xlsx','reordered.xlsx']){
  const target=path.join(FORMAT_WORK,'originals',filename);
  try{await fs.copyFile(path.join(ROOT,'data/fixtures',filename),target,1);}catch(e){if(!['EEXIST','ENOENT'].includes(e.code))throw e;}
}
for(const filename of ['Excel_검증_기준본.xlsx','Excel_검증_수정본.xlsx']){
  try{await fs.copyFile(path.join(OUT,filename),path.join(FORMAT_WORK,'originals',filename),1);}catch(e){if(!['EEXIST','ENOENT'].includes(e.code))throw e;}
}

function ledgerStyle(sheet, first, last, lastCol='G'){
  sheet.showGridLines=false;
  sheet.getRange(`A${first}:${lastCol}${last}`).format={font:{name:'Arial',size:12,color:'#203244'},verticalAlignment:'center',rowHeight:32};
  sheet.getRange(`A${first}:${lastCol}${last}`).format.borders={insideHorizontal:{style:'thin',color:'#B9C9D7'},top:{style:'medium',color:'#536F88'},bottom:{style:'medium',color:'#536F88'},left:{style:'thin',color:'#8EA5B9'},right:{style:'thin',color:'#8EA5B9'}};
  for(let row=first+1;row<=last;row++)sheet.getRange(`A${row}:${lastCol}${row}`).format.fill=(row-first)%2===0?'#EAF1F7':'#FFFFFF';
  sheet.getRange(`A${first}:${lastCol}${first}`).format={fill:'#173F5F',font:{name:'Arial',size:12,bold:true,color:'#FFFFFF'},rowHeight:34,horizontalAlignment:'center',verticalAlignment:'center'};
  sheet.getRange(`A${first}:${lastCol}${first}`).format.borders={insideVertical:{style:'thin',color:'#FFFFFF'},bottom:{style:'medium',color:'#163C5A'}};
  if(lastCol>='F'){
    sheet.getRange(`D${first+1}:F${last}`).setNumberFormat('#,##0;[Red](#,##0);0');
    sheet.getRange(`D${first+1}:F${last}`).format.horizontalAlignment='right';
    sheet.getRange(`F${first+1}:F${last}`).format.fill='#E0EDF5';
    sheet.getRange(`F${first+1}:F${last}`).format.font={name:'Arial',size:12,bold:true,color:'#174B6C'};
  }
  if(lastCol>='G'){
    sheet.getRange(`G${first+1}:G${last}`).format.horizontalAlignment='center';
    sheet.getRange(`G${first+1}:G${last}`).conditionalFormats.add('containsText',{text:'완료',format:{fill:'#E2F0E6',font:{color:'#24573C'}}});
    sheet.getRange(`G${first+1}:G${last}`).conditionalFormats.add('containsText',{text:'보류',format:{fill:'#FFF1D5',font:{color:'#855A12'}}});
  }
  sheet.freezePanes.freezeRows(first);
}

async function formatBasicFixtures(){
  const dir=path.join(ROOT,'data/fixtures');
  await fs.mkdir(dir,{recursive:true});
  const originalRows=[
    ['WK-001','ERP 인터페이스 개발','김민수',2,100000,null,'진행'],
    ['WK-002','정산 배치 수정','박서연',3,80000,null,'대기'],
    ['WK-003','고객 요구사항 확인','이도윤',1,150000,null,'완료'],
    ['WK-004','데이터 이관 검증','최지우',4,70000,null,'진행'],
    ['WK-005','권한 설정 점검','정하준',2,90000,null,'대기'],
    ['WK-006','회귀 검증 수행','한수빈',5,60000,null,'진행'],
    ['WK-007','운영 인수인계','송예린',1,120000,null,'보류'],
    ['WK-008','배포 결과 확인','오지호',3,50000,null,'완료']
  ];
  for(const kind of ['baseline','changed','clean','reordered']){
    const destination=path.join(dir,kind+'.xlsx');
    // Rebuild presentation from the preserved source so repeated runs do not
    // accumulate duplicate styles or conditional-format rules.
    let book;
    const originalPath=path.join(FORMAT_WORK,'originals',kind+'.xlsx');
    try{await fs.access(originalPath);book=await SpreadsheetFile.importXlsx(await FileBlob.load(originalPath));}
    catch(error){
      if(error.code!=='ENOENT')throw error;
      book=Workbook.create();const fresh=book.worksheets.add('업무내역');
      const order=kind==='reordered'?[5,1,7,0,3,2,4,6]:[0,1,2,3,4,5,6,7];
      fresh.getRange('A1:G1').values=[['ID','title','owner','quantity','unit_price','total','status']];
      fresh.getRange('A2:G9').values=order.map(i=>[...originalRows[i]]);
      for(let i=2;i<=9;i++)fresh.getRange(`F${i}`).formulas=[[`=D${i}*E${i}`]];
      const valid={rule:{type:'list',values:['대기','진행','완료','보류']}};
      if(kind==='changed'){
        for(const [address,value] of Object.entries({F2:200000,F3:null,F4:{formula:'=D4*#REF!'},C5:null,A7:'WK-005',G8:'확인중',D9:-1}))applyCell(fresh,address,value);
        fresh.getRange('G2:G7').dataValidation=valid;fresh.getRange('G9').dataValidation=valid;
      }else fresh.getRange('G2:G9').dataValidation=valid;
    }
    const sheet=book.worksheets.getItem('업무내역');
    if(kind==='baseline' && await fs.access(originalPath).then(()=>true,()=>false)){
      const originalBook=await SpreadsheetFile.importXlsx(await FileBlob.load(path.join(FORMAT_WORK,'originals','baseline.xlsx')));
      const preview=await originalBook.render({sheetName:'업무내역',range:'A1:I9',scale:1,format:'png'});
      await fs.writeFile(path.join(FORMAT_WORK,'original-basic.png'),new Uint8Array(await preview.arrayBuffer()));
    }
    // A1:G9 are the public detector fixtures. Keep every value, formula and row intact.
    ledgerStyle(sheet,1,9);
    sheet.tabColor='#173F5F';
    for(const [col,width] of [['A',16],['B',34],['C',16],['D',14],['E',17],['F',20],['G',14],['H',3],['I',3],['J',16],['K',21],['L',15]])sheet.getRange(`${col}1:${col}9`).format.columnWidth=width;
    sheet.getRange('I1:L1').merge();sheet.getRange('I1').values=[['ERP 프로젝트 업무·비용 대장']];
    sheet.getRange('I1:L1').format={font:{name:'Arial',size:16,bold:true,color:'#173F5F'},verticalAlignment:'center'};
    sheet.getRange('J2:L9').format={font:{name:'Arial',size:11,color:'#40576A'},rowHeight:32,verticalAlignment:'center'};
    sheet.getRange('J2').values=[['관리 프로젝트']];sheet.getRange('K2:L2').merge();sheet.getRange('K2').values=[['ERP 전환 / 업무관리']];
    sheet.getRange('J3').values=[['검토 기준일']];sheet.getRange('K3:L3').merge();sheet.getRange('K3').values=[[new Date('2026-10-22T00:00:00Z')]];sheet.getRange('K3').setNumberFormat('yyyy-mm-dd');
    sheet.getRange('J4').values=[['단위']];sheet.getRange('K4:L4').merge();sheet.getRange('K4').values=[['수량: 건 / 금액: 원']];
    sheet.getRange('J5:L5').merge();sheet.getRange('J5').values=[['입력과 계산을 구분해 검토합니다.']];
    sheet.getRange('J6:L6').merge();sheet.getRange('J6').values=[['파란 합계 열: 수량 × 단가']];
    sheet.getRange('J7:L7').merge();sheet.getRange('J7').values=[['ID를 유지하면 행 이동을 추적합니다.']];
    sheet.getRange('J8:L8').merge();sheet.getRange('J8').values=[['의도한 오류는 수정본 검사에 사용합니다.']];
    sheet.getRange('J9:L9').merge();sheet.getRange('J9').values=[['검사 후 원본은 보존하고 사본을 검토합니다.']];
    sheet.getRange('J2:L4').format.borders={insideHorizontal:{style:'thin',color:'#CCD7E0'},bottom:{style:'medium',color:'#173F5F'}};
    sheet.getRange('J5:L9').format.font={name:'Arial',size:10,color:'#62788A'};
    book.recalculate();
    const out=await SpreadsheetFile.exportXlsx(book);await out.save(destination);
    try{await fs.rename(destination+'.inspect.ndjson',path.join(FORMAT_WORK,kind+'-export.ndjson'));}catch(e){if(e.code!=='ENOENT')throw e;}
  }
}

// All input records are synthetic. Broken formulas and native error values are
// deliberately requested validation fixtures, not accidental modeling defects.
const HEADERS = ['고유번호', '제목', '담당자', '수량', '단가', '합계', '상태', null, '사례 설명'];
const cases = [];
const sheets = [];
const errorCells = [];
const row = (id, title = '업무 항목', qty = 2, price = 1000, note = '') => [id, title, '김예제', qty, price, null, '진행', null, note];
const addCase = (id, feature, sheet, cell, expect = {}, note = '') => cases.push({ id, feature, sheet, cell, ...expect, note });
const addSheet = (name, description, before, after = before, options = {}) => sheets.push({ name, description, before, after, ...options });

const fBefore = Array.from({ length: 5 }, (_, i) => row(`F-${i+1}`, ['수식 상수화', '수식 누락', '수식 변경', '깨진 참조', '수식이 텍스트로 변경'][i]));
addSheet('01_수식복원', '수식 5개를 의도적으로 변경했습니다. 검토 후 기준 수식을 사본에 복원합니다.', fBefore,
  fBefore.map(r => [...r]), { currentCells: { F6: 2000, F7: null, F8: { formula: '=D8+E8' }, F9: { formula: '=#REF!*E9' }, F10: '수식 삭제' } });
[['F6','formula_replaced'],['F7','missing_formula'],['F8','formula_changed'],['F9','broken_reference'],['F10','formula_replaced']].forEach(([cell, rule],i) => addCase(`F${i+1}`, fBefore[i][1], '01_수식복원', cell, {rule, repairable:true}));

const dataRows = Array.from({length:11},(_,i)=>row(`D-${i+1}`, ['제목 필수','담당자 필수','수량 필수','단가 필수','상태 필수','숫자 대신 문자','숫자 대신 논리값','음수 수량','음수 단가','허용 상태','0은 정상값'][i]));
addSheet('02_데이터검사', '필수값·숫자 입력·음수·상태 규칙을 확인합니다. 0은 비어 있음과 구분합니다.', dataRows, dataRows.map(r=>[...r]),
 {currentCells:{B6:null,C7:null,D8:null,E9:null,G10:null,D11:'열 개',E12:true,D13:-1,E14:-200,G15:'종료',D16:0}});
[['B6','required_value'],['C7','required_value'],['D8','required_value'],['E9','required_value'],['G10','required_value'],['D11','numeric_input'],['E12','numeric_input'],['D13','negative_input'],['E14','negative_input'],['G15','invalid_status']].forEach(([cell,rule],i)=>addCase(`D${i+1}`,dataRows[i][1],'02_데이터검사',cell,{rule,repairable:false}));
addCase('D11','0과 누락 구분','02_데이터검사','D16',{absent_rules:['required_value','negative_input','numeric_input']},'0은 유효한 숫자로 검사합니다.');

const keyed = [row('R-A','순서 A',2,100),row('R-B','순서 B',3,200),row('R-C','순서 C',4,300),row('R-DELETE','삭제될 행',1,500)];
addSheet('03_행비교', 'ID로 행을 맞춥니다. 순서만 바꾼 행은 변경으로 오인하지 않고 추가·삭제를 구분합니다.',keyed,[keyed[2],keyed[0],keyed[1],row('R-ADD','추가된 행',5,700)]);
addCase('R1','행 이동 오탐 방지','03_행비교','A6:G8',{no_changes_for_keys:['R-A','R-B','R-C']});
addCase('R2','추가된 행','03_행비교','A9',{change:'row_added',key:'R-ADD'});
addCase('R3','삭제된 행','03_행비교','기준 A9',{change:'row_removed',key:'R-DELETE'});

const ids = ['B-DUP','B-DUP','C-1','C-2','MISSING','FORMULA',1,' 공백ID '].map((id,i)=>row(id,`ID 사례 ${i+1}`));
const nowIds=ids.map(r=>[...r]);nowIds[1][0]='B-NEW';nowIds[2][0]='C-DUP';nowIds[3][0]='C-DUP';nowIds[4][0]=null;nowIds[6][0]='1';nowIds[7][0]='공백ID';
addSheet('04_ID안전성','중복·빈값·수식 ID에서는 복원을 차단합니다. 숫자/문자 ID와 공백은 임의로 합치지 않습니다.',ids,nowIds,{currentCells:{A11:{formula:'="FORMULA"'},F6:999,F8:999,F10:999}});
addCase('I1','기준 중복 ID','04_ID안전성','A6',{rule:'baseline_duplicate_id',repairable:false});
addCase('I2','수정본 중복 ID','04_ID안전성','A8',{rule:'duplicate_id',repairable:false});
addCase('I3','ID 누락','04_ID안전성','A10',{rule:'missing_id',repairable:false});
addCase('I4','수식 ID 사용 금지','04_ID안전성','A11',{rule:'formula_id',repairable:false});
addCase('I5','숫자/문자 ID 구분','04_ID안전성','A12',{change:'row_added',key:'1'},'숫자 1과 문자 1을 서로 다른 ID로 처리합니다.');
addCase('I6','공백 ID 보존','04_ID안전성','A13',{change:'row_added',key:'공백ID'},'ID의 공백을 자동 제거하지 않습니다.');
addCase('I7','모호한 행의 수식 복원 금지','04_ID안전성','F6,F8,F10',{no_repairs_at:['F6','F8','F10']});

const vals=Array.from({length:4},(_,i)=>row(`V-${i+1}`,['목록 규칙 삭제','목록 규칙 변경','목록 규칙 유지','숫자 범위 변경'][i]));
addSheet('05_유효성규칙','드롭다운 삭제·허용값 변경·숫자 범위 변경을 비교합니다. 규칙 자체는 자동 복원하지 않습니다.',vals,vals,{validation:true});
for(const [id,cell,label] of [['V1','G6','드롭다운 삭제'],['V2','G7','드롭다운 허용값 변경'],['V3','D9','숫자 유효 범위 변경']])addCase(id,label,'05_유효성규칙',cell,{rule:'validation_changed',repairable:false});
addCase('V4','동일 유효성 유지','05_유효성규칙','G8',{absent_rules:['validation_changed']});

const unsupportedFormulas=['=D7*E7','=SUM(D7:E7)','=F8+1','=D9/0','=1/0','=VLOOKUP(A11,A6:E11,4,FALSE)',"='01_수식복원'!D6",'=SUM($D$13:$E$13)','=IF(D14>0,E14,0)','=42'];
const scopeRows=unsupportedFormulas.map((f,i)=>row(`S-${i+1}`,['다른 행 참조','같은 행 SUM','자기참조','0 나누기 한계','상수 0 나누기','미지원 함수','다른 시트 참조','절대 행 참조','지원 IF','상수만 있는 식'][i],2,1000));
const sf=Object.fromEntries(unsupportedFormulas.map((f,i)=>[`F${i+6}`,{formula:f}]));
addSheet('06_수식검사범위','지원하지 않는 수식은 미검증으로 남깁니다. 자기참조와 0 나누기는 의도한 한계 확인 사례입니다.',scopeRows,scopeRows,{baseCells:sf,currentCells:sf});
for(const [id,cell,label] of [['S1','F6','교차행 참조'],['S2','F8','자기참조 미검증'],['S3','F10','상수식 0 나누기'],['S4','F11','미지원 VLOOKUP'],['S5','F12','다른 시트 참조'],['S6','F13','절대 행 참조'],['S7','F15','상수만 있는 식']])addCase(id,label,'06_수식검사범위',cell,{rule:'formula_scope_unverified',repairable:false});
addCase('S8','같은 행 0 나누기 계산 한계','06_수식검사범위','F9',{absent_rules:['stored_error','formula_scope_unverified']},'수식 구조는 지원되지만 0 나누기 계산을 수행하지 않아 오류로 확정하지 못합니다. Excel 재계산이 필요합니다.');
addCase('S9','지원 SUM·IF','06_수식검사범위','F7,F14',{no_findings_at:['F7','F14']});

const errorNames=['#DIV/0!','#VALUE!','#N/A','#NAME?','#NUM!','#NULL!','#SPILL!','#CALC!','#REF!'];
// SPILL/CALC are rich-value errors, not valid legacy t="e" literals. Storing
// their spellings directly makes this workbook unreadable in native Excel.
// Use the documented legacy-compatible VALUE error and state the scope openly.
const errorEncoding=errorNames.map(error=>['#SPILL!','#CALC!'].includes(error)?'#VALUE!':error);
const errors=errorNames.map((name,i)=>row(`E-${i+1}`,i===6?'확장 오류 호환값 A':i===7?'확장 오류 호환값 B':`저장된 ${name}`,2,1000,
  i===6||i===7?`${name} 원형은 범위 밖 · #VALUE! 저장`:''));
addSheet('07_저장오류값','저장된 오류값을 검출합니다. 확장 오류 2종은 #VALUE! 호환값으로 저장하며 원형 검증은 범위 밖입니다.',errors,errors,
 {baseCells:Object.fromEntries(errorNames.map((_,i)=>[`F${i+6}`,0])),currentCells:Object.fromEntries(errorEncoding.map((e,i)=>[`F${i+6}`,e]))});
errorNames.forEach((error,i)=>{errorCells.push({sheet:'07_저장오류값',cell:`F${i+6}`,error:errorEncoding[i]});addCase(`E${i+1}`,i===6||i===7?`${error} 확장 오류의 호환 저장값`:`저장된 ${error}`,'07_저장오류값',`F${i+6}`,{rule:error==='#REF!'?'broken_reference':'stored_error',repairable:false},i===6||i===7?'실제 셀에는 표준 #VALUE! 오류를 저장합니다. 확장 오류의 rich-value 메타데이터와 세부 종류는 이 파일로 검증하지 않습니다.':'');});

addSheet('08_열구조변경','같은 이름의 열은 비교하되 열 구조가 바뀌면 수식을 자동 복원하지 않습니다.',[row('C-1','열 순서 변경')],[['C-1','열 순서 변경',2,'김예제',1000,null,'진행',null,'수식 복원 차단']],
 {currentHeaders:['고유번호','제목','수량','담당자','단가','합계','상태',null,'사례 설명'],currentCells:{F6:{formula:'=C6*E6'}}});
addCase('C1','열 구조 변경','08_열구조변경','시트 전체',{rule:'column_structure_changed',repairable:false});
addCase('C2','열 이동 후 수식 복원 차단','08_열구조변경','F6',{rule:'formula_changed',repairable:false});
addSheet('09_부분스키마','ID 비교는 가능하지만 업무 헤더가 부족하여 필수값·상태 등 업무 규칙은 미검증입니다.',[['P-1','변경 전','김예제']],[['P-1','변경 후','김예제']],{headers:['고유번호','제목','담당자'],noFormulas:true});
addCase('P1','업무 규칙 부분 지원','09_부분스키마','시트 전체',{rule:'business_rules_unavailable',repairable:false});
addCase('P2','부분 스키마의 값 비교','09_부분스키마','B6',{change:'cell_changed'});
addSheet('10_ID헤더없음','ID 헤더가 없어 행 비교와 자동 복원을 수행하지 않습니다.',[['자료 설명',100]],[['자료 설명',200]],{headers:['설명','값'],noFormulas:true});
addCase('H1','ID 없는 스키마','10_ID헤더없음','시트 전체',{rule:'unsupported_schema',repairable:false});
addSheet('11_중복헤더','담당자 헤더가 중복되어 열 의미가 모호한 사례입니다.',[row('H-1','중복 헤더')],[row('H-1','중복 헤더')],{headers:[...HEADERS.slice(0,8),'담당자']});
addCase('H2','중복 헤더','11_중복헤더','시트 전체',{rule:'unsupported_schema',repairable:false});

const supported=['=SUM(D6:E6)','=PRODUCT(D7:E7)','=ROUND(D8*E8,0)','=ROUNDUP(D9*E9,0)','=ROUNDDOWN(D10*E10,0)','=ABS(D11*E11)','=MIN(D12:E12)','=MAX(D13:E13)','=AVERAGE(D14:E14)','=IF(D15>0,D15*E15,0)'];
const goodRows=supported.map((f,i)=>row(`OK-${i+1}`,f.match(/([A-Z]+)\(/)[1]+' 지원 확인'));
const goodCells=Object.fromEntries(supported.map((f,i)=>[`F${i+6}`,{formula:f}]));
addSheet('12_지원함수','동일 행 참조의 지원 함수는 기준과 같으면 경고 없이 검사합니다.',goodRows,goodRows,{baseCells:goodCells,currentCells:goodCells});
addCase('OK1','지원 함수 10종 정상 대조','12_지원함수','F6:F15',{no_findings_sheet:true});
addSheet('13_내보내기안전','CSV의 수식 해석 방지와 HTML 보고서의 텍스트 이스케이프를 확인합니다.',[row('X-1','일반 제목')],[row('X-1','+업무문자 <script>안전</script>')]);
addCase('X1','HTML·CSV 안전 출력','13_내보내기안전','B6',{change:'cell_changed',csv_escaped:true},'HTML 태그는 글자로 표시하고 CSV의 + 시작 텍스트는 작은따옴표로 보호합니다.');
addSheet('14_삭제된시트','수정본에서 제거되는 기준 시트입니다.',[row('DEL-1','삭제 확인')],null,{only:'before'});
addSheet('15_추가된시트','기준에 없는 새 시트는 정적 오류 검색만 수행합니다.',null,[row('ADD-1','추가 확인')],{only:'after',currentCells:{F6:{formula:'=#REF!'}}});
addCase('SH1','시트 삭제','14_삭제된시트','시트 전체',{rule:'sheet_removed',change:'sheet_removed',repairable:false});
addCase('SH2','시트 추가','15_추가된시트','시트 전체',{rule:'sheet_added',change:'sheet_added',repairable:false});
addCase('SH3','추가 시트의 정적 오류','15_추가된시트','F6',{rule:'broken_reference',repairable:false});

function applyCell(sheet, address, value){
  if(value && typeof value==='object' && value.formula) sheet.getRange(address).formulas=[[value.formula]];
  else sheet.getRange(address).values=[[value]];
}
async function build(kind){
  const wb=Workbook.create();const active=sheets.filter(s=>s.only!==(kind==='before'?'after':'before'));
  for(const item of active)wb.worksheets.add(item.name);
  for(const item of active){
    const sheet=wb.worksheets.getItem(item.name), rows=kind==='before'?item.before:item.after;
    const headers=kind==='after'&&item.currentHeaders?item.currentHeaders:item.headers||HEADERS;
    sheet.showGridLines=false;
    sheet.tabColor=item.name.startsWith('01')?'#313743':item.name.startsWith('06')?'#AC8654':'#8B929A';
    const last=5+rows.length;
    sheet.getRange(`A1:I${last}`).format.font={name:'Arial',size:12,color:'#203244'};
    sheet.getRange(`A1:I${last}`).format.rowHeight=32;
    sheet.getRange(`A1:I${last}`).format.verticalAlignment='center';
    for(const [column,width] of [['A',18],['B',34],['C',18],['D',17],['E',17],['F',23],['G',15],['H',3],['I',43]])sheet.getRange(`${column}1:${column}${last}`).format.columnWidth=width;
    sheet.getRange('A1:I1').merge();sheet.getRange('A1').values=[['WORKMATE  /  ERP 업무 품질 검토  /  서식 2']];
    sheet.getRange('A1:I1').format={font:{name:'Arial',size:10,color:'#657D90'},rowHeight:22};
    sheet.getRange('A2:I2').merge();
    sheet.getRange('A2').values=[[item.name.replace(/^\d+_/, '')+' · '+(kind==='before'?'기준본':'수정본')]];
    sheet.getRange('A2:I2').format.font={name:'Arial',size:18,bold:true,color:'#173F5F'};
    sheet.getRange('A2:I2').format.rowHeight=39;
    sheet.getRange('A3:I3').merge();
    sheet.getRange('A3').values=[['업무 예제. '+item.description]];
    sheet.getRange('A3:I3').format.font={name:'Arial',size:11,color:'#536D82'};
    sheet.getRange('A4:C4').merge();sheet.getRange('A4').values=[['프로젝트: ERP 전환 / 업무관리']];
    sheet.getRange('D4').values=[['검토 기준일']];sheet.getRange('E4').values=[[new Date('2026-10-22T00:00:00Z')]];sheet.getRange('E4').setNumberFormat('yyyy-mm-dd');
    sheet.getRange('F4:I4').merge();sheet.getRange('F4').values=[['수량: 건 · 금액: 원  |  원본 보존 · 사본 검토']];
    sheet.getRange('A4:I4').format={fill:'#E5EDF4',font:{name:'Arial',size:10,color:'#36566F'},rowHeight:27,verticalAlignment:'center'};
    sheet.getRange('D4:E4').format.horizontalAlignment='center';
    sheet.getRangeByIndexes(4,0,1,headers.length).values=[headers];
    sheet.getRange(`A5:${String.fromCharCode(64+Math.min(headers.length,7))}5`).format={fill:'#374151',font:{name:'Arial',size:11,bold:true,color:'#FFFFFF'},horizontalAlignment:'center',rowHeight:28};
    if(headers.length===9){sheet.getRange('I5').format.font={name:'Arial',size:10,bold:true,color:'#6B7280'};sheet.getRange(`I6:I${last}`).format.font={name:'Arial',size:10,color:'#6B7280'};}
    for(let i=0;i<rows.length;i++){
      const cells=[...rows[i]];while(cells.length<headers.length)cells.push(null);
      sheet.getRangeByIndexes(i+5,0,1,headers.length).values=[cells];
      if(!item.noFormulas)sheet.getRange(`F${i+6}`).formulas=[[`=D${i+6}*E${i+6}`]];
      if(i%2===1)sheet.getRange(`A${i+6}:G${i+6}`).format.fill='#F5F6F8';
    }
    ledgerStyle(sheet,5,last,String.fromCharCode(64+Math.min(headers.length,7)));
    const edits=kind==='before'?item.baseCells:item.currentCells;
    for(const [address,value] of Object.entries(edits||{}))applyCell(sheet,address,value);
    if(item.validation){
      for(const cell of kind==='before'?['G6','G7','G8']:['G8'])sheet.getRange(cell).dataValidation={rule:{type:'list',values:['대기','진행','완료','보류']}};
      if(kind==='after')sheet.getRange('G7').dataValidation={rule:{type:'list',values:['대기','진행','완료']}};
      sheet.dataValidations.add({range:'D9',rule:{type:'whole',operator:'between',formula1:kind==='before'?0:1,formula2:kind==='before'?100:50}});
    }
    if(item.name==='02_데이터검사'){
      sheet.getRange(`D6:E${last}`).conditionalFormats.add('cellIs',{operator:'lessThan',formula:0,format:{fill:'#FCE8E6',font:{color:'#A6332D'}}});
      sheet.getRange(`B6:C${last}`).conditionalFormats.add('containsBlanks',{format:{fill:'#FFF1CF'}});
    }
    sheet.freezePanes.freezeRows(5);
  }
  wb.recalculate();
  const errorScan=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:100},maxChars:10000,summary:'Intentional validation error fixtures'});
  await fs.writeFile(path.join(WORK,`${kind}-formula-scan.ndjson`),errorScan.ndjson);
  const inspection=await wb.inspect({kind:'table',range:'01_수식복원!A5:G10',include:'values,formulas',tableMaxRows:6,tableMaxCols:7,maxChars:4000});
  await fs.writeFile(path.join(WORK,`${kind}-key-range.ndjson`),inspection.ndjson);
  for(const item of active){
    const last=5+(kind==='before'?item.before:item.after).length;
    const preview=await wb.render({sheetName:item.name,range:`A1:I${last}`,scale:1,format:'png'});
    await fs.writeFile(path.join(WORK,`${kind}-${item.name}.png`),new Uint8Array(await preview.arrayBuffer()));
  }
  const destination=path.join(OUT,kind==='before'?'Excel_검증_기준본.xlsx':'Excel_검증_수정본.xlsx');
  const output=await SpreadsheetFile.exportXlsx(wb);await output.save(destination);
  console.log(`${kind}: exported ${active.length} sheets`);
  return destination;
}

await formatBasicFixtures();
const baseline=await build('before');
const current=await build('after');
for(const [kind, name] of [['before','Excel_검증_기준본.xlsx'],['after','Excel_검증_수정본.xlsx']]){
  const source=path.join(OUT,name+'.inspect.ndjson');
  try {await fs.rename(source,path.join(WORK,kind+'-export-inspect.ndjson'));} catch(error){if(error.code!=='ENOENT')throw error;}
}
await fs.writeFile(path.join(WORK,'expected-cases.json'),JSON.stringify({synthetic:true,cases,errorCells},null,2));

// Public artifact-tool range.values supports only string/number/bool/Date/null.
// Native stored Excel error literals have no documented setter. Patch only the
// nine explicitly requested error cells to OOXML t="e"; all authoring is above.
const verifyPython=String.raw`
import json, sys, hashlib, html, csv, io, gc, re
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET
root, base, current, work = map(Path, sys.argv[1:])
sys.path.insert(0,str(root))
from backend.excel_agent import inspect_workbook, inspect_workbooks, repair_workbook
from openpyxl import load_workbook
manifest=json.loads((work/'expected-cases.json').read_text(encoding='utf-8'))
ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
with ZipFile(current) as z: parts={n:z.read(n) for n in z.namelist()}
book=ET.fromstring(parts['xl/workbook.xml'])
rels=ET.fromstring(parts['xl/_rels/workbook.xml.rels'])
targets={r.attrib['Id']:r.attrib['Target'] for r in rels}
for sheet in book.find('s:sheets',ns):
    if sheet.attrib['name']!='07_저장오류값': continue
    target=targets[sheet.attrib['{'+ns['r']+'}id']]
    name=target.lstrip('/') if target.startswith('/') else 'xl/'+target
    tree=ET.fromstring(parts[name])
    for spec in manifest['errorCells']:
        cell=tree.find('.//s:c[@r="'+spec['cell']+'"]',ns)
        assert cell is not None
        for child in list(cell): cell.remove(child)
        cell.set('t','e');ET.SubElement(cell,'{'+ns['s']+'}v').text=spec['error']
    parts[name]=ET.tostring(tree,encoding='utf-8',xml_declaration=True)
with ZipFile(current,'w',ZIP_DEFLATED) as z:
    for name,data in parts.items():z.writestr(name,data)
# The public worksheet help exposes no print/pageSetup/autoFilter setter.
# Add only these native OOXML view/print controls; styles and cell contents are
# authored through artifact-tool above. No formulas, values or validations change.
native_checks=[]
for source in [root/'data'/'fixtures'/f'{name}.xlsx' for name in ('baseline','changed','clean','reordered')]+[base,current]:
    basic=source.parent.name=='fixtures'
    with ZipFile(source) as z:package={n:z.read(n) for n in z.namelist()}
    workbook=ET.fromstring(package['xl/workbook.xml'])
    relationships=ET.fromstring(package['xl/_rels/workbook.xml.rels'])
    targets={r.attrib['Id']:r.attrib['Target'] for r in relationships}
    names=workbook.find('s:definedNames',ns)
    if names is None:
        names=ET.Element('{'+ns['s']+'}definedNames')
        calc=workbook.find('s:calcPr',ns)
        workbook.insert(list(workbook).index(calc) if calc is not None else len(workbook),names)
    for child in list(names):
        if child.get('name') in ('_xlnm.Print_Area','_xlnm.Print_Titles'):names.remove(child)
    for index,sheet in enumerate(workbook.find('s:sheets',ns)):
        target=targets[sheet.attrib['{'+ns['r']+'}id']]
        filename=target.lstrip('/') if target.startswith('/') else 'xl/'+target
        xml=ET.fromstring(package[filename]);header=1 if basic else 5
        data=xml.find('s:sheetData',ns);last=max(int(r.get('r')) for r in data)
        for tag in ('autoFilter','printOptions','pageMargins','pageSetup'):
            prior=xml.find('s:'+tag,ns)
            if prior is not None:xml.remove(prior)
        header_cells=data.find('s:row[@r="'+str(header)+'"]',ns)
        columns=[re.match(r'[A-Z]+',c.get('r')).group() for c in header_cells if c.find('s:v',ns) is not None]
        filter_end=max((c for c in columns if len(c)==1 and c<='G'),default='G')
        filter_node=ET.Element('{'+ns['s']+'}autoFilter',{'ref':f'A{header}:{filter_end}{last}'})
        xml.insert(list(xml).index(data)+1,filter_node)
        # These must follow validation/conditional-format elements in worksheet order.
        ET.SubElement(xml,'{'+ns['s']+'}printOptions',{'horizontalCentered':'1'})
        ET.SubElement(xml,'{'+ns['s']+'}pageMargins',{'left':'0.25','right':'0.25','top':'0.35','bottom':'0.35','header':'0.15','footer':'0.15'})
        ET.SubElement(xml,'{'+ns['s']+'}pageSetup',{'paperSize':'9','orientation':'landscape','fitToWidth':'1','fitToHeight':'0'})
        props=xml.find('s:sheetPr',ns)
        if props is None:props=ET.Element('{'+ns['s']+'}sheetPr');xml.insert(0,props)
        setup=props.find('s:pageSetUpPr',ns)
        if setup is None:setup=ET.SubElement(props,'{'+ns['s']+'}pageSetUpPr')
        setup.set('fitToPage','1')
        escaped=sheet.get('name').replace("'","''")
        ET.SubElement(names,'{'+ns['s']+'}definedName',{'name':'_xlnm.Print_Area','localSheetId':str(index)}).text=f"'{escaped}'!"+'$A$1:$'+('L' if basic else 'I')+'$'+str(last)
        ET.SubElement(names,'{'+ns['s']+'}definedName',{'name':'_xlnm.Print_Titles','localSheetId':str(index)}).text=f"'{escaped}'!"+'$1:$'+str(header)
        package[filename]=ET.tostring(xml,encoding='utf-8',xml_declaration=True)
    package['xl/workbook.xml']=ET.tostring(workbook,encoding='utf-8',xml_declaration=True)
    with ZipFile(source,'w',ZIP_DEFLATED) as z:
        for filename,data in package.items():z.writestr(filename,data)
    styles=ET.fromstring(package['xl/styles.xml'])
    styled=load_workbook(source,data_only=False)
    assert len(styles.find('s:borders',ns))>1
    for ws in styled:
        assert ws.freeze_panes and ws.auto_filter.ref and ws.print_area and ws.page_setup.orientation=='landscape'
        assert ws.cell(1 if basic else 5,1).fill.fgColor.rgb=='FF173F5F'
    native_checks.append({'filename':source.name,'sheets':len(styled.worksheets),'borders':len(styles.find('s:borders',ns)),'fills':len(styles.find('s:fills',ns)),'cell_styles':len(styles.find('s:cellXfs',ns)),'freeze_filter_print':True})
    styled.close()
(work/'native-format-v2.json').write_text(json.dumps(native_checks,ensure_ascii=False,indent=2),encoding='utf-8')
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
original_hashes=[sha(base),sha(current)]
inspection=inspect_workbook(base,current,work/'inspection')
repair=repair_workbook(base,current,work/'repair-proof')
assert repair['applied_count']>=3
assert original_hashes==[sha(base),sha(current)],'Originals changed'
saved=load_workbook(work/'repair-proof'/'repaired.xlsx',data_only=False)
assert all(saved[a['sheet']][a['cell']].value==a['after'] for a in repair['applied'])
saved.close()
batch=inspect_workbooks(base,[current,base],work/'batch-proof')
assert batch['batch_counts']['total_files']==2
findings=inspection['findings'];changes=inspection['changes']
matrix=[]
for case in manifest['cases']:
    sheet,cell=case['sheet'],case['cell']
    local=[f for f in findings if f['sheet']==sheet and (f['cell']==cell or cell=='시트 전체')]
    checks=[]
    if case.get('rule'):checks.append(any(f['rule']==case['rule'] for f in local))
    if case.get('change'):checks.append(any(c['sheet']==sheet and c['type']==case['change'] and (case.get('key') is None or c.get('key')==case['key']) and (cell=='시트 전체' or case['change']!='cell_changed' or c.get('cell')==cell) for c in changes))
    if 'repairable' in case:
        checks.append(any(f['repairable'] for f in local) if case['repairable'] else not any(f['repairable'] for f in local))
    if case.get('absent_rules'):checks.append(not any(f['rule'] in case['absent_rules'] for f in local))
    if case.get('no_findings_at'):checks.append(not any(f['sheet']==sheet and f['cell'] in case['no_findings_at'] for f in findings))
    if case.get('no_findings_sheet'):checks.append(not any(f['sheet']==sheet for f in findings))
    if case.get('no_changes_for_keys'):checks.append(not any(c['sheet']==sheet and c.get('key') in case['no_changes_for_keys'] for c in changes))
    if case.get('no_repairs_at'):checks.append(not any(f['sheet']==sheet and f['cell'] in case['no_repairs_at'] and f['repairable'] for f in findings))
    if case.get('csv_escaped'):
        rows=list(csv.DictReader((work/'inspection'/'changes.csv').open(encoding='utf-8-sig')))
        checks.append(any(r['sheet']==sheet and r['cell']==cell and r['after'].startswith("'+") for r in rows))
        html_result=(work/'inspection'/'inspection.html').read_text(encoding='utf-8')
        checks.append('<script>안전</script>' not in html_result and '&lt;script&gt;안전&lt;/script&gt;' in html_result)
    matrix.append({**case,'passed':all(checks),'actual_rules':sorted({f['rule'] for f in local}),'actual_repairs':[a for a in repair['applied'] if a['sheet']==sheet and a['cell']==cell]})
summary={'case_count':len(matrix),'passed':sum(c['passed'] for c in matrix),'counts':inspection['counts'],'repair_count':repair['applied_count'],'remaining_counts':repair['remaining']['counts'],'original_preserved':True,'recalculated_by_agent':False,'formula_scope':inspection['formula_scope'],'coverage':inspection['coverage'],'baseline_sha256':sha(base),'current_sha256':sha(current),'cases':matrix}
(work/'verified-case-matrix.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
h=lambda x:html.escape(str(x))
rows=''.join('<tr><td>'+h(c['id'])+'</td><td>'+h(c['feature'])+'</td><td><strong>'+h(c['sheet'])+'</strong><br>'+h(c['cell'])+'</td><td><code>'+h(c.get('rule') or c.get('change') or '오탐 없음 / 범위 확인')+'</code><br>'+h(c['note'])+'</td><td>'+('기준 수식 사본 복원' if c.get('repairable') else '복원 대상 아님')+'</td><td>'+('확인' if c['passed'] else '확인 필요')+'</td></tr>' for c in matrix)
guide='''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Excel 검증 예제 안내</title><style>body{font:14px/1.7 system-ui,"Malgun Gothic",sans-serif;color:#30343b;background:#f6f7f9;margin:0;padding:32px}main{max-width:1300px;margin:auto}header,section{background:white;border:1px solid #e0e3e8;border-radius:12px;padding:26px;margin-bottom:20px}h1{font-size:29px}h2{font-size:20px}a{color:#364f70}.note{border-left:3px solid #ae894d;background:#fbf6eb;padding:12px 16px}.numbers{display:flex;gap:30px;font-size:18px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;vertical-align:top;padding:12px;border-bottom:1px solid #e6e8ec}th{background:#eef1f5;white-space:nowrap}code{font:12px Consolas,monospace;overflow-wrap:anywhere}small{color:#747b85}@media print{body{padding:0;background:white}tr{break-inside:avoid}}</style></head><body><main><header><h1>Excel 검증 기능을 확인하는 두 파일</h1><p>검사 기능을 확인하기 위한 업무 예제입니다. 수정본에는 검사를 위해 의도적으로 잘못된 수식과 값을 넣었습니다.</p><p><a download href="Excel_검증_기준본.xlsx">기준본 다운로드</a>　<a download href="Excel_검증_수정본.xlsx">수정본 다운로드</a></p><div class="numbers"><span>확인한 사례 <strong>CASE_COUNT개</strong></span><span>실제 사본 복원 <strong>REPAIR_COUNT셀</strong></span></div><p class="note">기준본도 일부 시트에 동일한 미지원 수식·중복 ID를 포함한 대조 실험입니다. 정상 업무용 양식이 아닙니다. 재계산 없는 정적 검사의 범위를 확인하는 예제입니다.</p></header><section><h2>실행 순서</h2><ol><li>Workmate의 Excel 검증에서 기준 원본에 기준본, 수정본에 수정본을 업로드합니다.</li><li>검증 실행 후 발견 사항, 변경 목록, 지원 범위를 확인합니다.</li><li>01_수식복원 시트의 제안을 검토하고 사본 복구를 실행합니다. 원본 두 파일은 바뀌지 않습니다.</li><li>복원 사본을 다시 검사합니다. 데이터 오류·모호한 ID·미지원 수식은 남는 것이 정상입니다.</li><li>일괄 검증은 동일 기준본에 수정본과 기준본을 각각 비교해 볼 수 있습니다. 최대 10개 수정본을 독립 비교하며 일괄 복원은 지원하지 않습니다.</li></ol><p>실제 실행 결과: COUNTS. 복원 REPAIR_COUNT셀. 원본 SHA-256 보존을 확인했습니다.</p></section><section><h2>시트·셀별 기대 결과와 실제 확인</h2><div class="scroll"><table><thead><tr><th>사례</th><th>확인 기능</th><th>위치</th><th>예상 판정 및 한계</th><th>복원</th><th>실행 확인</th></tr></thead><tbody>ROWS</tbody></table></div></section><section><h2>두 파일로 보장하지 않는 범위</h2><ul><li>수식 재계산, 저장된 계산값의 최신성, 모든 순환 참조 및 업무상 정답은 보장하지 않습니다. 특히 06_수식검사범위!F9의 0 나누기는 정적 검사에서 확정하지 않습니다.</li><li>드롭다운 정의의 변경은 비교하지만 모든 사용자 정의 유효성 수식을 계산하거나 규칙을 자동 복원하지 않습니다.</li><li>용량·압축 해제 한도, 20만 셀 제한, 손상 ZIP, 매크로 거절, 일괄 일부 실패, 차트·피벗·외부 연결 등 보존 불가 파일의 복원 차단은 별도 API/단위 검증 범위입니다. 이 두 파일에 손상·대용량·외부 연결은 넣지 않았습니다.</li><li>배열 수식·데이터 표·외부 파일 참조의 모든 형태는 이 예제에서 직접 확인하지 않습니다. #SPILL!·#CALC!의 rich-value 원형은 범위 밖이며, 07번 시트의 두 사례는 Excel에서 열리는 표준 #VALUE! 호환 오류로 저장했습니다. 이 문서의 확인 결과는 표에 명시한 사례에 한정됩니다.</li><li>수식의 네이티브 재계산 검증은 수행하지 않았습니다. 제작 도구로 저장된 모든 시트를 다시 읽어 렌더링했으며, 실제 검사 엔진·사본 복원을 확인했습니다.</li></ul></section><section><h2>근거 파일</h2><p>프로젝트의 data/examples/excel_showcase/에 실제 검사 HTML·JSON·CSV, 복원 로그, 파일별 화면, verified-case-matrix.json을 저장했습니다.</p><p><small>기준 SHA: BASE_SHA<br>수정 SHA: CURRENT_SHA</small></p></section></main></body></html>'''
guide=guide.replace('CASE_COUNT',str(len(matrix))).replace('REPAIR_COUNT',str(repair['applied_count'])).replace('COUNTS',h(inspection['summary'])).replace('ROWS',rows).replace('BASE_SHA',sha(base)).replace('CURRENT_SHA',sha(current))
(base.parent/'Excel_검증_예제_안내.html').write_text(guide,encoding='utf-8')
print(json.dumps({'cases':len(matrix),'passed':sum(c['passed'] for c in matrix),'repair_count':repair['applied_count'],'counts':inspection['counts'],'failures':[c['id'] for c in matrix if not c['passed']]},ensure_ascii=False))
assert all(c['passed'] for c in matrix), 'Showcase expectation failed'
`;
const checked=spawnSync(bundledPython,['-c',verifyPython,ROOT,baseline,current,WORK],{cwd:ROOT,encoding:'utf8',maxBuffer:3_000_000});
process.stdout.write(checked.stdout||'');process.stderr.write(checked.stderr||'');
if(checked.status!==0)process.exit(checked.status||1);
// Inspect the exported OOXML through a fresh import, not only the in-memory authoring view.
for(const filename of [
  ...['baseline','changed','clean','reordered'].map(n=>path.join(ROOT,'data/fixtures',n+'.xlsx')),
  baseline,current
]){
  const book=await SpreadsheetFile.importXlsx(await FileBlob.load(filename));
  const inventory=await book.inspect({kind:'sheet',include:'id,name',maxChars:10000});
  await fs.writeFile(path.join(FORMAT_WORK,path.basename(filename)+'.sheet-inspect.ndjson'),inventory.ndjson);
  const isBasic=path.dirname(filename).endsWith('fixtures');
  const active=isBasic?['업무내역']:sheets.filter(s=>s.only!==(filename===baseline?'after':'before')).map(s=>s.name);
  for(const name of active){
    const item=sheets.find(s=>s.name===name);
    const last=isBasic?9:5+(filename===baseline?item.before:item.after).length;
    const preview=await book.render({sheetName:name,range:`A1:${isBasic?'L':'I'}${last}`,scale:1,format:'png'});
    await fs.writeFile(path.join(FORMAT_WORK,path.basename(filename,'.xlsx')+'-'+name+'.png'),new Uint8Array(await preview.arrayBuffer()));
  }
}
for(const filename of ['Excel_검증_기준본.xlsx','Excel_검증_수정본.xlsx','Excel_검증_예제_안내.html'])await fs.copyFile(path.join(OUT,filename),path.join(WORK,filename));
console.log('Format 2: 6 workbooks exported, 32 sheets re-imported/rendered, app example sources synchronized.');
