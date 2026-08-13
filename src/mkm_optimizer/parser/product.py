"""
Parsing d'une fiche produit MKM (/fr/Magic/Cards/<carte>).

Contrairement à `seller_offers` (une page = un vendeur, N cartes), ici une page
= une carte, N vendeurs. C'est ce qui permet de découvrir des vendeurs absents
de `data/vendeurs_liste/vendeurs.yaml`.

Le nombre de ventes du vendeur est lu dans le tooltip du badge `sell-count`
(`"510&nbsp;Ventes&nbsp;|&nbsp;88&nbsp;Articles disponibles"`) et non dans son
texte visible, qui est arrondi en K au-delà de 1000 ("1K", "2K"…).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from selectolax.parser import HTMLParser, Node

# Nombre de ventes : "510 Ventes | 88 Articles disponibles" (espaces insécables)
_RE_VENTES = re.compile(r"([\d\s .,]+)\s*Ventes", re.IGNORECASE)
_RE_PRIX = re.compile(r"([\d\s .,]+)")


@dataclass(frozen=True)
class ProductOffer:
    """Une offre vue depuis la fiche produit (côté vendeur, pas côté carte)."""

    seller: str
    sales: int | None  # nb de ventes du vendeur, None si illisible
    condition: str | None  # badge MT / NM / EX…
    language: str | None  # libellé MKM ("Français", "Anglais")
    price: Decimal | None


def parse_product_page(html: Path | str) -> list[ProductOffer]:
    """Extrait toutes les offres d'une fiche produit déjà téléchargée."""
    if isinstance(html, Path):
        html = html.read_text(encoding="utf-8")
    tree = HTMLParser(html)
    return [o for row in tree.css("div.article-row") if (o := _parse_row(row)) is not None]


def _parse_row(row: Node) -> ProductOffer | None:
    cell = row.css_first(".col-seller")
    if cell is None:
        return None

    name_a = cell.css_first(".seller-name a")
    seller = name_a.text(strip=True) if name_a else ""
    if not seller:
        return None

    sales = None
    sc = cell.css_first("span.sell-count")
    if sc is not None:
        m = _RE_VENTES.search(sc.attributes.get("data-bs-original-title") or "")
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            sales = int(digits) if digits else None

    cond_a = row.css_first(".product-attributes a.article-condition")
    badge = cond_a.css_first("span.badge") if cond_a else None
    condition = badge.text(strip=True) if badge else None

    lang = row.css_first(".product-attributes span.icon[data-bs-original-title]")
    language = lang.attributes.get("data-bs-original-title") if lang else None

    return ProductOffer(
        seller=seller,
        sales=sales,
        condition=condition,
        language=language,
        price=_parse_price(row),
    )


def _parse_price(row: Node) -> Decimal | None:
    span = row.css_first(".price-container span.fw-bold")
    if span is None:
        return None
    m = _RE_PRIX.search(span.text(strip=True).replace("€", ""))
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None
