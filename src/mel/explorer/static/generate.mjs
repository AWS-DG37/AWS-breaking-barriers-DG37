import {budgetInput, capPanel, costSummary, money} from './costs.mjs';
import {el, skeleton} from './dom.mjs';
import {requestScope} from './api.mjs';
import {mountSurveyPlayer} from './survey-player.mjs';
import {insightJourney} from './journey.mjs';
import {addToReviewList} from './review.mjs';
import {isReadOnly} from './auth.mjs';
import {summarizeCounts} from './domain.mjs';
import {atLevel} from './detail.mjs';
// Generate: pick insights, see what the rules will do, run the agent, inspect results.
// Generate and the viewing-only Runs section share job, run and result screens.
// Routes: #<generate|runs>[/job/<id>|/run/<id>[/<insight id>]].
const requests=requestScope(), estimates=requestScope(), details=requestScope();
const api=(path,options)=>requests.api(path,options);

export const RULES = {
  R1:'Junk or invalid insight', R2:'Critical: needs a human call', R3:'Nothing to verify',
  R4:'Waiting for more details', R5:'No check needed', R6:'No send window before the season ends',
  R7:'Crop already harvested', R8:'Gets a survey',
};
const STATUS = {accepted:'Accepted', skipped:'Skipped by rules', rejected:'Rejected by reviewer', failed:'Failed'};
const FILTERS = [['crop','Crop'],['concern','Concern'],['resolution_type','Resolution'],['quality_flag','Quality']];
const PAGE = 50;
// Results table columns; a key makes the heading a sort control. Unsorted is the
// order results were saved. A third entry is the detail level; none shows always.
const COLUMNS = [['insight','Insight'],['insight_date','Date'],['outcome','Outcome'],[null,'What happened',2],['relevance','Relevance'],['time','Time',3]];
const UNSORTED = {sort:'processed',direction:'asc'};
const RUN_SORTS = [['newest','Newest first'],['oldest','Oldest first'],['most_accepted','Most accepted surveys'],['most_results','Most results']];
const RUN_STATUSES = ['completed','completed_with_errors','running','failed'];
// Query string for the set values only; the server rejects unknown parameters.
const query = values => new URLSearchParams(Object.entries(values).filter(([,v])=>v!==null && v!==undefined && v!=='')).toString();
const choice = (label,name,value,options,change) => el('label',{},label,el('select',{name,onChange:event=>change(event.target.value)},
  options.map(([key,text])=>el('option',{value:key,selected:key===value},text))));
const facetCache=new Map();

const view = {
  root:null, section:'generate', request:0, poll:null, datasets:[], configs:[], activeConfig:null, facets:null,
  form:{dataset:null, config:'', selection:{limit:25}},
  run:{id:null, filter:null, query:{}, facets:null, rows:[], insight:null, hasMore:false},
  runs:{status:'', config_id:'', sort:'newest'},
};

const words = value => String(value ?? '').replaceAll('_',' ');
const when = iso => iso ? new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) : '';
const number = value => Number(value).toLocaleString();
const disclosure = (label,value,attrs={}) => el('details',attrs,el('summary',{},label),el('pre',{},JSON.stringify(value,null,2)));
const statusBadge = status => el('span',{class:`badge status-${status}`},STATUS[status] || words(status));
function message(text,error=false) {
  return el('p',{class:error?'notice error':'notice',role:error?'alert':'status'},text);
}

export function describeSelection(selection) {
  const s=selection || {};
  const parts=[s.crop,words(s.concern),words(s.resolution_type),s.quality_flag && `${s.quality_flag} only`,s.farmer_id].filter(Boolean);
  if (s.created_from || s.created_to) parts.push(`${s.created_from || 'start'} to ${s.created_to || 'end'}`);
  const scope=parts.length?parts.join(' · '):'All insights';
  return s.limit?`${scope} · first ${number(s.limit)}`:scope;
}

