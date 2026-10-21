"""Local Branch Flow server: every page load re-runs collect.py (at most once a minute).

Run:  python -I serve.py [--config path/to/config.json] [--no-browser]
Then: http://localhost:8765   (port from config "port"; Ctrl+C to stop)
"""
import json
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIN_AGE = 60  # seconds a collected snapshot is reused before the next load re-collects

CONFIG_ARGS = sys.argv[sys.argv.index("--config"):sys.argv.index("--config") + 2] if "--config" in sys.argv else []
_cfg_path = Path(CONFIG_ARGS[1]) if CONFIG_ARGS else HERE / "config.json"
PORT = json.loads(_cfg_path.read_text(encoding="utf-8")).get("port", 8765) if _cfg_path.exists() else 8765

_lock = threading.Lock()
_last = 0.0


def refresh():
    global _last
    with _lock:
        if time.time() - _last < MIN_AGE:
            return
        t = time.time()
        r = subprocess.run([sys.executable, "-I", str(HERE / "collect.py"), *CONFIG_ARGS],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (r.stdout or r.stderr).strip()
        print(f"[{time.strftime('%H:%M:%S')}] collected in {time.time() - t:.0f}s: "
              f"{out.splitlines()[0] if out else r.returncode}")
        if r.returncode != 0 and r.stderr:
            print(r.stderr.strip())
        _last = time.time()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404)
            return
        refresh()
        page = HERE / "index.html"
        if not page.exists():
            self.send_error(500, "index.html was not built; check the console for collect.py errors")
            return
        body = page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Branch Flow on {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
