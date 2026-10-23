export async function api(path, options = {}) {
  const { body, ...rest } = options;
  const multipart = body instanceof FormData;
  let response;
  try {
    response = await fetch(`/api${path}`, {
      ...rest,
      headers: { ...(!multipart && body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...rest.headers },
      ...(body !== undefined ? { body: multipart ? body : JSON.stringify(body) } : {}),
    });
  } catch { throw new Error('서버에 연결하지 못했습니다. Workmate 서버 실행 상태를 확인해 주세요.'); }
  const raw = await response.text();
  let data;
  try { data = raw ? JSON.parse(raw) : {}; } catch { data = null; }
  if (!response.ok) {
    const detail = data?.detail;
    throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(x => x.msg || JSON.stringify(x)).join(', ') : `요청 처리에 실패했습니다 (${response.status}).`);
  }
  if (data === null) throw new Error('서버 응답을 읽지 못했습니다. API 서버가 실행 중인지 확인해 주세요.');
  return data;
}
export const post = (path, body = {}) => api(path, { method: 'POST', body });
export const asText = value => value === null || value === undefined ? '' : typeof value === 'string' ? value : typeof value === 'object' ? JSON.stringify(value) : String(value);
export const dateTime = value => {
  if (!value) return '아직 실행 전';
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${Number(value.slice(5, 7))}. ${Number(value.slice(8, 10))}. · 종일`;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? asText(value) : date.toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Seoul' });
};
