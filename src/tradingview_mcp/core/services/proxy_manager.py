"""
Proxy Manager Service for tradingview-mcp.

Reads Webshare proxy credentials from ENVIRONMENT VARIABLES only.
Never hardcode credentials in this file.

Setup:
    export PROXY_HOST=p.webshare.io
    export PROXY_PORT=80
    export PROXY_USERNAME_PREFIX=hvfvdamo   # your username prefix
    export PROXY_PASSWORD=your_password_here

Or create a .env file (see .env.example) — never commit .env to git.

Usage:
    from tradingview_mcp.core.services.proxy_manager import get_proxy, build_opener_with_proxy

    proxies = get_proxy()                    # for requests library
    opener  = build_opener_with_proxy()      # for urllib
"""
from __future__ import annotations

import os
import random
import urllib.request
from typing import Optional

# Try loading .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(__file__), "../../../../.env")
    load_dotenv(dotenv_path=_env_path, override=False)
except ImportError:
    pass


# ─── Read config from env ─────────────────────────────────────────────────────

def _cfg() -> dict:
    return {
        "host":    os.environ.get("PROXY_HOST", "p.webshare.io"),
        "port":    os.environ.get("PROXY_PORT", "80"),
        "prefix":  os.environ.get("PROXY_USERNAME_PREFIX", ""),
        "password": os.environ.get("PROXY_PASSWORD", ""),
        "enabled": os.environ.get("PROXY_ENABLED", "true").lower() == "true",
        "min":     int(os.environ.get("PROXY_SESSION_MIN", "1")),
        "max":     int(os.environ.get("PROXY_SESSION_MAX", "250")),
        # When true, the host's own (direct) connection joins the rotation as
        # one extra egress alongside the proxy sticky sessions.
        "include_direct": os.environ.get("PROXY_INCLUDE_DIRECT", "false").lower() == "true",
    }


def is_proxy_configured() -> bool:
    """Returns True only if all required env vars are set."""
    c = _cfg()
    return c["enabled"] and bool(c["prefix"]) and bool(c["password"])


def get_proxy_url() -> Optional[str]:
    """Build a rotating proxy URL with a random sticky session. Returns None if not configured."""
    if not is_proxy_configured():
        return None
    c = _cfg()
    session_id = random.randint(c["min"], c["max"])
    return f"http://{c['prefix']}-{session_id}:{c['password']}@{c['host']}:{c['port']}"


def get_proxy() -> Optional[dict]:
    """Return proxy dict for the `requests` library. Returns None if not configured."""
    url = get_proxy_url()
    if not url:
        return None
    return {"http": url, "https": url}


