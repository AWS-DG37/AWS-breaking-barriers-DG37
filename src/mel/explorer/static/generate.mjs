import {budgetInput, capPanel, costSummary, money} from './costs.mjs';
// Generate: pick insights, see what the rules will do, run the agent, inspect results.
// Routes: #generate, #generate/job/<job id>, #generate/run/<run id>[/<insight id>].
import {el} from './dom.mjs';
import {api} from './api.mjs';
import {mountSurveyPlayer} from './survey-player.mjs';

export const RULES = {
  R1:'Junk or invalid insight', R2:'Critical: needs a human call', R3:'Nothing to verify',
  R4:'Waiting for more details', R5:'No check needed', R6:'No send window before the season ends',
  R7:'Crop already harvested', R8:'Gets a survey',
};
const STATUS = {accepted:'Accepted', skipped:'Skipped by rules', rejected:'Rejected by reviewer', failed:'Failed'};
const FILTERS = [['crop','Crop'],['concern','Concern'],['resolution_type','Resolution'],['quality_flag','Quality']];
const PAGE = 50;

const view = {
  root:null, request:0, poll:null, datasets:[], configs:[], activeConfig:null, facets:null,
  form:{dataset:null, config:'', selection:{limit:25}},
  run:{id:null, filter:null, rows:[], insight:null},
};

const words = value => String(value ?? '').replaceAll('_',' ');
const when = iso => iso ? new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) : '';
const number = value => Number(value).toLocaleString();
const disclosure = (label,value) => el('details',{},el('summary',{},label),el('pre',{},JSON.stringify(value,null,2)));
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
  view.root=root;
  clearTimeout(view.poll);
  const [, page, id, insight]=hash.replace(/^#/,'').split('/');
  if (page==='run' && id) return renderRun(id, insight?decodeURIComponent(insight):null);
  if (page==='job' && id) return followJob(id);
  return renderHome();
}

// Home: the new-run panel and recent runs.

