// Sign-in runs on the server: /api/auth/* keeps an HttpOnly session cookie on this
// origin, so the page never sees a token. A local console reports sign-in as off.

const ATTEMPT = 'mel.auth.attempt';

function lastAttempt() {
  try { return Number(sessionStorage.getItem(ATTEMPT) || 0); } catch { return 0; }
}

// Leaves for Cognito. A second attempt within 30 seconds means the session cookie
// did not stick, so stop instead of looping between the console and Cognito.
export function signIn() {
  if (Date.now() - lastAttempt() < 30_000) {
    throw new Error('Sign-in did not complete. Allow cookies for this site, then reload.');
  }
  try { sessionStorage.setItem(ATTEMPT, String(Date.now())); } catch { /* Best effort. */ }
  location.assign('/api/auth/login');
  return new Promise(() => {});  // The page is leaving; nothing after this runs.
}

export function signedIn() {
  try { sessionStorage.removeItem(ATTEMPT); } catch { /* Best effort. */ }
}

export const signOut = () => location.assign('/api/auth/logout');
