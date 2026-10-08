"""각 AI 도구의 사용량 한도(5시간/주간)를 로컬 데이터와 공식 클라이언트 엔드포인트에서 수집한다.

모든 시각은 epoch 초(float), 잔여량은 0~100(%)으로 정규화한다.
결과 형식:
{
  "name": str, "plan": str|None, "ok": bool, "note": str|None, "fetched_at": float,
  "windows": [{"label": str, "remaining": float|None, "resets_at": float|None, "missing": bool}]
}
"""
import base64
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import ssl
import struct
import subprocess
import time
import urllib.request

HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
CACHE_DIR = os.path.join(HOME, ".ai-token-widget")
CACHE_FILE = os.path.join(CACHE_DIR, "cache.json")
TIMEOUT = 10
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- helpers

def _http_json(url, headers, data=None, insecure=False):
    req = urllib.request.Request(url, headers=headers, data=data)
    ctx = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def _iso_to_epoch(s):
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _window(label, remaining=None, resets_at=None, missing=False):
    if remaining is not None:
        remaining = max(0.0, min(100.0, float(remaining)))
    return {"label": label, "remaining": remaining, "resets_at": resets_at, "missing": missing}


def _result(name, windows, plan=None, ok=True, note=None):
    return {"name": name, "plan": plan, "ok": ok, "note": note,
            "fetched_at": time.time(), "windows": windows}


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_FILE)


# ---------------------------------------------------------------- Claude Code

CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_TOKEN_URLS = ("https://platform.claude.com/v1/oauth/token",
                     "https://console.anthropic.com/v1/oauth/token")


def _claude_refresh(path):
    """만료된 access token 을 refresh token 으로 갱신하고 credentials 파일에 되써 넣는다.

    데스크톱 앱은 이 파일이 아닌 자체 인증을 쓰므로, 파일 토큰은 CLI 를 쓰지 않으면
    만료된 채로 남는다. Claude Code CLI 와 같은 방식으로 갱신하고 다른 필드는 보존한다.
    """
    with open(path, encoding="utf-8") as f:
        creds = json.load(f)
    oauth = creds["claudeAiOauth"]
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": oauth["refreshToken"],
                       "client_id": CLAUDE_CLIENT_ID}).encode()
    last = None
    for url in CLAUDE_TOKEN_URLS:
        try:
            tok = _http_json(url, {"Content-Type": "application/json", "User-Agent": "ai-token-widget"}, body)
            break
        except Exception as e:
            last = e
    else:
        raise PermissionError(f"토큰 갱신 실패 ({last}) · 터미널에서 claude 를 한 번 실행하세요")
    # 갱신 사이에 다른 프로세스(CLI)가 파일을 바꿨다면 그쪽 값을 존중한다
    with open(path, encoding="utf-8") as f:
        latest = json.load(f)
    if latest["claudeAiOauth"].get("refreshToken") != oauth["refreshToken"]:
        return latest["claudeAiOauth"]
    oauth["accessToken"] = tok["access_token"]
    if tok.get("refresh_token"):
        oauth["refreshToken"] = tok["refresh_token"]
    oauth["expiresAt"] = int((time.time() + tok.get("expires_in", 3600)) * 1000)
    if tok.get("scope"):
        oauth["scopes"] = tok["scope"].split()
    tmp = path + ".widget-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(latest | {"claudeAiOauth": oauth}, f)
    os.replace(tmp, path)
    return oauth


