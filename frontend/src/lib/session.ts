let accessToken: string | null = null;
let expiry: number | undefined;
const listeners = new Set<SessionListener>();

export type SessionListener = () => void;
function publish() {
  listeners.forEach((listener) => listener());
}

export function getAccessToken() {
  return accessToken;
}

export function setAccessToken(token: string, expiresIn: number) {
  window.clearTimeout(expiry);
  accessToken = token;
  expiry = window.setTimeout(clearAccessToken, Math.min(expiresIn * 1000, 2_147_483_647));
  publish();
}

export function clearAccessToken() {
  window.clearTimeout(expiry);
  expiry = undefined;
  accessToken = null;
  publish();
}

export function subscribeSession(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
