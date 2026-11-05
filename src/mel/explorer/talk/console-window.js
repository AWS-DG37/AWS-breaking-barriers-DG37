// Slide 4: tap the MEL Console card in the AWS diagram to open the live console
// (fieldecho.org/#demo) over the deck. The card morphs into a small browser window
// showing the live site, which then grows to fill the screen. "Back to the talk"
// plays it in reverse. The console is on this same origin, so it runs in an iframe
// with the presenter's existing sign-in cookie.
(() => {
  const card = document.getElementById('ad-console');
  if (!card) return;
  const box = card.querySelector('rect');
  const scene = card.closest('.scene');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const CONSOLE = new URL('/#demo', location.href).href;
  const CHROME = 40;  // height of the window's address bar, px
  const INSET = 14;   // gap around the full-size window, px
  const EASE = 'cubic-bezier(.19,1,.22,1)';  // the deck's --ease
  const behind = ['deck', 'bar', 'foot'].map(name => document.getElementById(name) || document.querySelector('.' + name));

  card.setAttribute('role', 'button');
  card.setAttribute('tabindex', '0');
  card.setAttribute('aria-label', 'Open the live MEL Console');
  // Place the hint in the card's top-right corner, wherever the diagram puts the card.
  const hint = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  hint.setAttribute('class', 'ad-tag ad-live-hint');
  hint.setAttribute('x', String(+box.getAttribute('x') + +box.getAttribute('width') - 14));
  hint.setAttribute('y', String(+box.getAttribute('y') + 18));
  hint.setAttribute('text-anchor', 'end');
  hint.textContent = 'OPEN LIVE ↗';
  card.append(hint);

  const win = document.createElement('div');
  win.className = 'cw';
  win.hidden = true;
  win.setAttribute('role', 'dialog');
  win.setAttribute('aria-modal', 'true');
  win.setAttribute('aria-label', 'Live MEL Console');
  win.innerHTML = `
    <div class="cw-chrome">
      <span class="cw-dots" aria-hidden="true"><i></i><i></i><i></i></span>
      <span class="cw-url"><span class="cw-lock" aria-hidden="true"></span><span class="cw-addr">fieldecho.org/#demo</span></span>
      <a class="cw-tab" target="_blank" rel="noopener">New tab ↗</a>
      <button type="button" class="cw-back">← Back to the talk</button>
    </div>
    <div class="cw-view"><p class="cw-loading">Loading fieldecho.org…</p></div>`;
  document.body.append(win);
  const view = win.querySelector('.cw-view');
  const addr = win.querySelector('.cw-addr');
  const newTab = win.querySelector('.cw-tab');
  newTab.href = CONSOLE;
  let frame = null;
  // The presenter's latest request (want) and the settled state (open) are separate:
  // settle() plays transitions until they agree, checking the request between phases,
  // so Back during an opening reverses it and a tap during a close reopens.
  let want = false;
  let open = false;
  let running = false;
  let openedByKey = false;

  // Load the console once, ahead of the tap, and keep it: reopening resumes where the demo left off.
  function ensureFrame() {
    if (frame) return;
    frame = document.createElement('iframe');
    frame.className = 'cw-frame';
    frame.title = 'MEL Console';
    frame.src = CONSOLE;
    frame.addEventListener('load', () => {
      try {
        const page = frame.contentWindow;
        const show = () => {
          addr.textContent = 'fieldecho.org' + page.location.pathname + page.location.hash;
          newTab.href = page.location.href;
        };
        show();
        page.addEventListener('hashchange', show);
        // Focus sits inside the console once it opens. Escape closes the window, unless the
        // console has a modal open (the sign-in page's demo video): Escape closes that first.
        page.addEventListener('keydown', e => {
          if (e.key === 'Escape' && !page.document.querySelector('dialog[open]')) closeWindow();
        });
      } catch { /* Not same-origin (local preview): keep the static address. */ }
    });
    view.append(frame);
    size();
  }

  // The deployment banner (static/deployment-banner.mjs) is fixed above everything while a
  // release runs, so the window stays below it.
  const bannerHeight = () =>
    parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--deployment-banner-height')) || 0;
  const full = () => {
    const top = INSET + bannerHeight();
    return { left: INSET, top, width: innerWidth - 2 * INSET, height: innerHeight - INSET - top };
  };

  // The iframe always lays out at the full window's size and is scaled to the current
  // window, so the small window shows a true miniature of the desktop site.
  function size() {
    if (!frame) return;
    const f = full();
    frame.style.width = f.width + 'px';
    frame.style.height = (f.height - CHROME) + 'px';
  }

  function cardRect() {
    const r = box.getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  }

  function miniRect(c) {
    const f = full();
    const width = Math.min(Math.max(360, innerWidth * 0.3), 560, f.width);
    const height = width * (f.height - CHROME) / f.width + CHROME;
    const left = Math.min(Math.max(INSET, c.left + c.width / 2 - width / 2), innerWidth - INSET - width);
    const top = Math.min(Math.max(f.top, c.top + c.height / 2 - height / 2), innerHeight - INSET - height);
    return { left, top, width, height };
  }

  const px = r => ({ left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px' });
  const scaleFor = r => `scale(${r.width / full().width})`;

  function place(r, radius) {
    Object.assign(win.style, px(r), { borderRadius: radius + 'px' });
    if (frame) frame.style.transform = scaleFor(r);
  }

  function morph(from, to, ms, fromRadius, toRadius) {
    if (reduce) { place(to, toRadius); return Promise.resolve(); }
    const timing = { duration: ms, easing: EASE, fill: 'forwards' };
    const runs = [win.animate([
      { ...px(from), borderRadius: fromRadius + 'px' },
      { ...px(to), borderRadius: toRadius + 'px' },
    ], timing)];
    if (frame) runs.push(frame.animate([{ transform: scaleFor(from) }, { transform: scaleFor(to) }], timing));
    return Promise.all(runs.map(run => run.finished)).then(() => {
      place(to, toRadius);
      runs.forEach(run => run.cancel());
    });
  }

  const pause = ms => new Promise(done => setTimeout(done, reduce ? 0 : ms));

  const setInert = on => behind.forEach(el => { if (el) el.inert = on; });

  async function growToFull(from) {
    await morph(from, full(), 760, 12, 14);
    place(full(), 14);  // the viewport may have changed during the animation
    open = true;
    frame.focus();
  }

  async function shrinkToCard(from) {
    const c = cardRect();
    win.classList.add('cw-card');
    await morph(from, c, 480, 12, 8);
    win.hidden = true;
    win.classList.remove('cw-card');
    setInert(false);
    // Keyboard users get focus back on the card. After a tap or click, clear focus so
    // Space and the arrows drive the deck again instead of reopening the console.
    if (openedByKey) card.focus();
    else if (document.activeElement instanceof HTMLElement || document.activeElement instanceof SVGElement) document.activeElement.blur();
  }

  // Card -> mini browser -> full window. Turns back at a phase boundary if Back was pressed.
  async function openFromCard() {
    ensureFrame();
    size();
    const c = cardRect();
    win.hidden = false;
    win.classList.add('cw-card');
    place(c, 8);
    setInert(true);
    win.getBoundingClientRect();  // commit the card frame before the fade starts
    win.classList.remove('cw-card');
    await morph(c, miniRect(c), 560, 8, 12);
    if (want) await pause(240);
    if (want) await growToFull(miniRect(cardRect()));
    else await shrinkToCard(miniRect(cardRect()));
  }

  // Full window -> mini browser -> card. Turns back at the mini stage if reopened.
  async function closeToCard() {
    const m = miniRect(cardRect());
    await morph(full(), m, 600, 14, 12);
    if (want) return growToFull(m);
    open = false;
    await shrinkToCard(m);
  }

  async function settle() {
    if (running) return;
    running = true;
    while (want !== open) await (want ? openFromCard() : closeToCard());
    running = false;
  }

  function openWindow(byKey) {
    if (!want && !running) openedByKey = byKey === true;
    want = true;
    settle();
  }

  function closeWindow() {
    want = false;
    settle();
  }

  card.addEventListener('click', () => openWindow(false));
  card.addEventListener('keydown', e => {
    // Stop here: the deck reads Space as "next slide" on the document.
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); openWindow(true); }
  });
  card.addEventListener('pointerenter', ensureFrame);
  card.addEventListener('focus', ensureFrame);
  win.querySelector('.cw-back').addEventListener('click', closeWindow);

  // While the console is open or moving, keys never reach the deck, so slides cannot change behind it.
  window.addEventListener('keydown', e => {
    if (!open && !running) return;
    e.stopImmediatePropagation();
    if (e.key === 'Escape') closeWindow();
  }, true);

  const refit = () => {
    size();
    if (open && !running) place(full(), 14);
  };
  addEventListener('resize', refit);
  document.addEventListener('deployment-banner-resize', refit);

  new MutationObserver(() => {
    if (scene.classList.contains('active')) ensureFrame();
  }).observe(scene, { attributes: true, attributeFilter: ['class'] });
  if (scene.classList.contains('active')) ensureFrame();
})();
