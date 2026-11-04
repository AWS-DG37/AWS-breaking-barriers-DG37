export const displayValue = value => value === null || value === undefined ? 'Not recorded' : String(value);
export const questionAssessments = (assessments, runId, insightId, itemId) => assessments.filter(a => a.scope === 'question' && a.run_id === runId && a.insight_id === insightId && a.item_id === itemId);
export const eventsForStage = (events, stage) => events.filter(event => event.stage === stage);

// Contiguous visits preserve the execution path, including revisions and early exits.
// Keep stage-local event indices so the inspector can open the exact saved event.
export function journeyVisits(events = []) {
  const counts = new Map(), visits = [];
  for (const event of events) {
    const index = counts.get(event.stage) || 0;
    counts.set(event.stage, index + 1);
    const phase = event.stage === 'select' && event.data?.insight ? 'input' : event.stage;
    let visit = visits.at(-1);
    if (!visit || visit.phase !== phase || visit.attempt !== event.attempt) {
      visit = {stage:event.stage, phase, attempt:event.attempt, events:[], eventIndices:[]};
      visits.push(visit);
    }
    visit.events.push(event);
    visit.eventIndices.push(index);
  }
  return visits;
}

// One plain sentence per outcome for a run's counts; zero counts are left out.
export function summarizeCounts(counts = {}) {
  const n = key => counts?.[key] || 0;
  const s = (count, one, many) => `${count} ${count === 1 ? one : many}`;
  const clauses = [
    n('accepted') && `${s(n('accepted'), 'survey is', 'surveys are')} ready to send.`,
    n('skipped') && `The rules skipped ${s(n('skipped'), 'insight', 'insights')}.`,
    n('rejected') && `The reviewer rejected ${s(n('rejected'), 'draft', 'drafts')}.`,
    n('failed') && `${s(n('failed'), 'insight', 'insights')} failed.`,
  ].filter(Boolean);
  return clauses.length ? clauses.join(' ') : 'No results yet.';
}

// Value at a dotted path, e.g. read(definition, 'config.generation.model_id').
export const read = (value, path) => path.split('.').reduce((node, key) => node?.[key], value);

export function write(value, path, next) {
  const keys = path.split('.'), last = keys.pop();
  const parent = keys.reduce((node, key) => (node[key] ??= {}), value);
  parent[last] = next;
}

// Paths whose values differ between two definitions.
export const changedPaths = (before, after, paths) =>
  paths.filter(path => JSON.stringify(read(before, path)) !== JSON.stringify(read(after, path)));

// Line diff (longest common subsequence): [{op:' '|'-'|'+', text}] turning a into b.
export function lineDiff(a, b) {
  const x = a.split('\n'), y = b.split('\n');
  const lcs = Array.from({length:x.length + 1}, () => new Array(y.length + 1).fill(0));
  for (let i = x.length - 1; i >= 0; i--)
    for (let j = y.length - 1; j >= 0; j--)
      lcs[i][j] = x[i] === y[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) { out.push({op:' ', text:x[i]}); i++; j++; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) out.push({op:'-', text:x[i++]});
    else out.push({op:'+', text:y[j++]});
  }
  while (i < x.length) out.push({op:'-', text:x[i++]});
  while (j < y.length) out.push({op:'+', text:y[j++]});
  return out;
}
