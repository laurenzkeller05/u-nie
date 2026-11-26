"""mci — Sakai@MCI und myMCI im Terminal.  `uv run mci --help`"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from mci import config
from mci.util import html_to_text, local, safe_name

# ── Ausgabe ──────────────────────────────────────────────────────────────────
TTY = sys.stdout.isatty()


def c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if TTY else s


BOLD, DIM, RED, YEL, GRN, CYA = "1", "2", "31", "33", "32", "36"


def now() -> datetime:
    return datetime.now(timezone.utc)


def countdown(dt: datetime | None) -> str:
    if not dt:
        return ""
    delta = dt - now()
    secs = delta.total_seconds()
    if secs < 0:
        return c(RED, "überfällig")
    h = secs / 3600
    if h < 24:
        return c(RED, f"in {h:.0f} h")
    d = delta.days
    return c(YEL if d <= 3 else GRN, f"in {d} T")


def jsonable(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items() if k != "raw"}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    return o


def emit(args, data: Any, human: Callable[[], None]) -> None:
    if args.json:
        print(json.dumps(jsonable(data), ensure_ascii=False, indent=2))
    else:
        human()


def match(text: str | None, needle: str | None) -> bool:
    return not needle or (needle.lower() in (text or "").lower())


# ── Clients (lazy) ───────────────────────────────────────────────────────────
_sakai = _mymci = None


def sakai():
    global _sakai
    if _sakai is None:
        from mci.sakai import SakaiClient

        _sakai = SakaiClient()
        _sakai.ensure()
    return _sakai


def mymci():
    global _mymci
    if _mymci is None:
        from mci.mymci import MyMCIClient

        _mymci = MyMCIClient()
        _mymci.ensure()
    return _mymci


def try_source(name: str, fn: Callable[[], list], warnings: list[str]) -> list:
    try:
        return fn()
    except Exception as e:  # eine Quelle darf ausfallen, der Rest läuft weiter
        warnings.append(f"{name}: {e}")
        return []


# ── Befehle ──────────────────────────────────────────────────────────────────
def cmd_status(args):
    out = {}
    for name, fn in (("sakai", lambda: sakai().whoami()), ("mymci", lambda: mymci().userinfo())):
        try:
            info = fn()
            out[name] = {"ok": True, "user": info.get("displayName") or info.get("name") or info.get("userEid")}
        except Exception as e:
            out[name] = {"ok": False, "error": str(e)}

    def human():
        for name, s in out.items():
            mark = c(GRN, "✓") if s["ok"] else c(RED, "✗")
            print(f"{mark} {name:6} {s.get('user') or s.get('error')}")
        print(c(DIM, f"Dateien: {config.files_dir()}"))

    emit(args, out, human)


def cmd_courses(args):
    sites = sakai().sites()

    def human():
        for s in sites:
            print(f"{c(BOLD, s['title'])}  {c(DIM, s['id'])}")

    emit(args, sites, human)


def open_assignments(all_: bool, course: str | None) -> list[dict]:
    items = [a for a in sakai().assignments() if match(a.get("site_title"), course)]
    if not all_:
        items = [a for a in items if a.get("status") in ("offen", "entwurf", "unbekannt")
                 and not (a.get("close") or a.get("due") or now()) < now() - timedelta(days=1)]
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(items, key=lambda a: a.get("due") or far)


def cmd_assignments(args):
    items = open_assignments(args.all, args.course)

    def human():
        if not items:
            print(c(GRN, "Keine offenen Assignments. 🎉"))
        for a in items:
            print(f"{c(BOLD, a['title'])}  {c(DIM, a.get('site_title') or '')}")
            print(f"   fällig {local(a.get('due'))}  {countdown(a.get('due'))}  "
                  f"[{a.get('status')}]  id={c(CYA, str(a['id']))}")

    emit(args, items, human)


def assignment_dir(a: dict) -> Path:
    from mci.files import course_dir

    return course_dir({"title": a.get("site_title") or "Unbekannt"}) / "_Assignments" / safe_name(a["title"])


def cmd_assignment(args):
    a = sakai().assignment(args.id)
    a["instructions"] = html_to_text(a.get("instructions_html"))
    if args.download:
        d = assignment_dir(a)
        (d / "abgabe").mkdir(parents=True, exist_ok=True)
        saved = []
        for att in a.get("attachments") or []:
            try:
                saved.append(str(sakai().download(att["url"], d / safe_name(att["name"]))))
            except Exception as e:
                saved.append(f"FEHLER {att.get('name')}: {e}")
        md = [
            f"# {a['title']}",
            "",
            f"- Kurs: {a.get('site_title')}",
            f"- Offen ab: {local(a.get('open'))}",
            f"- Fällig: {local(a.get('due'))}",
            f"- Schluss: {local(a.get('close'))}",
            f"- Status: {a.get('status')}",
            f"- Note: {a.get('grade') or '–'}",
            f"- Link: {a.get('url') or '–'}",
            f"- Assignment-ID: {a['id']}",
            "",
            "## Aufgabenstellung",
            "",
            a["instructions"] or "_(keine Beschreibung)_",
            "",
            "## Anhänge",
            "",
            *[f"- {Path(p).name}" for p in saved],
        ]
        (d / "aufgabe.md").write_text("\n".join(md), encoding="utf-8")
        a["dir"] = d
        a["saved"] = saved

    def human():
        print(c(BOLD, a["title"]), c(DIM, a.get("site_title") or ""))
        print(f"fällig {local(a.get('due'))} {countdown(a.get('due'))}  status={a.get('status')}  note={a.get('grade') or '–'}")
        print(f"link   {a.get('url') or '–'}\n")
        print(a["instructions"] or "(keine Beschreibung)")
        for att in a.get("attachments") or []:
            print(c(DIM, f"📎 {att['name']}"))
        if args.download:
            print(c(GRN, f"\n→ {a['dir']}"))

    emit(args, a, human)


def cmd_deadlines(args):
    warn: list[str] = []
    horizon = now() + timedelta(days=args.days)
    rows: list[dict] = []
    for a in try_source("Sakai-Assignments", lambda: open_assignments(False, None), warn):
        rows.append({"when": a.get("due"), "kind": "Assignment", "title": a["title"],
                     "course": a.get("site_title"), "id": a["id"], "status": a.get("status")})
    for q in try_source("Sakai-Quizzes", lambda: sakai().quizzes(), warn):
        rows.append({"when": q.get("due"), "kind": "Quiz", "title": q["title"], "course": q.get("site_title")})
    titles = {r["title"].strip().lower() for r in rows}
    for e in try_source("Sakai-Kalender", lambda: sakai().calendar(), warn):
        t = (e.get("title") or "").strip()
        if t.lower().startswith("due ") and t[4:].strip().lower() in titles:
            continue  # Assignment steht schon drin
        if (e.get("type") or "").lower() not in ("", "activity", "class session", "lecture", "meeting"):
            rows.append({"when": e.get("start"), "kind": f"Kalender ({e.get('type')})",
                         "title": e["title"], "course": e.get("site_title")})
    for t in try_source("myMCI-Termine", lambda: mymci().termine(), warn):
        if t.get("is_exam"):
            rows.append({"when": t.get("start"), "kind": "Prüfung", "title": t["title"], "course": t.get("course") or t.get("room")})
    rows = [r for r in rows if r["when"] and now() - timedelta(days=1) <= r["when"] <= horizon]
    # Dubletten (Assignment steht auch im Kalender) entfernen
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda r: r["when"]):
        key = (r["title"].strip().lower(), r["when"].date())
        if key not in seen:
            seen.add(key)
            uniq.append(r)

    def human():
        print(c(BOLD, f"Deadlines der nächsten {args.days} Tage"))
        if not uniq:
            print(c(GRN, "Nichts fällig."))
        for r in uniq:
            print(f"{local(r['when']):>22}  {countdown(r['when']):>14}  {c(CYA, r['kind']):<12} "
                  f"{c(BOLD, r['title'])}  {c(DIM, r.get('course') or '')}")
        for w in warn:
            print(c(YEL, f"⚠ {w}"))

    emit(args, {"deadlines": uniq, "warnings": warn}, human)


def cmd_sync_files(args):
    from mci import files

    client = sakai()
    from mci.util import course_info

    sites = [s for s in client.sites() if match(s["title"], args.course)]
    sems = [course_info(s["title"])[0] for s in sites]
    current = max([x for x in sems if x] or [0])
    if not args.all and not args.course:
        sites = [s for s, n in zip(sites, sems) if n == current]
    say = (lambda m: None) if args.json else (lambda m: print(c(DIM, m)))
    results = [files.sync_site(client, s, dry_run=args.dry_run, progress=say) for s in sites]

    def human():
        for s, r in zip(sites, results):
            fail = len(r.get("failed") or [])
            print(f"{c(BOLD, s['title'])}: {len(r.get('downloaded') or [])} neu, {r.get('skipped', 0)} unverändert"
                  + (c(RED, f", {fail} Fehler") if fail else "") + c(DIM, f"  → {r.get('dir')}"))

    emit(args, results, human)


def cmd_announcements(args):
    items = [a for a in sakai().announcements(days=args.days) if match(a.get("site_title"), args.course)]
    for a in items:
        a["body"] = html_to_text(a.get("body_html"))

    def human():
        for a in items:
            print(f"{c(BOLD, a['title'])}  {c(DIM, (a.get('site_title') or '') + ' · ' + local(a.get('created')))}")
            body = a["body"]
            print("   " + (body[:400] + "…" if len(body) > 400 else body).replace("\n", "\n   "))
            print()

    emit(args, items, human)


def cmd_grades(args):
    warn: list[str] = []
    data = {
        "sakai": try_source("Sakai-Noten", lambda: sakai().grades(), warn),
        "mymci": try_source("myMCI-Beurteilungen", lambda: mymci().grades(), warn),
        "warnings": warn,
    }

    def human():
        print(c(BOLD, "myMCI (Beurteilungen)"))
        for g in data["mymci"]:
            print(f"  {g.get('course')}: {c(CYA, str(g.get('grade')))}  {c(DIM, (str(g['ects']) + ' ECTS  ' if g.get('ects') else '') + local(g.get('date')))}")
        print(c(BOLD, "\nSakai (Gradebook)"))
        for g in data["sakai"]:
            mx = f"/{g['max']}" if g.get("max") else ""
            print(f"  {g.get('site_title')} · {g.get('item')}: {c(CYA, str(g.get('grade') if g.get('grade') not in (None, '') else '–'))}{mx}")
        for w in warn:
            print(c(YEL, f"⚠ {w}"))

    emit(args, data, human)


def cmd_termine(args):
    horizon = now() + timedelta(days=args.days)
    items = [t for t in mymci().termine() if t.get("start") and now() - timedelta(hours=2) <= t["start"] <= horizon]

    def human():
        day = None
        for t in items:
            d = t["start"].astimezone().strftime("%A %d.%m.")
            if d != day:
                day = d
                print(c(BOLD, f"\n{d}"))
            end = t["end"].astimezone().strftime("%H:%M") if t.get("end") else ""
            exam = c(RED, " PRÜFUNG") if t.get("is_exam") else ""
            print(f"  {t['start'].astimezone():%H:%M}–{end}  {t['title']}{exam}  {c(DIM, t.get('room') or '')}")

    emit(args, items, human)


def cmd_draft(args):
    from mci.draft import save_draft

    res = save_draft(sakai(), args.id, args.files, dry_run=args.dry_run, text=args.text)

    def human():
        for k, v in res.items():
            print(f"{k:22} {v}")

    emit(args, res, human)


def cmd_raw(args):
    client = sakai() if args.system == "sakai" else mymci()
    print(json.dumps(client.get(args.path), ensure_ascii=False, indent=2, default=str))


# ── Parser ───────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mci", description="Sakai@MCI + myMCI im Terminal")
    p.add_argument("--json", action="store_true", help="maschinenlesbare Ausgabe")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        sp.set_defaults(fn=fn)
        return sp

    add("status", cmd_status, "Logins prüfen")
    add("courses", cmd_courses, "Sakai-Kurse")
    sp = add("deadlines", cmd_deadlines, "alle Fristen zusammengeführt")
    sp.add_argument("--days", type=int, default=30)
    sp = add("assignments", cmd_assignments, "offene Assignments")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--course")
    sp = add("assignment", cmd_assignment, "Details zu einem Assignment")
    sp.add_argument("id")
    sp.add_argument("--download", action="store_true", help="Aufgabe + Anhänge lokal ablegen")
    sp = add("sync-files", cmd_sync_files, "Kursdateien spiegeln")
    sp.add_argument("--course")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--all", action="store_true", help="alle Semester statt nur dem aktuellen")
    sp = add("announcements", cmd_announcements, "Ankündigungen")
    sp.add_argument("--days", type=int, default=14)
    sp.add_argument("--course")
    add("grades", cmd_grades, "Noten")
    sp = add("termine", cmd_termine, "myMCI-Stundenplan")
    sp.add_argument("--days", type=int, default=14)
    sp = add("draft", cmd_draft, "Dateien als ENTWURF hochladen (gibt nie final ab)")
    sp.add_argument("id")
    sp.add_argument("files", nargs="*")
    sp.add_argument("--text", help="Text fürs Textfeld (ersetzt den bisherigen)")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("raw", cmd_raw, "rohes JSON (zum Erkunden)")
    sp.add_argument("system", choices=["sakai", "mymci"])
    sp.add_argument("path")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (config.ConfigError, config.AuthError) as e:
        print(c(RED, f"✗ {e}"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # SakaiError, HTTP-Fehler usw. lesbar statt Traceback
        if type(e).__name__ in ("SakaiError", "MyMCIError", "DraftError", "HTTPError", "ConnectionError", "Timeout"):
            print(c(RED, f"✗ {type(e).__name__}: {e}"), file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
