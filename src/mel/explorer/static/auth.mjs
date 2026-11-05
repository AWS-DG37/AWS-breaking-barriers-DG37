// Sign-in runs on the server: /api/auth/* keeps an HttpOnly session cookie on this
// origin, so the page never sees a token. A local console reports sign-in as off.

const ATTEMPT = 'mel.auth.attempt';
let readOnly=false;
export const isReadOnly=()=>readOnly;

function lastAttempt() {
  try { return Number(sessionStorage.getItem(ATTEMPT) || 0); } catch { return 0; }
}

// Leaves for Cognito. A second attempt within 30 seconds means the session cookie
// did not stick, so stop instead of looping between the console and Cognito.
export async function signIn() {
  const response=await fetch('/api/auth/options');
  if (!response.ok) throw new Error('Sign-in unavailable. Reload to try again.');
  const options=await response.json();
  if (options.viewer) {
    location.assign(`/static/sign-in.html${location.hash}`);
    return new Promise(() => {});
  }
  if (Date.now() - lastAttempt() < 30_000) {
    throw new Error('Sign-in did not complete. Allow cookies for this site, then reload.');
  }
  try { sessionStorage.setItem(ATTEMPT, String(Date.now())); } catch { /* Best effort. */ }
  location.assign('/api/auth/login');
  return new Promise(() => {});  // The page is leaving; nothing after this runs.
}

export function signedIn(session={}) {
  readOnly=session.read_only===true;
  try { sessionStorage.removeItem(ATTEMPT); } catch { /* Best effort. */ }
}

export const signOut = () => location.assign('/api/auth/logout');