export function showGenerate(root, hash) {
  return isReadOnly()?showRuns(root,hash.replace(/^#generate/,'#runs')):showScreen(root,hash,'generate');
}
export function showRuns(root, hash) { return showScreen(root,hash,'runs'); }

function showScreen(root, hash, section) {
  view.root=root;
  view.section=section;
  const [, page, id, insight]=hash.replace(/^#/,'').split('/');
  if (page==='run' && id && !requests.signal.aborted && view.run.id===id && root.querySelector('.gen-run')) {
    showDetail(root.querySelector('.gen-detail'),id,insight?decodeURIComponent(insight):null);
    root.querySelectorAll('tr[aria-selected]').forEach(row=>row.setAttribute('aria-selected',String(row.dataset.insight===view.run.insight)));
    return;
  }
  leaveGenerate(); requests.renew();
  if (page==='run' && id) return renderRun(id, insight?decodeURIComponent(insight):null);
  if (page==='job' && id) return followJob(id);
  return section==='runs'?renderRuns():renderHome();
}

// Viewing saved runs needs no generation configuration, datasets or estimates.
async function renderRuns() {
  const ticket=++view.request;
  view.root.replaceChildren(skeleton('Loading runs…'));
  try {
    const [runs,jobs,configs]=await Promise.all([api(`/runs?${query(view.runs)}`),api('/jobs'),api('/configs')]);
    if (ticket!==view.request) return;
    const waiting=jobs.jobs.filter(job=>job.kind==='batch' && job.status==='queued');
    const set=(key,value)=>{ view.runs[key]=value; renderRuns(); };
    const filtered=view.runs.status || view.runs.config_id;
    const tools=el('div',{class:'gen-tools',role:'group','aria-label':'Filter and sort runs'},
      choice('Status','status',view.runs.status,[['','Any status'],...RUN_STATUSES.map(s=>[s,words(s)])],value=>set('status',value)),
      choice('Configuration','config_id',view.runs.config_id,[['','Any version'],...configs.configs.map(c=>[c.config_id,configLabel(c)])],value=>set('config_id',value)),
      choice('Sort','sort',view.runs.sort,RUN_SORTS,value=>set('sort',value)),
      filtered?el('button',{type:'button',class:'quiet',onClick:()=>{ Object.assign(view.runs,{status:'',config_id:''}); renderRuns(); }},'Clear filters'):null);
    view.root.replaceChildren(
      el('div',{class:'heading'},el('div',{},el('h1',{},'Runs'),
        el('p',{class:'muted'},'Browse saved runs, inspect outcomes and preview surveys.')),
        el('button',{type:'button',class:'secondary',onClick:()=>showRuns(view.root,'#runs')},'Refresh runs')),
      tools,
      filtered && !runs.runs.length?el('p',{class:'empty-note'},'No runs match these filters.'):recent(runs.runs,filtered?[]:waiting,'runs'));
  } catch(error) {
    if (ticket===view.request) view.root.replaceChildren(message(`Cannot load runs. ${error.message}`,true),
      el('button',{type:'button',class:'secondary',onClick:()=>showRuns(view.root,'#runs')},'Retry'));
  }
}

// Home: the new-run panel and recent runs.

async function renderHome() {
  requests.renew(); estimates.cancel(); details.cancel();
  const ticket=++view.request;
  view.root.replaceChildren(skeleton('Loading insights and runs…'));
  try {
    const [datasets,configs,runs,jobs]=await Promise.all([api('/datasets'),api('/configs'),api('/runs'),api('/jobs')]);
    if (ticket!==view.request) return;
    view.datasets=datasets.datasets;
    view.configs=configs.configs;
    view.activeConfig=configs.active_config_id;
    if (!view.datasets.some(d=>d.dataset_id===view.form.dataset)) view.form.dataset=view.datasets[0]?.dataset_id || null;
    const waiting=jobs.jobs.filter(j=>j.kind==='batch' && j.status==='queued');
    const [panel,plan]=await composer();
    if (ticket!==view.request) return;
    view.root.replaceChildren(el('div',{class:'gen-home'},panel,el('div',{class:'gen-side'},plan,recent(runs.runs,waiting))));
  } catch(error) {
    if (ticket===view.request) view.root.replaceChildren(message(`Cannot load the Generate page. ${error.message}`,true));
  }
}

async function composer() {
  const panel=el('section',{class:'gen-panel','aria-labelledby':'gen-title'},
    el('h1',{id:'gen-title'},'Generate surveys'),
    el('p',{class:'muted'},'Choose insights, check what the rules will do, then run the agent.'));
  if (!view.datasets.length) {
    const load=el('button',{class:'primary',onClick:async()=>{
      load.disabled=true; load.textContent='Loading 6,999 insights…';
      try { await api('/datasets/import-pack',{method:'POST'}); renderHome(); }
      catch(error) { load.disabled=false; load.textContent='Load participant pack'; panel.append(message(`Could not load the pack. ${error.message}`,true)); }
    }},'Load participant pack');
    panel.append(el('div',{class:'gen-empty'},el('h2',{},'Load insights to get started'),
      el('p',{},'The participant pack has 6,999 synthetic insights from farmers in Bihar.'),load));
    return [panel,null];
  }
  const datasetId=view.form.dataset;
  view.facets=facetCache.get(datasetId) || await api(`/datasets/${datasetId}/facets`);
  facetCache.set(datasetId,view.facets);
  const f=view.form, s=f.selection;
  const summary=el('div',{class:'gen-estimate',role:'status','aria-live':'polite'});
  const submit=el('button',{class:'primary wide',type:'button',onClick:()=>start(submit,summary)},'Run');
  const pageSignal=requests.signal;
  const changed=debounce(()=>{if (!pageSignal.aborted) estimate(summary,submit);},350);
  const set=(key,value)=>{ if (value===''||value===null) delete s[key]; else s[key]=value; estimates.cancel(); submit.disabled=true; delete submit.dataset.selected; changed(); };

  const datasetPicker=el('select',{'aria-label':'Insight dataset',onChange:event=>{f.dataset=event.target.value;f.selection={limit:s.limit};renderHome();}},
    view.datasets.map(d=>el('option',{value:d.dataset_id,selected:d.dataset_id===f.dataset},`${d.name} · ${number(d.row_count)} insights`)));
  const filters=FILTERS.map(([key,label])=>el('label',{},label,el('select',{onChange:event=>set(key,event.target.value)},
    el('option',{value:''},'Any'),
    view.facets.facets[key].map(o=>el('option',{value:o.value,selected:s[key]===o.value},`${words(o.value)} (${number(o.count)})`)))));
  const range=view.facets.facets.created;
  const dates=el('div',{class:'gen-row'},
    el('label',{},'From',el('input',{type:'date',min:range.from,max:range.to,value:s.created_from || '',onChange:event=>set('created_from',event.target.value)})),
    el('label',{},'To',el('input',{type:'date',min:range.from,max:range.to,value:s.created_to || '',onChange:event=>set('created_to',event.target.value)})));
  const limit=el('label',{},'How many',el('input',{type:'number',min:1,step:1,placeholder:'All matching',value:s.limit ?? '',onInput:event=>set('limit',event.target.value?Number(event.target.value):null)}));
  const configPicker=el('select',{'aria-label':'Agent configuration',onChange:event=>{f.config=event.target.value;estimates.cancel();submit.disabled=true;delete submit.dataset.selected;changed();}},
    el('option',{value:'',selected:!f.config},'Active version'),
    // Saved versions are the runnable ones; test drafts stay in Configure.
    view.configs.filter(c=>['saved','active'].includes(c.status)).map(c=>el('option',{value:c.config_id,selected:c.config_id===f.config},configLabel(c))));

  const budget=budgetInput();
  budget.querySelector('input').value=f.budget || '5';
  budget.querySelector('input').addEventListener('input',event=>{f.budget=event.target.value;});
  const form=el('form',{class:'gen-form',onSubmit:event=>event.preventDefault()},
    step('1','Insights',datasetPicker),
    step('2','Which ones',el('div',{class:'gen-filters'},...filters),dates,limit),
    step('3','Agent configuration',configPicker,el('p',{class:'small muted'},el('a',{href:'#configure'},'Edit configurations'))),
    step('4','Spending limit',budget,el('p',{class:'small muted'},'Stops before the next request would exceed the limit. Each request reserves its full context bound until usage returns.'),capPanel()));
  panel.append(form);
  const plan=el('section',{class:'gen-plan','aria-labelledby':'plan-title'},el('h2',{id:'plan-title'},'Before you run'),summary,submit);
  queueMicrotask(()=>{if (!requests.signal.aborted) estimate(summary,submit);});
  return [panel,plan];
}

const step=(n,title,...body)=>el('fieldset',{class:'gen-step'},el('legend',{},el('span',{class:'gen-step-number'},n),title),...body);

function configLabel(c) {
  const active=c.config_id===view.activeConfig?' (active)':'';
  return `${c.name}${active} · ${c.generation_model}`;
}

function batchBody() {
  return {dataset_id:view.form.dataset,config_id:view.form.config || null,selection:view.form.selection,budget_usd:view.form.budget || '5'};
}

async function estimate(summary,submit) {
  estimates.renew();
  const signal=estimates.signal;
  submit.disabled=true;
  delete submit.dataset.selected;
  summary.replaceChildren(skeleton('Checking the rules…'));
  try {
    const s=view.form.selection;
    const query=new URLSearchParams(Object.entries(s).filter(([,v])=>v!==undefined));
    const [count,plan]=await Promise.all([
      estimates.api(`/datasets/${view.form.dataset}/count?${query}`),
      estimates.api('/jobs/batch/estimate',{method:'POST',body:JSON.stringify(batchBody())}),
    ]);
    if (signal.aborted) return;
    submit.disabled=false;
    const skipped=Object.entries(plan.skipped);
    const skippedTotal=skipped.reduce((sum,[,n])=>sum+n,0);
    summary.replaceChildren(...[
      el('p',{},el('strong',{},number(count.matched)),` insights match. `,count.selected<count.matched?`The first ${number(count.selected)} will run.`:'All of them will run.'),
      el('div',{class:'gen-split'},
        el('div',{class:'metric'},el('span',{},'Get a survey'),el('strong',{},number(plan.surveys))),
        el('div',{class:'metric'},el('span',{},'Skipped by rules'),el('strong',{},number(skippedTotal)))),
      skipped.length?el('ul',{class:'gen-rules small'},skipped.map(([rule,n])=>el('li',{},`${number(n)} · ${rule} ${RULES[rule] || ''}`))):null,
      el('p',{class:'small muted'},plan.surveys?
        `About ${number(plan.model_calls.min)}–${number(plan.model_calls.max)} model calls and ${plan.minutes<1?'under a minute':`${plan.minutes} minutes`}. ${plan.cost_usd===null?'Cost unknown: verified prices are missing for one or more models.':`About $${plan.cost_usd}.`}`:
        'No model calls: the rules decide every selected insight.')].filter(Boolean));
    if(plan.costs) summary.append(el('p',{class:'small muted'},plan.costs.note),...plan.costs.models.map(m=>el('p',{class:'small'},`${m.role}: ${m.model_id} · reserves ${money(m.request_reservation_usd)} per request.`)));
    submit.textContent=`Run on ${number(plan.selected)} insight${plan.selected===1?'':'s'}`;
    submit.dataset.selected=plan.selected;
    submit.dataset.calls=plan.model_calls.max;
  } catch(error) {
    if (!signal.aborted) summary.replaceChildren(message(error.message,true));
  }
}

async function start(submit,summary) {
  const selected=Number(submit.dataset.selected || 0);
  if (!selected) { summary.append(message('No insights match this selection.',true)); return; }
  if (selected>200 && !confirm(`This runs ${number(selected)} insights, up to ${number(submit.dataset.calls)} model calls. Continue?`)) return;
  if(!view.root.querySelector('form').reportValidity()) return;
  submit.disabled=true; submit.textContent='Starting…';
  try {
    const job=await api('/jobs/batch',{method:'POST',body:JSON.stringify(batchBody())});
    if (!requests.signal.aborted) location.hash=`#generate/job/${job.job_id}`;
  } catch(error) {
    submit.disabled=false; submit.textContent='Run';
    summary.append(message(`The run did not start. ${error.message}`,true));
  }
}

function recent(runs,waiting,section='generate') {
  const titleId=`${section}-recent-title`;
  const list=el('section',{class:'gen-recent','aria-labelledby':titleId},el('h2',{id:titleId},'Recent runs'));
  if (!runs.length && !waiting.length) {
    list.append(el('p',{class:'muted'},'Runs appear here. Each one keeps its insights, rulings, questions and every agent step.'));
    return list;
  }
  for (const job of waiting) {
    list.append(el('a',{class:'gen-run-link',href:`#${section}/job/${job.job_id}`},
      el('strong',{},describeSelection(job.spec.selection)),el('span',{class:'small muted'},`Waiting to start · ${when(job.created_at)}`)));
  }
  for (const run of runs) {
    const counts=Object.entries(run.counts).map(([k,n])=>`${number(n)} ${k}`).join(' · ') || 'No results yet';
    list.append(el('a',{class:'gen-run-link',href:`#${section}/run/${run.run_id}`},
      el('span',{class:'gen-run-title'},el('strong',{},describeSelection(run.selection?.selection || {limit:run.selection?.limit})),el('span',{class:`badge status-${run.status}`},words(run.status))),
      el('span',{class:'small muted'},`${counts} · ${when(run.created_at)}`)));
  }
  return list;
}

// A queued job becomes a run once a runner claims it.

async function followJob(jobId, refresh=false) {
  const ticket=++view.request;
  if (!refresh) view.root.replaceChildren(skeleton('Loading run status…'));
  try {
    const job=await api(`/jobs/${jobId}`);
    if (ticket!==view.request) return;
    if (job.runs.length) { location.replace(`#${view.section}/run/${job.runs[0].run_id}`); return; }
    const waiting=job.status==='failed'?
      message(`The run failed before it started. ${job.error || ''}`,true):
      el('div',{class:'gen-waiting'},el('div',{class:'spinner','aria-hidden':true}),
        el('div',{},el('h2',{},'Starting the run'),el('p',{class:'muted'},'Hosted runs start a worker task, which takes about a minute. Locally, a `mel-worker jobs` runner picks the job up.')));
    view.root.replaceChildren(el('a',{href:`#${view.section}`,class:'back'},'← All runs'),waiting);
    if (job.status!=='failed') view.poll=setTimeout(()=>followJob(jobId,true),2000);
  } catch(error) {
    if (ticket===view.request) view.root.replaceChildren(message(`Cannot read the job. ${error.message}`,true));
  }
}

// Run page: progress, outcome counts, results, and the selected insight's detail.

async function renderRun(runId,insightId) {
  const ticket=++view.request;
  clearTimeout(view.poll);
  const fresh=view.run.id!==runId;
  if (fresh || !view.root.querySelector('.gen-run')) view.root.replaceChildren(skeleton('Loading run and results…'));
  if (fresh) Object.assign(view.run,{id:runId,filter:null,query:{...UNSORTED},facets:null,rows:[],insight:null});
  const previousInsight=view.run.insight;
  view.run.insight=insightId;
  try {
    const [summary,page,facets]=await Promise.all([
      api(`/runs/${runId}`),
      api(`/runs/${runId}/results?${resultQuery({limit:Math.max(PAGE,view.run.rows.length)})}`),
      view.run.facets || api(`/runs/${runId}/facets`),
    ]);
    view.run.facets=facets;
    if (ticket!==view.request) return;
    view.run.hasMore=page.results.length>=Math.max(PAGE,view.run.rows.length);
    view.run.rows=page.results;
    const shell=view.root.querySelector('.gen-run')?.dataset.run===runId?view.root.querySelector('.gen-run'):null;
    const layout=shell || el('div',{class:'gen-run','data-run':runId},
      el('a',{href:`#${view.section}`,class:'back'},'← All runs'),el('div',{class:'gen-run-head'}),
      el('div',{class:'gen-run-body'},el('div',{class:'gen-results-col'},resultTools(facets),el('div',{class:'gen-results'})),el('aside',{class:'gen-detail'})));
    if (!shell) view.root.replaceChildren(layout);
    layout.querySelector('.gen-run-head').replaceChildren(...runHeader(summary));
    layout.querySelector('.gen-results').replaceChildren(resultsTable(summary,page.results));
    if (previousInsight!==view.run.insight || !shell) showDetail(layout.querySelector('.gen-detail'),runId,view.run.insight);
    if (['running','queued'].includes(summary.status)) view.poll=setTimeout(()=>renderRun(runId,view.run.insight),4000);
  } catch(error) {
    if (ticket===view.request) view.root.replaceChildren(el('a',{href:`#${view.section}`,class:'back'},'← All runs'),message(`Cannot load the run. ${error.message}`,true));
  }
}

function runHeader(summary) {
  const progress=summary.progress || {};
  const counts=summary.counts || {};
  const total=progress.total || Object.values(counts).reduce((a,b)=>a+b,0);
  const done=progress.done ?? total;
  const head=[
    el('div',{class:'heading'},el('div',{},
      el('h1',{},describeSelection(summary.selection?.selection || {limit:summary.selection?.limit})),
      el('p',{class:'gen-summary'},summarizeCounts(counts)),
      el('p',{class:'muted small','data-level':2},`Started ${when(summary.created_at)} · configuration ${summary.config_id?.slice(0,8) || 'not recorded'} · ${number(summary.input_tokens+summary.output_tokens)} tokens`)),
      el('div',{class:'gen-run-actions'},el('span',{class:`badge status-${summary.status}`},words(summary.status)),
        isReadOnly() || ['running','queued'].includes(summary.status)?null:el('button',{type:'button',class:'quiet danger','data-level':3,onClick:()=>deleteRun(summary)},'Delete run'))),
  ];
  head.push(costSummary(summary.costs,summary.run_id));
  if (summary.status==='running' && total) {
    head.push(el('div',{class:'progress',role:'progressbar','aria-valuemin':0,'aria-valuemax':total,'aria-valuenow':done,'aria-label':'Insights processed'},
      el('span',{style:`width:${Math.round(100*done/total)}%`})),el('p',{class:'small muted'},`${number(done)} of ${number(total)} insights processed`));
  }
  // An active filter narrows the rows, so its controls stay visible to clear it.
  head.push(el('div',{class:'gen-metrics','data-level':view.run.filter?null:2},Object.keys(STATUS).map(key=>el('button',{type:'button',class:'metric','aria-pressed':view.run.filter===key,
    onClick:()=>{view.run.filter=view.run.filter===key?null:key;view.run.rows=[];renderRun(view.run.id,view.run.insight);}},
    el('span',{},STATUS[key]),el('strong',{},number(counts[key] || 0))))));
  return head;
}

// Removes the run, its results, request records and pending scheduler handoffs.
// Its spend still counts against the console cap; review comments keep their copy.
async function deleteRun(summary) {
  const counts=summary.counts || {};
  const results=Object.values(counts).reduce((a,b)=>a+b,0);
  const accepted=counts.accepted || 0;
  if (!confirm(`Delete this run? This removes ${number(results)} result${results===1?'':'s'}${accepted?`, including ${number(accepted)} accepted survey${accepted===1?'':'s'} waiting in the scheduler outbox`:''}, and its request cost records. Its spend still counts toward the console cap, and review comments made from it keep their copy. This cannot be undone.`)) return;
  try {
    await api(`/runs/${summary.run_id}`,{method:'DELETE'});
    view.run.id=null;
    location.hash=`#${view.section}`;
  } catch(error) {
    if (error.name!=='AbortError') view.root.querySelector('.gen-run-head')?.prepend(message(`Cannot delete this run. ${error.message}`,true));
  }
}

function outcomeText(row) {
  if (row.status==='accepted') return `“${row.first_question || 'Survey'}” · ${row.questions} question${row.questions===1?'':'s'}`;
  if (row.status==='skipped') return `${row.rule || ''} ${RULES[row.rule] || words(row.reason)}${row.detail?` · ${row.detail}`:''}`;
  if (row.status==='rejected') return `Reviewer: ${(row.reason || 'did not accept the draft').slice(0,160)}`;
  return `Failed: ${row.reason || 'unknown error'}`;
}

function resultQuery(page) {
  return query({...view.run.query,status:view.run.filter,...page});
}

// Search and facet filters for one run's results. Built once per run so typing
// keeps focus while the table below refreshes; the outcome tiles filter by status.
function resultTools(facets) {
  const refresh=()=>{ view.run.rows=[]; renderRun(view.run.id,view.run.insight); };
  const set=(key,value)=>{ view.run.query[key]=value || null; refresh(); };
  const facet=(label,key,counts,describe=words)=>Object.keys(counts).length>1?
    choice(label,key,view.run.query[key] || '',[['',`Any ${label.toLowerCase()}`],...Object.entries(counts).map(([value,n])=>[value,`${describe(value)} (${number(n)})`])],value=>set(key,value)):null;
  const search=debounce(value=>set('search',value.trim()),300);
  return el('div',{class:'gen-tools',role:'group','aria-label':'Filter and sort results'},
    el('label',{class:'gen-search'},'Search',el('input',{type:'search',name:'search',maxlength:200,value:view.run.query.search || '',
      placeholder:'Insight ID, farmer question, advice or survey question',onInput:event=>search(event.target.value)})),
    facet('Crop','crop',facets.crops),
    facet('Concern','concern',facets.concerns),
    facet('Rule','rule',facets.rules,rule=>`${rule} · ${RULES[rule] || rule}`));
}

// A heading cycles its column ascending, descending, then back to unsorted.
function sortHeading([key,label,level]) {
  if (!key) return el('th',{scope:'col','data-level':level},label);
  const q=view.run.query, active=q.sort===key;
  const next=!active?{sort:key,direction:'asc'}:q.direction==='asc'?{sort:key,direction:'desc'}:UNSORTED;
  const state=active?(q.direction==='asc'?'ascending':'descending'):'none';
  return el('th',{scope:'col','aria-sort':state,'data-level':level},el('button',{type:'button',class:'gen-sort',
    title:next===UNSORTED?'Back to processing order':`Sort by ${label.toLowerCase()}, ${next.direction==='asc'?'ascending':'descending'}`,
    onClick:()=>{ Object.assign(q,next); view.run.rows=[]; renderRun(view.run.id,view.run.insight); }},
    label,el('span',{class:'gen-sort-mark','aria-hidden':'true'},active?(q.direction==='asc'?'▲':'▼'):'↕')));
}

function resultsTable(summary,rows) {
  if (!rows.length) return el('p',{class:'empty-note'},['running','queued'].includes(summary.status)?'Results appear as each insight finishes.':'No results match this filter.');
  const table=el('table',{class:'gen-table'},
    el('thead',{},el('tr',{},COLUMNS.map(sortHeading))),
    el('tbody',{},rows.map(row=>el('tr',{'aria-selected':String(row.insight_id===view.run.insight),'data-insight':row.insight_id,tabindex:0,
      onClick:()=>{location.hash=`#${view.section}/run/${view.run.id}/${encodeURIComponent(row.insight_id)}`;},
      onKeydown:event=>{if(event.key==='Enter') location.hash=`#${view.section}/run/${view.run.id}/${encodeURIComponent(row.insight_id)}`;}},
      el('td',{},el('strong',{},row.insight_id),el('span',{class:'small muted'},` ${row.insight.crop || ''}`)),
      el('td',{class:'small muted'},row.insight.created_at?new Date(row.insight.created_at).toLocaleDateString(undefined,{dateStyle:'medium'}):''),
      el('td',{},statusBadge(row.status)),
      el('td',{class:'gen-what','data-level':2},outcomeText(row)),
      el('td',{class:'small'},row.relevance ?? ''),
      el('td',{class:'small muted','data-level':3},row.elapsed_seconds?`${row.elapsed_seconds.toFixed(1)} s`:'')))));
  const more=view.run.hasMore?el('button',{type:'button',class:'secondary',onClick:async()=>{
    more.disabled=true;
    const ticket=view.request, runId=view.run.id, filter=view.run.filter;
    try {
      const asked=JSON.stringify(view.run.query);
      const next=await api(`/runs/${runId}/results?${resultQuery({offset:view.run.rows.length,limit:PAGE})}`);
      if (ticket!==view.request || runId!==view.run.id || filter!==view.run.filter || asked!==JSON.stringify(view.run.query)) return;
      view.run.hasMore=next.results.length===PAGE;
      view.run.rows=[...view.run.rows,...next.results];
      more.parentElement.replaceWith(resultsTable(summary,view.run.rows));
    } catch(error) {
      if (ticket===view.request) { more.disabled=false; more.textContent='Retry loading more'; }
    }
  }},'Show more'):null;
  return el('div',{},el('div',{class:'table-scroll'},table),more);
}

// The stored English version of a saved survey, or a new one (a paid model call that
// view-only sessions and the offline demo cannot make). Null when neither is possible.
// Both requests carry the detail screen's signal: leaving the insight cancels them, and
// only a confirmed "none stored yet" (null) leads to the paid request.
async function englishVersion(runId,insightId,signal) {
  const path=`/runs/${runId}/results/${encodeURIComponent(insightId)}/english`;
  try {
    const stored=await details.api(path,{signal});
    if (signal.aborted) return null;
    if (stored?.items?.length) return stored;
    if (stored!==null) return null;  // not a "none stored yet" answer: never pay on a guess
    const made=await details.api(path,{method:'POST',signal});
    return made?.items?.length ? made : null;
  } catch { return null; }
}

async function showDetail(panel,runId,insightId) {
  details.renew();
  const signal=details.signal;
  view.run.insight=insightId;
  if (!insightId) { panel.replaceChildren(el('p',{class:'muted'},'Select an insight to see the agent’s steps and preview its survey.')); return; }
  panel.replaceChildren(skeleton('Loading insight…'));
  try {
    const record=await details.api(`/runs/${runId}/results/${encodeURIComponent(insightId)}`);
    if (signal.aborted || view.run.id!==runId || view.run.insight!==insightId) return;
    const {insight,outcome}=record;
    const survey=outcome.survey || outcome.candidate_survey;
    // The survey comes first; the insight and agent steps explain it below.
    const sections=[
      el('div',{class:'gen-detail-head'},el('h2',{},insightId),statusBadge(record.status)),
      el('p',{class:'small muted'},[insight.asset?.name,words(insight.concern),insight.conversation_language,when(insight.created_at)].filter(Boolean).join(' · ')),
    ];
    if (survey?.items?.length) {
      const host=el('div',{class:'survey-player'}), session={phase:'questions',question:0,answers:{}};
      const mount=translation=>mountSurveyPlayer(host,{survey,session,translation,photoUrl:'/static/assets/mustard-field.webp'});
      mount(null);
      // Saved runs keep only the farmer's language. The English version comes from the
      // console (made once per survey); when it arrives the phone gains its language switch
      // and keeps the reviewer's place and answers.
      if (!String(survey.language || '').startsWith('en')) englishVersion(runId,insightId,signal).then(english=>{
        if (english && !signal.aborted && host.isConnected) mount(english);
      });
      sections.push(el('h3',{},outcome.survey?'Survey preview':'Rejected draft'),el('figure',{class:'phone-stage'},host,el('figcaption',{},'Preview only · answers are not sent')),addToReviewList(runId,insightId,survey));
    }
    sections.push(
      el('h3',{},'Farmer asked'),el('p',{},insight.query_summary || 'Not recorded'),
      el('h3',{},'Advice given'),el('p',{},insight.advice_summary || 'Not recorded'),
      atLevel(insightJourney(outcome),2));
    sections.push(disclosure('Full insight',insight,{'data-level':3}),disclosure('Full outcome',outcome,{'data-level':3}));
    panel.replaceChildren(...sections.filter(Boolean));
    panel.scrollTop=0;  // A newly selected insight opens at its survey preview.
  } catch(error) {
    if (signal.aborted) return;
    panel.replaceChildren(message(`Cannot load this result. ${error.message}`,true));
  }
}

function debounce(fn,ms) {
  let timer;
  return (...args)=>{clearTimeout(timer);timer=setTimeout(()=>fn(...args),ms);};
}

export function leaveGenerate() {
  clearTimeout(view.poll);
  view.request++;
  requests.cancel(); estimates.cancel(); details.cancel();
}
