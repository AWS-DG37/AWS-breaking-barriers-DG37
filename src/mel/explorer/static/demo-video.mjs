// The 2-minute demo video, opened from the deck and the sign-in page.
// Any button with data-demo-video opens it. Both pages are public, so the video is too.
const SRC = '/static/assets/demo-video.mp4';
const POSTER = '/static/assets/demo-video-poster.jpg';
let dialog = null;

function build() {
  dialog = document.createElement('dialog');
  dialog.className = 'demo-video';
  dialog.setAttribute('aria-label', 'FarmerChat demo video');
  const box = document.createElement('div');
  box.className = 'demo-video-box';
  const bar = document.createElement('div');
  bar.className = 'demo-video-bar';
  const title = document.createElement('span');
  title.textContent = 'FarmerChat follow-up survey agent · 2:17';
  const close = document.createElement('button');
  close.type = 'button';
  close.textContent = 'Close';
  close.addEventListener('click', () => dialog.close());
  bar.append(title, close);
  const video = document.createElement('video');
  Object.assign(video, {src: SRC, poster: POSTER, controls: true, playsInline: true, preload: 'none'});
  const note = document.createElement('p');
  note.textContent = 'Recorded live on Amazon Bedrock with synthetic data. '
    + 'Near the end, the survey is read aloud in Hindi and then in English.';
  box.append(bar, video, note);
  dialog.append(box);
  dialog.addEventListener('close', () => video.pause());
  // The box fills the dialog, so a click on the dialog itself is a click on the backdrop.
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  document.body.append(dialog);
}

export function openDemoVideo() {
  if (!dialog) build();
  if (dialog.open) return;
  dialog.showModal();
  dialog.querySelector('video').play().catch(() => {});
}

for (const button of document.querySelectorAll('[data-demo-video]')) {
  button.addEventListener('click', openDemoVideo);
}
