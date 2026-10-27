"""GPU 사용량 수집.

- Windows: 성능 카운터(PDH)를 ctypes 로 직접 읽는다. 제조사(NVIDIA/AMD/Intel) 무관, 프로세스별 사용률/메모리.
- nvidia-smi 가 있으면 전체 사용률/VRAM/온도/전력을 보강한다 (Linux 는 이것만).
"""
import ctypes
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from ctypes import wintypes

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
PDH_NO_DATA = 0x800007D5


class _FmtValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _FmtItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _FmtValue)]


INST = re.compile(r"pid_(\d+)_luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)(?:_phys_\d+)?(?:_eng_\d+_engtype_(.*))?")


class PdhGpu:
    COUNTERS = {
        "engine": r"\GPU Engine(*)\Utilization Percentage",
        "proc_ded": r"\GPU Process Memory(*)\Dedicated Usage",
        "proc_shr": r"\GPU Process Memory(*)\Shared Usage",
        "ad_ded": r"\GPU Adapter Memory(*)\Dedicated Usage",
        "ad_shr": r"\GPU Adapter Memory(*)\Shared Usage",
    }

    def __init__(self):
        self.pdh = ctypes.WinDLL("pdh")
        for fn in ("PdhOpenQueryW", "PdhAddEnglishCounterW", "PdhCollectQueryData", "PdhGetFormattedCounterArrayW"):
            getattr(self.pdh, fn).restype = ctypes.c_uint32  # PDH 상태 코드는 0x8000xxxx (부호 없는 값)
        self.query = wintypes.HANDLE()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query)) != 0:
            raise OSError("PdhOpenQuery failed")
        self.handles = {}
        for k, path in self.COUNTERS.items():
            h = wintypes.HANDLE()
            if self.pdh.PdhAddEnglishCounterW(self.query, path, None, ctypes.byref(h)) == 0:
                self.handles[k] = h
        if "engine" not in self.handles:
            raise OSError("GPU Engine counter not available")
        self.pdh.PdhCollectQueryData(self.query)  # 사용률은 두 번째 수집부터 의미 있음

    def _array(self, h):
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        st = self.pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if st != PDH_MORE_DATA:
            return []
        buf = (ctypes.c_byte * size.value)()
        st = self.pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf)
        if st != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_FmtItem))
        return [(items[i].szName, items[i].FmtValue.doubleValue) for i in range(count.value) if items[i].FmtValue.CStatus in (0, 1)]

    def sample(self):
        if self.pdh.PdhCollectQueryData(self.query) != 0:
            return None
        # 엔진 종류(3D, Compute, Copy, VideoDecode...)별로 합산 후 가장 바쁜 엔진 = 작업 관리자 방식
        per_pid = defaultdict(lambda: defaultdict(float))
        per_adapter = defaultdict(lambda: defaultdict(float))
        for name, v in self._array(self.handles["engine"]):
            m = INST.match(name)
            if not m:
                continue
            pid, luid, eng = int(m.group(1)), m.group(2), m.group(3) or "?"
            per_pid[pid][eng] += v
            per_adapter[luid][eng] += v
        procs = {pid: {"util": min(100.0, max(e.values()))} for pid, e in per_pid.items()}
        for key, field in (("proc_ded", "mem_dedicated"), ("proc_shr", "mem_shared")):
            for name, v in self._array(self.handles.get(key)) if key in self.handles else []:
                m = INST.match(name)
                if m:
                    procs.setdefault(int(m.group(1)), {"util": 0.0})
                    procs[int(m.group(1))][field] = procs[int(m.group(1))].get(field, 0) + v
        adapters = {}
        for luid, e in per_adapter.items():
            adapters[luid] = {"util": min(100.0, max(e.values())), "engines": {k: round(min(100, v), 1) for k, v in e.items() if v > 0.05}}
        for key, field in (("ad_ded", "mem_dedicated"), ("ad_shr", "mem_shared")):
            for name, v in self._array(self.handles.get(key)) if key in self.handles else []:
                m = re.search(r"luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)", name)
                if m:
                    adapters.setdefault(m.group(1), {"util": 0.0, "engines": {}})
                    adapters[m.group(1)][field] = adapters[m.group(1)].get(field, 0) + v
        return {"procs": procs, "adapters": adapters}


def nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        gpus = []
        for line in out.strip().splitlines():
            f = [x.strip() for x in line.split(",")]
            num = lambda x: float(x) if re.fullmatch(r"[\d.]+", x) else None
            gpus.append({"index": int(f[0]), "name": f[1], "util": num(f[2]), "mem_used": (num(f[3]) or 0) * 2**20,
                         "mem_total": (num(f[4]) or 0) * 2**20, "temp": num(f[5]), "power": num(f[6])})
        procs = {}
        out = subprocess.run([exe, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.strip().splitlines():
            p, mem = [x.strip() for x in line.split(",")]
            if p.isdigit() and re.fullmatch(r"[\d.]+", mem):
                procs[int(p)] = float(mem) * 2**20
        return {"gpus": gpus, "procs": procs}
    except Exception:
        return None


def adapter_names():
    """Windows: 어댑터 이름 목록 (표시용)."""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_VideoController).Name"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        return []


class GpuMonitor:
    def __init__(self):
        self.pdh = None
        if sys.platform == "win32":
            try:
                self.pdh = PdhGpu()
            except OSError:
                self.pdh = None
        self.names = adapter_names()
        self.has_nvidia = shutil.which("nvidia-smi") is not None

    @property
    def available(self):
        return self.pdh is not None or self.has_nvidia

    def sample(self):
        """{"total": {...}, "procs": {pid: {"util", "mem"}}, "adapters": [...]} 또는 None"""
        if not self.available:
            return None
        procs, total = {}, {"util": None, "mem_used": None, "mem_total": None}
        adapters = []
        if self.pdh:
            s = self.pdh.sample()
            if s:
                for pid, d in s["procs"].items():
                    procs[pid] = {"util": d["util"], "mem": d.get("mem_dedicated", 0) + d.get("mem_shared", 0)}
                ads = sorted(s["adapters"].values(), key=lambda a: -(a.get("mem_dedicated", 0) + a.get("mem_shared", 0)))
                for i, a in enumerate(ads):
                    adapters.append({"name": self.names[i] if i < len(self.names) and len(ads) == len(self.names) else f"GPU {i}",
                                     "util": a["util"], "mem_dedicated": a.get("mem_dedicated", 0),
                                     "mem_shared": a.get("mem_shared", 0), "engines": a["engines"]})
                if adapters:
                    total["util"] = max(a["util"] for a in adapters)
                    total["mem_used"] = sum(a["mem_dedicated"] + a["mem_shared"] for a in adapters)
        if self.has_nvidia:
            n = nvidia_smi()
            if n and n["gpus"]:
                total["util"] = max([g["util"] or 0 for g in n["gpus"]] + [total["util"] or 0])
                total["mem_total"] = sum(g["mem_total"] for g in n["gpus"])
                if not self.pdh:
                    total["mem_used"] = sum(g["mem_used"] for g in n["gpus"])
                    for pid, mem in n["procs"].items():
                        procs.setdefault(pid, {"util": None, "mem": 0})["mem"] = mem
                total["nvidia"] = n["gpus"]
        if len(self.names) == 1 and len(adapters) > 1:
            # 가상 어댑터(원격 데스크톱 등)는 사용량이 없으면 숨김
            adapters = [a for a in adapters if a["util"] > 0.5 or a["mem_dedicated"] + a["mem_shared"] > 64 * 2**20] or adapters[:1]
            if len(adapters) == 1:
                adapters[0]["name"] = self.names[0]
        return {"total": total, "procs": procs, "adapters": adapters}


if __name__ == "__main__":
    import time
    g = GpuMonitor()
    print("available:", g.available, g.names)
    g.sample()
    time.sleep(1)
    t = time.time()
    s = g.sample()
    print("sample ms:", int((time.time() - t) * 1000))
    print("total:", s["total"])
    print("adapters:", s["adapters"])
    top = sorted(s["procs"].items(), key=lambda kv: -kv[1]["util"])[:5]
    print("top procs:", top)
