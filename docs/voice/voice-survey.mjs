// Voice survey page: shows one survey.schema.json, reads each question aloud, and records taps.
// The farmer never speaks. With no tap within REPEAT.timeout_seconds after a question ends,
// the question plays again, REPEAT.repeats times. Clips come from voice/render_audio.py.
import {REPEAT, createSession, finishSurvey, markShown, optionsFor, recordAnswer, spokenLines, clipName, voiceLanguage} from './survey-session.mjs';
import {englishSurvey} from './generated/demo-language.mjs';

const SCENARIOS = {
  '1-mustard-aphid-spray': 'Mustard aphids: spray within 2 days',
  '2-wheat-irrigation-timing': 'Wheat: first irrigation timing',
  '3-potato-blight-wait': 'Potato blight: wait and watch',
  '4-cow-vet-escalation': 'Sick cow: see a vet',
  '5-wheat-variety-plan': 'Wheat: which seed to sow',
};
const COPY = {
  en: {
    start: 'Read aloud', stop: 'Stop voice', back: 'Back', next: 'Next', finish: 'Finish',
    you: 'Your answer', followUp: 'FarmerChat will follow up on this answer.', thanks: 'Thank you',
    progress: (n, total) => `${n} of ${total}`,
    off: 'Voice off. Tap to answer', reading: 'Reading', waiting: 'Waiting for a tap', answered: 'Answered',
  },
  hi: {
    start: 'सुनें', stop: 'आवाज़ बंद', back: 'पीछे', next: 'आगे', finish: 'पूरा करें',
    you: 'आपका जवाब', followUp: 'FarmerChat इस जवाब पर आपसे फिर बात करेगा।', thanks: 'धन्यवाद',
    // "3 में से 1" is "1 of 3": the total comes first, as in the pack's step_label.
    progress: (n, total) => `${total} में से ${n}`,
    off: 'आवाज़ बंद। जवाब छूएँ', reading: 'पढ़ रहे हैं', waiting: 'जवाब का इंतज़ार', answered: 'जवाब मिला',
  },
};

const $ = id => document.getElementById(id);
// speech counts playbacks. A finished or failed clip whose number is no longer current was
// cut off by a tap, a page change or Stop, and must not start the repeat timer.
// load counts survey loads the same way: only the latest choice may install its survey.
const state = {lang: 'en', generated: [], survey: null, session: null, index: 0, done: false,
  english: null, englishText: false, englishAudio: false,
  voiceOn: false, speech: 0, load: 0, audio: null, timer: null, repeats: 0, mode: 'off', caption: ''};

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value !== false && value != null) node.setAttribute(key, value === true ? '' : value);
  }
  node.append(...children.flat().filter(c => c != null));
  return node;
}

const displaySurvey = () => state.englishText ? state.english : state.survey;
const audioSurvey = () => state.englishAudio ? state.english : state.survey;
const copy = () => COPY[voiceLanguage(displaySurvey())];
const currentItem = () => displaySurvey().items[state.index];
const answered = () => state.session.items[currentItem().item_id].result === 'answered';

// ---- Voice ------------------------------------------------------------------------------

function silence() {
  state.speech += 1;
  clearTimeout(state.timer);
  state.timer = null;
  state.audio?.pause();
  state.audio = null;
  globalThis.speechSynthesis?.cancel();
}

// Play one line: the recorded ElevenLabs clip, or the browser's own voice when a survey has
// no recording (a survey loaded with ?survey=). Resolves true when the line played to the end.
async function speak(text) {
  silence();
  const speech = state.speech;
  Object.assign(state, {mode: 'reading', caption: text});
  renderVoice();
  const name = await clipName(audioSurvey().language, text);
  if (speech !== state.speech) return false;
  const ended = await new Promise(resolve => {
    // A missing clip fires both onerror and the play() rejection; settle only once, so the
    // browser voice reads the line one time.
    let settled = false;
    const settle = outcome => {
      if (settled) return;
      settled = true;
      resolve(outcome());
    };
    const audio = new Audio(`audio/${name}`);
    state.audio = audio;
    audio.onended = () => settle(() => true);
    audio.onerror = () => settle(() => speakWithBrowser(text, speech));
    audio.play().catch(() => settle(() => speakWithBrowser(text, speech)));
  });
  return ended && speech === state.speech;
}

function speakWithBrowser(text, speech) {
  if (speech !== state.speech || !globalThis.speechSynthesis) return false;
  addEvent('No recording for this line: using the browser voice');
  return new Promise(resolve => {
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = audioSurvey().language;
    utterance.onend = () => resolve(true);
    utterance.onerror = () => resolve(false);
    speechSynthesis.speak(utterance);
  });
}

