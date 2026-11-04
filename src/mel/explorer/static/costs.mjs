import {el} from './dom.mjs';
import {api} from './api.mjs';

export const money=value=>value===null || value===undefined?'Unknown':`$${Number(value).toFixed(6)}`;
export function budgetInput(label='Run budget (USD)') {
  return el('label',{},label,el('input',{name:'budget_usd',type:'number',min:'0.000001',step:'any',value:'5',required:true,'aria-label':label}));
}
// The console-wide cumulative cap. Enter in its field must not submit the launch form.
export function capPanel() {
  const box=el('div',{class:'spend-cap small',role:'status'},'Loading the console spend cap…');
  async function load() {
    try {
      const cap=await api('/spend-cap');
      // The editor saves on its own button. A form id that matches no form detaches the
      // input from the launch form, so a draft hidden by the dial cannot block a launch.
      const input=el('input',{type:'number',min:'0.000001',step:'any',value:cap.budget_usd ?? '','aria-label':'Console spend cap (USD)',form:'spend-cap-editor',onKeydown:event=>{if(event.key==='Enter') event.preventDefault();}});
      const save=el('button',{type:'button',class:'secondary',onClick:async()=>{
        save.disabled=true;
        try {await api('/spend-cap',{method:'PUT',body:JSON.stringify({expected_version:cap.version,budget_usd:input.value || null})});await load();}
        catch(e) {box.removeAttribute('data-level');box.append(el('p',{class:'notice error'},e.message));save.disabled=false;}
      }},'Save cap');
      // An unset cap refuses live runs, so it shows at every detail level.
      if (cap.budget_usd===null) box.removeAttribute('data-level'); else box.setAttribute('data-level','2');
      box.replaceChildren(
        el('p',{},cap.budget_usd===null?'No console spend cap is set. Live runs are refused until you set one.':`Console spend cap ${money(cap.budget_usd)} · committed ${money(cap.committed_usd)} · remaining ${money(cap.remaining_usd)}`),
        el('label',{},'Console spend cap (USD)',input),save,
        el('p',{class:'muted'},cap.scope));
    } catch(e) {box.removeAttribute('data-level');box.replaceChildren(el('p',{class:'notice error'},`Spend cap unavailable. ${e.message}`));}
  }
  load();
  return box;
}
export function costSummary(costs,runId) {
  if (!costs?.available) return el('p',{class:'small muted','data-level':2},'Cost unavailable: this run predates request accounting.');
  // The cost lines are Detailed; a stop reason is a notice and shows at every level.
  const summary=el('section',{class:'run-costs'},el('p',{'data-level':2},
    `Estimated spend ${money(costs.estimated_usd)}${!costs.complete?` · known subtotal ${money(costs.known_estimated_usd)}`:''} · budget ${costs.budget_usd===null?'No limit':money(costs.budget_usd)} · committed ${money(costs.committed_usd)}`),
    el('p',{class:'small muted','data-level':2},`${costs.complete_calls}/${costs.call_count} requests fully priced. Known tokens: ${costs.known_input_tokens} input / ${costs.known_output_tokens} output. Figures use saved model prices.`),
    !costs.complete?el('p',{class:'small muted','data-level':2},'Committed includes held reservations for requests whose final costs are unresolved. It is not confirmed spend; a zero known subtotal does not mean those requests were free.'):null);
  if (costs.stop_reason) summary.append(el('p',{class:'notice error'},costs.stop_reason));
  const body=el('div',{}), details=el('details',{'data-level':2},el('summary',{},'Request costs and price sources'),body);
  let loaded=false, offset=0;
  async function load() {
    const result=await api(`/runs/${runId}/costs?offset=${offset}&limit=100`);
    const rows=result.calls.map(call=>el('p',{class:'small'},
      `${call.insight_id} · ${call.role} · attempt ${call.attempt} · ${call.model_id} · ${call.api} · ${call.region} · ${call.state} · ${call.input_tokens ?? '?'} in / ${call.output_tokens ?? '?'} out · ${money(call.estimated_usd)}. `,
      call.pricing?el('a',{href:call.pricing.source,target:'_blank',rel:'noopener'},`Price source (checked ${call.pricing.checked_on})`):'Price unavailable'));
    body.append(...rows);offset+=result.calls.length;
    next.hidden=offset>=result.call_count;
  }
  const next=el('button',{type:'button',class:'secondary',onClick:async()=>{next.disabled=true;try{await load();}catch(e){body.append(el('p',{},e.message));}finally{next.disabled=false;}}},'More requests');
  details.addEventListener('toggle',async()=>{if(!details.open || loaded)return;loaded=true;try{await load();}catch(e){loaded=false;body.replaceChildren(el('p',{},e.message));}});
  details.append(next);summary.append(details);return summary;
}
