// fieldecho.org snapshot: static assets answer a Range request with the whole file, and Safari
// and iPhones need a 206 to play video. This runs only for the MP4 (see wrangler.toml).
export function byteRange(header, size) {
  const match = /^bytes=(\d*)-(\d*)$/.exec(header || '');
  if (!match || (match[1] === '' && match[2] === '')) return null;
  let start, end;
  if (match[1] === '') {
    start = Math.max(size - Number(match[2]), 0);
    end = size - 1;
  } else {
    start = Number(match[1]);
    end = match[2] === '' ? size - 1 : Math.min(Number(match[2]), size - 1);
  }
  return start > end || start >= size ? 'unsatisfiable' : {start, end};
}

export default {
  async fetch(request, env) {
    const response = await env.ASSETS.fetch(request);
    const header = request.headers.get('Range');
    const headers = new Headers(response.headers);
    headers.set('Accept-Ranges', 'bytes');
    if (!response.ok || !header || request.method !== 'GET') {
      return new Response(response.body, {status: response.status, headers});
    }
    const body = await response.arrayBuffer();
    const range = byteRange(header, body.byteLength);
    if (range === null) return new Response(body, {status: 200, headers});
    if (range === 'unsatisfiable') {
      return new Response(null, {status: 416, headers: {'Content-Range': `bytes */${body.byteLength}`}});
    }
    headers.set('Content-Range', `bytes ${range.start}-${range.end}/${body.byteLength}`);
    headers.set('Content-Length', String(range.end - range.start + 1));
    return new Response(body.slice(range.start, range.end + 1), {status: 206, headers});
  },
};
