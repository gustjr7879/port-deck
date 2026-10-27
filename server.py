"""Port Deck - 내 프로젝트가 점유한 포트/LLM/로그를 보여주는 로컬 대시보드.

실행: python server.py  ->  http://127.0.0.1:7070
"""
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psutil

BASE = Path(__file__).resolve().parent
HOME = Path.home()
SELF_PID = os.getpid()
TOKEN = secrets.token_hex(16)


def load_config():
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

    def norm(p):
        return os.path.normcase(os.path.abspath(os.path.expanduser(p)))

    cfg["project_roots"] = [norm(p) for p in cfg.get("project_roots", [])]
    cfg["exclude_paths"] = [norm(p) for p in cfg.get("exclude_paths", [])]
    cfg["exclude_process_names"] = {n.lower() for n in cfg.get("exclude_process_names", [])}
    return cfg


CFG = load_config()

PROJECT_MARKERS = (".git", "pyproject.toml", "setup.py", "package.json", "requirements.txt",
                   "Cargo.toml", "go.mod", "pom.xml", "build.gradle", ".venv", "venv")
LOG_EXTS = {".log", ".out", ".err", ".output", ".jsonl", ".txt"}
MODEL_EXTS = {".gguf", ".litertlm", ".safetensors", ".onnx", ".tflite", ".task", ".ggml", ".mlmodel", ".pt", ".pth"}
LLM_KEYWORDS = ("ollama", "llama-server", "llama_cpp", "llama.cpp", "vllm", "litert", "lmstudio", "lm-studio",
                "koboldcpp", "text-generation", "tabbyapi", "sglang", "mlx_lm", "local-ai", "localai",
                "llamafile", "exllama", "tgi", "lmdeploy", "llm_server", "llm-server")
DB_KEYWORDS = {"postgres": "PostgreSQL", "mysqld": "MySQL", "mariadbd": "MariaDB", "redis-server": "Redis",
               "mongod": "MongoDB", "qdrant": "Qdrant", "chroma": "Chroma", "milvus": "Milvus",
               "clickhouse": "ClickHouse", "elasticsearch": "Elasticsearch", "memgraph": "Memgraph", "neo4j": "Neo4j"}
SCRIPT_EXTS = (".py", ".js", ".mjs", ".cjs", ".ts", ".exe", ".jar")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ----------------------------------------------------------------- helpers

def ncase(p):
    return os.path.normcase(os.path.abspath(p))


def real(p):
    """심볼릭 링크/정션을 풀어낸 실제 경로 (비교용)."""
    return os.path.normcase(os.path.realpath(p))


def attr(fn, default):
    try:
        return fn()
    except psutil.Error:
        return default


def under(path, roots):
    p = ncase(path)
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def is_project_path(path):
    return bool(path) and under(path, CFG["project_roots"]) and not under(path, CFG["exclude_paths"])


# 값을 하나 받는 옵션 (그 값은 엔트리포인트가 아님)
OPERAND_OPTS = {"-r", "--require", "--import", "--loader", "--experimental-loader", "-X", "-W", "--env-file"}


def first_positional(args):
    """옵션과 옵션 값을 건너뛴 첫 위치 인자 (python -m / -c 는 None)."""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in ("-m", "-c"):
            return None
        if a in OPERAND_OPTS:
            skip = True
            continue
        if a.startswith("-"):
            continue
        return a
    return None


def entry_path(cmdline, cwd):
    """실제 엔트리포인트(스크립트) 경로만 뽑는다. --config 같은 데이터 인자는 프로젝트 판정에 쓰지 않는다."""
    args = cmdline[1:]
    if "-m" in args and cwd:  # python -m pkg.mod -> cwd/pkg/mod(.py) 가 엔트리
        i = args.index("-m")
        if i + 1 < len(args):
            base = os.path.join(cwd, *args[i + 1].split("."))
            for p in (base + ".py", os.path.join(base, "__main__.py"), base):
                if os.path.exists(p):
                    return [os.path.abspath(p)]
        return []
    a = first_positional(args)
    if not a:
        return []
    p = a if os.path.isabs(a) else (os.path.join(cwd, a) if cwd else None)
    return [os.path.abspath(p)] if p and os.path.isfile(p) else []


