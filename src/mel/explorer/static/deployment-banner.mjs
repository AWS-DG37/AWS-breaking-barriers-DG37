const messages = {
  queued: 'Site update queued.',
  running: 'The site is being updated.',
  failed: 'The site update did not finish.',
  unconfirmed: 'The update finished, but this site has not confirmed it.',
  unknown: 'Update status is unavailable.',
};
const unknown = () => ({visible:true, text:messages.unknown, href:null});
const sha = value => typeof value === 'string' && /^[0-9a-f]{40}$/.test(value);

export function bannerView(value) {
  if (!value || typeof value !== 'object') return unknown();
  const state = value.state;
  if (state === 'disabled') return {visible:false, text:'', href:null, disabled:true};
  if (state === 'unknown') return unknown();
  if (!(Object.hasOwn(messages, state) || state === 'current') ||
      !sha(value.serving_sha) || !sha(value.target_sha) ||
      ![value.run_id,value.run_number,value.run_attempt].every(n => Number.isSafeInteger(n) && n > 0)) {
    return unknown();
  }
  return {visible:state !== 'current', text:messages[state] || '',
    href:`https://github.com/AWS-DG37/AWS-breaking-barriers-DG37/actions/runs/${value.run_id}`};
}

export function pollDeploymentStatus(doc, render, {
  fetchImpl = (...args) => fetch(...args),
  setTimer = setTimeout, clearTimer = clearTimeout,
} = {}) {
  let stopped = false, disabled = false, pending = false, timer = null, controller = null;
  const clear = () => { if (timer !== null) clearTimer(timer); timer = null; };
  async function poll() {
    clear();
    if (stopped || disabled || doc.hidden || pending) return;
    pending = true;
    controller = new AbortController();
    const timeout = setTimer(() => controller?.abort(), 8000);
    let view, state;
    try {
      const response = await fetchImpl('/api/deployment-status', {
        cache:'no-store', signal:controller.signal, credentials:'same-origin',
      });
      if (!response.ok) throw new Error('Unavailable');
      const text = await response.text();
      if (text.length > 16384) throw new Error('Oversized status');
      const value = JSON.parse(text);
      view = bannerView(value);
      state = value?.state;
    } catch {
      view = unknown();
    } finally {
      clearTimer(timeout);
      pending = false;
      controller = null;
    }
    if (stopped) return;
    disabled = Boolean(view.disabled);
    render(view);
    if (!disabled && !doc.hidden) {
      timer = setTimer(poll, state === 'queued' || state === 'running' ? 5000 : 20000);
    }
  }
  const visibility = () => { clear(); if (!doc.hidden) void poll(); };
  doc.addEventListener('visibilitychange', visibility);
  void poll();
  return () => {
    stopped = true;
    clear();
    controller?.abort();
    doc.removeEventListener('visibilitychange', visibility);
  };
}

export function mountDeploymentBanner(doc = document) {
  const win = doc.defaultView;
  const banner = doc.createElement('div');
  banner.id = 'deployment-banner';
  banner.hidden = true;
  banner.setAttribute('role', 'status');
  banner.setAttribute('aria-live', 'polite');
  banner.setAttribute('aria-atomic', 'true');
  const message = doc.createElement('span');
  const details = doc.createElement('a');
  details.textContent = 'Update details';
  details.target = '_blank';
  details.rel = 'noopener noreferrer';
  banner.append(message, details);
  doc.body.prepend(banner);
  let lastView = '', lastHeight = '';
  const measure = () => {
    const height = `${banner.hidden ? 0 : Math.ceil(banner.getBoundingClientRect().height)}px`;
    if (height === lastHeight) return;
    lastHeight = height;
    doc.documentElement.style.setProperty('--deployment-banner-height', height);
    doc.dispatchEvent(new win.Event('deployment-banner-resize'));
  };
  const observer = new win.ResizeObserver(measure);
  observer.observe(banner);
  const stopPolling = pollDeploymentStatus(doc, view => {
    const key = JSON.stringify(view);
    if (key !== lastView) {
      lastView = key;
      message.textContent = view.text;
      details.hidden = !view.href;
      if (view.href) details.href = view.href;
      else details.removeAttribute('href');
      banner.hidden = !view.visible;
    }
    measure();
  });
  measure();
  const stop = () => {
    stopPolling();
    observer.disconnect();
    banner.remove();
    doc.documentElement.style.setProperty('--deployment-banner-height', '0px');
    doc.dispatchEvent(new win.Event('deployment-banner-resize'));
    win.removeEventListener('pagehide', stop);
  };
  win.addEventListener('pagehide', stop, {once:true});
  return stop;
}

if (typeof document !== 'undefined') {
  mountDeploymentBanner(document);
  document.defaultView.addEventListener('pageshow', event => {
    if (event.persisted) mountDeploymentBanner(document);
  });
}
