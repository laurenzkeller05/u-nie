"""Kursdateien (Sakai "Resources") lokal spiegeln.

Format von /direct/content/site/{siteId}.json (ContentEntityProvider.getSiteListItems):
  {"content_collection": [ContentItem, ...]}  -- FLACHE Liste aller Ordner + Dateien
  ContentItem: title, url (…/access/content/group/{siteId}/Ordner/Datei.pdf, URL-encodiert),
  type ("collection" für Ordner, sonst MIME-Typ; Weblinks "text/url" mit webLinkUrl),
  size, modifiedDate (Sakai-Zeitstring "yyyyMMddHHmmssSSS", GMT), container,
  numChildren, visible, hidden.
Verschachtelte Antworten (resourceChildren) werden ebenfalls verstanden.

Lokale Dateien werden nie gelöscht – der User darf eigene Notizen in die Ordner legen.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .config import DATA_DIR, AuthError, files_dir
from .util import parse_time, safe_name

MANIFEST = DATA_DIR / "files_manifest.json"
LINKS_FILE = "_links.md"
LINK_TYPES = {"text/url", "text/x-uri", "application/x-url"}
LISTING_PATHS = ("/content/site/{site_id}.json", "/direct/content/site/{site_id}.json")
CHILD_KEYS = ("resourceChildren", "children", "content_collection")

Progress = Callable[[str], None] | None


def _say(progress: Progress, msg: str) -> None:
    if progress:
        try:
            progress(msg)
        except Exception:
            pass


# --------------------------------------------------------------------------- parsing


def _parse_modified(v: Any) -> datetime | None:
    """Sakai-Property-Zeit 'yyyyMMddHHmmss[SSS]' (GMT) oder alles, was parse_time kann."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit() and len(s) in (14, 17):
            try:
                dt = datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                if len(s) == 17:
                    dt = dt.replace(microsecond=int(s[14:]) * 1000)
                return dt
            except ValueError:
                return None
    try:
        return parse_time(v)
    except (OverflowError, ValueError, OSError, TypeError):
        return None


def _to_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _flatten(data: Any) -> list[dict]:
    """Flache oder verschachtelte Listing-Antwort -> flache Liste aller Items."""
    out: list[dict] = []
    seen: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen.add(id(node))
        if "url" in node or "type" in node or "title" in node:
            out.append(node)
        for key in CHILD_KEYS:
            if isinstance(node.get(key), list):
                walk(node[key])

    walk(data)
    return out


def _is_folder(item: dict) -> bool:
    t = str(item.get("type") or "").lower()
    if t == "collection" or item.get("isCollection") is True or item.get("collection") is True:
        return True
    url = str(item.get("url") or "")
    return url.endswith("/") and not t


def _is_link(item: dict) -> bool:
    t = str(item.get("type") or "").lower()
    return t in LINK_TYPES or bool(item.get("webLinkUrl"))


def _is_hidden(item: dict) -> bool:
    return item.get("hidden") is True or item.get("isHidden") is True or item.get("visible") is False


def _segments_after_site(url_or_path: str, site_id: str) -> list[str] | None:
    path = urlparse(url_or_path).path if "://" in url_or_path else url_or_path
    parts = [p for p in path.split("/") if p]
    for i in range(len(parts) - 1):
        # /access/content/group/{siteId}/...  bzw. container /content/group/{siteId}/...
        if parts[i] == "group" and unquote(parts[i + 1]) == site_id:
            return [unquote(p) for p in parts[i + 2:]]
    return None


def _rel_path(item: dict, site_id: str) -> str:
    url = str(item.get("url") or "")
    segs = _segments_after_site(url, site_id) if url else None
    if not segs:
        title = str(item.get("title") or "") or (unquote(url.rstrip("/").rsplit("/", 1)[-1]) if url else "")
        folder = _segments_after_site(str(item.get("container") or ""), site_id) or []
        segs = [*folder, title or "unbenannt"]
    return "/".join(safe_name(s) for s in segs if s is not None)


# --------------------------------------------------------------------------- listing


def _fetch_listing(client, site_id: str) -> Any:
    last: Exception | None = None
    for tpl in LISTING_PATHS:
        try:
            return client.get(tpl.format(site_id=site_id))
        except AuthError:
            raise
        except Exception as e:  # falscher Pfad-Präfix o. ä. -> nächste Variante
            last = e
    raise last or RuntimeError("kein Listing")


def _collect(client, site: dict) -> tuple[list[dict], list[dict]]:
    site_id = str(site.get("id") or "")
    items = _flatten(_fetch_listing(client, site_id))
    files: list[dict] = []
    links: list[dict] = []
    used: set[str] = set()
    for it in items:
        try:
            if _is_folder(it) or _is_hidden(it):
                continue
            title = str(it.get("title") or "")
            url = str(it.get("url") or "")
            if _is_link(it):
                links.append({
                    "title": title or url,
                    "url": str(it.get("webLinkUrl") or url),
                    "rel_path": _rel_path(it, site_id),
                })
                continue
            if not url:
                continue
            rel = _rel_path(it, site_id)
            if rel in used:  # Namenskollision nach safe_name
                stem, dot, ext = rel.rpartition(".")
                n = 2
                while (cand := f"{stem} ({n}).{ext}" if dot else f"{rel} ({n})") in used:
                    n += 1
                rel = cand
            used.add(rel)
            files.append({
                "rel_path": rel,
                "url": url,
                "size": _to_int(it.get("size")),
                "modified": _parse_modified(it.get("modifiedDate") or it.get("modified")),
                "title": title or rel.rsplit("/", 1)[-1],
            })
        except Exception:
            continue  # einzelnes kaputtes Item nie fatal
    return files, links


