import os
import re
import requests
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Query

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="ViaggiaTreno AI Proxy",
    description=(
        "Real-time Italian train information powered by ViaggiaTreno. "
        "Use get-train-status to check a specific train by number. "
        "Use get-departures to see all trains leaving a station right now. "
        "Use search-trains to find trains between two stations on a given date or time. "
        "Station codes follow the ViaggiaTreno format (e.g. S01700 = Milano Centrale, S08409 = Roma Termini)."
    ),
)

BASE_URL = "http://www.viaggiatreno.it/infomobilita/resteasy/viaggiatreno"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

ITALY_TZ = timezone(timedelta(hours=1))

CATEGORIES = {
    "FR": "Frecciarossa", " FR": "Frecciarossa",
    "REG": "Regionale",
    "IC": "InterCity",
    "EC": "Eurocity", "EC FR": "Eurocity Frecciarossa",
    "FA": "Frecciargento", " FA": "Frecciargento",
}

STATUSES = {
    "/vt_static/img/legenda/icone_legenda/cancellazione.png": "CANCELLED",
    "/vt_static/img/legenda/icone_legenda/regolare.png": "ON_TIME",
    "/vt_static/img/legenda/icone_legenda/nonpartito.png": "NOT_YET_DEPARTED",
    "/vt_static/img/legenda/icone_legenda/ritardo03.png": "DELAYED",
}

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _format_time(timestamp_ms: Optional[int]) -> str:
    if not timestamp_ms:
        return "N/A"
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return f"{str(dt.hour + 1).zfill(2)}:{str(dt.minute).zfill(2)}"


def _now_italy() -> datetime:
    return datetime.now(ITALY_TZ)


def _api_timestamp(dt: datetime) -> str:
    return (
        f"{_WEEKDAYS[dt.weekday()]} {_MONTHS[dt.month - 1]} {dt.day:02d} {dt.year} "
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} GMT+0100 (Central European Standard Time)"
    )


def _parse_when(when: Optional[str]) -> tuple[datetime, bool]:
    now = _now_italy()
    if not when:
        return now, False

    parts = when.strip().split()
    base_date: Optional[datetime] = None
    time_str: Optional[str] = None

    for part in parts:
        lower = part.lower()
        if lower == "domani":
            base_date = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        elif lower == "dopodomani":
            base_date = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)
        elif "/" in part:
            date_parts = part.split("/")
            day, month = int(date_parts[0]), int(date_parts[1])
            year = int(date_parts[2]) if len(date_parts) > 2 else now.year
            base_date = datetime(year, month, day, 0, 0, 0, tzinfo=ITALY_TZ)
        elif ":" in part:
            time_str = part

    if base_date is None and time_str is None and ":" in when:
        time_str = when
        base_date = now

    if base_date is None:
        base_date = now

    if time_str:
        t = time_str.split(":")
        base_date = base_date.replace(hour=int(t[0]), minute=int(t[1]) if len(t) > 1 else 0, second=0, microsecond=0)
        return base_date, True

    if base_date.date() != now.date():
        base_date = base_date.replace(hour=0, minute=0, second=0, microsecond=0)

    return base_date, False


def _matches_station(station_name: str, search_term: str) -> bool:
    station = (station_name or "").upper()
    search = search_term.upper()
    if station == search or station.startswith(search + " "):
        return True
    return bool(re.search(r'\b' + re.escape(search) + r'\b', station))


def _fetch_train_details(session: requests.Session, train_number: int | str) -> Optional[dict]:
    resp = session.get(f"{BASE_URL}/cercaNumeroTrenoTrenoAutocomplete/{train_number}", timeout=10)
    if not resp.ok or not resp.text.strip():
        return None

    raw = resp.text.strip().split("\n")[0].split("|")[1]
    train_id, station_code, timestamp = raw.split("-")

    details = session.get(f"{BASE_URL}/andamentoTreno/{station_code}/{train_id}/{timestamp}", timeout=10)
    if not details.ok or details.status_code == 204:
        return None

    return details.json()


