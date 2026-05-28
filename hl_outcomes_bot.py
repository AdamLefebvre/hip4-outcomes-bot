#!/usr/bin/env python3
"""
Hyperliquid HIP-4 Outcomes Farming Bot
Volume-farming sur les marchés binaires pour un potentiel Airdrop 2.

Stratégie :
  • Limit-buy 10 USDH au mid-price (ordres limit = 0 frais)
  • Take-profit à +5 %
  • DCA si -10 % (2 legs max)
  • Hard-stop à -35 %
  • Recommence immédiatement après chaque clôture
  • Auto-détecte le nouveau marché chaque jour

Usage :
  python hl_outcomes_bot.py                 # lance le bot
  python hl_outcomes_bot.py --list-markets  # affiche les marchés Outcome actifs
"""

import math
import os
import sys
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

try:
    import requests
    import eth_account
    from eth_account.signers.local import LocalAccount
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants
    from hyperliquid.utils.signing import (
        order_request_to_order_wire,
        order_wires_to_order_action,
        sign_l1_action,
        get_timestamp_ms,
    )
    from dotenv import load_dotenv
except ImportError:
    sys.exit(
        "Dépendances manquantes. Lance :\n"
        "  pip install hyperliquid-python-sdk eth-account python-dotenv requests"
    )

load_dotenv()

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

API = constants.MAINNET_API_URL


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG — modifie uniquement cette section
# ═══════════════════════════════════════════════════════════════════════════════

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

# Côté à trader : "YES" (bullish BTC) ou "NO" (bearish BTC)
# Le coin exact (#NNN) est auto-détecté chaque jour depuis outcomeMeta.
SIDE = "NO"

BASE_USDH        = 10.0   # USDH par leg (≈ 10 €)
TAKE_PROFIT_PCT  = 0.05   # vend à +5 %
DCA_TRIGGER_PCT  = -0.10  # DCA à -10 %
HARD_STOP_PCT    = -0.35  # coupe tout à -35 %
MAX_DCA_LEGS     = 2      # max 2 DCA supplémentaires (3 legs total)

ORDER_FILL_TIMEOUT = 90   # secondes pour attendre un fill
POLL_SEC           = 20   # intervalle de vérification de position

REENTRY_PULLBACK_PCT = 0.02   # attend -2% depuis le prix de vente avant re-entrée
REENTRY_TIMEOUT      = 300    # secondes max d'attente de pullback (5 min)
SLIPPAGE_BPS       = 15   # bps de slippage sur le prix limite

ROUNDS = 0                # 0 = tourne indéfiniment

# ═══════════════════════════════════════════════════════════════════════════════


# ── POSITION ──────────────────────────────────────────────────────────────────

@dataclass
class Position:
    coin: str
    entries: list = field(default_factory=list)  # [(price, qty), ...]
    dca_count: int = 0

    @property
    def qty(self) -> float:
        return sum(q for _, q in self.entries)

    @property
    def cost(self) -> float:
        return sum(p * q for p, q in self.entries)

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty else 0.0

    def pnl_pct(self, price: float) -> float:
        return (price / self.avg - 1.0) if self.avg else 0.0

    def __str__(self) -> str:
        return (
            f"qty={self.qty:.5f}  avg={self.avg:.5f}  "
            f"legs={len(self.entries)}  DCA={self.dca_count}"
        )


# ── CONNEXION ─────────────────────────────────────────────────────────────────

def setup() -> Tuple[Info, Exchange, str]:
    account: LocalAccount = eth_account.Account.from_key(PRIVATE_KEY)
    info     = Info(API, skip_ws=True)
    exchange = Exchange(account, API)
    return info, exchange, account.address


# ── AUTO-DÉTECTION DU MARCHÉ ──────────────────────────────────────────────────

