import {budgetInput, capPanel, costSummary, money} from './costs.mjs';
import {el, skeleton} from './dom.mjs';
import {signedIn, signOut} from './auth.mjs';
import {api as globalApi, requestScope} from './api.mjs';
import {showGenerate, showRuns, leaveGenerate} from './generate.mjs';
import {showConfigure, leaveConfigure} from './configure.mjs';
import {showReview, leaveReview, addToReviewList} from './review.mjs';
import {mountSurveyPlayer} from './survey-player.mjs';
import {displayValue, questionAssessments, eventsForStage} from './domain.mjs';
import {insightJourney} from './journey.mjs';
import {apply as applyDetail, atLevel, mountDial} from './detail.mjs';
const requests=requestScope();
const api=(path,options)=>requests.api(path,options);

const $ = id => document.getElementById(id);
const state = {config:null, experiments:[], detail:null, run:0, result:0, stage:null, event:0, question:0, request:0};
let poll;
let rehearsalKey, rehearsalSession;
const text=(value)=>typeof value === 'object' ? JSON.stringify(value,null,2) : displayValue(value);
const raw=value=>el('pre',{},text(value));
const badge=value=>el('span',{class:`badge ${['failed','rejected','completed_with_errors'].includes(value)?'warning':''}`},value?.replaceAll('_',' ') || 'Unknown');
function notice(message, error=false) { $('notice').hidden=!message; $('notice').className=error?'notice error':'notice'; $('notice').textContent=message; }
const disclosure=(label,value,attrs={})=>el('details',attrs,el('summary',{},label),raw(value));
const EVERYTHING={'data-level':3};
function picker(label,options,value,change) {
  return el('label',{},label,el('select',{'aria-label':label,onChange:event=>change(event.target.value)},options.map(([id,title])=>el('option',{value:id,selected:String(id)===String(value)},title))));
}
async function listExperiments() {
  const data=await api('/experiments'); state.experiments=data.experiments;
  $('experiment-list').replaceChildren(...(state.experiments.length ? state.experiments.map(item=>el('button',{
    class:'experiment', 'aria-pressed':state.detail?.experiment_id===item.experiment_id,
    onClick:()=>openExperiment(item.experiment_id),
  },el('strong',{},item.name),el('span',{class:'small muted'},`${item.source==='recorded'?'Recorded':'Live'} · ${item.insight_count} insights · ${item.candidate_count} versions`),badge(item.status))) : [el('p',{class:'empty-note'},'No comparisons yet. Import the recorded run, or compare saved versions with New comparison.')]));
}
async function openExperiment(id, refresh=false, reference=null) {
  requests.renew();
  const ticket=++state.request;
  clearTimeout(poll);
  notice('');
  $('composer').hidden=true; $('content').hidden=false;
  // Shareable address without a hashchange, which would route back here.
  if (location.hash!==`#compare/${id}`) history.replaceState(null,'',`#compare/${id}`);
  if(!refresh) $('content').replaceChildren(skeleton('Loading saved evidence…'));
  try {
    reference=reference || (state.detail?.experiment_id===id ? state.detail.comparison?.reference : null);
    const query=reference?`?evaluation_reference=${encodeURIComponent(reference)}`:'';
    const detail=await api(`/experiments/${encodeURIComponent(id)}${query}`);
    if(ticket!==state.request) return;
    const previousStatus=state.detail?.status;
    state.detail=detail;
    if(!refresh) { rehearsalKey=null; Object.assign(state,{run:0,result:0,stage:null,event:0,question:0}); }
    renderExperiment();
    if(!refresh || previousStatus!==detail.status) listExperiments().catch(()=>{});
    if(ticket===state.request && ['queued','running'].includes(detail.status)) poll=setTimeout(()=>openExperiment(id,true),3000);
  } catch(error) { if(ticket===state.request) { notice(`Cannot load experiment. ${error.message} Use Refresh to try again.`,true); $('content').replaceChildren(el('h1',{},'Experiment unavailable')); } }
}
function selected() {
  const run=state.detail.runs[state.run];
  const result=run?.results[state.result];
  return {run,result,outcome:result?.outcome};
}
function renderExperiment() {
  const d=state.detail, {run,result,outcome}=selected();
  const heading=el('div',{class:'heading'},el('div',{},el('h1',{},d.name),el('p',{class:'muted'},d.source==='recorded'?'Recorded comparison. Saved results only.':'Comparison of saved configuration versions on the same insights.')),el('div',{class:'status-group'},badge(d.source),badge(d.status)));
  const controls=el('div',{class:'selection'},picker('Version',d.runs.map((r,i)=>[i,`${r.label} · ${r.api || 'API not recorded'}`]),state.run,value=>{rehearsalKey=null;state.run=Number(value);state.result=0;state.stage=null;state.question=0;renderExperiment();}),picker('Insight',(run?.results || []).map((r,i)=>[i,`${r.insight_id} · ${r.outcome.status}`]),state.result,value=>{rehearsalKey=null;state.result=Number(value);state.stage=null;state.question=0;renderExperiment();}));
  const sections=[heading,renderProgress(d),run?costSummary(run.costs,run.run_id):null].filter(Boolean);
  if(!d.runs.length) {
    // No run has started yet: show what will run instead of empty pickers.
    sections.push(el('div',{class:'gen-waiting'},['queued','running'].includes(d.status)?el('div',{class:'spinner','aria-hidden':true}):null,
      el('div',{},el('h2',{},d.job?.status==='failed'?'The comparison did not start':'Starting the comparison'),
        el('p',{class:'muted'},d.job?.status==='failed'?(d.job.error || 'The job failed before any version ran.'):'Hosted comparisons start a worker task, which takes about a minute. Each version then runs every insight in turn.'),
        el('ul',{},(d.spec.candidates || []).map(c=>el('li',{},c.label))))));
    sections.push(disclosure('Comparison inputs',d.spec));
    $('content').replaceChildren(...sections);
    return;
  }
  sections.push(controls);
  if(outcome) {
    sections.push(el('div',{class:'outcome-line'},badge(outcome.status),el('span',{},outcome.reason || 'No additional outcome reason recorded.'),el('span',{class:'small muted'},run.model_id)));
    sections.push(renderJourney(outcome));
    sections.push(el('div',{class:'inspect-grid'},renderInspector(result,outcome),renderSurvey(outcome)));
  } else sections.push(el('div',{class:'empty-note'},['queued','running'].includes(d.status)?'Waiting for persisted results. This view refreshes while execution is active.':'No result records are available for this candidate.'));
  sections.push(renderComparison(d));
  sections.push(disclosure('Experiment inputs and configuration',d.spec,EVERYTHING),disclosure('Run manifest',run?.manifest ?? 'Not recorded',EVERYTHING));
  $('content').replaceChildren(...sections);
}
function renderProgress(d) {
  const job=d.job, progress=job?.progress || {};
  if(!job || !['queued','running'].includes(job.status) || !progress.total) return null;
  const versions=(d.spec.candidates || []).length, perVersion=progress.total/versions;
  const current=Math.min(versions,Math.floor(progress.done/perVersion)+1);
  return el('div',{class:'comparison-progress'},
    el('div',{class:'progress',role:'progressbar','aria-valuemin':0,'aria-valuemax':progress.total,'aria-valuenow':progress.done,'aria-label':'Results saved'},el('span',{style:`width:${Math.round(100*progress.done/progress.total)}%`})),
    el('p',{class:'small muted'},`${progress.done} of ${progress.total} results saved · running version ${current} of ${versions}`));
}
function renderJourney(outcome) {
  if (!state.stage && outcome.events?.length) {
    state.stage=outcome.events.at(-1).stage;
    state.event=eventsForStage(outcome.events,state.stage).length-1;
  }
  return insightJourney(outcome,{stage:state.stage,event:state.event,onSelect:(stage,event)=>{
    state.stage=stage; state.event=event; renderExperiment();
  }});
}
function renderInspector(result,outcome) {
  const events=eventsForStage(outcome.events || [],state.stage);
  const event=events[state.event] || events[0];
  const panel=el('section',{class:'inspector'},el('h2',{},'Stage evidence'));
  if(!event) panel.append(el('p',{class:'muted'},'No event was recorded for this step.'));
  else {
    panel.append(atLevel(picker('Recorded event',events.map((e,i)=>[i,`#${e.sequence} · ${e.source} · attempt ${e.attempt}`]),state.event,value=>{state.event=Number(value);renderExperiment();}),3),el('p',{class:'small muted','data-level':3},`${event.stage} · ${event.source} evidence · ${event.created_at || 'Time not recorded'}`));
    const data=event.data;
    if('eligible' in data) panel.append(el('h3',{},data.eligible?'Eligible for a survey':'Survey not required'),el('p',{},data.reason || 'The insight passed the configured eligibility rules.'));
    if('valid' in data) panel.append(el('h3',{},`${data.target} schema ${data.valid?'passed':'failed'}`),...(data.errors || []).map(issue=>el('p',{class:'missing'},text(issue))));
    if(data.send_window) panel.append(el('h3',{},'Send window'),el('p',{},`${data.send_window.not_before} → ${data.send_window.not_after}`));
    if(data.survey) panel.append(el('h3',{},`${data.survey.items?.length || 0} questions ${data.kind==='response'?'generated':'submitted for review'}`),el('p',{},`Language · ${data.survey.language}`));
    if(data.review) panel.append(el('h3',{},'Reviewer findings'),el('p',{},`Survey relevance · ${data.review.relevance_score}`),...(data.review.issues || []).map(issue=>el('p',{class:'finding'},issue)));
    if(data.context) panel.append(el('h3',{},'Context and provenance'),el('p',{},`${data.context.profile?'Farmer profile matched':'No farmer profile matched'} · ${(data.context.crop_calendar || []).length} calendar rows`),...(data.context.conflicts || []).map(issue=>el('p',{class:'missing'},issue)));
    if(data.status) panel.append(el('h3',{},`Ruling · ${data.status}`),el('p',{},data.reason || 'The saved result satisfied the configured acceptance gates.'));
    if(data.error_type) panel.append(el('h3',{},'Execution failed'),el('p',{class:'missing'},data.error_type));
    panel.append(disclosure('Full recorded event',event.data,EVERYTHING));
  }
  panel.append(disclosure('Original insight',result.input,EVERYTHING),disclosure('Final outcome',outcome,EVERYTHING));
  if(outcome.review) panel.append(el('h3',EVERYTHING,'Survey-wide model review'),el('p',{class:'small muted',...EVERYTHING},'Model judgment across the survey. This is not an independent question-quality score.'),atLevel(raw(outcome.review),3));
  return panel;
}
function renderSurvey(outcome) {
  const survey=outcome.status==='failed'?null:outcome.survey || outcome.candidate_survey;
  const panel=el('section',{class:'survey-panel'},el('h2',{},outcome.candidate_survey && !outcome.survey?'Rejected draft':'Question preview'));
  if(!survey?.items?.length) { panel.append(el('p',{class:'muted'},'No survey questions recorded for this outcome.')); return panel; }
  panel.append(el('p',{class:'small muted'},'Try the survey. Answers stay in this page only and are not submitted. Inspection tabs show question evidence.'));
  const {run:chosenRun,result:chosenResult}=selected();
  const key=JSON.stringify([state.detail.experiment_id,chosenRun.run_id,chosenResult.insight_id,chosenResult.input,survey]);
  if(key!==rehearsalKey) {
    rehearsalKey=key;
    rehearsalSession={phase:'intro',question:0,answers:{}};
    state.question=0;
  }
  const tabs=el('div',{class:'question-tabs',role:'group','aria-label':'Questions'});
  const analysis=el('div',{});
  const host=el('div',{class:'survey-player'});
  const updateAnalysis=index=>{
    state.question=index;
    [...tabs.children].forEach((tab,i)=>tab.setAttribute('aria-pressed',String(i===index)));
    analysis.replaceChildren(renderQuestionAnalysis(outcome,survey.items[index]));
  };
  const player=mountSurveyPlayer(host,{survey,session:rehearsalSession,photoUrl:'/static/assets/mustard-field.webp',onQuestionChange:updateAnalysis});
  survey.items.forEach((q,i)=>tabs.append(el('button',{type:'button',onClick:()=>player.showQuestion(i)},`Question ${i+1}`)));
  panel.append(tabs,el('figure',{class:'phone-stage'},host,el('figcaption',{},'Survey preview · answers are not submitted',el('span',{},'Illustrative field photo, unrelated to the selected case. ',el('a',{href:'https://unsplash.com/photos/a-person-standing-in-a-field-of-yellow-flowers-u5Jb5tTfSa4',target:'_blank',rel:'noreferrer'},'Abhi Verma / Unsplash')))),analysis);
  updateAnalysis(rehearsalSession.question);
  if(chosenRun.run_id) panel.append(addToReviewList(chosenRun.run_id,chosenResult.insight_id,survey));
  return panel;
}
function renderQuestionAnalysis(outcome,item) {
  const panel=el('div',{class:'question-analysis'},el('p',{class:'small muted question-record'},`${item.item_id} · ${item.type}`));
  const evidence=(outcome.question_evidence || []).filter(e=>e.item_id===item.item_id);
  panel.append(el('div',{class:'evidence'},el('h3',{},'What this question measures'),evidence.length?evidence.map(e=>el('div',{},el('strong',{},e.measurement.replaceAll('_',' ')),el('p',{},e.explanation),el('p',{class:'small'},`Source fields · ${e.source_fields.join(', ') || 'Not recorded'}`))):el('p',{},'Question-level evidence not recorded.'),el('p',{class:'small muted'},'Diagnostic explanation supplied by the model. It is not independent validation.')));
  const {run,result}=selected();
  const assessments=questionAssessments(state.detail.assessments || [],run.run_id,result.insight_id,item.item_id);
  panel.append(el('h3',{},'Question quality'),...(assessments.length?assessments.map(a=>el('p',{class:'assessment'},`${a.assessor} · question · ${a.dimension} · ${a.assessment==='assessed'?displayValue(a.score):'Not assessed'} · ${a.note || ''}`)):[el('p',{class:'muted'},'No question-level quality assessments recorded. Human quality is not assessed.')]),disclosure('Question metadata',item));
  return panel;
}
function renderComparison(d) {
  const c=d.comparison;
  const section=el('section',{class:'comparison'},el('h2',{},'Compare candidates'),el('p',{class:'muted'},`Evaluation reference · ${c?.reference || d.spec?.evaluation_reference || 'Not recorded'}. All saved cases remain in the reliability counts.`));
  section.append(picker('Reanalyze saved outputs',[['rules','Saved execution rules'],['golden','Current golden reference']],c?.reference || 'rules',value=>openExperiment(d.experiment_id,true,value)),el('p',{class:'small muted'},'Changing the analysis reference reuses saved outputs. It makes no model calls and does not change execution rules.'));
  if(!c?.rows?.length) {section.append(el('p',{},'Comparison is not available yet.')); return section;}
  const agreement=row=>{const cases=(c.cases || []).filter(item=>item.run_id===row.run_id);return `${cases.filter(item=>item.decision_agreement===true).length} matched · ${cases.filter(item=>item.assessment==='assessed').length}/${row.total} assessed`;};
  const columns=[['Candidate',r=>r.label],['Cases',r=>r.total],['Accepted',r=>r.accepted],['Skipped',r=>r.skipped],['Rejected',r=>r.rejected],['Failed',r=>r.failed],['Missing',r=>r.missing],['Decision agreement',agreement],['Input tokens',r=>r.usage_complete?r.input_tokens:null,3],['Output tokens',r=>r.usage_complete?r.output_tokens:null,3],['Survey relevance',r=>r.mean_relevance]];
  const header=el('thead',{},el('tr',{},columns.map(([label,,level])=>el('th',{scope:'col','data-level':level},label))));
  const body=el('tbody',{},c.rows.map(row=>el('tr',{},columns.map(([,get,level],i)=>el(i===0?'th':'td',i===0?{scope:'row'}:{'data-level':level},displayValue(get(row)))))));
  section.append(el('div',{class:'table-scroll',tabindex:0,'aria-label':'Candidate comparison'},el('table',{},header,body)),el('p',{class:'small muted'},'Survey relevance is a survey-wide model judgment. Missing scores and incomplete token usage are not zero. No overall quality score is inferred.'));
  for(const note of c.notes || []) section.append(el('p',{class:'small'},note));
  const {run,result}=selected();
  const diagnostic=c.cases?.find(item=>item.run_id===run?.run_id && item.insight_id===result?.insight_id);
  if(diagnostic) {
    const checks=diagnostic.deterministic_checks;
    section.append(el('h3',{},'Selected survey checks'),el('div',{class:'check-grid'},Object.entries(checks?.checks || {}).map(([key,value])=>el('span',{class:`check ${value?'pass':'fail'}`},`${value?'Pass':'Fail'} · ${key.replaceAll('_',' ')}`))),el('p',{class:'small muted'},checks?.assessment==='assessed'?checks.limitations:'Reference checks are not assessed for this result.'),disclosure('Case-level reference evidence',diagnostic));
  }
  section.append(disclosure('Analysis reference version',c.reference_snapshot,EVERYTHING));
  const other=(d.assessments || []).filter(a=>a.scope!=='question');
  section.append(disclosure(`Scoped survey and run assessments (${other.length})`,other.length?other:'No survey or run assessments recorded.',EVERYTHING));
  return section;
}
// A comparison runs saved configuration versions over the same insights.
async function renderComposer() {
  requests.renew();
  clearTimeout(poll); const ticket=++state.request;
  $('content').hidden=true; $('composer').hidden=false;
  $('composer').replaceChildren(skeleton('Loading versions and datasets…'));
  let configs, active, datasets;
  try {
    const [list,data]=await Promise.all([api('/configs'),api('/datasets')]);
    if (ticket!==state.request) return;
    configs=list.configs.filter(c=>c.status!=='retired'); active=list.active_config_id; datasets=data.datasets;
  } catch(error) { if (ticket!==state.request) return; $('composer').replaceChildren(el('p',{class:'notice error'},`Cannot load versions. ${error.message}`)); return; }
  const back=el('button',{class:'secondary',onClick:()=>{ $('composer').hidden=true; $('content').hidden=false; if(state.detail) openExperiment(state.detail.experiment_id); }},'Back to comparisons');
  const heading=el('div',{class:'heading'},el('div',{},el('h1',{id:'form-title'},'New comparison'),el('p',{class:'muted'},'Run saved versions on the same insights and see them side by side.')),back);
  if (!datasets.length) { $('composer').replaceChildren(heading,el('p',{class:'notice'},'Load the participant pack in Generate first.'),el('a',{href:'#generate'},'Open Generate')); return; }
  const versionLabel=c=>`${c.name || `Draft ${c.config_id.slice(0,8)}`}${c.config_id===active?' (active)':''} · ${c.generation_model}`;
  const candidates=el('div',{class:'candidate-fields'});
  function addCandidate(config) {
    const picker=el('select',{name:'candidate_config','aria-label':'Configuration version'},configs.map(c=>el('option',{value:c.config_id,selected:c.config_id===config?.config_id},versionLabel(c))));
    const label=el('input',{name:'candidate_label',required:true,maxlength:120,value:config?.name || `Candidate ${candidates.children.length+1}`});
    const row=el('div',{class:'candidate-row'},el('label',{},'Label',label),el('label',{},'Version',picker));
    row.append(el('button',{type:'button',class:'quiet',onClick:()=>{row.remove();add.disabled=candidates.children.length>=6;}},'Remove')); candidates.append(row);
    add.disabled=candidates.children.length>=6;
  }
  const add=el('button',{type:'button',class:'secondary',onClick:()=>addCandidate()},'Add version');
  addCandidate(configs.find(c=>c.config_id===active) || configs[0]);
  const dataset=el('select',{name:'dataset','aria-label':'Dataset'},datasets.map(d=>el('option',{value:d.dataset_id},`${d.name} · ${d.row_count.toLocaleString()} insights`)));
  const crop=el('select',{name:'crop','aria-label':'Crop'},el('option',{value:''},'Any crop'));
  let cropRequest=0;
  const loadCrops=async()=>{ const current=++cropRequest; crop.disabled=true; const f=await api(`/datasets/${dataset.value}/facets`); if(current!==cropRequest || ticket!==state.request) return; crop.disabled=false; crop.replaceChildren(el('option',{value:''},'Any crop'),...f.facets.crop.map(o=>el('option',{value:o.value},`${o.value} (${o.count.toLocaleString()})`))); };
  dataset.addEventListener('change',()=>loadCrops().catch(error=>{if(ticket===state.request) notice(error.message,true);})); loadCrops().catch(()=>{});
  const form=el('form',{onSubmit:async event=>{
    event.preventDefault();
    const submit=form.querySelector('[type=submit]');
    try {
      const data=new FormData(form);
      const rows=[...candidates.children];
      if(!rows.length) throw new Error('Add at least one version.');
      const selection={limit:Number(data.get('limit'))}; if(data.get('crop')) selection.crop=data.get('crop');
      const spec={name:data.get('name').trim(),dataset_id:data.get('dataset'),selection,evaluation_reference:data.get('reference'),budget_usd:data.get('budget_usd'),
        candidates:rows.map(row=>({label:row.querySelector('input').value.trim(),config_id:row.querySelector('select').value}))};
      submit.disabled=true; notice('Starting the comparison…');
      const result=await api('/experiments',{method:'POST',body:JSON.stringify(spec)});
      notice('Comparison queued. Results appear as each version finishes an insight.'); await openExperiment(result.experiment_id);
    } catch(error) { if(error.name!=='AbortError') notice(`Comparison was not started. ${error.message}`,true); }
    finally {submit.disabled=!state.config?.live_enabled;}
  }});
  form.append(
    el('label',{},'Name',el('input',{name:'name',required:true,maxlength:200,placeholder:'Shorter outcome options vs baseline'})),
    el('h2',{},'Versions'),
    el('p',{class:'muted'},'Each version runs the same insights. Edit and save versions in Configure.'),
    candidates,add,
    el('h2',{},'Insights'),
    el('div',{class:'form-grid'},el('label',{},'Dataset',dataset),el('label',{},'Crop',crop),
      el('label',{},'How many (1–50)',el('input',{name:'limit',type:'number',min:1,max:50,value:10,required:true})),
      el('label',{},'Score against',el('select',{name:'reference'},el('option',{value:'rules'},'Each version’s rules'),el('option',{value:'golden'},'Golden reference')))),
    budgetInput('Budget per candidate run (USD)'),
    capPanel(),
    el('p',{class:'small muted'},'Each candidate has its own limit. Total comparison exposure is at most the sum of those limits, subject to the configured prices and context bounds.'),
    el('div',{class:'launch'},
      el('p',{},state.config?.live_enabled?'This calls each version’s models. Results stay outside the scheduler handoff queue.':'Live execution is disabled by the server. Recorded comparisons remain available.'),
      el('button',{type:'submit',class:'primary',disabled:!state.config?.live_enabled},'Run comparison')));
  const costEstimate=el('div',{role:'status','data-level':2});
  form.append(el('button',{type:'button',class:'secondary','data-level':2,onClick:async()=>{
    try { const data=new FormData(form);const selection={limit:Number(data.get('limit'))};if(data.get('crop'))selection.crop=data.get('crop');
      const result=await api('/experiments/estimate',{method:'POST',body:JSON.stringify({name:data.get('name').trim() || 'Estimate',dataset_id:data.get('dataset'),selection,budget_usd:data.get('budget_usd'),candidates:[...candidates.children].map(row=>({label:row.querySelector('input').value,config_id:row.querySelector('select').value}))})});
      costEstimate.replaceChildren(...result.candidates.map(c=>el('p',{},`${c.label}: about ${money(c.estimated_usd)}. ${c.note}`)));
    } catch(e) {costEstimate.replaceChildren(el('p',{class:'notice error'},e.message));}
  }},'Estimate cost'),costEstimate);
  form.addEventListener('input',()=>costEstimate.replaceChildren());
  form.addEventListener('change',()=>costEstimate.replaceChildren());
  $('composer').replaceChildren(heading,form);
}
$('new').addEventListener('click',renderComposer);
$('refresh').addEventListener('click',async()=>{try {await listExperiments();if(state.detail) await openExperiment(state.detail.experiment_id,true); notice('Saved experiments refreshed.');}catch(error){if(error.name!=='AbortError') notice(error.message,true);}});
$('import').addEventListener('click',async()=>{const b=$('import');b.disabled=true;try {const result=await api('/import-recorded',{method:'POST'});notice('Recorded artifact imported. Intermediate events are shown only when they were saved.');await openExperiment(result.experiment_id);}catch(error){if(error.name!=='AbortError') notice(`Recorded import unavailable. ${error.message}`,true);}finally{b.disabled=false;}});
let compareLoaded=false;
let activeTab=null;
function route() {
  const hash=location.hash || '#generate';
  const tab=['runs','compare','review','configure'].find(name=>hash.startsWith(`#${name}`)) || 'generate';
  if (tab!==activeTab) {
    leaveGenerate(); leaveConfigure(); leaveReview();
    clearTimeout(poll); ++state.request; requests.renew();
    activeTab=tab;
  }
  applyDetail(tab);
  for (const name of ['generate','runs','compare','review','configure']) $(`${name}-view`).hidden=name!==tab;
  document.querySelectorAll('[data-tab]').forEach(link=>link.setAttribute('aria-current',String(link.dataset.tab===tab)));
  if (tab==='runs') { clearTimeout(poll); showRuns($('runs-view'),hash); return; }
  if (tab==='generate') { clearTimeout(poll); showGenerate($('generate-view'),hash); return; }
  leaveGenerate();
  if (tab==='configure') { clearTimeout(poll); showConfigure($('configure-view'),hash); return; }
  if (tab==='review') { clearTimeout(poll); showReview($('review-view'),hash); return; }
  const id=hash.split('/')[1];
  if (!compareLoaded) { compareLoaded=true; bootCompare(id); }
  else if (id) openExperiment(id,id===state.detail?.experiment_id);
  else if (state.detail) openExperiment(state.detail.experiment_id,true);
  else bootCompare();
}
async function boot() {
  try {
    const session=await globalApi('/auth/session');
    if (session.auth) { signedIn(); $('sign-out').hidden=false; }
  } catch(error) { $('health').textContent='Sign-in unavailable'; $('generate-view').replaceChildren(el('p',{class:'notice error'},error.message)); return; }
  globalApi('/health').then(h=>{$('health').textContent=`Database ${h.database}`;}).catch(()=>{$('health').textContent='Server unavailable';});
  window.addEventListener('hashchange',route);
  route();
}
async function bootCompare(id) {
  const ticket=state.request;
  $('content').replaceChildren(skeleton('Loading comparisons…'));
  const results=await Promise.allSettled([api('/config'),listExperiments()]);
  if(ticket!==state.request) return;
  const [config,experiments]=results;
  if(config.status==='fulfilled') state.config=config.value;
  if(experiments.status==='rejected') notice(`Cannot read experiments. ${experiments.reason.message} Use Refresh to try again.`,true);
  else if(id) await openExperiment(id);
  else if(state.experiments.length) await openExperiment(state.experiments[0].experiment_id);
  else $('content').replaceChildren(el('p',{class:'empty-note'},'No comparisons yet. Import the recorded run, or create a new comparison.'));
}
mountDial($('detail-dial'));
boot();
window.addEventListener('pagehide',()=>{clearTimeout(poll);++state.request;requests.cancel();leaveGenerate();leaveConfigure();leaveReview();});
$('sign-out').addEventListener('click',signOut);
// The skip link focuses the visible section; a hash link would switch tabs.
document.querySelector('.skip').addEventListener('click',event=>{event.preventDefault();(['generate-view','runs-view','review-view','configure-view'].map($).find(v=>!v.hidden) || $('workspace')).focus();});
