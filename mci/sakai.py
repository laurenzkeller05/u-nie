"""Sakai-25-Client für sakai.mci4me.at (direkter Login, kein SSO).

Teile der Logik (Login-Reihenfolge /direct/session -> /api/login, Sessionprüfung über
/direct/session/current.json, Abgabestatus aus `submissions[0]`, Noten über
/api/users/me/grades) stammen aus jtayped/sakai-mcp (MIT, (c) 2026 Joel Taylor),
https://github.com/jtayped/sakai-mcp

Wichtig: Sakai beantwortet anonyme /direct-Anfragen mit 200 und leeren Listen statt
mit 401. Deshalb wird die Session immer explizit über current.json geprüft.
Das Passwort wird nie ausgegeben oder geloggt.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests

from .config import (
    DATA_DIR,
    SAKAI_BASE,
    AuthError,
    ConfigError,
    require_credentials,
    sakai_cookie,
)
from .util import parse_time

UA = "mci-cli/0.1 (+python-requests; Sakai student client)"
TIMEOUT = 30
SESSION_FILE = DATA_DIR / "sakai_session.json"
HOST = urlsplit(SAKAI_BASE).hostname or "sakai.mci4me.at"

STATUSES = ("offen", "entwurf", "abgegeben", "bewertet", "unbekannt")

# Modulweiter Cache: site_id -> Titel (wird von sites() und bei Bedarf einzeln gefüllt).
_SITE_TITLES: dict[str, str] = {}
# site_id -> Tool-Placement-ID des Aufgaben-Tools (für Deep Links).
_ASSIGN_TOOLS: dict[str, str | None] = {}


class SakaiError(RuntimeError):
    """HTTP-/Formatfehler bei einer Sakai-Anfrage (nicht Auth)."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- helpers


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _time(d: dict, *keys: str) -> datetime | None:
    """Erstes parsebares Zeitfeld (dict/epoch/ISO) aus den angegebenen Keys."""
    for k in keys:
        t = parse_time(d.get(k))
        if t:
            return t
    return None


