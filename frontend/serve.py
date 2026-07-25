#!/usr/bin/env python3
"""Dev server for the viz harness: serves the frontend statically (like `python3 -m http.server`)
AND lets the preset panel's Save button write presets into the repo.

    cd frontend && python3 serve.py            # http://127.0.0.1:8081/dev.html

Endpoints (same-origin, dev only):
  POST /api/preset         body = a preset JSON ({id,label,renderer,version,params}) -> writes
                           presets/<id>.preset.json
  POST /api/preset/delete  body = {"id": "..."} -> removes presets/<id>.preset.json

Presets are plain files under frontend/presets/, so they're version-controlled and shared via git.
The app loads that folder on startup. If you run plain http.server instead, Save just falls back to
localStorage (the POST fails harmlessly).
"""
import http.server, os, json, re, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PRESETS_DIR = os.path.join(ROOT, "presets")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081


def safe_name(pid):
    fn = re.sub(r"[^a-z0-9_-]+", "-", str(pid or "preset").lower()).strip("-")
    return fn or "preset"


class Handler(http.server.SimpleHTTPRequestHandler):
    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        try:
            if self.path == "/api/preset":
                data = self._body()
                fn = safe_name(data.get("id"))
                os.makedirs(PRESETS_DIR, exist_ok=True)
                with open(os.path.join(PRESETS_DIR, fn + ".preset.json"), "w") as f:
                    json.dump(data, f, indent=2)
                return self._json(200, {"ok": True, "file": "presets/" + fn + ".preset.json"})
            if self.path == "/api/preset/delete":
                fn = safe_name(self._body().get("id"))
                p = os.path.join(PRESETS_DIR, fn + ".preset.json")
                if os.path.commonpath([os.path.abspath(p), PRESETS_DIR]) == PRESETS_DIR and os.path.exists(p):
                    os.remove(p)
                return self._json(200, {"ok": True})
            self._json(404, {"error": "not found"})
        except Exception as e:
            self._json(400, {"error": str(e)})

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # dev: always fetch fresh
        super().end_headers()

    def log_message(self, *a):
        pass  # quiet


if __name__ == "__main__":
    os.chdir(ROOT)
    os.makedirs(PRESETS_DIR, exist_ok=True)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # localhost only (has a write endpoint)
    print(f"viz harness on http://127.0.0.1:{PORT}/dev.html   (presets -> {PRESETS_DIR})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
