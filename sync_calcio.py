"""
Sincronizza il calendario di Serie A / Champions League (football-data.org)
con Google Calendar.

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
  COMPETITION        SA = Serie A (default), CL = Champions League  (opzionale)
  ONLY_MY_TEAM       "true" = solo le partite della tua squadra      (opzionale)
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
TEAM_COLOR_ID = "6"                      # Mandarino (arancione)
OTHER_COLOR_ID = None                    # None = colore del calendario
TEAM_REMINDERS = [(1, "12:00"), 5]       # promemoria: giorno prima alle 12:00 e 5 minuti prima
# formato: numero = minuti prima; (giorni, "HH:MM") = N giorni prima a quell'ora
MATCH_DURATION = timedelta(hours=2)
# colore del calendario, usato solo quando viene creato
CALENDAR_COLORS = {"SA": "#33B679", "CL": "#3F51B5"}   # Serie A verde, Champions blu

# Squadre di interesse: salva solo le partite che coinvolgono almeno una di queste.
# TEAM_NOTIFY è già inclusa automaticamente, non serve riscriverla qui.
# Lascia la lista vuota [] per salvare TUTTE le partite.
TEAMS_OF_INTEREST = [
    "FC Internazionale Milano",
    "AC Milan",
    "Juventus FC",
    "AS Roma",
    "SS Lazio",
    "SSC Napoli",
    "Como 1907",
    "Atalanta BC",
    # aggiungi o rimuovi squadre qui
]
# ──────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/calendar"]
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

STAGE_IT = {
    "LEAGUE_STAGE": "Fase campionato", "PLAYOFFS": "Spareggi", "LAST_16": "Ottavi di finale",
    "QUARTER_FINALS": "Quarti di finale", "SEMI_FINALS": "Semifinali", "FINAL": "Finale",
}
COMPETITION_NAMES = {"SA": "Serie A", "CL": "Champions League"}


def current_season() -> int:
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def env_true(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "si", "sì")


COMPETITION = os.environ.get("COMPETITION", "SA").strip().upper()
COMP_NAME = COMPETITION_NAMES.get(COMPETITION, COMPETITION)
ONLY_MY_TEAM = env_true("ONLY_MY_TEAM")
SEASON = int(os.environ.get("SEASON") or current_season())
CALENDAR_NAME = (os.environ.get("CALENDAR_NAME")
                 or f"{COMP_NAME} {SEASON}-{str(SEASON + 1)[-2:]}")
RESET = env_true("RESET")

try:
    from zoneinfo import ZoneInfo
    ROME = ZoneInfo("Europe/Rome")
except Exception:  # pragma: no cover
    ROME = timezone.utc


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
        matches = r.json()["matches"]
        
        # Filtra per squadre di interesse (se la lista non è vuota)
        if TEAMS_OF_INTEREST:
            teams_lower = [t.lower() for t in TEAMS_OF_INTEREST]
            matches = [
                m for m in matches
                if any(t in m["homeTeam"]["name"].lower() for t in teams_lower)
                or any(t in m["awayTeam"]["name"].lower() for t in teams_lower)
            ]

        return matches
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
        body={"backgroundColor": CALENDAR_COLORS.get(COMPETITION, "#33B679"), "foregroundColor": "#ffffff"},
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


def reminder_overrides(start, specs):
    """Converte i promemoria in minuti prima dell'inizio (come vuole Google).
    specs: numeri = minuti prima; (giorni, "HH:MM") = N giorni prima a quell'ora."""
    if start <= datetime.now(timezone.utc):
        return []
    local = start.astimezone(ROME)
    out = set()
    for spec in specs:
        if isinstance(spec, (tuple, list)):
            days, hhmm = spec
            h, m = map(int, hhmm.split(":"))
            at = (local - timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0)
            # confronto in UTC: gestisce correttamente il cambio ora legale/solare
            minutes = int((start.astimezone(timezone.utc) - at.astimezone(timezone.utc)).total_seconds() // 60)
        else:
            minutes = int(spec)
        if 0 <= minutes <= 40320:  # limite Google: 4 settimane
            out.add(minutes)
    return [{"method": "popup", "minutes": mn} for mn in sorted(out, reverse=True)][:5]


# ─── Partita -> evento ────────────────────────────────────
def team_label(team):
    return team.get("shortName") or team.get("name") or "Da definire"


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

    stage = match.get("stage", "")
    if stage in ("REGULAR_SEASON", "LEAGUE_STAGE") and match.get("matchday"):
        phase = f"Giornata {match['matchday']}"
    else:
        phase = STAGE_IT.get(stage, stage.replace("_", " ").title() or "?")
    desc = [f"{COMP_NAME} {SEASON}/{SEASON + 1} — {phase}",
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
            "overrides": reminder_overrides(start, TEAM_REMINDERS) if mine else [],
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
    print(f"{COMP_NAME} {SEASON} — calendario '{CALENDAR_NAME}'"
          + (" — MODALITÀ RESET" if RESET else ""))

    matches = [m for m in get_fixtures() if m.get("utcDate")]
    print(f"  → {len(matches)} partite da football-data.org")
    if not matches:
        sys.exit("Nessuna partita ricevuta: non tocco il calendario.")
    if ONLY_MY_TEAM:
        matches = [m for m in matches if is_my_team(m)]
        print(f"  → {len(matches)} partite di {TEAM_NAME}")

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


if __name__ == "__main__":
    main()