def fetch_claude():
    """~/.claude/.credentials.json 의 OAuth 토큰으로 /api/oauth/usage 조회 (만료 시 자동 갱신)."""
    path = os.path.join(HOME, ".claude", ".credentials.json")
    with open(path, encoding="utf-8") as f:
        oauth = json.load(f)["claudeAiOauth"]
    plan = (oauth.get("subscriptionType") or "").capitalize() or None
    if oauth.get("expiresAt", 0) / 1000 < time.time() + 60:
        oauth = _claude_refresh(path)
    data = _http_json("https://api.anthropic.com/api/oauth/usage", {
        "Authorization": "Bearer " + oauth["accessToken"],
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "ai-token-widget",
    })
    windows = []
    for key, label in (("five_hour", "5시간"), ("seven_day", "주간")):
        w = data.get(key)
        if w:
            windows.append(_window(label, 100 - (w.get("utilization") or 0), _iso_to_epoch(w.get("resets_at"))))
        else:
            windows.append(_window(label, missing=True))
    for key, label in (("seven_day_opus", "주간 Opus"), ("seven_day_sonnet", "주간 Sonnet")):
        w = data.get(key)
        if w and w.get("utilization") is not None:
            windows.append(_window(label, 100 - w["utilization"], _iso_to_epoch(w.get("resets_at"))))
    return _result("Claude Code", windows, plan)


# ---------------------------------------------------------------- Codex

def _codex_windows(win_list):
    """[(window_seconds, used_percent, resets_at)] -> 5시간/주간 행."""
    five = week = None
    for secs, used, reset in win_list:
        if secs is None:
            continue
        if secs <= 6 * 3600:
            five = (used, reset)
        else:
            week = (used, reset)
    out = []
    for label, w in (("5시간", five), ("주간", week)):
        out.append(_window(label, 100 - w[0], w[1]) if w else _window(label, missing=True))
    return out


def fetch_codex():
    with open(os.path.join(HOME, ".codex", "auth.json"), encoding="utf-8") as f:
        tokens = json.load(f)["tokens"]
    try:
        data = _http_json("https://chatgpt.com/backend-api/wham/usage", {
            "Authorization": "Bearer " + tokens["access_token"],
            "ChatGPT-Account-Id": tokens.get("account_id", ""),
            "User-Agent": "codex_cli_rs",
            "Accept": "application/json",
        })
        rl = data.get("rate_limit") or {}
        wins = []
        for k in ("primary_window", "secondary_window"):
            w = rl.get(k)
            if w:
                wins.append((w.get("limit_window_seconds"), w.get("used_percent") or 0, w.get("reset_at")))
        plan = (data.get("plan_type") or "").capitalize() or None
        return _result("Codex", _codex_windows(wins), plan)
    except Exception as e:
        res = _codex_from_logs()
        if res:
            res["note"] = "세션 로그 기준"
            return res
        raise e


def _codex_from_logs():
    """API 실패 시 가장 최근 세션 로그의 rate_limits 이벤트를 사용."""
    files = glob.glob(os.path.join(HOME, ".codex", "sessions", "**", "*.jsonl"), recursive=True)
    for path in sorted(files, key=os.path.getmtime, reverse=True)[:5]:
        last = None
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"rate_limits"' in line:
                    last = line
        if not last:
            continue
        m = json.loads(last)
        rl = _find_key(m, "rate_limits")
        if not rl:
            continue
        wins = []
        for k in ("primary", "secondary"):
            w = rl.get(k)
            if w:
                wins.append(((w.get("window_minutes") or 0) * 60, w.get("used_percent") or 0, w.get("resets_at")))
        res = _result("Codex", _codex_windows(wins), (rl.get("plan_type") or "").capitalize() or None)
        res["fetched_at"] = os.path.getmtime(path)
        return res
    return None


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


# ---------------------------------------------------------------- Antigravity

def _family(model_name):
    """'Gemini 3.8 Flash (High)' -> 'Gemini Flash', 'Claude Opus 5.5 (Low)' -> 'Claude Opus'."""
    base = model_name.split(" (")[0]
    words = [w for w in base.split() if not re.fullmatch(r"[\d.]+", w)]
    return " ".join(words) or base