async function readQuestion() {
  if (!state.voiceOn || state.done) return;
  const item = currentItem();
  const text = spokenLines(audioSurvey()).items[item.item_id];
  addEvent(state.repeats ? `No tap in ${REPEAT.timeout_seconds} s: read ${item.item_id} again` : `Read ${item.item_id}`);
  if (!(await speak(text))) return;
  if (answered()) {
    state.mode = 'answered';
    renderVoice();
    return;
  }
  state.mode = 'waiting';
  renderVoice();
  const speech = state.speech;
  state.timer = setTimeout(() => {
    if (speech !== state.speech || answered()) return;
    if (state.repeats < REPEAT.repeats) {
      state.repeats += 1;
      readQuestion();
    } else {
      addEvent(`No tap after ${REPEAT.repeats + 1} readings: waiting for ${item.item_id}`);
    }
  }, REPEAT.timeout_seconds * 1000);
}

async function startVoice() {
  Object.assign(state, {voiceOn: true, repeats: 0});
  addEvent('Voice on');
  // A tap, Stop or survey change during the opening cuts it off; then read nothing more.
  if (state.index === 0 && !state.done && !(await speak(spokenLines(audioSurvey()).opening))) return;
  readQuestion();
}

function stopVoice() {
  silence();
  Object.assign(state, {voiceOn: false, mode: 'off', caption: ''});
  addEvent('Voice off');
  renderVoice();
}

// ---- Survey ------------------------------------------------------------------------------

// The picker holds "gen:<file>" for surveys the MEL agent wrote (docs/voice/generated, made by
// voice/export_surveys.py) and "ref:<scenario>" for the participant pack's reference surveys.
function surveySource() {
  const custom = new URLSearchParams(location.search).get('survey');
  if (custom) return {url: custom, note: `Survey loaded from ${custom}.`};
  const [kind, id] = $('scenario').value.split(/:(.*)/s);
  if (kind === 'gen') {
    const entry = state.generated.find(e => e.file === id);
    return {url: `generated/${id}`, generated: true, note:
      `Written by the MEL agent (${entry.model_id}, config ${entry.config_version}, run ${entry.experiment_id?.slice(0, 8)}) ` +
      `for insight ${entry.insight_id}. Advice the farmer got: "${entry.advice_summary}"`};
  }
  return {url: `../participant-pack/json/scenarios/${id}/survey.${state.lang}-IN.json`,
    note: "Reference survey from Digital Green's participant pack. The MEL agent did not write it."};
}

