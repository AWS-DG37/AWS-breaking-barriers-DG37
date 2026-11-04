import {parseJsonLines, displayValue, questionAssessments, eventsForStage, selectedModel} from './domain.mjs';

const $ = id => document.getElementById(id);
const state = {config:null, models:[], experiments:[], detail:null, run:0, result:0, stage:null, event:0, question:0, request:0};
let poll;
function el(tag, attrs={}, ...children) {
  const node=document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(),value);
    else if (key === 'class') node.className=value;
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key,String(value));
  }
  node.append(...children.flat().filter(c=>c !== null && c !== undefined).map(c=>c instanceof Node ? c : document.createTextNode(String(c))));
  return node;
}
const text=(value)=>typeof value === 'object' ? JSON.stringify(value,null,2) : displayValue(value);
const raw=value=>el('pre',{},text(value));
const badge=value=>el('span',{class:`badge ${['failed','rejected','completed_with_errors'].includes(value)?'warning':''}`},value?.replaceAll('_',' ') || 'Unknown');
function notice(message, error=false) { $('notice').hidden=!message; $('notice').className=error?'notice error':'notice'; $('notice').textContent=message; }
async function api(path, options={}) {
  const response=await fetch(`/api${path}`,{...options,headers:{'Content-Type':'application/json',...options.headers}});
  const data=await response.json().catch(()=>({detail:`Server returned ${response.status}.`}));
  if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail || data));
  return data;
}
const disclosure=(label,value,open=false)=>el('details',open?{open:true}:{},el('summary',{},label),raw(value));
function picker(label,options,value,change) {
  return el('label',{},label,el('select',{'aria-label':label,onChange:event=>change(event.target.value)},options.map(([id,title])=>el('option',{value:id,selected:String(id)===String(value)},title))));
}
async function listExperiments() {
  const data=await api('/experiments'); state.experiments=data.experiments;
  $('experiment-list').replaceChildren(...(state.experiments.length ? state.experiments.map(item=>el('button',{
    class:'experiment', 'aria-pressed':state.detail?.experiment_id===item.experiment_id,
    onClick:()=>openExperiment(item.experiment_id),
  },el('strong',{},item.name),el('span',{class:'small muted'},`${item.source==='recorded'?'Recorded':'Live'} · ${item.insight_count} insights · ${item.candidate_count} candidates`),badge(item.status))) : [el('p',{class:'empty-note'},'No experiments saved yet. Import a recorded run or create a bounded live experiment.')]));
}
async function openExperiment(id, refresh=false, reference=null) {
  const ticket=++state.request;
  clearTimeout(poll);
  if(!refresh) { $('composer').hidden=true; $('content').hidden=false; $('content').replaceChildren(el('p',{class:'empty-note'},'Loading saved evidence…')); }
  try {
    reference=reference || (state.detail?.experiment_id===id ? state.detail.comparison?.reference : null);
    const query=reference?`?evaluation_reference=${encodeURIComponent(reference)}`:'';
    const detail=await api(`/experiments/${encodeURIComponent(id)}${query}`);
    if(ticket!==state.request) return;
    state.detail=detail;
    if(!refresh) Object.assign(state,{run:0,result:0,stage:null,event:0,question:0});
    renderExperiment(); await listExperiments();
    if(['queued','running'].includes(detail.status)) poll=setTimeout(()=>openExperiment(id,true),3000);
  } catch(error) { if(ticket===state.request) { notice(`Cannot load experiment. ${error.message} Use Refresh to try again.`,true); $('content').replaceChildren(el('h1',{},'Experiment unavailable')); } }
}
function selected() {
  const run=state.detail.runs[state.run];
  const result=run?.results[state.result];
  return {run,result,outcome:result?.outcome};
}
function renderExperiment() {
  const d=state.detail, {run,result,outcome}=selected();
  const heading=el('div',{class:'heading'},el('div',{},el('h1',{},d.name),el('p',{class:'muted'},d.source==='recorded'?'Recorded experiment. Saved results only.':'Live experiment. Selected native models and API configurations.')),el('div',{class:'status-group'},badge(d.source),badge(d.status)));
  const controls=el('div',{class:'selection'},picker('Candidate',d.runs.map((r,i)=>[i,`${r.label} · ${r.api || 'API not recorded'}`]),state.run,value=>{state.run=Number(value);state.result=0;state.stage=null;state.question=0;renderExperiment();}),picker('Insight',(run?.results || []).map((r,i)=>[i,`${r.insight_id} · ${r.outcome.status}`]),state.result,value=>{state.result=Number(value);state.stage=null;state.question=0;renderExperiment();}));
  const sections=[heading,controls];
  if(outcome) {
    sections.push(el('div',{class:'outcome-line'},badge(outcome.status),el('span',{},outcome.reason || 'No additional outcome reason recorded.'),el('span',{class:'small muted'},run.model_id)));
    sections.push(renderJourney(d.flow || state.config?.flow,outcome));
    sections.push(el('div',{class:'inspect-grid'},renderInspector(result,outcome),renderSurvey(outcome)));
  } else sections.push(el('div',{class:'empty-note'},['queued','running'].includes(d.status)?'Waiting for persisted results. This view refreshes while execution is active.':'No result records are available for this candidate.'));
  sections.push(renderComparison(d));
  sections.push(disclosure('Experiment inputs and configuration',d.spec),disclosure('Run manifest',run?.manifest ?? 'Not recorded'));
  $('content').replaceChildren(...sections);
}
function renderJourney(flow,outcome) {
  const events=outcome.events || [];
  const nodes=flow?.nodes || [];
  if(!state.stage) state.stage=events[0]?.stage || nodes[0]?.id || 'input';
  const extra=[...new Set(events.map(e=>e.stage))].filter(stage=>!nodes.some(n=>n.id===stage)).map(id=>({id,label:id.replaceAll('_',' ')}));
  const diagram=el('div',{class:'journey',role:'group','aria-label':'Pipeline stages'});
  for(const node of [...nodes,...extra]) {
    const count=eventsForStage(events,node.id).length;
    diagram.append(el('button',{class:'stage','aria-pressed':node.id===state.stage,onClick:()=>{state.stage=node.id;state.event=Math.max(0,eventsForStage(events,node.id).length-1);renderExperiment();}},el('strong',{},node.label),el('span',{class:'small'},count?`${count} recorded event${count===1?'':'s'}`:'No recorded event')));
    const edges=(flow?.edges || []).filter(edge=>edge.from===node.id);
    if(edges.length) diagram.append(el('div',{class:'edges','aria-label':`Connections from ${node.label}`},edges.map(edge=>el('span',{},`${edge.label?edge.label+' → ': '→ '}${flow.nodes.find(n=>n.id===edge.to)?.label || edge.to}`))));
  }
  return el('section',{class:'journey-section'},el('div',{class:'section-heading'},el('h2',{},'Insight journey'),el('span',{class:'small muted'},'Select a stage to inspect saved evidence')),diagram,!events.length?el('p',{class:'missing'},'Intermediate history not recorded. The final outcome remains available below.'):null);
}
function renderInspector(result,outcome) {
  const events=eventsForStage(outcome.events || [],state.stage);
  const event=events[state.event] || events[0];
  const panel=el('section',{class:'inspector'},el('h2',{},'Stage evidence'));
  if(!event) panel.append(el('p',{class:'muted'},'No event was recorded for this stage. The diagram describes the pipeline, not proof that every stage ran.'));
  else {
    panel.append(picker('Recorded event',events.map((e,i)=>[i,`#${e.sequence} · ${e.source} · attempt ${e.attempt}`]),state.event,value=>{state.event=Number(value);renderExperiment();}),el('p',{class:'small muted'},`${event.stage} · ${event.source} evidence · ${event.created_at || 'Time not recorded'}`));
    const data=event.data;
    if('eligible' in data) panel.append(el('h3',{},data.eligible?'Eligible for a survey':'Survey not required'),el('p',{},data.reason || 'The insight passed the configured eligibility rules.'));
    if('valid' in data) panel.append(el('h3',{},`${data.target} schema ${data.valid?'passed':'failed'}`),...(data.errors || []).map(issue=>el('p',{class:'missing'},text(issue))));
    if(data.send_window) panel.append(el('h3',{},'Send window'),el('p',{},`${data.send_window.not_before} → ${data.send_window.not_after}`));
    if(data.survey) panel.append(el('h3',{},`${data.survey.items?.length || 0} questions ${data.kind==='response'?'generated':'submitted for review'}`),el('p',{},`Language · ${data.survey.language}`));
    if(data.review) panel.append(el('h3',{},'Reviewer findings'),el('p',{},`Survey relevance · ${data.review.relevance_score}`),...(data.review.issues || []).map(issue=>el('p',{class:'finding'},issue)));
    if(data.context) panel.append(el('h3',{},'Context and provenance'),el('p',{},`${data.context.profile?'Farmer profile matched':'No farmer profile matched'} · ${(data.context.crop_calendar || []).length} calendar rows`),...(data.context.conflicts || []).map(issue=>el('p',{class:'missing'},issue)));
    if(data.status) panel.append(el('h3',{},`Ruling · ${data.status}`),el('p',{},data.reason || 'The saved result satisfied the configured acceptance gates.'));
    if(data.error_type) panel.append(el('h3',{},'Execution failed'),el('p',{class:'missing'},data.error_type));
    panel.append(disclosure('Full recorded event',event.data));
  }
  panel.append(disclosure('Original insight',result.input),disclosure('Final outcome',outcome));
  if(outcome.review) panel.append(el('h3',{},'Survey-wide model review'),el('p',{class:'small muted'},'Model judgment across the survey. This is not an independent question-quality score.'),raw(outcome.review));
  return panel;
}
function renderSurvey(outcome) {
  const survey=outcome.survey || outcome.candidate_survey;
  const panel=el('section',{class:'survey-panel'},el('h2',{},outcome.candidate_survey && !outcome.survey?'Rejected draft':'Question preview'));
  if(!survey?.items?.length) { panel.append(el('p',{class:'muted'},'No survey questions recorded for this outcome.')); return panel; }
  panel.append(el('p',{class:'small muted'},'Farmer-facing text. Select a question for its measurement evidence. Answers are shown for inspection only.'));
  const item=survey.items[state.question] || survey.items[0];
  panel.append(el('div',{class:'question-tabs',role:'group','aria-label':'Questions'},survey.items.map((q,i)=>el('button',{'aria-pressed':q===item,onClick:()=>{state.question=i;renderExperiment();}},`Question ${i+1}`))));
  panel.append(el('p',{class:'small muted question-record'},`${item.item_id} · ${item.type}`));
  const language=survey.language || undefined;
  const preview=el('div',{class:'question',lang:language},el('div',{class:'survey-progress'},el('span',{},language?.split('-')[0]==='hi'?'आपकी राय':'Your feedback'),el('span',{},`${survey.items.indexOf(item)+1} / ${survey.items.length}`)),el('h3',{},item.prompt));
  if(item.options) preview.append(el('ul',{class:'answers',lang:language},item.options.map(option=>el('li',{},el('span',{'aria-hidden':true,class:'answer-marker'}),option.label))));
  else preview.append(el('p',{class:'scale'},`${displayValue(item.scale_min)} to ${displayValue(item.scale_max)}`));
  const phone=el('div',{class:'survey-phone',role:'region','aria-label':'FarmerChat survey preview'},
    el('div',{class:'phone-screen'},
      el('div',{class:'phone-status','aria-hidden':true},el('span',{},'9:41'),el('span',{class:'phone-camera'}),el('span',{class:'phone-reception'},el('i'),el('i'),el('i'),el('i'),el('span',{class:'phone-battery'}))),
      el('div',{class:'phone-brand'},el('strong',{},'FarmerChat'),el('span',{},'Digital Green')),
      el('img',{class:'phone-field',src:'/static/assets/mustard-field.webp',alt:'Mustard flowers in a field in Rajasthan, India',width:960,height:768}),
      preview,
      el('div',{class:'phone-home','aria-hidden':true})));
  panel.append(el('figure',{class:'phone-stage'},phone,el('figcaption',{},'Survey preview · answers are not submitted',el('span',{},'Illustrative field photo, unrelated to the selected case. ',el('a',{href:'https://unsplash.com/photos/a-person-standing-in-a-field-of-yellow-flowers-u5Jb5tTfSa4',target:'_blank',rel:'noreferrer'},'Abhi Verma / Unsplash')))));
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
  const columns=[['Candidate',r=>r.label],['Cases',r=>r.total],['Accepted',r=>r.accepted],['Skipped',r=>r.skipped],['Rejected',r=>r.rejected],['Failed',r=>r.failed],['Missing',r=>r.missing],['Decision agreement',agreement],['Input tokens',r=>r.usage_complete?r.input_tokens:null],['Output tokens',r=>r.usage_complete?r.output_tokens:null],['Survey relevance',r=>r.mean_relevance]];
  const header=el('thead',{},el('tr',{},columns.map(([label])=>el('th',{scope:'col'},label))));
  const body=el('tbody',{},c.rows.map(row=>el('tr',{},columns.map(([,get],i)=>el(i===0?'th':'td',i===0?{scope:'row'}:{},displayValue(get(row)))))));
  section.append(el('div',{class:'table-scroll',tabindex:0,'aria-label':'Candidate comparison'},el('table',{},header,body)),el('p',{class:'small muted'},'Survey relevance is a survey-wide model judgment. Missing scores and incomplete token usage are not zero. No overall quality score is inferred.'));
  for(const note of c.notes || []) section.append(el('p',{class:'small'},note));
  const {run,result}=selected();
  const diagnostic=c.cases?.find(item=>item.run_id===run?.run_id && item.insight_id===result?.insight_id);
  if(diagnostic) {
    const checks=diagnostic.deterministic_checks;
    section.append(el('h3',{},'Selected survey checks'),el('div',{class:'check-grid'},Object.entries(checks?.checks || {}).map(([key,value])=>el('span',{class:`check ${value?'pass':'fail'}`},`${value?'Pass':'Fail'} · ${key.replaceAll('_',' ')}`))),el('p',{class:'small muted'},checks?.assessment==='assessed'?checks.limitations:'Reference checks are not assessed for this result.'),disclosure('Case-level reference evidence',diagnostic));
  }
  section.append(disclosure('Analysis reference version',c.reference_snapshot));
  const other=(d.assessments || []).filter(a=>a.scope!=='question');
  section.append(disclosure(`Scoped survey and run assessments (${other.length})`,other.length?other:'No survey or run assessments recorded.'));
  return section;
}
function modelOptions() {
  const choices=[...state.models];
  for(const candidate of [...(state.config?.default_candidates || []),{model:state.config?.default_reviewer}]) {
    const m=candidate.model;
    if(m?.model_id && !choices.some(c=>c.model_id===m.model_id && c.api===m.api)) choices.push({...m,name:m.model_id,status:'discovered',reason:'Configured model. Invocation access not verified.'});
  }
  return choices;
}
function renderComposer() {
  clearTimeout(poll); ++state.request;
  $('content').hidden=true; $('composer').hidden=false;
  const config=state.config;
  if(!config) { $('composer').replaceChildren(el('h1',{},'Configuration unavailable'),el('p',{},'Refresh the page after the server configuration is available.')); return; }
  const choices=modelOptions();
  const selectModel=(name,defaultModel)=>el('select',{name,required:true,'aria-label':name==='reviewer'?'Reviewer model and native API':'Model and native API'},el('option',{value:''},'Choose a native model and API'),choices.map((m,i)=>el('option',{value:i,disabled:m.status==='unavailable',selected:m.model_id===defaultModel?.model_id && m.api===(defaultModel.api || 'responses')},`${m.name || m.model_id} · ${m.api} · ${m.status}${m.reason?' · '+m.reason:''}`)));
  const input=el('textarea',{name:'insights',rows:7,required:true,spellcheck:false,placeholder:'One normalized insight JSON object per line'});
  const profiles=el('textarea',{name:'profiles',rows:4,spellcheck:false,placeholder:'Optional profile JSONL. One source record per line.'});
  const candidates=el('div',{class:'candidate-fields'});
  function addCandidate(model) {
    const row=el('div',{class:'candidate-row'},el('label',{},'Candidate label',el('input',{name:'candidate_label',required:true,value:`Candidate ${candidates.children.length+1}`})),el('label',{},'Model and native API',selectModel('candidate_model',model)));
    row.append(el('button',{type:'button',class:'quiet',onClick:()=>{row.remove();add.disabled=candidates.children.length>=6;}},'Remove')); candidates.append(row);
    add.disabled=candidates.children.length>=6;
  }
  const add=el('button',{type:'button',class:'secondary',onClick:()=>addCandidate()},'Add candidate');
  for(const candidate of config.default_candidates || []) addCandidate(candidate.model);
  if(!candidates.children.length) addCandidate();
  const upload=el('input',{type:'file',accept:'.jsonl,.ndjson,.json,text/plain',onChange:async event=>{const file=event.target.files[0]; if(file) input.value=await file.text();}});
  const form=el('form',{onSubmit:async event=>{
    event.preventDefault();
    const submit=form.querySelector('[type=submit]');
    try {
      const data=new FormData(form), insights=parseJsonLines(input.value), profileRows=parseJsonLines(profiles.value);
      if(!insights.length) throw new Error('Add at least one normalized insight.');
      if(!candidates.children.length) throw new Error('Select at least one candidate.');
      const model=(index,defaults)=>selectedModel(choices[Number(index)],defaults);
      const spec={name:data.get('name').trim(),insights,profiles:profileRows,candidates:[...candidates.children].map(row=>({label:row.querySelector('input').value.trim(),model:model(row.querySelector('select').value,config.default_candidates[0]?.model)})),reviewer:model(data.get('reviewer'),config.default_reviewer),row_limit:Number(data.get('row_limit')),ruleset:data.get('ruleset'),evaluation_reference:data.get('reference')};
      submit.disabled=true; notice('Submitting bounded live experiment…');
      const result=await api('/experiments',{method:'POST',body:JSON.stringify(spec)});
      notice('Live experiment queued. Execution results will appear as they are saved.'); await openExperiment(result.experiment_id);
    } catch(error) { notice(`Experiment was not submitted. ${error.message}`,true); }
    finally {submit.disabled=!config.live_enabled;}
  }});
  form.append(
    el('div',{class:'form-grid'},
      el('label',{},'Experiment name',el('input',{name:'name',required:true,maxlength:200,placeholder:'Name the change you want to compare'})),
      el('label',{},'Maximum insight rows',el('input',{name:'row_limit',type:'number',min:1,max:50,value:3,required:true}))),
    el('label',{},'Normalized insight JSONL',input),
    el('label',{class:'file'},'Load a local JSONL file',upload),
    el('h2',{},'Generation candidates'),
    el('p',{class:'muted'},'Each candidate runs the same selected insights. Catalog discovery does not prove invocation access. No model or API is silently substituted.'),
    candidates,add,el('h2',{},'Shared review and evaluation'));
  const shared=el('div',{class:'form-grid'},
    el('label',{},'Reviewer model and native API',selectModel('reviewer',config.default_reviewer)),
    el('label',{},'Ruleset',el('select',{name:'ruleset'},config.rulesets.map(r=>el('option',{value:r.id},r.label)))),
    el('label',{},'Evaluation reference',el('select',{name:'reference'},el('option',{value:'rules'},'Configured rules'),el('option',{value:'golden'},'Golden reference'))));
  form.append(shared,
    el('p',{class:'small muted'},'Expected evaluation values are not generation inputs. The shared reviewer and rules stay fixed across candidates.'),
    el('details',{},el('summary',{},'Optional farmer profile context'),el('label',{},'Profile JSONL',profiles)),
    el('div',{class:'launch'},
      el('p',{},config.live_enabled?'This explicitly invokes the selected Bedrock models. Default run size is 3 insight rows. Experiment results stay outside the scheduler handoff queue.':'Live execution is disabled by the server. Recorded experiments remain available.'),
      el('button',{type:'submit',class:'primary',disabled:!config.live_enabled},'Run live experiment')));

  $('composer').replaceChildren(el('div',{class:'heading'},el('div',{},el('h1',{id:'form-title'},'New live experiment'),el('p',{class:'muted'},'Make a deliberate change. Keep the evidence.')),el('button',{class:'secondary',onClick:()=>{ $('composer').hidden=true; $('content').hidden=false; if(state.detail) openExperiment(state.detail.experiment_id); }},'Back to saved runs')),form);
}
$('new').addEventListener('click',renderComposer);
$('refresh').addEventListener('click',async()=>{try {await listExperiments();if(state.detail) await openExperiment(state.detail.experiment_id,true); notice('Saved experiments refreshed.');}catch(error){notice(error.message,true);}});
$('import').addEventListener('click',async()=>{const b=$('import');b.disabled=true;try {const result=await api('/import-recorded',{method:'POST'});notice('Recorded artifact imported. Intermediate events are shown only when they were saved.');await openExperiment(result.experiment_id);}catch(error){notice(`Recorded import unavailable. ${error.message}`,true);}finally{b.disabled=false;}});
async function boot() {
  const catalog=api('/models').then(models=>{state.models=models.models;if(models.error) notice(`Model catalog partially available. ${models.error}`,true);}).catch(error=>notice(`Model catalog unavailable. ${error.message} Recorded runs remain usable.`,true));
  const results=await Promise.allSettled([api('/health'),api('/config'),listExperiments()]);
  const [health,config,experiments]=results;
  $('health').textContent=health.status==='fulfilled'?`Database ${health.value.database}`:'Server unavailable';
  if(config.status==='fulfilled') state.config=config.value;
  if(experiments.status==='rejected') notice(`Cannot read experiments. ${experiments.reason.message} Use Refresh to try again.`,true);
  else if(state.experiments.length) await openExperiment((state.experiments.find(e=>e.source==='recorded') || state.experiments[0]).experiment_id);
  await catalog;
}
boot();
window.addEventListener('pagehide',()=>clearTimeout(poll));
