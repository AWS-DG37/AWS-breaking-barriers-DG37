import {el} from './dom.mjs';
import {mountSurveyPlayer} from './survey-player.mjs';

const root=document.getElementById('recorded-preview');
async function load() {
  root.replaceChildren(el('p',{},'Loading recorded survey…'));
  try {
    const response=await fetch(new URL('./surveys.json',import.meta.url));
    if(!response.ok) throw new Error(`Catalog returned ${response.status}`);
    const catalog=await response.json();
    const host=el('div',{id:'recorded-phone',class:'survey-player'});
    const context=el('div',{class:'recorded-context'});
    const evidence=el('div',{class:'recorded-evidence'});
    const select=el('select',{'aria-label':'Recorded candidate',onChange:()=>choose(Number(select.value))},catalog.candidates.map((candidate,index)=>el('option',{value:index},candidate.label)));
    root.replaceChildren(el('label',{class:'recorded-picker'},'Recorded candidate',select),context,host,evidence);
    function choose(index) {
      const candidate=catalog.candidates[index];
      const survey=candidate.status==='failed'?null:candidate.survey || candidate.candidate_survey;
      context.replaceChildren(el('p',{},`${candidate.label} · ${candidate.status} · ${candidate.insight_id}`));
      host.replaceChildren();
      evidence.replaceChildren();
      if(!survey?.items?.length) {
        context.append(el('p',{},'No survey questions recorded for this outcome.'),el('p',{},candidate.error_type || candidate.reason || 'No additional reason recorded.'));
        if(candidate.reason && candidate.reason!==candidate.error_type) context.append(el('p',{},candidate.reason));
        return;
      }
      if(!candidate.survey) context.append(el('p',{},'Rejected draft. This survey was not accepted for delivery.'));
      const update=index=>{
        const item=survey.items[index];
        const records=candidate.question_evidence.filter(record=>record.item_id===item.item_id);
        evidence.replaceChildren(el('p',{},`${item.item_id} · ${item.type}`),el('strong',{},'Model-supplied measurement evidence'),...records.map(record=>el('div',{},el('p',{},`${record.measurement} · ${record.explanation}`),el('p',{},`Source fields · ${record.source_fields.join(', ')}`))),el('p',{},'Diagnostic explanation, not independent quality validation.'),el('details',{},el('summary',{},'Question metadata'),el('pre',{},JSON.stringify(item,null,2))));
      };
      mountSurveyPlayer(host,{survey,session:{phase:'questions',question:0,answers:{}},photoUrl:new URL('./mustard-field.webp',import.meta.url).href,onQuestionChange:update});
      update(0);
    }
    choose(0);
  } catch {
    root.replaceChildren(el('p',{role:'alert'},'Recorded preview unavailable. Build this page with python scripts/build_architecture_viewer.py <output-folder>, serve the output folder over HTTP, and reload.'),el('button',{type:'button',onClick:load},'Retry loading preview'));
  }
}
load();