def get_active_coin(side: str = "YES") -> Optional[str]:
    """Retourne le coin (#NNN) du marché priceBinary BTC actif."""
    try:
        r = requests.post(API + "/info", json={"type": "outcomeMeta"}, timeout=5)
        meta = r.json()
    except Exception as exc:
        log.warning(f"get_active_coin: {exc}")
        return None

    for outcome in meta.get("outcomes", []):
        if "priceBinary" in outcome.get("description", ""):
            oid   = outcome["outcome"]
            specs = outcome.get("sideSpecs", [])
            for idx, spec in enumerate(specs):
                if spec["name"].upper() == side.upper():
                    coin = f"#{oid * 10 + idx}"
                    log.info(
                        f"Marché actif : {coin} ({spec['name']}) | "
                        f"{outcome['description']}"
                    )
                    return coin

    log.warning("Aucun marché priceBinary BTC actif trouvé dans outcomeMeta.")
    return None


# ── MARKET DATA ───────────────────────────────────────────────────────────────

def debug_outcome_market() -> None:
    """Diagnostique l'asset_id correct pour les coins Outcome #NNN."""
    import json

    coin = "#750"  # le coin actif

    # 1. spotMeta complet — universe count + tokens count
    sm = requests.post(API + "/info", json={"type": "spotMeta"}, timeout=10).json()
    univ   = sm.get("universe", [])
    tokens = sm.get("tokens", [])
    print(f"\n══ spotMeta : {len(univ)} marchés, {len(tokens)} tokens ══")
    # cherche tokens avec # ou 'outcome' ou 'yes'/'no' dans le nom
    for t in tokens:
        n = t.get("name", "")
        if "#" in n or n.lower() in ("yes", "no") or "outcome" in n.lower():
            print(f"  TOKEN: {t}")
    # cherche marchés avec index élevé (> 450)
    high_idx = [u for u in univ if u.get("index", 0) > 450]
    print(f"  Marchés spot avec index > 450 : {high_idx[:10]}")

    # 2. spotMetaAndAssetCtxs — compte les ctxs vs univers
    sma = requests.post(API + "/info", json={"type": "spotMetaAndAssetCtxs"}, timeout=10).json()
    su = sma[0].get("universe", []) if isinstance(sma, list) else []
    sc = sma[1] if isinstance(sma, list) and len(sma) > 1 else []
    print(f"\n══ spotMetaAndAssetCtxs : {len(su)} marchés, {len(sc)} ctxs ══")
    # si plus de ctxs que de marchés, les entrées extra pourraient être des outcomes
    if len(sc) > len(su):
        print(f"  ⚠ {len(sc) - len(su)} ctx sans marché correspondant !")
        for i in range(len(su), min(len(su)+20, len(sc))):
            print(f"    ctx[{i}] = {sc[i]}")

    # 3. perpDexs — tous les dexes disponibles
    try:
        dexs = requests.post(API + "/info", json={"type": "perpDexs"}, timeout=5).json()
        print(f"\n══ perpDexs ══")
        print(json.dumps(dexs[:5] if isinstance(dexs, list) else dexs, indent=2))
    except Exception as e:
        print(f"\n══ perpDexs erreur : {e} ══")

    # 4. allMids — tous les coins #NNN
    mids = requests.post(API + "/info", json={"type": "allMids"}, timeout=5).json()
    hash_mids = {k: v for k, v in mids.items() if k.startswith("#")}
    print(f"\n══ allMids : {len(hash_mids)} coins #NNN → {hash_mids} ══")

    # 5. Toutes les balances spot du wallet
    wallet_addr = eth_account.Account.from_key(os.getenv("PRIVATE_KEY", "")).address
    sus = requests.post(API + "/info", json={"type": "spotClearinghouseState", "user": wallet_addr}, timeout=5).json()
    balances = sus.get("balances", [])
    print(f"\n══ Spot balances ({len(balances)}) ══")
    for b in balances:
        print(f"  {b}")

    # 6. Marge perp du wallet
    cs = requests.post(API + "/info", json={"type": "clearinghouseState", "user": wallet_addr}, timeout=5).json()
    margin = cs.get("crossMarginSummary", {})
    print(f"\n══ Perp margin ══")
    print(f"  accountValue={margin.get('accountValue')}  withdrawable={cs.get('withdrawable')}")