async function renderHome() {
  const ticket=++view.request;
  view.root.replaceChildren(el('p',{class:'empty-note'},'Loading insights and runs…'));
  try {
    const [datasets,configs,runs,jobs]=await Promise.all([api('/datasets'),api('/configs'),api('/runs'),api('/jobs')]);
    if (ticket!==view.request) return;
    view.datasets=datasets.datasets;
    view.configs=configs.configs;
    view.activeConfig=configs.active_config_id;
    if (!view.datasets.some(d=>d.dataset_id===view.form.dataset)) view.form.dataset=view.datasets[0]?.dataset_id || null;
    const waiting=jobs.jobs.filter(j=>j.kind==='batch' && j.status==='queued');
    const [panel,plan]=await composer();
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
  view.facets=await api(`/datasets/${view.form.dataset}/facets`);
  const f=view.form, s=f.selection;
  const summary=el('div',{class:'gen-estimate',role:'status','aria-live':'polite'});
  const submit=el('button',{class:'primary wide',type:'button',onClick:()=>start(submit,summary)},'Run');
  const changed=debounce(()=>estimate(summary,submit),350);
  const set=(key,value)=>{ if (value===''||value===null) delete s[key]; else s[key]=value; changed(); };

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
  const configPicker=el('select',{'aria-label':'Agent configuration',onChange:event=>{f.config=event.target.value;changed();}},
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
  estimate(summary,submit);
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
  const ticket=++view.request;
  summary.replaceChildren(el('p',{class:'small muted'},'Checking the rules…'));
  try {
    const s=view.form.selection;
    const query=new URLSearchParams(Object.entries(s).filter(([,v])=>v!==undefined));
    const [count,plan]=await Promise.all([
      api(`/datasets/${view.form.dataset}/count?${query}`),
      api('/jobs/batch/estimate',{method:'POST',body:JSON.stringify(batchBody())}),
    ]);
    if (ticket!==view.request) return;
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
    if (ticket===view.request) summary.replaceChildren(message(error.message,true));
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
    location.hash=`#generate/job/${job.job_id}`;
  } catch(error) {
    submit.disabled=false; submit.textContent='Run';
    summary.append(message(`The run did not start. ${error.message}`,true));
  }
}

function recent(runs,waiting) {
  const list=el('section',{class:'gen-recent','aria-labelledby':'recent-title'},el('h2',{id:'recent-title'},'Recent runs'));
  if (!runs.length && !waiting.length) {
    list.append(el('p',{class:'muted'},'Runs appear here. Each one keeps its insights, rulings, questions and every agent step.'));
    return list;
  }
  for (const job of waiting) {
    list.append(el('a',{class:'gen-run-link',href:`#generate/job/${job.job_id}`},
      el('strong',{},describeSelection(job.spec.selection)),el('span',{class:'small muted'},`Waiting to start · ${when(job.created_at)}`)));
  }
  for (const run of runs) {
    const counts=Object.entries(run.counts).map(([k,n])=>`${number(n)} ${k}`).join(' · ') || 'No results yet';
    list.append(el('a',{class:'gen-run-link',href:`#generate/run/${run.run_id}`},
      el('span',{class:'gen-run-title'},el('strong',{},describeSelection(run.selection?.selection || {limit:run.selection?.limit})),el('span',{class:`badge status-${run.status}`},words(run.status))),
      el('span',{class:'small muted'},`${counts} · ${when(run.created_at)}`)));
  }
  return list;
}

// A queued job becomes a run once a runner claims it.

async function followJob(jobId) {
  const ticket=++view.request;
  try {
    const job=await api(`/jobs/${jobId}`);
    if (ticket!==view.request) return;
    if (job.runs.length) { location.replace(`#generate/run/${job.runs[0].run_id}`); return; }
    const waiting=job.status==='failed'?
      message(`The run failed before it started. ${job.error || ''}`,true):
      el('div',{class:'gen-waiting'},el('div',{class:'spinner','aria-hidden':true}),
        el('div',{},el('h2',{},'Starting the run'),el('p',{class:'muted'},'Hosted runs start a worker task, which takes about a minute. Locally, a `mel-worker jobs` runner picks the job up.')));
    view.root.replaceChildren(el('a',{href:'#generate',class:'back'},'← All runs'),waiting);
    if (job.status!=='failed') view.poll=setTimeout(()=>followJob(jobId),2000);
  } catch(error) {
    if (ticket===view.request) view.root.replaceChildren(message(`Cannot read the job. ${error.message}`,true));
  }
}

// Run page: progress, outcome counts, results, and the selected insight's detail.

async function renderRun(runId,insightId) {
  const ticket=++view.request;
  const fresh=view.run.id!==runId;
  if (fresh) Object.assign(view.run,{id:runId,filter:null,rows:[],insight:null});
  try {
    const status=view.run.filter?`&status=${view.run.filter}`:'';
    const [summary,page]=await Promise.all([
      api(`/runs/${runId}`),
      api(`/runs/${runId}/results?limit=${Math.max(PAGE,view.run.rows.length)}${status}`),
    ]);
    if (ticket!==view.request) return;
    view.run.rows=page.results;
    const shell=view.root.querySelector('.gen-run')?.dataset.run===runId?view.root.querySelector('.gen-run'):null;
    const layout=shell || el('div',{class:'gen-run','data-run':runId},
      el('a',{href:'#generate',class:'back'},'← All runs'),el('div',{class:'gen-run-head'}),
      el('div',{class:'gen-run-body'},el('div',{class:'gen-results'}),el('aside',{class:'gen-detail'})));
    if (!shell) view.root.replaceChildren(layout);
    layout.querySelector('.gen-run-head').replaceChildren(...runHeader(summary));
    layout.querySelector('.gen-results').replaceChildren(resultsTable(summary,page.results));
    if (insightId!==view.run.insight || !shell) showDetail(layout.querySelector('.gen-detail'),runId,insightId);
    if (['running','queued'].includes(summary.status)) view.poll=setTimeout(()=>renderRun(runId,view.run.insight),4000);
  } catch(error) {
    if (ticket===view.request) view.root.replaceChildren(el('a',{href:'#generate',class:'back'},'← All runs'),message(`Cannot load the run. ${error.message}`,true));
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
      el('p',{class:'muted small'},`Started ${when(summary.created_at)} · configuration ${summary.config_id?.slice(0,8) || 'not recorded'} · ${number(summary.input_tokens+summary.output_tokens)} tokens`)),
      el('span',{class:`badge status-${summary.status}`},words(summary.status))),
  ];
  head.push(costSummary(summary.costs,summary.run_id));
  if (summary.status==='running' && total) {
    head.push(el('div',{class:'progress',role:'progressbar','aria-valuemin':0,'aria-valuemax':total,'aria-valuenow':done,'aria-label':'Insights processed'},
      el('span',{style:`width:${Math.round(100*done/total)}%`})),el('p',{class:'small muted'},`${number(done)} of ${number(total)} insights processed`));
  }
  head.push(el('div',{class:'gen-metrics'},Object.keys(STATUS).map(key=>el('button',{type:'button',class:'metric','aria-pressed':view.run.filter===key,
    onClick:()=>{view.run.filter=view.run.filter===key?null:key;view.run.rows=[];renderRun(view.run.id,view.run.insight);}},
    el('span',{},STATUS[key]),el('strong',{},number(counts[key] || 0))))));
  return head;
}