def _group_models(models, fetched_at):
    """models: [(name, remaining_fraction, reset_epoch)] -> 쿼터가 같은 모델끼리 묶은 행."""
    groups = {}
    for name, frac, reset in models:
        groups.setdefault((round(frac, 4), reset), []).append(_family(name))
    rows = []
    for (frac, reset), fams in groups.items():
        uniq = list(dict.fromkeys(fams))
        # 'Gemini Flash', 'Gemini Pro' -> 'Gemini Flash·Pro'
        merged = {}
        for f in uniq:
            head, _, tail = f.partition(" ")
            merged.setdefault(head, []).append(tail)
        label = " / ".join(h + (" " + "·".join(t for t in tails if t) if any(tails) else "")
                           for h, tails in merged.items())
        # 모델마다 쿼터가 하나뿐이라, 리셋까지 남은 시간으로 5시간/주간 창을 구분한다
        left = (reset or 0) - fetched_at
        period = "주간" if left > 6 * 3600 else "5시간" if left > 0 else None
        w = _window(f"{period} · {label}" if period else label, frac * 100, reset)
        w["period"] = period
        if reset and reset < time.time():
            w["remaining"], w["estimated"] = 100.0, True  # 리셋 시각이 지났으면 회복된 것으로 간주
        rows.append(w)
    rows.sort(key=lambda w: (w["period"] != "5시간", w["label"]))
    return rows


AG_EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Antigravity IDE", "Antigravity IDE.exe")
AG_BG_INTERVAL = 30 * 60   # 앱이 꺼져 있을 때 백그라운드 실행으로 재조회하는 최소 간격
AG_BG_TIMEOUT = 90
AG_BG_NOTE = "백그라운드 조회 · 30분마다 갱신"


def fetch_antigravity():
    try:
        models, plan = _antigravity_live()
        note = None
    except Exception:
        prev = load_cache().get("Antigravity") or {}
        if prev.get("live") and time.time() - (prev.get("fetched_at") or 0) < AG_BG_INTERVAL:
            return dict(prev, note=AG_BG_NOTE)
        try:
            models, plan = _antigravity_background()
            note = AG_BG_NOTE
        except Exception as e:
            models, plan, mtime = _antigravity_cached()
            res = _result("Antigravity", _group_models(models, mtime), plan,
                          note=f"앱이 마지막으로 저장한 값 ({e})")
            res["fetched_at"] = mtime
            return res
    res = _result("Antigravity", _group_models(models, time.time()), plan, note=note)
    res["live"] = True
    return res


def _ps_json(script):
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script + " | ConvertTo-Json -Compress"],
                         capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout.strip()
    if not out:
        return []
    v = json.loads(out)
    return v if isinstance(v, list) else [v]


def _antigravity_background():
    """Antigravity IDE 를 최소화 상태로 잠깐 띄워 쿼터를 조회한 뒤 종료한다."""
    if not os.path.exists(AG_EXE):
        raise FileNotFoundError("Antigravity IDE 설치 경로 없음")
    if _ps_json("Get-Process -Name 'Antigravity IDE' -ErrorAction SilentlyContinue | Select-Object Id"):
        raise RuntimeError("Antigravity IDE 실행 중이지만 응답 없음")  # 사용자가 켠 창은 건드리지 않는다
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 7  # SW_SHOWMINNOACTIVE: 최소화 + 포커스 뺏지 않음
    proc = subprocess.Popen([AG_EXE], startupinfo=si)
    try:
        deadline = time.time() + AG_BG_TIMEOUT
        while time.time() < deadline:
            time.sleep(4)
            try:
                return _antigravity_live()
            except Exception:
                pass
        raise TimeoutError("백그라운드 조회 시간 초과")
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=NO_WINDOW)