def list_outcome_markets() -> None:
    """Affiche les marchés Outcome HIP-4 actifs avec leurs coins et prix."""
    try:
        meta = requests.post(API + "/info", json={"type": "outcomeMeta"}, timeout=5).json()
        mids = requests.post(API + "/info", json={"type": "allMids"}, timeout=5).json()
    except Exception as e:
        print(f"Erreur: {e}")
        return

    print("\n═══ Marchés Outcome HIP-4 actifs ═══\n")

    for outcome in meta.get("outcomes", []):
        oid   = outcome["outcome"]
        desc  = outcome.get("description", "")
        specs = outcome.get("sideSpecs", [])

        label = desc
        if "priceBinary" in desc:
            parts  = dict(p.split(":") for p in desc.split("|") if ":" in p)
            target = parts.get("targetPrice", "?")
            expiry = parts.get("expiry", "?")
            label  = f"BTC > {target}$  (expiry {expiry})"

        print(f"  Outcome {oid}  [{outcome.get('name', '')}]  {label}")
        for i, spec in enumerate(specs):
            coin_key = f"#{oid * 10 + i}"
            mid      = mids.get(coin_key, "N/A")
            print(f"    → COIN={coin_key!r:<10}  side={spec['name']:<5}  mid={mid}")
        print()

    print("─── Le bot auto-détecte le bon coin via SIDE = \"YES\" ou \"NO\" ───\n")


def get_book(coin: str) -> Tuple[Optional[float], Optional[float]]:
    """Retourne (best_bid, best_ask) via appel API direct."""
    try:
        r = requests.post(
            API + "/info",
            json={"type": "l2Book", "coin": coin},
            timeout=5,
        )
        r.raise_for_status()
        book = r.json()
        if not book or "levels" not in book:
            return None, None
        bids = book["levels"][0]
        asks = book["levels"][1]
        bid  = float(bids[0]["px"]) if bids else None
        ask  = float(asks[0]["px"]) if asks else None
        return bid, ask
    except Exception as exc:
        log.warning(f"get_book({coin}): {exc}")
        return None, None


def get_mid(coin: str) -> Optional[float]:
    bid, ask = get_book(coin)
    if bid and ask:
        return (bid + ask) / 2
    return bid or ask


def bps_of(price: float, n: int) -> float:
    return price * n / 10_000


def rp(x: float, d: int = 5) -> float:
    return round(x, d)


def _fw(x: float) -> str:
    """float → wire string (même logique que le SDK Hyperliquid)."""
    r = round(x, 8)
    if abs(r) < 1e-12:
        return "0"
    return f"{r:g}"


# ── ORDRES : EIP-712 signing + _post_action ───────────────────────────────────
#
# exchange.order() utilise info.name_to_asset() qui ne connaît pas les coins
# #NNN (Outcome). On bypasse le lookup en passant l'asset_id = int(N) directement,
# puis on signe manuellement avec sign_l1_action et on poste via _post_action.

def _is_mainnet(exchange: Exchange) -> bool:
    return exchange.base_url == constants.MAINNET_API_URL


def _load_outcome_asset_cache() -> None:
    pass  # plus nécessaire, la formule est directe


def _outcome_asset_id(coin: str) -> int:
    """HIP-4 asset_id = 100_000_000 + NNN où coin = '#NNN'."""
    return 100_000_000 + int(coin.lstrip("#"))


def _place_order(
    exchange: Exchange,
    coin: str,
    is_buy: bool,
    sz: float,
    px: float,
) -> dict:
    """Signe et poste un ordre limit pour un coin Outcome (#NNN)."""
    asset_id = _outcome_asset_id(coin)
    order_wire = order_request_to_order_wire(
        {
            "coin": coin,
            "is_buy": is_buy,
            "sz": sz,
            "limit_px": px,
            "order_type": {"limit": {"tif": "Gtc"}},
            "reduce_only": False,
        },
        asset_id,
    )
    action    = order_wires_to_order_action([order_wire], None, "na")
    timestamp = get_timestamp_ms()
    signature = sign_l1_action(
        exchange.wallet,
        action,
        exchange.vault_address,
        timestamp,
        exchange.expires_after,
        _is_mainnet(exchange),
    )
    return exchange._post_action(action, signature, timestamp)