function outcomeText(row) {
  if (row.status==='accepted') return `“${row.first_question || 'Survey'}” · ${row.questions} question${row.questions===1?'':'s'}${row.relevance!==null?` · relevance ${row.relevance}`:''}`;
  if (row.status==='skipped') return `${row.rule || ''} ${RULES[row.rule] || words(row.reason)}${row.detail?` · ${row.detail}`:''}`;
  if (row.status==='rejected') return `Reviewer: ${(row.reason || 'did not accept the draft').slice(0,160)}`;
  return `Failed: ${row.reason || 'unknown error'}`;
}

function resultsTable(summary,rows) {
  if (!rows.length) return el('p',{class:'empty-note'},['running','queued'].includes(summary.status)?'Results appear as each insight finishes.':'No results match this filter.');
  const table=el('table',{class:'gen-table'},
    el('thead',{},el('tr',{},el('th',{},'Insight'),el('th',{},'Outcome'),el('th',{},'What happened'),el('th',{},'Time'))),
    el('tbody',{},rows.map(row=>el('tr',{'aria-selected':row.insight_id===view.run.insight,tabindex:0,
      onClick:()=>{location.hash=`#generate/run/${view.run.id}/${encodeURIComponent(row.insight_id)}`;},
      onKeydown:event=>{if(event.key==='Enter') location.hash=`#generate/run/${view.run.id}/${encodeURIComponent(row.insight_id)}`;}},
      el('td',{},el('strong',{},row.insight_id),el('span',{class:'small muted'},` ${row.insight.crop || ''}`)),
      el('td',{},statusBadge(row.status)),
      el('td',{class:'gen-what'},outcomeText(row)),
      el('td',{class:'small muted'},row.elapsed_seconds?`${row.elapsed_seconds.toFixed(1)} s`:'')))));
  const more=rows.length>=Math.max(PAGE,view.run.rows.length)?el('button',{type:'button',class:'secondary',onClick:async()=>{
    more.disabled=true;
    const status=view.run.filter?`&status=${view.run.filter}`:'';
    const next=await api(`/runs/${view.run.id}/results?offset=${view.run.rows.length}&limit=${PAGE}${status}`);
    view.run.rows=[...view.run.rows,...next.results];
    table.replaceWith(resultsTable(summary,view.run.rows));
  }},'Show more'):null;
  return el('div',{},el('div',{class:'table-scroll'},table),more);
}

