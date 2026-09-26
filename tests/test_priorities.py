"""
Tests unitaires pour le module de priorités Excel (priorities.py)
et l'intégration de la pondération dans le solveur MIP.
"""

from decimal import Decimal
from pathlib import Path

from mkm_optimizer.models import Condition, Foil, Offer, ShippingBracket, WantEntry
from mkm_optimizer.optimizer.mip import solve
from mkm_optimizer.priorities import (
    apply_priorities_to_wants,
    export_priorities_excel,
    load_priorities_excel,
    parse_priority_value,
)


def test_parse_priority_value() -> None:
    assert parse_priority_value(1) == 1.0
    assert parse_priority_value(0.5) == 0.5
    assert parse_priority_value("0.3") == 0.3
    assert parse_priority_value("0,25") == 0.25
    assert parse_priority_value("haute") == 1.0
    assert parse_priority_value("urgente") == 1.0
    assert parse_priority_value("basse") == 0.3
    assert parse_priority_value("bonus") == 0.0
    assert parse_priority_value(None) == 1.0
    assert parse_priority_value("invalide_xyz") == 1.0


def test_export_and_load_priorities_excel(tmp_path: Path) -> None:
    xlsx_file = tmp_path / "test_priorities.xlsx"

    wants = [
        WantEntry(
            card_name="Black Lotus",
            product_url="https://mkm/1",
            quantity=1,
            set_code="LEA",
            set_label="Alpha",
            min_condition=Condition.EX,
            languages=["en"],
            priority=1.0,
        ),
        WantEntry(
            card_name="Sol Ring",
            product_url="https://mkm/2",
            quantity=2,
            set_code=None,
            set_label="Indifférent",
            min_condition=Condition.NM,
            languages=["fr", "en"],
            priority=0.5,
            notes="Secondaire",
        ),
    ]

    # 1. Export initial
    out_path = export_priorities_excel(wants, xlsx_file)
    assert out_path.exists()

    # 2. Chargement
    loaded = load_priorities_excel(xlsx_file)
    assert "black lotus" in loaded
    assert "sol ring" in loaded
    assert loaded["black lotus"][0] == 1.0
    assert loaded["sol ring"][0] == 0.5
    assert loaded["sol ring"][1] == "Secondaire"

    # 3. Application sur les wants
    applied = apply_priorities_to_wants(wants, xlsx_file)
    assert len(applied) == 2
    assert applied[0].priority == 1.0
    assert applied[1].priority == 0.5
    assert applied[1].notes == "Secondaire"


def test_mip_priority_selection() -> None:
    """
    Vérifie qu'avec une contrainte de 1 seul vendeur max, le solveur choisit
    le vendeur qui propose la carte prioritaire (prio 1.0) plutôt que la carte
    secondaire (prio 0.1).
    """
    wants = [
        WantEntry(
            card_name="Carte_Urgente",
            product_url="https://mkm/1",
            quantity=1,
            set_code=None,
            set_label=None,
            min_condition=Condition.NM,
            languages=["fr"],
            priority=1.0,
        ),
        WantEntry(
            card_name="Carte_Secondaire",
            product_url="https://mkm/2",
            quantity=1,
            set_code=None,
            set_label=None,
            min_condition=Condition.NM,
            languages=["fr"],
            priority=0.1,
        ),
    ]

    # Vendeur A a Carte_Urgente
    # Vendeur B a Carte_Secondaire
    offers = [
        Offer(
            seller="Vendeur_A",
            card_name="Carte_Urgente",
            product_url="https://mkm/1",
            set_label="Set 1",
            set_code=None,
            condition=Condition.NM,
            language="fr",
            foil=Foil.NO,
            is_signed=False,
            is_altered=False,
            price=Decimal("5.00"),
            quantity_available=1,
        ),
        Offer(
            seller="Vendeur_B",
            card_name="Carte_Secondaire",
            product_url="https://mkm/2",
            set_label="Set 1",
            set_code=None,
            condition=Condition.NM,
            language="fr",
            foil=Foil.NO,
            is_signed=False,
            is_altered=False,
            price=Decimal("1.00"),
            quantity_available=1,
        ),
    ]

    brackets = [ShippingBracket(max_cards=100, cost=Decimal("4.00"))]

    # Contrainte : max 1 vendeur
    sol = solve(wants=wants, offers=offers, brackets=brackets, max_vendors=1)

    assert len(sol.baskets) == 1
    # Le solveur doit avoir sélectionné Vendeur_A pour satisfaire Carte_Urgente
    assert sol.baskets[0].seller == "Vendeur_A"
    assert sol.baskets[0].assignments[0].offer.card_name == "Carte_Urgente"
    # Carte_Secondaire doit être dans les unmet_wants
    assert any(w.card_name == "Carte_Secondaire" for w in sol.unmet_wants)
