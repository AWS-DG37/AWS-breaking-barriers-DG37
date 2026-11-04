import test from 'node:test';
import assert from 'node:assert/strict';
import {insightJourney} from './journey.mjs';

class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.attrs={}; this.listeners={}; }
  setAttribute(key,value) { this.attrs[key]=value; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  addEventListener(type,fn) { this.listeners[type]=fn; }
  get textContent() { return this.text ?? this.children.map(n=>n?.textContent || '').join(' '); }
}
globalThis.Node=Element;
globalThis.document={createElement:tag=>new Element(tag),createTextNode:text=>Object.assign(new Element('#text'),{text})};
const buttons = node => node.children.flatMap(n=>n instanceof Element?[...(n.tag==='button'?[n]:[]),...buttons(n)]:[]);

test('comparison selects the precise revision event rather than the last event for every attempt',()=>{
  const events = [
    {stage:'generate',attempt:1,data:{}}, {stage:'generate',attempt:1,data:{}},
    {stage:'schema',attempt:1,data:{valid:false}},
    {stage:'generate',attempt:2,data:{}}, {stage:'generate',attempt:2,data:{}},
    {stage:'decision',attempt:2,data:{status:'accepted'}},
  ];
  let selected;
  const panel=insightJourney({events},{stage:'generate',event:0,onSelect:(...args)=>{selected=args;}});
  const steps=buttons(panel);
  assert.equal(steps[0].attrs['aria-pressed'],'true');
  assert.equal(steps[2].attrs['aria-pressed'],'false');
  steps[0].listeners.click();
  assert.deepEqual(selected,['generate',1]);
  steps[2].listeners.click();
  assert.deepEqual(selected,['generate',3]);
});

test('run detail opens final outcome and lets the reader inspect the skipped rule',()=>{
  const panel=insightJourney({status:'skipped',events:[
    {sequence:1,stage:'select',source:'rule',data:{eligible:false,rule:'R3',explanation:'Nothing to verify'}},
    {sequence:2,stage:'decision',source:'rule',data:{status:'skipped'}},
  ]});
  const steps=buttons(panel);
  assert.equal(steps.length,2);
  assert.equal(steps[1].attrs['aria-pressed'],'true');
  steps[0].listeners.click();
  assert.equal(steps[0].attrs['aria-pressed'],'true');
  assert.match(panel.textContent,/Nothing to verify/);
  const legacy=insightJourney({status:'accepted'});
  assert.equal(buttons(legacy).length,0);
  assert.match(legacy.textContent,/No execution path can be reconstructed/);
});
