# API-Recherche Sakai@MCI & myMCI (Stand 2026-11-26, ohne Login)

Alles aus öffentlichen Seiten/JS-Bundles. Nach dem ersten echten Login verifizieren.

## Sakai@MCI
- URL: https://sakai.mci4me.at — Sakai 25.x, direktes Login (kein SSO)
- Login Skript: `POST /direct/session` (`_username`, `_password`) oder `POST /api/login` (`username`, `password`) → Cookie `JSESSIONID`
- Session prüfen: `GET /direct/session/current.json` (anonym 1 h Inaktivität)
- Endpunkt-Doku der Instanz: `/direct/describe`
- Sites: `/direct/site.json`, `/api/users/me/sites` (unsicher)
- Assignments: `/direct/assignment/my.json`, `/direct/assignment/site/{siteId}.json`, `/direct/assignment/item/{id}.json`
  - Felder: `title`, `instructions`, `attachments`, `openTime`, `dueTime`, `closeTime`, `dropDeadTime`, `submissions`, `allowResubmission`
- Ressourcen: `/direct/content/site/{siteId}.json` (Download über `url` + Cookie)
- Ankündigungen: `/direct/announcement/user.json?n=50&d=365`, `/api/users/me/announcements`
- Kalender: `/direct/calendar/my.json`, `/api/users/current/calendar`
- Noten: `/api/users/me/grades`, `/api/sites/{siteId}/grades`
- Tasks: `/api/users/me/tasks`
- Quizzes: `/direct/sam_pub/context/{siteId}.json` (unsicher, ob für Studierende befüllt)
- Abgabe: kein API-Endpunkt, nur Web-Formular (Multipart + `sakai_csrf_token`)
- Vorlage: https://github.com/jtayped/sakai-mcp (Python, MIT, Sakai 25)

## myMCI
- Frontend: https://my.mci4me.at (MCI-Eigenbau, React/Ionic)
- API-Basis: `https://callmyapi.mci4me.at/api/my/5`
- Login: `POST /auth/credentials?lang=de` `{"username":"xx1234@mci4me.at","password":"…","deviceFingerprint":"…"}` → JWT `auth_token` + `refresh_token`
- Header: `Authorization: Bearer …`, `Software-Version` (aus `GET /versioninfo`), `X-Platform: desktop`
- Refresh: `POST /token/refresh`; 429 mit `Retry-After` respektieren
- Termine: `/termine`, `/mcitermine` (mit `sakailink`, `erlaubtehilfsmittel`), `/termin/{id}`
- iCal: `GET /ical/link` → webcal-URL, Feed ohne Auth, Update nur alle 12 h
- Noten: `/beurteilungen/{id}`, `/beurteilung/{id}` (id vermutlich `personenkennzeichen`)
- Studienplan: `/studienplan/{id}`; Dokumente: `/dokumente`; Notifikationen: `/notifikationen`
- Anwesenheit: keine API/Daten (Route ausgeblendet)
- Vorlage: https://github.com/MrP123/Lecture_Calendar_Fixer

## Zugangsdaten
Nur im macOS-Schlüsselbund (`keyring`) oder lokaler `.env` (gitignored). Nie in Chat oder Code.
