import {el, skeleton} from './dom.mjs';
import {requestScope} from './api.mjs';
import {mountSurveyPlayer} from './survey-player.mjs';
// Agents: the Strands survey agents deployed on Bedrock AgentCore Runtime.
// Try one insight on one or more models side by side, and read saved model sweeps.
const requests=requestScope();
const api=(path,options)=>requests.api(path,options);
const view={root:null, request:0, options:null, insights:[], runs:[], form:null, cards:[]};
const STATUS={accepted:'Accepted', skipped:'Skipped by rules', rejected:'Rejected by reviewer', failed:'Failed'};
const short=id=>String(id || '').replace(/^(global|us)\./,'').replace(/^(anthropic|openai|amazon|google|meta|qwen)\./,'').replace(/-\d{8}-v\d:\d$/,'');
const pct=v=>v===null || v===undefined ? '—' : `${Math.round(v*100)}%`;
const num=(v,d=0)=>v===null || v===undefined ? '—' : Number(v).toFixed(d);
const badge=(text,warn=false)=>el('span',{class:`badge ${warn?'warning':''}`},text);
const LANGUAGE={'hi-IN':'Hindi', 'en-IN':'English'};
const cap=s=>s ? s[0].toUpperCase()+s.slice(1) : s;
const lang=i=>LANGUAGE[i.language] || i.language;
const cropName=i=>cap(i.crop) || 'No crop';

export function leaveAgents() { ++view.request; requests.cancel(); }

export async function showAgents(root) {
  view.root=root;
  requests.renew();
  const ticket=++view.request;
  if (!root.childElementCount) root.replaceChildren(skeleton('Loading the survey agents…'));
  const [options,insights,runs]=await Promise.allSettled([api('/agents/options'),api('/agents/insights'),api('/agents/comparison')]);
  if (ticket!==view.request) return;
  view.options=options.status==='fulfilled'?options.value:{configured:false, error:options.reason?.message};
  view.insights=insights.status==='fulfilled'?insights.value.insights:[];
  view.runs=runs.status==='fulfilled'?runs.value.runs:[];
  render();
}

function render() {
  const o=view.options;
  view.root.replaceChildren(
    el('div',{class:'heading'},
      el('div',{},el('h1',{},'Survey agents'),
        el('p',{class:'muted'},'Strands generator and verifier agents running on Amazon Bedrock AgentCore. Pick an insight and one or more models to compare their surveys side by side. Nothing is sent to farmers.')),
      el('div',{class:'status-group'},badge(o.configured?'AgentCore connected':'AgentCore not configured',!o.configured))),
    o.configured?tryPanel():el('p',{class:'notice error'},o.error || 'Set MEL_AGENT_RUNTIME_ARN to the deployed agent ARN, then restart the console.'),
    el('div',{class:'ag-results',id:'ag-results'},...view.cards.map(c=>c.node)),
    comparisonPanel());
}

function insightLabel(i) {
  return `${i.golden_id || i.insight_id} · ${cropName(i)} · ${lang(i)} · ${i.query_summary || ''}`;
}
// The picked insight as a card: the farmer's question, then what the golden set expects.
function insightCard(i) {
  if (!i) return [];
  const exp=i.expected;
  return [el('p',{class:'ag-insight-q'},i.query_summary || 'No farmer question recorded.'),
    el('div',{class:'ag-insight-meta'},
      el('span',{class:'ag-chip'},i.golden_id || i.insight_id),
      el('span',{class:'ag-chip'},cropName(i)),
      el('span',{class:'ag-chip'},lang(i)),
      i.resolution_type ? el('span',{class:'ag-chip'},cap(i.resolution_type.replaceAll('_',' '))) : null,
      exp ? el('span',{class:`ag-chip ${exp.survey?'ag-chip-survey':'ag-chip-none'}`},`Golden: ${exp.survey?'survey':'no survey'} · ${exp.rule}`) : null)];
}