def _extra_proxy_urls() -> list:
    """Parse ``PROXY_EXTRA_URLS`` — a comma-separated list of full proxy URLs.

    These are fixed, self-hosted forward proxies (e.g. one per Oracle/k3s
    node) that join the rotation alongside the Webshare gateway. Each URL is
    ``http://[user:pass@]host:port`` and counts as one egress slot. Whitespace
    and empty entries are ignored.
    """
    raw = os.environ.get("PROXY_EXTRA_URLS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def _select_opener_egress() -> Optional[str]:
    """Pick the egress for a single outbound request.

    Returns a proxy URL, or ``None`` meaning "go direct on the host's own IP".

    The rotation pool is a flat, per-IP-fair list of slots drawn from three
    sources, all optional:

    * **Webshare sticky sessions** — one slot per session id in ``[min, max]``
      (each maps to ~one egress IP on Webshare's gateway). Requires the
      ``PROXY_USERNAME_PREFIX`` / ``PROXY_PASSWORD`` creds.
    * **Extra fixed proxies** (``PROXY_EXTRA_URLS``) — one slot per URL, e.g.
      self-hosted forward proxies on your own VPS/k3s nodes.
    * **Direct/home** — one slot when ``PROXY_INCLUDE_DIRECT`` is set; the
      host's own (often residential) IP, which Yahoo tends to trust more than
      datacenter proxy ranges.

    A request is assigned uniformly across all slots, so e.g. 10 Webshare
    sessions + 4 node proxies + home = 15 egresses at ~1/15 each. Returns
    ``None`` (graceful direct) when the pool is empty or the proxy is disabled.
    """
    c = _cfg()
    if not c["enabled"]:
        return None

    has_webshare = bool(c["prefix"]) and bool(c["password"])
    n_sessions = max(0, c["max"] - c["min"] + 1) if has_webshare else 0
    extras = _extra_proxy_urls()
    n_extra = len(extras)
    n_direct = 1 if c["include_direct"] else 0

    total = n_sessions + n_extra + n_direct
    if total == 0:
        return None

    pick = random.randint(0, total - 1)
    if pick < n_sessions:
        session_id = c["min"] + pick
        return f"http://{c['prefix']}-{session_id}:{c['password']}@{c['host']}:{c['port']}"
    pick -= n_sessions
    if pick < n_extra:
        return extras[pick]
    return None  # direct / home slot


def build_opener_with_proxy(
    user_agent: str = "tradingview-mcp/0.5.0",
    *,
    extra_handlers: tuple = (),
) -> urllib.request.OpenerDirector:
    """
    Build a urllib OpenerDirector with proxy if configured, plain opener otherwise.
    Services degrade gracefully when no proxy is set — no crashes.

    Pass ``extra_handlers`` (e.g. ``HTTPCookieProcessor``) when the caller
    needs cookie-jar persistence or any other handler stacked under the same
    opener. Without this, callers that built their own opener bypassed the
    proxy entirely — that's what put Yahoo's quoteSummary endpoint into a
    permanent 429 on the container's outbound IP.

    The egress is chosen per call by :func:`_select_opener_egress`: a Webshare
    sticky session, a fixed extra proxy (``PROXY_EXTRA_URLS``), or — when
    ``PROXY_INCLUDE_DIRECT`` is set — the host's own direct connection. A
    ``None`` egress (empty pool or the direct slot) yields a plain opener that
    still carries any ``extra_handlers``.
    """
    proxy_url = _select_opener_egress()
    if proxy_url is None:
        opener = urllib.request.build_opener(*extra_handlers)
        opener.addheaders = [("User-Agent", user_agent)]
        return opener

    # Derive auth from the chosen URL itself (http://[user:pass@]host:port) so
    # self-hosted proxies authenticate with their own creds, not Webshare's.
    # A urllib ProxyHandler already replays embedded user:pass as a
    # Proxy-Authorization header; the explicit ProxyBasicAuthHandler also
    # answers 407 challenges. Proxies without creds skip the auth handler.
    netloc = proxy_url.split("//", 1)[1]
    handlers = [urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})]
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
        username, _, password = userinfo.partition(":")
        pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pwd_mgr.add_password(None, f"http://{hostport}", username, password)
        handlers.append(urllib.request.ProxyBasicAuthHandler(pwd_mgr))

    opener = urllib.request.build_opener(*handlers, *extra_handlers)
    opener.addheaders = [("User-Agent", user_agent)]
    return opener


# Cap on how many Webshare sessions check_proxy() probes, so the diagnostic
# stays fast even when PROXY_SESSION_MAX is large.
_CHECK_PROXY_SESSION_CAP = 16


def _probe_egress(proxy_url: Optional[str], timeout: int = 8) -> dict:
    """Hit ipinfo.io through one egress (proxy URL, or None = direct).

    Mirrors build_opener_with_proxy's auth handling so the probe matches how
    real traffic authenticates. Returns ok/ip/country/city/error.
    """
    import json
    out = {"ok": False, "ip": None, "country": None, "city": None, "org": None, "error": None}
    try:
        if proxy_url is None:
            opener = urllib.request.build_opener()
        else:
            netloc = proxy_url.split("//", 1)[1]
            handlers = [urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})]
            if "@" in netloc:
                userinfo, hostport = netloc.rsplit("@", 1)
                username, _, password = userinfo.partition(":")
                pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
                pwd_mgr.add_password(None, f"http://{hostport}", username, password)
                handlers.append(urllib.request.ProxyBasicAuthHandler(pwd_mgr))
            opener = urllib.request.build_opener(*handlers)
        opener.addheaders = [("User-Agent", "tradingview-mcp/0.5.0")]
        with opener.open("https://ipinfo.io/json", timeout=timeout) as resp:
            data = json.loads(resp.read())
        out.update(ok=True, ip=data.get("ip"), country=data.get("country"),
                   city=data.get("city"), org=data.get("org"))
    except Exception as e:
        out["error"] = str(e)
    return out


_NO_EGRESS_NOTE = ("No egresses configured — set PROXY_USERNAME_PREFIX/"
                   "PROXY_PASSWORD, PROXY_EXTRA_URLS, or PROXY_INCLUDE_DIRECT.")


