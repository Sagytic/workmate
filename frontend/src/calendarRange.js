export function koreaDate(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en', {timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(now);
  const value = type => parts.find(part => part.type === type).value;
  return `${value('year')}-${value('month')}-${value('day')}`;
}

export function monthRange(offset = 0, now = new Date()) {
  const today = koreaDate(now);
  const year = Number(today.slice(0,4)), month = Number(today.slice(5,7)) - 1 + offset;
  return {start:new Date(Date.UTC(year,month,1)).toISOString().slice(0,10),end:new Date(Date.UTC(year,month+1,0)).toISOString().slice(0,10)};
}

export function calendarRange(mode, start, end, now = new Date()) {
  if (mode === 'this_month' || mode === 'next_month') return monthRange(mode === 'next_month' ? 1 : 0, now);
  if (mode === 'custom') return {start,end};
  return null;
}

export function rangeError(range) {
  if (!range) return '';
  const valid = value => /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0,10) === value;
  if (!valid(range.start) || !valid(range.end)) return '조회할 시작일과 종료일을 입력해 주세요.';
  const days = (Date.parse(range.end) - Date.parse(range.start)) / 86400000 + 1;
  if (days < 1) return '종료일은 시작일과 같거나 이후여야 합니다.';
  if (days > 366) return '한 번에 최대 366일까지 조회할 수 있습니다.';
  return '';
}
