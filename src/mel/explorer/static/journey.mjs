import {el} from './dom.mjs';
import {journeyVisits} from './domain.mjs';

const labels = {input:'Receive insight', context:'Source context', select:'Eligibility & timing',
  generate:'Generate draft', schema:'Validate survey schema', review:'Model review',
  decision:'Final outcome', failure:'Execution failed'};
const words = value => String(value ?? '').replaceAll('_',' ');

export function visitSummary(visit) {
  const data = visit.events.at(-1)?.data || {};
  if (data.status) return words(data.status);
  if (data.error_type) return data.error_type;
  if ('eligible' in data) return `${data.rule || ''} · ${data.eligible?'Eligible':'Skipped'}`;
  if ('valid' in data) return data.valid?'Schema passed':'Schema failed';
  if (data.review) return `Relevance ${data.review.relevance_score}${data.review.issues?.length?' · issues found':''}`;
  if (data.survey) return `${data.survey.items?.length || 0} questions`;
  if (data.context) return data.context.profile?'Farmer profile matched':'No farmer profile matched';
  return `${visit.events.length} recorded event${visit.events.length===1?'':'s'}`;
}

// The same saved-evidence view serves batch runs and comparisons.
export function insightJourney(outcome, {stage, event = 0, onSelect} = {}) {
  const visits = journeyVisits(outcome.events || []);
  let active = stage ? visits.findIndex(v=>v.stage===stage && v.eventIndices.includes(event)) : visits.length-1;
  if (active < 0) active = visits.length-1;
  const panel = el('section',{class:'journey-section'},
    el('div',{class:'section-heading'},el('h2',{},'Insight journey'),
      el('span',{class:'small muted'},'Recorded path · select a step for evidence')));
  const evidence = el('div',{class:'journey-evidence'});
  const buttons = [];
  function select(index) {
    const visit = visits[index];
    buttons.forEach((button,i)=>button.setAttribute('aria-pressed',String(i===index)));
    if (onSelect) { onSelect(visit.stage,visit.eventIndices.at(-1)); return; }
    const data = visit.events.at(-1)?.data || {};
    evidence.replaceChildren(el('h3',{},labels[visit.phase] || words(visit.stage)),
      el('p',{},visitSummary(visit)),
      ...(data.explanation || data.reason ? [el('p',{},data.explanation || data.reason)] : []),
      ...visit.events.map(saved=>el('details',{},
        el('summary',{},`#${saved.sequence} · ${saved.source}${saved.attempt?` · attempt ${saved.attempt}`:''}`),
        el('pre',{},JSON.stringify(saved.data,null,2)))));
  }
  if (visits.length) {
    const path = el('ol',{class:'journey','aria-label':'Recorded execution path'});
    visits.forEach((visit,index)=>{
      const button = el('button',{type:'button',class:`stage stage-${visit.phase}`,'aria-pressed':String(index===active),onClick:()=>select(index)},
        el('span',{class:'small muted'},`Step ${index+1}${visit.attempt?` · attempt ${visit.attempt}`:''}`),
        el('strong',{},labels[visit.phase] || words(visit.stage)),el('span',{class:'small'},visitSummary(visit)));
      buttons.push(button);
      path.append(el('li',{},button));
    });
    panel.append(path);
    if (!onSelect) { panel.append(evidence); select(active); }
  } else panel.append(el('p',{class:'missing'},`Intermediate history not recorded. Final outcome: ${words(outcome.status)}. No execution path can be reconstructed.`));
  panel.append(el('details',{class:'journey-process'},el('summary',{},'How the process works'),
    el('p',{},'Receive insight → eligibility and send-window rules → generate draft → validate schema → model review → final outcome. Source context is optional and, when supplied, is recorded before eligibility.'),
    el('ul',{},
      el('li',{},'Ineligible insights stop as skipped, without model calls.'),
      el('li',{},'Schema failures bypass the reviewer. Schema or review failures return to generation with feedback while revisions remain; otherwise the draft is rejected.'),
      el('li',{},'Acceptance requires the configured relevance threshold, correct language, no new advice, no personal-information requests and no reviewer issues. Survey and handoff contracts are validated before acceptance.'),
      el('li',{},'Execution errors stop as failed. Accepted results include a handoff payload; only production runs enqueue it. Comparisons do not schedule or deliver surveys.'))));
  return panel;
}
