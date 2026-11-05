import {el, skeleton} from './dom.mjs';
import {requestScope} from './api.mjs';
import {mountSurveyPlayer} from './survey-player.mjs';
import {englishVersion, runAgent} from './agents.mjs';
import {renderDashboard} from './demo-dashboard.mjs';
// Demo: one insight told as a story. What happened, whether FarmerChat follows up, and the
// survey the farmer would get, written by the chosen model and checked by Sonnet 5.5.

// Insights offered in the Demo drop-down, in order. Edit this list to change the demo.
export const DEMO_INSIGHTS = [
  'I-000523',  // cabbage, Hindi: caterpillars, told not to keep spraying -> survey
  'I-000700',  // mustard, English: heat, light irrigation -> survey
  'I-000798',  // goat, Hindi: fever and poor appetite -> survey
  'I-004743',  // buffalo, Hindi: vaccination, sent to the vet -> survey
  'I-001303',  // wheat, English: weather question only -> no survey (R3)
  'I-001333',  // gram, Hindi: still needs details -> no survey (R4)
  'I-004256',  // lentil, Hindi: lodging advice that needs no check -> no survey (R5)
  'I-000090',  // a thank-you and goodbye -> no survey (R1)
  'I-001180',  // asked for a cricket score -> no survey (R1)
];
export const TIERS = [
  {id:'premium', label:'Most capable · Claude Opus 5.5', model:'global.anthropic.claude-opus-5-5', name:'Claude Opus 5.5'},
  {id:'balanced', label:'Balanced · Claude Haiku 5.5', model:'global.anthropic.claude-haiku-5-5', name:'Claude Haiku 5.5'},
  {id:'budget', label:'Lowest cost · GPT-5.5', model:'us.openai.gpt-5.5', name:'GPT-5.5'},
];
const VERIFIER = {model:'global.anthropic.claude-sonnet-5-5', name:'Claude Sonnet 5.5'};
// Who decides whether to follow up: the code rules, or a Strands classifier agent.
export const GATES = [
  {id:'rules', label:'Follow-up rules · instant'},
  {id:'classifier', label:'Classifier agent · Claude Haiku 5.5', model:'global.anthropic.claude-haiku-5-5', name:'Claude Haiku 5.5'},
];
const RULE_TEXT = {
  R1:'This was not a real farming conversation (a greeting, or a message that could not be understood), so there is nothing to follow up on.',
  R3:'The farmer only asked for general information. They were not asked to do anything, so there is nothing to check.',
  R4:'FarmerChat still needs more details from the farmer before any advice can be checked.',
  R5:'This advice does not need a follow-up check.',
  R8:'The farmer was given advice they can act on. A short survey checks whether they acted on it and whether it helped.',
};
const LANGUAGE = {'hi-IN':'Hindi', 'en-IN':'English'};
const words = value => String(value ?? '').replaceAll('_',' ');
const cap = value => { const s=words(value); return s ? s[0].toUpperCase()+s.slice(1) : ''; };

const requests=requestScope(), runs=requestScope();
const view={root:null, request:0, insights:new Map(), insightId:DEMO_INSIGHTS[0], tier:'premium', gate:'rules', run:0, mode:'live'};

// Shrink the whole phone, never just its height, so it stays a normal phone shape and still
// fits beside the story without scrolling the page.
function fitPhone(wrap) {
  const phone=wrap.querySelector('.survey-phone');
  if (!phone || !wrap.isConnected) return;
  phone.style.transform=''; wrap.style.height='';
  const full=phone.offsetHeight, top=Math.max(12, phone.getBoundingClientRect().top);
  const caption=wrap.querySelector('figcaption')?.offsetHeight || 0;
  const scale=Math.max(0.55, Math.min(1, (window.innerHeight-top-caption-36)/full));
  phone.style.transform=`scale(${scale})`;
  phone.style.marginBottom=`${-(full*(1-scale))}px`;   // give back the space the scale removed
}
window.addEventListener('resize',()=>{ const wrap=document.querySelector('.demo-phone'); if (wrap) fitPhone(wrap); });

