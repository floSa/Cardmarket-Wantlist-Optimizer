"""
Parser de la page produit d'une carte (toutes offres, tous vendeurs).

URL type : https://www.cardmarket.com/fr/Magic/Cards/<Slug>?sellerCountry=<n>

C'est l'inverse d'une page d'offres vendeur : une ligne = une offre d'UN
vendeur pour LA carte de la page (vendeur explicite par ligne, carte
implicite = celle de la page, constante).

Sert à compléter le stock de vendeurs déjà suivis sans relancer un fetch
complet — typiquement quand on a élargi le prix max d'un want sur MKM (le
fetch normal filtre côté serveur via `?idWantslist=`, donc les offres
au-dessus de l'ancien prix max n'ont jamais été scrapées) et qu'on veut
juste injecter les nouvelles offres visibles pour cette carte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from selectolax.parser import HTMLParser, Node

from ..models import Condition, Foil, Offer
from ..selectors import (
    OFFER_ROW,
    OFFER_CONDITION_A,
    OFFER_LANG_SPAN,
    OFFER_PRICE,
    OFFER_AMOUNT,
    OFFER_ROW_ID_PREFIX,
    parse_condition,
    parse_language,
)
from .seller_offers import _has_attribute_icon, _parse_price


_CARD_SELLER_A = ".col-seller a[href*='/Users/']"
_RE_SET_FROM_EXPANSION_URL = re.compile(r"/Expansions/([^/?]+)")


@dataclass(frozen=True)
class CardOffersPage:
    card_name: str
    product_url: str
    offers: list[Offer]


def parse_card_offers(html: str | Path) -> CardOffersPage:
    """
    Parse une page /Cards/<Slug> (SingleFile) et retourne toutes les offres,
    tous vendeurs confondus (Offer.seller varie par ligne).
    """
    if isinstance(html, Path):
        html = html.read_text(encoding="utf-8", errors="ignore")
    tree = HTMLParser(html)

    card_name, product_url = _detect_card(tree)

    offers: list[Offer] = []
    for row in tree.css(OFFER_ROW):
        o = _parse_card_row(row, card_name, product_url)
        if o is not None:
            offers.append(o)
    return CardOffersPage(card_name=card_name, product_url=product_url, offers=offers)


def _detect_card(tree: HTMLParser) -> tuple[str, str]:
    canon = tree.css_first("link[rel=canonical]")
    product_url = (canon.attributes.get("href") or "").strip() if canon else ""
    title_node = tree.css_first("title")
    title = (title_node.text(strip=True) if title_node else "") or ""
    # Titre MKM : "<Nom carte> - Cartes MTG | Cardmarket"
    card_name = title.split(" - ")[0].strip() if title else ""
    if not card_name or not product_url:
        raise ValueError(
            "Impossible de déterminer la carte de cette page "
            "(canonical / <title> manquants ou inattendus). "
            "Est-ce bien une page /Cards/<Slug> sauvegardée avec SingleFile ?"
        )
    return card_name, product_url


def _parse_card_row(row: Node, card_name: str, product_url: str) -> Offer | None:
    raw_id = row.attributes.get("id", "")
    article_id = (
        raw_id[len(OFFER_ROW_ID_PREFIX):] if raw_id.startswith(OFFER_ROW_ID_PREFIX) else None
    )

    seller_a = row.css_first(_CARD_SELLER_A)
    if seller_a is None:
        return None
    seller = (seller_a.text(strip=True) or "").strip()
    if not seller:
        return None

    set_a = row.css_first(".product-attributes a.expansion-symbol")
    set_label = (
        (set_a.attributes.get("data-bs-original-title") or set_a.attributes.get("aria-label") or "").strip()
        if set_a is not None
        else ""
    )
    set_code: str | None = None
    if set_a is not None:
        m = _RE_SET_FROM_EXPANSION_URL.search(set_a.attributes.get("href") or "")
        if m:
            set_code = m.group(1)

    cond_a = row.css_first(OFFER_CONDITION_A)
    cond_label = cond_a.attributes.get("data-bs-original-title") if cond_a else None
    condition = parse_condition(cond_label)
    if condition is None and cond_a is not None:
        for cls in (cond_a.attributes.get("class") or "").split():
            if cls.startswith("condition-"):
                try:
                    condition = Condition(cls.split("-", 1)[1].upper())
                except ValueError:
                    pass
    if condition is None:
        return None

    lang_span = row.css_first(OFFER_LANG_SPAN)
    lang_label = lang_span.attributes.get("data-bs-original-title") if lang_span else None
    language = parse_language(lang_label) or "??"

    attrs_block = row.css_first(".product-attributes")
    foil_flag = _has_attribute_icon(attrs_block, {"Foil", "Reverse Holo", "Reverse Holographique"})
    signed_flag = _has_attribute_icon(attrs_block, {"Signed", "Signé"})
    altered_flag = _has_attribute_icon(attrs_block, {"Altered", "Altérée"})
    foil = Foil.YES if foil_flag else Foil.NO

    price_span = row.css_first(OFFER_PRICE)
    price = _parse_price(price_span.text(strip=True) if price_span else "")
    if price is None:
        return None

    amount_span = row.css_first(OFFER_AMOUNT)
    try:
        quantity = int((amount_span.text(strip=True) if amount_span else "1") or "1")
    except ValueError:
        quantity = 1

    return Offer(
        seller=seller,
        card_name=card_name,
        product_url=product_url,
        set_label=set_label,
        set_code=set_code,
        condition=condition,
        language=language,
        foil=foil,
        is_signed=signed_flag,
        is_altered=altered_flag,
        price=price,
        quantity_available=quantity,
        article_id=article_id,
        comment=None,
    )
