"""Exécuteur cash-and-carry réel : deux jambes simultanées (spot long + perp short).

Structure delta-neutre : on achète du BTC au comptant ET on vend le même
notionnel en perpétuel. Le risque de prix s'annule ; on encaisse le funding
versé aux shorts. Le levier ne s'applique qu'à la jambe perp (le collatéral
est le spot détenu, via le portfolio/cross margin de l'exchange).

⚠️ Ce module manipule DEUX comptes (spot + futures) et doit être validé sur
testnet avant tout usage réel. Le risque opérationnel principal est
l'exécution partielle d'une seule jambe (on se retrouve directionnel) : chaque
ouverture/fermeture vérifie que les deux jambes sont passées, et tente de
défaire la jambe orpheline en cas d'échec de l'autre.

Sécurité : clés API dans l'environnement (BINANCE_API_KEY/SECRET), sandbox
activable, retries réseau, clientOrderId déterministe (idempotence).
"""

from __future__ import annotations

import logging
import os
import time

import ccxt

from ..notify import notify

log = logging.getLogger(__name__)

MAX_RETRIES = 4


class CarryBroker:
    def __init__(
        self,
        symbol: str = "BTC/USDT",
        testnet: bool = True,
        leverage: int = 3,
    ) -> None:
        key = os.environ.get("BINANCE_API_KEY")
        secret = os.environ.get("BINANCE_API_SECRET")
        if not key or not secret:
            raise RuntimeError("Clés API manquantes (BINANCE_API_KEY / BINANCE_API_SECRET).")

        self.symbol = symbol
        self.perp_symbol = f"{symbol}:{symbol.split('/')[1]}"  # BTC/USDT:USDT
        common = {"apiKey": key, "secret": secret, "enableRateLimit": True, "timeout": 30_000}
        self.spot: ccxt.Exchange = ccxt.binance({**common, "options": {"defaultType": "spot"}})
        self.perp: ccxt.Exchange = ccxt.binanceusdm(common)
        if testnet:
            self.spot.set_sandbox_mode(True)
            self.perp.set_sandbox_mode(True)
            log.warning("CarryBroker en mode SANDBOX/TESTNET")
        self.spot.load_markets()
        self.perp.load_markets()
        self._with_retries(self.perp.set_leverage, max(1, int(leverage)), self.perp_symbol)
        self._seq = 0

    # ── helpers ──────────────────────────────────────────────────────────────
    def _with_retries(self, fn, *args, **kwargs):
        for attempt in range(MAX_RETRIES):
            try:
                return fn(*args, **kwargs)
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as e:
                wait = 2**attempt
                log.warning("Erreur réseau %s, retry %ss", e.__class__.__name__, wait)
                time.sleep(wait)
        return fn(*args, **kwargs)

    def _coid(self, tag: str) -> str:
        self._seq += 1
        return f"btcq-carry-{tag}-{int(time.time())}-{self._seq}"

    def last_price(self) -> float:
        return float(self.perp.fetch_ticker(self.perp_symbol)["last"])

    def funding_rate(self) -> float:
        return float(self.perp.fetch_funding_rate(self.perp_symbol)["fundingRate"])

    def free_usdt(self) -> float:
        """Solde USDT libre côté spot (base de sizing de la position carry)."""
        bal = self._with_retries(self.spot.fetch_balance)
        return float(bal.get("free", {}).get("USDT", 0.0))

    # ── ouverture / fermeture des deux jambes ────────────────────────────────
    def open_position(self, notional_usdt: float) -> dict | None:
        """Achète `notional_usdt` de BTC au comptant ET short le même montant en
        perp. Retourne {qty, spot_price, perp_price} ou None en cas d'échec."""
        price = self.last_price()
        qty = float(self.perp.amount_to_precision(self.perp_symbol, notional_usdt / price))
        if qty <= 0:
            return None

        # jambe 1 : achat spot
        try:
            spot_order = self._with_retries(
                self.spot.create_order, self.symbol, "market", "buy", qty, None,
                {"newClientOrderId": self._coid("spotbuy")},
            )
        except Exception as e:
            log.error("Échec jambe spot : %s — position non ouverte", e)
            notify(f"⚠ Carry live : échec achat spot ({e}) — position non ouverte")
            return None

        # jambe 2 : short perp. Si elle échoue, on défait le spot (sinon on est long nu).
        try:
            perp_order = self._with_retries(
                self.perp.create_order, self.perp_symbol, "market", "sell", qty, None,
                {"newClientOrderId": self._coid("perpshort")},
            )
        except Exception as e:
            log.error("Échec jambe perp : %s — annulation du spot", e)
            notify(f"⚠ Carry live : short perp échoué ({e}), on revend le spot pour rester neutre")
            try:
                self._with_retries(self.spot.create_order, self.symbol, "market", "sell", qty, None,
                                   {"newClientOrderId": self._coid("spotunwind")})
            except Exception as e2:
                notify(f"🛑 CARRY CRITIQUE : spot non défait ({e2}) — POSITION LONGUE NUE, "
                       f"intervention manuelle requise")
            return None

        sp = float(spot_order.get("average") or price)
        pp = float(perp_order.get("average") or price)
        log.info("Carry ouvert : %.6f BTC — spot %.2f / perp short %.2f", qty, sp, pp)
        notify(f"🔵 Carry live OUVERT : {qty:.5f} BTC neutre (spot {sp:,.0f} / short {pp:,.0f})")
        return {"qty": qty, "spot_price": sp, "perp_price": pp}

    def close_position(self, qty: float) -> bool:
        """Referme les deux jambes : vend le spot, rachète le short perp."""
        ok = True
        try:
            self._with_retries(self.spot.create_order, self.symbol, "market", "sell", qty, None,
                               {"newClientOrderId": self._coid("spotsell")})
        except Exception as e:
            log.error("Échec vente spot : %s", e); ok = False
            notify(f"⚠ Carry live : échec vente spot ({e})")
        try:
            self._with_retries(self.perp.create_order, self.perp_symbol, "market", "buy", qty, None,
                               {"newClientOrderId": self._coid("perpcover"), "reduceOnly": True})
        except Exception as e:
            log.error("Échec rachat perp : %s", e); ok = False
            notify(f"⚠ Carry live : échec rachat short perp ({e})")
        if ok:
            log.info("Carry fermé : %.6f BTC (deux jambes)", qty)
            notify(f"⚪ Carry live FERMÉ : {qty:.5f} BTC")
        return ok

    def reconcile(self) -> bool:
        """Vérifie que spot détenu ≈ short perp (position réellement neutre)."""
        try:
            bal = self._with_retries(self.spot.fetch_balance)
            spot_btc = float(bal.get("total", {}).get("BTC", 0.0))
            positions = self._with_retries(self.perp.fetch_positions, [self.perp_symbol])
            perp_short = 0.0
            for p in positions:
                if p.get("side") == "short":
                    perp_short += float(p.get("contracts") or 0.0)
        except Exception as e:
            log.warning("Réconciliation carry impossible : %s", e)
            return True
        diff = abs(spot_btc - perp_short)
        if diff > 1e-4:
            notify(f"⚠ Carry live : déséquilibre des jambes — spot {spot_btc:.5f} BTC "
                   f"vs short {perp_short:.5f} BTC (écart {diff:.5f}). Vérifier.")
            return False
        return True