def _cancel_order(exchange: Exchange, coin: str, oid: int) -> dict:
    """Annule un ordre Outcome avec signature EIP-712."""
    asset_id = _outcome_asset_id(coin)
    action    = {
        "type": "cancel",
        "cancels": [{"a": asset_id, "o": oid}],
    }
    timestamp = get_timestamp_ms()
    signature = sign_l1_action(
        exchange.wallet,
        action,
        exchange.vault_address,
        timestamp,
        exchange.expires_after,
        _is_mainnet(exchange),
    )
    return exchange._post_action(action, signature, timestamp)


def _open_orders(address: str) -> list:
    """Récupère les ordres ouverts via API directe."""
    try:
        r = requests.post(
            API + "/info",
            json={"type": "openOrders", "user": address},
            timeout=5,
        )
        return r.json() or []
    except Exception as exc:
        log.warning(f"_open_orders: {exc}")
        return []


def _extract_oid(result: dict) -> Optional[int]:
    try:
        status = result["response"]["data"]["statuses"][0]
        if "error" in status:
            raise ValueError(status["error"])
        if "resting" in status:
            return status["resting"]["oid"]
        if "filled" in status:
            return None  # filled immédiatement
    except ValueError:
        raise
    except (KeyError, IndexError, TypeError):
        pass
    return None


def _wait_fill(address: str, coin: str, oid: int) -> bool:
    """Attend que l'ordre ne soit plus dans les ordres ouverts."""
    deadline = time.time() + ORDER_FILL_TIMEOUT
    while time.time() < deadline:
        time.sleep(5)
        orders = _open_orders(address)
        if not any(o.get("oid") == oid for o in orders):
            return True
    return False


# ── LIMIT BUY / SELL ──────────────────────────────────────────────────────────

def limit_buy(
    exchange: Exchange,
    address: str,
    coin: str,
    usdh: float,
) -> Optional[Tuple[float, float]]:
    """Retourne (fill_price, qty) ou None."""
    _, ask = get_book(coin)
    if ask is None:
        log.error("Pas d'ask disponible — marché inexistant ou expiré ?")
        return None

    px  = rp(min(ask + bps_of(ask, SLIPPAGE_BPS), 1.0))
    qty = max(1, math.ceil(usdh * 1.05 / px))  # +5% buffer → valeur serveur ≥ 10 USDH même si mid bouge
    log.info(f"  → BUY  {qty} {coin} @ {px:.5f}  ({usdh:.2f} USDH)")

    try:
        result = _place_order(exchange, coin, True, qty, px)
    except Exception as exc:
        log.error(f"limit_buy exception: {exc}")
        return None

    log.info(f"  Réponse: {result}")
    if result.get("status") != "ok":
        log.error(f"Ordre refusé: {result}")
        return None

    try:
        oid = _extract_oid(result)
    except ValueError as exc:
        log.error(f"  Ordre refusé par le serveur: {exc}")
        return None

    if oid is None:
        log.info("  Filled immédiatement ✓")
        return px, qty

    log.info(f"  En attente (oid={oid})…")
    if _wait_fill(address, coin, oid):
        log.info(f"  Filled ✓")
        return px, qty

    log.warning(f"  Timeout — annulation oid={oid}")
    try:
        _cancel_order(exchange, coin, oid)
    except Exception:
        pass
    return None


