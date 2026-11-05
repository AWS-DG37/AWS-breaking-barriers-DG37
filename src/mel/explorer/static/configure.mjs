import {budgetInput} from './costs.mjs';
import {el, skeleton} from './dom.mjs';
import {requestScope} from './api.mjs';
import {isReadOnly} from './auth.mjs';
import {changedPaths, lineDiff, read, write} from './domain.mjs';
// Configure: view versions, edit a draft step by step, test it against its parent, save
// it as a named (runnable) version, and make it active.
// Routes: #configure, #configure/<config id>, #configure/<config id>/edit.
const requests=requestScope();
const api=(path,options)=>requests.api(path,options);
export function leaveConfigure() { ++view.request; requests.cancel(); }

const SEVERITIES = ['low','medium','high','critical'];
const EFFORTS = ['low','medium','high'];
const WINDOW_DAYS = ['next_day','two_to_five_days','three_to_ten_days','seven_to_fourteen_days','seasonal'];

// Each pipeline step, the definition paths it owns, and how to edit them.
const STEPS = [
  {id:'decide', title:'Decide', blurb:`Rules R1–R8 choose survey or not, then the send window.`, paths:['config.decision','rules']},
  {id:'generate', title:'Generate', blurb:'The model drafts tap-answer questions from the insight.', paths:['config.generation','prompts.generate']},
  {id:'review', title:'Review', blurb:'A second model checks the draft; accepted drafts become surveys.', paths:['config.review','prompts.review','config.graph.relevance_threshold']},
  {id:'loop', title:'Loop and limits', blurb:'Revisions, timeouts and concurrency.', paths:['config.graph.max_revisions','config.graph.insight_timeout_seconds','config.graph.agent_output_retries','config.graph.max_requests_per_call','config.batch.concurrency']},
];

const view = {root:null, request:0, versions:[], active:null, models:[], settings:null, draft:null, dirty:false};

const words = value => String(value ?? '').replaceAll('_',' ');
const when = iso => iso ? new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) : '';
const message = (text,error=false) => el('p',{class:error?'notice error':'notice',role:error?'alert':'status'},text);
const versionName = v => v ? (v.name || `Draft ${v.config_id.slice(0,8)}`) : 'Unknown version';
const MAX_NAME = 80;
const statusBadge = v => el('span',{class:`badge status-${v.status}`},v.config_id===view.active?'active':v.status);

window.addEventListener('beforeunload',event=>{ if (view.dirty) { event.preventDefault(); event.returnValue=''; } });