function tryPanel() {
  const o=view.options, d=o.defaults;
  const f=view.form || (view.form={insight:view.insights.find(i=>i.expected?.survey)?.insight_id, generators:[d.generator], verifier:d.verifier, pipeline:d.pipeline, retries:d.max_retries, surveysOnly:true});
  const list=view.insights.filter(i=>!f.surveysOnly || i.expected?.survey);
  // Grouped by crop; each option leads with its golden ID, then the language and the farmer's question.
  const crops=[...new Set(list.map(cropName))].sort((a,b)=>a==='No crop'?1:b==='No crop'?-1:a.localeCompare(b));
  const insight=el('select',{'aria-label':'Insight',onChange:e=>{f.insight=e.target.value;summary.replaceChildren(...insightSummary());}},
    crops.map(crop=>el('optgroup',{label:crop},list.filter(i=>cropName(i)===crop).map(i=>
      el('option',{value:i.insight_id,selected:i.insight_id===f.insight},`${i.golden_id || i.insight_id} · ${lang(i)} · ${i.query_summary || ''}`)))));
  const summary=el('div',{class:'ag-summary'});
  const insightSummary=()=>insightCard(view.insights.find(i=>i.insight_id===f.insight));
  summary.append(...insightSummary());
  const surveysOnly=el('label',{class:'ag-check'},el('input',{type:'checkbox',checked:f.surveysOnly,onChange:e=>{f.surveysOnly=e.target.checked;render();}}),'Only insights the golden set expects a survey for');
  const generators=el('fieldset',{class:'ag-models'},el('legend',{},'Generator models (pick up to 5 to compare)'),
    o.generators.map(g=>el('label',{class:'ag-check'},el('input',{type:'checkbox',value:g,checked:f.generators.includes(g),onChange:e=>{
      f.generators=e.target.checked?[...new Set([...f.generators,g])]:f.generators.filter(x=>x!==g);
      run.disabled=!f.generators.length;
    }}),short(g))));
  const select=(label,values,value,change,fmt=x=>x)=>el('label',{},label,el('select',{onChange:e=>change(e.target.value)},values.map(v=>el('option',{value:v,selected:String(v)===String(value)},fmt(v)))));
  const run=el('button',{type:'submit',class:'primary',disabled:!f.generators.length},'Run on AgentCore');
  const form=el('form',{class:'ag-form',onSubmit:e=>{e.preventDefault();start();}},
    el('label',{},'Insight',insight),summary,surveysOnly,generators,
    el('div',{class:'form-grid'},
      select('Verifier model',o.verifiers,f.verifier,v=>f.verifier=v,short),
      select('Pipeline',o.pipelines,f.pipeline,v=>f.pipeline=v,v=>v==='direct'?'Direct: write in the farmer’s language':'Translate: write English, then translate'),
      select('Revise rounds after the first',[0,1,2],f.retries,v=>f.retries=Number(v))),
    el('div',{class:'launch'},el('p',{class:'small muted'},'Each model runs as its own AgentCore request, in parallel. A survey usually takes 15–60 seconds; the first one after a quiet spell can take longer.'),run));
  return el('section',{class:'gen-panel ag-panel'},el('h2',{},'Try an insight'),form);
}

// Runs keep going when the user switches tabs; the cards fill in when they come back.
// Starting a new run cancels the previous one.
const runs=requestScope();
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
// The server starts the run in the background (a survey outlasts the hosted API's 29 s limit);
// poll until it is done. Gives up after 5 minutes.
export async function runAgent(body, scope=runs) {
  const {run_id}=await scope.api('/agents/runs',{method:'POST',body:JSON.stringify(body)});
  const deadline=Date.now()+300000;
  while (Date.now()<deadline) {
    await sleep(3000);
    scope.signal.throwIfAborted();
    const record=await scope.api(`/agents/runs/${run_id}`);
    if (record.status==='done') return record;
    if (record.status==='failed') throw new Error(record.error || 'The run failed.');
  }
  throw new Error('No answer after 5 minutes. Try again.');
}
function start() {
  const f=view.form;
  runs.renew();
  const insight=view.insights.find(i=>i.insight_id===f.insight);
  const caption=el('p',{class:'small muted ag-wide'},insight ? insightLabel(insight) : f.insight);
  view.cards=[{node:caption},...f.generators.map(generator=>{
    const card={generator,node:el('section',{class:'survey-panel ag-card'},el('h2',{},short(generator)),el('div',{class:'gen-waiting'},el('div',{class:'spinner','aria-hidden':true}),el('p',{},'Running on AgentCore…')))};
    runAgent({insight_id:f.insight,generator,verifier:f.verifier,pipeline:f.pipeline,max_retries:f.retries})
      .then(data=>card.node.replaceChildren(...resultCard(generator,data)))
      .catch(error=>{ if(error.name!=='AbortError') card.node.replaceChildren(el('h2',{},short(generator)),el('p',{class:'notice error'},error.message)); });
    return card;
  })];
  const results=document.getElementById('ag-results');
  results.replaceChildren(...view.cards.map(c=>c.node));
  results.scrollIntoView({behavior:'smooth',block:'start'});
}

// English for the phone's language switch. Translate pipeline: the English survey the translator
// started from. Direct pipeline: the verifier's back-translation of the final survey, laid over
// the farmer's items so ids and answer values stay the same.
export function englishVersion(r) {
  if (!r.items?.length || String(r.language).startsWith('en')) return null;
  if (r.items_en?.length) return {language:'en-IN', items:r.items_en};
  const back=new Map(((r.reviews || []).at(-1)?.back_translation || []).map(b=>[b.item_id,b]));
  if (!back.size) return null;
  return {language:'en-IN', items:r.items.map(item=>{
    const b=back.get(item.item_id);
    if (!b) return item;
    const labels=b.labels_en || [];
    return {...item, prompt:b.prompt_en || item.prompt,
      options:item.options?.map((o,i)=>({...o, label:(labels.length===item.options.length && labels[i]) || o.label}))};
  })};
}

