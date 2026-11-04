import {el} from './dom.mjs';
import {api} from './api.mjs';

export const money=value=>value===null || value===undefined?'Unknown':`$${Number(value).toFixed(6)}`;
export function budgetInput(label='Run budget (USD)') {
  return el('label',{},label,el('input',{name:'budget_usd',type:'number',min:'0.000001',step:'any',value:'5',required:true,'aria-label':label}));
}
export function costSummary(costs,runId) {
  if (!costs?.available) return el('p',{class:'small muted'},'Cost unavailable: this run predates request accounting.');
  const summary=el('section',{class:'run-costs'},el('p',{},
    `Estimated spend ${money(costs.estimated_usd)}${!costs.complete?` · known subtotal ${money(costs.known_estimated_usd)}`:''} · budget ${costs.budget_usd===null?'No limit':money(costs.budget_usd)} · committed ${money(costs.committed_usd)}`),
    el('p',{class:'small muted'},`${costs.complete_calls}/${costs.call_count} requests fully priced. Known tokens: ${costs.known_input_tokens} input / ${costs.known_output_tokens} output. Figures use saved model prices.`),
    !costs.complete?el('p',{class:'small muted'},'Committed includes held reservations for requests whose final costs are unresolved. It is not confirmed spend; a zero known subtotal does not mean those requests were free.'):null);
  if (costs.stop_reason) summary.append(el('p',{class:'notice error'},costs.stop_reason));
  const body=el('div',{}), details=el('details',{},el('summary',{},'Request costs and price sources'),body);
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