def find_project_root(start):
    start = Path(start)
    if start.is_file():
        start = start.parent
    boundary = next((Path(r) for r in CFG["project_roots"] if under(str(start), [r])), None)
    chain = []
    cur = start
    while True:
        chain.append(cur)
        if boundary is None or ncase(str(cur)) == ncase(str(boundary)) or cur.parent == cur:
            break
        cur = cur.parent
    chain = [c for c in chain if boundary is None or ncase(str(c)) != ncase(str(boundary))]
    for c in chain:  # git 저장소 루트 우선
        if (c / ".git").exists():
            return c
    for c in chain:
        if any((c / m).exists() for m in PROJECT_MARKERS):
            return c
    return chain[-1] if chain else start


def app_name(name, cmdline):
    exe = Path(name).stem
    args = cmdline[1:]
    low = exe.lower()
    if re.fullmatch(r"pythonw?(\d+(\.\d+)?)?|py", low):
        for i, a in enumerate(args):
            if a == "-m" and i + 1 < len(args):
                return args[i + 1]
        a = first_positional(args)
        if a and a.lower().endswith(SCRIPT_EXTS):
            return Path(a).stem if a.lower().endswith(".exe") else Path(a).name
    if low in ("node", "bun", "deno"):
        a = first_positional(args)
        if a:
            m = re.search(r"node_modules[\\/](@[^\\/]+[\\/][^\\/]+|[^\\/]+)", a)
            if m:
                return m.group(1).replace("\\", "/")
            return Path(a).name
    return exe


def classify(name, app, model_files):
    # 경로 문자열 전체가 아니라 실행 파일 / 모듈 이름만 보고 판정
    text = (name + " " + app).lower()
    if model_files or any(k in text for k in LLM_KEYWORDS):
        return "llm", None
    for k, label in DB_KEYWORDS.items():
        if k in text:
            return "db", label
    if re.search(r"\bmcp\b|[_\-.]mcp|mcp[_\-.]", text):
        return "mcp", None
    return None, None


def model_label(path):
    p = Path(path)
    if p.stem.lower() in ("model", "weights", "pytorch_model", "consolidated"):
        return p.parent.name
    return p.stem