def _antigravity_live():
    """실행 중인 language_server 에 GetUserStatus 를 요청한다."""
    procs = _ps_json("Get-CimInstance Win32_Process -Filter \"Name like 'language_server%'\" | "
                     "Select-Object ProcessId,CommandLine")
    if not procs:
        raise RuntimeError("Antigravity 미실행")
    netstat = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=15, creationflags=NO_WINDOW).stdout
    for p in procs:
        cmd = p.get("CommandLine") or ""
        m = re.search(r"--csrf_token[= ](\S+)", cmd)
        if not m:
            continue
        csrf, pid = m.group(1), str(p["ProcessId"])
        ports = re.findall(r"127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\s+" + pid + r"\b", netstat)
        for port in ports:
            try:
                data = _http_json(
                    f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus",
                    {"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                     "X-Codeium-Csrf-Token": csrf},
                    data=json.dumps({"metadata": {"ideName": "antigravity", "extensionName": "antigravity",
                                                  "locale": "en"}}).encode(),
                    insecure=True)
            except Exception:
                continue
            us = data.get("userStatus") or {}
            cfgs = (us.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []
            models = []
            for c in cfgs:
                q = c.get("quotaInfo")
                if q is None:
                    continue
                models.append((c.get("label", "?"), float(q.get("remainingFraction") or 0),
                               _iso_to_epoch(q.get("resetTime"))))
            if models:
                plan = (us.get("userTier") or {}).get("name") \
                    or ((us.get("planStatus") or {}).get("planInfo") or {}).get("planName")
                return models, plan
    raise RuntimeError("language_server 응답 없음")


# 최소한의 protobuf 디코더 (state.vscdb 캐시용)
def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, i


def _pb(b):
    i, out = 0, []
    while i < len(b):
        k, i = _varint(b, i)
        f, t = k >> 3, k & 7
        if t == 0:
            v, i = _varint(b, i)
        elif t == 1:
            v = struct.unpack("<d", b[i:i + 8])[0]
            i += 8
        elif t == 5:
            v = struct.unpack("<f", b[i:i + 4])[0]
            i += 4
        elif t == 2:
            n, i = _varint(b, i)
            v = b[i:i + n]
            i += n
        else:
            raise ValueError("unsupported wire type")
        out.append((f, v))
    return out


def _pb_get(fields, num, default=None):
    for f, v in fields:
        if f == num:
            return v
    return default


def _antigravity_cached():
    """Antigravity 가 마지막으로 저장한 userStatus(state.vscdb)를 읽는다."""
    candidates = [os.path.join(APPDATA, d, "User", "globalStorage", "state.vscdb")
                  for d in ("Antigravity IDE", "Antigravity")]
    candidates = [c for c in candidates if os.path.exists(c)]
    if not candidates:
        raise FileNotFoundError("Antigravity 데이터 없음")
    path = max(candidates, key=os.path.getmtime)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM ItemTable WHERE key='antigravityUnifiedStateSync.userStatus'").fetchone()
    finally:
        con.close()
    if not row:
        raise FileNotFoundError("Antigravity 사용자 상태 없음")
    outer = _pb(base64.b64decode(row[0]))
    entry = _pb(_pb_get(outer, 1))
    status = base64.b64decode(_pb_get(_pb(_pb_get(entry, 2)), 1))
    top = _pb(status)
    models = []
    for f, v in _pb(_pb_get(top, 33, b"")):
        if f != 1:
            continue
        m = _pb(v)
        q = _pb_get(m, 15)
        if q is None:
            continue
        q = _pb(q)
        reset = _pb_get(q, 2)
        models.append((_pb_get(m, 1, b"?").decode(), float(_pb_get(q, 1, 0.0)),
                       _pb_get(_pb(reset), 1) if reset else None))
    plan_info = _pb_get(top, 36)
    plan = _pb_get(_pb(plan_info), 2, b"").decode() if plan_info else None
    return models, plan or None, os.path.getmtime(path)


# ---------------------------------------------------------------- entry

SOURCES = [("Antigravity", fetch_antigravity), ("Claude Code", fetch_claude), ("Codex", fetch_codex)]


def collect_one(name, fn, cache):
    try:
        res = fn()
        cache[name] = res
        return res
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        old = cache.get(name)
        if old:
            stale = dict(old, ok=False, note=msg + " · 이전 값 표시")
            return stale
        res = _result(name, [], ok=False, note=msg)
        res["fetched_at"] = None
        return res


def collect_all(only=None):
    cache = load_cache()
    out = {}
    for name, fn in SOURCES:
        if only and name not in only:
            out[name] = cache.get(name)
            continue
        out[name] = collect_one(name, fn, cache)
    save_cache(cache)
    return [out[n] for n, _ in SOURCES]


if __name__ == "__main__":
    print(json.dumps(collect_all(), ensure_ascii=False, indent=1))