def limit_sell(
    exchange: Exchange,
    address: str,
    coin: str,
    qty: float,
    target_px: Optional[float] = None,
    _retry: int = 0,
) -> bool:
    if _retry > 3:
        log.error("Échec de vente après 3 tentatives.")
        return False

    bid, _ = get_book(coin)
    if bid is None:
        log.error("Pas de bid disponible.")
        return False

    px = rp(target_px if target_px is not None else max(bid - bps_of(bid, SLIPPAGE_BPS), 0.0001))
    log.info(f"  → SELL {qty:.5f} {coin} @ {px:.5f}")

    try:
        result = _place_order(exchange, coin, False, qty, px)
    except Exception as exc:
        log.error(f"limit_sell exception: {exc}")
        return False

    log.info(f"  Réponse: {result}")
    if result.get("status") != "ok":
        log.error(f"Sell refusé: {result}")
        return False

    try:
        oid = _extract_oid(result)
    except ValueError as exc:
        log.error(f"  Sell refusé par le serveur: {exc}")
        return False

    if oid is None:
        log.info("  Sold immédiatement ✓")
        return True

    log.info(f"  En attente (oid={oid})…")
    if _wait_fill(address, coin, oid):
        log.info("  Sold ✓")
        return True

    log.warning("  Sell timeout — relance au bid actuel")
    try:
        _cancel_order(exchange, coin, oid)
    except Exception:
        pass
    return limit_sell(exchange, address, coin, qty, target_px=None, _retry=_retry + 1)


# ── COOLDOWN POST-TP ──────────────────────────────────────────────────────────

def wait_for_pullback(coin: str, sell_px: float) -> None:
    """Après un TP, attend que le prix repulle de REENTRY_PULLBACK_PCT avant de re-rentrer."""
    target = sell_px * (1 - REENTRY_PULLBACK_PCT)
    deadline = time.time() + REENTRY_TIMEOUT
    log.info(
        f"  ⏳ Attente pullback : prix cible ≤ {target:.5f} "
        f"(timeout {REENTRY_TIMEOUT}s)"
    )
    while time.time() < deadline:
        time.sleep(POLL_SEC)
        price = get_mid(coin)
        if price is None:
            continue
        log.info(f"  Pullback watch : price={price:.5f}  cible={target:.5f}")
        if price <= target:
            log.info(f"  ✓ Pullback atteint ({price:.5f} ≤ {target:.5f})")
            return
    log.info("  Timeout pullback — re-entrée immédiate.")


# ── ROUND ─────────────────────────────────────────────────────────────────────

