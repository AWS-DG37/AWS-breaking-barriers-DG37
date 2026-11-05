import test from 'node:test';
import assert from 'node:assert/strict';
import { bannerView, pollDeploymentStatus } from './deployment-banner.mjs';

const evidence = state => ({state, run_id:123, run_number:5, run_attempt:1,
  serving_sha:'a'.repeat(40), target_sha:'a'.repeat(40), conclusion:null});
const flush = () => new Promise(resolve => setImmediate(resolve));

test('same-revision active/failed states stay visible; only current/disabled hide', () => {
  for (const state of ['queued','running','failed','unconfirmed','unknown']) {
    assert.equal(bannerView(state === 'unknown' ? {state} : evidence(state)).visible, true);
  }
  assert.equal(bannerView({...evidence('current'), conclusion:'success'}).visible, false);
  assert.equal(bannerView({state:'disabled'}).visible, false);
});

test('untrusted URLs and HTML cannot enter banner presentation', () => {
  const view = bannerView({...evidence('failed'), html_url:'https://evil.org',
    message:'<img src=x onerror=bad()>'});
  assert.equal(view.href, 'https://github.com/AWS-DG37/AWS-breaking-barriers-DG37/actions/runs/123');
  assert.equal(view.text.includes('<'), false);
  for (const bad of [null, {}, {state:'invented'}, {state:'constructor'}, {...evidence('running'),run_id:'123'},
    {...evidence('current'),target_sha:'bad'}]) {
    assert.equal(bannerView(bad).text, 'Update status is unavailable.');
    assert.equal(bannerView(bad).href, null);
  }
});

function fixture(fetchImpl) {
  const listeners = new Map(), timers = new Map();
  let sequence = 0;
  const doc = {hidden:false,
    addEventListener:(event,fn) => listeners.set(event,fn),
    removeEventListener:event => listeners.delete(event)};
  const seen = [];
  const stop = pollDeploymentStatus(doc, view => seen.push(view), {
    fetchImpl, setTimer:(fn,ms) => { const id=++sequence; timers.set(id,{fn,ms}); return id; },
    clearTimer:id => timers.delete(id)});
  return {doc, timers, listeners, seen, stop};
}

test('polls active updates every5s, pauses hidden and refreshes on resume', async () => {
  let calls=0;
  const f=fixture(async (url,options) => {
    calls++; assert.equal(url,'/api/deployment-status'); assert.equal(options.cache,'no-store');
    return {ok:true,text:async () => JSON.stringify(evidence('running'))};
  });
  await flush();
  assert.equal(calls,1);
  assert.equal([...f.timers.values()][0].ms,5000);
  f.doc.hidden=true; f.listeners.get('visibilitychange')();
  assert.equal(f.timers.size,0);
  f.doc.hidden=false; f.listeners.get('visibilitychange')(); await flush();
  assert.equal(calls,2);
  f.stop(); assert.equal(f.timers.size,0); assert.equal(f.listeners.size,0);
});

test('idle and unknown status keep the slower local polling interval', async () => {
  for (const state of ['current', 'failed', 'unconfirmed', 'unknown']) {
    const f=fixture(async () => ({ok:true,text:async () => JSON.stringify(evidence(state))}));
    await flush();
    assert.equal([...f.timers.values()][0].ms,20000);
    f.stop();
  }
});

test('disabled ends polling; transport failures stay visible; teardown ignores late response', async () => {
  const disabled=fixture(async () => ({ok:true,text:async () => '{"state":"disabled"}'}));
  await flush(); assert.equal(disabled.timers.size,0); disabled.stop();
  const bad=fixture(async () => {throw new Error('secret diagnostic');});
  await flush(); assert.equal(bad.seen.at(-1).text,'Update status is unavailable.');
  assert.equal([...bad.timers.values()][0].ms,20000); bad.stop();
  let release;
  const late=fixture(() => new Promise(resolve => release=resolve));
  late.stop(); release({ok:true,text:async () => JSON.stringify(evidence('running'))});
  await flush(); assert.equal(late.seen.length,0); assert.equal(late.timers.size,0);
});

test('single flight does not duplicate requests on visibility events', async () => {
  let calls=0,release;
  const f=fixture(() => {calls++; return new Promise(resolve => release=resolve);});
  f.listeners.get('visibilitychange')(); f.listeners.get('visibilitychange')();
  assert.equal(calls,1);
  release({ok:true,text:async () => JSON.stringify(evidence('running'))});
  await flush(); f.stop();
});

test('back-forward cached page remounts banner after pagehide teardown', async () => {
  const win = new EventTarget();
  win.Event = Event;
  win.ResizeObserver = class { observe() {} disconnect() {} };
  const doc = new EventTarget();
  doc.defaultView = win;
  doc.hidden = false;
  const properties = new Map(), nodes = [];
  doc.documentElement = {style:{setProperty:(key,value) => properties.set(key,value)}};
  doc.body = {prepend:node => nodes.push(node)};
  doc.createElement = () => ({hidden:false, setAttribute() {}, removeAttribute() {}, append() {},
    getBoundingClientRect:() => ({height:48}), remove() {nodes.splice(nodes.indexOf(this),1);} });
  const previousDoc=globalThis.document, previousFetch=globalThis.fetch;
  globalThis.document=doc;
  globalThis.fetch=async () => ({ok:true,text:async () => '{"state":"disabled"}'});
  try {
    await import('./deployment-banner.mjs?bfcache-test');
    await flush(); assert.equal(nodes.length,1);
    win.dispatchEvent(new Event('pagehide')); assert.equal(nodes.length,0);
    assert.equal(properties.get('--deployment-banner-height'),'0px');
    const event=new Event('pageshow'); event.persisted=true; win.dispatchEvent(event);
    await flush(); assert.equal(nodes.length,1);
    win.dispatchEvent(new Event('pagehide')); assert.equal(nodes.length,0);
  } finally {
    globalThis.document=previousDoc;
    globalThis.fetch=previousFetch;
  }
});
