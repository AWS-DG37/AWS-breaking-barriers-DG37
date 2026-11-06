// Static snapshot only (scripts/build_static_talk.py): there is no live console to open,
// so slide 4's MEL Console card plays the demo video.
(() => {
  const card = document.getElementById('ad-console');
  if (!card) return;
  const box = card.querySelector('rect');
  const play = () => document.querySelector('[data-demo-video]')?.click();

  card.setAttribute('role', 'button');
  card.setAttribute('tabindex', '0');
  card.setAttribute('aria-label', 'Watch the demo video of the MEL Console');
  const hint = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  hint.setAttribute('class', 'ad-tag ad-live-hint');
  hint.setAttribute('x', String(+box.getAttribute('x') + +box.getAttribute('width') - 14));
  hint.setAttribute('y', String(+box.getAttribute('y') + 18));
  hint.setAttribute('text-anchor', 'end');
  hint.textContent = 'WATCH DEMO ▶';
  card.append(hint);

  card.addEventListener('click', play);
  card.addEventListener('keydown', e => {
    // Stop here: the deck reads Space as "next slide" on the document.
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); play(); }
  });
})();