// Returns false when a newer survey choice replaced this load while it was in flight.
async function loadSurvey(load) {
  const {url, generated, note} = surveySource();
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Could not load the survey (${response.status}) from ${url}`);
  const survey = await response.json();
  if (load !== state.load) return false;
  state.survey = survey;
  state.english = englishSurvey(survey);
  if (!state.english) Object.assign(state, {englishText: false, englishAudio: false});
  state.lang = voiceLanguage(state.survey);
  document.querySelector(`input[name=lang][value=${state.lang}]`).checked = true;
  document.querySelector('.lang').disabled = Boolean(generated);
  $('provenance').textContent = note;
  state.session = createSession(state.survey);
  Object.assign(state, {index: 0, done: false, repeats: 0});
  markShown(state.session, currentItem().item_id);
  document.documentElement.lang = state.survey.language;
  render();
  return true;
}

function goTo(index) {
  silence();
  Object.assign(state, {index, repeats: 0});
  markShown(state.session, currentItem().item_id);
  render();
  readQuestion();
}

async function finish() {
  silence();
  state.done = true;
  render();
  if (!state.voiceOn) return;
  const record = finishSurvey(state.session);
  const lines = spokenLines(audioSurvey());
  // The voice ends with the survey. A Stop or survey change during the closing owns the state.
  if (!(await speak(record.needs_follow_up ? lines.closing_if_follow_up : lines.closing))) return;
  Object.assign(state, {voiceOn: false, mode: 'off', caption: ''});
  addEvent('Voice off');
  renderVoice();
}

function tap(option) {
  silence();
  const item = currentItem();
  recordAnswer(state.session, {item_id: item.item_id, value: option.value, heard: option.label});
  addEvent(`Tapped ${item.item_id} = ${option.value}`);
  if (state.voiceOn) Object.assign(state, {mode: 'answered', caption: ''});
  render();
}

// ---- Rendering ---------------------------------------------------------------------------

function renderCard() {
  const c = copy();
  const card = $('card');
  if (state.done) {
    const record = finishSurvey(state.session);
    const lines = spokenLines(displaySurvey());
    card.replaceChildren(h('h2', {tabindex: -1}, c.thanks),
      h('p', {}, record.needs_follow_up ? lines.closing_if_follow_up : lines.closing));
    $('progress').textContent = '';
    return;
  }
  const item = currentItem();
  const entry = state.session.items[item.item_id];
  const scale = item.type === 'survey-scale';
  const options = optionsFor(item).map(option => h('button', {
    type: 'button', class: 'option', 'aria-pressed': String(entry.value === option.value), onClick: () => tap(option),
  }, option.label));
  const chosen = optionsFor(item).find(o => o.value === entry.value);
  const last = state.index === state.survey.items.length - 1;
  card.replaceChildren(...[
    h('h2', {}, item.prompt),
    h('div', {class: scale ? 'options scale' : 'options', role: 'group', 'aria-label': item.prompt}, options),
    h('p', {class: 'echo'}, chosen ? `${c.you}: ${chosen.label}` : ''),
    entry.follow_up ? h('p', {class: 'follow'}, c.followUp) : null,
    h('div', {class: 'nav'},
      h('button', {type: 'button', class: 'back', disabled: state.index === 0, onClick: () => goTo(state.index - 1)}, c.back),
      h('button', {type: 'button', class: 'next', disabled: !chosen,
        onClick: () => (last ? finish() : goTo(state.index + 1))}, last ? c.finish : c.next)),
  ].filter(Boolean));
  $('progress').textContent = c.progress(state.index + 1, state.survey.items.length);
}

function renderVoice() {
  const c = copy();
  const button = $('voice');
  button.textContent = state.voiceOn ? c.stop : c.start;
  button.dataset.live = String(state.voiceOn);
  button.dataset.mode = state.mode;
  $('status').textContent = c[state.mode] ?? c.off;
  $('caption').textContent = state.caption;
  $('caption').lang = audioSurvey().language;
}

function renderRecord() {
  const record = finishSurvey(state.session);
  $('record').textContent = JSON.stringify(record, null, 2);
  $('record-status').textContent = state.done ? record.status : 'draft';
  $('record-status').dataset.status = state.done ? record.status : '';
  $('record-note').textContent = state.done ? 'Sent when the farmer pressed Finish.'
    : 'Draft: the record FarmerChat would get if the farmer closed the sheet now.';
}

function render() {
  const nativeEnglish = voiceLanguage(state.survey) === 'en';
  for (const [id, enabled] of [['english-text', state.englishText], ['english-audio', state.englishAudio]]) {
    $(id).checked = nativeEnglish || enabled;
    $(id).disabled = !state.english || nativeEnglish;
  }
  $('translation-note').textContent = !state.english
    ? 'English demo translation is unavailable for this survey.'
    : nativeEnglish ? 'This survey is already in English.'
    : 'Text and audio can use different languages. Answers keep the original survey language.';
  $('card').lang = displaySurvey().language;
  $('progress').lang = displaySurvey().language;
  renderCard();
  renderVoice();
  renderRecord();
}

function addEvent(text) {
  const list = $('log');
  list.querySelector('.muted')?.remove();
  const time = new Date().toLocaleTimeString([], {minute: '2-digit', second: '2-digit'});
  list.append(h('li', {}, h('time', {}, time), ' ', text));
  list.scrollTop = list.scrollHeight;
}

function showError(message) {
  const box = $('error');
  box.textContent = message;
  box.hidden = false;
  setTimeout(() => { box.hidden = true; }, 8000);
}

async function restart() {
  const load = ++state.load;
  silence();
  $('log').replaceChildren(h('li', {class: 'muted'}, 'Each reading, repeat and tap appears here.'));
  try {
    if ((await loadSurvey(load)) && state.voiceOn) startVoice();
  } catch (error) {
    if (load === state.load) showError(error.message);
  }
}

async function init() {
  const params = new URLSearchParams(location.search);
  const select = $('scenario');
  state.generated = await fetch('generated/index.json').then(r => (r.ok ? r.json() : []), () => []);
  select.append(
    h('optgroup', {label: 'Written by the MEL agent'},
      state.generated.map(e => h('option', {value: `gen:${e.file}`}, e.label))),
    h('optgroup', {label: 'Reference surveys (participant pack)'},
      Object.entries(SCENARIOS).map(([id, label]) => h('option', {value: `ref:${id}`}, label))));
  const wanted = params.get('scenario');
  const value = wanted in SCENARIOS ? `ref:${wanted}` : wanted;
  if ([...select.options].some(o => o.value === value)) select.value = value;
  if (params.get('lang') === 'hi') state.lang = 'hi';
  if (params.has('survey')) select.closest('label').hidden = true;
  select.addEventListener('change', restart);
  for (const radio of document.querySelectorAll('input[name=lang]')) {
    radio.addEventListener('change', () => { state.lang = radio.value; restart(); });
  }
  $('voice').addEventListener('click', () => (state.voiceOn ? stopVoice() : startVoice()));
  $('english-text').addEventListener('change', event => {
    state.englishText = event.target.checked;
    render();
  });
  $('english-audio').addEventListener('change', event => {
    silence();
    state.englishAudio = event.target.checked;
    Object.assign(state, {repeats: 0, caption: '', mode: state.voiceOn ? 'waiting' : 'off'});
    render();
    if (state.voiceOn) {
      if (state.done) finish();
      else readQuestion();
    }
  });
  window.addEventListener('pagehide', silence);
  restart();
}

init();
