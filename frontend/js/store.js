// Tiny shared state store: holds the latest StateFrame and notifies subscribers.
// Both pages import this; neither depends on the other.

const subscribers = new Set();
let latest = null;
let connected = false;

export function setFrame(frame) {
  latest = frame;
  for (const fn of subscribers) fn(latest);
}

export function setConnected(v) {
  connected = v;
}

export function isConnected() {
  return connected;
}

export function getFrame() {
  return latest;
}

export function subscribe(fn) {
  subscribers.add(fn);
  if (latest) fn(latest);
  return () => subscribers.delete(fn);
}
