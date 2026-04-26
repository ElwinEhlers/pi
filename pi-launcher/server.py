#!/usr/bin/env python3
import http.server
import socketserver
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime

CONFIG_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
PROMPTS_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts.json")
DEFAULT_WORKDIR = os.path.expanduser("~/Desktop/pi-aufgaben")
PI_SETTINGS_PATH = os.path.expanduser("~/.pi/agent/settings.json")

# On Windows, npm-installed tools are .cmd files which Popen won't find without shell=True.
# shutil.which respects PATHEXT and returns the full resolved path (e.g. pi.cmd).
PI_CMD = shutil.which("pi") or "pi"
OLLAMA_CMD = shutil.which("ollama") or "ollama"

# Tracks processes started by this server
_ollama_proc = None
_pi_term_proc = None


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("workdir"):
            data["workdir"] = DEFAULT_WORKDIR
        return data
    except (OSError, json.JSONDecodeError):
        return {"workdir": DEFAULT_WORKDIR}


def save_config(data):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def ollama_is_running():
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=1)
        return True
    except Exception:
        return False


class PiLauncherHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), format % args))

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _json_response(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/":
            self._handle_get_index()
        elif self.path == "/config":
            self._handle_get_config()
        elif self.path == "/service/status":
            self._handle_get_service_status()
        elif self.path == "/models":
            self._handle_get_models()
        elif self.path == "/pi-settings":
            self._handle_get_pi_settings()
        elif self.path == "/prompts":
            self._handle_get_prompts()
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/run":
            self._handle_post_run()
        elif self.path == "/config":
            self._handle_post_config()
        elif self.path == "/service/start-ollama":
            self._handle_start_ollama()
        elif self.path == "/service/open-pi":
            self._handle_open_pi()
        elif self.path == "/pi-settings":
            self._handle_post_pi_settings()
        elif self.path == "/prompts":
            self._handle_post_prompts()
        else:
            self._json_response(404, {"error": "not found"})

    # ── Index ─────────────────────────────────────────────────────────────────

    def _handle_get_index(self):
        index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        try:
            with open(index_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError as e:
            self._json_response(500, {"error": str(e)})

    # ── Config ────────────────────────────────────────────────────────────────

    def _handle_get_config(self):
        self._json_response(200, load_config())

    def _handle_post_config(self):
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, ValueError):
            self._json_response(400, {"error": "invalid JSON"})
            return

        workdir = body.get("workdir", "").strip()
        if not workdir:
            self._json_response(400, {"error": "workdir darf nicht leer sein"})
            return

        try:
            save_config({"workdir": workdir})
        except OSError as e:
            self._json_response(500, {"error": str(e)})
            return

        self._json_response(200, {"ok": True})

    # ── Service status ────────────────────────────────────────────────────────

    def _handle_get_service_status(self):
        pi_running = _pi_term_proc is not None and _pi_term_proc.poll() is None
        self._json_response(200, {"ollama": ollama_is_running(), "pi": pi_running})

    # ── Prompts ──────────────────────────────────────────────────────────────

    def _handle_get_prompts(self):
        try:
            with open(PROMPTS_PATH, encoding="utf-8") as f:
                self._json_response(200, json.load(f))
        except (OSError, json.JSONDecodeError):
            self._json_response(200, [])

    def _handle_post_prompts(self):
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, ValueError):
            self._json_response(400, {"error": "invalid JSON"})
            return
        if not isinstance(body, list):
            self._json_response(400, {"error": "Array erwartet"})
            return
        try:
            with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
                json.dump(body, f, indent=2, ensure_ascii=False)
            self._json_response(200, {"ok": True})
        except OSError as e:
            self._json_response(500, {"error": str(e)})

    # ── Models ───────────────────────────────────────────────────────────────

    def _handle_get_models(self):
        try:
            req = urllib.request.Request("http://localhost:11434/api/tags")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._json_response(200, data)
        except Exception as e:
            self._json_response(503, {"error": str(e), "models": []})

    # ── Pi settings ───────────────────────────────────────────────────────────

    def _handle_get_pi_settings(self):
        try:
            with open(PI_SETTINGS_PATH, encoding="utf-8") as f:
                self._json_response(200, json.load(f))
        except (OSError, json.JSONDecodeError):
            self._json_response(200, {})

    def _handle_post_pi_settings(self):
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, ValueError):
            self._json_response(400, {"error": "invalid JSON"})
            return

        try:
            with open(PI_SETTINGS_PATH, encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, json.JSONDecodeError):
            settings = {}

        settings.update(body)

        try:
            os.makedirs(os.path.dirname(PI_SETTINGS_PATH), exist_ok=True)
            with open(PI_SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            self._json_response(200, {"ok": True})
        except OSError as e:
            self._json_response(500, {"error": str(e)})

    # ── Start Ollama (SSE) ────────────────────────────────────────────────────

    def _handle_start_ollama(self):
        global _ollama_proc

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._send_cors_headers()
        self.end_headers()

        def sse(line):
            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
            self.wfile.flush()

        def sse_done():
            self.wfile.write(b"event: done\ndata: \n\n")
            self.wfile.flush()

        if ollama_is_running():
            sse("Ollama läuft bereits auf Port 11434.")
            sse_done()
            return

        sse("Starte Ollama...")
        try:
            _ollama_proc = subprocess.Popen(
                [OLLAMA_CMD, "serve"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                shell=True,
            )
        except FileNotFoundError:
            sse("[Fehler: 'ollama' nicht gefunden. Ist Ollama installiert?]")
            sse_done()
            return
        except Exception as e:
            sse(f"[Fehler: {e}]")
            sse_done()
            return

        # Stream output until port becomes available or process exits
        import threading, time

        ready = threading.Event()

        def watch_ready():
            for _ in range(30):
                time.sleep(0.5)
                if ollama_is_running():
                    ready.set()
                    return
            ready.set()

        threading.Thread(target=watch_ready, daemon=True).start()

        try:
            for line in _ollama_proc.stdout:
                text = line.rstrip()
                if text:
                    sse(text)
                if ready.is_set():
                    break
        except (BrokenPipeError, ConnectionResetError):
            return

        if ollama_is_running():
            sse("Ollama ist bereit auf http://localhost:11434")
        else:
            sse("[Hinweis: Ollama antwortet noch nicht — läuft möglicherweise noch hoch]")

        sse_done()

    # ── Open Pi terminal ──────────────────────────────────────────────────────

    def _handle_open_pi(self):
        global _pi_term_proc
        config = load_config()
        workdir = os.path.expanduser(config.get("workdir", DEFAULT_WORKDIR))
        os.makedirs(workdir, exist_ok=True)

        try:
            # Open a new console window running pi; /k keeps window open after exit
            _pi_term_proc = subprocess.Popen(
                ["cmd", "/k", PI_CMD],
                cwd=workdir,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                shell=True,
            )
            self._json_response(200, {"ok": True})
        except FileNotFoundError:
            self._json_response(500, {"error": "'pi' nicht gefunden"})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    # ── Run prompt (SSE) ──────────────────────────────────────────────────────

    def _handle_post_run(self):
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self._send_cors_headers()
            self.end_headers()
            return

        prompt = body.get("prompt", "").strip()
        if not prompt:
            self.send_response(400)
            self._send_cors_headers()
            self.end_headers()
            return

        config = load_config()
        workdir = os.path.expanduser(config.get("workdir", DEFAULT_WORKDIR))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._send_cors_headers()
        self.end_headers()

        def sse(line):
            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
            self.wfile.flush()

        def sse_done():
            self.wfile.write(b"event: done\ndata: \n\n")
            self.wfile.flush()

        if not os.path.isdir(workdir):
            sse(f"[Fehler: Arbeitsverzeichnis nicht gefunden: {workdir}]")
            sse_done()
            return

        proc = None
        try:
            proc = subprocess.Popen(
                [PI_CMD, "-p", prompt],
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                shell=True,
            )
            for line in proc.stdout:
                sse(line.rstrip())
            proc.wait()
        except FileNotFoundError:
            sse("[Fehler: 'pi' wurde nicht gefunden. Ist es im PATH?]")
        except (BrokenPipeError, ConnectionResetError):
            if proc:
                proc.kill()
            return
        except Exception as e:
            sse(f"[Fehler: {e}]")
            if proc:
                proc.kill()

        sse_done()


class ThreadingPiServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    server = ThreadingPiServer(("localhost", 8765), PiLauncherHandler)
    print("Server läuft auf http://localhost:8765")
    print("Stoppen mit Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer gestoppt.")
