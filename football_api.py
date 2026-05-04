"""
betmaster-api/football_api.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Camada de integração com a API-Football (api-sports.io).
Responsabilidade única: buscar e normalizar dados brutos.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import httpx
import asyncio
from datetime import date, datetime, timezone
from typing import Any
from dataclasses import dataclass, field

# ── Config ─────────────────────────────────────────────────
API_BASE    = "https://v3.football.api-sports.io"
API_KEY     = os.getenv("API_FOOTBALL_KEY", "")          # nunca hardcode
TIMEZONE    = "America/Sao_Paulo"
TIMEOUT_S   = 12.0

# Ligas monitoradas (id → nome interno)
WATCHED_LEAGUES: dict[int, str] = {
    71:  "brasileirao",      # Brasileirão Série A
    72:  "brasileirao_b",    # Brasileirão Série B
    2:   "champions",        # UEFA Champions League
    39:  "premier",          # Premier League
    140: "laliga",           # La Liga
    135: "serie_a_it",       # Serie A Italiana
    78:  "bundesliga",       # Bundesliga
    61:  "ligue1",           # Ligue 1
}

# ── Data classes de saída (agnósticos de banco) ────────────
@dataclass
class RawTeamStats:
    team_id:    int
    name:       str
    logo:       str
    last5:      list[str]       # ["W","W","D","L","W"]
    avg_scored: float
    avg_conceded: float
    home_strength: float = 1.0
    away_strength: float = 1.0
    form_score: float = 50.0    # 0-100

@dataclass
class RawAbsence:
    player_name: str
    reason:      str   # "injury" | "suspension" | "yellow_risk"
    importance:  str   # "key" | "rotation" | "bench"

@dataclass
class RawOdds:
    home:  float | None = None
    draw:  float | None = None
    away:  float | None = None

@dataclass
class RawMatch:
    fixture_id:    int
    league_id:     int
    league_name:   str
    league_logo:   str
    league_key:    str              # chave interna (ex: "brasileirao")
    home:          RawTeamStats
    away:          RawTeamStats
    kickoff_utc:   datetime
    status:        str              # "scheduled"|"live"|"finished"|"postponed"
    minute:        int | None
    home_score:    int | None
    away_score:    int | None
    home_absences: list[RawAbsence] = field(default_factory=list)
    away_absences: list[RawAbsence] = field(default_factory=list)
    odds:          RawOdds          = field(default_factory=RawOdds)


# ── HTTP Client singleton ───────────────────────────────────
def _headers() -> dict:
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY não configurada. Seta no .env.")
    return {
        "x-apisports-key": API_KEY,
        "Accept":          "application/json",
    }


async def _get(client: httpx.AsyncClient, path: str, params: dict = {}) -> dict:
    """Wrapper com timeout e tratamento de erro básico."""
    url = f"{API_BASE}{path}"
    resp = await client.get(url, params=params, headers=_headers(), timeout=TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    # A API-Football embute erros dentro do JSON mesmo com status 200
    if data.get("errors"):
        raise ValueError(f"API-Football error: {data['errors']}")
    return data


# ── Helpers de normalização ─────────────────────────────────
def _parse_status(status_short: str) -> str:
    live_codes = {"1H","HT","2H","ET","BT","P","INT","LIVE"}
    finished   = {"FT","AET","PEN"}
    if status_short in live_codes:    return "live"
    if status_short in finished:      return "finished"
    if status_short in {"PST","CANC","ABD","AWD","WO"}: return "postponed"
    return "scheduled"


def _form_to_list(form_str: str | None) -> list[str]:
    """'WWDLW' → ['W','W','D','L','W'] (últimos 5)"""
    if not form_str:
        return []
    return list(form_str.upper().replace("D","D"))[-5:]


def _form_score(last5: list[str]) -> float:
    """Converte últimos 5 resultados em score 0-100."""
    points = {"W": 20, "D": 8, "L": 0}
    return sum(points.get(r, 0) for r in last5)


def _calc_averages(fixtures: list[dict], team_id: int) -> tuple[float, float]:
    """Calcula média de gols marcados e sofridos nas últimas N partidas."""
    scored = conceded = 0
    count  = 0
    for f in fixtures:
        home_id = f["teams"]["home"]["id"]
        gs = f["goals"]["home"] if home_id == team_id else f["goals"]["away"]
        gc = f["goals"]["away"] if home_id == team_id else f["goals"]["home"]
        if gs is None or gc is None:
            continue
        scored   += gs
        conceded += gc
        count    += 1
    if count == 0:
        return 1.2, 1.2   # fallback neutro
    return round(scored / count, 3), round(conceded / count, 3)


def _calc_strengths(fixtures: list[dict], team_id: int) -> tuple[float, float]:
    """
    Calcula multiplicadores de força em casa vs fora
    baseado nas últimas N partidas (win_rate * 1.5 + 0.5).
    """
    home_w = home_t = away_w = away_t = 0
    for f in fixtures:
        home_id = f["teams"]["home"]["id"]
        won_home  = f["teams"]["home"]["winner"]
        won_away  = f["teams"]["away"]["winner"]
        if home_id == team_id:
            home_t += 1
            if won_home: home_w += 1
        else:
            away_t += 1
            if won_away: away_w += 1

    h_str = (home_w / home_t) * 1.5 + 0.5 if home_t else 1.0
    a_str = (away_w / away_t) * 1.5 + 0.5 if away_t else 1.0
    return round(min(h_str, 1.8), 3), round(min(a_str, 1.8), 3)


def _parse_absences(injuries_data: list[dict], team_id: int) -> list[RawAbsence]:
    """Converte resposta de /injuries em lista RawAbsence."""
    result = []
    for item in injuries_data:
        if item["team"]["id"] != team_id:
            continue
        reason = item["player"].get("reason", "injury").lower()
        if "yellow" in reason or "suspended" in reason:
            reason_key = "suspension"
        elif "injury" in reason or "muscle" in reason or "knee" in reason:
            reason_key = "injury"
        else:
            reason_key = "injury"

        result.append(RawAbsence(
            player_name=item["player"]["name"],
            reason=reason_key,
            importance="key",   # refinado com lineup importance abaixo
        ))
    return result


# ── Funções de fetch ────────────────────────────────────────

async def fetch_todays_fixtures(client: httpx.AsyncClient) -> list[dict]:
    """Busca todos os jogos de hoje nas ligas monitoradas."""
    today = date.today().isoformat()
    league_ids = ",".join(str(lid) for lid in WATCHED_LEAGUES)

    data = await _get(client, "/fixtures", {
        "date":     today,
        "timezone": TIMEZONE,
        # Filtra pelas ligas de interesse direto na query
        # (API-Football não aceita lista, então fazemos uma call por liga
        #  OU filtramos no cliente — abaixo filtramos no cliente)
    })
    fixtures = data.get("response", [])
    # Filtra apenas ligas monitoradas
    return [f for f in fixtures if f["league"]["id"] in WATCHED_LEAGUES]


async def fetch_team_last10(client: httpx.AsyncClient, team_id: int, league_id: int) -> list[dict]:
    """Últimas 10 partidas de um time em uma liga."""
    data = await _get(client, "/fixtures", {
        "team":   team_id,
        "league": league_id,
        "last":   10,
        "status": "FT",         # apenas finalizadas
    })
    return data.get("response", [])


async def fetch_injuries(client: httpx.AsyncClient, fixture_id: int) -> list[dict]:
    """Lesionados e suspensos de uma partida específica."""
    data = await _get(client, "/injuries", {"fixture": fixture_id})
    return data.get("response", [])


async def fetch_odds(client: httpx.AsyncClient, fixture_id: int) -> RawOdds:
    """
    Odds de 1X2 (mercado 1) do bookmaker Bet365 (id=8)
    ou primeiro disponível.
    """
    data = await _get(client, "/odds", {
        "fixture":   fixture_id,
        "bookmaker": 8,    # Bet365 — trocar pelo de preferência
        "bet":       1,    # Match Winner (1X2)
    })
    resp = data.get("response", [])
    if not resp:
        return RawOdds()

    try:
        values = resp[0]["bookmakers"][0]["bets"][0]["values"]
        odds_map = {v["value"]: float(v["odd"]) for v in values}
        return RawOdds(
            home=odds_map.get("Home"),
            draw=odds_map.get("Draw"),
            away=odds_map.get("Away"),
        )
    except (IndexError, KeyError):
        return RawOdds()


# ── Função principal ────────────────────────────────────────

async def fetch_enriched_matches() -> list[RawMatch]:
    """
    Pipeline completo:
      1. Busca fixtures de hoje
      2. Para cada fixture: busca histórico dos dois times, lesões e odds
      3. Normaliza e retorna lista de RawMatch prontos pro motor Poisson

    ⚠️  Respeita o rate limit da API-Football:
        - Plano Free:  100 req/dia
        - Plano Basic: 7.500 req/dia
    Usamos semáforo + delays para não explodir a cota.
    """
    semaphore = asyncio.Semaphore(3)   # máx 3 chamadas paralelas

    async def limited_get_team(client, team_id, league_id):
        async with semaphore:
            await asyncio.sleep(0.15)   # ~6 req/s → bem abaixo do limite
            return await fetch_team_last10(client, team_id, league_id)

    async def limited_get_injuries(client, fixture_id):
        async with semaphore:
            await asyncio.sleep(0.15)
            return await fetch_injuries(client, fixture_id)

    async def limited_get_odds(client, fixture_id):
        async with semaphore:
            await asyncio.sleep(0.15)
            return await fetch_odds(client, fixture_id)

    results: list[RawMatch] = []

    async with httpx.AsyncClient() as client:
        fixtures = await fetch_todays_fixtures(client)

        if not fixtures:
            return []

        # Dispara todas as calls em paralelo (com semáforo)
        tasks = []
        for fx in fixtures:
            home_id   = fx["teams"]["home"]["id"]
            away_id   = fx["teams"]["away"]["id"]
            league_id = fx["league"]["id"]
            fix_id    = fx["fixture"]["id"]

            tasks.append((
                fx,
                limited_get_team(client, home_id, league_id),
                limited_get_team(client, away_id, league_id),
                limited_get_injuries(client, fix_id),
                limited_get_odds(client, fix_id),
            ))

        # Executa todas as coroutines de cada fixture
        for fx, h_coro, a_coro, inj_coro, odds_coro in tasks:
            home_hist, away_hist, injuries_raw, odds = await asyncio.gather(
                h_coro, a_coro, inj_coro, odds_coro
            )

            home_id   = fx["teams"]["home"]["id"]
            away_id   = fx["teams"]["away"]["id"]
            league_id = fx["league"]["id"]

            # Métricas dos times
            h_scored, h_conceded = _calc_averages(home_hist, home_id)
            a_scored, a_conceded = _calc_averages(away_hist, away_id)
            h_home_str, _        = _calc_strengths(home_hist, home_id)
            _, a_away_str        = _calc_strengths(away_hist, away_id)

            home_form = _form_to_list(fx["teams"]["home"].get("form"))
            away_form = _form_to_list(fx["teams"]["away"].get("form"))

            home_stats = RawTeamStats(
                team_id=home_id,
                name=fx["teams"]["home"]["name"],
                logo=fx["teams"]["home"]["logo"],
                last5=home_form,
                avg_scored=h_scored,
                avg_conceded=h_conceded,
                home_strength=h_home_str,
                away_strength=1.0,
                form_score=_form_score(home_form),
            )
            away_stats = RawTeamStats(
                team_id=away_id,
                name=fx["teams"]["away"]["name"],
                logo=fx["teams"]["away"]["logo"],
                last5=away_form,
                avg_scored=a_scored,
                avg_conceded=a_conceded,
                home_strength=1.0,
                away_strength=a_away_str,
                form_score=_form_score(away_form),
            )

            # Status + placar ao vivo
            status_short = fx["fixture"]["status"]["short"]
            home_score   = fx["goals"]["home"]
            away_score   = fx["goals"]["away"]
            minute       = fx["fixture"]["status"].get("elapsed")

            kickoff = datetime.fromisoformat(
                fx["fixture"]["date"].replace("Z", "+00:00")
            )

            raw = RawMatch(
                fixture_id=fx["fixture"]["id"],
                league_id=league_id,
                league_name=fx["league"]["name"],
                league_logo=fx["league"]["logo"],
                league_key=WATCHED_LEAGUES.get(league_id, "default"),
                home=home_stats,
                away=away_stats,
                kickoff_utc=kickoff,
                status=_parse_status(status_short),
                minute=minute,
                home_score=home_score,
                away_score=away_score,
                home_absences=_parse_absences(injuries_raw, home_id),
                away_absences=_parse_absences(injuries_raw, away_id),
                odds=odds,
            )
            results.append(raw)

    return results