def build_egress_jobs() -> tuple:
    """Return ``(jobs, note)`` describing the whole rotation pool.

    ``jobs`` is an ordered list of ``(label, url_or_None)`` — Webshare sticky
    sessions first (capped at ``_CHECK_PROXY_SESSION_CAP``), then every
    ``PROXY_EXTRA_URLS`` proxy, then the direct/home slot when enabled. A
    ``url`` of ``None`` means the direct/home egress. ``note`` flags Webshare
    sessions skipped by the cap (or ``None``). Returns ``([], None)`` when the
    proxy is disabled or nothing is configured — the single source of truth for
    both :func:`check_proxy` and :func:`run_check_proxy`.
    """
    c = _cfg()
    if not c["enabled"]:
        return [], None

    jobs: list = []
    note = None
    if bool(c["prefix"]) and bool(c["password"]):
        hi = min(c["max"], c["min"] + _CHECK_PROXY_SESSION_CAP - 1)
        if hi < c["max"]:
            note = f"webshare sessions {hi + 1}..{c['max']} not probed (cap)"
        for sid in range(c["min"], hi + 1):
            url = f"http://{c['prefix']}-{sid}:{c['password']}@{c['host']}:{c['port']}"
            jobs.append((f"webshare#{sid}", url))

    for url in _extra_proxy_urls():
        jobs.append(("extra:" + url.split("@")[-1], url))

    if c["include_direct"]:
        jobs.append(("direct/home", None))

    return jobs, note


def check_proxy(*, max_workers: int = 8) -> dict:
    """Probe EVERY egress in the rotation pool (in parallel) and report each
    exit IP.

    Tests the Webshare sticky sessions (capped at ``_CHECK_PROXY_SESSION_CAP``),
    every ``PROXY_EXTRA_URLS`` proxy, and the direct/home slot when enabled —
    so a too-wide ``PROXY_SESSION_MAX`` (sessions beyond your plan size → 407)
    or a dead self-hosted proxy shows up per-egress instead of hiding behind a
    single random sample (the old behaviour only tested one Webshare session).

    Egress order is preserved in ``egresses`` even though probing is concurrent.
    """
    from concurrent.futures import ThreadPoolExecutor

    c = _cfg()
    if not c["enabled"]:
        return {"enabled": False, "ok_count": 0, "total": 0, "egresses": [],
                "note": "PROXY_ENABLED is not 'true'."}

    jobs, note = build_egress_jobs()
    egresses: list = [None] * len(jobs)

    def _work(i: int):
        label, url = jobs[i]
        r = _probe_egress(url)
        r["label"] = label
        return i, r

    if jobs:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as ex:
            for i, r in ex.map(_work, range(len(jobs))):
                egresses[i] = r

    result = {
        "enabled": True,
        "ok_count": sum(1 for e in egresses if e["ok"]),
        "total": len(egresses),
        "egresses": egresses,
    }
    if not egresses:
        result["note"] = _NO_EGRESS_NOTE
    elif note:
        result["note"] = note
    return result


def format_proxy_report(result: dict) -> str:
    """Render :func:`check_proxy` output as an aligned, human-readable report.

    Kept here (not in server.py) so the CLI stays thin and the formatting is
    unit-testable without capturing stdout.
    """
    if not result.get("enabled", False):
        return result.get("note", "Proxy disabled (PROXY_ENABLED is not 'true').")

    egresses = result.get("egresses", [])
    lines = [f"Proxy egress check — {result.get('ok_count', 0)}/{result.get('total', 0)} OK"]
    if not egresses:
        lines.append("  " + result.get("note", "No egresses configured."))
        return "\n".join(lines)

    label_w = max((len(e.get("label") or "") for e in egresses), default=0)
    lines.append("")
    for e in egresses:
        glyph = "✓" if e.get("ok") else "✗"
        label = (e.get("label") or "").ljust(label_w)
        if e.get("ok"):
            loc = " ".join(x for x in (e.get("country"), e.get("city")) if x)
            row = f"  {glyph} {label}  {(e.get('ip') or '?'):<15}  {loc}"
            if e.get("org"):
                row += f"  {e['org']}"
            lines.append(row.rstrip())
        else:
            lines.append(f"  {glyph} {label}  error: {e.get('error')}")
    if result.get("note"):
        lines.append("")
        lines.append(f"note: {result['note']}")
    return "\n".join(lines)