def run_round(exchange: Exchange, address: str, coin: str, n: int, pullback: bool = True) -> bool:
    """Retourne True si le round s'est clôturé sur un TP (→ attendre pullback)."""
    log.info(f"\n{'═'*56}\n  ROUND {n}  |  {coin}  |  {BASE_USDH:.0f} USDC/leg\n{'═'*56}")

    fill = limit_buy(exchange, address, coin, BASE_USDH)
    if fill is None:
        log.warning("Entrée initiale échouée, pause 30s.")
        time.sleep(30)
        return False

    pos = Position(coin=coin)
    pos.entries.append(fill)
    log.info(f"  Position: {pos}")

    sep = "─" * 56
    while True:
        time.sleep(POLL_SEC)

        price = get_mid(coin)
        if price is None:
            log.warning("Prix indisponible, réessai…")
            continue

        pnl = pos.pnl_pct(price)
        log.info(f"  {sep}")
        log.info(f"  price={price:.5f}  avg={pos.avg:.5f}  pnl={pnl:+.2%}  qty={pos.qty:.5f}")

        if pnl >= TAKE_PROFIT_PCT:
            log.info(f"  ✅ Take profit ({pnl:+.2%})")
            sell_px = rp(price - bps_of(price, SLIPPAGE_BPS))
            limit_sell(exchange, address, coin, pos.qty, target_px=sell_px)
            if pullback:
                wait_for_pullback(coin, sell_px)
            return True

        if pnl <= HARD_STOP_PCT:
            log.info(f"  🛑 Hard stop ({pnl:+.2%})")
            limit_sell(exchange, address, coin, pos.qty)
            return False

        if pnl <= DCA_TRIGGER_PCT and pos.dca_count < MAX_DCA_LEGS:
            log.info(f"  📉 DCA #{pos.dca_count + 1} ({pnl:+.2%})")
            dca = limit_buy(exchange, address, coin, BASE_USDH)
            if dca:
                pos.entries.append(dca)
                pos.dca_count += 1
                log.info(f"  Position: {pos}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main() -> None:
    if "--debug" in sys.argv:
        debug_outcome_market()
        return

    if "--list-markets" in sys.argv:
        list_outcome_markets()
        return

    pullback = "--no-pullback" not in sys.argv
    if not pullback:
        log.info("Mode : re-entrée immédiate après TP (sans pullback)")

    if not PRIVATE_KEY:
        sys.exit("⚠️  PRIVATE_KEY manquante. Crée un fichier .env avec PRIVATE_KEY=0x...")

    info, exchange, address = setup()
    log.info(f"Wallet : {address}")

    # Vérification balance USDH en spot (balance libre = total - hold)
    sus = requests.post(API + "/info", json={"type": "spotClearinghouseState", "user": address}, timeout=5).json()
    # Les Outcome markets utilisent USDC comme quote currency (pas USDH)
    usdc_bal  = next((b for b in sus.get("balances", []) if b.get("coin") == "USDC"), {})
    usdc_free = float(usdc_bal.get("total", 0)) - float(usdc_bal.get("hold", 0))
    min_needed = BASE_USDH * (1 + MAX_DCA_LEGS)
    log.info(f"Balance USDC spot libre : {usdc_free:.2f} (total={float(usdc_bal.get('total',0)):.2f}  hold={float(usdc_bal.get('hold',0)):.2f}  minimum conseillé : {min_needed:.2f})")
    if usdc_free < BASE_USDH:
        sys.exit(
            f"⚠️  Balance USDC spot insuffisante ({usdc_free:.2f} USDC).\n"
            "   Dépose du USDC directement dans ton wallet spot HL (pas en perp margin).\n"
            "   Sur l'UI : Portfolio → Transfer → Deposit to Spot."
        )

    # Annulation des ordres outcome stale au démarrage
    open_ords = _open_orders(address)
    outcome_ords = [o for o in open_ords if str(o.get("coin", "")).startswith("#")]
    if outcome_ords:
        log.info(f"Annulation de {len(outcome_ords)} ordre(s) outcome stale…")
        for o in outcome_ords:
            try:
                _cancel_order(exchange, o["coin"], o["oid"])
                log.info(f"  Annulé oid={o['oid']} coin={o['coin']}")
            except Exception as exc:
                log.warning(f"  Échec annulation oid={o['oid']}: {exc}")

    # Auto-détection du marché actif
    coin = get_active_coin(SIDE)
    if coin is None:
        sys.exit("Aucun marché Outcome actif trouvé. Réessaie plus tard.")

    mid = get_mid(coin)
    if mid is None:
        sys.exit(f"Impossible d'obtenir le prix de '{coin}'. Marché expiré ou vide ?")
    log.info(f"Mid price {coin} ({SIDE}) : {mid:.5f}")

    round_n  = 0
    last_day = time.strftime("%Y%m%d")

    try:
        while ROUNDS == 0 or round_n < ROUNDS:
            # Renouvellement quotidien du coin
            today = time.strftime("%Y%m%d")
            if today != last_day:
                log.info("Nouveau jour — re-détection du marché actif…")
                new_coin = get_active_coin(SIDE)
                if new_coin and new_coin != coin:
                    coin     = new_coin
                    last_day = today
                    log.info(f"Nouveau coin : {coin}")

            round_n += 1
            try:
                run_round(exchange, address, coin, round_n, pullback=pullback)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.exception(f"Round {round_n} erreur: {exc}")
                log.info("Pause 60s avant le prochain round…")
                time.sleep(60)
    except KeyboardInterrupt:
        log.info("\nBot arrêté par l'utilisateur.")

    log.info("Bot terminé.")


if __name__ == "__main__":
    main()
