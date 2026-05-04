"""
betmaster-api/main.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FastAPI — expõe os dados enriquecidos pro frontend na Vercel.

Rodar local:
  uvicorn main:app --reload --port 8000

Deploy:
  Railway / Render / Fly.io  (ver README)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import json
import math
import asyncio
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Header, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from football_api import fetch_enriched_matches, RawMatch, RawOdds
from betmaster_engine import (   # motor Poisson do arquivo anterior
    TeamStats, MatchContext, MatchOdds,
    predict_match, PredictionResult, Signal,
)

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379")
STRIPE_SECRET   = os.getenv("STRIPE_SECRET_KEY", "")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "https://betmaster.vercel.app")

CACHE_TTL_MATCHES  = 300   # 5 min — jogos de hoje
CACHE_TTL_HISTORY  = 1800  # 30 min — histórico de greens
CACHE_TTL_PREMIUM  = 60    # 1 min  — resultado premium (atualiza mais rápido)

app = FastAPI(
    title="BetMaster AI",
    version="2.0.0",
    docs_url="/docs" if os.getenv("ENV") != "production" else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "http://localhost:3000", "http://localhost:5173"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════════════════════
# REDIS
# ══════════════════════════════════════════════════════════
_redis: aioredis.Redis | None = None

async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def cache_get(key: str) -> Any | None:
    r = await get_redis()
    val = await r.get(key)
    return json.loads(val) if val else None


async def cache_set(key: str, data: Any, ttl: int):
    r = await get_redis()
    await r.setex(key, ttl, json.dumps(data, default=str))


# ══════════════════════════════════════════════════════════
# BRIDGE: RawMatch → MatchContext (para o motor Poisson)
# ══════════════════════════════════════════════════════════
def raw_to_context(raw: RawMatch) -> MatchContext:
    h = raw.home
    a = raw.away

    home_inj  = [ab.player_name for ab in raw.home_absences if ab.reason == "injury"]
    home_susp = [ab.player_name for ab in raw.home_absences if ab.reason == "suspension"]
    away_inj  = [ab.player_name for ab in raw.away_absences if ab.reason == "injury"]
    away_susp = [ab.player_name for ab in raw.away_absences if ab.reason == "suspension"]

    return MatchContext(
        home_team=TeamStats(
            name=h.name,
            avg_scored=h.avg_scored,
            avg_conceded=h.avg_conceded,
            home_strength=h.home_strength,
            away_strength=h.away_strength,
            form_score=h.form_score,
        ),
        away_team=TeamStats(
            name=a.name,
            avg_scored=a.avg_scored,
            avg_conceded=a.avg_conceded,
            home_strength=a.home_strength,
            away_strength=a.away_strength,
            form_score=a.form_score,
        ),
        odds=MatchOdds(
            home=raw.odds.home or 2.0,
            draw=raw.odds.draw,
            away=raw.odds.away or 3.5,
        ),
        league=raw.league_key,
        home_injuries=home_inj,
        away_injuries=away_inj,
        home_suspended=home_susp,
        away_suspended=away_susp,
    )


# ══════════════════════════════════════════════════════════
# SERIALIZER: RawMatch + PredictionResult → dict pro frontend
# ══════════════════════════════════════════════════════════
def serialize_match(raw: RawMatch, pred: PredictionResult, is_premium: bool) -> dict:
    """
    Separa dados FREE dos dados VIP.
    Frontend recebe o mesmo objeto — campos VIP vêm como None se não premium.
    """
    absences_away = [
        {"name": ab.player_name, "reason": ab.reason}
        for ab in raw.away_absences
    ]
    absences_home = [
        {"name": ab.player_name, "reason": ab.reason}
        for ab in raw.home_absences
    ]

    base = {
        # ── Identificação ──
        "id":           raw.fixture_id,
        "league_id":    raw.league_id,
        "league_name":  raw.league_name,
        "league_logo":  raw.league_logo,
        "league_key":   raw.league_key,

        # ── Times ──
        "home_name":    raw.home.name,
        "home_logo":    raw.home.logo,
        "home_last5":   raw.home.last5,
        "away_name":    raw.away.name,
        "away_logo":    raw.away.logo,
        "away_last5":   raw.away.last5,

        # ── Jogo ──
        "kickoff_utc":  raw.kickoff_utc.isoformat(),
        "status":       raw.status,
        "minute":       raw.minute,
        "home_score":   raw.home_score,
        "away_score":   raw.away_score,

        # ── Odds (públicas) ──
        "odds_home":    raw.odds.home,
        "odds_draw":    raw.odds.draw,
        "odds_away":    raw.odds.away,

        # ── FREE: contexto ──
        "signal":           pred.signal.value,
        "bank_vacilou":     pred.bank_vacilou,
        "is_zebra":         pred.is_zebra,
        "best_ev":          max(pred.ev.values()) if pred.ev else 0,
        "prob_home":        pred.win_probs.get("home", 0),
        "prob_draw":        pred.win_probs.get("draw", 0),
        "prob_away":        pred.win_probs.get("away", 0),
        "ev_home":          pred.ev.get("home", 0),
        "ev_draw":          pred.ev.get("draw"),
        "ev_away":          pred.ev.get("away", 0),
        "verdict_vibe":     pred.verdict_vibe,         # análise de contexto (free)
        "absences_home":    absences_home,
        "absences_away":    absences_away,

        # ── VIP: bloqueados se não premium ──
        "predicted_score":  pred.top_score          if is_premium else None,
        "score_prob":       pred.top_score_prob      if is_premium else None,
        "confidence":       pred.confidence          if is_premium else None,
        "kelly_pct":        round(pred.kelly_pct*100,2) if is_premium else None,
        "stake_r":          pred.stake_r             if is_premium else None,
        "stake_label":      pred.stake_label         if is_premium else None,
        "verdict_papo":     pred.verdict_papo        if is_premium else None,
        "top5_scores":      [
            {"score": s, "prob": round(p*100,2)} for s, p in pred.top_5_scores
        ] if is_premium else None,
    }
    return base


# ══════════════════════════════════════════════════════════
# AUTH — verifica se usuário é VIP
# (Em produção: JWT assinado pelo Stripe webhook)
# ══════════════════════════════════════════════════════════
async def is_premium_user(authorization: str | None = Header(default=None)) -> bool:
    """
    Valida token VIP.
    Token = SHA256(email + STRIPE_SECRET) gerado no webhook de pagamento.
    Frontend envia: Authorization: Bearer <token>
    """
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization.removeprefix("Bearer ").strip()
    r = await get_redis()
    result = await r.get(f"vip_token:{token}")
    return result == "1"


# ══════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/matches/today")
async def matches_today(
    filter: str = "todos",            # todos | gold | green | live | value
    premium: bool = Depends(is_premium_user),
):
    """
    Jogos de hoje com predição Poisson + EV+.
    Dados VIP (placar, confiança, kelly, papo_reto) só para usuários premium.
    Cache de 5 minutos no Redis — evita explodir a cota da API-Football.
    """
    cache_key = f"matches:today:{filter}:{'vip' if premium else 'free'}"
    cached = await cache_get(cache_key)
    if cached:
        return JSONResponse(cached, headers={"X-Cache": "HIT"})

    # Busca dados reais
    raw_matches = await fetch_enriched_matches()

    # Roda o motor Poisson para cada jogo
    output = []
    for raw in raw_matches:
        if raw.status == "postponed":
            continue
        if not raw.odds.home or not raw.odds.away:
            continue   # sem odds = não conseguimos calcular EV

        ctx  = raw_to_context(raw)
        pred = predict_match(ctx)
        serialized = serialize_match(raw, pred, is_premium=premium)
        output.append(serialized)

    # Filtros do frontend
    if filter == "gold":
        output = [m for m in output if m["signal"] == "gold"]
    elif filter == "green":
        output = [m for m in output if m["signal"] in ("gold", "green")]
    elif filter == "live":
        output = [m for m in output if m["status"] == "live"]
    elif filter == "value":
        output = [m for m in output if (m["best_ev"] or 0) > 5]

    # Ordena: live primeiro, depois por EV desc
    output.sort(key=lambda m: (
        0 if m["status"] == "live" else 1,
        -(m["best_ev"] or 0),
    ))

    ttl = CACHE_TTL_PREMIUM if premium else CACHE_TTL_MATCHES
    await cache_set(cache_key, output, ttl)
    return JSONResponse(output, headers={"X-Cache": "MISS"})


@app.get("/matches/{fixture_id}/premium")
async def match_premium(
    fixture_id: int,
    premium: bool = Depends(is_premium_user),
):
    """
    Retorna dados VIP de uma partida específica.
    Usado quando usuário assina e quer ver o placar imediatamente.
    """
    if not premium:
        raise HTTPException(status_code=402, detail={
            "error": "vip_required",
            "message": "Sai fora — esse dado é só pra quem é VIP. Assina por R$30/mês.",
        })

    cache_key = f"match:vip:{fixture_id}"
    cached = await cache_get(cache_key)
    if cached:
        return cached

    # Busca o jogo específico
    raw_matches = await fetch_enriched_matches()
    raw = next((m for m in raw_matches if m.fixture_id == fixture_id), None)
    if not raw:
        raise HTTPException(status_code=404, detail="Jogo não encontrado.")

    ctx  = raw_to_context(raw)
    pred = predict_match(ctx)
    data = serialize_match(raw, pred, is_premium=True)

    await cache_set(cache_key, data, CACHE_TTL_PREMIUM)
    return data


@app.get("/history/greens")
async def greens_history(limit: int = 20):
    """
    Histórico de tips com resultado (GREEN/RED), EV% documentado.
    Alimentado manualmente via /admin/log_result ou automaticamente
    por job que roda após final de cada partida.
    """
    cache_key = f"history:greens:{limit}"
    cached = await cache_get(cache_key)
    if cached:
        return cached

    r = await get_redis()
    keys = await r.lrange("predictions_log", 0, limit - 1)
    logs = [json.loads(k) for k in keys]

    stats = {
        "total":     len(logs),
        "greens":    sum(1 for l in logs if l.get("hit")),
        "reds":      sum(1 for l in logs if not l.get("hit")),
        "win_rate":  0,
        "total_profit_r": 0,
    }
    if stats["total"]:
        stats["win_rate"] = round(stats["greens"] / stats["total"] * 100, 1)
        stats["total_profit_r"] = round(sum(l.get("profit_r", 0) for l in logs), 2)

    result = {"stats": stats, "logs": logs}
    await cache_set(cache_key, result, CACHE_TTL_HISTORY)
    return result


# ══════════════════════════════════════════════════════════
# STRIPE WEBHOOK — ativa VIP após pagamento
# ══════════════════════════════════════════════════════════
@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """
    Recebe eventos do Stripe.
    Quando checkout.session.completed → gera token VIP e salva no Redis.
    
    No Stripe Dashboard:
      Endpoint URL: https://seu-backend.railway.app/webhook/stripe
      Eventos: checkout.session.completed, customer.subscription.deleted
    """
    import hmac, hashlib
    stripe_sig = request.headers.get("stripe-signature", "")
    body = await request.body()

    # Verificação de assinatura (NUNCA pule isso em produção)
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if webhook_secret:
        try:
            import stripe
            event = stripe.Webhook.construct_event(body, stripe_sig, webhook_secret)
        except Exception:
            raise HTTPException(status_code=400, detail="Assinatura Stripe inválida.")
    else:
        event = json.loads(body)

    r = await get_redis()

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email   = session.get("customer_email") or session.get("customer_details", {}).get("email")
        if email:
            token = hashlib.sha256(f"{email}{STRIPE_SECRET}".encode()).hexdigest()
            # VIP por 31 dias
            await r.setex(f"vip_token:{token}", 31 * 86400, "1")
            await r.set(f"vip_email:{email}", token)
            # Retorna token pro frontend via webhook ou email
            print(f"✅ VIP ativado: {email} → token {token[:12]}...")

    elif event["type"] == "customer.subscription.deleted":
        session = event["data"]["object"]
        email   = session.get("customer_email")
        if email:
            token = await r.get(f"vip_email:{email}")
            if token:
                await r.delete(f"vip_token:{token}")
                await r.delete(f"vip_email:{email}")
            print(f"⛔ VIP cancelado: {email}")

    return {"received": True}


# ══════════════════════════════════════════════════════════
# ADMIN — loga resultado real (roda após final de cada jogo)
# ══════════════════════════════════════════════════════════
class PredictionLog(BaseModel):
    fixture_id:       int
    game_label:       str        # "Flamengo 2x0 Palmeiras"
    tip:              str        # "Flamengo vencer"
    predicted_score:  str
    actual_score:     str
    hit:              bool
    odd_used:         float
    profit_r:         float      # positivo = lucro, negativo = perda
    ev_pct:           float

@app.post("/admin/log_result")
async def log_result(
    log: PredictionLog,
    x_admin_key: str = Header(default=""),
):
    """Endpoint protegido para registrar resultados reais."""
    if x_admin_key != os.getenv("ADMIN_KEY", "change_me"):
        raise HTTPException(status_code=403, detail="Acesso negado.")

    r = await get_redis()
    entry = {
        **log.model_dump(),
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    await r.lpush("predictions_log", json.dumps(entry))
    await r.ltrim("predictions_log", 0, 499)   # mantém últimos 500

    # Invalida cache do histórico
    keys = await r.keys("history:greens:*")
    if keys:
        await r.delete(*keys)

    return {"ok": True, "entry": entry}
