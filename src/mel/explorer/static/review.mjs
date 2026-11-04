import {budgetInput, capPanel} from './costs.mjs';
import {el, skeleton} from './dom.mjs';
import {api as globalApi, requestScope} from './api.mjs';
import {lineDiff} from './domain.mjs';
// Review: lists of generated surveys people flagged, each comment saying what was wrong
// and what it should have been, and improvement loops that revise a configuration
// against a list. Results anywhere in the console can be added with addToReviewList.
// Routes: #review, #review/<list id>, #review/improvement/<improvement id>.
const requests=requestScope();
const api=(path,options)=>requests.api(path,options);
const view={root:null, request:0, poll:null};
export function leaveReview() { ++view.request; clearTimeout(view.poll); requests.cancel(); }

const EFFORTS=['low','medium','high'];
const POLL_MS=3000;
const words=value=>String(value ?? '').replaceAll('_',' ');
const when=iso=>iso?new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'';
const message=(text,error=false)=>el('p',{class:error?'notice error':'notice',role:error?'alert':'status'},text);
const badge=status=>el('span',{class:`badge status-${status} ${status==='failed'?'warning':''}`},words(status));
const pct=score=>score===null || score===undefined?'—':`${Math.round(score*100)}%`;
const versionName=v=>v?(v.name || `Draft ${v.config_id.slice(0,8)}`):'Unknown version';
const plural=(n,word)=>`${n} ${word}${n===1?'':'s'}`;
const questionText=(survey,id)=>survey.items.find(q=>q.item_id===id)?.prompt || id;

