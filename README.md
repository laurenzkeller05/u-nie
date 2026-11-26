# mci

Lokales CLI für Sakai@MCI und myMCI: Kurse, Deadlines, Assignments, Kursdateien, Ankündigungen, Noten, Stundenplan. Läuft nur lokal und nur auf Aufruf.

## Setup

```sh
uv sync
cp .env.example .env     # dann .env selbst ausfüllen (MCI_USER, MCI_PASS, optional MCI_UNI_DIR)
uv run mci status        # prüft Login bei Sakai und myMCI
```

`.env` und `data/` sind gitignored. Zugangsdaten nie committen oder weitergeben.

Kursdateien landen in `MCI_UNI_DIR/<n>_Semester/Sakai/<Kurs>/` (Default-Wurzel `~/Me/Uni`, Semester aus dem Kurscode; allgemeine Sites unter `Sakai_Allgemein/`; `sync-files` lädt nur das aktuelle Semester, `--all` alle), Assignment-Material in `<Kurs>/_Assignments/<Titel>/`.

## Befehle

Alle Befehle kennen `--json`.

| Befehl | Zweck |
|---|---|
| `mci status` | Login-Check Sakai + myMCI |
| `mci courses` | Sakai-Kurse |
| `mci deadlines [--days 30]` | Assignments, Quizzes, Kalender, Prüfungen nach Datum |
| `mci assignments [--all] [--course TEXT]` | Assignments auflisten |
| `mci assignment <id> [--download]` | Details; `--download` speichert `aufgabe.md` + Anhänge |
| `mci sync-files [--course TEXT] [--dry-run]` | Kursdateien spiegeln |
| `mci announcements [--days 14]` | Ankündigungen |
| `mci grades` | Noten (Sakai + myMCI) |
| `mci termine [--days 14]` | Stundenplan (myMCI) |
| `mci raw sakai\|mymci <path>` | Roh-JSON |

Aufruf jeweils mit `uv run mci ...` aus dem Repo.

## Privater Bereich `workspace/`
`workspace/` ist in `.gitignore` – dort (bzw. im verlinkten Uni-Ordner `workspace/uni`) liegen Kursdateien, Assignment-Lösungen und Abgaben. Nichts davon landet im Repo.
