"""
Découverte de vendeurs à partir des fiches produit de la wantlist.

`fetch` part d'une liste de vendeurs connue et récupère leurs offres. Ici c'est
l'inverse : on part des cartes voulues, on ouvre la fiche produit de chacune
(qui liste TOUS les vendeurs qui la proposent) et on en déduit quels vendeurs
mériteraient d'entrer dans `data/vendeurs_liste/vendeurs.yaml`.

Filtrage à la source via les paramètres natifs MKM ajoutés à l'URL produit :
  - `sellerCountry=12` : déjà présent dans les URLs de la wantlist (France)
  - `language=1,2`     : anglais + français uniquement
  - `minCondition=3`   : EX ou mieux (1=MT, 2=NM, 3=EX…)

MKM trie par prix croissant et n'affiche que 50 offres par palier. Un clic sur
« MONTRER PLUS DE RÉSULTATS » en ajoute 50. On s'arrête volontairement bas
(2 clics = 150 offres) : au-delà on ne récupère que des prix hors sujet.
"""

from __future__ import annotations

import logging
import random
import re
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from patchright.sync_api import sync_playwright

from ..models import WantEntry
from ..parser.product import parse_product_page
from .auth import get_authenticated_context

log = logging.getLogger(__name__)

LOAD_MORE_BUTTON = "#loadMoreButton"
OFFER_ROW = "div.article-row"
URL_FILTERS = "&language=1,2&minCondition=3"


@dataclass
class DiscoveredSeller:
    """Agrégat par vendeur, tous produits confondus."""

    seller: str
    sales: int | None = None
    cards: set[str] = field(default_factory=set)
    prices: list[Decimal] = field(default_factory=list)

    @property
    def n_cards(self) -> int:
        return len(self.cards)

    @property
    def median_price(self) -> Decimal | None:
        return statistics.median(self.prices) if self.prices else None


def fetch_product_pages(
    wants: list[WantEntry],
    output_dir: Path,
    clicks: int = 2,
    headless: bool = True,
    min_delay_ms: int = 800,
    max_delay_ms: int = 1500,
) -> tuple[int, int]:
    """
    Télécharge la fiche produit de chaque want dans `output_dir`.
    Retourne (nb_ok, nb_erreurs).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ok = ko = 0

    with sync_playwright() as p:
        browser, ctx = get_authenticated_context(p, headless=headless)
        try:
            page = ctx.new_page()
            for i, w in enumerate(wants, start=1):
                dest = output_dir / f"{_slug(w.card_name)}.html"
                try:
                    page.goto(w.product_url + URL_FILTERS,
                              wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(2000)
                    for _ in range(clicks):
                        btn = page.locator(LOAD_MORE_BUTTON)
                        if not btn.count() or not btn.first.is_visible():
                            break
                        btn.first.click()
                        page.wait_for_timeout(2500)
                    dest.write_text(page.content(), encoding="utf-8")
                    log.info("[%d/%d] %s — %d offres", i, len(wants), w.card_name,
                             page.locator(OFFER_ROW).count())
                    ok += 1
                except Exception as exc:  # noqa: BLE001 — on continue sur erreur isolée
                    log.warning("[%d/%d] %s — échec : %s", i, len(wants), w.card_name, exc)
                    ko += 1
                time.sleep(random.uniform(min_delay_ms / 1000, max_delay_ms / 1000))
        finally:
            browser.close()
    return ok, ko


def aggregate(discovery_dir: Path) -> list[DiscoveredSeller]:
    """Agrège toutes les fiches produit téléchargées, un enregistrement par vendeur."""
    by_seller: dict[str, DiscoveredSeller] = {}
    for f in sorted(discovery_dir.glob("*.html")):
        for offer in parse_product_page(f):
            d = by_seller.setdefault(offer.seller, DiscoveredSeller(seller=offer.seller))
            if offer.sales is not None:
                d.sales = offer.sales if d.sales is None else max(d.sales, offer.sales)
            d.cards.add(f.stem)
            if offer.price is not None:
                d.prices.append(offer.price)
    return sorted(by_seller.values(), key=lambda d: (-d.n_cards, -(d.sales or 0)))


def select(
    sellers: list[DiscoveredSeller],
    known: set[str],
    min_cards: int,
    min_sales: int,
) -> list[DiscoveredSeller]:
    """Retient les vendeurs inconnus qui passent les deux seuils."""
    return [
        d for d in sellers
        if d.seller not in known
        and d.n_cards >= min_cards
        and (d.sales or 0) >= min_sales
    ]


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:80]