def list_files(client, site: dict) -> list[dict]:
    return _collect(client, site)[0]


def course_dir(site: dict) -> Path:
    from mci.util import course_info

    title = str(site.get("title") or site.get("id") or "Kurs")
    sem, name = course_info(title)
    base = files_dir() / (f"{sem}_Semester" if sem else "Sakai_Allgemein")
    return base / "Sakai" / safe_name(name) if sem else base / safe_name(name)


# --------------------------------------------------------------------------- manifest


def _load_manifest() -> dict:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(m: dict) -> None:
    try:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        tmp = MANIFEST.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(m, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, MANIFEST)
    except OSError:
        pass


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _needs_download(f: dict, dest: Path, prev: dict | None) -> bool:
    if not dest.exists():
        return True
    try:
        local_size = dest.stat().st_size
    except OSError:
        return True
    if prev is None:
        # Datei liegt schon da (z. B. manuell kopiert): nur bei abweichender Größe neu laden
        return f["size"] is not None and f["size"] != local_size
    if f["size"] is not None and f["size"] != prev.get("size"):
        return True
    # lokale Datei verändert/abgeschnitten (gegen die beim Download gemessene Größe)
    if local_size != prev.get("local_size", prev.get("size", local_size)):
        return True
    if f["modified"] is not None and _iso(f["modified"]) != prev.get("modified"):
        return True
    return prev.get("path") != str(dest)


def _write_links(cdir: Path, site: dict, links: list[dict]) -> None:
    lines = [f"# Links – {site.get('title') or site.get('id')}", ""]
    for ln in sorted(links, key=lambda x: x["rel_path"].lower()):
        folder = ln["rel_path"].rsplit("/", 1)[0] if "/" in ln["rel_path"] else ""
        where = f" _(in {folder})_" if folder else ""
        lines.append(f"- [{ln['title']}]({ln['url']}){where}")
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / LINKS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- sync


def sync_site(client, site: dict, dry_run: bool = False, progress: Progress = None) -> dict:
    cdir = course_dir(site)
    result: dict = {"site": site.get("title") or site.get("id"), "downloaded": [], "skipped": 0,
                    "failed": [], "dir": str(cdir)}
    try:
        files, links = _collect(client, site)
    except AuthError:
        raise
    except Exception as e:
        result["failed"].append(("<listing>", f"{type(e).__name__}: {e}"))
        _say(progress, f"{result['site']}: Listing fehlgeschlagen ({e})")
        return result

    if links and not dry_run:
        try:
            _write_links(cdir, site, links)
        except OSError as e:
            result["failed"].append((LINKS_FILE, str(e)))

    manifest = _load_manifest()
    root = cdir.resolve()
    for f in files:
        rel = f["rel_path"]
        try:
            dest = (cdir / rel).resolve()
            if root not in dest.parents:
                raise ValueError("Pfad außerhalb des Kursordners")
            prev = manifest.get(f["url"])
            if not _needs_download(f, dest, prev):
                result["skipped"] += 1
                if prev is None and not dry_run:
                    manifest[f["url"]] = {"path": str(dest), "size": f["size"], "modified": _iso(f["modified"]),
                                          "local_size": dest.stat().st_size}
                continue
            if dry_run:
                _say(progress, f"[dry-run] {rel}")
                result["downloaded"].append(rel)
                continue
            _say(progress, f"↓ {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            # In Temp-Datei laden, damit download() nicht wegen gleicher Größe überspringt
            # und eine halbe Datei nie die alte Version ersetzt.
            tmp = dest.with_name(f".{dest.name}.part")
            got = Path(client.download(f["url"], tmp))
            os.replace(got, dest)
            manifest[f["url"]] = {"path": str(dest), "size": f["size"], "modified": _iso(f["modified"]),
                                  "local_size": dest.stat().st_size}
            result["downloaded"].append(rel)
            _save_manifest(manifest)
        except AuthError:
            _save_manifest(manifest)
            raise
        except Exception as e:
            result["failed"].append((rel, f"{type(e).__name__}: {e}"))
            _say(progress, f"✗ {rel}: {e}")
            try:
                tmp_left = (cdir / rel).with_name(f".{Path(rel).name}.part")
                if tmp_left.exists():
                    tmp_left.unlink()  # nur eigene Temp-Datei
            except OSError:
                pass

    if not dry_run:
        _save_manifest(manifest)
    _say(progress, f"{result['site']}: {len(result['downloaded'])} neu/geändert, "
                   f"{result['skipped']} aktuell, {len(result['failed'])} Fehler")
    return result


def sync_all(client, dry_run: bool = False, progress: Progress = None) -> list[dict]:
    out: list[dict] = []
    try:
        sites = client.sites()
    except AuthError:
        raise
    for site in sites or []:
        _say(progress, f"== {site.get('title') or site.get('id')}")
        try:
            out.append(sync_site(client, site, dry_run=dry_run, progress=progress))
        except AuthError:
            raise
        except Exception as e:
            out.append({"site": site.get("title"), "downloaded": [], "skipped": 0,
                        "failed": [("<site>", f"{type(e).__name__}: {e}")], "dir": str(course_dir(site))})
    return out
