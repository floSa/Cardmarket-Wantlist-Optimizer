"""
Modèles de données typés.

Conventions :
- Tous les prix sont en euros (Decimal). On évite float pour éviter les pertes
  d'arrondi à la sommation sur des dizaines d'articles.
- Les enums utilisent les noms courts MKM (NM, EX, GD…).
- `set_code = None`  → metacard (édition indifférente).
- Les noms de cartes sont normalisés en lower-case ASCII pour le matching
  wantlist ↔ offres, mais le label affiché préserve la casse d'origine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional


# --- Énumérations métier MKM -------------------------------------------------

class Condition(str, Enum):
    MT = "MT"   # Mint
    NM = "NM"   # Near Mint
    EX = "EX"   # Excellent
    GD = "GD"   # Good
    LP = "LP"   # Light Played
    PL = "PL"   # Played
    PO = "PO"   # Poor

    @property
    def rank(self) -> int:
        """Plus le rang est petit, meilleur est l'état (MT=0)."""
        return list(Condition).index(self)

    def at_least(self, other: "Condition") -> bool:
        """True si self est au moins aussi bon que `other`."""
        return self.rank <= other.rank


class Foil(str, Enum):
    YES = "yes"
    NO = "no"
    ANY = "any"


# --- Modèles de données -------------------------------------------------------

@dataclass(frozen=True)
class WantEntry:
    """Une ligne de la wantlist MKM."""
    card_name: str                  # "Abrise, première année éloquente"
    product_url: str                # URL Cardmarket (peut être metacard /Cards/ ou /Products/Singles/<set>/)
    quantity: int                   # nombre d'exemplaires voulus
    set_code: Optional[str]         # None = metacard (édition indifférente)
    set_label: Optional[str]        # label affiché ("Indifférent" ou "Magic 2013")
    min_condition: Condition
    languages: list[str] = field(default_factory=list)   # codes ISO ["fr","en"]
    foil: Foil = Foil.ANY
    is_signed: Optional[bool] = None
    is_altered: Optional[bool] = None
    max_price: Optional[Decimal] = None                  # "Prix souhaité" — Decimal ou None
    priority: float = 1.0                                # Coefficient de priorité (défaut: 1.0)
    notes: Optional[str] = None                          # Commentaire optionnel utilisateur

    @property
    def is_metacard(self) -> bool:
        return self.set_code is None

    @property
    def key(self) -> str:
        """Clé stable pour identifier un want dans le rapport."""
        return f"{self.card_name}|{self.set_code or 'ANY'}"


@dataclass(frozen=True)
class Offer:
    """Une offre individuelle chez un vendeur."""
    seller: str                     # pseudo MKM
    card_name: str                  # nom de la carte tel qu'affiché
    product_url: str                # URL de la fiche produit
    set_label: str                  # nom du set affiché ("Septième Edition")
    set_code: Optional[str]         # slug stable extrait de l'URL ("Seventh-Edition")
    condition: Condition
    language: str                   # code ISO 2 lettres ("fr")
    foil: Foil
    is_signed: bool
    is_altered: bool
    price: Decimal                  # prix unitaire en euros
    quantity_available: int         # stock disponible chez ce vendeur pour cette offre
    article_id: Optional[str] = None    # id MKM (articleRow<id>) — utile pour add-to-cart direct
    comment: Optional[str] = None       # commentaire vendeur ("TES 4-53"…)

    @property
    def card_key(self) -> str:
        """Clé carte (sans set, sans état) pour grouper par carte."""
        return self.card_name


@dataclass
class Seller:
    """Métadonnées d'un vendeur."""
    name: str
    country: Optional[str] = None
    seller_type: Optional[str] = None       # "private" | "commercial" | "powerseller"
    reputation: Optional[float] = None      # /5
    sales_count: Optional[int] = None
    profile_url: Optional[str] = None


# --- Modèles d'optimisation ---------------------------------------------------

@dataclass
class ShippingBracket:
    """Un palier de FDP : `cost` € si le nb total de cartes ≤ `max_cards`."""
    max_cards: int
    cost: Decimal


@dataclass
class Assignment:
    """Une attribution dans la solution : N exemplaires de cette offre."""
    offer: Offer
    quantity: int

    @property
    def line_total(self) -> Decimal:
        return self.offer.price * self.quantity


@dataclass
class VendorBasket:
    """Le panier final chez un vendeur."""
    seller: str
    assignments: list[Assignment]
    shipping_cost: Decimal

    @property
    def cards_subtotal(self) -> Decimal:
        return sum((a.line_total for a in self.assignments), Decimal("0"))

    @property
    def total_units(self) -> int:
        return sum(a.quantity for a in self.assignments)

    @property
    def grand_total(self) -> Decimal:
        return self.cards_subtotal + self.shipping_cost


@dataclass
class Solution:
    """Résultat complet d'un scénario d'optimisation."""
    scenario_name: str
    baskets: list[VendorBasket]
    unmet_wants: list[WantEntry]    # cartes qu'on n'a pas pu couvrir

    @property
    def cards_total(self) -> Decimal:
        return sum((b.cards_subtotal for b in self.baskets), Decimal("0"))

    @property
    def shipping_total(self) -> Decimal:
        return sum((b.shipping_cost for b in self.baskets), Decimal("0"))

    @property
    def grand_total(self) -> Decimal:
        return self.cards_total + self.shipping_total

    @property
    def vendor_count(self) -> int:
        return len(self.baskets)
