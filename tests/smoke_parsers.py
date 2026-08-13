"""
Smoke test : applique les 2 parsers aux HTMLs réels présents dans data/.
Pas un test pytest — un script de validation rapide pour le développement.

Lancer :
    .venv/bin/python tests/smoke_parsers.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

# Permet de lancer le script sans installer le package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mkm_optimizer.parser.wantlist import parse_wantlist, parse_wantlist_meta
from mkm_optimizer.parser.seller_offers import parse_seller_offers


WANT_HTML = Path(__file__).parent.parent / "data" / "wantlists" / "wantlists.html"
SELLER_HTML = Path(__file__).parent.parent / "data" / "sellers" / "CORP-F" / "page1.html"  # page CORP-F filtrée par wantlist


def banner(s: str) -> None:
    print()
    print("=" * 70)
    print(s)
    print("=" * 70)


def test_wantlist() -> list:
    banner("WANTLIST  —  parse_wantlist + parse_wantlist_meta")
    meta = parse_wantlist_meta(WANT_HTML)
    print(f"Titre wantlist           : {meta['title']!r}")
    print(f"En-tête brut             : {meta['header_raw']!r}")
    print(f"Wants annoncés (h2)      : {meta['announced_wants']}")
    print(f"Cartes annoncées (h2)    : {meta['announced_cards']}")

    entries = parse_wantlist(WANT_HTML)
    total_units = sum(e.quantity for e in entries)
    metacards = sum(1 for e in entries if e.is_metacard)
    print(f"Wants parsés             : {len(entries)}")
    print(f"Exemplaires totaux       : {total_units}")
    print(f"Metacards (set indiff.)  : {metacards} / {len(entries)}")

    # Distribution des quantités, langues, conditions
    qty_dist = Counter(e.quantity for e in entries)
    cond_dist = Counter(e.min_condition.value for e in entries)
    lang_dist = Counter(tuple(sorted(e.languages)) for e in entries)
    foil_dist = Counter(e.foil.value for e in entries)
    print(f"Distrib. quantités        : {dict(qty_dist)}")
    print(f"Distrib. condition min   : {dict(cond_dist)}")
    print(f"Distrib. langues         : {dict(lang_dist)}")
    print(f"Distrib. foil            : {dict(foil_dist)}")
    with_max_price = sum(1 for e in entries if e.max_price is not None)
    print(f"Avec prix souhaité       : {with_max_price} / {len(entries)}")

    # Sanity checks
    assert meta["announced_wants"] == len(entries), (
        f"Disparité : MKM annonce {meta['announced_wants']} wants, "
        f"on en parse {len(entries)}"
    )
    assert meta["announced_cards"] == total_units, (
        f"Disparité : MKM annonce {meta['announced_cards']} cartes, "
        f"on en parse {total_units}"
    )

    # Aperçu
    print("\nAperçu 5 premières entrées :")
    for e in entries[:5]:
        max_p = f"{e.max_price} €" if e.max_price else "-"
        print(
            f"  qty={e.quantity}  {e.card_name!r}  "
            f"set={e.set_label or 'ANY'}  cond>={e.min_condition.value}  "
            f"langs={e.languages}  foil={e.foil.value}  max={max_p}"
        )
    return entries


def test_seller_offers(wantlist_entries: list) -> list:
    banner("OFFRES VENDEUR  —  parse_seller_offers")
    seller, offers = parse_seller_offers(SELLER_HTML)
    print(f"Vendeur détecté          : {seller!r}")
    print(f"Offres parsées           : {len(offers)}")

    # Matching avec la wantlist (par nom de carte normalisé)
    want_names = {e.card_name.lower() for e in wantlist_entries}
    matched = [o for o in offers if o.card_name.lower() in want_names]
    print(f"Offres matchant wantlist : {len(matched)} / {len(offers)}")

    cond_dist = Counter(o.condition.value for o in offers)
    lang_dist = Counter(o.language for o in offers)
    print(f"Distrib. conditions      : {dict(cond_dist)}")
    print(f"Distrib. langues         : {dict(lang_dist)}")
    print(f"Total stock dispo        : {sum(o.quantity_available for o in offers)}")
    prices = [float(o.price) for o in offers if o.price]
    if prices:
        print(
            f"Prix : min={min(prices):.2f} €  "
            f"median={sorted(prices)[len(prices)//2]:.2f} €  "
            f"max={max(prices):.2f} €  "
            f"total_si_tout_pris={sum(prices):.2f} €"
        )

    print("\nAperçu 5 premières offres :")
    for o in offers[:5]:
        print(
            f"  {o.card_name!r}  set={o.set_label!r}  "
            f"cond={o.condition.value}  lang={o.language}  "
            f"{o.price} €  x{o.quantity_available}  "
            f"foil={o.foil.value}  comment={o.comment!r}"
        )
    return offers


if __name__ == "__main__":
    if not WANT_HTML.exists():
        sys.exit(f"HTML wantlist introuvable : {WANT_HTML}")
    if not SELLER_HTML.exists():
        sys.exit(f"HTML vendeur introuvable : {SELLER_HTML}")

    wants = test_wantlist()
    offers = test_seller_offers(wants)
    banner("✓ Smoke test terminé sans erreur")