export function leaveDemo() { ++view.request; requests.cancel(); }

export async function showDemo(root) {
  view.root=root;
  requests.renew();
  const ticket=++view.request;
  if (root.childElementCount) return;   // keep the current story when coming back to the tab
  root.replaceChildren(skeleton('Loading the demo…'));
  const found=await Promise.all(DEMO_INSIGHTS.map(id=>requests.api(`/agents/insights/${id}`).then(data=>[id,data]).catch(()=>null)));
  if (ticket!==view.request) return;
  for (const entry of found) if (entry) view.insights.set(...entry);
  if (!view.insights.size) { root.replaceChildren(el('p',{class:'notice error'},'None of the demo insights could be loaded.')); return; }
  if (!view.insights.has(view.insightId)) view.insightId=[...view.insights.keys()][0];
  render();
}

function title(id) {
  const i=view.insights.get(id).insight;
  return `${cap(i.asset?.name) || 'No crop'} · ${LANGUAGE[i.conversation_language] || i.conversation_language} · ${i.query_summary}`;
}

function render() {
  const insight=el('select',{onChange:e=>{view.insightId=e.target.value;reset();}},
    [...view.insights.keys()].map(id=>el('option',{value:id,selected:id===view.insightId},title(id))));
  const tier=el('select',{onChange:e=>{view.tier=e.target.value;}},
    TIERS.map(t=>el('option',{value:t.id,selected:t.id===view.tier},t.label)));
  const gate=el('select',{onChange:e=>{view.gate=e.target.value;}},
    GATES.map(g=>el('option',{value:g.id,selected:g.id===view.gate},g.label)));
  const go=el('button',{type:'submit',class:'primary'},'Run the follow-up');
  const live=el('div',{class:'demo-layout',id:'demo-live',hidden:view.mode!=='live'},
    el('div',{class:'demo-main'},
      el('div',{class:'heading'},el('div',{},el('h1',{},'Demo'),
        el('p',{class:'muted'},'Pick a farmer conversation and a model level. FarmerChat decides whether to follow up, then writes the survey the farmer would get.')),modeSwitch()),
      el('form',{class:'demo-controls',onSubmit:e=>{e.preventDefault();start();}},
        el('label',{},'Conversation',insight),
        el('label',{},'Who decides to follow up',gate),
        el('label',{},el('span',{},'Model level ',el('span',{class:'muted demo-verifier'},`· checked by ${VERIFIER.name}`)),tier),
        go),
      el('div',{class:'demo-stage'},el('section',{class:'demo-scenario',id:'demo-scenario'},...scenario()),
        el('section',{class:'demo-flow',id:'demo-flow','aria-live':'polite'}))),
    el('section',{class:'demo-phone-col',id:'demo-phone','aria-label':'Survey on the farmer’s phone'}));
  const dashboard=el('div',{class:'demo-dashboard',id:'demo-dashboard',hidden:view.mode!=='dashboard'},
    el('div',{class:'heading'},el('div',{},el('h1',{},'After 1,000 surveys'),
      el('p',{class:'muted'},'What a Digital Green team could see once farmers answer their follow-ups.')),modeSwitch()),
    ...renderDashboard());
  view.root.replaceChildren(live,dashboard);
}

