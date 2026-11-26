from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "ul", "ol", "table"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.cell = 0  # innerhalb von <td>/<th> keine Zeilenumbrüche
        self.in_table = 0

    def handle_endtag(self, tag):
        if tag == "table":
            self.in_table = max(0, self.in_table - 1)
        if tag in ("td", "th"):
            self.cell = max(0, self.cell - 1)

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self.cell += 1
            self.out.append(" | ")
            return
        if tag == "table":
            self.in_table += 1
        if tag == "tr":
            self.cell = 0
        if tag in self.BLOCK and not (self.cell and tag != "tr"):
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.out.append(f"[{href}] ")

    def handle_data(self, data):
        if self.cell:
            self.out.append(re.sub(r"\s+", " ", data))
        elif data.strip() or not self.in_table:  # Einrückung zwischen Tabellen-Tags ignorieren
            self.out.append(data)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    p = _Text()
    p.feed(s)
    text = html.unescape("".join(p.out)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.splitlines()]
    lines = [l[2:] if l.startswith("| ") else l for l in lines]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_time(v: Any) -> datetime | None:
    """Sakai liefert Zeiten mal als Epoch-ms, mal als {epochSecond}, mal als ISO-String."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, dict):
            if "epochSecond" in v:
                return datetime.fromtimestamp(v["epochSecond"], tz=timezone.utc)
            if "time" in v:
                return datetime.fromtimestamp(v["time"] / 1000, tz=timezone.utc)
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
        if isinstance(v, str):
            if v.isdigit() and len(v) in (14, 17):  # Sakai-Zeitstring yyyyMMddHHmmss[SSS] in GMT
                return datetime.strptime(v[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            if v.isdigit():
                return parse_time(int(v))
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (ValueError, OSError, TypeError, OverflowError):
        return None
    return None


def local(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%a %d.%m.%Y %H:%M") if dt else "–"


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", s).strip().strip(".")
    return s[:120] or "unbenannt"


_SEM_RE = re.compile(r"^(WS|SS)\s+\d{4}\s+")
_CODE_RE = re.compile(r"\s+[A-Z]{2,}-B-[\w+\-]+$")


def course_info(title: str) -> tuple[int | None, str]:
    """'WS 2026 Biosignalverarbeitung MGST-B-3-BB-BS-ILV' -> (3, 'Biosignalverarbeitung')."""
    m = re.search(r"-B-(\d)", title or "")
    sem = int(m.group(1)) if m and _SEM_RE.match(title) else None
    name = _CODE_RE.sub("", _SEM_RE.sub("", title or "")).strip()
    return sem, name or title
