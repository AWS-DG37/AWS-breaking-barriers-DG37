// Survey state for the voice page: one survey.schema.json in, one response.schema.json out.
// No DOM and no network, so tests can run it in Node (tests/test_voice_session.py).

const SKIPPED = 'skipped';

// What the page says aloud, per language. The farmer answers only by tapping.
// voice/render_audio.py records every line once with ElevenLabs.
export const VOICE_LINES = {
  en: {
    opening: n => `Hello, this is FarmerChat. I have ${n} short questions about the advice you got. Tap your answer on the screen.`,
    closing: 'Thank you. Your answers help us give better advice.',
    closing_if_follow_up: 'Thank you. We have noted this. FarmerChat will contact you again about it.',
    or: 'Or',
    stop: '.',
    scale: (min, max) => `Tap a number from ${min} to ${max}.`,
  },
  hi: {
    opening: n => `नमस्ते, यह FarmerChat है। आपको मिली सलाह के बारे में ${n} छोटे सवाल हैं। स्क्रीन पर अपना जवाब छूएँ।`,
    closing: 'धन्यवाद। आपके जवाब से हम और अच्छी सलाह दे पाएँगे।',
    closing_if_follow_up: 'धन्यवाद। हमने यह नोट कर लिया है। FarmerChat आपसे इस बारे में फिर बात करेगा।',
    or: 'या',
    stop: '।',
    scale: (min, max) => `${min} से ${max} तक कोई नंबर छूएँ।`,
  },
};

// The participant pack's voice channel (scenarios/*/render/*/voice.json) waits 8 seconds
// for an answer and repeats the question once. The page uses the same rule for taps.
export const REPEAT = {timeout_seconds: 8, repeats: 1};

export const voiceLanguage = survey => (survey.language.split('-')[0] === 'hi' ? 'hi' : 'en');

// One question as spoken: the prompt, then each choice as its own short sentence, so a
// farmer who cannot read can tap. Labels may hold commas ("Yes, easily"), so a comma
// list would run them together.
export function spokenQuestion(survey, item) {
  const lines = VOICE_LINES[voiceLanguage(survey)];
  if (item.type === 'survey-scale') return `${item.prompt} ${lines.scale(item.scale_min, item.scale_max)}`;
  const labels = item.options.map(o => o.label);
  const choices = [...labels.slice(0, -1), `${lines.or} ${labels.at(-1)}`];
  return `${item.prompt} ${choices.map(c => c + lines.stop).join(' ')}`;
}

export function spokenLines(survey) {
  const lines = VOICE_LINES[voiceLanguage(survey)];
  return {
    opening: lines.opening(survey.items.length),
    items: Object.fromEntries(survey.items.map(item => [item.item_id, spokenQuestion(survey, item)])),
    closing: lines.closing,
    closing_if_follow_up: lines.closing_if_follow_up,
  };
}

// The recorded clip for one line: the first 16 hex digits of SHA-256("<language>\n<text>").
// voice/render_audio.py names its files the same way.
export async function clipName(language, text) {
  const bytes = new TextEncoder().encode(`${language}\n${text}`);
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes));
  return [...digest].map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16) + '.mp3';
}

export function optionsFor(item) {
  if (item.type !== 'survey-scale') return item.options;
  return Array.from({length: item.scale_max - item.scale_min + 1}, (_, i) => {
    const value = item.scale_min + i;
    return {value, label: String(value)};
  });
}

export function createSession(survey, {now = new Date()} = {}) {
  const items = Object.fromEntries(survey.items.map(item => [item.item_id, {
    item, result: 'not_reached', value: null, raw_input: null, input_kind: null, attempts: 0, follow_up: null,
  }]));
  return {survey, items, openedAt: now, answeredAt: null};
}

export function markShown(session, itemId) {
  const entry = session.items[itemId];
  if (entry && entry.result === 'not_reached') entry.result = 'skipped';
}

function nextItemId(session, itemId) {
  const ids = session.survey.items.map(item => item.item_id);
  const after = ids.slice(ids.indexOf(itemId) + 1);
  return after.find(id => !['answered', 'skipped'].includes(session.items[id].result)) ?? null;
}

