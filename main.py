import os
import requests
import logging
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query
from typing import Optional

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ViaggiaTreno AI Proxy")

CATEGORIES = {
    "FR": "Frecciarossa", "REG": "Regionale", "IC": "InterCity",
    "EC": "Eurocity", "EC FR": "Eurocity Frecciarossa", "FA": "Frecciargento",
}

STATUSES = {
    "/vt_static/img/legenda/icone_legenda/cancellazione.png": "CANCELLED",
    "/vt_static/img/legenda/icone_legenda/regolare.png": "ON_TIME",
    "/vt_static/img/legenda/icone_legenda/nonpartito.png": "NOT_YET_DEPARTED",
    "/vt_static/img/legenda/icone_legenda/ritardo03.png": "DELAYED",
}

BASE_URL = "http://www.viaggiatreno.it/infomobilita/resteasy/viaggiatreno"

def _format_time(timestamp_ms: Optional[int]) -> str:
    if not timestamp_ms:
        return "N/A"
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    # Italian time is UTC+1 (or +2 in summer). Simplified logic:
    return f"{str(dt.hour + 1).zfill(2)}:{str(dt.minute).zfill(2)}"

@app.get("/get-train-status")
async def get_train_status(number: str = Query(..., description="The train number, e.g., 9604")):
    """
    Retrieves real-time information for a specific train number.
    Used by the AI Agent to answer questions about delays and arrivals.
    """
    logger.info(f"AI Agent requesting status for train: {number}")
    
    try:
        # 1. Autocomplete to find station code and UID
        search_url = f"{BASE_URL}/cercaNumeroTrenoTrenoAutocomplete/{number}"
        auto_resp = requests.get(search_url, timeout=10)
        auto_resp.raise_for_status()
        
        if not auto_resp.text.strip():
            raise HTTPException(status_code=404, detail=f"Train {number} not found.")

        # Data format: "9604 - MILANO CENTRALE|9604-S01700-1709334000000"
        parts = auto_resp.text.split("|")
        train_info = parts[1].split("-") # [ID, StationCode, Timestamp]

        # 2. Get real-time status
        status_url = f"{BASE_URL}/andamentoTreno/{train_info[1]}/{train_info[0]}/{train_info[2]}"
        resp = requests.get(status_url, timeout=10)
        resp.raise_for_status()
        tr = resp.json()

        # 3. Clean and Simplify for AI Narration
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
                    "platform": s.get("binarioEffettivoArrivoDescrizione") or s.get("binarioProgrammatoArrivoDescrizione")
                } for s in stops[:5] # Limit stops so AI doesn't get overwhelmed
            ]
        }

    except Exception as e:
        logger.error(f"Error fetching train data: {e}")
        raise HTTPException(status_code=500, detail="The train service is temporarily unreachable.")

if __name__ == "__main__":
    import uvicorn
    # Use PORT from environment or default to 8000
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)