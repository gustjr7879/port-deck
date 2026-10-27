"""리소스/LLM 지표 수집과 저장 (Prometheus 스타일 시계열).

- Sampler: N초마다 시스템과 서비스별 CPU / 메모리 / GPU 를 SQLite 에 기록
- LLM 지표 출처 3가지
    1) scrape : LLM 서버가 /metrics 를 내주면 (vLLM, llama.cpp --metrics, SGLang, TGI) 히스토그램에서 P50/P95/P99 계산
    2) proxy  : 대시보드가 측정용 프록시 포트를 열고, 앱이 그 포트로 요청하면 요청마다 TTFT/지연/토큰 기록
    3) probe  : 짧은 테스트 요청을 직접 보내 측정 (버튼 또는 주기 설정)
"""
import http.client
import json
import math
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

GEN_PATHS = re.compile(r"/(v1/)?(chat/completions|completions)$|/api/(chat|generate)$")


def service_key(s):
    return f"{s.get('project_rel') or s['project']}/{s['app']}"


def percentile(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


# ----------------------------------------------------------------- storage

class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS series(ts INTEGER, key TEXT, metric TEXT, value REAL);
                CREATE INDEX IF NOT EXISTS series_idx ON series(key, metric, ts);
                CREATE TABLE IF NOT EXISTS llm_req(ts REAL, key TEXT, source TEXT, path TEXT, status INTEGER,
                    stream INTEGER, ttft_ms REAL, latency_ms REAL, prompt_tokens INTEGER, completion_tokens INTEGER,
                    tps REAL, error TEXT);
                CREATE INDEX IF NOT EXISTS llm_req_idx ON llm_req(key, ts);
            """)

    def add_points(self, rows):
        with self.lock:
            self.db.executemany("INSERT INTO series VALUES (?,?,?,?)", rows)
            self.db.commit()

    def add_llm(self, r):
        with self.lock:
            self.db.execute("INSERT INTO llm_req VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (r["ts"], r["key"], r["source"], r.get("path"), r.get("status"), int(bool(r.get("stream"))),
                             r.get("ttft_ms"), r.get("latency_ms"), r.get("prompt_tokens"), r.get("completion_tokens"),
                             r.get("tps"), r.get("error")))
            self.db.commit()

    def query(self, key, metric, start, step):
        with self.lock:
            cur = self.db.execute(
                "SELECT (ts/?)*? AS b, AVG(value), MAX(value) FROM series WHERE key=? AND metric=? AND ts>=? "
                "GROUP BY b ORDER BY b", (step, step, key, metric, int(start)))
            return [[b, round(a, 3), round(m, 3)] for b, a, m in cur.fetchall()]

    def has_metric(self, key, metric, start):
        with self.lock:
            return self.db.execute("SELECT 1 FROM series WHERE key=? AND metric=? AND ts>=? LIMIT 1",
                                   (key, metric, int(start))).fetchone() is not None

    def llm_rows(self, key, start):
        with self.lock:
            cur = self.db.execute("SELECT ts, source, path, status, stream, ttft_ms, latency_ms, prompt_tokens, "
                                  "completion_tokens, tps, error FROM llm_req WHERE key=? AND ts>=? ORDER BY ts",
                                  (key, start))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def prune(self, days):
        cutoff = time.time() - days * 86400
        with self.lock:
            self.db.execute("DELETE FROM series WHERE ts < ?", (int(cutoff),))
            self.db.execute("DELETE FROM llm_req WHERE ts < ?", (cutoff,))
            self.db.commit()


# ----------------------------------------------------------------- stream measuring

class StreamMeter:
    """OpenAI SSE / Ollama NDJSON / 일반 JSON 응답을 흘려보내며 TTFT 와 토큰 수를 잰다."""

    def __init__(self, t0):
        self.t0 = t0
        self.ttft = None
        self.chunks = 0
        self.usage = {}
        self.buf = b""
        self.body = b""
        self.streaming = False

    def feed(self, data):
        now = time.perf_counter()
        if len(self.body) < 4 * 2**20:
            self.body += data
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self._line(line.strip(), now)

    def _line(self, line, now):
        if line.startswith(b"data:"):
            self.streaming = True
            line = line[5:].strip()
            if line == b"[DONE]":
                return
        if not line.startswith(b"{"):
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        text = ""
        if isinstance(obj.get("choices"), list) and obj["choices"]:
            c = obj["choices"][0]
            text = (c.get("delta") or {}).get("content") or c.get("text") or ""
        elif "message" in obj or "response" in obj:  # ollama
            self.streaming = self.streaming or not obj.get("done", True) or self.chunks > 0
            text = (obj.get("message") or {}).get("content") or obj.get("response") or ""
            if obj.get("done"):
                self.usage.update(prompt_tokens=obj.get("prompt_eval_count"), completion_tokens=obj.get("eval_count"))
                if obj.get("eval_duration"):
                    self.usage["tps"] = obj["eval_count"] / (obj["eval_duration"] / 1e9)
        if text:
            self.chunks += 1
            if self.ttft is None:
                self.ttft = now - self.t0
        if obj.get("usage"):
            u = obj["usage"]
            self.usage.update(prompt_tokens=u.get("prompt_tokens"), completion_tokens=u.get("completion_tokens"))

    def result(self, end):
        if self.buf:
            self._line(self.buf.strip(), end)
            self.buf = b""
        if not self.streaming:  # 비스트리밍: 마지막에 한 번에 받은 JSON
            try:
                obj = json.loads(self.body)
                u = obj.get("usage") or {}
                self.usage.setdefault("prompt_tokens", u.get("prompt_tokens") or obj.get("prompt_eval_count"))
                self.usage.setdefault("completion_tokens", u.get("completion_tokens") or obj.get("eval_count"))
                if obj.get("eval_duration") and obj.get("eval_count"):
                    self.usage["tps"] = obj["eval_count"] / (obj["eval_duration"] / 1e9)
            except ValueError:
                pass
        latency = end - self.t0
        out_tok = self.usage.get("completion_tokens") or (self.chunks if self.streaming else None)
        tps = self.usage.get("tps")
        if tps is None and out_tok and self.streaming and self.ttft:
            # 생성 속도 = 첫 토큰 이후 구간. 비스트리밍은 prefill 이 섞여 왜곡되므로 계산하지 않음
            gen = latency - self.ttft
            tps = (out_tok - 1) / gen if gen > 0 and out_tok > 1 else None
        return {"stream": self.streaming, "ttft_ms": self.ttft * 1000 if (self.streaming and self.ttft) else None,
                "latency_ms": latency * 1000, "prompt_tokens": self.usage.get("prompt_tokens"),
                "completion_tokens": out_tok, "tps": tps}


# ----------------------------------------------------------------- proxy

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "host"}


def make_proxy_handler(store, key, upstream):
    class ProxyHandler(BaseHTTPRequestHandler):
        timeout = 600

        def log_message(self, *a):
            pass

        def _forward(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            measure = self.command == "POST" and GEN_PATHS.search(self.path.split("?")[0])
            t0 = time.perf_counter()
            meter = StreamMeter(t0)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            conn = http.client.HTTPConnection(*upstream, timeout=600)
            status, err = None, None
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if measure:
                        meter.feed(chunk)
            except (OSError, http.client.HTTPException) as e:
                err = str(e)
                if status is None:
                    try:
                        self.send_error(502, "upstream error")
                    except OSError:
                        pass
            finally:
                conn.close()
            if measure:
                r = meter.result(time.perf_counter())
                r.update(ts=time.time(), key=key, source="proxy", path=self.path, status=status, error=err)
                store.add_llm(r)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward

        def do_OPTIONS(self):
            self._forward()

    return ProxyHandler


class Proxies:
    def __init__(self, store, path):
        self.store = store
        self.path = Path(path)
        self.running = {}  # key -> (server, info)
        self.lock = threading.Lock()

    def saved(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self):
        data = {k: info for k, (_, info) in self.running.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def start(self, key, upstream_port, listen_port=None, save=True):
        with self.lock:
            if key in self.running:
                return self.running[key][1]
            port = listen_port or free_port(upstream_port + 10000 if upstream_port + 10000 < 65536 else 0)
            srv = ThreadingHTTPServer(("127.0.0.1", port), make_proxy_handler(self.store, key, ("127.0.0.1", upstream_port)))
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            info = {"listen": port, "upstream": upstream_port}
            self.running[key] = (srv, info)
            if save:
                self._save()
            return info

    def stop(self, key):
        with self.lock:
            item = self.running.pop(key, None)
            if item:
                item[0].shutdown()
                item[0].server_close()
                self._save()

    def info(self, key):
        item = self.running.get(key)
        return item[1] if item else None

    def restore(self):
        for key, info in self.saved().items():
            try:
                self.start(key, info["upstream"], info["listen"], save=False)
            except OSError as e:
                print(f"proxy restore failed {key}: {e}", file=sys.stderr)


def free_port(preferred):
    for p in ([preferred] if preferred else []) + [0]:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return s.getsockname()[1]
            except OSError:
                continue
    raise OSError("no free port")


# ----------------------------------------------------------------- probe

PROBE_PROMPT = "Count from 1 to 20, separated by spaces. Output only the numbers."


def probe(store, key, endpoint, api, model, max_tokens=48):
    """짧은 스트리밍 요청 하나로 TTFT / 응답시간 / 생성 속도 측정."""
    r = _probe_once(endpoint, api, model, max_tokens, usage_opt=True)
    if r["status"] == 400 and api != "ollama":  # stream_options 를 모르는 서버
        r = _probe_once(endpoint, api, model, max_tokens, usage_opt=False)
    r.update(ts=time.time(), key=key, source="probe")
    store.add_llm(r)
    return r


def _probe_once(endpoint, api, model, max_tokens, usage_opt):
    m = re.match(r"http://(\[[^\]]+\]|[^:/]+):(\d+)(/.*)?", endpoint)
    host, port, base = m.group(1).strip("[]"), int(m.group(2)), (m.group(3) or "").rstrip("/")
    if api == "ollama":
        path = base + "/api/chat"
        payload = {"model": model, "stream": True, "messages": [{"role": "user", "content": PROBE_PROMPT}],
                   "options": {"num_predict": max_tokens}}
    else:
        path = base + "/chat/completions"
        payload = {"model": model, "stream": True, "max_tokens": max_tokens, "temperature": 0,
                   "messages": [{"role": "user", "content": PROBE_PROMPT}]}
        if usage_opt:
            payload["stream_options"] = {"include_usage": True}
    conn = http.client.HTTPConnection(host, port, timeout=300)
    t0 = time.perf_counter()
    meter = StreamMeter(t0)
    status, err = None, None
    try:
        conn.request("POST", path, body=json.dumps(payload), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                break
            meter.feed(chunk)
        if status >= 400:
            err = meter.body[:300].decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as e:
        err = str(e)
    finally:
        conn.close()
    r = meter.result(time.perf_counter())
    r.update(path=path, status=status, error=err)
    return r


# ----------------------------------------------------------------- /metrics scrape

INF = float("inf")


def parse_prom(text):
    hist = defaultdict(lambda: defaultdict(float))
    scalars = defaultdict(float)
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+(\S+)", line)
        if not m:
            continue
        name, labels, raw = m.groups()
        try:
            v = float(raw)
        except ValueError:
            continue
        if math.isnan(v):
            continue
        if name.endswith("_bucket"):
            le = re.search(r'le="([^"]+)"', labels or "")
            if le:
                hist[name[:-7]][INF if le.group(1) in ("+Inf", "Inf") else float(le.group(1))] += v
        else:
            scalars[name] += v
    return hist, scalars


def hist_quantile(buckets, q):
    """누적 버킷 {le: count} 에서 분위수 (Prometheus histogram_quantile 과 같은 선형 보간)."""
    items = sorted(buckets.items())
    if not items or items[-1][1] <= 0:
        return None
    total = items[-1][1]
    target = q * total
    prev_le, prev_c = 0.0, 0.0
    for le, c in items:
        if c >= target:
            if le == INF:
                return prev_le
            return prev_le + (le - prev_le) * ((target - prev_c) / (c - prev_c) if c > prev_c else 0)
        prev_le, prev_c = le, c
    return prev_le


def find(d, pattern):
    return next((k for k in d if re.search(pattern, k)), None)


SCRAPE = {
    "ttft": r"time_to_first_token_seconds$",
    "lat": r"(e2e_request_latency|request_duration|request_latency)_seconds$|tgi_request_duration$",
    "tpot": r"(time_per_output_token|inter_token_latency)_seconds$",
}


# ----------------------------------------------------------------- sampler

class Sampler:
    def __init__(self, cfg, base, get_services, gpu):
        self.cfg = cfg
        self.interval = cfg.get("metrics_interval_sec", 15)
        self.store = Store(base / "data" / "metrics.db")
        self.proxies = Proxies(self.store, base / "data" / "proxies.json")
        self.get_services = get_services
        self.gpu = gpu
        self.procs = {}
        self.latest = {}   # key -> {cpu, mem, gpu, gpu_mem, pid}
        self.system = {}
        self.gpu_info = None
        self.prom_prev = {}  # key -> (ts, hist, scalars)
        self.prom_absent = {}  # key -> 다음 재시도 시각
        self.last_probe = {}
        self.ncpu = psutil.cpu_count() or 1
        psutil.cpu_percent(None)

    def _proc(self, pid):
        p = self.procs.get(pid)
        if p is None or not p.is_running():
            try:
                p = psutil.Process(pid)
                p.cpu_percent(None)
            except psutil.Error:
                return None
            self.procs[pid] = p
        return p

    def tick(self):
        now = int(time.time())
        rows = []
        gpu = self.gpu.sample() if self.gpu.available else None
        self.gpu_info = gpu
        vm = psutil.virtual_memory()
        sysm = {"cpu": psutil.cpu_percent(None), "mem": vm.used, "mem_pct": vm.percent, "mem_total": vm.total}
        if gpu:
            sysm["gpu"] = gpu["total"]["util"]
            sysm["gpu_mem"] = gpu["total"]["mem_used"]
        self.system = sysm
        rows += [(now, "_system", k, v) for k, v in sysm.items() if v is not None and k != "mem_total"]

        latest, alive = {}, set()
        for s in self.get_services():
            key = service_key(s)
            p = self._proc(s["pid"])
            if p is None:
                continue
            tree = [p]
            try:
                tree += p.children(recursive=True)
            except psutil.Error:
                pass
            cpu = mem = 0.0
            gutil = gmem = 0.0
            for q in tree:
                alive.add(q.pid)
                q = self._proc(q.pid)
                if q is None:
                    continue
                try:
                    cpu += q.cpu_percent(None)
                    mem += q.memory_info().rss
                except psutil.Error:
                    pass
                if gpu and q.pid in gpu["procs"]:
                    gutil += gpu["procs"][q.pid]["util"] or 0
                    gmem += gpu["procs"][q.pid]["mem"] or 0
            d = {"pid": s["pid"], "cpu": cpu / self.ncpu, "mem": mem}
            if gpu:
                d.update(gpu=min(100.0, gutil), gpu_mem=gmem)
            latest[key] = d
            rows += [(now, key, k, v) for k, v in d.items() if k != "pid"]
            if s["kind"] == "llm":
                rows += self._scrape(key, s, now)
                self._maybe_probe(key, s)
        for pid in list(self.procs):
            if pid not in alive:
                self.procs.pop(pid, None)
        self.latest = latest
        self.store.add_points(rows)

    def _scrape(self, key, s, now):
        if self.prom_absent.get(key, 0) > now:
            return []
        port = s["ports"][0]["port"]
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
            conn.request("GET", "/metrics")
            resp = conn.getresponse()
            text = resp.read(4 * 2**20).decode("utf-8", "replace") if resp.status == 200 else ""
            conn.close()
        except (OSError, http.client.HTTPException):
            text = ""
        if "# TYPE" not in text and "_bucket" not in text:
            self.prom_absent[key] = now + 300  # 없으면 5분 뒤 재확인
            return []
        hist, scal = parse_prom(text)
        prev = self.prom_prev.get(key)
        self.prom_prev[key] = (now, hist, scal)
        rows = []
        run = find(scal, r"num_requests_running$|requests_processing$|batch_current_size$")
        if run:
            rows.append((now, key, "llm_running", scal[run]))
        if not prev:
            return rows
        dt = max(1, now - prev[0])
        for short, pat in SCRAPE.items():
            name = find(hist, pat)
            if not name or name not in prev[1]:
                continue
            delta = {le: hist[name][le] - prev[1][name].get(le, 0) for le in hist[name]}
            if any(v < 0 for v in delta.values()):  # 서버 재시작으로 카운터 리셋
                continue
            if short == "lat":
                rows.append((now, key, "llm_req", max(delta.values()) * 60 / dt))
            for q in (50, 95, 99):
                v = hist_quantile(delta, q / 100)
                if v is not None and max(delta.values()) > 0:
                    rows.append((now, key, f"llm_{short}_p{q}", v * 1000))
        gen = find(scal, r"generation_tokens_total$|tokens_predicted_total$|generated_tokens_total$")
        if gen and gen in prev[2] and scal[gen] >= prev[2][gen]:
            rows.append((now, key, "llm_tps", (scal[gen] - prev[2][gen]) / dt))
        return rows

    def _maybe_probe(self, key, s):
        every = self.cfg.get("llm_probe_interval_min", 0)
        if not every or not s.get("llm_endpoint") or not s.get("llm_models"):
            return
        if time.time() - self.last_probe.get(key, 0) < every * 60:
            return
        self.last_probe[key] = time.time()
        threading.Thread(target=probe, args=(self.store, key, s["llm_endpoint"], s["llm_api"], s["llm_models"][0]),
                         daemon=True).start()

    def probe(self, key, s):
        self.last_probe[key] = time.time()
        return probe(self.store, key, s["llm_endpoint"], s["llm_api"], s["llm_models"][0])

    def loop(self):
        self.proxies.restore()
        last_prune = 0
        while True:
            t0 = time.time()
            try:
                self.tick()
                if t0 - last_prune > 3600:
                    self.store.prune(self.cfg.get("metrics_retention_days", 7))
                    last_prune = t0
            except Exception as e:
                print("sampler error:", repr(e), file=sys.stderr)
            time.sleep(max(1, self.interval - (time.time() - t0)))

    # ------------------------------------------------------------- queries

    def step_for(self, rng):
        return max(self.interval, int(rng / 120 // self.interval * self.interval) or self.interval)

    def series(self, key, rng, metrics=("cpu", "mem", "gpu", "gpu_mem")):
        start, step = time.time() - rng, self.step_for(rng)
        return {"step": step, "start": int(start), "end": int(time.time()),
                "metrics": {m: self.store.query(key, m, start, step) for m in metrics}}

    def llm(self, key, rng):
        start, step = time.time() - rng, self.step_for(max(rng, 300))
        rows = self.store.llm_rows(key, start)
        out = {"step": step, "start": int(start), "end": int(time.time()), "proxy": self.proxies.info(key),
               "recent": rows[-25:][::-1], "source": None}
        if rows:
            ok = [r for r in rows if not r["error"] and (r["status"] or 0) < 400]
            col = lambda k, rs: [r[k] for r in rs if r[k] is not None]
            out["source"] = "requests"
            out["summary"] = {
                "count": len(rows), "errors": len(rows) - len(ok),
                "sources": sorted({r["source"] for r in rows}),
                **{f"ttft_p{q}": percentile(col("ttft_ms", ok), q / 100) for q in (50, 95, 99)},
                **{f"lat_p{q}": percentile(col("latency_ms", ok), q / 100) for q in (50, 95, 99)},
                "tps": (sum(col("tps", ok)) / len(col("tps", ok))) if col("tps", ok) else None,
                "out_tokens": sum(col("completion_tokens", ok)),
            }
            # 이동 창 분위수 (Prometheus 의 histogram_quantile(rate[5m]) 과 같은 개념):
            # 각 시점 값 = 직전 window 안의 요청들로 계산. 요청이 드문드문해도 선이 이어지고 P50/P95/P99 가 의미를 가짐
            step = max(60, int(rng / 60))
            window = max(300, step * 2)
            out["step"], out["window"] = step, window
            ser = defaultdict(list)
            lo = 0
            t = int(start // step * step) + step
            while t <= time.time() + step:
                while lo < len(ok) and ok[lo]["ts"] <= t - window:
                    lo += 1
                rs = [r for r in ok[lo:] if r["ts"] <= t]
                if rs:
                    for q in (50, 95, 99):
                        for short, k in (("ttft", "ttft_ms"), ("lat", "latency_ms")):
                            v = percentile(col(k, rs), q / 100)
                            if v is not None:
                                ser[f"{short}_p{q}"].append([t, round(v, 1)])
                    if col("tps", rs):
                        ser["tps"].append([t, round(sum(col("tps", rs)) / len(col("tps", rs)), 2)])
                ser["req"].append([t, round(len(rs) * 60 / window, 2)])
                t += step
            out["series"] = ser
        elif self.store.has_metric(key, "llm_lat_p99", start) or self.store.has_metric(key, "llm_ttft_p99", start):
            out["source"] = "scrape"
            names = [f"llm_{s}_p{q}" for s in ("ttft", "lat", "tpot") for q in (50, 95, 99)] + ["llm_tps", "llm_req", "llm_running"]
            ser = {n[4:]: [[b, a] for b, a, _ in self.store.query(key, n, start, step)] for n in names}
            out["series"] = ser
            last = lambda n: ser[n][-1][1] if ser.get(n) else None
            out["summary"] = {"count": None, "errors": None, "sources": ["scrape"],
                              **{f"{s}_p{q}": last(f"{s}_p{q}") for s in ("ttft", "lat") for q in (50, 95, 99)},
                              "tps": last("tps"), "running": last("running")}
        out["scrape_available"] = key in self.prom_prev
        return out

    def prometheus(self, services):
        """현재 값을 Prometheus 텍스트 형식으로 (Grafana/Prometheus 에서 수집 가능)."""
        esc = lambda v: str(v).replace("\\", "\\\\").replace('"', '\\"')
        lines = []

        def g(name, help_, samples):
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} gauge")
            for labels, v in samples:
                if v is None:
                    continue
                lab = ",".join(f'{k}="{esc(x)}"' for k, x in labels.items())
                lines.append(f"{name}{{{lab}}} {v}" if lab else f"{name} {v}")

        sm = self.system
        g("portdeck_system_cpu_percent", "System CPU usage", [({}, sm.get("cpu"))])
        g("portdeck_system_memory_used_bytes", "System memory used", [({}, sm.get("mem"))])
        g("portdeck_system_gpu_percent", "Busiest GPU engine utilization", [({}, sm.get("gpu"))])
        g("portdeck_system_gpu_memory_used_bytes", "GPU memory used (dedicated+shared)", [({}, sm.get("gpu_mem"))])
        per = []
        for s in services:
            k = service_key(s)
            if k in self.latest:
                per.append(({"project": s["project"], "app": s["app"], "kind": s["kind"],
                             "port": s["ports"][0]["port"], "pid": s["pid"]}, self.latest[k]))
        for metric, name, help_ in (("cpu", "portdeck_process_cpu_percent", "Process tree CPU (% of all cores)"),
                                    ("mem", "portdeck_process_memory_bytes", "Process tree RSS"),
                                    ("gpu", "portdeck_process_gpu_percent", "Process tree GPU utilization"),
                                    ("gpu_mem", "portdeck_process_gpu_memory_bytes", "Process tree GPU memory")):
            g(name, help_, [(lab, d.get(metric)) for lab, d in per])
        llm = []
        for s in services:
            if s["kind"] != "llm":
                continue
            d = self.llm(service_key(s), 300).get("summary") or {}
            lab = {"project": s["project"], "app": s["app"], "port": s["ports"][0]["port"]}
            for q in (50, 95, 99):
                llm.append(("portdeck_llm_ttft_ms", {**lab, "quantile": f"0.{q}"}, d.get(f"ttft_p{q}")))
                llm.append(("portdeck_llm_latency_ms", {**lab, "quantile": f"0.{q}"}, d.get(f"lat_p{q}")))
            llm.append(("portdeck_llm_tokens_per_second", lab, d.get("tps")))
        for name, help_ in (("portdeck_llm_ttft_ms", "Time to first token, last 5m"),
                            ("portdeck_llm_latency_ms", "Request latency, last 5m"),
                            ("portdeck_llm_tokens_per_second", "Generation speed, last 5m")):
            g(name, help_, [(lab, v) for n, lab, v in llm if n == name])
        return "\n".join(lines) + "\n"
