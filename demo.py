"""데모 모드: 실제 시스템 대신 샘플 데이터를 보여준다 (스크린샷/체험용).

실행: python server.py --demo
"""
import random
import time

NOW = time.time()


def _svc(pid, project, rel, app, name, kind, ports, mem, cpu, up_min, logs=(), http=None, **extra):
    s = {
        "pid": pid, "name": name, "app": app, "project": project, "project_rel": rel,
        "project_path": f"C:/Users/demo/Desktop/{rel}", "cwd": f"C:/Users/demo/Desktop/{rel}",
        "exe": f"C:/Python312/{name}", "cmdline": extra.pop("cmd", [name]),
        "ports": [{"port": p, "addrs": [a], "http": http} for p, a in ports],
        "started": NOW - up_min * 60, "mem": mem, "cpu": cpu, "children": 0, "models": [],
        "kind": kind, "kind_label": None, "http": http,
        "logs": [{"path": f"C:/Users/demo/Desktop/{rel}/{l}", "source": "열린 파일", "size": 184_320,
                  "mtime": NOW - (5 if i == 0 and extra.get("active") else 3600), "active": i == 0 and extra.get("active", False)}
                 for i, l in enumerate(logs)],
        "exposed": any(a == "0.0.0.0" for _, a in ports),
    }
    extra.pop("active", None)
    s.update(extra)
    return s


def _web(port, title, frameable=True):
    return {"url": f"http://127.0.0.1:{port}", "status": 200, "html": True, "title": title,
            "content_type": "text/html", "frameable": frameable, "server": "uvicorn"}


def _api(port):
    return {"url": f"http://127.0.0.1:{port}", "status": 200, "html": False, "title": None,
            "content_type": "application/json", "frameable": True, "server": "uvicorn"}


SERVICES = [
    _svc(21408, "rag-assistant", "rag-assistant", "llama-server", "llama-server.exe", "llm",
         [(8080, "127.0.0.1")], 5_620_000_000, 38.4, 194, ["logs/llama-server.log"], _api(8080),
         cmd=["llama-server.exe", "-m", "models/qwen3-8b-instruct-q4_k_m.gguf", "--port", "8080", "-c", "8192", "-ngl", "99"],
         models=[{"label": "qwen3-8b-instruct-q4_k_m", "path": "C:/Users/demo/Desktop/rag-assistant/models/qwen3-8b-instruct-q4_k_m.gguf"}],
         llm_api="openai", llm_models=["qwen3-8b-instruct-q4_k_m"], llm_endpoint="http://127.0.0.1:8080/v1", active=True),
    _svc(21877, "rag-assistant", "rag-assistant", "uvicorn", "python.exe", "api",
         [(8000, "127.0.0.1")], 182_000_000, 1.2, 190, ["logs/api.log"], _api(8000),
         cmd=["python.exe", "-m", "uvicorn", "app.main:app", "--port", "8000", "--reload"], active=True),
    _svc(22010, "rag-assistant", "rag-assistant", "vite", "node.exe", "web",
         [(5173, "127.0.0.1")], 96_000_000, 0.3, 188, [], _web(5173, "RAG Assistant"),
         cmd=["node.exe", "node_modules/vite/bin/vite.js", "--port", "5173"]),
    _svc(9120, "vision-lab", "lab/vision-lab", "ollama", "ollama.exe", "llm",
         [(11434, "127.0.0.1")], 3_870_000_000, 12.7, 1460, [], _api(11434),
         cmd=["ollama.exe", "serve"], llm_api="ollama", llm_models=["llama3.2-vision:11b", "nomic-embed-text"],
         llm_endpoint="http://127.0.0.1:11434"),
    _svc(30544, "vision-lab", "lab/vision-lab", "streamlit", "python.exe", "web",
         [(8501, "0.0.0.0")], 240_000_000, 0.8, 42, ["streamlit.out.log"], _web(8501, "Vision Lab · Streamlit"),
         cmd=["python.exe", "-m", "streamlit", "run", "app.py"]),
    _svc(4412, "shop-backend", "work/shop-backend", "postgres", "postgres.exe", "db",
         [(5432, "127.0.0.1")], 64_000_000, 0.1, 2880, ["pgdata/log/postgresql.log"],
         cmd=["postgres.exe", "-D", "pgdata"], kind_label="PostgreSQL"),
    _svc(4530, "shop-backend", "work/shop-backend", "redis-server", "redis-server.exe", "db",
         [(6379, "127.0.0.1")], 12_000_000, 0.0, 2880, [], cmd=["redis-server.exe"], kind_label="Redis"),
    _svc(27801, "shop-backend", "work/shop-backend", "server.js", "node.exe", "api",
         [(3000, "127.0.0.1"), (9229, "127.0.0.1")], 128_000_000, 2.4, 75, ["logs/server.log"], _api(3000),
         cmd=["node.exe", "--inspect", "server.js"]),
    _svc(31002, "agent-tools", "agent-tools", "notes_mcp.server", "python.exe", "mcp",
         [(8765, "127.0.0.1")], 71_000_000, 0.0, 12, ["mcp.log"], None,
         cmd=["python.exe", "-m", "notes_mcp.server", "--transport", "http"]),
]