@app.get(
    "/get-train-status",
    summary="Get real-time status of a specific train",
    description=(
        "Use this when the user asks about a specific train by number — its current delay, where it is right now, "
        "what platform it departs from, and upcoming stops with scheduled vs actual times. "
        "Requires the numeric train number (e.g. 9663 for FR 9663). "
        "Returns: origin, destination, delay in minutes, current status (ON_TIME / DELAYED / CANCELLED / NOT_YET_DEPARTED), "
        "last detected position, and up to 5 stops with scheduled and actual times."
    ),
)
async def get_train_status(number: str = Query(..., description="Train number, e.g. 9604")):
    logger.info(f"Status request for train: {number}")
    try:
        session = requests.Session()
        session.headers.update(HEADERS)

        tr = _fetch_train_details(session, number)
        if tr is None:
            raise HTTPException(status_code=404, detail=f"Train {number} not found.")

        number_parts = tr.get("compNumeroTreno", "").strip().split(" ")
        stops = sorted(tr.get("fermate", []), key=lambda s: s.get("programmata", 0))

        return {
            "summary": f"Train {number} ({CATEGORIES.get(number_parts[0], 'Train')}) is currently {tr.get('ritardo')} minutes late.",
            "origin": tr.get("origine"),
            "destination": tr.get("destinazione"),
            "current_status": STATUSES.get(tr.get("compImgRitardo2", ""), "UNKNOWN"),
            "delay_minutes": tr.get("ritardo"),
            "last_detection": tr.get("stazioneUltimoRilevamento"),
            "stops": [
                {
                    "station": s["stazione"],
                    "scheduled": _format_time(s.get("programmata")),
                    "actual": _format_time(s.get("effettiva") or s.get("arrivo_teorico")),
                    "platform": s.get("binarioEffettivoArrivoDescrizione") or s.get("binarioProgrammatoArrivoDescrizione"),
                }
                for s in stops[:5]
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching train {number}: {e}")
        raise HTTPException(status_code=500, detail="The train service is temporarily unreachable.")


@app.get(
    "/get-departures",
    summary="Get current departures from a station",
    description=(
        "Use this when the user asks what trains are leaving from a given station right now. "
        "Requires the station code (e.g. S01700 for Milano Centrale). "
        "Returns a list of all imminent departures with: train number and category (Frecciarossa, Regionale, etc.), "
        "final destination, scheduled departure time, estimated departure time accounting for delay, "
        "delay in minutes, departure platform, and current status. "
        "Always call this before search-trains if the user only asks 'what trains leave from X'."
    ),
)
async def get_departures(station: str = Query(..., description="Station code, e.g. S01700")):
    logger.info(f"Departures request for station: {station}")
    try:
        session = requests.Session()
        session.headers.update(HEADERS)

        resp = session.get(f"{BASE_URL}/partenze/{station}/{_api_timestamp(_now_italy())}", timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if not data or not isinstance(data, list):
            return {"trains": []}

        trains = []
        for tr in data:
            departure_ms = tr.get("orarioPartenza") or 0
            delay_ms = (tr.get("ritardo") or 0) * 60 * 1000
            cat = (tr.get("categoriaDescrizione") or "").strip()
            trains.append({
                "number": f"{CATEGORIES.get(cat, cat)} {tr.get('numeroTreno', '')}".strip(),
                "destination": tr.get("destinazione", ""),
                "scheduled_time": tr.get("compOrarioPartenza", ""),
                "estimated_time": _format_time(departure_ms + delay_ms) if departure_ms else "N/A",
                "delay": tr.get("ritardo", 0),
                "platform": tr.get("binarioEffettivoPartenzaDescrizione") or tr.get("binarioProgrammatoPartenzaDescrizione", ""),
                "status": STATUSES.get(tr.get("compImgRitardo2", ""), "UNKNOWN"),
            })

        return {"trains": trains}
    except Exception as e:
        logger.error(f"Error fetching departures for {station}: {e}")
        raise HTTPException(status_code=500, detail="The train service is temporarily unreachable.")


@app.get(
    "/search-trains",
    summary="Search trains between two stations",
    description=(
        "Use this when the user wants to travel from station A to station B and asks which trains are available, "
        "what time they arrive, or whether there are options at a specific time or day. "
        "Requires: from_station as a station code (e.g. S01700), to_station as a plain name (e.g. ROMA or FIRENZE). "
        "The optional 'when' parameter accepts natural Italian or numeric formats: "
        "'domani' (tomorrow), 'dopodomani' (day after tomorrow), '15/03' or '15/03/2026' (specific date), "
        "'15:00' (specific time today), or combinations like 'domani 15:00' or '15/03 09:30'. "
        "Without 'when', or with a date only, it scans the full day in 4 time windows and deduplicates results. "
        "With a specific time, it returns only trains departing around that time. "
        "Each result includes: train number and category, departure time, arrival time at destination, "
        "current delay, departure platform, final destination, and whether the destination is an intermediate stop."
    ),
)
async def search_trains(
    from_station: str = Query(..., description="Departure station code, e.g. S01700"),
    to_station: str = Query(..., description="Destination station name, e.g. ROMA"),
    when: Optional[str] = Query(None, description="When: 'domani', '15/03', '15:00', 'domani 15:00'"),
):
    logger.info(f"Search trains {from_station} → {to_station}, when={when}")

    to_name = to_station.upper()

    def fetch_departures(query_date: datetime) -> list:
        resp = session.get(f"{BASE_URL}/partenze/{from_station}/{_api_timestamp(query_date)}", timeout=10)
        if not resp.ok:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []

    def process_trains(departures: list) -> list:
        results = []
        for train in departures:
            train_number = train.get("numeroTreno")
            final_dest = train.get("destinazione", "")
            cat = (train.get("categoriaDescrizione") or "").strip()
            category_name = CATEGORIES.get(cat, cat)
            dep_time = train.get("compOrarioPartenza", "")
            delay = train.get("ritardo", 0)
            platform = train.get("binarioEffettivoPartenzaDescrizione") or train.get("binarioProgrammatoPartenzaDescrizione", "")

            if _matches_station(final_dest, to_name):
                details = _fetch_train_details(session, train_number)
                results.append({
                    "number": f"{category_name} {train_number}".strip(),
                    "departure_time": dep_time,
                    "arrival_time": details.get("compOrarioArrivoZero") if details else None,
                    "delay": delay,
                    "platform": platform,
                    "final_destination": final_dest,
                    "is_intermediate_stop": False,
                })
                continue

            details = _fetch_train_details(session, train_number)
            if not details:
                continue

            found_origin = False
            for fermata in details.get("fermate", []):
                station_id = fermata.get("id", "")
                station_name = fermata.get("stazione", "")

                if not found_origin:
                    if from_station in station_id or _matches_station(station_name, from_station):
                        found_origin = True
                    continue

                if _matches_station(station_name, to_name):
                    arr_ts = fermata.get("arrivo_teorico")
                    results.append({
                        "number": f"{category_name} {train_number}".strip(),
                        "departure_time": dep_time,
                        "arrival_time": _format_time(arr_ts) if arr_ts else None,
                        "delay": fermata.get("ritardo") or delay,
                        "platform": platform,
                        "final_destination": final_dest,
                        "is_intermediate_stop": True,
                    })
                    break

        return results

    try:
        session = requests.Session()
        session.headers.update(HEADERS)

        parsed_date, has_specific_time = _parse_when(when)
        all_results: list = []

        if has_specific_time:
            all_results = process_trains(fetch_departures(parsed_date))
        else:
            seen: set[str] = set()
            for hour in [0, 6, 12, 18]:
                query_date = parsed_date.replace(hour=hour, minute=0, second=0, microsecond=0)
                for train in process_trains(fetch_departures(query_date)):
                    if train["number"] not in seen:
                        seen.add(train["number"])
                        all_results.append(train)

        all_results.sort(key=lambda t: t.get("departure_time") or "")
        return {"trains": all_results}

    except Exception as e:
        logger.error(f"Error searching trains {from_station}→{to_station}: {e}")
        raise HTTPException(status_code=500, detail="The train service is temporarily unreachable.")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
