import os
import httpx
import asyncio
from datetime import date, datetime
from typing import Any
from dataclasses import dataclass, field

# Configurações de Conexão
API_BASE    = "https://v3.football.api-sports.io"
API_KEY     = os.getenv("API_FOOTBALL_KEY", "")
TIMEZONE    = "America/Sao_Paulo"
TIMEOUT_S   = 15.0

# Ligas que o sistema vai monitorar
WATCHED_LEAGUES: dict[int, str] = {
    71: "brasileirao", 72: "brasileirao_b", 2: "champions",
    39: "premier", 140: "laliga", 135: "serie_a_it",
    78: "bundesliga", 61: "ligue1",
}

@dataclass
class RawTeamStats:
    team_id: int; name: str; logo: str; last5: list[str]
    avg_scored: float; avg_conceded: float
    home_strength: float = 1.0; away_strength: float = 1.0; form_score: float = 50.0

@dataclass
class RawAbsence:
    player_name: str; reason: str; importance: str

@dataclass
class RawOdds:
    home: float | None = None; draw: float | None = None; away: float | None = None

@dataclass
class RawMatch:
    fixture_id: int; league_id: int; league_name: str; league_logo: str; league_key: str
    home: RawTeamStats; away: RawTeamStats; kickoff_utc: datetime; status: str
    minute: int | None; home_score: int | None; away_score: int | None
    home_absences: list[RawAbsence] = field(default_factory=list)
    away_absences: list[RawAbsence] = field(default_factory=list)
    odds: RawOdds = field(default_factory=RawOdds)

def _headers() -> dict:
    return {"x-apisports-key": API_KEY, "Accept": "application/json"}

async def _get(client: httpx.AsyncClient, path: str, params: dict = {}) -> dict:
    url = f"{API_BASE}{path}"
    try:
        resp = await client.get(url, params=params, headers=_headers(), timeout=TIMEOUT_S)
        if resp.status_code != 200: return {"response": []}
        return resp.json()
    except Exception:
        return {"response": []}

def _parse_status(status_short: str) -> str:
    if status_short in {"1H","HT","2H","ET","BT","P","INT","LIVE"}: return "live"
    if status_short in {"FT","AET","PEN"}: return "finished"
    return "scheduled"

def _form_to_list(form_str: str | None) -> list[str]:
    return list((form_str or "").upper())[-5:]

async def fetch_team_last10(client: httpx.AsyncClient, team_id: int, league_id: int) -> list[dict]:
    # Tenta buscar na temporada 2026 e depois 2025 para garantir dados
    for season in [2026, 2025]:
        data = await _get(client, "/fixtures", {"team": team_id, "league": league_id, "season": season, "status": "FT"})
        res = data.get("response", [])
        if res: return res[-10:]
    return []

async def fetch_todays_fixtures(client: httpx.AsyncClient) -> list[dict]:
    data = await _get(client, "/fixtures", {"date": date.today().isoformat(), "timezone": TIMEZONE})
    return [f for f in data.get("response", []) if f["league"]["id"] in WATCHED_LEAGUES]

async def fetch_enriched_matches() -> list[RawMatch]:
    async with httpx.AsyncClient() as client:
        fixtures = await fetch_todays_fixtures(client)
        results = []
        
        for fx in fixtures:
            try:
                h_id, a_id, l_id = fx["teams"]["home"]["id"], fx["teams"]["away"]["id"], fx["league"]["id"]
                
                # Busca histórico para calcular médias de gols
                h_hist = await fetch_team_last10(client, h_id, l_id)
                a_hist = await fetch_team_last10(client, a_id, l_id)
                
                # Cálculo seguro (evita divisão por zero se não houver histórico)
                h_scored = sum(f["goals"]["home"] if f["teams"]["home"]["id"] == h_id else f["goals"]["away"] for f in h_hist if f["goals"]["home"] is not None) / (len(h_hist) or 1)
                a_scored = sum(f["goals"]["away"] if f["teams"]["away"]["id"] == a_id else f["goals"]["home"] for f in a_hist if f["goals"]["away"] is not None) / (len(a_hist) or 1)

                results.append(RawMatch(
                    fx["fixture"]["id"], l_id, fx["league"]["name"], fx["league"]["logo"], WATCHED_LEAGUES.get(l_id, "default"),
                    RawTeamStats(h_id, fx["teams"]["home"]["name"], fx["teams"]["home"]["logo"], _form_to_list(fx["teams"]["home"].get("form")), round(h_scored, 2), 1.0),
                    RawTeamStats(a_id, fx["teams"]["away"]["name"], fx["teams"]["away"]["logo"], _form_to_list(fx["teams"]["away"].get("form")), round(a_scored, 2), 1.0),
                    datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00")),
                    _parse_status(fx["fixture"]["status"]["short"]), fx["fixture"]["status"].get("elapsed"),
                    fx["goals"]["home"], fx["goals"]["away"]
                ))
            except Exception:
                continue # Se um jogo falhar, pula para o próximo para não travar o site
        
        return results
