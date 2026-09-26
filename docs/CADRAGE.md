# Cadrage — MKM Optimizer

Le POURQUOI du projet. Pour le COMMENT (composants, flux, modèle MIP), voir
[ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Pitch

Outil en ligne de commande **à usage strictement personnel** qui, à partir d'une
wantlist Cardmarket et d'une liste de vendeurs, produit des paniers d'achat
optimisés minimisant le **coût total** (prix des cartes + frais de port). Il
gère trois choses que le Shopping Wizard natif de Cardmarket traite mal :

1. Le **split de quantité** entre vendeurs (2× chez A + 1× chez B si c'est
   globalement plus économique en tenant compte des FDP).
2. Le **mélange d'éditions** sur les metacards (édition indifférente) : la même
   carte peut venir de plusieurs sets si c'est le moins cher.
3. Les **scénarios multiples** (min vendeurs vs min prix vs équilibre) et les
   frais de port en paliers.

---

## 2. Objectifs & périmètre

**Dans le périmètre (V1)**
- Parsing d'une wantlist MKM (HTML) et de pages d'offres vendeurs (HTML).
- Optimisation exacte du panier par scénario (MIP PuLP + CBC).
- Rapports lisibles Markdown + CSV dans `reports/`.
- Récupération semi-automatique des offres via Playwright (`login` + `fetch`).
- Vérification a posteriori d'un panier construit sur le site (`check-cart`).

**Hors périmètre (V1)**
- Utilisation de l'API officielle MKM (réservée aux vendeurs professionnels).
- Achat / ajout automatique au panier sur le site.
- Scraping des frais de port réels par vendeur (barème global uniquement).
- Découverte automatique de vendeurs (liste maintenue à la main).
- Heuristique de fallback pour très grands volumes.

---

## 3. Contraintes (fermes)

| Contrainte | Détail |
|---|---|
| Accès données | Pas d'API officielle MKM ; scraping HTML uniquement, à usage personnel. |
| Conformité | Rate limit respectueux, aucun contournement de CAPTCHA / anti-bot. |
| Exécution | Locale, sur poste personnel (Chromium headed via WSLg sur Windows 11). |
| Secrets | Credentials et session jamais versionnés (`.env`, `.auth/` gitignored). |
| Reproductibilité | Solveur déterministe (CBC) : même entrée → même sortie. |

---

## 4. Hypothèses

- **Filtres par-want, pas globaux** : chaque want de la wantlist MKM porte ses
  propres contraintes (état minimum, langues, foil), parsées depuis la page. Un
  filtre global unique produirait des faux négatifs. Ce qui la remettrait en
  cause : une wantlist sans filtres exploitables côté HTML.
- **Barème de FDP unique** : tous les vendeurs partagent les mêmes paliers
  (`config.yaml`), faute de scraper les FDP réels. Vrai pour un usage FR
  homogène ; faux si mélange de pays / vendeurs aux grilles très différentes.
- **Optimum exact atteignable** : le volume typique (~100-150 wants, quelques
  milliers d'offres) reste dans les capacités de CBC en < 60 s. Au-delà,
  hypothèse caduque → heuristique nécessaire.
- **Variantes d'art interchangeables** : les suffixes MKM `(V.1)`, `(V.2)` sont
  ignorés (même carte Magic). Faux si l'utilisateur veut une variante précise.
- **Session courte** : hypothèse initiale d'un `storage_state` valide ~30 jours,
  démentie à l'usage — le cookie `idUser` importé expire en ~1 h. En pratique on
  réexporte les cookies du navigateur juste avant chaque campagne de `fetch`
  (`import-cookies`). Un fetch complet (~70 vendeurs) tient dans cette fenêtre.

---

## 5. Stack technique

| Brique | Choix | Licence |
|---|---|---|
| Langage | Python ≥ 3.11 | PSF |
| Solveur MIP | PuLP + CBC | MIT / EPL-2.0 |
| Parsing HTML | selectolax | MIT |
| Modèles | Pydantic | MIT |
| CLI | Typer + Rich | MIT |
| Config | PyYAML | MIT |
| Credentials | python-dotenv | BSD-3-Clause |
| Scraping | Playwright (Chromium) | Apache-2.0 |

Détail des versions dans [ARCHITECTURE.md](ARCHITECTURE.md#3-stack-technologique).

---

## 6. Décisions

**Décisions figées**
- Optimisation **exacte** (MIP) plutôt qu'heuristique, parce que le volume le
  permet et que l'optimum est vérifiable.
- Contraintes de compatibilité **par want** plutôt que filtres globaux, parce que
  le global créait des faux négatifs.
- HTML **sauvegardé sur disque** puis parsé, plutôt que parse-à-la-volée, pour
  rejouer et déboguer sans re-scraper.
- **selectolax** plutôt que BeautifulSoup, pour la vitesse sur des pages lourdes.

**À trancher**
- FDP par vendeur (scraping des grilles réelles) — reco : viser une V2 si le
  barème unique devient limitant.
- Heuristique de fallback au-delà de `exact_threshold` — reco : ne l'implémenter
  que si un cas réel dépasse les seuils.
- Nettoyage des clés `filters.*` inertes de `config.yaml` — reco : documenter ou
  retirer pour éviter la confusion.

---

## 7. Roadmap

0. Modèles de données + parsers wantlist / offres (socle).
1. Solveur MIP exact + scénarios + rapports MD/CSV.
2. Scraping Playwright (`login` + `fetch` paginé avec resume).
3. Vérification de panier (`check-cart`).
4. (Ouvert) FDP par vendeur, heuristique de fallback, découverte de vendeurs.

---

## 8. Stratégie de tests

Aujourd'hui, deux scripts *smoke* (non pytest) dans
[tests/](../tests) :
- [smoke_mip.py](../tests/smoke_mip.py) : un cas synthétique (3 vendeurs
  construits pour forcer un split, solution vérifiable) + un run sur données
  réelles pour vérifier la cohérence.
- [smoke_parsers.py](../tests/smoke_parsers.py) : applique les deux parsers à des
  HTMLs réels et compte les entrées.

Piste : convertir ces scripts en suite pytest (dépendance `pytest` déjà déclarée
dans l'extra `dev`).

---

## 9. Références

- Cardmarket (MKM) — source des données, soumise aux conditions de la plateforme.
- Shopping Wizard natif de Cardmarket — état de l'art fonctionnel que l'outil
  cherche à dépasser sur le split de quantité et le mélange d'éditions.
