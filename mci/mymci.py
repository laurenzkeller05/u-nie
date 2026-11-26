"""Client für die myMCI-API (https://my.mci4me.at, API https://callmyapi.mci4me.at/api/my/5).

myMCI ist die React/Ionic-App des MCI. Die Feldnamen unten stammen aus dem
ausgelieferten JS-Bundle der Web-App (main.js + Chunks) und sind nicht offiziell
dokumentiert -- alles wird defensiv geparst, das Original-JSON bleibt in ``raw``.

Login-Ablauf, Header (``X-Platform: desktop``, ``Software-Version``, Origin) und
``deviceFingerprint`` im Login-Body nach dem Vorbild von
https://github.com/MrP123/Lecture_Calendar_Fixer (api_call.py) -- danke an MrP123.

Gefundene Endpunkte (Bundle):
  POST /auth/credentials?lang=..   {username, password[, deviceFingerprint]} -> {userInfo, token{auth_token, refresh_token}}
  POST /token/refresh              {auth_token, refresh_token} -> {auth_token, refresh_token}
  GET  /versioninfo                -> {current}   (öffentlich)
  GET  /user/info, /user/profile
  GET  /termine                    -> [{id, title, start, end, art, online, color, raeume[{raum, standort}],
                                       referenten[{name}], gruppen[{bezeichnung}], lvnummern, course, sakailink, link}]
  GET  /mcitermine                 -> [{day, dates:[wie /termine + info, erlaubtehilfsmittel, einheiten]}]  (MCI-weiter Plan)
  GET  /beurteilungen/{pkz}        -> [{bezeichnung, beurteilungen:[{id, pruefungtitel, notenwert, notentext,
                                       erstelldatum, infotext, gesehen, ...}]}]
  GET  /beurteilung/{id}           -> {pruefungtitel, notenwert, notentext, punkte, maximalpunkte, mittelwert, median,
                                       lvaname, lvatyp, wintersemester, semesterjahr, pruefungdatum, erstelldatum}
  GET  /studienplan/{pkz}          -> {studienplanEintraege:[{name, modul, lvtyp, lvanummer, semesterjahr, sws, ects, ...}]}
  GET  /notifikationen             -> [{id, artlabel, art, text (HTML), zeitstempel, gesehen, link, kontext}]
  GET  /ical/link                  -> {webcalUrl}  (404 = noch kein Link; anlegen ginge per POST -- machen wir NICHT)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import DATA_DIR, MYMCI_API, MYMCI_ORIGIN, AuthError, require_credentials
from .util import html_to_text, parse_time

TOKEN_FILE = DATA_DIR / "mymci_token.json"
FINGERPRINT_FILE = DATA_DIR / "mymci_device.json"
FALLBACK_VERSION = "2.9.0"  # Stand 2026-11 laut /versioninfo
LANG = "de"
TZ = ZoneInfo("Europe/Vienna")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
EXAM_RE = re.compile(r"prüfung|pruefung|klausur|exam|test\b", re.I)


# ---------------------------------------------------------------- Hilfsfunktionen

def _jwt_exp(token: str | None) -> float | None:
    """exp-Claim eines JWT lesen (ohne Signaturprüfung). None wenn nicht lesbar."""
    if not token or token.count(".") < 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return float(exp) if exp is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _valid(token: str | None, margin: int = 60) -> bool:
    exp = _jwt_exp(token)
    return exp is not None and exp - margin > time.time()


def _write_private(path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _dt(v: Any) -> datetime | None:
    """ISO / Epoch / 'dd.mm.yyyy[ HH:MM]' -> datetime mit Zeitzone (naiv = Europe/Vienna)."""
    if isinstance(v, str):
        s = v.strip()
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ ,T]+(\d{1,2}):(\d{2}))?", s)
        if m:
            d, mo, y, h, mi = m.groups()
            try:
                return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), tzinfo=TZ)
            except ValueError:
                return None
    dt = parse_time(v)
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return default


def _names(items: Any, key: str) -> list[str]:
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict):
            v = it.get(key)
            if v:
                out.append(str(v).strip())
        elif isinstance(it, str) and it.strip():
            out.append(it.strip())
    return out


def _room(raeume: Any) -> str:
    if not isinstance(raeume, list):
        return str(raeume or "")
    parts = []
    for r in raeume:
        if not isinstance(r, dict):
            continue
        raum = (r.get("raum") or "").strip()
        ort = (r.get("standort") or "").strip()
        txt = raum if not ort or ort.lower() == raum.lower() else f"{raum} / {ort}" if raum else ort
        if txt:
            parts.append(txt)
    return ", ".join(parts)


# ---------------------------------------------------------------- Client

class MyMCIClient:
    def __init__(self) -> None:
        self.base = MYMCI_API.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "de-DE,de;q=0.9",
            "Origin": MYMCI_ORIGIN,
            "Referer": MYMCI_ORIGIN + "/",
            "X-Platform": "desktop",
        })
        self._version: str | None = None
        self._fp: str | None = None
        self._auth: str | None = None
        self._refresh: str | None = None
        self._userinfo: dict | None = None
        self._studienplan_cache: Any = None

    # ------------------------------------------------ Grundlagen

    def version(self) -> str:
        """Software-Version, die die API erwartet (öffentlich via /versioninfo)."""
        if self._version is None:
            try:
                r = self.s.get(f"{self.base}/versioninfo", timeout=10)
                r.raise_for_status()
                cur = (r.json() or {}).get("current")
                self._version = str(cur) if cur and cur != "undefined" else FALLBACK_VERSION
            except (requests.RequestException, ValueError, AttributeError):
                self._version = FALLBACK_VERSION
            self.s.headers["Software-Version"] = self._version
        return self._version

    def fingerprint(self) -> str:
        """Stabiler, zufälliger Geräte-Fingerprint (einmal erzeugt, in DATA_DIR gespeichert)."""
        if self._fp is None:
            fp = _read_json(FINGERPRINT_FILE).get("deviceFingerprint")
            if not fp:
                fp = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
                _write_private(FINGERPRINT_FILE, {"deviceFingerprint": fp})
            self._fp = fp
            self.s.headers["X-Device-Fingerprint"] = fp
        return self._fp

    def _send(self, method: str, path: str, *, params=None, json_body=None,
              auth: bool = True) -> requests.Response:
        """Ein Request mit 429-Behandlung (Retry-After). Kein 401-Handling hier."""
        self.version()
        self.fingerprint()
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        headers = {}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if auth and self._auth:
            headers["Authorization"] = f"Bearer {self._auth}"
        for attempt in range(4):
            try:
                r = self.s.request(method, url, params=params, json=json_body,
                                   headers=headers, timeout=30)
            except requests.RequestException as e:
                raise RuntimeError(f"myMCI nicht erreichbar: {e.__class__.__name__}") from None
            if r.status_code != 429 or attempt == 3:
                return r
            ra = r.headers.get("Retry-After", "")
            wait = float(ra) if ra.replace(".", "", 1).isdigit() else 5.0 * (attempt + 1)
            time.sleep(min(max(wait, 1.0), 120.0))
        return r  # pragma: no cover

    # ------------------------------------------------ Auth

    def _save_tokens(self) -> None:
        _write_private(TOKEN_FILE, {
            "auth_token": self._auth,
            "refresh_token": self._refresh,
            "userInfo": self._userinfo,
            "saved_at": int(time.time()),
        })

    def _try_refresh(self) -> bool:
        if not self._refresh or not self._auth:
            return False
        exp = _jwt_exp(self._refresh)
        if exp is not None and exp <= time.time():
            return False
        r = self._send("POST", "/token/refresh", auth=False,
                       json_body={"auth_token": self._auth, "refresh_token": self._refresh})
        if not r.ok:
            return False
        try:
            data = r.json() or {}
        except ValueError:
            return False
        tok = data.get("token") if isinstance(data.get("token"), dict) else data
        if not tok.get("auth_token"):
            return False
        self._auth = tok["auth_token"]
        self._refresh = tok.get("refresh_token") or self._refresh
        if isinstance(data.get("userInfo"), dict):
            self._userinfo = data["userInfo"]
        self._save_tokens()
        return True

    def _credentials_login(self) -> None:
        user, pw = require_credentials()
        if "@" not in user:
            user += "@mci4me.at"
        r = self._send("POST", "/auth/credentials", params={"lang": LANG}, auth=False,
                       json_body={"username": user, "password": pw,
                                  "deviceFingerprint": self.fingerprint()})
        del pw
        if r.status_code in (401, 403):
            raise AuthError("myMCI-Login fehlgeschlagen: Benutzername oder Passwort falsch.")
        if not r.ok:
            raise AuthError(f"myMCI-Login fehlgeschlagen (HTTP {r.status_code}).")
        try:
            data = r.json() or {}
        except ValueError:
            raise AuthError("myMCI-Login: Antwort war kein JSON.") from None
        tok = data.get("token") or {}
        if not isinstance(tok, dict) or not tok.get("auth_token"):
            raise AuthError("myMCI-Login: Antwort enthielt kein Token.")
        self._auth = tok["auth_token"]
        self._refresh = tok.get("refresh_token")
        self._userinfo = data.get("userInfo") if isinstance(data.get("userInfo"), dict) else None
        self._save_tokens()

    def login(self) -> None:
        """Token-Cache -> Refresh -> Credentials. Wirft AuthError."""
        cache = _read_json(TOKEN_FILE)
        self._auth = cache.get("auth_token")
        self._refresh = cache.get("refresh_token")
        if isinstance(cache.get("userInfo"), dict):
            self._userinfo = cache["userInfo"]
        if _valid(self._auth):
            return
        try:
            if self._try_refresh():
                return
        except RuntimeError:
            pass
        self._auth = self._refresh = None
        self._credentials_login()

    def ensure(self) -> None:
        if not _valid(self._auth):
            self.login()

    def _reauth(self) -> None:
        if not self._try_refresh():
            self._auth = self._refresh = None
            self._credentials_login()

    # ------------------------------------------------ Requests

    def get(self, path: str, **params) -> Any:
        """GET mit Auth; lang=de als Default; bei 401 einmal refresh/relogin."""
        self.ensure()
        params.setdefault("lang", LANG)
        r = self._send("GET", path, params=params)
        if r.status_code == 401:
            self._reauth()
            r = self._send("GET", path, params=params)
        if r.status_code == 401:
            raise AuthError("myMCI: Sitzung abgelaufen und erneuter Login fehlgeschlagen.")
        if r.status_code == 429:
            raise RuntimeError("myMCI: zu viele Anfragen (429), bitte später erneut versuchen.")
        r.raise_for_status()
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # ------------------------------------------------ Benutzer

    def userinfo(self) -> dict:
        self.ensure()
        if not self._userinfo:
            try:
                ui = self.get("/user/info")
                if isinstance(ui, dict):
                    self._userinfo = ui
                    self._save_tokens()
            except (requests.HTTPError, RuntimeError):
                pass
        ui = self._userinfo or {}
        settings = ui.get("settings") if isinstance(ui.get("settings"), dict) else {}
        imm = ui.get("immatrikulationen") if isinstance(ui.get("immatrikulationen"), list) else []
        pkz = settings.get("personenkennzeichen") or ui.get("personenkennzeichen")
        if not pkz and imm and isinstance(imm[0], dict):
            pkz = imm[0].get("personenkennzeichen")
        vorname = _first(ui, "vorname", "firstName", "firstname")
        nachname = _first(ui, "nachname", "lastName", "lastname")
        name = _first(ui, "name", "anzeigename", "displayName") or \
            " ".join(x for x in (vorname, nachname) if x) or None
        return {
            "name": name,
            "vorname": vorname,
            "nachname": nachname,
            "personenkennzeichen": pkz,
            "immatrikulationen": [
                {"personenkennzeichen": i.get("personenkennzeichen"), "bezeichnung": i.get("bezeichnung")}
                for i in imm if isinstance(i, dict)
            ],
            "roles": ui.get("possibleRoles") or [],
            "student": ui.get("student"),
            "raw": ui,
        }

    def _pkz(self) -> str:
        pkz = self.userinfo().get("personenkennzeichen")
        if not pkz:
            raise RuntimeError("Kein Personenkennzeichen in userInfo gefunden (keine Immatrikulation?).")
        return str(pkz)

    # ------------------------------------------------ Termine

    @staticmethod
    def _termin(e: dict, source: str) -> dict | None:
        start = _dt(_first(e, "start", "von", "beginn"))
        if start is None:
            return None
        end = _dt(_first(e, "end", "bis", "ende")) or start
        art = str(_first(e, "art", "typ", "type", default="") or "")
        title = str(_first(e, "title", "titel", "bezeichnung", "course", default="") or "")
        groups = _names(e.get("gruppen"), "bezeichnung")
        is_exam = bool(EXAM_RE.search(art) or e.get("pruefungam") or e.get("pruefung")
                       or e.get("erlaubtehilfsmittel"))
        tid = e.get("id")
        if tid in (None, ""):
            tid = f"{source}:{title}:{start.isoformat()}"
        return {
            "id": str(tid),
            "title": title + (f" | {', '.join(groups)}" if groups and groups[0] not in title else ""),
            "start": start,
            "end": end,
            "room": _room(e.get("raeume")),
            "lecturer": ", ".join(_names(e.get("referenten"), "name")),
            "type": art,
            "is_exam": is_exam,
            "online": e.get("online"),
            "course": e.get("course"),
            "sakailink": e.get("sakailink") or None,
            "link": e.get("link") or None,
            "source": source,
            "raw": e,
        }

    def termine(self, days: int | None = None, include_mci: bool = False) -> list[dict]:
        """Persönliche Termine aus /termine (wie 'Meine Termine' in der App).

        /mcitermine ist laut Bundle der MCI-weite Stundenplan ('mci-schedule', existiert
        auch als /public/mcitermine) und nicht personalisiert -- daher nur mit
        include_mci=True dazugemischt (dedupe per id).
        """
        items: dict[str, dict] = {}
        data = self.get("/termine")
        for e in data if isinstance(data, list) else []:
            if isinstance(e, dict) and (t := self._termin(e, "termine")):
                items.setdefault(t["id"], t)
        if include_mci:
            try:
                mci = self.get("/mcitermine")
            except (requests.HTTPError, RuntimeError):
                mci = []
            for day in mci if isinstance(mci, list) else []:
                if not isinstance(day, dict):
                    continue
                dates = day.get("dates") if isinstance(day.get("dates"), list) else [day]
                for e in dates:
                    if isinstance(e, dict) and (t := self._termin(e, "mcitermine")):
                        items.setdefault(t["id"], t)
        out = list(items.values())
        if days is not None:
            now = datetime.now(TZ)
            lo = now.replace(hour=0, minute=0, second=0, microsecond=0)
            hi = now + timedelta(days=days)
            out = [t for t in out if t["end"] >= lo and t["start"] <= hi]
        out.sort(key=lambda t: (t["start"], t["end"]))
        return out

    # ------------------------------------------------ Noten

    def studienplan(self) -> Any:
        """Rohes JSON von /studienplan/{personenkennzeichen}."""
        if self._studienplan_cache is None:
            self._studienplan_cache = self.get(f"/studienplan/{self._pkz()}")
        return self._studienplan_cache

    def _ects_map(self) -> dict[str, Any]:
        try:
            sp = self.studienplan()
        except (requests.HTTPError, RuntimeError):
            return {}
        entries = sp.get("studienplanEintraege") if isinstance(sp, dict) else sp
        m: dict[str, Any] = {}
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict) or e.get("ects") in (None, ""):
                continue
            for k in ("name", "nameEnglish", "lvanummer"):
                if e.get(k):
                    m.setdefault(str(e[k]).strip().lower(), e["ects"])
        return m

    def grades(self) -> list[dict]:
        """Beurteilungen aus /beurteilungen/{personenkennzeichen}.

        Annahme (aus dem Bundle): {id} ist userInfo.settings.personenkennzeichen bzw.
        das erste Element von userInfo.immatrikulationen. Antwort ist nach LV gruppiert
        ([{bezeichnung, beurteilungen:[...]}]); flache Listen werden ebenfalls akzeptiert.
        ECTS stehen nicht in den Beurteilungen -- best effort über den Studienplan
        (Name/LV-Nummer) nachgeschlagen.
        """
        data = self.get(f"/beurteilungen/{self._pkz()}")
        flat: list[tuple[str | None, dict]] = []
        for g in data if isinstance(data, list) else []:
            if not isinstance(g, dict):
                continue
            if isinstance(g.get("beurteilungen"), list):
                for b in g["beurteilungen"]:
                    if isinstance(b, dict):
                        flat.append((g.get("bezeichnung"), b))
            else:
                flat.append((None, g))
        ects_map = self._ects_map() if flat else {}
        out = []
        for group, b in flat:
            course = _first(b, "lvaname", "pruefungtitel", "titel", "bezeichnung") or group or ""
            wert, text = b.get("notenwert"), b.get("notentext")
            if isinstance(wert, (int, float)) and 0 < wert < 6:
                grade = f"{wert:g}" + (f" ({text})" if text else "")
            else:
                grade = str(text if text not in (None, "") else (wert if wert is not None else ""))
            ects = _first(b, "ects", "ECTS")
            if ects is None:
                for k in (b.get("lvaname"), b.get("pruefungtitel"), group, b.get("lvanummer")):
                    if k and str(k).strip().lower() in ects_map:
                        ects = ects_map[str(k).strip().lower()]
                        break
            out.append({
                "course": str(course),
                "title": b.get("pruefungtitel"),
                "group": group,
                "grade": grade,
                "grade_value": wert,
                "ects": ects,
                "date": _dt(_first(b, "pruefungdatum", "pruefungam", "erstelldatum", "datum")),
                "examiner": _first(b, "pruefer", "prueferin", "beurteiler", "referent", "lektor"),
                "id": b.get("id"),
                "read": b.get("gesehen"),
                "raw": b,
            })
        out.sort(key=lambda g: g["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out

    def grade_detail(self, grade_id: Any) -> Any:
        """Rohes JSON von /beurteilung/{id} (punkte, mittelwert, median, lvatyp, semester ...)."""
        return self.get(f"/beurteilung/{grade_id}")

    # ------------------------------------------------ Sonstiges

    def notifications(self) -> list[dict]:
        data = self.get("/notifikationen")
        out = []
        for n in data if isinstance(data, list) else []:
            if not isinstance(n, dict):
                continue
            out.append({
                "title": str(_first(n, "artlabel", "titel", "title", "art", default="") or ""),
                "text": html_to_text(str(_first(n, "text", "nachricht", "inhalt", default="") or "")),
                "created": _dt(_first(n, "zeitstempel", "erstelldatum", "created", "datum")),
                "read": n.get("gesehen"),
                "link": n.get("link"),
                "raw": n,
            })
        out.sort(key=lambda n: n["created"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out

    def ical_url(self) -> str | None:
        """Bestehenden iCal-Abo-Link lesen (webcal:// -> https://). Legt KEINEN neuen an."""
        try:
            data = self.get("/ical/link", lang="de")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise
        url = data.get("webcalUrl") if isinstance(data, dict) else None
        if not url:
            return None
        return re.sub(r"^webcals?://", "https://", str(url))


if __name__ == "__main__":
    # Selbsttest ohne Login: nur der öffentliche /versioninfo-Endpunkt.
    c = MyMCIClient()
    v = c.version()
    print(f"myMCI API erreichbar: {MYMCI_API} -> Software-Version {v}")
    print("JWT-Parser:", _jwt_exp("x." + base64.urlsafe_b64encode(b'{"exp": 1}').decode().rstrip("=") + ".y") == 1.0)
