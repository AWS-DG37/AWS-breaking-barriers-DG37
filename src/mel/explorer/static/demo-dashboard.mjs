import {el} from './dom.mjs';
// Demo dashboard: what a Digital Green team could see after 1,000 follow-up surveys.
// ILLUSTRATIVE MOCK DATA. No farmer answered these surveys; the page says so on screen.
// The numbers are hand-made to be internally consistent: every breakdown adds up to the
// same 1,000 surveys sent, 640 answered and 455 farmers who acted on the advice.

export const MOCK = {
  conversations: 6999, eligible: 3940, sent: 1000, answered: 640, acted: 455,
  outcome: {improved: 264, same: 121, worse: 70},          // of the 455 who acted
  followUps: [                                              // 118 answers that need a person
    {code:'advice_did_not_work', label:'Advice did not work', count:52, route:'Call back from an agronomist'},
    {code:'could_not_get_input', label:'Could not get the input', count:38, route:'Tell the local input dealer network'},
    {code:'crop_or_animal_damaged', label:'Crop or animal damaged', count:19, route:'Priority call from extension staff'},
    {code:'could_not_reach_expert', label:'Could not reach a vet or expert', count:9, route:'Escalate to the Pashu Sakhi or KVK network'},
  ],
  channels: [                                               // sent, answered
    {name:'Voice call (IVR)', sent:380, answered:266},
    {name:'WhatsApp', sent:300, answered:204},
    {name:'FarmerChat app', sent:160, answered:96},
    {name:'Telegram', sent:50, answered:24},
    {name:'Web', sent:110, answered:50},
  ],
  crops: [                                                  // of farmers who acted
    {name:'Wheat', improved:75, same:30, worse:15},
    {name:'Mustard', improved:60, same:22, worse:13},
    {name:'Livestock', improved:47, same:27, worse:16},
    {name:'Potato', improved:38, same:20, worse:12},
    {name:'Cabbage', improved:24, same:13, worse:8},
    {name:'Lentil', improved:20, same:9, worse:6},
  ],
  districts: [                                              // surveys sent, answers needing follow-up
    {name:'Saran', sent:160, followUps:28},
    {name:'Gaya', sent:140, followUps:22},
    {name:'Muzaffarpur', sent:150, followUps:17},
    {name:'Vaishali', sent:180, followUps:18},
    {name:'Patna', sent:210, followUps:19},
    {name:'Nalanda', sent:160, followUps:14},
  ],
  hindiShare: 0.88,
  medianDaysToAnswer: 1.8,
};

const pct = (part, whole) => whole ? Math.round(100 * part / whole) : 0;
const n = value => Number(value).toLocaleString('en-IN');
const sum = list => list.reduce((a, b) => a + b, 0);

function tile(value, label, note) {
  return el('div', {class:'dash-tile'}, el('strong', {}, value), el('span', {}, label), note ? el('small', {}, note) : null);
}

function bar(label, value, max, text, tone='') {
  return el('div', {class:'dash-bar'},
    el('span', {class:'dash-bar-label'}, label),
    el('span', {class:'dash-bar-track', role:'img', 'aria-label':`${label}: ${text}`},
      el('span', {class:`dash-bar-fill ${tone}`, style:`width:${Math.max(2, 100 * value / max)}%`})),
    el('span', {class:'dash-bar-value'}, text));
}

function funnel(m) {
  const steps = [
    ['FarmerChat conversations', m.conversations, 'All advice given'],
    ['Worth a follow-up', m.eligible, 'Passed the rules (R8)'],
    ['Surveys sent', m.sent, 'Written by AI, checked by a second AI'],
    ['Farmers answered', m.answered, `${pct(m.answered, m.sent)}% response`],
    ['Acted on the advice', m.acted, `${pct(m.acted, m.answered)}% of those who answered`],
    ['Problem got better', m.outcome.improved, `${pct(m.outcome.improved, m.acted)}% of those who acted`],
  ];
  return el('section', {class:'dash-card dash-wide'}, el('h2', {}, 'From advice to impact'),
    el('div', {class:'dash-funnel'}, steps.map(([label, value, note], i) =>
      el('div', {class:'dash-step', style:`--h:${Math.round(Math.max(10, 110 * value / steps[0][1]))}px`},
        el('div', {class:`dash-step-bar s${i}`}), el('strong', {}, n(value)), el('span', {}, label), el('small', {}, note)))));
}

