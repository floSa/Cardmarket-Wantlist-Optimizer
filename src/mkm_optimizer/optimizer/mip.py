"""
Optimiseur exact par programmation linéaire en nombres entiers (MIP).

Modélisation :

  Variables
  ---------
  z[o, w] ∈ ℤ ≥ 0   : nb d'exemplaires de l'offre o attribués au want w.
                       (≥ 0, ≤ stock dispo de o, et = 0 si offre/want incompat.)
  x[v]    ∈ {0, 1}  : vendeur v sélectionné.
  y[v, k] ∈ {0, 1}  : palier de FDP k actif pour le vendeur v.
  u[w]    ∈ ℤ ≥ 0   : nb d'exemplaires non couverts pour le want w (slack).

  Contraintes
  -----------
  (1) Demande : ∀w     Σ_o z[o,w] + u[w] = qty(w)
  (2) Stock   : ∀o     Σ_w z[o,w] ≤ stock(o)
  (3) Activ.  : ∀v     Σ_{o chez v} Σ_w z[o,w] ≤ M_v · x[v]      (M_v = stock total chez v)
  (4) Palier  : ∀v     Σ_k y[v,k] = x[v]
  (5) Capa pal: ∀v     Σ_{o chez v} Σ_w z[o,w] ≤ Σ_k max_cards_k · y[v,k]
  (6) Max v.  :        Σ_v x[v] ≤ MAX_VENDEURS                   (si défini)

  Objectif
  --------
  min   Σ_{o,w} z[o,w] · price(o)
      + Σ_{v,k} y[v,k] · cost_k                                 (FDP)
      + PENALTY · Σ_w u[w]                                       (slack)

  Avec PENALTY largement supérieur au prix max possible : le solveur ne
  laisse un want non couvert que s'il n'existe AUCUNE offre compatible.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import Iterable

import pulp

from ..models import (
    Assignment,
    Offer,
    ShippingBracket,
    Solution,
    VendorBasket,
    WantEntry,
)
from .compat import is_compatible


log = logging.getLogger(__name__)


# Pénalité par exemplaire non couvert — doit dépasser tout coût raisonnable.
DEFAULT_UNMET_PENALTY = Decimal("10000")


def solve(
    wants: list[WantEntry],
    offers: list[Offer],
    brackets: list[ShippingBracket],
    max_vendors: int | None = None,
    scenario_name: str = "default",
    time_limit_seconds: int = 60,
    unmet_penalty: Decimal = DEFAULT_UNMET_PENALTY,
    vendor_fixed_cost: Decimal = Decimal("0"),
    msg: bool = False,
) -> Solution:
    """
    `vendor_fixed_cost` : coût supplémentaire (€) ajouté à l'objectif pour
    chaque vendeur sélectionné, EN PLUS du FDP réel. Sert à pénaliser les
    vendeurs marginaux qui ne font économiser que quelques centimes. Mettre
    à 0 (défaut) pour comportement standard ; mettre à 5-15 € pour favoriser
    un panier frugal en nombre de vendeurs.
    """
    """
    Résout le problème d'optimisation et retourne une Solution.

    Lève RuntimeError si le solveur ne trouve pas de solution faisable
    (très improbable avec la variable de slack u[w]).
    """
    if not brackets:
        raise ValueError("Au moins un palier de FDP requis.")
    brackets = sorted(brackets, key=lambda b: b.max_cards)

    # --- Pré-calculs : compatibilité want ↔ offre ---------------------------
    # compat_offers[w_idx]   → liste d'offer indices compatibles
    # compat_wants[o_idx]    → liste de want indices compatibles
    compat_offers: dict[int, list[int]] = {wi: [] for wi in range(len(wants))}
    compat_wants: dict[int, list[int]] = {oi: [] for oi in range(len(offers))}
    for wi, w in enumerate(wants):
        for oi, o in enumerate(offers):
            if is_compatible(o, w):
                compat_offers[wi].append(oi)
                compat_wants[oi].append(wi)

    # Vendeurs distincts présents dans `offers`
    sellers: list[str] = sorted({o.seller for o in offers})
    seller_idx: dict[str, int] = {s: i for i, s in enumerate(sellers)}
    offers_by_seller: dict[str, list[int]] = defaultdict(list)
    for oi, o in enumerate(offers):
        offers_by_seller[o.seller].append(oi)

    # --- Variables -----------------------------------------------------------
    prob = pulp.LpProblem(f"mkm_optim_{scenario_name}", pulp.LpMinimize)

    # z[o, w] (entier ≥ 0) — seulement pour les paires compatibles
    z: dict[tuple[int, int], pulp.LpVariable] = {}
    for oi, wi_list in compat_wants.items():
        for wi in wi_list:
            stock = offers[oi].quantity_available
            z[(oi, wi)] = pulp.LpVariable(
                f"z_{oi}_{wi}", lowBound=0, upBound=stock, cat=pulp.LpInteger
            )

    # x[v] binaire
    x = {
        v: pulp.LpVariable(f"x_{seller_idx[v]}", cat=pulp.LpBinary)
        for v in sellers
    }

    # y[v, k] binaire
    y = {
        (v, k): pulp.LpVariable(f"y_{seller_idx[v]}_{k}", cat=pulp.LpBinary)
        for v in sellers
        for k in range(len(brackets))
    }

    # u[w] (slack pour wants non couverts)
    u = {
        wi: pulp.LpVariable(
            f"u_{wi}", lowBound=0, upBound=wants[wi].quantity, cat=pulp.LpInteger
        )
        for wi in range(len(wants))
    }

    # --- Contraintes ---------------------------------------------------------

    # (1) Demande exacte (avec slack)
    for wi, w in enumerate(wants):
        prob += (
            pulp.lpSum(z[(oi, wi)] for oi in compat_offers[wi]) + u[wi]
            == w.quantity,
            f"demand_{wi}",
        )

    # (2) Stock par offre — déjà géré par upBound sur z[o,w] individuellement,
    #     mais il faut aussi Σ_w z[o,w] ≤ stock_o (sinon on pourrait dépasser
    #     si l'offre est compatible avec plusieurs wants).
    for oi, wi_list in compat_wants.items():
        if not wi_list:
            continue
        prob += (
            pulp.lpSum(z[(oi, wi)] for wi in wi_list) <= offers[oi].quantity_available,
            f"stock_{oi}",
        )

    # (3) Activation vendeur via big-M (M = stock total chez v)
    for v in sellers:
        big_m = sum(offers[oi].quantity_available for oi in offers_by_seller[v]) or 1
        prob += (
            pulp.lpSum(
                z[(oi, wi)]
                for oi in offers_by_seller[v]
                for wi in compat_wants[oi]
            )
            <= big_m * x[v],
            f"activate_{seller_idx[v]}",
        )

    # (4) Si vendeur sélectionné, exactement un palier actif
    for v in sellers:
        prob += (
            pulp.lpSum(y[(v, k)] for k in range(len(brackets))) == x[v],
            f"one_bracket_{seller_idx[v]}",
        )

    # (5) Capacité palier (linéaire) :
    #     n_v ≤ Σ_k max_cards_k · y[v,k]
    for v in sellers:
        n_v = pulp.lpSum(
            z[(oi, wi)]
            for oi in offers_by_seller[v]
            for wi in compat_wants[oi]
        )
        prob += (
            n_v
            <= pulp.lpSum(brackets[k].max_cards * y[(v, k)] for k in range(len(brackets))),
            f"bracket_cap_{seller_idx[v]}",
        )

    # (6) Plafond du nombre de vendeurs (scénario)
    if max_vendors is not None:
        prob += (
            pulp.lpSum(x[v] for v in sellers) <= max_vendors,
            "max_vendors",
        )

    # --- Objectif ------------------------------------------------------------
    cards_cost = pulp.lpSum(
        z[(oi, wi)] * float(offers[oi].price)
        for oi in compat_wants
        for wi in compat_wants[oi]
    )
    shipping_cost = pulp.lpSum(
        y[(v, k)] * float(brackets[k].cost)
        for v in sellers
        for k in range(len(brackets))
    )
    slack_cost = pulp.lpSum(
        u[wi] * float(unmet_penalty * Decimal(str(wants[wi].priority)))
        for wi in range(len(wants))
    )
    # Coût fixe par vendeur sélectionné : pénalise les vendeurs marginaux
    vendor_cost = pulp.lpSum(x[v] * float(vendor_fixed_cost) for v in sellers)
    prob += cards_cost + shipping_cost + slack_cost + vendor_cost

    # --- Résolution ----------------------------------------------------------
    solver = pulp.PULP_CBC_CMD(msg=msg, timeLimit=time_limit_seconds)
    status = prob.solve(solver)
    status_name = pulp.LpStatus[status]
    log.info("MIP %s : status=%s, objectif=%.2f", scenario_name, status_name, pulp.value(prob.objective) or 0.0)
    if status_name not in ("Optimal", "Not Solved"):
        raise RuntimeError(f"Solveur MIP en échec : {status_name}")

    # --- Extraction de la solution ------------------------------------------
    baskets: list[VendorBasket] = []
    for v in sellers:
        if pulp.value(x[v]) is None or pulp.value(x[v]) < 0.5:
            continue
        assignments: list[Assignment] = []
        for oi in offers_by_seller[v]:
            qty_total = 0
            for wi in compat_wants[oi]:
                qv = pulp.value(z[(oi, wi)]) or 0
                qty_total += int(round(qv))
            if qty_total > 0:
                assignments.append(Assignment(offer=offers[oi], quantity=qty_total))
        # Palier actif
        ship = Decimal("0")
        for k in range(len(brackets)):
            if pulp.value(y[(v, k)]) and pulp.value(y[(v, k)]) > 0.5:
                ship = brackets[k].cost
                break
        if assignments:
            baskets.append(
                VendorBasket(seller=v, assignments=assignments, shipping_cost=ship)
            )

    unmet: list[WantEntry] = []
    for wi, w in enumerate(wants):
        slack = pulp.value(u[wi]) or 0
        if slack > 0.5:
            # On reporte le want avec une quantité = slack (= nb manquants)
            unmet.append(_with_quantity(w, int(round(slack))))

    return Solution(
        scenario_name=scenario_name,
        baskets=baskets,
        unmet_wants=unmet,
    )


def _with_quantity(w: WantEntry, qty: int) -> WantEntry:
    """Copie d'un WantEntry avec une quantité différente (slack restant)."""
    return WantEntry(
        card_name=w.card_name,
        product_url=w.product_url,
        quantity=qty,
        set_code=w.set_code,
        set_label=w.set_label,
        min_condition=w.min_condition,
        languages=list(w.languages),
        foil=w.foil,
        is_signed=w.is_signed,
        is_altered=w.is_altered,
        max_price=w.max_price,
    )


# ---- Utilitaire : convertir la config YAML en list[ShippingBracket] --------

def brackets_from_config(brackets_cfg: Iterable[dict]) -> list[ShippingBracket]:
    return [
        ShippingBracket(max_cards=int(b["max_cards"]), cost=Decimal(str(b["cost"])))
        for b in brackets_cfg
    ]