def run_check_proxy(out, *, is_tty: bool = False, max_workers: int = 8) -> dict:
    """Probe every egress with live progress, returning the same dict as
    :func:`check_proxy`.

    ``is_tty`` picks the renderer:

    * **terminal** — a cursor-addressed board that shows every egress as
      ``queued`` → ``checking…`` (animated spinner) → ``✓``/``✗``, with a
      ``done k/N`` header that ticks up as probes resolve.
    * **piped/redirected** — plain ``[k/N] [OK|FAIL] label …`` lines streamed
      as each probe finishes, then a one-line summary. No ANSI, so logs and
      ``docker compose exec -T`` stay clean.

    ``out`` is the stream to write to (a param so tests can pass a StringIO).
    Probing is concurrent (``max_workers``); with more egresses than workers the
    board genuinely shows some ``queued`` while others are ``checking``.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    c = _cfg()
    if not c["enabled"]:
        result = {"enabled": False, "ok_count": 0, "total": 0, "egresses": [],
                  "note": "PROXY_ENABLED is not 'true'."}
        out.write(format_proxy_report(result) + "\n")
        out.flush()
        return result

    jobs, note = build_egress_jobs()
    if not jobs:
        result = {"enabled": True, "ok_count": 0, "total": 0, "egresses": [],
                  "note": _NO_EGRESS_NOTE}
        out.write(format_proxy_report(result) + "\n")
        out.flush()
        return result

    n = len(jobs)
    label_w = max(len(label) for label, _ in jobs)
    statuses: list = ["queued"] * n          # "queued" | "checking" | result-dict
    spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    spin_idx = [0]
    lock = threading.Lock()

    GREEN, RED, DIM, RST = (
        ("\x1b[32m", "\x1b[31m", "\x1b[2m", "\x1b[0m") if is_tty else ("", "", "", "")
    )

    def _done() -> int:
        return sum(1 for s in statuses if isinstance(s, dict))

    def _ok() -> int:
        return sum(1 for s in statuses if isinstance(s, dict) and s["ok"])

    def _line(i: int) -> str:
        label = jobs[i][0].ljust(label_w)
        st = statuses[i]
        if st == "queued":
            return f"  {DIM}·  {label}  queued{RST}"
        if st == "checking":
            return f"  {spin[spin_idx[0] % len(spin)]}  {label}  checking…"
        if st["ok"]:
            loc = " ".join(x for x in (st.get("country"), st.get("city")) if x)
            row = f"  {GREEN}✓{RST}  {label}  {(st.get('ip') or '?'):<15}  {loc}"
            if st.get("org"):
                row += f"  {DIM}{st['org']}{RST}"
            return row
        return f"  {RED}✗  {label}  error: {st.get('error')}{RST}"

    def _render(first: bool = False) -> None:
        body = [f"Proxy egress check — {_done()}/{n} done, {_ok()} OK"]
        body += [_line(i) for i in range(n)]
        if first:
            out.write("\n".join(body) + "\n")
        else:
            out.write(f"\x1b[{n + 1}A")  # cursor up to the header line
            out.write("\n".join("\x1b[2K" + ln for ln in body) + "\n")
        out.flush()

    def _work(i: int):
        with lock:
            statuses[i] = "checking"
            if is_tty:
                _render()
        r = _probe_egress(jobs[i][1])
        r["label"] = jobs[i][0]
        with lock:
            statuses[i] = r
            if is_tty:
                _render()
            else:
                tag = "OK  " if r["ok"] else "FAIL"
                if r["ok"]:
                    loc = " ".join(x for x in (r.get("country"), r.get("city")) if x)
                    detail = f"{r.get('ip')}  {loc}"
                    if r.get("org"):
                        detail += f"  {r['org']}"
                else:
                    detail = f"error: {r.get('error')}"
                out.write(f"  [{_done()}/{n}] [{tag}] {jobs[i][0]}  {detail}\n")
                out.flush()
        return i

    ticker = None
    stop = None
    if is_tty:
        out.write(f"{DIM}Probing {n} egress(es) via ipinfo.io…{RST}\n")
        _render(first=True)
        stop = threading.Event()

        def _tick() -> None:
            while not stop.wait(0.12):
                with lock:
                    spin_idx[0] += 1
                    _render()

        ticker = threading.Thread(target=_tick, daemon=True)
        ticker.start()
    else:
        out.write(f"Probing {n} egress(es) via ipinfo.io…\n")
        out.flush()

    with ThreadPoolExecutor(max_workers=min(max_workers, n)) as ex:
        list(ex.map(_work, range(n)))

    if is_tty:
        stop.set()
        ticker.join(timeout=0.5)
        with lock:
            _render()  # final frame: everything resolved

    egresses = [statuses[i] for i in range(n)]
    result = {"enabled": True,
              "ok_count": sum(1 for e in egresses if e["ok"]),
              "total": n, "egresses": egresses}
    if note:
        result["note"] = note
    if not is_tty:
        out.write(f"\nProxy egress check — {result['ok_count']}/{n} OK\n")
    if note:
        out.write(f"{DIM}note: {note}{RST}\n" if is_tty else f"note: {note}\n")
    out.flush()
    return result
