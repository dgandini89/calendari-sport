"""
Sincronizza il calendario di Formula 1 (Jolpica / ex Ergast) con Google Calendar.
Stessa logica di sync_calendar.py (Serie A):
  - sessione nuova             -> evento creato
  - orario cambiato / vincitore -> evento aggiornato
  - sessione sparita dall'API  -> evento cancellato
  - nulla cambiato             -> evento lasciato stare
Con RESET=true cancella tutti gli eventi gestiti e li ricrea da zero.

Variabili d'ambiente:
  GOOGLE_TOKEN_JSON  contenuto di token.json (stesso secret della Serie A)
  RESET              "true" per ripartire da zero        (opzionale)
  SEASON             anno della stagione, es. 2026       (opzionale, auto)
  CALENDAR_NAME      nome del calendario                 (opzionale, auto)
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
# Quali sessioni creare nel calendario
CREATE_SESSIONS = {
    "Race":             True,   # 🏁 Gara
    "Qualifying":       False,  # ⏱ Qualifiche
    "Sprint":           True,   # 💨 Sprint
    "SprintQualifying": False,  # ⏱ Qualifiche Sprint
    "FP1":              False,  # 🔧 Prove libere 1
    "FP2":              False,  # 🔧 Prove libere 2
    "FP3":              False,  # 🔧 Prove libere 3
}

SESSION_LABEL = {
    "Race": "Gara", "Qualifying": "Qualifiche", "Sprint": "Sprint",
    "SprintQualifying": "Qualifiche Sprint",
    "FP1": "Libere 1", "FP2": "Libere 2", "FP3": "Libere 3",
}
SESSION_EMOJI = {
    "Race": "🏁", "Qualifying": "⏱", "Sprint": "💨", "SprintQualifying": "⏱",
    "FP1": "🔧", "FP2": "🔧", "FP3": "🔧",
}
SESSION_DURATION_MIN = {
    "Race": 120, "Qualifying": 60, "Sprint": 45, "SprintQualifying": 45,
    "FP1": 60, "FP2": 60, "FP3": 60,
}
# Colori evento (colorId), None = colore del calendario
# 1 Lavanda  2 Salvia  3 Uva  4 Fenicottero  5 Banana
# 6 Mandarino  7 Pavone  8 Grafite  9 Mirtillo  10 Basilico  11 Pomodoro
SESSION_COLOR = {"Race": None, "Sprint": None}
# Promemoria in minuti prima dell'inizio, es. {"Race": [60]}; vuoto = nessuno
SESSION_REMINDERS_MIN = {}
SHOW_WINNER = True   # aggiunge il vincitore alle gare concluse
# ──────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/calendar"]
JOLPICA_BASE = "https://api.jolpi.ca/ergast/f1"
SOURCE_TAG = "jolpica-f1-sync"
WRITE_PAUSE = 0.15
DEFAULT_TIME = "13:00:00Z"   # usato se l'API non ha ancora l'orario

# nome sessione -> chiave nel JSON di Jolpica
API_KEYS = {
    "FP1": "FirstPractice", "FP2": "SecondPractice", "FP3": "ThirdPractice",
    "Qualifying": "Qualifying", "Sprint": "Sprint",
    "SprintQualifying": "SprintQualifying", "Race": None,
}

SEASON = int(os.environ.get("SEASON") or datetime.now(timezone.utc).year)
CALENDAR_NAME = os.environ.get("CALENDAR_NAME") or f"Formula 1 {SEASON}"
RESET = os.environ.get("RESET", "").strip().lower() in ("1", "true", "yes", "si", "sì")

try:
    from zoneinfo import ZoneInfo
    ROME = ZoneInfo("Europe/Rome")
except Exception:  # pragma: no cover
    ROME = timezone.utc


# ─── Jolpica ──────────────────────────────────────────────
def jolpica(path):
    for attempt in range(4):
        r = requests.get(f"{JOLPICA_BASE}/{path}", timeout=30)
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()["MRData"]
    sys.exit("Jolpica: troppe richieste, riprova più tardi")


def get_races():
    return jolpica(f"{SEASON}.json?limit=100")["RaceTable"]["Races"]


def get_winners():
    """round -> 'Nome Cognome (Team)' per le gare già concluse."""
    if not SHOW_WINNER:
        return {}
    try:
        races = jolpica(f"{SEASON}/results/1.json?limit=100")["RaceTable"]["Races"]
    except Exception as e:  # il vincitore è un extra: se fallisce, si va avanti
        print(f"  (vincitori non disponibili: {e})")
        return {}
    out = {}
    for race in races:
        res = (race.get("Results") or [None])[0]
        if res:
            d = res["Driver"]
            out[race["round"]] = f"{d['givenName']} {d['familyName']} ({res['Constructor']['name']})"
    return out


def parse_dt(date_str, time_str):
    if not date_str:
        return None, False
    confirmed = bool(time_str)
    dt = datetime.fromisoformat(f"{date_str}T{time_str or DEFAULT_TIME}".replace("Z", "+00:00"))
    return dt, confirmed


def extract_sessions(race):
    sessions = []
    for name, key in API_KEYS.items():
        if not CREATE_SESSIONS.get(name):
            continue
        block = race if name == "Race" else race.get(key)
        if not block:
            continue
        dt, confirmed = parse_dt(block.get("date"), block.get("time"))
        if dt:
            sessions.append((name, dt, confirmed))
    return sessions


# ─── Google ───────────────────────────────────────────────
def google_service():
    raw = os.environ.get("GOOGLE_TOKEN_JSON")
    if raw:
        info = json.loads(raw)
    elif os.path.exists("token.json"):
        with open("token.json", encoding="utf-8") as f:
            info = json.load(f)
    else:
        sys.exit("Credenziali Google mancanti: imposta GOOGLE_TOKEN_JSON o crea token.json")
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
        if e.resp.status not in (404, 410):
            raise
    time.sleep(WRITE_PAUSE)


# ─── Sessione -> evento ───────────────────────────────────
def build_event(race, session, start, confirmed, winner):
    gp = race["raceName"].replace("Grand Prix", "GP")
    loc = race["Circuit"]["Location"]
    emoji = "🕒" if not confirmed else SESSION_EMOJI[session]
    title = f"{emoji} F1 {gp} | {SESSION_LABEL[session]}"
    if session == "Race" and winner:
        title += f" — 🏆 {winner.split(' (')[0]}"

    desc = [f"Round {race['round']} — {race['raceName']} {SEASON}",
            f"Sessione: {SESSION_LABEL[session]}",
            f"Circuito: {race['Circuit']['circuitName']}",
            f"Luogo: {loc['locality']}, {loc['country']}"]
    if not confirmed:
        desc.append("Orario provvisorio: verrà aggiornato in automatico.")
    if session == "Race" and winner:
        desc.append(f"Vincitore: {winner}")

    end = start + timedelta(minutes=SESSION_DURATION_MIN[session])
    reminders = SESSION_REMINDERS_MIN.get(session) or []
    event = {
        "summary": title,
        "description": "\n".join(desc),
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Rome"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Rome"},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in reminders]
            if start > datetime.now(timezone.utc) else [],
        },
        "transparency": "transparent",
    }
    if SESSION_COLOR.get(session):
        event["colorId"] = SESSION_COLOR[session]

    digest = hashlib.sha1(json.dumps(event, sort_keys=True).encode()).hexdigest()
    event["extendedProperties"] = {"private": {
        "sessionKey": f"{SEASON}-{race['round']}-{session}",
        "hash": digest, "source": SOURCE_TAG,
    }}
    return event, digest


def is_legacy(ev):
    """Eventi creati dal vecchio script (senza chiave)."""
    return "F1 –" in ev.get("summary", "")


# ─── Sync ─────────────────────────────────────────────────
def main():
    print(f"Stagione {SEASON} — calendario '{CALENDAR_NAME}'"
          + (" — MODALITÀ RESET" if RESET else ""))

    races = get_races()
    print(f"  → {len(races)} GP da Jolpica")
    if not races:
        sys.exit("Nessun GP ricevuto: non tocco il calendario.")
    winners = get_winners()

    service = google_service()
    cal_id = get_or_create_calendar(service)

    by_key, to_delete = {}, []
    for ev in list_events(service, cal_id):
        key = ev.get("extendedProperties", {}).get("private", {}).get("sessionKey")
        if key:
            if RESET or key in by_key:
                to_delete.append(ev)
            else:
                by_key[key] = ev
        elif is_legacy(ev):
            to_delete.append(ev)

    stats = {"creati": 0, "aggiornati": 0, "invariati": 0, "cancellati": 0}
    now = datetime.now(timezone.utc)
    next_race = None

    for race in races:
        for session, start, confirmed in extract_sessions(race):
            event, digest = build_event(race, session, start, confirmed,
                                        winners.get(race["round"]))
            if session == "Race" and start > now and (next_race is None or start < next_race[1]):
                next_race = (race["raceName"], start)

            key = event["extendedProperties"]["private"]["sessionKey"]
            current = by_key.pop(key, None)
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

    to_delete.extend(by_key.values())   # sessioni non più presenti o disattivate
    for ev in to_delete:
        safe_delete(service, cal_id, ev["id"])
        stats["cancellati"] += 1

    report = "\n".join(f"- **{k.capitalize()}**: {v}" for k, v in stats.items())
    if next_race:
        local = next_race[1].astimezone(ROME)
        giorno = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"][local.weekday()]
        report += (f"\n\n**Prossima gara:** {next_race[0]} — "
                   f"{giorno} {local:%d/%m ore %H:%M}")
    print("\n" + report.replace("**", ""))

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"## Calendario {CALENDAR_NAME}\n\n{report}\n")


if __name__ == "__main__":
    main()