function resultCard(generator,{result:r,expected}) {
  const tokens=Object.values(r.stages || {}).reduce((s,x)=>s+(x.input_tokens||0)+(x.output_tokens||0),0);
  const decisionOk=expected ? (r.decision_survey===expected.survey) : null;
  const out=[el('h2',{},short(generator)),
    el('div',{class:'ag-badges'},badge(STATUS[r.status] || r.status,r.status!=='accepted' && r.status!=='skipped'),
      r.relevance!==null && r.relevance!==undefined ? badge(`relevance ${num(r.relevance,2)}`) : null,
      r.attempts ? badge(`${r.attempts} attempt${r.attempts>1?'s':''}`) : null,
      badge(`${num(r.elapsed_seconds,1)} s`), tokens ? badge(`${tokens.toLocaleString()} tokens`) : null),
    el('p',{class:'small'},`Rule ${r.decision_rule} · ${r.decision_survey?'survey':'no survey'}`,
      expected ? el('span',{class:decisionOk?'':'missing'},` · golden expects ${expected.rule} (${decisionOk?'match':'mismatch'})`) : null),
    el('p',{class:'small muted'},`Verifier ${short(r.models?.verifier)}${r.models?.translator?` · translator ${short(r.models.translator)}`:''}`)];
  if (r.error) out.push(el('p',{class:'notice error'},r.error));
  if (r.items?.length) {
    const host=el('div',{class:'survey-player'});
    out.push(el('figure',{class:'phone-stage'},host));
    mountSurveyPlayer(host,{survey:{language:r.language,items:r.items},translation:englishVersion(r),session:{phase:'questions',question:0,answers:{}},photoUrl:'/static/assets/mustard-field.webp'});
  } else if (r.status!=='skipped') out.push(el('p',{class:'muted'},'No survey questions came back.'));
  for (const issue of r.code_errors || []) out.push(el('p',{class:'missing small'},`Code check: ${issue}`));
  const last=(r.reviews || []).at(-1);
  for (const issue of last?.issues || []) out.push(el('p',{class:'finding small'},`${issue.severity || 'blocking'} · ${issue.item_id || ''} ${issue.check?`[${issue.check}]`:''} ${issue.problem || ''}`));
  if (r.items_en?.length) out.push(el('details',{},el('summary',{},'English draft'),el('pre',{},JSON.stringify(r.items_en,null,1))));
  out.push(el('details',{},el('summary',{},'Full agent result'),el('pre',{},JSON.stringify(r,null,1))));
  return out;
}

function comparisonPanel() {
  const section=el('section',{class:'comparison'},el('h2',{},'Model sweeps'),
    el('p',{class:'muted'},'Saved runs of agents/sweep.py over golden insights. Accept rate counts surveys the verifier passed out of those generated. Compare rows on the same golden file and prompt version.'));
  if (!view.runs.length) { section.append(el('p',{class:'empty-note'},'No sweep results found. Run agents/sweep.py to add some.')); return section; }
  const cols=[['Model',r=>short(r.model_id)],['Pipeline',r=>r.tag?`${r.pipeline} · ${r.tag}`:r.pipeline],['Golden',r=>r.golden || (r.file.startsWith('dev/')?'golden_dev.jsonl':'—')],['Prompt',r=>r.prompt_sha || '—'],
    ['Cases',r=>r.cases],['Accepted',r=>r.status_counts?.accepted ?? 0],['Rejected',r=>r.status_counts?.rejected ?? 0],['Failed',r=>r.status_counts?.failed ?? 0],
    ['Accept rate',r=>pct(r.accept_rate)],['Decision accuracy',r=>pct(r.decision_accuracy)],['Relevance',r=>num(r.relevance_mean,3)],['Attempts',r=>num(r.attempts_mean,2)],
    ['Tokens / survey',r=>r.tokens_per_survey?.toLocaleString() ?? '—'],['Seconds / survey',r=>num(r.seconds_per_survey,1)],['Run',r=>(r.created_at || '').slice(0,16).replace('T',' ')]];
  section.append(el('div',{class:'table-scroll',tabindex:0,'aria-label':'Model sweeps'},el('table',{},
    el('thead',{},el('tr',{},cols.map(([l])=>el('th',{scope:'col'},l)))),
    el('tbody',{},view.runs.map(r=>el('tr',{},cols.map(([,get],i)=>el(i===0?'th':'td',i===0?{scope:'row',title:r.file}:{},String(get(r) ?? '—')))))))));
  return section;
}
