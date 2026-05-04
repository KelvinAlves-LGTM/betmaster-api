import math
from enum import Enum
from typing import Any
from dataclasses import dataclass, field
from scipy.stats import poisson

class Signal(Enum):
    GOLD = "gold"
    GREEN = "green"
    NEUTRAL = "neutral"
    ZEBRA = "zebra"

@dataclass
class TeamStats:
    name: str
    avg_scored: float
    avg_conceded: float
    home_strength: float
    away_strength: float
    form_score: float

@dataclass
class MatchOdds:
    home: float
    draw: float | None
    away: float

@dataclass
class MatchContext:
    home_team: TeamStats
    away_team: TeamStats
    odds: MatchOdds
    league: str
    home_injuries: list[str] = field(default_factory=list)
    away_injuries: list[str] = field(default_factory=list)
    home_suspended: list[str] = field(default_factory=list)
    away_suspended: list[str] = field(default_factory=list)

@dataclass
class PredictionResult:
    win_probs: dict[str, float]
    ev: dict[str, float]
    top_score: str
    top_score_prob: float
    top_5_scores: list[tuple[str, float]]
    signal: Signal
    confidence: int
    bank_vacilou: bool
    is_zebra: bool
    kelly_pct: float
    stake_r: float
    stake_label: str
    verdict_vibe: str
    verdict_papo: str

def predict_match(ctx: MatchContext) -> PredictionResult:
    # Cálculo de Expectativa de Gols (Poisson)
    exp_home = ctx.home_team.avg_scored * ctx.home_team.home_strength * (ctx.away_team.avg_conceded / 1.1)
    exp_away = ctx.away_team.avg_scored * ctx.away_team.away_strength * (ctx.home_team.avg_conceded / 1.1)
    
    prob_home = prob_away = prob_draw = 0.0
    scores = []

    for h in range(6):
        for a in range(6):
            p = poisson.pmf(h, exp_home) * poisson.pmf(a, exp_away)
            if h > a: prob_home += p
            elif h < a: prob_away += p
            else: prob_draw += p
            scores.append((f"{h}x{a}", p))
    
    scores.sort(key=lambda x: x[1], reverse=True)
    
    # Cálculo de Valor Esperado (EV)
    ev_home = (prob_home * ctx.odds.home - 1) * 100
    
    is_zebra = ctx.odds.home > ctx.odds.away
    signal = Signal.NEUTRAL
    if ev_home > 15: signal = Signal.GOLD
    elif ev_home > 5: signal = Signal.GREEN

    return PredictionResult(
        win_probs={"home": round(prob_home*100, 1), "draw": round(prob_draw*100, 1), "away": round(prob_away*100, 1)},
        ev={"home": round(ev_home, 1), "away": 0.0},
        top_score=scores[0][0],
        top_score_prob=round(scores[0][1]*100, 1),
        top_5_scores=scores[:5],
        signal=signal,
        confidence=int(prob_home * 100),
        bank_vacilou=ev_home > 10,
        is_zebra=is_zebra,
        kelly_pct=0.02,
        stake_r=1.0,
        stake_label="1u",
        verdict_vibe="Análise estatística concluída.",
        verdict_papo="O modelo detectou uma oportunidade baseada em médias históricas."
    )
