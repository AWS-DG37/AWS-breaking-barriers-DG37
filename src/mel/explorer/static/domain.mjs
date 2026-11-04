export function parseJsonLines(text) {
  return text.split(/\r?\n/).flatMap((line, index) => {
    if (!line.trim()) return [];
    let value;
    try { value = JSON.parse(line); }
    catch { throw new Error(`Line ${index + 1} is not valid JSON.`); }
    if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error(`Line ${index + 1} must be a JSON object.`);
    return [value];
  });
}

export const displayValue = value => value === null || value === undefined ? 'Not recorded' : String(value);
export const questionAssessments = (assessments, runId, insightId, itemId) => assessments.filter(a => a.scope === 'question' && a.run_id === runId && a.insight_id === insightId && a.item_id === itemId);
export const eventsForStage = (events, stage) => events.filter(event => event.stage === stage);
export const selectedModel = (choice, defaults) => ({...defaults,model_id:choice.model_id,api:choice.api});
