from datetime import timezone
from pathlib import Path

import pytest

from mci import files

SITE = {"id": "abc-123", "title": "Mathe: Analysis/1", "url": "x"}
BASE = "https://sakai.mci4me.at/access/content/group/abc-123/"


def item(path, type_="application/pdf", size=10, **kw):
    d = {"url": BASE + path, "type": type_, "size": size, "title": path.rstrip("/").rsplit("/", 1)[-1],
         "modifiedDate": "20241012093000123", "visible": True, "hidden": False}
    d.update(kw)
    return d


FLAT = {"content_collection": [
    item("", "collection", title="Mathe"),
    item("Folien%20%C3%9Cbung/", "collection"),
    item("Folien%20%C3%9Cbung/VL%201%3A%20Intro.pdf", size=5),
    item("skript.pdf", size=7),
    item("Moodle-Link", "text/url", size=0, title="Zoom", webLinkUrl="https://zoom.us/j/1"),
    item("geheim.pdf", hidden=True, visible=False),
]}

NESTED = [{
    "url": BASE, "type": "collection", "title": "root",
    "resourceChildren": [
        {"url": BASE + "A/", "type": "collection", "title": "A",
         "resourceChildren": [item("A/B/tief.txt", "text/plain", 3)]},
        item("oben.pdf", size=4),
    ],
}]


class FakeClient:
    def __init__(self, listing):
        self.listing = listing
        self.downloads = []

    def get(self, path, **params):
        assert "abc-123" in path
        return self.listing

    def download(self, url, dest: Path) -> Path:
        self.downloads.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 5)
        return dest

    def sites(self):
        return [SITE]


@pytest.fixture(autouse=True)
def tmp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(files, "files_dir", lambda: tmp_path / "Sakai")
    return tmp_path


def test_flat_listing_decoding_and_links():
    fs = files.list_files(FakeClient(FLAT), SITE)
    rels = sorted(f["rel_path"] for f in fs)
    assert rels == ["Folien Übung/VL 1_ Intro.pdf", "skript.pdf"]
    vl = next(f for f in fs if f["rel_path"].startswith("Folien"))
    assert vl["size"] == 5
    assert vl["modified"].tzinfo == timezone.utc and vl["modified"].year == 2024


def test_nested_listing():
    rels = sorted(f["rel_path"] for f in files.list_files(FakeClient(NESTED), SITE))
    assert rels == ["A/B/tief.txt", "oben.pdf"]


def test_fallback_to_title():
    listing = [{"url": "https://other/x?id=1", "type": "application/pdf", "title": "a/b.pdf",
                "container": "/content/group/abc-123/Ordner/"}]
    assert files.list_files(FakeClient(listing), SITE)[0]["rel_path"] == "Ordner/a_b.pdf"


def test_sync_writes_links_and_manifest(tmp_env):
    c = FakeClient(FLAT)
    r = files.sync_site(c, SITE)
    cdir = Path(r["dir"])
    assert cdir.name == "Mathe_ Analysis_1"
    assert sorted(r["downloaded"]) == ["Folien Übung/VL 1_ Intro.pdf", "skript.pdf"]
    assert not r["failed"]
    assert "https://zoom.us/j/1" in (cdir / "_links.md").read_text()
    assert (cdir / "Folien Übung" / "VL 1_ Intro.pdf").exists()
    assert not list(cdir.rglob("*.part"))
    # zweiter Lauf: nichts Neues
    n = len(c.downloads)
    (cdir / "meine_notiz.md").write_text("eigene Notiz")
    r2 = files.sync_site(c, SITE)
    assert r2["downloaded"] == [] and r2["skipped"] == 2
    # geänderter modifiedDate -> neu laden
    FLAT["content_collection"][3]["modifiedDate"] = "20250101000000000"
    r3 = files.sync_site(c, SITE)
    assert "skript.pdf" in r3["downloaded"]
    assert (cdir / "meine_notiz.md").exists()
    assert len(c.downloads) > n


def test_single_error_does_not_crash():
    class Bad(FakeClient):
        def download(self, url, dest):
            if "skript" in url:
                raise OSError("boom")
            return super().download(url, dest)

    r = files.sync_site(Bad(NESTED + [item("skript.pdf")]), SITE)
    assert [x[0] for x in r["failed"]] == ["skript.pdf"]
    assert len(r["downloaded"]) == 2


def test_dry_run_and_sync_all():
    c = FakeClient(NESTED)
    out = files.sync_all(c, dry_run=True)
    assert len(out) == 1 and len(out[0]["downloaded"]) == 2 and c.downloads == []
