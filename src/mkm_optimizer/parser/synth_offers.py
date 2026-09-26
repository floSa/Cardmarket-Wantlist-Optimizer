"""
Génère une page HTML minimale, mais structurellement identique aux vraies
pages `/Users/<pseudo>/Offers/Singles`, à partir d'une liste d'`Offer` déjà
parsée ailleurs (typiquement `card_offers.parse_card_offers`).

Pourquoi : l'architecture du projet fait TOUT transiter par des fichiers HTML
sur disque (cf. docs/ARCHITECTURE.md) — `optimize` ne sait lire que des
`data/sellers/<pseudo>/page*.html`. Plutôt que de contourner ce pipeline avec
un cache JSON parallèle, on réinjecte les offres "manuelles" sous la même
forme, ce qui les rend auditables comme n'importe quelle page scrapée et les
fait bénéficier gratuitement de la dédup par `article_id` déjà en place dans
`parse_seller_offers_dir`.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape
from pathlib import Path

from ..models import Foil, Offer


# Inverse (partiel) de selectors.LANG_FROM_LABEL — juste de quoi ré-émettre un
# libellé FR reconnu par `parse_language` pour les langues qu'on rencontre.
_LABEL_FROM_LANG: dict[str, str] = {
    "en": "Anglais", "fr": "Français", "de": "Allemand", "es": "Espagnol",
    "it": "Italien", "ja": "Japonais", "pt": "Portugais", "ru": "Russe",
    "ko": "Coréen", "zh-Hans": "Chinois simplifié", "zh-Hant": "Chinois traditionnel",
    "nl": "Néerlandais", "pl": "Polonais", "cs": "Tchèque", "hu": "Hongrois",
}


def _fmt_price(p: Decimal) -> str:
    return f"{p:.2f}".replace(".", ",")


def _row_html(offer: Offer, index: int) -> str:
    article_id = offer.article_id or f"manual-{index}"
    lang_label = _LABEL_FROM_LANG.get(offer.language, offer.language)
    set_href = (
        f"https://www.cardmarket.com/fr/Magic/Expansions/{escape(offer.set_code)}"
        if offer.set_code
        else "#"
    )

    attr_icons = ""
    if offer.foil == Foil.YES:
        attr_icons += '<span data-bs-original-title="Foil" aria-label="Foil"></span>'
    if offer.is_signed:
        attr_icons += '<span data-bs-original-title="Signé" aria-label="Signé"></span>'
    if offer.is_altered:
        attr_icons += '<span data-bs-original-title="Altérée" aria-label="Altérée"></span>'

    return f"""
<div id="articleRow{escape(article_id)}" class="row g-0 article-row">
  <div class="col-seller"><a href="{escape(offer.product_url)}">{escape(offer.card_name)}</a></div>
  <div class="product-attributes">
    <a href="{set_href}" class="expansion-symbol" data-bs-original-title="{escape(offer.set_label)}"
       aria-label="{escape(offer.set_label)}"></a>
    <a href="#" class="article-condition condition-{offer.condition.value.lower()}"></a>
    <span class="icon" data-bs-original-title="{escape(lang_label)}"></span>
    {attr_icons}
  </div>
  <div class="price-container"><span class="fw-bold">{_fmt_price(offer.price)} &euro;</span></div>
  <div class="amount-container"><span class="item-count">{offer.quantity_available}</span></div>
</div>"""


def write_synthetic_seller_page(seller: str, offers: list[Offer], out_path: Path) -> None:
    """
    Écrit une page HTML autonome contenant les `offers` (déjà filtrées pour
    ce `seller`), au format attendu par `parser.seller_offers.parse_seller_offers`.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(_row_html(o, i) for i, o in enumerate(offers))
    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Offres manuelles — {escape(seller)}</title>
<!-- Généré par `mkm-optim import-card-offers`, pas une vraie page MKM. -->
</head><body>
{rows}
</body></html>"""
    out_path.write_text(html, encoding="utf-8")
