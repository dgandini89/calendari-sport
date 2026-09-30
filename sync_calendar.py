"""
Sincronizza il calendario di Serie A (football-data.org) con Google Calendar.

Pensato per girare su GitHub Actions (automatico + avvio manuale da telefono),
ma funziona anche in locale.

Logica di sync (invece di cancellare e ricreare tutto ogni volta):
  - ogni evento è legato alla partita tramite il suo ID (extendedProperties)
  - partita nuova            -> evento creato
  - orario/risultato cambiato -> evento aggiornato
  - partita sparita dall'API -> evento cancellato
  - nulla cambiato           -> evento lasciato stare (niente notifiche inutili)
Con RESET=true cancella tutti gli eventi gestiti e li ricrea da zero.

Variabili d'ambiente:
  FOOTBALL_API_KEY   chiave football-data.org                       (obbligatoria)
  GOOGLE_TOKEN_JSON  contenuto di token.json (vedi genera_token.py)  (oppure file token.json)
  RESET              "true" per ripartire da zero                    (opzionale)
  SEASON             anno di inizio stagione, es. 2026               (opzionale, auto)
  CALENDAR_NAME      nome del calendario                             (opzionale, auto)
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ─── CONFIG ───────────────────────────────────────────────
TEAM_NAME = "FC Internazionale Milano"   # la tua squadra
# Colori evento Google Calendar (colorId):
# 1 Lavanda  2 Salvia  3 Uva  4 Fenicottero  5 Banana
# 6 Mandarino  7 Pavone  8 Grafite  9 Mirtillo  10 Basilico  11 Pomodoro
TEAM_COLOR_ID = "9"                      # Mirtillo (blu)
OTHER_COLOR_ID = None                    # None = colore del calendario
TEAM_REMINDERS_MIN = [60 * 24, 60 * 2]   # promemoria: 1 giorno e 2 ore prima
MATCH_DURATION = timedelta(hours=2)
CALENDAR_COLOR = "#33B679"               # usato solo se il calendario viene creato
# ──────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/calendar"]
COMPETITION = "SA"
SOURCE_TAG = "football-data-sync"
WRITE_PAUSE = 0.15  # secondi tra una scrittura e l'altra (evita rate limit Google)

STATUS_IT = {
    "SCHEDULED": "Orario da confermare",
    "TIMED": "Orario confermato",
    "IN_PLAY": "In corso",
    "PAUSED": "Intervallo",
    "FINISHED": "Terminata",
    "POSTPONED": "Rinviata",
    "SUSPENDED": "Sospesa",
    "CANCELLED": "Annullata",
    "AWARDED": "Assegnata a tavolino",
}


def current_season() -> int:
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


SEASON = int(os.environ.get("SEASON") or current_season())
CALENDAR_NAME = os.environ.get("CALENDAR_NAME") or f"Serie A {SEASON}-{str(SEASON + 1)[-2:]}"
RESET = os.environ.get("RESET", "").strip().lower() in ("1", "true", "yes", "si", "sì")


# ─── football-data.org ────────────────────────────────────
def get_fixtures():
    api_key = os.environ.get("FOOTBALL_API_KEY")
    if not api_key:
        sys.exit("FOOTBALL_API_KEY mancante")
    url = f"https://api.football-data.org/v4/competitions/{COMPETITION}/matches"
    for attempt in range(4):
        r = requests.get(url, headers={"X-Auth-Token": api_key},
                         params={"season": SEASON}, timeout=30)
        if r.status_code == 429:  # rate limit free tier
            time.sleep(15 * (attempt + 1))
            continue
        if r.status_code in (400, 403, 404):
            sys.exit(f"Errore {r.status_code} da football-data.org: "
                     f"{r.json().get('message', r.text)}")
        r.raise_for_status()
        return r.json()["matches"]
    sys.exit("football-data.org: troppe richieste, riprova più tardi")


# ─── Google ───────────────────────────────────────────────
def google_service():
    raw = os.environ.get("GOOGLE_TOKEN_JSON")
    if raw:
        info = json.loads(raw)
    elif os.path.exists("token.json"):
        with open("token.json", encoding="utf-8") as f:
            info = json.load(f)
    else:
        sys.exit("Credenziali Google mancanti: imposta GOOGLE_TOKEN_JSON o crea token.json "
                 "con genera_token.py")
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_or_create_calendar(service):
    page_token = None
    while True:
        res = service.calendarList().list(pageToken=page_token).execute(num_retries=3)
        for cal in res.get("items", []):
            if cal.get("summary") == CALENDAR_NAME:
                return cal["id"]
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    cal = service.calendars().insert(
        body={"summary": CALENDAR_NAME, "timeZone": "Europe/Rome"}
    ).execute(num_retries=3)
    service.calendarList().patch(
        calendarId=cal["id"],
        colorRgbFormat=True,
        body={"backgroundColor": CALENDAR_COLOR, "foregroundColor": "#ffffff"},
    ).execute(num_retries=3)
    print(f"Calendario creato: {CALENDAR_NAME}")
    return cal["id"]


def list_events(service, cal_id):
    events, page_token = [], None
    while True:
        res = service.events().list(
            calendarId=cal_id, maxResults=2500, singleEvents=True,
            showDeleted=False, pageToken=page_token,
        ).execute(num_retries=3)
        events.extend(res.get("items", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            return events


def safe_delete(service, cal_id, event_id):
    try:
        service.events().delete(calendarId=cal_id, eventId=event_id).execute(num_retries=3)
    except HttpError as e:
        if e.resp.status not in (404, 410):  # già cancellato
            raise
    time.sleep(WRITE_PAUSE)


# ─── Partita -> evento ────────────────────────────────────
def team_label(team):
    return team.get("shortName") or team.get("name") or "?"


def is_my_team(match):
    names = " ".join(filter(None, [
        match["homeTeam"].get("name"), match["awayTeam"].get("name"),
    ])).lower()
    return TEAM_NAME.lower() in names


def build_event(match):
    home, away = team_label(match["homeTeam"]), team_label(match["awayTeam"])
    status = match.get("status", "")
    start = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
    end = start + MATCH_DURATION
    ft = (match.get("score") or {}).get("fullTime") or {}
    has_score = ft.get("home") is not None and ft.get("away") is not None

    if status in ("FINISHED", "AWARDED") and has_score:
        title = f"⚽ {home} {ft['home']}-{ft['away']} {away}"
    elif status in ("IN_PLAY", "PAUSED") and has_score:
        title = f"🔴 {home} {ft['home']}-{ft['away']} {away}"
    elif status == "SCHEDULED":
        title = f"🕒 {home} - {away}"
    elif status == "POSTPONED":
        title = f"⏸ {home} - {away} (rinviata)"
    elif status in ("CANCELLED", "SUSPENDED"):
        title = f"❌ {home} - {away} ({STATUS_IT[status].lower()})"
    else:
        title = f"⚽ {home} - {away}"

    desc = [f"Serie A {SEASON}/{SEASON + 1} — Giornata {match.get('matchday', '?')}",
            f"Stato: {STATUS_IT.get(status, status)}"]
    if status == "SCHEDULED":
        desc.append("L'orario è provvisorio: verrà aggiornato in automatico.")
    if match.get("venue"):
        desc.append(f"Stadio: {match['venue']}")

    mine = is_my_team(match)
    event = {
        "summary": title,
        "description": "\n".join(desc),
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Rome"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Rome"},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in TEAM_REMINDERS_MIN]
            if mine and start > datetime.now(timezone.utc) else [],
        },
        "transparency": "transparent",  # non ti segna "occupato"
    }
    color = TEAM_COLOR_ID if mine else OTHER_COLOR_ID
    if color:
        event["colorId"] = color

    digest = hashlib.sha1(json.dumps(event, sort_keys=True).encode()).hexdigest()
    event["extendedProperties"] = {"private": {
        "matchId": str(match["id"]), "hash": digest, "source": SOURCE_TAG,
    }}
    return event, digest, mine, start


# ─── Sync ─────────────────────────────────────────────────
def main():
    print(f"Stagione {SEASON} — calendario '{CALENDAR_NAME}'"
          + (" — MODALITÀ RESET" if RESET else ""))

    matches = [m for m in get_fixtures() if m.get("utcDate")]
    print(f"  → {len(matches)} partite da football-data.org")
    if not matches:
        sys.exit("Nessuna partita ricevuta: non tocco il calendario.")

    service = google_service()
    cal_id = get_or_create_calendar(service)
    existing = list_events(service, cal_id)

    by_id, to_delete = {}, []
    for ev in existing:
        mid = ev.get("extendedProperties", {}).get("private", {}).get("matchId")
        if mid:
            if RESET or mid in by_id:   # reset oppure duplicato
                to_delete.append(ev)
            else:
                by_id[mid] = ev
        elif ev.get("summary", "").startswith("⚽"):
            to_delete.append(ev)        # eventi del vecchio script senza ID
        # altri eventi aggiunti a mano: non li tocco

    stats = {"creati": 0, "aggiornati": 0, "invariati": 0, "cancellati": 0}
    next_mine = None

    for m in matches:
        event, digest, mine, start = build_event(m)
        if mine and start > datetime.now(timezone.utc) and (next_mine is None or start < next_mine[1]):
            next_mine = (event["summary"], start)

        current = by_id.pop(str(m["id"]), None)
        if current is None:
            service.events().insert(calendarId=cal_id, body=event).execute(num_retries=5)
            stats["creati"] += 1
            print(f"  + {event['summary']} ({start:%d/%m %H:%M} UTC)")
        elif current.get("extendedProperties", {}).get("private", {}).get("hash") != digest:
            service.events().update(calendarId=cal_id, eventId=current["id"],
                                    body=event).execute(num_retries=5)
            stats["aggiornati"] += 1
            print(f"  ~ {event['summary']} ({start:%d/%m %H:%M} UTC)")
        else:
            stats["invariati"] += 1
            continue
        time.sleep(WRITE_PAUSE)

    # eventi rimasti = partite non più presenti nell'API
    to_delete.extend(by_id.values())
    for ev in to_delete:
        safe_delete(service, cal_id, ev["id"])
        stats["cancellati"] += 1

    report = "\n".join(f"- **{k.capitalize()}**: {v}" for k, v in stats.items())
    if next_mine:
        local = next_mine[1].astimezone(ROME)
        giorno = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"][local.weekday()]
        report += (f"\n\n**Prossima partita:** {next_mine[0]} — "
                   f"{giorno} {local:%d/%m ore %H:%M}")
    print("\n" + report.replace("**", ""))

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"## Calendario {CALENDAR_NAME}\n\n{report}\n")


try:
    from zoneinfo import ZoneInfo
    ROME = ZoneInfo("Europe/Rome")
except Exception:  # pragma: no cover
    ROME = timezone.utc

if __name__ == "__main__":
    main()