// Two views of the demo. Switching keeps the live run and its phone where they were.
function modeSwitch() {
  const options=[['live','Live follow-up'],['dashboard','After 1,000 surveys']];
  return el('div',{class:'demo-mode',role:'group','aria-label':'Demo view'},options.map(([mode,label])=>
    el('button',{type:'button','data-mode':mode,'aria-pressed':String(view.mode===mode),onClick:()=>setMode(mode)},label)));
}
function setMode(mode) {
  view.mode=mode;
  document.getElementById('demo-live').hidden=mode!=='live';
  document.getElementById('demo-dashboard').hidden=mode!=='dashboard';
  view.root.querySelectorAll('.demo-mode button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.mode===mode)));
  if (mode==='live') { const wrap=document.querySelector('.demo-phone'); if (wrap) fitPhone(wrap); }
  window.scrollTo({top:0});
}

function reset() {
  runs.renew(); ++view.run;
  document.getElementById('demo-scenario').replaceChildren(...scenario());
  document.getElementById('demo-flow').replaceChildren();
  document.getElementById('demo-phone').replaceChildren();
}

function scenario() {
  const {insight:i}=view.insights.get(view.insightId);
  const where=[i.location?.district,i.location?.state].filter(Boolean).join(', ');
  const when=i.created_at ? new Date(i.created_at).toLocaleDateString('en-IN',{day:'numeric',month:'long',year:'numeric'}) : null;
  const fact=(label,value)=>value ? el('div',{class:'demo-fact'},el('dt',{},label),el('dd',{},value)) : null;
  return [
    el('h2',{},'What happened so far'),
    el('p',{class:'small muted'},[where && `A farmer in ${where}`, when && `on ${when}`, `spoke ${LANGUAGE[i.conversation_language] || i.conversation_language}`].filter(Boolean).join(' · ')),
    el('div',{class:'demo-bubble farmer'},el('span',{class:'demo-who'},'The farmer'),el('p',{},i.query_summary)),
    el('div',{class:'demo-bubble assistant'},el('span',{class:'demo-who'},'FarmerChat advised'),el('p',{},i.advice_summary)),
    el('dl',{class:'demo-facts'},
      fact('Crop or animal',[cap(i.asset?.name),words(i.asset?.growth_stage)].filter(Boolean).join(', ')),
      fact('Concern',cap(i.concern)),
      fact('Severity',cap(i.severity)),
      fact('Kind of advice',cap(i.resolution_type)),
      fact('Act within',i.action_window_days ? `${i.action_window_days} day${i.action_window_days===1?"":"s"}` : null)),
  ];
}

function step(label, state='active', detail=null) {
  const node=el('li',{class:`demo-step ${state}`},
    el('span',{class:'demo-step-mark','aria-hidden':true}),
    el('div',{},el('strong',{},label),detail ? el('p',{class:'small muted'},detail) : null));
  return node;
}
const finish=(node,label,detail,state='done')=>{
  node.className=`demo-step ${state}`;
  node.querySelector('strong').textContent=label;
  const p=node.querySelector('p') || node.lastChild.appendChild(el('p',{class:'small muted'}));
  p.textContent=detail || '';
};
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));