def http_get(url, timeout=0.8, limit=65536):
    req = urllib.request.Request(url, headers={"User-Agent": "PortDeck/1.0", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(limit)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b""
    except Exception:
        return None, {}, b""


def probe_http(port, addr):
    host = "[::1]" if addr in ("::", "::1") else "127.0.0.1"
    base = f"http://{host}:{port}"
    status, headers, body = http_get(base + "/")
    if status is None:
        return None
    h = {k.lower(): v for k, v in headers.items()}
    ctype = h.get("content-type", "")
    title = None
    if "html" in ctype:
        m = re.search(rb"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        if m:
            title = m.group(1).decode("utf-8", "replace").strip()[:80]
    xfo = h.get("x-frame-options", "").lower()
    csp = h.get("content-security-policy", "").lower()
    frameable = not xfo and "frame-ancestors" not in csp
    return {"url": base, "status": status, "html": "html" in ctype, "title": title,
            "content_type": ctype.split(";")[0], "frameable": frameable,
            "server": h.get("server")}


def probe_llm(port, addr):
    host = "[::1]" if addr in ("::", "::1") else "127.0.0.1"
    base = f"http://{host}:{port}"
    st, _, body = http_get(base + "/v1/models", timeout=1.5)
    if st == 200:
        try:
            return "openai", [m.get("id") for m in json.loads(body).get("data", [])], base + "/v1"
        except Exception:
            pass
    st, _, body = http_get(base + "/api/tags", timeout=1.5)  # ollama
    if st == 200:
        try:
            return "ollama", [m.get("name") for m in json.loads(body).get("models", [])], base
        except Exception:
            pass
    return None, [], None


GENERIC_TOKENS = {"server", "python", "pythonw", "main", "app", "run", "serve", "exe", "src", "http", "transport"}


def name_tokens(*names):
    toks = set()
    for n in names:
        for t in re.split(r"[^a-z0-9]+", (n or "").lower()):
            if len(t) >= 3 and t not in GENERIC_TOKENS:
                toks.add(t)
    return toks


def collect_logs(proc_files, root, tokens=()):
    logs = {}
    now = time.time()
    for f in proc_files:
        p = Path(f)
        if p.suffix.lower() in LOG_EXTS or "log" in p.name.lower():
            if not under(f, [ncase(r"C:\Windows"), ncase(r"C:\Program Files"), ncase(r"C:\Program Files (x86)")]):
                src = "Claude Code 백그라운드 출력" if p.suffix.lower() == ".output" and "claude" in f.lower() else "열린 파일"
                logs[ncase(f)] = {"path": str(p), "source": src}
    if root:
        root = Path(root)
        cutoff = now - CFG["log_recent_days"] * 86400
        cands = list(root.glob("*.log"))
        for d in ("logs", "log"):
            if (root / d).is_dir():
                cands += list((root / d).glob("*.log")) + list((root / d).glob("*/*.log"))
        def mtime(c):
            try:
                return c.stat().st_mtime if c.is_file() else 0
            except OSError:  # 스캔 도중 삭제된 파일
                return 0
        cands = [c for _, c in sorted(((mtime(c), c) for c in cands), reverse=True) if _ >= cutoff]
        for c in cands[:30]:
            if tokens & name_tokens(c.stem):  # 같은 프로젝트라도 이름이 관련된 로그만
                logs.setdefault(ncase(str(c)), {"path": str(c), "source": "프로젝트 폴더"})
    out = []
    for v in logs.values():
        try:
            st = os.stat(v["path"])
            v.update(size=st.st_size, mtime=st.st_mtime,
                     active=(now - st.st_mtime) < CFG["log_active_window_sec"])
            out.append(v)
        except OSError:
            pass
    out.sort(key=lambda v: (v["source"] == "프로젝트 폴더", -v["mtime"]))
    return out


# ----------------------------------------------------------------- scanner

class Scanner:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"services": [], "others": [], "scanned_at": None, "duration_ms": 0, "events": []}
        self.allowed_logs = {}   # pid -> {ncase(path)}
        self.kill_allowed = set()
        self.proc_cache = {}
        self.wake = threading.Event()
        self.prev_keys = None
        self.events = []
        self.pool = ThreadPoolExecutor(max_workers=12)

    def proc(self, pid):
        p = self.proc_cache.get(pid)
        try:
            if p is None or not p.is_running():
                p = psutil.Process(pid)
                p.cpu_percent(None)
                self.proc_cache[pid] = p
        except psutil.Error:
            return None
        return p

    def scan(self):
        t0 = time.time()
        listen = {}
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.pid:
                listen.setdefault(c.pid, {})
                listen[c.pid].setdefault(c.laddr.port, set()).add(c.laddr.ip)

        services, others = [], []
        for pid, ports in listen.items():
            if pid == SELF_PID:
                continue
            p = self.proc(pid)
            if p is None:
                continue
            if not p.is_running():
                continue
            # 속성별로 따로 읽는다: cwd 하나가 AccessDenied 여도 나머지 정보는 살림
            with p.oneshot():
                name = attr(p.name, "?")
                exe = attr(p.exe, "") if pid > 4 else ""
                cmd = attr(p.cmdline, [])
                cwd = attr(p.cwd, "")
                ctime = attr(p.create_time, None)
                mem = attr(lambda: p.memory_info().rss, None)
                cpu = attr(lambda: p.cpu_percent(None), None)

            port_list = [{"port": port, "addrs": sorted(addrs)} for port, addrs in sorted(ports.items())]
            cands = entry_path(cmd, cwd) + ([cwd] if cwd else []) + ([exe] if exe else [])
            anchor = next((c for c in cands if is_project_path(c)), None)
            if name.lower() in CFG["exclude_process_names"]:
                anchor = None
            base = {"pid": pid, "name": name, "app": app_name(name, cmd) if cmd else Path(name).stem,
                    "cmdline": cmd, "cwd": cwd, "exe": exe, "ports": port_list,
                    "started": ctime, "mem": mem, "cpu": cpu}
            if anchor is None:
                others.append(base)
                continue
            services.append((p, base, anchor))

        futures = [(self.pool.submit(self._enrich, it), it) for it in services]
        details = []
        for fut, (_, base, anchor) in futures:
            try:
                details.append(fut.result(timeout=20))
            except Exception as e:  # 한 서비스의 실패/지연이 전체 스캔을 망치지 않도록
                print(f"enrich error pid={base['pid']}: {e!r}", file=sys.stderr)
                root = find_project_root(anchor)
                base.update(project=root.name, project_path=str(root), project_rel=None, children=0,
                            models=[], kind="tcp", kind_label=None, logs=[], http=None,
                            exposed=any(a in ("0.0.0.0", "::") for pt in base["ports"] for a in pt["addrs"]))
                details.append(base)
        details.sort(key=lambda s: (s["project"].lower(), s["ports"][0]["port"]))
        others.sort(key=lambda s: s["ports"][0]["port"])

        keys = {(s["pid"], s["app"]): s for s in details}
        if self.prev_keys is not None:
            now = time.time()
            for k in keys.keys() - self.prev_keys.keys():
                s = keys[k]
                self.events.append({"t": now, "type": "up", "text": f"{s['project']} · {s['app']} 시작 (:{s['ports'][0]['port']})"})
            for k in self.prev_keys.keys() - keys.keys():
                s = self.prev_keys[k]
                self.events.append({"t": now, "type": "down", "text": f"{s['project']} · {s['app']} 종료 (:{s['ports'][0]['port']})"})
            self.events = self.events[-50:]
        self.prev_keys = keys

        for pid in list(self.proc_cache):
            if pid not in listen:
                self.proc_cache.pop(pid, None)

        with self.lock:
            # 로그는 실제 경로(realpath) 기준, 종료는 (pid, 시작시각) 쌍으로 허용 -> PID 재사용 방지
            self.allowed_logs = {s["pid"]: {real(l["path"]) for l in s["logs"]} for s in details}
            self.kill_allowed = {s["pid"]: s["started"] for s in details if s["started"]}
            self.state = {"services": details, "others": others, "scanned_at": time.time(),
                          "duration_ms": int((time.time() - t0) * 1000), "events": self.events[::-1],
                          "interval": CFG["scan_interval_sec"]}

    def _enrich(self, item):
        p, s, anchor = item
        root = find_project_root(anchor)
        rel = None
        for r in CFG["project_roots"]:
            if under(str(root), [r]):
                rel = os.path.relpath(str(root), r).replace("\\", "/")
        s["project"] = root.name
        s["project_path"] = str(root)
        s["project_rel"] = rel

        files = []
        procs = [p]
        try:
            procs += p.children(recursive=True)[:20]
        except psutil.Error:
            pass
        for q in procs:
            try:
                files += [f.path for f in q.open_files()]
            except psutil.Error:
                pass
        models = sorted({f for f in files if Path(f).suffix.lower() in MODEL_EXTS})
        s["children"] = len(procs) - 1
        s["models"] = [{"label": model_label(m), "path": m} for m in models]
        kind, label = classify(s["name"], s["app"], models)
        s["kind"], s["kind_label"] = kind, label
        s["logs"] = collect_logs(files, root, name_tokens(s["app"], s["name"], label or "", kind or ""))

        s["http"] = None
        for pt in s["ports"]:
            if kind == "db":
                break
            info = probe_http(pt["port"], pt["addrs"][0])
            pt["http"] = info
            if info and s["http"] is None:
                s["http"] = info
        if s["kind"] is None:
            s["kind"] = "web" if s["http"] and s["http"]["html"] else ("api" if s["http"] else "tcp")
        if s["kind"] == "llm":
            for pt in s["ports"]:
                api, ids, endpoint = probe_llm(pt["port"], pt["addrs"][0])
                if api:
                    s["llm_api"] = api
                    s["llm_models"] = ids
                    s["llm_endpoint"] = endpoint  # 실제로 응답한 주소 (IPv6 전용이면 [::1])
                    break
        s["exposed"] = any(a in ("0.0.0.0", "::") for pt in s["ports"] for a in pt["addrs"])
        return s

    def loop(self):
        while True:
            try:
                self.scan()
            except Exception as e:  # 스캔 하나가 실패해도 루프는 유지
                print("scan error:", e, file=sys.stderr)
            self.wake.wait(CFG["scan_interval_sec"])
            self.wake.clear()

    def snapshot(self):
        with self.lock:
            return self.state

    def proc_detail(self, pid):
        # 스캔용 캐시 객체와 분리해야 스캔의 CPU 평균이 흐트러지지 않는다
        try:
            p = psutil.Process(pid)
            cpu = p.cpu_percent(interval=0.3)
            with p.oneshot():
                d = {"pid": pid, "status": p.status(), "threads": p.num_threads(), "mem": p.memory_info().rss,
                     "ppid": p.ppid(), "cpu": cpu}
            try:
                d["parent"] = psutil.Process(d["ppid"]).name()
            except psutil.Error:
                d["parent"] = None
            kids = []
            for c in p.children(recursive=True)[:30]:
                try:
                    kids.append({"pid": c.pid, "name": c.name(), "mem": c.memory_info().rss})
                except psutil.Error:
                    pass
            d["children"] = kids
            conns = p.net_connections(kind="inet")
            d["established"] = sum(1 for c in conns if c.status == psutil.CONN_ESTABLISHED)
            return d
        except psutil.Error:
            return None

    def read_log(self, pid, path, offset):
        rp = real(path)
        with self.lock:
            ok = rp in self.allowed_logs.get(pid, set())
        if not ok or not os.path.isfile(rp):
            return None
        with open(rp, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            reset = offset is not None and offset > size  # 파일이 잘렸거나 교체됨
            start = max(0, size - 96 * 1024) if offset is None or reset else offset
            f.seek(start)
            raw = f.read(min(size - start, 512 * 1024))
        # 끝에서 잘린 UTF-8 문자는 다음 요청으로 넘긴다
        data = raw
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as e:
            if e.reason == "unexpected end of data" and e.start >= len(raw) - 3:
                data = raw[:e.start]
        consumed = len(data)
        if start > 0 and (offset is None or reset):  # 중간부터 읽은 첫 줄은 잘렸을 수 있으니 화면에서만 버림
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl >= 0 else data
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp949")
            except UnicodeDecodeError:
                text = data.decode("utf-8", "replace")
        return {"text": ANSI.sub("", text), "offset": start + consumed, "size": size, "reset": reset}

    def kill(self, pid):
        with self.lock:
            started = self.kill_allowed.get(pid)
        if started is None:
            return {"ok": False, "error": "대시보드에 표시된 프로젝트 프로세스만 종료할 수 있습니다."}
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - started) > 1:
                return {"ok": False, "error": "스캔 이후 이 PID가 다른 프로세스에 재사용됐어요. 다시 스캔한 뒤 시도하세요."}
            # 대시보드 자신과 그 조상 프로세스는 절대 건드리지 않는다
            protected = {SELF_PID} | {a.pid for a in psutil.Process(SELF_PID).parents()}
            if pid in protected:
                return {"ok": False, "error": "대시보드 자신(또는 그 상위 프로세스)은 종료할 수 없어요."}
            tree = [p]
            for c in p.children(recursive=True):
                try:
                    if c.pid in protected or c.name().lower() in CFG["exclude_process_names"]:
                        continue
                    tree.append(c)
                except psutil.Error:
                    pass
        except psutil.Error as e:
            return {"ok": False, "error": str(e)}
        names = []
        for q in tree:
            try:
                names.append(f"{q.name()}({q.pid})")
                q.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(tree, timeout=3)
        for q in alive:
            try:
                q.kill()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(alive, timeout=2)
        self.wake.set()
        return {"ok": not alive, "killed": names, "alive": [q.pid for q in alive]}


SCANNER = Scanner()


# ----------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    timeout = 10  # 느리게 보내는 요청이 스레드를 붙잡지 않도록

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        # 커스텀 헤더 필수 -> 다른 사이트에서의 교차 출처 요청(CSRF) 차단
        return secrets.compare_digest(self.headers.get("X-Deck-Token", ""), TOKEN)

    def host_ok(self):
        # DNS 리바인딩 방어: 브라우저가 보낸 Host 가 로컬 주소일 때만 응답 (토큰이 든 HTML 포함)
        if self.headers.get("Host", "").lower() in ALLOWED_HOSTS:
            return True
        self.send_json({"error": "invalid host"}, 421)
        return False

    def do_GET(self):
        if not self.host_ok():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            return self.route_get(u, q)
        except BadRequest as e:
            return self.send_json({"error": str(e)}, 400)

    def route_get(self, u, q):
        if u.path in ("/", "/index.html"):
            html = (BASE / "static" / "index.html").read_text(encoding="utf-8").replace("__DECK_TOKEN__", TOKEN)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.authorized():
            return self.send_json({"error": "unauthorized"}, 403)
        if u.path == "/api/state":
            return self.send_json(SCANNER.snapshot())
        if u.path == "/api/proc":
            d = SCANNER.proc_detail(to_int(q.get("pid"), "pid"))
            return self.send_json(d if d else {"error": "not found"}, 200 if d else 404)
        if u.path == "/api/log":
            off = to_int(q["offset"], "offset") if q.get("offset") not in (None, "") else None
            try:
                d = SCANNER.read_log(to_int(q.get("pid"), "pid"), q.get("path", ""), off)
            except OSError as e:
                return self.send_json({"error": str(e)}, 500)
            return self.send_json(d if d else {"error": "forbidden"}, 200 if d else 403)
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.host_ok():
            return
        if not self.authorized():
            return self.send_json({"error": "unauthorized"}, 403)
        u = urlparse(self.path)
        try:
            n = to_int(self.headers.get("Content-Length") or "0", "Content-Length")
            if n > 4096:
                return self.send_json({"error": "body too large"}, 413)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise BadRequest("invalid json")
            if not isinstance(data, dict):
                raise BadRequest("body must be an object")
            if u.path == "/api/refresh":
                SCANNER.wake.set()
                return self.send_json({"ok": True})
            if u.path == "/api/kill":
                return self.send_json(SCANNER.kill(to_int(data.get("pid"), "pid")))
        except BadRequest as e:
            return self.send_json({"error": str(e)}, 400)
        self.send_json({"error": "not found"}, 404)


class BadRequest(Exception):
    pass


def to_int(v, name):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BadRequest(f"invalid {name}")
    if not 0 <= n < 2**40:
        raise BadRequest(f"invalid {name}")
    return n


ALLOWED_HOSTS = {f"127.0.0.1:{CFG['port']}", f"localhost:{CFG['port']}"}


def main():
    threading.Thread(target=SCANNER.loop, daemon=True).start()
    srv = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    print(f"Port Deck  ->  http://{CFG['host']}:{CFG['port']}  (scan every {CFG['scan_interval_sec']}s)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
