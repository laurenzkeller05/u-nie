"""Dateien als ENTWURF in ein Sakai-Assignment laden ("Save Draft"). Kann bewusst NICHT abgeben.

Ablauf nach jtayped/sakai-mcp (MIT, submit.py): doView_submission öffnen, je Datei ein
doAttachUpload-Post, dann option=save. Sakai antwortet bei abgelehntem CSRF-Token oder
verlorener Tool-Session mit HTTP 200 und dem alten Formular, deshalb wird nach jedem Schritt
geprüft, welches Velocity-Template zurückkam.
"""
from __future__ import annotations

import mimetypes
import re
import unicodedata
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from mci import config

TOOL_ID = "sakai.assignment.grades"
VIEW = "chef_assignments_student_view_submission.vm"
PLEDGE = "chef_assignments_student_view_assignment_honor_pledge.vm"
DONE = "chef_assignments_student_view_submission_confirmation.vm"
EULA = "review_service_eula_agreement"
TEXT = "Assignment.view_submission_text"
FILE_TYPES = {"ATTACHMENT_ONLY_ASSIGNMENT_SUBMISSION", "TEXT_AND_ATTACHMENT_ASSIGNMENT_SUBMISSION",
              "SINGLE_ATTACHMENT_SUBMISSION"}


class DraftError(RuntimeError):
    pass


class Page(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.templates: list[str] = []
        self.forms: dict[str, dict] = {}
        self.texts: dict[str, list[str]] = defaultdict(list)
        self._form = None
        self._textarea = None
        self._caps: list[list] = []
        self.feed(html)
        self.close()

    def _key(self, tag, a):
        cls, id_ = a.get("class", "").split(), a.get("id")
        if tag == "div" and "sak-banner-error" in cls:
            return "alert"
        if tag == "div" and "sak-banner-success" in cls:
            return "success"
        if tag == "a" and "'removeAttachment'" in a.get("onclick", ""):
            return "remove-link"
        if tag == "span" and "skip" in cls and any(c[0] == "remove-link" for c in self._caps):
            return "attachment"

    def handle_comment(self, data):
        if m := re.match(r"\s*start:\s*(\S+\.vm)", data):
            self.templates.append(m[1])

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        for c in self._caps:
            c[2] += c[1] == tag
        if key := self._key(tag, a):
            self._caps.append([key, tag, 1, []])
        if tag == "form":
            self._form = {"action": a.get("action", ""), "fields": {}, "upload_url": None}
            self.forms[a.get("name") or a.get("id", "")] = self._form
        elif self._form is None or not a.get("name"):
            return
        elif tag == "textarea":
            self._textarea = [a["name"], []]
        elif tag == "input":
            kind = a.get("type", "text").lower()
            if kind == "file":
                if m := re.search(r"\.action='([^']*)'", a.get("onchange", "")):
                    self._form["upload_url"] = m[1].replace("&amp;", "&")
            elif kind in ("hidden", "text") or (kind in ("checkbox", "radio") and "checked" in a):
                self._form["fields"][a["name"]] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "textarea" and self._textarea and self._form is not None:
            self._form["fields"][self._textarea[0]] = "".join(self._textarea[1])
            self._textarea = None
        if tag == "form":
            self._form = None
        for c in self._caps:
            c[2] -= c[1] == tag
        for c in [c for c in self._caps if c[2] == 0]:
            self.texts[c[0]].append(" ".join("".join(c[3]).split()))
            self._caps.remove(c)

    def handle_data(self, data):
        if self._textarea:
            self._textarea[1].append(data)
        for c in self._caps:
            c[3].append(data)


def _portal_session():
    """Formular-Login über /portal/relogin. Nur so legt Sakai@MCI ein CSRF-Token in die Session
    (Login über /direct/session rendert $sakai_csrf_token leer und Uploads werden still verworfen)."""
    import requests

    u, pw = config.require_credentials()
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 mci-cli"
    s.get(config.SAKAI_BASE + "/portal/xlogin", timeout=30)
    for user in dict.fromkeys((u, u.split("@")[0])):
        s.post(config.SAKAI_BASE + "/portal/relogin", data={"eid": user, "pw": pw, "submit": "Log in"}, timeout=30)
        if s.get(config.SAKAI_BASE + "/direct/session/current.json", timeout=30).json().get("userId"):
            return s
    raise DraftError("Portal-Login fehlgeschlagen")


def _esc(s: str) -> str:
    import html

    return html.escape(s)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def _state(sub: dict | None) -> str:
    sub = sub or {}
    if sub.get("draft"):
        return "entwurf"
    if sub.get("submitted") and sub.get("dateSubmittedEpochSeconds"):
        return "abgegeben"
    return "leer"


def _check(page: Page, template: str, what: str) -> None:
    if page.texts["alert"]:
        raise DraftError(f"{what}: {' '.join(page.texts['alert'])}")
    if template not in page.templates:
        raise DraftError(f"{what}: Sakai zeigte {', '.join(page.templates) or 'eine unbekannte Seite'} "
                         f"statt {template} (Session abgelaufen oder CSRF abgelehnt?)")


def _post(ps, url: str, data: dict, upload=None) -> Page:
    u = urlsplit(url)
    if u.netloc and u.netloc != urlsplit(config.SAKAI_BASE).netloc:
        raise DraftError(f"Post an fremden Host verweigert: {u.netloc}")
    if data.get("option") in ("post", "confirm"):  # harte Sperre: niemals final abgeben
        raise DraftError("Finale Abgabe ist in diesem Tool gesperrt.")
    # Upload-URL enthält ein leeres sakai_csrf_token, das bei Multipart den Body-Wert überschreiben würde
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "sakai_csrf_token"])
    r = ps.post(config.SAKAI_BASE + u.path + (f"?{query}" if query else ""), data=data,
                      files={"upload": upload} if upload else None, timeout=120)
    r.raise_for_status()
    return Page(r.text)