function followUpFor(item, option) {
  if (item.type === 'survey-scale') {
    return item.follow_up_at_or_below != null && option.value <= item.follow_up_at_or_below
      ? item.follow_up_reason : null;
  }
  return option.follow_up ?? null;
}

// Record one tapped answer.
export function recordAnswer(session, {item_id, value, heard = null, input_kind = 'sheet_tap', now = new Date()}) {
  const entry = session.items[item_id];
  if (!entry) {
    return {accepted: false, reason: `Unknown item_id "${item_id}". Use one of: ${Object.keys(session.items).join(', ')}.`};
  }
  entry.attempts = Math.min(entry.attempts + 1, 5);
  entry.raw_input = heard == null ? null : String(heard).slice(0, 256);
  entry.input_kind = input_kind;
  if (value === SKIPPED) {
    Object.assign(entry, {result: 'skipped', value: null, follow_up: null});
    return {accepted: true, skipped: true, next_item_id: nextItemId(session, item_id)};
  }
  const option = optionsFor(entry.item).find(o => String(o.value) === String(value).trim());
  if (!option) {
    if (entry.result !== 'answered') entry.result = 'invalid';
    const choices = optionsFor(entry.item).map(o => o.value).join(', ');
    return {accepted: false, reason: `"${value}" is not a choice for ${item_id}. Choices: ${choices}, or ${SKIPPED}.`};
  }
  Object.assign(entry, {result: 'answered', value: option.value, follow_up: followUpFor(entry.item, option)});
  session.answeredAt = now;
  return {accepted: true, label: option.label, follow_up: entry.follow_up, next_item_id: nextItemId(session, item_id)};
}

export function istTime(date) {
  return new Date(date.getTime() + 330 * 60_000).toISOString().slice(0, 19) + '+05:30';
}

// Build the web answer record for the sheet as it stands, as if the farmer closed it now.
// Codes follow the participant pack's rules/outcome_codes.json for the web channel: no answer
// is sheet_dismissed (give up), some answers is sheet_closed_partway, all is finished.
export function finishSurvey(session, {now = new Date()} = {}) {
  const {survey} = session;
  const entries = survey.items.map(item => session.items[item.item_id]);
  const answered = entries.filter(e => e.result === 'answered');
  const reasons = [...new Set(answered.map(e => e.follow_up).filter(Boolean))];
  const complete = answered.length === entries.length;
  const dismissed = answered.length === 0;
  let outcome = complete ? 'completed' : 'partial';
  if (reasons.length) outcome = 'negative_outcome';
  else if (dismissed) outcome = 'declined';
  return {
    response_key: `${survey.survey_id}-web-${now.getTime()}`,
    survey_id: survey.survey_id,
    farmer_id: survey.farmer_id,
    source_insight_ids: survey.source_insight_ids,
    channel: 'web',
    language: survey.language,
    status: outcome,
    failure_code: dismissed ? 'sheet_dismissed' : null,
    answers: Object.fromEntries(answered.map(e => [e.item.item_id, e.value])),
    items: entries.map(e => ({
      item_id: e.item.item_id, item_type: e.item.type, result: e.result, value: e.value,
      raw_input: e.raw_input, input_kind: e.input_kind, attempts: e.attempts,
    })),
    complete,
    delivery_attempt: 1,
    timestamps: {
      sent: null, delivered: null, opened: istTime(session.openedAt),
      answered: session.answeredAt ? istTime(session.answeredAt) : null,
    },
    recorded_at: istTime(now),
    answered_by: survey.farmer_id,
    sheet_outcome: complete ? 'finished' : dismissed ? 'dismissed' : 'closed_partway',
    raw_event: dismissed ? 'sheet closed with no answer' : null,
    retry_decision: dismissed ? 'give_up' : 'none',
    retry_after_minutes: null,
    counts_toward_cap: true,
    farmerchat_reply: null,
    needs_follow_up: reasons.length > 0,
    follow_up_reasons: reasons,
  };
}