async function start() {
  runs.renew();
  const ticket=++view.run, tier=TIERS.find(t=>t.id===view.tier), gate=GATES.find(g=>g.id===view.gate);
  const choice=gate.model ? {gate:gate.id, classifier:gate.model} : {};
  const flow=document.getElementById('demo-flow'), phoneCol=document.getElementById('demo-phone');
  phoneCol.replaceChildren();
  const steps=el('ol',{class:'demo-steps'});
  flow.replaceChildren(el('h2',{},'What FarmerChat does next'),steps);
  const alive=()=>ticket===view.run;
  try {
    const rules=step(gate.model ? `${gate.name} is reading the conversation to decide…` : 'Reading the conversation and checking the follow-up rules…');
    steps.append(rules);
    const [{decision}]=await Promise.all([
      runs.api('/agents/decide',{method:'POST',body:JSON.stringify({insight_id:view.insightId,...choice})}),
      pause(1600)]);   // long enough for the audience to read the step
    if (!alive()) return;
    // Label by what answered: an agent deployed before the classifier always runs the rules.
    finish(rules,decision.gate==='classifier'?`Decided by ${gate.name}`:'Rules checked',`Rule ${decision.rule}`);
    const shown=verdict(decision.survey,decision);
    flow.append(shown);
    if (!decision.survey) return;
    await pause(900);
    if (!alive()) return;
    const write=step(`${tier.name} is writing the survey, and ${VERIFIER.name} is checking it…`,'active','0 s');
    const later=el('ol',{class:'demo-steps'},write);
    flow.append(later);
    const started=Date.now();
    const clock=setInterval(()=>{ if(!alive()) return clearInterval(clock); write.querySelector('p').textContent=`${Math.round((Date.now()-started)/1000)} s`; },1000);
    let record;
    try {
      record=await runAgent({insight_id:view.insightId,generator:tier.model,verifier:VERIFIER.model,pipeline:'direct',max_retries:1,...choice},runs);
    } finally { clearInterval(clock); }
    if (!alive()) return;
    const r=record.result, seconds=Math.round((Date.now()-started)/1000);
    const revisions=r.attempts>1 ? ` after ${r.attempts-1} revision${r.attempts>2?'s':''}` : '';
    // The run asks the classifier again, and a second read can rarely disagree or fail. Replace
    // the verdict on screen with what the run decided, so the page never says yes above a no.
    if (r.status==='skipped' || r.decision_survey==null) {
      shown.replaceWith(r.status==='skipped' ? verdict(false,{rule:r.decision_rule,reason:r.decision_reason}) : undecided(r.error));
      finish(write,r.status==='skipped'?'No survey after all':'The classifier did not decide',
        r.status==='skipped'?`On a second read the classifier chose rule ${r.decision_rule}`:'Nothing was written.','warn');
      return;
    }
    if (r.status==='accepted') finish(write,`Survey written by ${tier.name} and approved by ${VERIFIER.name}${revisions}`,`${seconds} s${r.relevance!=null?` · relevance ${Number(r.relevance).toFixed(2)}`:''}`);
    else finish(write,r.status==='failed'?'The survey could not be written':`${VERIFIER.name} did not approve this survey${revisions}`,r.error || `${seconds} s · showing the last draft`,'warn');
    if (r.items?.length) phoneCol.append(phone(r));
  } catch (error) {
    if (error.name==='AbortError' || !alive()) return;
    flow.append(el('ol',{class:'demo-steps'},step('Something went wrong','warn',error.message)));
  }
}

function verdict(yes, decision) {
  return el('div',{class:`demo-verdict ${yes?'yes':'no'}`,role:'status'},
    el('span',{class:`demo-verdict-icon ${yes?'tick':'dash'}`,'aria-hidden':true}),
    el('div',{},el('strong',{},yes?'We will follow up with a survey':'No survey for this conversation'),
      el('p',{},RULE_TEXT[decision.rule] || words(decision.reason_code)),
      decision.reason ? el('p',{class:'small muted'},`The classifier’s reason: ${decision.reason}`) : null));
}

function undecided(error) {
  return el('div',{class:'demo-verdict no',role:'status'},
    el('span',{class:'demo-verdict-icon dash','aria-hidden':true}),
    el('div',{},el('strong',{},'No decision for this conversation'),
      el('p',{},`The classifier gave no usable answer, so no survey was written.${error?` ${error}`:''}`)));
}

function phone(r) {
  const host=el('div',{class:'survey-player'}), english=englishVersion(r);
  const wrap=el('div',{class:'demo-phone'},el('figure',{class:'phone-stage'},host,
    el('figcaption',{},`What the farmer sees.${english?' Use the switch at the top to read it in English.':''} Answers are not sent.`)));
  requestAnimationFrame(()=>fitPhone(wrap));
  mountSurveyPlayer(host,{survey:{language:r.language,items:r.items},translation:english,
    session:{phase:'questions',question:0,answers:{}},photoUrl:'/static/assets/mustard-field.webp'});
  return wrap;
}