def _collection(data: Any, *keys: str) -> list:
    """Listen aus Sakai-Antworten holen: {"x_collection": [...]}, {"grades": [...]} oder [...]."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return v
    for k, v in data.items():
        if k.endswith("_collection") and isinstance(v, list):
            return v
    return []


def _attachments(items: Any) -> list[dict]:
    out: list[dict] = []
    for a in items or []:
        if isinstance(a, str):
            out.append({"name": a.rstrip("/").rsplit("/", 1)[-1], "url": _abs(a)})
        elif isinstance(a, dict):
            url = _first(a, "url", "href", "ref", "reference", "id") or ""
            name = _first(a, "name", "displayName", "title") or _s(url).rstrip("/").rsplit("/", 1)[-1]
            out.append({"name": _s(name), "url": _abs(_s(url))})
    return out


def _abs(url: str) -> str:
    if not url:
        return url
    if url.startswith("/content/") or url.startswith("/attachment/"):
        url = "/access" + url
    return urljoin(SAKAI_BASE + "/", url)


def _submission_status(a: dict) -> tuple[str, str | None]:
    """(status, grade) aus der Abgabe des Users.

    Nach sakai-mcp: Sakai setzt `submitted` auch bei unberührten Aufgaben auf true;
    das echte Signal ist ein Abgabezeitpunkt. Ein `draft` mit altem Datum ist eine
    angefangene Neuabgabe.
    """
    if "submissions" not in a:
        return "unbekannt", None
    subs = a.get("submissions") or []
    if not subs:
        return "offen", None
    sb = subs[0] if isinstance(subs[0], dict) else {}
    grade = _first(sb, "grade", "gradeDisplay")
    grade = _s(grade) if grade is not None else None
    returned = bool(sb.get("returned"))
    graded = bool(sb.get("graded")) or returned
    submitted_at = _first(sb, "dateSubmittedEpochSeconds", "dateSubmitted", "timeSubmitted")
    if graded and (grade or returned) and not sb.get("draft"):
        return "bewertet", grade
    if sb.get("draft"):
        return "entwurf", grade
    if submitted_at:
        return "abgegeben", grade
    if sb.get("submittedText") or sb.get("submittedAttachments"):
        return "entwurf", grade
    return "offen", grade


# --------------------------------------------------------------------------- client


class SakaiClient:
    def __init__(self) -> None:
        self.base = SAKAI_BASE.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json, */*"})
        self.timeout = TIMEOUT
        self._logged_in = False
        self._me: dict | None = None

    # ------------------------------------------------------------------ session

    def _set_cookie(self, value: str) -> None:
        value = value.strip()
        if value.upper().startswith("JSESSIONID="):
            value = value.split("=", 1)[1].split(";", 1)[0].strip()
        self.s.cookies.set("JSESSIONID", value, domain=HOST, path="/")

    def _jsessionid(self) -> str | None:
        for c in self.s.cookies:
            if c.name == "JSESSIONID" and (c.domain or "").lstrip(".") in (HOST, ""):
                return c.value
        return self.s.cookies.get("JSESSIONID")

    def _current(self) -> dict | None:
        """Session-Info, falls eingeloggt (userId gesetzt), sonst None."""
        try:
            r = self.s.get(self.base + "/direct/session/current.json", timeout=self.timeout)
            if not r.ok:
                return None
            d = r.json()
        except (requests.RequestException, ValueError):
            return None
        return d if isinstance(d, dict) and d.get("userId") else None

    def _save_session(self) -> None:
        val = self._jsessionid()
        if not val:
            return
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"JSESSIONID": val, "saved": datetime.now(timezone.utc).isoformat()}, f)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except OSError:
            pass

    def _load_cached(self) -> str | None:
        try:
            return json.loads(SESSION_FILE.read_text()).get("JSESSIONID") or None
        except (OSError, ValueError, AttributeError):
            return None

    def _password_login(self) -> bool:
        try:
            user, pw = require_credentials()
        except ConfigError as e:
            raise AuthError(
                f"{e} Alternativ SAKAI_COOKIE (Wert von JSESSIONID aus dem Browser) in .env setzen."
            ) from None
        names = [user]
        if "@" in user:
            names.append(user.split("@", 1)[0])
        attempts: list[tuple[str, dict]] = [
            ("/direct/session", {"_username": n, "_password": pw}) for n in names
        ]
        attempts += [("/api/login", {"username": n, "password": pw}) for n in names]
        for path, form in attempts:
            self.s.cookies.clear()
            try:
                r = self.s.post(self.base + path, data=form, timeout=self.timeout)
            except requests.RequestException:
                continue
            if r.ok and not self._jsessionid():
                # Manche Sakais liefern die Session-ID nur im Body.
                body = r.text.strip()
                if body and len(body) < 128 and " " not in body and "<" not in body:
                    self._set_cookie(body)
            if r.ok and self._current():
                return True
        return False

    def login(self) -> None:
        # 1. explizites Cookie aus .env
        cookie = sakai_cookie()
        if cookie:
            self.s.cookies.clear()
            self._set_cookie(cookie)
            if self._current():
                self._logged_in = True
                return
            raise AuthError(
                "SAKAI_COOKIE ist ungültig oder abgelaufen. Im Browser bei Sakai einloggen, "
                "den JSESSIONID-Wert neu kopieren oder SAKAI_COOKIE leeren, um mit MCI_USER/MCI_PASS "
                "einzuloggen."
            )
        # 2. gecachte Session
        cached = self._load_cached()
        if cached:
            self.s.cookies.clear()
            self._set_cookie(cached)
            if self._current():
                self._logged_in = True
                return
        # 3. Login mit Benutzername/Passwort
        if self._password_login():
            self._save_session()
            self._logged_in = True
            return
        self.s.cookies.clear()
        raise AuthError(
            "Sakai-Login fehlgeschlagen. MCI_USER/MCI_PASS in .env prüfen (Benutzername ohne "
            "oder mit @mci4me.at probiert) oder SAKAI_COOKIE aus dem Browser setzen."
        )

    def ensure(self) -> None:
        if not self._logged_in:
            self.login()

    # ------------------------------------------------------------------ http

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base + ("" if path.startswith("/") else "/") + path

    def _expired(self, r: requests.Response) -> bool:
        if r.status_code == 401 or "/sakai-login-tool/" in r.url or "/portal/xlogin" in r.url:
            return True
        # 403 kann auch "keine Berechtigung" bei gültiger Session heißen.
        return r.status_code == 403 and not self._current()

    def get(self, path: str, **params: Any) -> Any:
        self.ensure()
        url = self._url(path)
        try:
            r = self.s.get(url, params=params or None, timeout=self.timeout)
            if self._expired(r):
                self._logged_in = False
                self.login()
                r = self.s.get(url, params=params or None, timeout=self.timeout)
        except requests.RequestException as e:
            raise SakaiError(f"GET {path}: {e.__class__.__name__}: {e}") from None
        if r.status_code == 401:
            raise AuthError(f"GET {path}: Sitzung ungültig (HTTP 401) auch nach erneutem Login.")
        if not r.ok:
            raise SakaiError(f"GET {path}: HTTP {r.status_code}", r.status_code)
        try:
            return r.json()
        except ValueError:
            raise SakaiError(f"GET {path}: keine JSON-Antwort", r.status_code) from None

    def _try(self, path: str, default: Any = None, **params: Any) -> Any:
        """get(), aber 4xx/5xx/Format-Fehler -> default. AuthError läuft durch."""
        try:
            return self.get(path, **params)
        except SakaiError:
            return default

    # ------------------------------------------------------------------ user/sites

    def whoami(self) -> dict:
        u = self._try("/direct/user/current.json") or {}
        if not u.get("id"):
            cur = self._current() or {}
            u = {"id": cur.get("userId"), "eid": cur.get("userEid"), "displayName": cur.get("userEid")}
        return {
            "userId": u.get("id"),
            "userEid": u.get("eid"),
            "displayName": u.get("displayName") or u.get("displayId") or u.get("eid"),
        }

    def sites(self) -> list[dict]:
        data = self.get("/direct/site.json", _limit=500)
        out = []
        for x in _collection(data, "site_collection"):
            sid = _s(x.get("id"))
            if not sid or sid.startswith("~"):
                continue
            title = _s(x.get("title")) or sid
            _SITE_TITLES[sid] = title
            out.append({"id": sid, "title": title, "url": f"{self.base}/portal/site/{quote(sid)}"})
        return out

    def site_title(self, site_id: str | None) -> str:
        return site_title(site_id, self)

    def _assignment_tool(self, site_id: str) -> str | None:
        if site_id in _ASSIGN_TOOLS:
            return _ASSIGN_TOOLS[site_id]
        tool_id = None
        pages = self._try(f"/direct/site/{site_id}/pages.json", default=[])
        for p in pages if isinstance(pages, list) else []:
            for t in (p or {}).get("tools") or []:
                if t.get("toolId") == "sakai.assignment.grades":
                    tool_id = t.get("id") or t.get("placementId")
                    break
            if tool_id:
                break
        _ASSIGN_TOOLS[site_id] = tool_id
        return tool_id

    def _assignment_url(self, site_id: str, aid: str, deep: bool = True) -> str:
        if not site_id:
            return f"{self.base}/portal"
        tool = self._assignment_tool(site_id) if deep else None
        if not tool:
            return f"{self.base}/portal/site/{quote(site_id)}"
        ref = f"/assignment/a/{site_id}/{aid}"
        return (f"{self.base}/portal/site/{quote(site_id)}/tool/{quote(tool)}"
                f"?assignmentReference={quote(ref, safe='')}&sakai_action=doView_submission")

    # ------------------------------------------------------------------ assignments

    def _norm_assignment(self, a: dict, deep_link: bool = True) -> dict:
        site_id = _s(_first(a, "context", "siteId"))
        aid = _s(a.get("id"))
        status, grade = _submission_status(a)
        return {
            "id": aid,
            "site_id": site_id,
            "site_title": site_title(site_id, self),
            "title": _s(a.get("title")),
            "open": _time(a, "openTime", "openTimeString"),
            "due": _time(a, "dueTime", "dueTimeString"),
            "close": _time(a, "closeTime", "closeTimeString", "dropDeadTime", "dropDeadTimeString"),
            "status": status,
            "grade": grade,
            "instructions_html": _s(a.get("instructions")),
            "attachments": _attachments(a.get("attachments")),
            "url": self._assignment_url(site_id, aid, deep_link),
            "raw": a,
        }

    def assignments(self, site_id: str | None = None) -> list[dict]:
        if site_id:
            items = _collection(self.get(f"/direct/assignment/site/{site_id}.json"), "assignment_collection")
        else:
            items = _collection(self.get("/direct/assignment/my.json"), "assignment_collection")
            if not items:  # Fallback: pro Site
                items = []
                for s in self.sites():
                    data = self._try(f"/direct/assignment/site/{s['id']}.json")
                    items += _collection(data, "assignment_collection")
        if items and not _SITE_TITLES:
            try:
                self.sites()
            except SakaiError:
                pass
        out = [self._norm_assignment(a) for a in items if isinstance(a, dict)]
        far = datetime.max.replace(tzinfo=timezone.utc)
        out.sort(key=lambda x: (x["due"] or far, x["title"]))
        return out

    def assignment(self, assignment_id: str) -> dict:
        a = self.get(f"/direct/assignment/item/{assignment_id}.json")
        if not isinstance(a, dict) or not a.get("id"):
            raise SakaiError(f"Aufgabe {assignment_id} nicht gefunden")
        if "submissions" not in a:
            # Submission-Infos aus my.json ergänzen
            mine = _collection(self._try("/direct/assignment/my.json"), "assignment_collection")
            for m in mine:
                if isinstance(m, dict) and m.get("id") == a.get("id") and "submissions" in m:
                    a = {**a, "submissions": m["submissions"]}
                    break
        return self._norm_assignment(a)

    # ------------------------------------------------------------------ announcements

    def announcements(self, days: int = 30, n: int = 50) -> list[dict]:
        data = self.get("/direct/announcement/user.json", n=n, d=days)
        out = []
        for x in _collection(data, "announcement_collection"):
            if not isinstance(x, dict):
                continue
            sid = _s(x.get("siteId"))
            out.append({
                "id": _s(_first(x, "id", "announcementId")),
                "site_title": _s(x.get("siteTitle")) or site_title(sid, self),
                "title": _s(x.get("title")),
                "body_html": _s(x.get("body")),
                "created": _time(x, "createdOn", "date"),
                "author": _s(_first(x, "createdByDisplayName", "author", "createdBy")),
                "attachments": _attachments(x.get("attachments")),
            })
        old = datetime.min.replace(tzinfo=timezone.utc)
        out.sort(key=lambda a: a["created"] or old, reverse=True)
        return out[:n]

    # ------------------------------------------------------------------ calendar

    def calendar(self) -> list[dict]:
        today = date.today()
        data = self._try("/direct/calendar/my.json",
                         firstDate=(today - timedelta(days=1)).isoformat(),
                         lastDate=(today + timedelta(days=180)).isoformat())
        items = _collection(data, "calendar_collection")
        api = False
        if not items:
            data = self._try("/api/users/current/calendar")
            items = _collection(data, "events")
            api = bool(items)
        out = []
        for e in items:
            if not isinstance(e, dict):
                continue
            sid = _s(e.get("siteId"))
            dur = e.get("duration")
            try:
                # /direct liefert ms; /api ebenfalls ms (best effort)
                dur_min = int(int(dur) / 60000) if dur not in (None, "") else 0
            except (TypeError, ValueError):
                dur_min = 0
            start = _time(e, "firstTime", "start", "startTime")
            if start is None and api:
                start = _time(e, "date")
            out.append({
                "title": _s(e.get("title")),
                "start": start,
                "duration_min": dur_min,
                "type": _s(_first(e, "type", "eventType", "eventIcon")),
                "site_title": _s(_first(e, "siteName", "siteTitle")) or site_title(sid, self),
                "description": _s(_first(e, "description", "descriptionFormatted")),
            })
        old = datetime.min.replace(tzinfo=timezone.utc)
        out.sort(key=lambda x: x["start"] or old)
        return out

    # ------------------------------------------------------------------ grades

    def grades(self) -> list[dict]:
        try:
            data = self.get("/api/users/me/grades")
        except SakaiError:
            return []
        out = []
        for g in _collection(data, "grades"):
            if not isinstance(g, dict):
                continue
            try:
                sid = _s(g.get("siteId"))
                score = _first(g, "score", "grade", "displayGrade")
                if g.get("notGradedYet") and score in (None, "", "-"):
                    score = None
                mx = _first(g, "points", "pointsPossible", "maxPoints", "total")
                out.append({
                    "site_title": _s(g.get("siteTitle")) or site_title(sid, self),
                    "item": _s(_first(g, "name", "itemName", "title")),
                    "grade": None if score is None else _s(score),
                    "max": None if mx is None else _s(mx),
                    "raw": g,
                })
            except Exception:  # noqa: BLE001 - Form der Antwort unbekannt
                continue
        return out

    # ------------------------------------------------------------------ quizzes

    def quizzes(self, site_id: str | None = None) -> list[dict]:
        if site_id:
            site_ids = [site_id]
        else:
            try:
                site_ids = [s["id"] for s in self.sites()]
            except SakaiError:
                return []
        out = []
        for sid in site_ids:
            data = self._try(f"/direct/sam_pub/context/{sid}.json")
            for q in _collection(data, "sam_pub_collection"):
                if not isinstance(q, dict):
                    continue
                qsid = _s(_first(q, "ownerSiteId", "siteId", "context")) or sid
                out.append({
                    "title": _s(_first(q, "title", "name")),
                    "site_title": site_title(qsid, self),
                    "open": _time(q, "startDate", "openDate", "startTime"),
                    "due": _time(q, "dueDate", "dueTime", "retractDate"),
                    "raw": q,
                })
        far = datetime.max.replace(tzinfo=timezone.utc)
        out.sort(key=lambda x: x["due"] or far)
        return out

    # ------------------------------------------------------------------ download

    def download(self, url: str, dest: Path) -> Path:
        self.ensure()
        dest = Path(dest)
        full = _abs(url) if url.startswith("/") else url
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.s.get(full, stream=True, timeout=self.timeout, allow_redirects=True) as r:
                if self._expired(r):
                    r.close()
                    self._logged_in = False
                    self.login()
                    return self.download(url, dest)
                if not r.ok:
                    raise SakaiError(f"Download {full}: HTTP {r.status_code}", r.status_code)
                length = r.headers.get("Content-Length")
                if dest.exists() and length and length.isdigit() and dest.stat().st_size == int(length):
                    return dest
                part = dest.with_name(dest.name + ".part")
                with open(part, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
                os.replace(part, dest)
        except requests.RequestException as e:
            raise SakaiError(f"Download {full}: {e.__class__.__name__}: {e}") from None
        return dest


# --------------------------------------------------------------------------- site titles


def site_title(site_id: str | None, client: SakaiClient | None = None) -> str:
    """Titel einer Site aus dem Modul-Cache; lädt bei Bedarf /direct/site/{id}.json."""
    if not site_id:
        return ""
    if site_id in _SITE_TITLES:
        return _SITE_TITLES[site_id]
    title = site_id
    if client is not None:
        d = client._try(f"/direct/site/{site_id}.json")
        if isinstance(d, dict) and d.get("title"):
            title = _s(d["title"])
    _SITE_TITLES[site_id] = title
    return title


# --------------------------------------------------------------------------- self-check


def _selfcheck() -> int:
    """Nur öffentliche Endpunkte, kein Login."""
    c = SakaiClient()
    ok = True
    t0 = time.monotonic()
    r = c.s.get(c.base + "/direct/server-config/version.sakai.json", timeout=TIMEOUT)
    ver = r.json().get("value") if r.ok else None
    print(f"version.sakai: {ver} (HTTP {r.status_code})")
    ok &= bool(ver)
    cur = c.s.get(c.base + "/direct/session/current.json", timeout=TIMEOUT).json()
    print(f"session/current anonym: userId={cur.get('userId')!r} (erwartet None)")
    ok &= cur.get("userId") is None and c._current() is None
    for ent in ("assignment", "announcement", "calendar", "sam_pub", "content"):
        rr = c.s.get(f"{c.base}/direct/{ent}/describe", timeout=TIMEOUT)
        print(f"describe {ent}: HTTP {rr.status_code}")
        ok &= rr.ok
    # Parser mit Beispieldaten
    fake = {"id": "a1", "context": "s1", "title": "T", "dueTime": {"epochSecond": 1790719247},
            "openTimeString": "2026-10-01T08:00:00Z", "attachments": [{"name": "x.pdf", "url": "/access/content/x.pdf"}],
            "submissions": [{"dateSubmittedEpochSeconds": 1790719247, "graded": True, "returned": True, "grade": "8,5"}]}
    _SITE_TITLES["s1"] = "Kurs"
    n = c._norm_assignment(fake, deep_link=False)
    print(f"normalize: status={n['status']} grade={n['grade']} due={n['due']} open={n['open']}")
    ok &= n["status"] == "bewertet" and n["grade"] == "8,5" and n["due"] is not None
    for subs, want in (([], "offen"), ([{"submitted": True}], "offen"), ([{"draft": True}], "entwurf"),
                       ([{"dateSubmittedEpochSeconds": 1}], "abgegeben")):
        got = _submission_status({"submissions": subs})[0]
        ok &= got == want
    ok &= _submission_status({})[0] == "unbekannt"
    print(f"submission-status Fälle: {'ok' if ok else 'FEHLER'}  ({time.monotonic() - t0:.1f}s)")
    print("SELFCHECK", "OK" if ok else "FEHLGESCHLAGEN")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
