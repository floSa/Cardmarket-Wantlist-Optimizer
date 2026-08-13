"""
Filtres "durs" appliqués aux offres avant l'optimisation.

⚠️ Ce module ne fait PLUS de filtrage sur min_condition/languages/foil au
niveau global, parce que ça créait des FAUX NÉGATIFS : une carte avec
`min_condition: GD` côté want était bloquée par un `min_condition: EX`
global, alors qu'elle était parfaitement valide pour ce want précis.

Les contraintes par-want sont gérées plus finement par
`optimizer.compat.is_compatible` au moment de la construction du MIP.

Seuls les filtres TRULY globaux (insensibles au want) sont appliqués ici :
  - `excluded_sellers` : pseudos avec qui on refuse de traiter (litige passé)
  - éventuellement plus tard : seller_country/seller_type/min_reputation
    quand on récupérera ces métadonnées (TODO)
"""

from __future__ import annotations

from decimal import Decimal

from .models import Offer


def filter_offers(
    offers: list[Offer],
    excluded_sellers: list[str] | None = None,
    min_condition: str | None = None,    # ignoré, conservé pour compat de signature
    languages: list[str] | None = None,  # ignoré, idem
    foil: str | None = None,             # ignoré, idem
    max_offer_price: Decimal | float | str | None = None,
) -> list[Offer]:
    """
    Pré-filtre global. Filtre :
      - `excluded_sellers` : pseudos avec qui on refuse de traiter
      - `max_offer_price`  : écarte toute offre dont le prix unitaire dépasse
        ce plafond (en €). Sert à exclure les listings « poubelle » à prix
        aberrant (ex. une commune affichée à 1000 €) qui, sous contrainte de
        vendeurs, polluent la solution. None = pas de plafond.

    Les params min_condition/languages/foil sont conservés dans la signature
    pour ne pas casser le CLI et `config.yaml` existants, mais ils sont
    volontairement ignorés (le matching fin est fait par compat.is_compatible).
    """
    excluded = {s.lower() for s in (excluded_sellers or [])}
    cap = Decimal(str(max_offer_price)) if max_offer_price is not None else None

    def keep(o: Offer) -> bool:
        if o.seller.lower() in excluded:
            return False
        if cap is not None and o.price > cap:
            return False
        return True

    return [o for o in offers if keep(o)]


def drop_price_outliers(
    offers: list[Offer],
    ratio: Decimal | float | str | None,
    floor: Decimal | float | str = "1.50",
) -> tuple[list[Offer], list[tuple[Offer, Decimal]]]:
    """
    Écarte les offres à prix aberrant : celles qui coûtent à la fois PLUS de
    `floor` euros ET plus de `ratio` fois le prix le plus bas constaté pour la
    MÊME carte, tous vendeurs confondus.

    Motivation : le solveur retient parfois une offre très au-dessus du marché
    parce que le vendeur est déjà dans le panier (le port est déjà payé). C'est
    arithmétiquement juste — 4,00 € chez un vendeur ouvert battent 0,99 € + 4,10 €
    de port — mais inacceptable en pratique.

    Les DEUX conditions sont nécessaires. Le ratio seul écarterait une commune
    passant de 0,02 € à 0,10 € (×5 mais 8 centimes d'écart, sans importance) ;
    le plancher seul écarterait des cartes chères mais correctement tarifées.
    Ensemble, ils ne visent que ce qui fait mal : une carte à plus de 1,50 €
    payée au double de sa valeur.

    Le repère est le MINIMUM et non la médiane : c'est ainsi qu'on juge
    spontanément qu'un prix est aberrant (« ça vaut 1 €, il la vend 4 € »).

    Retourne (offres_gardées, [(offre_écartée, prix_mini_de_la_carte), …]).
    """
    if ratio is None:
        return list(offers), []

    r = Decimal(str(ratio))
    f = Decimal(str(floor))

    mini: dict[str, Decimal] = {}
    for o in offers:
        k = o.card_key
        if k not in mini or o.price < mini[k]:
            mini[k] = o.price

    gardees: list[Offer] = []
    ecartees: list[tuple[Offer, Decimal]] = []
    for o in offers:
        m = mini[o.card_key]
        if o.price >= f and o.price > m * r:
            ecartees.append((o, m))
        else:
            gardees.append(o)
    return gardees, ecartees
