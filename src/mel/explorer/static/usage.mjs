import {el} from './dom.mjs';

const money=value=>value===null || value===undefined?'Unknown':`$${value}`;
const label=(title,input)=>el('label',{},title,input);
const field=(name,value,attrs={})=>el('input',{name,value:value??'',...attrs});

export function quoteView(quote) {
  return el('div',{class:'exposure'},
    el('strong',{},`Maximum configured estimate ${money(quote.maximum_estimated_usd)}`),
    el('p',{},`Remaining website budget ${money(quote.remaining_usd)}`),
    ...quote.requests.map(row=>el('p',{class:'small'},`${row.candidate} · ${row.role} · ${row.model_id} · ${row.api}. Each request holds ${money(row.request_reservation_usd)}. At most ${row.maximum_requests} requests under the selected limits.`)),
    ...quote.blockers.map(message=>el('p',{class:'missing'},message)),
    el('p',{class:'small muted'},quote.note));
}

export async function mountUsage(host,{api,config,detail,onPolicy,notice}) {
  host.replaceChildren(el('p',{},'Loading usage and live controls…'));
  const policy=await api('/live-policy');
  onPolicy(policy);
  const status=el('div',{class:'exposure'},el('strong',{},policy.effective?'Live execution available':'Live execution blocked'),
    ...policy.blockers.map(message=>el('p',{class:'missing'},message)),
    el('p',{},`Cumulative budget ${money(policy.budget_usd)} · Remaining ${money(policy.remaining_usd)}`),
    el('p',{},`Observed estimate ${money(policy.estimated_usd)} · Held requests ${money(policy.held_usd)}`),
    el('p',{class:'small'},`${policy.pending_calls} requests awaiting a result · ${policy.unresolved_calls} unresolved requests`),
    el('p',{class:'small muted'},'All dollar figures are configured-price estimates, not an AWS invoice or account-wide cap. Hosting and other service costs are excluded. Held amounts are reservations, not charges. Pending requests remain held after a restart. Unresolved requests require manual operator reconciliation with provider evidence.'),
    el('p',{class:'small muted'},policy.scope));
  const budget=field('budget',policy.budget_usd,{type:'number',min:'0.000000000001',step:'any',required:true});
  const enabled=field('enabled',null,{type:'checkbox',checked:policy.enabled});
  const rows=el('div',{class:'price-rows'});
  function addPrice(price={}) {
    const apiSelect=el('select',{name:'api'},['responses','converse'].map(api=>el('option',{value:api,selected:api===price.api},api)));
    const row=el('fieldset',{class:'price-row'},el('legend',{},'Model price and request bound'),
      el('div',{class:'form-grid'},
        label('Model ID',field('model_id',price.model_id,{required:true})),label('Native API',apiSelect),
        label('AWS region',field('region',price.region??config.aws_region,{required:true})),
        label('Input USD per million tokens',field('input_per_million',price.input_per_million,{type:'number',min:0,step:'any',required:true})),
        label('Output USD per million tokens',field('output_per_million',price.output_per_million,{type:'number',min:0,step:'any',required:true})),
        label('Documented maximum billable input tokens',field('max_input_tokens',price.max_input_tokens,{type:'number',min:1,step:1,required:true}))),
      label('Pricing source and date',field('source',price.source,{required:true,placeholder:'Source URL, effective date and model tier'})),
      label('Input bound source and assumptions',field('bound_source',price.bound_source,{required:true,placeholder:'Source URL and documented billable input bound'})),
      el('button',{type:'button',class:'quiet',onClick:()=>row.remove()},'Remove model price'));
    rows.append(row);
  }
  policy.prices.forEach(addPrice);
  const form=el('form',{onSubmit:async event=>{
    event.preventDefault();
    const submit=form.querySelector('[type=submit]');submit.disabled=true;
    try {
      const prices=[...rows.children].map(row=>Object.fromEntries([...row.querySelectorAll('input,select')].map(input=>[input.name,input.name==='max_input_tokens'?Number(input.value):input.value])));
      await api('/live-policy',{method:'PUT',body:JSON.stringify({expected_version:policy.version,enabled:enabled.checked,budget_usd:budget.value||null,prices})});
      notice('Live controls saved. Review a fresh estimate before starting an experiment.');
      await mountUsage(host,{api,config,detail,onPolicy,notice});
    } catch(error) {notice(error.message,true);submit.disabled=false;}
  }},el('h2',{},'Live controls'),
    el('p',{class:'muted'},'Prices and bounds are supplied by the operator. The app does not certify their accuracy. Use rates that cover the chosen native API, region, routing mode and every applicable context tier. Cached or audio usage stops execution as unresolved.'),
    label('Cumulative website budget in USD',budget),
    el('p',{class:'small muted'},'Changing this amount preserves all prior spend estimates and held requests.'),rows,
    el('button',{type:'button',class:'secondary',onClick:()=>addPrice()},'Add model price'),
    label('Allow live website requests',enabled),
    el('p',{class:'small muted'},'Host permission is also required. Disabling stops the next request. Requests already sent may finish. Responses output limits include reasoning tokens.'),
    el('button',{type:'submit',class:'primary'},'Save live controls'));
  const analysis=el('section',{class:'usage-analysis'},el('h2',{},'Observed usage'));
  const choices=[['','All stored runs']];
  if(detail) {
    choices.push([`experiment_id=${detail.experiment_id}`,'Selected experiment']);
    for(const run of detail.runs) choices.push([`run_id=${run.run_id}`,`Candidate · ${run.label}`]);
  }
  const results=el('div',{});
  async function load(query='') {
    try {
      const report=await api(`/usage${query?'?'+query:''}`),m=report.metered;
      results.replaceChildren(el('p',{},`Website requests · ${m.calls}. Complete usage · ${m.complete_calls}/${m.calls}. Known input tokens · ${m.input_tokens}. Known output tokens · ${m.output_tokens}.`),
        el('p',{},`Complete configured-price estimate · ${money(m.estimated_usd)}. Known partial estimate · ${money(m.known_estimated_usd)}`),
        el('p',{class:'small muted'},report.note),
        ...report.breakdown.map(row=>el('p',{class:'usage-breakdown'},`${row.model_id} · ${row.api} · ${row.region} · ${row.role}. ${row.calls} calls, ${row.complete_calls} complete. Known tokens ${row.input_tokens} input / ${row.output_tokens} output. Estimate ${money(row.estimated_usd)}.`)),
        ...Object.entries(report.historical).map(([kind,row])=>el('p',{class:'small'},`${kind==='recorded'?'Recorded historical work':'Other stored work'} · ${row.outcomes} outcomes. ${row.complete_outcomes} with complete usage. Known tokens ${row.input_tokens} input / ${row.output_tokens} output. Cost unknown. Historical model roles are not inferred.`)));
    } catch(error) {notice(error.message,true);}
  }
  analysis.append(label('Usage scope',el('select',{onChange:event=>load(event.target.value)},choices.map(([value,title])=>el('option',{value},title)))),results);
  host.replaceChildren(el('h1',{},'Usage and live controls'),status,analysis,form);
  await load();
}
