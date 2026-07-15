// WebSocket client for the /ws StateFrame stream, with auto-reconnect.
// Pushes every frame into the shared store.

import { setFrame, setConnected } from "./store.js";

export function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws`;
  let sock;
  let retry = 500;

  function open() {
    sock = new WebSocket(url);
    sock.onopen = () => {
      setConnected(true);
      retry = 500;
    };
    sock.onmessage = (ev) => {
      try {
        setFrame(JSON.parse(ev.data));
      } catch (e) {
        /* ignore malformed frame */
      }
    };
    sock.onclose = () => {
      setConnected(false);
      setTimeout(open, retry);
      retry = Math.min(5000, retry * 2);
    };
    sock.onerror = () => sock.close();
  }
  open();
}

// Minimal REST helper for Ops control actions.
export async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return res.json();
}
