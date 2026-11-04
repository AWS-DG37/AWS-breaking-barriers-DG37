import {el} from './dom.mjs';
// The detail dial: how much the console shows. Elements carry data-level 1-3 and
// style.css hides those above <html data-detail>. A level change never re-renders.
// The preference is per browser. A section's floor raises the effective level
// while it is open, so a deep link to Compare or Configure stays usable.

export const LEVELS = ['Essential','Detailed','Everything'];
const KEY = 'mel.detail';
const FLOORS = {compare:2, configure:3};
let section = 'generate';
let buttons = [];

export function level() {
  try {
    const n = Number(localStorage.getItem(KEY));
    return Number.isInteger(n) && n >= 1 && n <= LEVELS.length ? n : 1;
  } catch { return 1; }
}

export function setLevel(n) {
  const clamped = Math.min(LEVELS.length, Math.max(1, Math.round(Number(n)) || 1));
  try { localStorage.setItem(KEY, String(clamped)); } catch { /* Best effort: the dial still applies for this page. */ }
  apply(section, clamped);
}

// The stored value can be unreadable, so setLevel passes the level it just chose.
export function apply(name = section, chosen = level()) {
  section = name;
  const effective = Math.max(chosen, FLOORS[name] || 1);
  document.documentElement.dataset.detail = String(effective);
  buttons.forEach((button, i) => button.setAttribute('aria-pressed', String(i + 1 === effective)));
  return effective;
}

export function mountDial(host) {
  buttons = LEVELS.map((label, i) => el('button', {type:'button', 'aria-pressed':'false', onClick:() => setLevel(i + 1)}, label));
  host.replaceChildren(el('div', {class:'detail-dial', role:'group', 'aria-label':'Detail level'}, buttons));
  apply();
}

// Tags a node built elsewhere, e.g. atLevel(costSummary(...), 2).
export function atLevel(node, n) {
  node?.setAttribute('data-level', String(n));
  return node;
}
