"""CPU / RAM / GPU 사용량을 외부 프로세스 없이 Win32 API 로 읽는다.

- CPU: GetSystemTimes 두 시점의 차이
- RAM: GlobalMemoryStatusEx
- GPU: 성능 카운터(PDH) "GPU Engine" 사용률 — 작업 관리자와 같은 방식으로 엔진 종류별 합 중 최댓값
       VRAM: "GPU Adapter Memory" 전용 메모리 사용량 / 레지스트리의 어댑터 메모리 크기
"""
import ctypes
import ctypes.wintypes as wt
import re
import winreg

k32 = ctypes.windll.kernel32


class _MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def ram():
    """(사용률 %, 사용 GB, 전체 GB)"""
    m = _MEMSTAT()
    m.dwLength = ctypes.sizeof(m)
    k32.GlobalMemoryStatusEx(ctypes.byref(m))
    total = m.ullTotalPhys / 2**30
    used = (m.ullTotalPhys - m.ullAvailPhys) / 2**30
    return used / total * 100, used, total


class CPU:
    def __init__(self):
        self._prev = self._times()

    @staticmethod
    def _times():
        idle, kern, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        f = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
        return f(idle), f(kern), f(user)

    def read(self):
        """직전 호출 이후 CPU 사용률 %"""
        cur = self._times()
        di, dk, du = (c - p for c, p in zip(cur, self._prev))
        self._prev = cur
        total = dk + du          # kernel 시간에 idle 이 포함됨
        return max(0.0, min(100.0, (total - di) / total * 100)) if total else 0.0


# ---------------------------------------------------------------- GPU (PDH)

pdh = ctypes.windll.pdh
PDH_FMT_DOUBLE = 0x200
PDH_FMT_NOCAP100 = 0x8000
PDH_MORE_DATA = 0x800007D2


class _FMTVAL(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("_pad", wt.DWORD), ("doubleValue", ctypes.c_double)]


class _FMTITEM(ctypes.Structure):
    _fields_ = [("szName", wt.LPWSTR), ("FmtValue", _FMTVAL)]


class GPU:
    def __init__(self):
        self.ok = False
        self.q = wt.HANDLE()
        self.c_util = wt.HANDLE()
        self.c_mem = wt.HANDLE()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.q)):
            return
        if pdh.PdhAddEnglishCounterW(self.q, r"\GPU Engine(*)\Utilization Percentage", 0,
                                     ctypes.byref(self.c_util)):
            return
        if pdh.PdhAddEnglishCounterW(self.q, r"\GPU Adapter Memory(*)\Dedicated Usage", 0,
                                     ctypes.byref(self.c_mem)):
            self.c_mem = None
        pdh.PdhCollectQueryData(self.q)   # 사용률은 두 번 수집해야 값이 나온다
        self.vram_total = _vram_total()
        self.ok = True

    def _array(self, counter):
        size, count = wt.DWORD(0), wt.DWORD(0)
        r = pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100,
                                             ctypes.byref(size), ctypes.byref(count), None)
        if r & 0xFFFFFFFF != PDH_MORE_DATA:
            return []
        buf = (ctypes.c_byte * size.value)()
        if pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100,
                                            ctypes.byref(size), ctypes.byref(count), buf):
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_FMTITEM))
        return [(items[i].szName, items[i].FmtValue.doubleValue) for i in range(count.value)
                if items[i].FmtValue.CStatus in (0, 1)]

    def read(self):
        """(사용률 %, VRAM 사용 GB 또는 None, VRAM 전체 GB 또는 None)"""
        if not self.ok or pdh.PdhCollectQueryData(self.q):
            return None, None, None
        per_engine = {}
        for name, v in self._array(self.c_util):
            m = re.search(r"(luid_\w+?_phys_\d+).*engtype_(\w+)", name)
            if m:
                key = (m.group(1), m.group(2))
                per_engine[key] = per_engine.get(key, 0.0) + v
        util = min(100.0, max(per_engine.values(), default=0.0))
        used = None
        if self.c_mem:
            mem = [v for _, v in self._array(self.c_mem)]
            used = max(mem) / 2**30 if mem else None
        return util, used, self.vram_total


def _vram_total():
    """디스플레이 어댑터 중 가장 큰 전용 메모리(GB). 못 찾으면 None."""
    best = 0
    path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as cls:
            for i in range(64):
                try:
                    sub = winreg.EnumKey(cls, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(cls, sub) as k:
                        v, _ = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")
                        best = max(best, int(v if isinstance(v, int) else int.from_bytes(v, "little")))
                except OSError:
                    continue
    except OSError:
        return None
    return best / 2**30 if best else None


if __name__ == "__main__":
    import time
    cpu, gpu = CPU(), GPU()
    for _ in range(3):
        time.sleep(1)
        print("cpu %.1f%%" % cpu.read(), "ram %.0f%% %.1f/%.1fG" % ram(), "gpu", gpu.read())