export function showConfigure(root, hash) {
  view.root=root;
  const [, id, mode]=hash.replace(/^#/,'').split('/');
  if (view.dirty && !(mode==='edit' && id===view.draft?.parentId) && !confirm('Discard the unsaved draft?')) {
    history.replaceState(null,'',`#configure/${view.draft.parentId}/edit`);
    return;
  }
  if (mode!=='edit') { view.dirty=false; view.draft=null; }
  return render(id || null, mode==='edit' && !isReadOnly());
}

async function render(id, editing) {
  requests.renew();
  const ticket=++view.request;
  view.root.replaceChildren(skeleton('Loading configuration…'));
  try {
    const [list,settings,catalog]=await Promise.all([
      api('/configs'), view.settings || api('/config'), !editing || view.models.length?{models:view.models}:api('/models'),
    ]);
    if (ticket!==view.request) return;
    view.versions=list.configs; view.active=list.active_config_id || settings.active_config_id;
    view.settings=settings; view.models=catalog.models || [];
    const key=id || view.active || view.versions[0]?.config_id;
    const record=key?await api(`/configs/${key}`):null;
    if (ticket!==view.request) return;
    const main=el('div',{class:'cfg-main'});
    view.root.replaceChildren(el('div',{class:'cfg'},sidebar(key),main));
    if (!record) { main.append(el('p',{class:'empty-note'},isReadOnly()?'No saved configuration versions yet.':'No configuration versions yet. Open Generate once to seed the baseline.')); return; }
    if (editing) editor(main,record); else details(main,record);
  } catch(error) {
    if (ticket===view.request) view.root.replaceChildren(message(`Cannot load configurations. ${error.message}`,true));
  }
}

function sidebar(selected) {
  const saved=view.versions.filter(v=>['saved','active'].includes(v.status));
  const drafts=view.versions.filter(v=>v.status==='draft');
  const retired=view.versions.filter(v=>v.status==='retired');
  const item=v=>el('a',{class:'cfg-item',href:`#configure/${v.config_id}`,'aria-current':String(v.config_id===selected)},
    el('strong',{},versionName(v)),el('span',{class:'small muted'},`${v.config_id===view.active?'Active':words(v.status)} · ${when(v.created_at)}`));
  const group=(title,items,open=true)=>items.length?el('details',{open},el('summary',{},`${title} (${items.length})`),items.map(item)):null;
  return el('nav',{class:'cfg-list','aria-label':'Configuration versions'},el('h2',{},'Versions'),
    saved.map(item),group('Test drafts',drafts,drafts.some(v=>v.config_id===selected)),group('Retired',retired,retired.some(v=>v.config_id===selected)));
}

// Read-only view of one version.

function details(main,record) {
  const parent=view.versions.find(v=>v.config_id===record.parent_id);
  const isActive=record.config_id===view.active;
  const actions=el('div',{class:'cfg-actions'},
    el('a',{class:'button primary',href:`#configure/${record.config_id}/edit`},'Edit as draft'),
    record.status==='draft'?el('button',{type:'button',onClick:()=>saveDraft(record)},'Save as version'):null,
    record.status!=='draft' && !isActive?el('button',{type:'button',onClick:()=>activate(record)},'Make active'):null);
  main.append(...[
    el('div',{class:'heading'},el('div',{},
      el('h1',{},versionName(record)),
      el('p',{class:'muted small'},[record.version,`created ${when(record.created_at)}`,parent?`from ${versionName(parent)}`:null].filter(Boolean).join(' · ')),
      record.note?el('p',{},record.note):null),statusBadge(record)),
    isReadOnly()?null:actions,
    parent?changesFrom(parent,record):null,
    rulesCheck(record.config_id),
    tests(record.config_id),
    ...STEPS.map(step=>el('section',{class:'cfg-step'},el('div',{class:'cfg-step-head'},el('h2',{},step.title),el('span',{class:'small muted'},step.blurb)),summaryFor(step.id,record.definition)))].filter(Boolean));
}

// What this version changed relative to its parent, step by step.
function changesFrom(parent,record) {
  const box=el('section',{class:'cfg-check'},el('p',{class:'small muted'},`Comparing with ${versionName(parent)}…`));
  api(`/configs/${parent.config_id}`).then(before=>{
    const rows=STEPS.flatMap(step=>changedPaths(before.definition,record.definition,leafPaths(before.definition,record.definition,step.paths))
      .map(path=>el('li',{},el('strong',{},`${step.title}: `),describeChange(path,before.definition,record.definition))));
    box.replaceChildren(el('h2',{},`Changes from ${versionName(parent)}`),
      rows.length?el('ul',{class:'cfg-changes'},rows):el('p',{class:'muted'},'Same steps as its parent.'));
  }).catch(error=>box.replaceChildren(message(`Cannot compare with the parent. ${error.message}`,true)));
  return box;
}

function describeChange(path,before,after) {
  const label=path.replace(/^config\./,'').replaceAll('.',' › ').replaceAll('_',' ');
  const a=read(before,path), b=read(after,path);
  if (path.startsWith('prompts.')) {
    const lines=lineDiff(a,b).filter(l=>l.op!==' ');
    const count=(op,verb)=>{ const n=lines.filter(l=>l.op===op).length; return `${n} line${n===1?'':'s'} ${verb}`; };
    return `${label}: ${count('-','removed')}, ${count('+','added')}`;
  }
  const show=v=>v===null || v===undefined?'unset':Array.isArray(v)?v.join(', '):String(v);
  return `${label}: ${show(a)} → ${show(b)}`;
}

function summaryFor(step,def) {
  const c=def.config;
  const model=m=>`${m.model_id} · ${m.api} · reasoning ${m.reasoning_effort}${m.temperature!==null?` · temperature ${m.temperature}`:''} · ${m.max_tokens} tokens`;
  if (step==='decide') return el('div',{},
    el('p',{},`Human call for ${c.decision.human_call_severities.join(', ')} severity · ${c.decision.window_policy==='golden_v1'?'Timing inferred from golden examples':'Timing from participant pack’s written rules'}`),
    el('p',{class:'small muted'},'The supplied golden examples and written rules disagree on timing. This version uses the selected interpretation to calculate send windows.'),
    el('details',{},el('summary',{},'Rules file'),el('pre',{},JSON.stringify(def.rules,null,2))));
  if (step==='generate') return el('div',{},el('p',{},model(c.generation)),promptView('Generation prompt',def.prompts.generate));
  if (step==='review') return el('div',{},el('p',{},`${model(c.review)} · accept at relevance ${c.graph.relevance_threshold}`),promptView('Review prompt',def.prompts.review));
  return el('p',{},`${c.graph.max_revisions} revision${c.graph.max_revisions===1?'':'s'} · ${c.graph.insight_timeout_seconds} s per insight · ${c.graph.agent_output_retries} output retries · ${c.graph.max_requests_per_call} requests per call · ${c.batch.concurrency} at once`);
}

const promptView=(label,text)=>el('details',{},el('summary',{},`${label} · ${text.split('\n').length} lines`),el('pre',{class:'cfg-prompt'},text));

function rulesCheck(id) {
  const box=el('section',{class:'cfg-check'},el('p',{class:'small muted'},'Checking agreement with the golden examples…'));
  api(`/configs/${id}/rules-check`).then(report=>{
    const pct=value=>value===null?'n/a':`${Math.round(value*1000)/10}%`;
    box.replaceChildren(...[el('h2',{},'Agreement with golden examples'),
      el('div',{class:'gen-split'},
        el('div',{class:'metric'},el('span',{},'Survey decision'),el('strong',{},pct(report.decision_accuracy))),
        el('div',{class:'metric'},el('span',{},'Deciding rule'),el('strong',{},pct(report.rule_accuracy))),
        el('div',{class:'metric'},el('span',{},'Exact send window'),el('strong',{},pct(report.window.exact_match)))),
      el('p',{class:'small muted'},`${report.cases} golden cases, decided by this version's rules alone. No model calls. ${report.mismatches.length?`${report.mismatches.length} mismatches listed below.`:''}`),
      el('p',{class:'small muted'},'These scores measure agreement with the supplied examples. Following the pack’s written timing rules can reduce exact-window agreement because the sources conflict. The rules were developed using the golden cases, so this is a regression check, not an independent measure of performance on unseen cases.'),
      report.mismatches.length?el('details',{},el('summary',{},'Mismatches'),el('pre',{},JSON.stringify(report.mismatches,null,2))):null].filter(Boolean));
  }).catch(error=>box.replaceChildren(message(`Rules check unavailable. ${error.message}`,true)));
  return box;
}

function tests(id) {
  const box=el('section',{class:'cfg-tests',hidden:true});
  api('/experiments').then(({experiments})=>{
    const mine=experiments.filter(e=>(e.config_ids || []).includes(id));
    if (!mine.length) return;
    box.hidden=false;
    box.append(el('h2',{},'Tests and comparisons'),...mine.map(e=>el('a',{class:'gen-run-link',href:`#compare/${e.experiment_id}`},
      el('span',{class:'gen-run-title'},el('strong',{},e.name),el('span',{class:`badge status-${e.status}`},words(e.status))),
      el('span',{class:'small muted'},`${e.candidate_count} versions · ${e.insight_count} insights · ${when(e.created_at)}`))));
  }).catch(()=>{});
  return box;
}

async function activate(record) {
  if (!confirm(`Make "${versionName(record)}" the active version? Generate will use it by default.`)) return;
  try { await api(`/configs/${record.config_id}/activate`,{method:'POST'}); render(record.config_id,false); }
  catch(error) { if(error.name!=='AbortError') view.root.querySelector('.cfg-main')?.prepend(message(error.message,true)); }
}

// A tested draft becomes a saved, runnable version.
async function saveDraft(record) {
  const name=window.prompt('Name this version. Saved versions can be picked in Generate and made active.',record.name || '');
  if (!name?.trim()) return;
  try { await api(`/configs/${record.config_id}/save`,{method:'POST',body:JSON.stringify({name:name.trim()})}); render(record.config_id,false); }
  catch(error) { if(error.name!=='AbortError') view.root.querySelector('.cfg-main')?.prepend(message(error.message,true)); }
}

// Draft editor.

function editor(main,parent) {
  if (view.draft?.parentId!==parent.config_id) {
    view.draft={parentId:parent.config_id,definition:structuredClone(parent.definition)};
    view.dirty=false;
  }
  const draft=view.draft.definition, before=parent.definition;
  const markers={};
  const refresh=()=>{
    view.dirty=JSON.stringify(draft)!==JSON.stringify(before);
    for (const step of STEPS) {
      const n=changedPaths(before,draft,leafPaths(before,draft,step.paths)).length;
      markers[step.id].textContent=n?`${n} change${n===1?'':'s'}`:'unchanged';
      markers[step.id].className=n?'cfg-marker changed':'cfg-marker';
    }
    status.textContent=view.dirty?'Unsaved changes':'No changes yet';
    main.querySelector('[aria-label="Test name"]')?.setAttribute('placeholder',view.dirty?changeSummary(before,draft):'What this draft tries');
  };
  const status=el('span',{class:'small muted'});
  const set=(path,value)=>{ write(draft,path,value); refresh(); };
  const section=(step,...body)=>{
    markers[step.id]=el('span',{class:'cfg-marker'});
    return el('section',{class:'cfg-step'},el('div',{class:'cfg-step-head'},el('h2',{},step.title),el('span',{class:'small muted'},step.blurb),markers[step.id]),...body);
  };
  const [decide,generate,review,loop]=STEPS;
  main.append(
    el('div',{class:'heading'},el('div',{},el('h1',{},'Draft'),el('p',{class:'muted small'},`From ${versionName(parent)} · edits stay in this tab until you test or save`)),status),
    el('div',{class:'cfg-actions'},
      el('a',{class:'button',href:`#configure/${parent.config_id}`,onClick:event=>{ if (view.dirty && !confirm('Discard the unsaved draft?')) event.preventDefault(); else view.dirty=false; }},'Discard')),
    section(decide,decisionFields(draft,set)),
    section(generate,modelFields('config.generation',draft,set),promptField('prompts.generate','Generation prompt',draft,before,set)),
    section(review,modelFields('config.review',draft,set),
      numberField('Accept at reviewer relevance (0–1)','config.graph.relevance_threshold',draft,set,{min:0,max:1,step:0.05}),
      promptField('prompts.review','Review prompt',draft,before,set)),
    section(loop,el('div',{class:'cfg-grid'},
      numberField('Revisions after the first draft','config.graph.max_revisions',draft,set,{min:0,max:5,step:1}),
      numberField('Seconds per insight','config.graph.insight_timeout_seconds',draft,set,{min:10,step:10}),
      numberField('Output retries','config.graph.agent_output_retries',draft,set,{min:0,max:5,step:1}),
      numberField('Requests per model call','config.graph.max_requests_per_call',draft,set,{min:1,max:10,step:1}),
      numberField('Insights at once','config.batch.concurrency',draft,set,{min:1,max:20,step:1}))),
    finish(parent,draft));
  refresh();
}

// Every leaf path under the given roots, in either definition.
function leafPaths(a,b,roots) {
  const out=[];
  const walk=(path,x,y)=>{
    const object=v=>v && typeof v==='object' && !Array.isArray(v);
    if (object(x) || object(y)) for (const key of new Set([...Object.keys(x || {}),...Object.keys(y || {})])) walk(`${path}.${key}`,x?.[key],y?.[key]);
    else out.push(path);
  };
  for (const root of roots) walk(root,read(a,root),read(b,root));
  return out;
}

function numberField(label,path,draft,set,attrs={}) {
  return el('label',{},label,el('input',{type:'number',...attrs,value:read(draft,path) ?? '',
    onInput:event=>{ if (event.target.value!=='') set(path,Number(event.target.value)); }}));
}

function choiceField(label,path,choices,draft,set) {
  return el('label',{},label,el('select',{onChange:event=>set(path,event.target.value)},
    choices.map(([value,text])=>el('option',{value,selected:read(draft,path)===value},text))));
}

function decisionFields(draft,set) {
  const severities=new Set(read(draft,'config.decision.human_call_severities'));
  const golden=read(draft,'config.decision.golden_v1');
  const days=el('table',{class:'cfg-days'},el('thead',{},el('tr',{},el('th',{},'Verification timing'),el('th',{},'Opens on day'),el('th',{},'Closes on day'))),
    el('tbody',{},WINDOW_DAYS.map(key=>el('tr',{},el('td',{},words(key)),...[0,1].map(i=>el('td',{},el('input',{type:'number',min:0,max:90,step:1,value:golden.days[key][i],
      'aria-label':`${words(key)} ${i?'closes':'opens'}`,
      onInput:event=>{ if (event.target.value==='') return; const pair=[...read(draft,`config.decision.golden_v1.days.${key}`)]; pair[i]=Number(event.target.value); set(`config.decision.golden_v1.days.${key}`,pair); }})))))));
  const rules=el('textarea',{class:'cfg-code',rows:14,spellcheck:false,'aria-label':'Rules file JSON'},JSON.stringify(read(draft,'rules'),null,2));
  const rulesError=el('p',{class:'small cfg-error',role:'alert'});
  rules.addEventListener('input',()=>{ try { set('rules',JSON.parse(rules.value)); rulesError.textContent=''; } catch { rulesError.textContent='Not valid JSON yet; the last valid rules are kept.'; } });
  return el('div',{},
    el('fieldset',{class:'cfg-checks'},el('legend',{},'Severities that need a human call instead of a survey (R2)'),
      SEVERITIES.map(level=>el('label',{class:'cfg-check-item'},el('input',{type:'checkbox',checked:severities.has(level),onChange:event=>{
        event.target.checked?severities.add(level):severities.delete(level);
        set('config.decision.human_call_severities',SEVERITIES.filter(s=>severities.has(s)));
      }}),level))),
    choiceField('Send window timing source','config.decision.window_policy',[['golden_v1','Timing inferred from golden examples'],['pack','Timing from participant pack’s written rules']],draft,set),
    el('p',{class:'small muted'},'The supplied examples and written rules disagree on timing; precedence has not been confirmed. This choice changes how send windows are calculated for runs using this configuration. It does not select the input dataset or the scoring reference.'),
    el('p',{class:'small muted'},'Golden timing is our interpretation of the example surveys, not a separate supplied policy. It uses the settings below and does not enforce the action window or a minimum two-day width. The pack’s written rules wait until the action window ends, require at least a two-day window and use the timing values in the rules file.'),
    el('details',{},el('summary',{},'Settings for timing inferred from golden examples'),
      el('p',{class:'small muted'},'Applies only when the golden timing source is selected. Day offsets count from the advice date; times are in India Standard Time.'),
      el('div',{class:'cfg-grid'},
        el('label',{},'Opens at',el('input',{type:'time',value:golden.opens,onInput:event=>set('config.decision.golden_v1.opens',event.target.value)})),
        el('label',{},'Closes at',el('input',{type:'time',value:golden.closes,onInput:event=>set('config.decision.golden_v1.closes',event.target.value)})),
        numberField('Same-day delay (hours)','config.decision.golden_v1.same_day_delay_hours',draft,set,{min:0,max:24,step:1}),
        numberField('Same-day window (hours)','config.decision.golden_v1.same_day_length_hours',draft,set,{min:1,max:24,step:1})),
      days),
    el('details',{},el('summary',{},'Advanced: rules file (JSON)'),rules,rulesError));
}

// Model choices: the deployment's allowed models (all catalog models when unrestricted),
// named from the catalog where it knows them, grouped by provider.
function modelChoices(current) {
  const named=new Map(view.models.map(m=>[`${m.api}|${m.model_id}`,m]));
  const allowed=view.settings.allowed_models || [];
  const choices=(allowed.length?allowed:view.models).map(m=>named.get(`${m.api}|${m.model_id}`) || {...m,name:m.model_id,provider:providerOf(m.model_id)});
  if (!choices.some(m=>m.model_id===current.model_id && m.api===current.api)) choices.unshift({...current,name:current.model_id,provider:providerOf(current.model_id)});
  return choices;
}

const providerOf=id=>({anthropic:'Anthropic',amazon:'Amazon',openai:'OpenAI',mistral:'Mistral AI',deepseek:'DeepSeek',meta:'Meta'})[id.replace(/^(us|global|eu|apac)\./,'').split('.')[0]] || 'Other';

function modelFields(path,draft,set) {
  const current=read(draft,path);
  const choices=modelChoices(current);
  const providers=[...new Set(choices.map(m=>m.provider))];
  const temperature=el('input',{type:'number',min:0,max:1,step:0.1,placeholder:'Model default',value:current.temperature ?? '',
    onInput:event=>set(`${path}.temperature`,event.target.value===''?null:Number(event.target.value))});
  return el('div',{class:'cfg-grid'},
    el('label',{class:'cfg-wide'},'Model',el('select',{onChange:event=>{ const m=choices[Number(event.target.value)]; set(`${path}.model_id`,m.model_id); set(`${path}.api`,m.api); }},
      providers.map(provider=>el('optgroup',{label:provider},choices.map((m,i)=>m.provider===provider?el('option',{value:i,selected:m.model_id===current.model_id && m.api===current.api},`${m.name} · ${m.api==='responses'?'Mantle':'Converse'}`):null))))),
    choiceField('Reasoning effort (Mantle models)',`${path}.reasoning_effort`,EFFORTS.map(e=>[e,e]),draft,set),
    el('label',{},'Temperature',temperature),
    numberField('Max output tokens',`${path}.max_tokens`,draft,set,{min:256,step:256}));
}

function promptField(path,label,draft,before,set) {
  const diff=el('div',{class:'cfg-diff',hidden:true});
  const area=el('textarea',{class:'cfg-code',rows:18,spellcheck:false,'aria-label':label},read(draft,path));
  const toggle=el('button',{type:'button',class:'quiet',onClick:()=>{ diff.hidden=!diff.hidden; toggle.textContent=diff.hidden?'Show changes':'Hide changes'; showDiff(); }},'Show changes');
  const showDiff=()=>{
    if (diff.hidden) return;
    const lines=lineDiff(read(before,path),area.value);
    diff.replaceChildren(...(lines.some(l=>l.op!==' ')?collapse(lines):[el('p',{class:'small muted'},'No changes to this prompt.')]));
  };
  area.addEventListener('input',()=>{ set(path,area.value); showDiff(); });
  return el('div',{class:'cfg-prompt-field'},el('div',{class:'cfg-prompt-head'},el('strong',{},label),toggle),area,diff);
}

// Changed lines with two lines of context; long unchanged runs fold away.
function collapse(lines) {
  const keep=lines.map((l,i)=>l.op!==' ' || lines.slice(Math.max(0,i-2),i+3).some(n=>n.op!==' '));
  const out=[];
  lines.forEach((line,i)=>{
    if (keep[i]) out.push(el('div',{class:`diff-line diff-${line.op==='+'?'add':line.op==='-'?'del':'same'}`},`${line.op} ${line.text}`));
    else if (keep[i-1] || i===0) out.push(el('div',{class:'diff-line diff-fold'},'…'));
  });
  return out;
}

function finish(parent,draft) {
  const box=el('section',{class:'cfg-finish'});
  const feedback=el('div',{});
  const save=async body=>{
    const record=await api('/configs',{method:'POST',body:JSON.stringify({definition:draft,parent_id:parent.config_id,...body})});
    if (record.config_id===parent.config_id) throw new Error('This draft is identical to its parent. Change something first.');
    return record;
  };
  // Test: save as an unnamed draft, then compare it with its parent on the same insights.
  const testName=el('input',{maxlength:MAX_NAME,'aria-label':'Test name',placeholder:'What this draft tries'});
  const datasetPicker=el('select',{'aria-label':'Test insights'});
  const limit=el('input',{type:'number',min:1,max:50,step:1,value:10,'aria-label':'How many insights'});
  api('/datasets').then(({datasets})=>datasetPicker.replaceChildren(...(datasets.length?datasets.map(d=>el('option',{value:d.dataset_id},`${d.name} · ${d.row_count.toLocaleString()}`)):[el('option',{value:''},'Load the participant pack in Generate first')]))).catch(error=>{if (!requests.signal.aborted) feedback.append(message(error.message,true));});
  const budget=budgetInput('Budget per test candidate (USD)');
  const test=el('button',{type:'button',onClick:async()=>{
    feedback.replaceChildren();
    if (!view.settings.live_enabled) { feedback.append(message('Live runs are turned off on this server, so tests cannot run here. You can still save the version.',true)); return; }
    if(!budget.querySelector('input').reportValidity()) return;
    if (!datasetPicker.value) { feedback.append(message('Load a dataset in Generate first.',true)); return; }
    const label=(testName.value.trim() || changeSummary(parent.definition,draft)).slice(0,MAX_NAME);
    if (label===versionName(parent)) { feedback.append(message('Give the test a name that differs from its parent.',true)); return; }
    test.disabled=true; test.textContent='Starting the test…';
    try {
      const record=await save({name:label,note:`Tested against ${versionName(parent)}`});
      const result=await api('/experiments',{method:'POST',body:JSON.stringify({
        name:`${label} vs ${versionName(parent)}`,budget_usd:budget.querySelector('input').value,
        candidates:[{label:versionName(parent),config_id:parent.config_id},{label,config_id:record.config_id}],
        dataset_id:datasetPicker.value,selection:{limit:Number(limit.value) || 10},
      })});
      view.draft.parentId=record.config_id; view.dirty=false;
      location.hash=`#compare/${result.experiment_id}`;
    } catch(error) { feedback.append(message(`The test did not start. ${error.message}`,true)); test.disabled=false; test.textContent='Test against parent'; }
  }},'Test against parent');
  const name=el('input',{maxlength:80,placeholder:'Shorter outcome options','aria-label':'Version name'});
  const note=el('input',{maxlength:500,placeholder:'What changed and why','aria-label':'Version note'});
  const saveButton=el('button',{type:'button',class:'primary',onClick:async()=>{
    feedback.replaceChildren();
    if (!name.value.trim()) { feedback.append(message('Name the version to save it.',true)); name.focus(); return; }
    saveButton.disabled=true;
    try {
      const record=await save({name:name.value.trim(),note:note.value.trim() || null,save:true});
      view.dirty=false; view.draft=null;
      location.hash=`#configure/${record.config_id}`;
    } catch(error) { feedback.append(message(`Not saved. ${error.message}`,true)); saveButton.disabled=false; }
  }},'Save version');
  box.append(
    el('div',{class:'cfg-finish-part'},el('h2',{},'Test it'),
      el('p',{class:'small muted'},`Runs this draft and ${versionName(parent)} on the same insights, then opens the side-by-side comparison. This calls the models. The draft is kept under Test drafts; save it as a version if it wins.`),
      el('label',{},'Test name',testName),
      el('div',{class:'cfg-grid'},el('label',{class:'cfg-wide'},'Insights',datasetPicker),el('label',{},'How many (1–50)',limit)),budget,test),
    el('div',{class:'cfg-finish-part'},el('h2',{},'Save it'),
      el('p',{class:'small muted'},'A saved version can be picked in Generate and made active. Saving does not run anything.'),
      el('label',{},'Name',name),el('label',{},'Note',note),saveButton),
    feedback);
  return box;
}

// A short label for a draft from what it changed, e.g. "generation reasoning effort medium".
function changeSummary(before,after) {
  const changed=STEPS.flatMap(step=>changedPaths(before,after,leafPaths(before,after,step.paths)));
  // A model switch changes model_id and usually api; name it once, by the model.
  const paths=changed.filter(path=>!(path.endsWith('.api') && changed.includes(path.replace(/api$/,'model_id'))));
  if (!paths.length) return 'Unchanged draft';
  const short=path=>{
    if (path.startsWith('prompts.')) return `${path.split('.')[1]} prompt edited`;
    if (path.endsWith('.model_id')) return `${path.split('.')[1]} model ${read(after,path).replace(/^(us|global|eu|apac)\./,'').split('.').slice(1).join('.') || read(after,path)}`;
    const value=read(after,path);
    return `${path.replace(/^config\./,'').split('.').slice(-2).join(' ').replaceAll('_',' ')} ${Array.isArray(value)?value.join('/'):value}`;
  };
  const text=paths.slice(0,2).map(short).join(', ')+(paths.length>2?` and ${paths.length-2} more`:'');
  return text.slice(0,MAX_NAME);
}