def _fields(form: dict, option: str, text: str | None = None) -> dict:
    fields = {k: v for k, v in form["fields"].items() if v is not None}
    if text is not None:
        fields[TEXT] = text
    return {**fields, "option": option}


def save_draft(client, assignment_id: str, files: list[str], dry_run: bool = False,
               text: str | None = None) -> dict:
    """text: Klartext für das Textfeld (wird als <p>-HTML gespeichert); None lässt es unverändert."""
    if not files and text is None:
        raise DraftError("Nichts zu tun: Dateien oder --text angeben")
    html_text = None if text is None else "".join(f"<p>{_esc(line)}</p>" for line in text.splitlines() or [""])
    client.ensure()
    paths = [Path(f).expanduser() for f in files]
    for p in paths:
        if not p.is_file() or p.stat().st_size == 0:
            raise DraftError(f"{p}: keine lesbare, nicht-leere Datei")

    a = client.get(f"/direct/assignment/item/{assignment_id}.json")
    sub = (a.get("submissions") or [None])[0]
    if a.get("submissionType") not in FILE_TYPES:
        raise DraftError(f"Assignment-Typ {a.get('submissionType')} nimmt keine Dateien an")
    if a.get("status") not in ("OPEN", "DUE"):
        raise DraftError(f"Assignment ist {a.get('status')}, nichts mehr möglich")
    if _state(sub) == "abgegeben":
        raise DraftError("Schon abgegeben: ein Entwurf würde die Abgabe zurückziehen. Abbruch.")
    if sub and sub.get("canSubmit") is False:
        raise DraftError("Sakai sagt: canSubmit=false")

    info = {
        "assignment": a.get("title"),
        "faellig": a.get("dueTimeString"),
        "gruppe": bool(sub and sub.get("groupId")),
        "resubmission_erlaubt": bool(a.get("allowResubmission")),
        "status_vorher": _state(sub),
        "anhaenge_vorher": [x.get("name") for x in (sub or {}).get("submittedAttachments") or []],
        "dateien": [{"name": p.name, "bytes": p.stat().st_size} for p in paths],
        "text_neu": html_text,
        "text_vorher": (sub or {}).get("submittedText"),
    }
    if dry_run:
        return {"dry_run": True, **info}

    tool = next((t["placementId"] for pg in client.get(f"/direct/site/{a['context']}/pages.json")
                 for t in pg.get("tools", []) if t.get("toolId") == TOOL_ID), None)
    if not tool:
        raise DraftError("Kein Assignment-Tool in der Site gefunden")
    ref = f"/assignment/a/{a['context']}/{a['id']}"
    ps = _portal_session()
    r = ps.get(f"{config.SAKAI_BASE}/portal/tool/{tool}",
                     params={"assignmentReference": ref, "sakai_action": "doView_submission"}, timeout=60)
    page = Page(r.text)
    if PLEDGE in page.templates:
        raise DraftError("Assignment verlangt eine Honor Pledge: bitte im Browser bestätigen")
    _check(page, VIEW, "Formular öffnen")
    tok = (page.forms.get("addSubmissionForm") or {}).get("fields", {}).get("sakai_csrf_token", "")
    if not tok or tok.startswith("$"):
        raise DraftError("Kein CSRF-Token im Formular, Upload würde verworfen")
    form = page.forms.get("addSubmissionForm")
    if not form:
        raise DraftError("addSubmissionForm fehlt")
    if EULA in form["fields"]:
        raise DraftError("Plagiats-Service-Lizenz muss im Browser akzeptiert werden")

    for p in paths:
        form = page.forms["addSubmissionForm"]
        if not form["upload_url"]:
            raise DraftError("Formular hat kein Datei-Feld")
        before = page.texts["attachment"]
        upload = (p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        page = _post(ps, form["upload_url"], _fields(form, form["fields"].get("option", "cancel")), upload)
        _check(page, VIEW, f"Upload {p.name}")
        names = page.texts["attachment"]
        if len(names) != len(before) + 1 or _nfc(p.name) not in map(_nfc, names):
            raise DraftError(f"Upload {p.name}: Datei erscheint nicht bei den Anhängen")

    form = page.forms["addSubmissionForm"]
    if html_text is not None and TEXT not in form["fields"]:
        raise DraftError("Formular hat kein Textfeld")
    page = _post(ps, form["action"], _fields(form, "save", html_text), ("", b"", "application/octet-stream"))
    _check(page, DONE, "Entwurf speichern")

    after = (client.get(f"/direct/assignment/item/{assignment_id}.json").get("submissions") or [None])[0]
    info["status_nachher"] = _state(after)
    info["anhaenge_nachher"] = [x.get("name") for x in (after or {}).get("submittedAttachments") or []]
    problems = []
    if info["status_nachher"] != "entwurf":
        problems.append(f"Status ist {info['status_nachher']}, erwartet entwurf")
    info["text_nachher"] = (after or {}).get("submittedText")
    if html_text is not None and "".join((info["text_nachher"] or "").split()) != "".join(html_text.split()):
        problems.append("Text in Sakai weicht ab")
    expected = [*info["anhaenge_vorher"], *(p.name for p in paths)]
    missing = [n for n in expected if _nfc(n) not in map(_nfc, info["anhaenge_nachher"])]
    if missing:
        problems.append(f"fehlt in den Anhängen: {', '.join(missing)}")
    return {"done": "entwurf gespeichert", "verified": not problems, "problems": problems, **info}
