"""
Smoke test de l'optimiseur MIP.

Deux scénarios :
1) Synthétique : 1 wantlist simple, 3 vendeurs construits à la main pour
   forcer un split de quantité. La solution attendue est vérifiable.
2) Données réelles : ta wantlist 96 wants + les 20 offres CORP-F.
   On vérifie au moins que le solveur tourne et que la solution est cohérente.

Lancer :
    .venv/bin/python tests/smoke_mip.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mkm_optimizer.models import (
    Condition,
    Foil,
    Offer,
    ShippingBracket,
    WantEntry,
)
from mkm_optimizer.optimizer.mip import solve
from mkm_optimizer.parser.seller_offers import parse_seller_offers
from mkm_optimizer.parser.wantlist import parse_wantlist


WANT_HTML = Path(__file__).parent.parent / "data" / "wantlists" / "wantlists.html"
SELLER_HTML = Path(__file__).parent.parent / "data" / "sellers" / "CORP-F" / "page1.html"

BRACKETS = [
    ShippingBracket(max_cards=20,  cost=Decimal("3.60")),
    ShippingBracket(max_cards=50,  cost=Decimal("6.00")),
    ShippingBracket(max_cards=100, cost=Decimal("8.00")),
]


def banner(s: str) -> None:
    print()
    print("=" * 70)
    print(s)
    print("=" * 70)


# ============================================================================
# Test 1 — Synthétique avec split forcé
# ============================================================================

def test_synthetic_split() -> None:
    banner("MIP TEST 1 — Synthétique avec split forcé")

    # Wantlist : 4 Bolt (metacard) + 1 Sol Ring (metacard)
    wants = [
        WantEntry(
            card_name="Lightning Bolt",
            product_url="https://www.cardmarket.com/en/Magic/Cards/Lightning-Bolt",
            quantity=4,
            set_code=None, set_label=None,
            min_condition=Condition.EX,
            languages=["en"],
            foil=Foil.NO,
        ),
        WantEntry(
            card_name="Sol Ring",
            product_url="https://www.cardmarket.com/en/Magic/Cards/Sol-Ring",
            quantity=1,
            set_code=None, set_label=None,
            min_condition=Condition.EX,
            languages=["en"],
            foil=Foil.NO,
        ),
    ]

    # Vendeurs : on construit un cas où le split est rationnel.
    # A : 1 Bolt à 0.10, 1 Sol Ring à 1.30
    # B : 5 Bolt à 0.50, 0 Sol Ring
    # C : 4 Bolt à 0.60, 1 Sol Ring à 1.20
    #
    # Avec FDP = 3.60 € au 1er palier (≤20 cartes) :
    #   Tout chez B + C : 4×0.50 + FDP_B  + 1×1.20 + FDP_C  = 2.00 + 1.20 + 7.20 = 10.40
    #   Tout chez A + B : 1×0.10 (A) + 3×0.50 (B) + 1.30 (A) = 2.90 + 7.20 = 10.10
    #   Tout chez C     : 4×0.60 + 1×1.20 + 3.60              = 7.20
    #   Tout chez A + C : 1×0.10 (A) + 3×0.60 (C) + 1.20 (C) + 7.20 = 10.30
    #   Tout chez B + C : on l'a déjà
    #
    # Le solveur DEVRAIT choisir "tout chez C" (1 vendeur, 7.20 €).
    # Pour forcer un split, on baisse C's Bolt à 0.62 → tout chez C = 4×0.62 + 1.20 + 3.60 = 7.28
    # Et on bloque A.Bolt à 0.10 (1 ex), B 5×0.50 :
    #   A+B : 0.10 + 1.50 + 1.30(A) + 7.20 = 10.10   (A et B sélectionnés)
    # Cas où le SPLIT s'impose dans le sens "deux vendeurs déjà sélectionnés
    # pour d'autres cartes" : il faut que B ait aussi le Sol Ring moins cher.
    #
    # On reconstruit pour un cas vraiment forcé :
    #   A : Bolt 1× à 0.10 (très bas), Sol Ring 1× à 1.30
    #   B : Bolt 5× à 0.50, Sol Ring 1× à 1.00 (le meilleur Sol Ring)
    #   C : Bolt 4× à 0.55, Sol Ring 1× à 1.10
    #
    # Tout chez B : 4×0.50 + 1×1.00 + 3.60 = 6.60   ← 1 vendeur, 6.60 €
    # Tout chez C : 4×0.55 + 1×1.10 + 3.60 = 6.90
    # A+B  : 1×0.10 + 3×0.50 + 1×1.00 + 3.60+3.60 = 9.80
    # → le solveur prend "tout chez B".
    #
    # Pour forcer A à entrer (split), il faut que A ait quelque chose
    # d'irremplaçable. Ajoutons une 3e carte qu'on ne trouve que chez A :
    # "Black Lotus", 1× chez A à 5.00 €. Pas dispo ailleurs.
    #
    # → A devient incontournable, donc déjà payé. Le solveur peut alors
    #   décider d'acheter aussi le Bolt 1× chez A (0.10 € au lieu de 0.50 chez B).
    #
    wants.append(
        WantEntry(
            card_name="Black Lotus",
            product_url="https://www.cardmarket.com/en/Magic/Cards/Black-Lotus",
            quantity=1,
            set_code=None, set_label=None,
            min_condition=Condition.GD,
            languages=["en"],
            foil=Foil.NO,
        )
    )

    offers = [
        # A
        Offer(seller="A", card_name="Lightning Bolt", product_url="…",
              set_label="Beta", set_code=None, condition=Condition.NM, language="en",
              foil=Foil.NO, is_signed=False, is_altered=False,
              price=Decimal("0.10"), quantity_available=1),
        Offer(seller="A", card_name="Sol Ring",       product_url="…",
              set_label="Beta", set_code=None, condition=Condition.NM, language="en",
              foil=Foil.NO, is_signed=False, is_altered=False,
              price=Decimal("1.30"), quantity_available=1),
        Offer(seller="A", card_name="Black Lotus",    product_url="…",
              set_label="Beta", set_code=None, condition=Condition.GD, language="en",
              foil=Foil.NO, is_signed=False, is_altered=False,
              price=Decimal("5.00"), quantity_available=1),
        # B
        Offer(seller="B", card_name="Lightning Bolt", product_url="…",
              set_label="Magic 2011", set_code=None, condition=Condition.NM, language="en",
              foil=Foil.NO, is_signed=False, is_altered=False,
              price=Decimal("0.50"), quantity_available=5),
        Offer(seller="B", card_name="Sol Ring",       product_url="…",
              set_label="Commander 2021", set_code=None, condition=Condition.NM, language="en",
              foil=Foil.NO, is_signed=False, is_altered=False,
              price=Decimal("1.00"), quantity_available=2),
        # C
        Offer(seller="C", card_name="Lightning Bolt", product_url="…",
              set_label="Magic 2010", set_code=None, condition=Condition.NM, language="en",
              foil=Foil.NO, is_signed=False, is_altered=False,
              price=Decimal("0.55"), quantity_available=4),
        Offer(seller="C", card_name="Sol Ring",       product_url="…",
              set_label="Revised", set_code=None, condition=Condition.NM, language="en",
              foil=Foil.NO, is_signed=False, is_altered=False,
              price=Decimal("1.10"), quantity_available=1),
    ]

    sol = solve(wants, offers, BRACKETS, max_vendors=None, scenario_name="synth")

    print(f"Vendeurs retenus    : {[b.seller for b in sol.baskets]}")
    print(f"Cartes               : {sol.cards_total} €")
    print(f"FDP                  : {sol.shipping_total} €")
    print(f"TOTAL                : {sol.grand_total} €")
    for b in sol.baskets:
        print(f"\n  Vendeur {b.seller}  ({b.total_units} cartes, {b.cards_subtotal} € cartes + {b.shipping_cost} € FDP) :")
        for a in b.assignments:
            print(f"    {a.quantity}× {a.offer.card_name}  ({a.offer.set_label}, {a.offer.condition.value}, {a.offer.language})  @ {a.offer.price} €  → {a.line_total} €")
    if sol.unmet_wants:
        print("\nWants non couverts :")
        for w in sol.unmet_wants:
            print(f"  - {w.quantity}× {w.card_name}")

    # Sanity :
    # - A doit être sélectionné (Black Lotus introuvable ailleurs)
    # - Total cartes = qty cumulée demandée = 6
    sellers_used = {b.seller for b in sol.baskets}
    assert "A" in sellers_used, "A doit être sélectionné (Black Lotus exclusif)"
    total_units = sum(b.total_units for b in sol.baskets)
    expected_units = sum(w.quantity for w in wants) - sum(w.quantity for w in sol.unmet_wants)
    assert total_units == expected_units, (
        f"Décompte incohérent : total_units={total_units}, attendu={expected_units}"
    )
    # Optimum théorique (réfléchi à la main) :
    #   A : 1×Bolt 0.10 + 1×Lotus 5.00 = 5.10 + FDP 3.60 = 8.70
    #   B : 3×Bolt 0.50 + 1×SolRing 1.00 = 2.50 + FDP 3.60 = 6.10
    #   Total : 14.80 €
    # Alternative sans split (tout chez B + Lotus chez A) :
    #   A : Lotus 5.00 + FDP 3.60 = 8.60
    #   B : 4×Bolt 0.50 + SolRing 1.00 = 3.00 + FDP 3.60 = 6.60
    #   Total : 15.20 € (pire de 0.40)
    # Le solveur doit choisir 14.80 (split).
    assert sol.grand_total == Decimal("14.80"), f"Attendu 14.80 €, obtenu {sol.grand_total}"
    # Et le split sur Bolt : 1 chez A + 3 chez B
    bolt_qty_by_seller = {
        b.seller: sum(a.quantity for a in b.assignments if a.offer.card_name == "Lightning Bolt")
        for b in sol.baskets
    }
    assert bolt_qty_by_seller.get("A") == 1, f"Attendu 1 Bolt chez A, obtenu {bolt_qty_by_seller}"
    assert bolt_qty_by_seller.get("B") == 3, f"Attendu 3 Bolt chez B, obtenu {bolt_qty_by_seller}"
    print("\n✓ Test synthétique OK : split forcé (1×A + 3×B Bolt) correctement détecté, total = 14.80 €")


# ============================================================================
# Test 2 — Données réelles : ta wantlist + CORP-F seul
# ============================================================================

def test_real_data() -> None:
    banner("MIP TEST 2 — Wantlist réelle vs. CORP-F seul")
    wants = parse_wantlist(WANT_HTML)
    seller, offers = parse_seller_offers(SELLER_HTML)
    print(f"Wantlist : {len(wants)} wants / {sum(w.quantity for w in wants)} cartes")
    print(f"Vendeur  : {seller} — {len(offers)} offres / {sum(o.quantity_available for o in offers)} unités")

    sol = solve(wants, offers, BRACKETS, max_vendors=5, scenario_name="real_corp_f")
    print(f"\nVendeurs retenus  : {[b.seller for b in sol.baskets]}")
    print(f"Cartes achetées   : {sum(b.total_units for b in sol.baskets)}")
    print(f"Wants couverts    : {len(wants) - len(sol.unmet_wants)} / {len(wants)}")
    print(f"Cartes manquantes : {sum(w.quantity for w in sol.unmet_wants)}")
    print(f"Coût cartes       : {sol.cards_total} €")
    print(f"FDP               : {sol.shipping_total} €")
    print(f"TOTAL             : {sol.grand_total} €")
    for b in sol.baskets:
        print(f"\n  Vendeur {b.seller}  ({b.total_units} cartes, palier {b.shipping_cost} €) :")
        for a in b.assignments[:15]:
            print(f"    {a.quantity}× {a.offer.card_name}  ({a.offer.set_label}, {a.offer.condition.value}, {a.offer.language})  @ {a.offer.price} €")


if __name__ == "__main__":
    test_synthetic_split()
    test_real_data()
    banner("✓ MIP smoke tests OK")
