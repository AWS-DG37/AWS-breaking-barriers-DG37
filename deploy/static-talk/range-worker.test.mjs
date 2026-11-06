import assert from 'node:assert/strict';
import {test} from 'node:test';
import worker, {byteRange} from './range-worker.js';

const bytes = new Uint8Array(100).map((_, i) => i);
const env = {ASSETS: {fetch: async () => new Response(bytes, {headers: {'Content-Type': 'video/mp4'}})}};
const get = range => worker.fetch(new Request('https://x/v.mp4', {headers: range ? {Range: range} : {}}), env);

test('byteRange reads the forms browsers send', () => {
  assert.deepEqual(byteRange('bytes=0-1', 100), {start: 0, end: 1});
  assert.deepEqual(byteRange('bytes=90-', 100), {start: 90, end: 99});
  assert.deepEqual(byteRange('bytes=-10', 100), {start: 90, end: 99});
  assert.deepEqual(byteRange('bytes=0-999', 100), {start: 0, end: 99});
  assert.equal(byteRange('bytes=100-', 100), 'unsatisfiable');
  assert.equal(byteRange('bytes=5-2', 100), 'unsatisfiable');
  assert.equal(byteRange('bytes=0-1,5-6', 100), null);
  assert.equal(byteRange(null, 100), null);
});

test('a range request gets a 206 with exactly those bytes', async () => {
  const response = await get('bytes=10-19');
  assert.equal(response.status, 206);
  assert.equal(response.headers.get('Content-Range'), 'bytes 10-19/100');
  assert.equal(response.headers.get('Content-Type'), 'video/mp4');
  assert.deepEqual([...new Uint8Array(await response.arrayBuffer())], [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]);
});

test('no range gets the whole file and advertises ranges', async () => {
  const response = await get();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('Accept-Ranges'), 'bytes');
  assert.equal((await response.arrayBuffer()).byteLength, 100);
});

test('a range past the end gets a 416', async () => {
  const response = await get('bytes=500-');
  assert.equal(response.status, 416);
  assert.equal(response.headers.get('Content-Range'), 'bytes */100');
});
