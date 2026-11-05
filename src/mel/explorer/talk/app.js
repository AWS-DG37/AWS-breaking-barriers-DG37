(() => {
  const deck = document.getElementById('deck');
  const scenes = [...deck.querySelectorAll('.scene')];
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const dots = document.getElementById('dots');
  let cur = 0;

  // build dot nav
  scenes.forEach((s, i) => {
    const b = document.createElement('button');
    b.setAttribute('aria-label', 'Go to slide ' + (i + 1));
    b.addEventListener('click', () => go(i));
    dots.append(b);
  });

  // count-up numbers
  function countUp(scene) {
    scene.querySelectorAll('.num').forEach(el => {
      if (el.classList.contains('word')) return;
      if (el._done) return;
      // only animate when a numeric data-to is provided; otherwise leave the text as written
      if (el.dataset.to === undefined || isNaN(+el.dataset.to)) return;
      el._done = true;
      const to = +el.dataset.to;
      const comma = el.hasAttribute('data-comma');
      const fmt = n => comma ? n.toLocaleString() : String(n);
      if (reduce) { el.textContent = fmt(to); return; }
      const start = performance.now() + 450;
      (function tick(t) {
        const p = Math.min(1, Math.max(0, (t - start) / 1400));
        el.textContent = fmt(Math.round(to * (1 - Math.pow(1 - p, 3))));
        if (p < 1) requestAnimationFrame(tick);
      })(performance.now());
    });
  }

  function fitCurrentSlide() {
    const sc = scenes[cur], c = sc.querySelector('.content');
    sc.classList.toggle('overflowing', c && c.scrollHeight > sc.clientHeight - 40);
  }
  document.addEventListener('deployment-banner-resize', () => requestAnimationFrame(fitCurrentSlide));
  addEventListener('resize', () => requestAnimationFrame(fitCurrentSlide));

  function go(i) {
    cur = Math.max(0, Math.min(scenes.length - 1, i));
    deck.style.transform = `translateX(${-cur * 100}%)`;
    scenes.forEach((s, k) => {
      s.setAttribute('aria-hidden', k !== cur);
      if (k !== cur) s.classList.remove('active');
    });
    // let a slide scroll only if its content genuinely overflows
    requestAnimationFrame(fitCurrentSlide);
    requestAnimationFrame(() => {
      scenes[cur].classList.add('active');
      countUp(scenes[cur]);
    });
    [...dots.children].forEach((d, k) => d.setAttribute('aria-current', k === cur));
    history.replaceState(null, '', '#' + (cur + 1));
  }

  // keyboard
  document.addEventListener('keydown', e => {
    // While the demo video is open, its keys (space, arrows) belong to the video.
    if (document.querySelector('dialog[open]')) return;
    // Space on the focused demo button opens the video; it must not also advance the slide.
    if (e.key === ' ' && e.target.closest?.('[data-demo-video]')) return;
    if (e.key.toLowerCase() === 'd' && !e.metaKey && !e.ctrlKey && !e.altKey) { document.querySelector('[data-demo-video]')?.click(); return; }
    if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); go(cur + 1); }
    else if (e.key === 'ArrowLeft' || e.key === 'PageUp') go(cur - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(scenes.length - 1);
    else if (e.key.toLowerCase() === 'f') {
      document.fullscreenElement ? document.exitFullscreen?.() : document.documentElement.requestFullscreen?.();
    }
  });

  // swipe
  let sx = null;
  deck.addEventListener('touchstart', e => sx = e.touches[0].clientX, { passive: true });
  deck.addEventListener('touchend', e => {
    if (sx === null) return;
    const dx = e.changedTouches[0].clientX - sx;
    if (Math.abs(dx) > 55) go(cur + (dx < 0 ? 1 : -1));
    sx = null;
  });

  // parallax orbs
  if (!reduce) {
    document.addEventListener('mousemove', e => {
      const x = (e.clientX / innerWidth - .5);
      const y = (e.clientY / innerHeight - .5);
      scenes[cur]?.querySelectorAll('.orb').forEach((o, idx) => {
        const amt = 24 + idx * 10;
        o.style.translate = `${x * amt}px ${y * amt}px`;
      });
    });
  }

  const start = Math.max(0, (parseInt(location.hash.slice(1), 10) || 1) - 1);
  go(start);
})();
