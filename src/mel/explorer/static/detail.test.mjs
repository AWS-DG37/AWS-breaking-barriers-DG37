import test from 'node:test';
import assert from 'node:assert/strict';

// A small DOM fixture, as in navigation.test.mjs. Only what detail.mjs and el() touch.
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.attrs={}; this.dataset={}; this.listeners={}; }
  setAttribute(key,value) { this.attrs[key]=String(value); if(key.startsWith('data-')) this.dataset[key.slice(5)]=String(value); }
  getAttribute(key) { return this.attrs[key]; }
  append(...nodes) { for(const n of nodes) { n.parentElement=this; this.children.push(n); } }
  replaceChildren(...nodes) { this.children=[]; this.append(...nodes); }
  addEventListener(type,fn) { this.listeners[type]=fn; }
  get textContent() { return this.text ?? this.children.map(c=>c.textContent).join(''); }
  set textContent(text) { this.text=text; this.children=[]; }
  querySelectorAll(tag) { return this.children.flatMap(c=>[...(c.tagName===tag?[c]:[]),...c.querySelectorAll(tag)]); }
}
globalThis.Node=Element;
globalThis.document={documentElement:new Element('html'),createElement:tag=>new Element(tag),createTextNode:text=>{const node=new Element('#text');node.textContent=text;return node;}};

const store=new Map();
const memory={getItem:key=>store.has(key)?store.get(key):null,setItem:(key,value)=>store.set(key,String(value))};
const broken={getItem(){throw new Error('denied');},setItem(){throw new Error('denied');}};
const useStorage=value=>Object.defineProperty(globalThis,'localStorage',{value,configurable:true,writable:true});
useStorage(memory);

const {LEVELS,level,setLevel,apply,mountDial}=await import('./detail.mjs');
const detail=()=>document.documentElement.dataset.detail;

test('the levels are named in order',()=>{
  assert.deepEqual(LEVELS,['Essential','Detailed','Everything']);
});

test('a missing, invalid or out-of-range stored level reads as Essential',()=>{
  useStorage(memory); store.clear();
  assert.equal(level(),1);
  for (const [stored,expected] of [['3',3],['2',2],['7',1],['0',1],['x',1],['',1],['2.5',1]]) {
    store.set('mel.detail',stored);
    assert.equal(level(),expected,`stored ${JSON.stringify(stored)}`);
  }
});

test('storage failures never reach the caller',()=>{
  useStorage(broken);
  assert.equal(level(),1);
  assert.doesNotThrow(()=>setLevel(2));
  Object.defineProperty(globalThis,'localStorage',{get(){throw new Error('blocked');},configurable:true});
  assert.equal(level(),1);
  assert.doesNotThrow(()=>setLevel(3));
  useStorage(memory);
});

test('setLevel clamps to the three positions and stores the result',()=>{
  useStorage(memory); store.clear();
  setLevel(5);
  assert.equal(store.get('mel.detail'),'3');
  setLevel(-1);
  assert.equal(store.get('mel.detail'),'1');
});

test('a section floor raises the effective level without changing the preference',()=>{
  useStorage(memory); store.set('mel.detail','1');
  apply('configure');
  assert.equal(detail(),'3');
  assert.equal(store.get('mel.detail'),'1');
  apply('compare');
  assert.equal(detail(),'2');
  store.set('mel.detail','2');
  apply('runs');
  assert.equal(detail(),'2');
});

test('the dial is one labelled group whose pressed button is the effective level',()=>{
  useStorage(memory); store.set('mel.detail','1');
  const host=new Element('div');
  mountDial(host);
  const [group]=host.children;
  assert.equal(group.getAttribute('role'),'group');
  assert.equal(group.getAttribute('aria-label'),'Detail level');
  const buttons=group.querySelectorAll('button');
  assert.deepEqual(buttons.map(b=>b.textContent),LEVELS);
  assert.ok(buttons.every(b=>b.getAttribute('type')==='button'));
  const pressed=()=>buttons.map(b=>b.getAttribute('aria-pressed'));
  apply('runs');
  assert.deepEqual(pressed(),['true','false','false']);
  apply('configure');
  assert.deepEqual(pressed(),['false','false','true']);
  apply('review');
  buttons[1].listeners.click();
  assert.equal(store.get('mel.detail'),'2');
  assert.equal(detail(),'2');
  assert.deepEqual(pressed(),['false','true','false']);
});
