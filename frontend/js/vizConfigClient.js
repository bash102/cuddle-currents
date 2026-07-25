// Subscribes to the server-authoritative active viz config over /ws/viz and hands each
// pushed config (or null) to a callback. Auto-reconnects like ws.js. The server sends the
// current config immediately on connect, so a late-joining Show still renders the latest.

export function subscribeVizConfig(onConfig) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/viz`;
  let retry = 500;
  function open() {
    const sock = new WebSocket(url);
    sock.onopen = () => { retry = 500; };
    sock.onmessage = (ev) => {
      try { onConfig(JSON.parse(ev.data)); } catch { /* ignore malformed */ }
    };
    sock.onclose = () => { setTimeout(open, retry); retry = Math.min(5000, retry * 2); };
    sock.onerror = () => sock.close();
  }
  open();
}