async function showDetail(panel,runId,insightId) {
  view.run.insight=insightId;
  if (!insightId) { panel.replaceChildren(el('p',{class:'muted'},'Select an insight to see the agent’s steps and preview its survey.')); return; }
  panel.replaceChildren(el('p',{class:'small muted'},'Loading…'));
  try {
    const record=await api(`/runs/${runId}/results/${encodeURIComponent(insightId)}`);
    if (view.run.insight!==insightId) return;
    const {insight,outcome}=record;
    const survey=outcome.survey || outcome.candidate_survey;
    const sections=[
      el('div',{class:'gen-detail-head'},el('h2',{},insightId),statusBadge(record.status)),
      el('p',{class:'small muted'},[insight.asset?.name,words(insight.concern),insight.conversation_language,when(insight.created_at)].filter(Boolean).join(' · ')),
      el('h3',{},'Farmer asked'),el('p',{},insight.query_summary || 'Not recorded'),
      el('h3',{},'Advice given'),el('p',{},insight.advice_summary || 'Not recorded'),
      el('h3',{},'Agent steps'),steps(outcome.events || []),
    ];
    if (survey?.items?.length) {
      const host=el('div',{class:'survey-player'});
      mountSurveyPlayer(host,{survey,session:{phase:'intro',question:0,answers:{}},photoUrl:'/static/assets/mustard-field.webp'});
      sections.push(el('h3',{},outcome.survey?'Survey preview':'Rejected draft'),el('figure',{class:'phone-stage'},host,el('figcaption',{},'Preview only · answers are not sent')));
    }
    sections.push(disclosure('Full insight',insight),disclosure('Full outcome',outcome));
    panel.replaceChildren(...sections);
  } catch(error) {
    panel.replaceChildren(message(`Cannot load this result. ${error.message}`,true));
  }
}

function stepSummary(event) {
  const d=event.data || {};
  if (d.rule) return `${d.rule} · ${RULES[d.rule] || ''}${d.explanation?` · ${d.explanation}`:''}`;
  if ('valid' in d) return `${words(d.target)} schema ${d.valid?'valid':`invalid (${(d.errors || []).length} errors)`}`;
  if (event.stage==='generate' && d.kind==='request') return 'Insight and context sent to the generator';
  if (event.stage==='generate' && d.survey) return `${d.survey.items?.length || 0} questions drafted`;
  if (event.stage==='review' && d.kind==='request') return 'Draft sent to the reviewer';
  if (d.review) return `Reviewer: relevance ${d.review.relevance_score}${d.review.issues?.length?` · ${d.review.issues.join('; ')}`:''}`;
  if (d.status) return `Ruling: ${words(d.status)}${d.reason?` · ${d.reason}`:''}`;
  if (d.error_type) return `Failed: ${d.error_type}`;
  if (d.insight) return 'Insight received';
  if (d.context) return 'Farmer context resolved';
  return '';
}

function steps(events) {
  if (!events.length) return el('p',{class:'muted'},'No steps were recorded for this result.');
  return el('ol',{class:'gen-steps'},events.map(event=>el('li',{},
    el('details',{},el('summary',{},el('span',{class:`step-source source-${event.source}`},event.source),
      el('strong',{},words(event.stage)),event.attempt?el('span',{class:'small muted'},` attempt ${event.attempt}`):null,
      el('span',{class:'step-text'},stepSummary(event))),
    el('pre',{},JSON.stringify(event.data,null,2))))));
}

function debounce(fn,ms) {
  let timer;
  return (...args)=>{clearTimeout(timer);timer=setTimeout(()=>fn(...args),ms);};
}

export function leaveGenerate() {
  clearTimeout(view.poll);
  view.request++;
}
