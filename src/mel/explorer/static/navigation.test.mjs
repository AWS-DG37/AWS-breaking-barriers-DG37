import test from 'node:test';
import assert from 'node:assert/strict';
import {api, requestScope} from './api.mjs';

// A small DOM fixture exercises screen ownership with the actual rendering code.
// Browser layout and native input behavior are outside these tests.
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.attrs={}; this.dataset={}; this.listeners={}; }
  setAttribute(key,value) { this.attrs[key]=value; if(key==='value') this.value=value; if(key.startsWith('data-')) this.dataset[key.slice(5)]=value; }
  getAttribute(key) { return this.attrs[key]; }
  append(...nodes) { for(const n of nodes) { n.parentElement=this; this.children.push(n); } }
  replaceChildren(...nodes) { this.children=[]; this.append(...nodes); }
  addEventListener(type,fn) { this.listeners[type]=fn; }
  get textContent() { return this.text ?? this.children.map(c=>c.textContent).join(''); }
  set textContent(text) { this.text=text; this.children=[]; }
  querySelectorAll(selector) {
    const matches=node=>selector.startsWith('.')?node.className?.split(' ').includes(selector.slice(1)):
      selector==='tr[aria-selected]'?node.tagName==='tr' && 'aria-selected' in node.attrs:
      selector.startsWith('[')?node.attrs[selector.slice(1,-1).split('=')[0]]===selector.slice(1,-1).split('=')[1]?.replaceAll('"',''):
      node.tagName===selector;
    return this.children.flatMap(child=>[...(matches(child)?[child]:[]),...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
globalThis.Node=Element;
globalThis.document={createElement:tag=>new Element(tag),createTextNode:text=>{const node=new Element('#text');node.textContent=text;return node;}};
globalThis.location={hash:'',replace(hash){this.hash=hash;}};
globalThis.window={addEventListener(){}};
const {showGenerate,leaveGenerate}=await import('./generate.mjs');
const {showConfigure,leaveConfigure}=await import('./configure.mjs');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
const response=value=>({ok:true,status:200,json:async()=>value});

test('a canceled response body cannot update the caller, even if fetch already completed',async()=>{
  const body=deferred();
  globalThis.fetch=async()=>({ok:true,status:200,json:()=>body.promise});
  const scope=requestScope();
  const pending=scope.api('/runs');
  await tick(); scope.cancel(); body.resolve({runs:[]});
  await assert.rejects(pending,{name:'AbortError'});
  scope.renew(); globalThis.fetch=async()=>response({runs:[]});
  assert.deepEqual(await scope.api('/runs'),{runs:[]});
});

test('canceled unauthorized responses do not trigger a sign-in redirect',async()=>{
  const controller=new AbortController(); controller.abort();
  globalThis.fetch=async()=>({status:401});
  await assert.rejects(api('/runs',{signal:controller.signal}),{name:'AbortError'});
});

test('leaving Generate during facets loading preserves the next screen',async()=>{
  const facets=deferred(), root=new Element('main');
  let signal;
  globalThis.fetch=async(url,options)=>{
    if(url.endsWith('/facets')) {signal=options.signal;return response(await facets.promise);}
    const data={'/api/datasets':{datasets:[{dataset_id:'d',name:'Insights',row_count:2}]},'/api/configs':{configs:[]},'/api/runs':{runs:[]},'/api/jobs':{jobs:[]}};
    assert.ok(data[url],url);return response(data[url]);
  };
  showGenerate(root,'#generate'); await tick();
  assert.match(root.textContent,/Loading/);
  leaveGenerate(); root.textContent='Next screen';
  facets.resolve({facets:{}}); await tick();
  assert.equal(signal.aborted,true);
  assert.equal(root.textContent,'Next screen');
});

test('leaving Configure aborts loading and does not render an error on the next screen',async()=>{
  const configs=deferred(), root=new Element('main');
  globalThis.fetch=async url=>response(url==='/api/configs'?await configs.promise:{});
  showConfigure(root,'#configure'); await tick();
  assert.match(root.textContent,/Loading configuration/);
  leaveConfigure(); root.textContent='Next screen'; configs.resolve({configs:[]});await tick();
  assert.equal(root.textContent,'Next screen');
});

test('selecting insights loads only detail and ignores an older detail response',async()=>{
  const first=deferred(), requests=[], root=new Element('main');
  globalThis.fetch=async url=>{
    requests.push(url);
    if(url==='/api/runs/r') return response({run_id:'r',status:'completed',counts:{skipped:2}});
    if(url.startsWith('/api/runs/r/results?')) return response({results:['a','b'].map(insight_id=>({insight_id,status:'skipped',insight:{},rule:'R1'}))});
    if(url.endsWith('/a')) return response(await first.promise);
    if(url.endsWith('/b')) return response({insight:{query_summary:'Latest insight'},status:'skipped',outcome:{events:[]}});
    throw new Error(url);
  };
  showGenerate(root,'#generate/run/r'); await tick();
  showGenerate(root,'#generate/run/r/a'); await tick();
  showGenerate(root,'#generate/run/r/b'); await tick();
  assert.match(root.querySelector('.gen-detail').textContent,/Latest insight/);
  assert.equal(root.querySelectorAll('tr[aria-selected]').find(row=>row.dataset.insight==='b').getAttribute('aria-selected'),'true');
  first.resolve({insight:{query_summary:'Old insight'},outcome:{events:[]}}); await tick();
  assert.doesNotMatch(root.querySelector('.gen-detail').textContent,/Old insight/);
  assert.equal(requests.filter(url=>url==='/api/runs/r').length,1);
  assert.equal(requests.filter(url=>url.includes('results?')).length,1);
  leaveGenerate();
});

test('leaving Compare during a delayed response cannot restart polling or change the route',async()=>{
  const pending=deferred(), nodes=new Map(), handlers={};
  const get=id=>{if(!nodes.has(id)) nodes.set(id,new Element('div'));return nodes.get(id);};
  globalThis.document.getElementById=get;
  globalThis.document.querySelectorAll=()=>[];
  globalThis.document.querySelector=()=>get('skip');
  globalThis.window.addEventListener=(type,fn)=>{handlers[type]=fn;};
  globalThis.history={replaceState(_,__,hash){location.hash=hash;}};
  globalThis.location.hash='#compare/slow';
  let detailSignal;
  globalThis.fetch=async(url,options)=>{
    if(url==='/api/experiments/slow') {detailSignal=options.signal;return response(await pending.promise);}
    const fixtures={'/api/auth/session':{},'/api/health':{database:'connected'},'/api/config':{},'/api/configs':{configs:[]},'/api/experiments':{experiments:[]}};
    assert.ok(fixtures[url],url);return response(fixtures[url]);
  };
  await import('./app.mjs'); await tick(); await tick();
  assert.match(get('content').textContent,/Loading saved evidence/);
  location.hash='#configure';handlers.hashchange();await tick();
  pending.resolve({experiment_id:'slow',name:'Old comparison',status:'running',runs:[],spec:{candidates:[]}});
  await tick();
  assert.equal(detailSignal.aborted,true);
  assert.equal(location.hash,'#configure');
  assert.doesNotMatch(get('content').textContent,/Old comparison/);
  assert.equal(get('notice').textContent,'');
  // No active poll timer remains to keep this test process alive.
});

test('Generate renders while estimates load and a changed selection invalidates the old estimate',async()=>{
  const oldPlan=deferred(), root=new Element('main');
  let plans=0;
  const plan=selected=>({selected,surveys:0,skipped:{R1:selected},model_calls:{min:0,max:0}});
  globalThis.fetch=async url=>{
    if(url==='/api/jobs/batch/estimate') return response(++plans===1?await oldPlan.promise:plan(1));
    if(url.includes('/count?')) return response({matched:25,selected:1});
    if(url.endsWith('/facets')) return response({facets:{crop:[],concern:[],resolution_type:[],quality_flag:[],created:{}}});
    const fixtures={'/api/datasets':{datasets:[{dataset_id:'d',name:'Insights',row_count:25}]},'/api/configs':{configs:[]},'/api/runs':{runs:[]},'/api/jobs':{jobs:[]}};
    assert.ok(fixtures[url],url);return response(fixtures[url]);
  };
  showGenerate(root,'#generate');await tick();
  assert.match(root.textContent,/Before you run/);
  const submit=root.querySelector('button');
  assert.equal(submit.disabled,true);
  const originalTimeout=globalThis.setTimeout;
  let recalculate;
  try {
    globalThis.setTimeout=fn=>{recalculate=fn;return 0;};
    const limit=root.querySelectorAll('input').find(input=>input.attrs.placeholder==='All matching');
    limit.listeners.input({target:{value:'1'}});
    assert.equal(submit.disabled,true);
    recalculate(); await tick();
  } finally { globalThis.setTimeout=originalTimeout; }
  assert.equal(submit.dataset.selected,1);
  assert.equal(submit.disabled,false);
  oldPlan.resolve(plan(25));await tick();
  assert.equal(submit.dataset.selected,1);
  assert.equal(submit.textContent,'Run on 1 insight');
  leaveGenerate();
});