EVENTS = [
    {"t": NOW - 12 * 60, "type": "up", "text": "agent-tools · notes_mcp.server 시작 (:8765)"},
    {"t": NOW - 31 * 60, "type": "down", "text": "vision-lab · gradio 종료 (:7860)"},
    {"t": NOW - 42 * 60, "type": "up", "text": "vision-lab · streamlit 시작 (:8501)"},
    {"t": NOW - 75 * 60, "type": "up", "text": "shop-backend · server.js 시작 (:3000)"},
]

LOG_LINES = [
    "INFO  srv  load_model: loading model 'models/qwen3-8b-instruct-q4_k_m.gguf'",
    "INFO  srv  llama_context: n_ctx = 8192, n_batch = 2048, flash_attn = 1",
    "INFO  srv  offloaded 37/37 layers to GPU",
    "INFO  srv  server listening on http://127.0.0.1:8080",
    "INFO  slot 0  POST /v1/chat/completions  prompt=1,284 tok",
    "INFO  slot 0  prompt eval: 1284 tok, 412.6 tok/s",
    "INFO  slot 0  generation: 356 tok, 48.2 tok/s",
    "INFO  slot 0  200 OK  7.81s",
    "WARN  slot 1  context nearly full (7,940 / 8,192), truncating oldest messages",
    "INFO  slot 1  POST /v1/chat/completions  prompt=2,031 tok",
    "INFO  slot 1  generation: 512 tok, 46.9 tok/s",
    "ERROR slot 1  client disconnected before completion",
    "INFO  slot 0  POST /v1/embeddings  batch=16",
    "INFO  slot 0  200 OK  0.42s",
]


class DemoScanner:
    """Scanner 와 같은 인터페이스를 흉내 낸다."""

    def __init__(self, interval):
        self.interval = interval
        self.scanned_at = time.time()
        self.wake = _Wake(self)

    def loop(self):
        while True:
            time.sleep(self.interval)
            self.scanned_at = time.time()

    def snapshot(self):
        return {"services": SERVICES, "others": [], "scanned_at": self.scanned_at, "duration_ms": 412,
                "events": EVENTS, "interval": self.interval}

    def proc_detail(self, pid):
        s = next((s for s in SERVICES if s["pid"] == pid), None)
        if not s:
            return None
        return {"pid": pid, "status": "running", "threads": random.randint(8, 40), "mem": s["mem"],
                "ppid": 1200, "parent": "WindowsTerminal.exe", "cpu": max(0, s["cpu"] + random.uniform(-2, 2)),
                "children": [], "established": random.randint(0, 6)}

    def read_log(self, pid, path, offset):
        if offset is None:
            base = time.time() - len(LOG_LINES) * 3
            text = "".join(f"{time.strftime('%H:%M:%S', time.localtime(base + i * 3))} {l}\n" for i, l in enumerate(LOG_LINES))
            return {"text": text, "offset": 1, "size": 184_320, "reset": False}
        line = random.choice(LOG_LINES[4:8])
        return {"text": f"{time.strftime('%H:%M:%S')} {line}\n", "offset": offset + 1, "size": 184_320, "reset": False}

    def kill(self, pid):
        return {"ok": False, "error": "데모 모드에서는 종료할 수 없어요."}


class _Wake:
    def __init__(self, sc):
        self.sc = sc

    def set(self):
        self.sc.scanned_at = time.time()