export function showReview(root,hash) {
  view.root=root;
  leaveReview(); requests.renew();
  const [, first, second]=hash.replace(/^#/,'').split('/');
  return first==='improvement' && second?renderImprovement(second):renderList(first || null);
}

function layout(lists,selected,main) {
  const create=el('form',{class:'rv-new',onSubmit:async event=>{
    event.preventDefault();
    const name=event.target.elements.name.value.trim();
    if (!name) return;
    try { const list=await api('/review-lists',{method:'POST',body:JSON.stringify({name})}); location.hash=`#review/${list.list_id}`; }
    catch(error) { if (error.name!=='AbortError') create.append(message(error.message,true)); }
  }},el('label',{},'New list',el('input',{name:'name',maxlength:120,placeholder:'e.g. Vague outcome questions',required:true})),el('button',{type:'submit'},'Create list'));
  const nav=el('nav',{class:'cfg-list','aria-label':'Review lists'},el('h2',{},'Review lists'),
    lists.map(l=>el('a',{class:'cfg-item',href:`#review/${l.list_id}`,'aria-current':String(l.list_id===selected)},
      el('strong',{},l.name),el('span',{class:'small muted'},`${plural(l.item_count,'comment')} · ${plural(l.insight_count,'insight')}`))),
    create);
  view.root.replaceChildren(el('div',{class:'cfg'},nav,main));
}

async function renderList(id) {
  const ticket=++view.request;
  view.root.replaceChildren(skeleton('Loading review lists…'));
  try {
    const {lists}=await api('/review-lists');
    if (ticket!==view.request) return;
    const key=id || lists[0]?.list_id;
    const main=el('div',{class:'cfg-main'});
    layout(lists,key,main);
    if (!key) {
      main.append(el('div',{class:'gen-empty'},el('h1',{},'Collect questions to improve'),
        el('p',{},'Create a list, then open any result in Runs or Compare and use Add to review list. Say what was wrong with the survey or a question and what it should have been. A stronger model then revises a configuration against the list, and the console tests the revision.')));
      return;
    }
    const [list,configs,settings]=await Promise.all([api(`/review-lists/${key}`),api('/configs'),api('/config')]);
    if (ticket!==view.request) return;
    main.append(el('div',{class:'heading'},el('div',{},el('h1',{},list.name),
      el('p',{class:'muted small'},`${plural(list.item_count,'comment')} on ${plural(list.insight_count,'insight')} · created ${when(list.created_at)}`),
      list.description?el('p',{},list.description):null)),
      ...[comments(list),improveForm(list,configs,settings),history(list)].filter(Boolean));
  } catch(error) {
    if (ticket===view.request) view.root.replaceChildren(message(`Cannot load review lists. ${error.message}`,true));
  }
}

function comments(list) {
  const box=el('section',{class:'cfg-step'},el('h2',{},'Comments'));
  if (!list.items.length) { box.append(el('p',{class:'muted'},'No comments yet. Open a result in Runs or Compare and use Add to review list.')); return box; }
  const byInsight=Map.groupBy(list.items,item=>item.insight_id);
  for (const [insightId,items] of byInsight) {
    const insight=items[0].insight;
    box.append(el('article',{class:'rv-insight'},
      el('h3',{},[insightId,insight.asset?.name,words(insight.concern)].filter(Boolean).join(' · ')),
      el('p',{class:'small muted'},insight.advice_summary || ''),
      items.map(item=>commentCard(item))));
  }
  return box;
}

function commentCard(item) {
  const card=el('div',{class:'rv-item'});
  const show=()=>card.replaceChildren(
    el('ol',{class:'rv-survey'},item.survey.items.map(q=>el('li',{class:q.item_id===item.question_id?'rv-flag':''},q.prompt,el('span',{class:'small muted'},` · ${q.options?.map(o=>o.label).join(' / ') || `${q.scale_min}–${q.scale_max}`}`)))),
    el('p',{},el('strong',{},item.question_id?`On question “${questionText(item.survey,item.question_id)}”: `:'On the whole survey: '),item.comment),
    item.should_be?el('p',{},el('strong',{},'Should have been: '),item.should_be):null,
    el('p',{class:'small muted rv-meta'},`${words(item.status)} in `,item.run_id?el('a',{href:`#runs/run/${item.run_id}/${encodeURIComponent(item.insight_id)}`},`run ${item.run_id.slice(0,8)}`):'a deleted run',` · ${when(item.updated_at)}`),
    el('div',{class:'cfg-actions'},el('button',{type:'button',class:'quiet',onClick:edit},'Edit'),el('button',{type:'button',class:'quiet',onClick:remove},'Remove')));
  async function remove() {
    if (!confirm('Remove this comment from the list?')) return;
    try { await api(`/review-items/${item.item_id}`,{method:'DELETE'}); showReview(view.root,location.hash); }
    catch(error) { if (error.name!=='AbortError') card.append(message(error.message,true)); }
  }
  function edit() {
    const form=el('form',{onSubmit:async event=>{
      event.preventDefault();
      const f=event.target.elements;
      try { Object.assign(item,await api(`/review-items/${item.item_id}`,{method:'PUT',body:JSON.stringify({comment:f.comment.value,should_be:f.should_be.value || null})})); show(); }
      catch(error) { if (error.name!=='AbortError') form.append(message(error.message,true)); }
    }},commentFields(item),el('div',{class:'cfg-actions'},el('button',{type:'submit',class:'primary'},'Save'),el('button',{type:'button',onClick:show},'Cancel')));
    card.replaceChildren(form);
  }
  show();
  return card;
}

const commentFields=(item={})=>[
  el('label',{},'What is wrong',el('textarea',{name:'comment',rows:3,maxlength:4000,required:true},item.comment || '')),
  el('label',{},'What it should have been (optional)',el('textarea',{name:'should_be',rows:2,maxlength:4000},item.should_be || ''))];

function improveForm(list,{configs,active_config_id:active},settings) {
  const box=el('section',{class:'cfg-step'},el('div',{class:'cfg-step-head'},el('h2',{},'Improve a configuration'),
    el('span',{class:'small muted'},'A stronger model edits prompts and settings to address these comments. Each candidate runs on the commented insights and is judged against the comments; better ones are kept.')));
  if (!list.items.length) { box.append(el('p',{class:'muted'},'Add comments before improving.')); return box; }
  if (!settings.live_enabled) { box.append(message('Live execution is disabled on this console.')); return box; }
  const runnable=configs.filter(c=>c.status!=='retired');
  const versionSelect=(name,label)=>el('label',{},label,el('select',{name,required:true},runnable.map(c=>el('option',{value:c.config_id,selected:c.config_id===active},`${versionName(c)}${c.config_id===active?' (active)':c.status==='draft'?' (draft)':''}`))));
  const models=settings.allowed_models || [];
  const improver=models.length
    ?el('label',{},'Improver and judge model',el('select',{name:'improver',required:true},models.map((m,i)=>el('option',{value:i},`${m.model_id} · ${m.api==='responses'?'Mantle':'Converse'}`))))
    :el('div',{class:'gen-row'},el('label',{},'Improver and judge model',el('input',{name:'model_id',required:true,placeholder:'Bedrock model ID'})),
      el('label',{},'API',el('select',{name:'api'},el('option',{value:'responses'},'Mantle (Responses)'),el('option',{value:'converse'},'Converse'))));
  const insights=list.insight_count;
  const plan=el('p',{class:'small muted'});
  const form=el('form',{class:'gen-form',onInput:describe,onSubmit:start},
    el('div',{class:'gen-row'},versionSelect('base_config_id','Start from'),versionSelect('compare_config_id','Compare the result against')),
    improver,
    el('div',{class:'gen-row'},
      el('label',{},'Reasoning effort',el('select',{name:'reasoning_effort'},EFFORTS.map(e=>el('option',{value:e,selected:e==='high'},e)))),
      el('label',{},'Output token budget',el('input',{name:'max_tokens',type:'number',min:1000,step:1000,value:16000,required:true}))),
    el('div',{class:'gen-row'},el('label',{},'Rounds',el('input',{name:'rounds',type:'number',min:1,max:5,value:2,required:true})),budgetInput('Budget per evaluation run (USD)')),
    plan,capPanel(),el('button',{type:'submit',class:'primary'},'Start improving'));
  function describe() {
    const f=form.elements;
    const evaluations=1+(f.base_config_id.value!==f.compare_config_id.value)+Number(f.rounds.value || 0);
    plan.textContent=`${plural(evaluations,'evaluation')} of ${plural(insights,'insight')}: each runs generation and review, then one judge call per insight. One improver call per round. The improver and judge share the model chosen here; their calls are charged to the run they read, under its budget and the console spend cap.`;
  }
  async function start(event) {
    event.preventDefault();
    const f=form.elements, button=form.querySelector('button[type=submit]');
    const model=models.length?models[Number(f.improver.value)]:{model_id:f.model_id.value.trim(),api:f.api.value};
    button.disabled=true;
    try {
      const body={base_config_id:f.base_config_id.value,compare_config_id:f.compare_config_id.value,rounds:Number(f.rounds.value),budget_usd:f.budget_usd.value,
        improver:{...model,reasoning_effort:f.reasoning_effort.value,max_tokens:Number(f.max_tokens.value)}};
      const result=await api(`/review-lists/${list.list_id}/improvements`,{method:'POST',body:JSON.stringify(body)});
      location.hash=`#review/improvement/${result.improvement_id}`;
    } catch(error) {
      button.disabled=false;
      if (error.name!=='AbortError') form.append(message(error.message,true));
    }
  }
  describe();
  box.append(form);
  return box;
}

function history(list) {
  if (!list.improvements.length) return null;
  return el('section',{class:'cfg-tests','data-level':2},el('h2',{},'Improvement runs'),list.improvements.map(i=>el('a',{class:'gen-run-link',href:`#review/improvement/${i.improvement_id}`},
    el('span',{class:'gen-run-title'},el('strong',{},`${plural(i.spec.rounds,'round')} · ${i.spec.improver.model_id}`),badge(i.status)),
    el('span',{class:'small muted'},i.verdict?`Best ${pct(i.verdict.best.score)} vs comparison ${pct(i.verdict.compare.score)} · ${i.verdict.improved?'improved':'no improvement'} · ${when(i.created_at)}`:when(i.created_at)))));
}

async function renderImprovement(id,refresh=false) {
  const ticket=++view.request;
  if (!refresh) view.root.replaceChildren(skeleton('Loading improvement…'));
  try {
    const record=await api(`/improvements/${id}`);
    const [{lists},{configs}]=await Promise.all([api('/review-lists'),api('/configs')]);
    if (ticket!==view.request) return;
    const main=el('div',{class:'cfg-main'});
    layout(lists,record.list_id,main);
    const name=key=>versionName(configs.find(c=>c.config_id===key));
    const list=lists.find(l=>l.list_id===record.list_id);
    const spec=record.spec;
    main.append(el('div',{class:'heading'},el('div',{},
      el('h1',{},`Improving ${list?.name || 'a review list'}`),
      el('p',{class:'muted small'},`From ${name(spec.base_config_id)} · compared with ${name(spec.compare_config_id)} · ${spec.improver.model_id} (${spec.improver.reasoning_effort}) · ${plural(spec.rounds,'round')} · ${plural(spec.items.length,'comment')} · ${when(record.created_at)}`)),
      badge(record.status)),
      el('p',{},el('a',{href:`#review/${record.list_id}`},'← Back to the list')));
    const job=record.job, progress=job?.progress || {};
    if (['queued','running'].includes(record.status) && progress.total) main.append(el('div',{class:'comparison-progress'},
      el('div',{class:'progress',role:'progressbar','aria-valuemin':0,'aria-valuemax':progress.total,'aria-valuenow':progress.done,'aria-label':'Evaluations finished'},el('span',{style:`width:${Math.round(100*progress.done/progress.total)}%`})),
      el('p',{class:'small muted'},`${progress.done} of ${progress.total} evaluations finished`)));
    else if (record.status==='queued') main.append(message('Queued. A job runner starts it shortly.'));
    if (record.error) main.append(message(record.error,true));
    if (record.verdict) main.append(verdict(record.verdict,name,record.best_config_id));
    main.append(...record.rounds.map(r=>roundView(r,name)));
    if (record.rounds.length) main.append(judgementTable(spec.items,record.rounds));
    if (['queued','running'].includes(record.status)) view.poll=setTimeout(()=>renderImprovement(id,true),POLL_MS);
  } catch(error) {
    if (ticket===view.request && error.name!=='AbortError') view.root.replaceChildren(message(`Cannot load this improvement. ${error.message}`,true));
  }
}

function verdict(v,name,bestId) {
  const better=v.improved;
  return el('section',{class:'cfg-check','data-level':2},el('h2',{},better?'The revision scores higher':'No improvement over the comparison'),
    el('div',{class:'gen-split'},
      el('div',{class:'metric'},el('span',{},`Best · ${name(v.best.config_id)}`),el('strong',{},pct(v.best.score))),
      el('div',{class:'metric'},el('span',{},`Comparison · ${name(v.compare.config_id)}`),el('strong',{},pct(v.compare.score))),
      el('div',{class:'metric'},el('span',{},'Comments addressed'),el('strong',{},`${v.best.addressed} vs ${v.compare.addressed}`))),
    el('p',{class:'small muted'},v.note),
    el('div',{class:'cfg-actions rv-verdict-actions'},
      better?el('a',{class:'button primary',href:`#configure/${bestId}`},'Open the best version to save it'):null,
      el('a',{class:'button',href:`#compare/${v.best.experiment_id}`},'Inspect its surveys'),
      el('a',{class:'button',href:'#compare'},'Compare on more insights')));
}

function evaluationLine(e,name) {
  const counts=Object.entries(e.counts).map(([k,n])=>`${n} ${words(k)}`).join(', ');
  return el('div',{class:'rv-eval'},
    el('span',{},el('strong',{},`${e.label} · ${pct(e.score)}`),` · ${e.addressed} of ${plural(e.comments,'comment')} addressed · ${counts}${e.mean_relevance!==null?` · reviewer relevance ${e.mean_relevance}`:''}`),
    el('span',{class:'small'},el('a',{href:`#configure/${e.config_id}`},name(e.config_id)),' · ',el('a',{href:`#compare/${e.experiment_id}`},'surveys')));
}

function roundView(r,name) {
  if (r.round===0) return el('section',{class:'cfg-step'},el('h2',{},'Starting point'),r.evaluations.map(e=>evaluationLine(e,name)));
  const p=r.proposal;
  return el('section',{class:'cfg-step'},
    el('div',{class:'cfg-step-head'},el('h2',{},`Round ${r.round}`),el('span',{class:`cfg-marker ${r.kept?'changed':''}`},r.kept?'kept':'not kept')),
    el('p',{},p.summary),
    evaluationLine(r.evaluations[0],name),
    el('p',{class:'small muted'},`Previous best ${pct(r.parent_score)}`),
    ...p.prompt_edits.map(edit=>el('details',{'data-level':3},el('summary',{},`${edit.prompt==='generate'?'Generation':'Review'} prompt edit · ${edit.reason}`),
      el('div',{class:'cfg-diff'},lineDiff(edit.find,edit.replace).map(l=>el('div',{class:`diff-line ${l.op==='+'?'diff-add':l.op==='-'?'diff-del':''}`},`${l.op} ${l.text}`))))),
    p.setting_changes.length?el('ul',{class:'cfg-changes'},p.setting_changes.map(c=>el('li',{},el('strong',{},`${c.path.replaceAll('.',' › ').replaceAll('_',' ')} → ${c.value ?? 'unset'}`),` · ${c.reason}`))):null);
}

function judgementTable(items,rounds) {
  const evaluations=rounds.flatMap(r=>r.evaluations);
  const cell=(e,item)=>{
    const j=e.judgements.find(x=>x.item_id===item.item_id);
    return el('td',{title:j?.reason || ''},j?pct(j.score):'—');
  };
  return el('section',{class:'cfg-tests','data-level':3},el('h2',{},'Judge scores per comment'),
    el('p',{class:'small muted'},'A model’s judgement of whether each new survey addresses the comment. Hover a score for its reason.'),
    el('div',{class:'table-scroll'},el('table',{class:'gen-table'},
      el('thead',{},el('tr',{},el('th',{},'Comment'),evaluations.map(e=>el('th',{},e.label)))),
      el('tbody',{},items.map(item=>el('tr',{},el('td',{},`${item.insight_id}: ${item.comment}`),evaluations.map(e=>cell(e,item))))))));
}

// "Add to review list" for any saved result's survey. Lists load when it opens.
export function addToReviewList(runId,insightId,survey) {
  const body=el('div',{});
  const box=el('details',{class:'rv-add',onToggle:()=>{ if (box.open && !body.childElementCount) load(); }},el('summary',{},'Add to review list'),body);
  async function load() {
    body.replaceChildren(el('p',{class:'small muted'},'Loading review lists…'));
    try {
      const {lists}=await globalApi('/review-lists');
      const newName=el('input',{name:'new_list',maxlength:120,placeholder:'New list name'});
      const choose=el('select',{name:'list',onChange:()=>{ newName.hidden=choose.value!==''; newName.required=choose.value===''; }},
        lists.map(l=>el('option',{value:l.list_id},l.name)),el('option',{value:''},'New list…'));
      newName.hidden=lists.length>0; newName.required=!lists.length;
      const form=el('form',{onSubmit:submit},
        el('label',{},'List',choose),newName,
        el('label',{},'About',el('select',{name:'question'},el('option',{value:''},'The whole survey'),survey.items.map((q,i)=>el('option',{value:q.item_id},`Question ${i+1}: ${q.prompt}`)))),
        commentFields(),el('button',{type:'submit',class:'primary'},'Add comment'));
      body.replaceChildren(form);
      async function submit(event) {
        event.preventDefault();
        const f=form.elements, button=form.querySelector('button[type=submit]');
        button.disabled=true;
        try {
          const list=choose.value?{list_id:choose.value,name:choose.selectedOptions[0].textContent}
            :await globalApi('/review-lists',{method:'POST',body:JSON.stringify({name:newName.value.trim()})});
          await globalApi(`/review-lists/${list.list_id}/items`,{method:'POST',body:JSON.stringify({run_id:runId,insight_id:insightId,question_id:f.question.value || null,comment:f.comment.value,should_be:f.should_be.value || null})});
          body.replaceChildren(el('p',{class:'notice',role:'status'},'Added to ',el('a',{href:`#review/${list.list_id}`},list.name),'. Commenting on the same question again replaces the comment.'),
            el('button',{type:'button',class:'quiet',onClick:load},'Add another'));
        } catch(error) { button.disabled=false; form.append(message(error.message,true)); }
      }
    } catch(error) { body.replaceChildren(message(`Review lists unavailable. ${error.message}`,true)); }
  }
  return box;
}