function outcomes(m) {
  const {improved, same, worse} = m.outcome, total = improved + same + worse;
  return el('section', {class:'dash-card'}, el('h2', {}, 'Did it work?'),
    el('p', {class:'small muted'}, `${n(total)} farmers who acted on the advice`),
    el('div', {class:'dash-stack', role:'img', 'aria-label':`Improved ${pct(improved,total)}%, same ${pct(same,total)}%, worse ${pct(worse,total)}%`},
      el('span', {class:'good', style:`flex:${improved}`}, `${pct(improved, total)}%`),
      el('span', {class:'mid', style:`flex:${same}`}, `${pct(same, total)}%`),
      el('span', {class:'bad', style:`flex:${worse}`}, `${pct(worse, total)}%`)),
    el('div', {class:'dash-legend'}, el('span', {class:'good'}, 'Better'), el('span', {class:'mid'}, 'Same'), el('span', {class:'bad'}, 'Worse')),
    el('h3', {}, 'By crop or animal'),
    m.crops.map(c => {
      const t = c.improved + c.same + c.worse;
      return el('div', {class:'dash-crop'}, el('span', {}, c.name),
        el('span', {class:'dash-stack thin', role:'img', 'aria-label':`${c.name}: ${pct(c.improved,t)}% better`},
          el('span', {class:'good', style:`flex:${c.improved}`}), el('span', {class:'mid', style:`flex:${c.same}`}), el('span', {class:'bad', style:`flex:${c.worse}`})),
        el('span', {class:'dash-bar-value'}, `${pct(c.improved, t)}% better`));
    }));
}

function followUps(m) {
  const total = sum(m.followUps.map(f => f.count)), max = Math.max(...m.followUps.map(f => f.count));
  return el('section', {class:'dash-card'}, el('h2', {}, 'Farmers who need a person'),
    el('p', {class:'small muted'}, `${n(total)} answers (${pct(total, m.answered)}% of answers) were routed to a human, from the follow-up code on the answer the farmer tapped`),
    m.followUps.map(f => el('div', {class:'dash-follow'},
      bar(f.label, f.count, max, n(f.count), 'warn'),
      el('small', {class:'muted'}, `Next step: ${f.route}`))));
}

function channels(m) {
  return el('section', {class:'dash-card'}, el('h2', {}, 'Response by channel'),
    el('p', {class:'small muted'}, 'Share of sent surveys that were answered'),
    m.channels.map(c => bar(c.name, c.answered, c.sent, `${pct(c.answered, c.sent)}% of ${n(c.sent)}`)));
}

function districts(m) {
  const rows = [...m.districts].sort((a, b) => b.followUps / b.sent - a.followUps / a.sent);
  const avg = sum(m.districts.map(d => d.followUps)) / sum(m.districts.map(d => d.sent));
  return el('section', {class:'dash-card'}, el('h2', {}, 'Where help is needed'),
    el('p', {class:'small muted'}, `Answers needing follow-up per district. Average ${Math.round(avg * 100)}%.`),
    el('table', {class:'dash-table'},
      el('thead', {}, el('tr', {}, el('th', {scope:'col'}, 'District'), el('th', {scope:'col'}, 'Surveys'), el('th', {scope:'col'}, 'Need follow-up'))),
      el('tbody', {}, rows.map(d => {
        const rate = d.followUps / d.sent, hot = rate > avg * 1.4;
        return el('tr', {class:hot ? 'hot' : ''}, el('th', {scope:'row'}, d.name), el('td', {}, n(d.sent)),
          el('td', {}, `${Math.round(rate * 100)}%`, hot ? el('span', {class:'badge warning'}, 'hotspot') : null));
      }))));
}

function learnings() {
  const card = (title, body, action) => el('div', {class:'dash-learning'}, el('strong', {}, title), el('p', {}, body), el('small', {}, action));
  return el('section', {class:'dash-card dash-wide'}, el('h2', {}, 'What the team learns'),
    el('div', {class:'dash-learnings'},
      card('Mustard aphids in Saran', '1 in 3 farmers who sprayed say the insects increased, twice the rate elsewhere.', 'Check spray quality or resistance. Send a KVK agronomist.'),
      card('Inputs are the bottleneck', '38 farmers could not get the recommended input; 24 of them are in Gaya and Saran.', 'Share with input dealers before the next spray window.'),
      card('Voice reaches more farmers', 'Voice calls get 70% answers against 45% on web.', 'Default to voice for farmers who have not answered by text.'),
      card('Vets are hard to reach', '9 livestock owners could not reach a vet after being told to call one.', 'Escalate to the Pashu Sakhi network in those villages.')));
}

export function renderDashboard(m = MOCK) {
  const followUpTotal = sum(m.followUps.map(f => f.count));
  return [
    el('p', {class:'dash-mock', role:'note'}, el('strong', {}, 'Illustrative mock data. '),
      'This shows what the dashboard could look like after 1,000 surveys. No farmer answered these surveys.'),
    el('div', {class:'dash-tiles'},
      tile(n(m.sent), 'Surveys sent', `${n(m.conversations)} conversations reviewed`),
      tile(`${pct(m.answered, m.sent)}%`, 'Answered', `${n(m.answered)} farmers · median ${m.medianDaysToAnswer} days`),
      tile(`${pct(m.acted, m.answered)}%`, 'Acted on the advice', `${n(m.acted)} farmers`),
      tile(`${pct(m.outcome.improved, m.acted)}%`, 'Problem got better', `${n(m.outcome.improved)} farmers`),
      tile(n(followUpTotal), 'Sent to a person', `${pct(followUpTotal, m.answered)}% of answers`),
      tile(`${Math.round(m.hindiShare * 100)}%`, 'Answered in Hindi', `${Math.round((1 - m.hindiShare) * 100)}% English`)),
    el('div', {class:'dash-grid'}, funnel(m), outcomes(m), followUps(m), channels(m), districts(m), learnings()),
  ];
}
