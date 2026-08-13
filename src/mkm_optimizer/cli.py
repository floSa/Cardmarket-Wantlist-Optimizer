"""
CLI MKM Optimizer.

Commandes :
  optimize     run principal : wantlist + dossier HTMLs vendeurs → rapports MD/CSV
  parse        debug : affiche le contenu parsé d'une page MKM (wantlist OU vendeur)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .filters import drop_price_outliers, filter_offers
from .models import Offer, Solution, WantEntry
from .optimizer.mip import brackets_from_config, solve
from .parser import parse_seller_offers, parse_seller_offers_dir, parse_wantlist
from .parser.wantlist import parse_wantlist_meta
from .overrides import apply_wantlist_overrides
from .reporter import write_reports
from .wantlist_export import write_wantlist_csv


app = typer.Typer(
    name="mkm-optim",
    help="Optimiseur d'achats Cardmarket (MKM) — minimise coût total cartes + FDP.",
    add_completion=False,
)
console = Console()


# ---- Commande : optimize ----------------------------------------------------

@app.command()
def optimize(
    wantlist: Path = typer.Option(
        ..., "--wantlist", "-w", exists=True, dir_okay=False, file_okay=True, readable=True,
        help="HTML de la page wantlist Cardmarket (/fr/Magic/Wants/<id>).",
    ),
    sellers_dir: Path = typer.Option(
        ..., "--sellers-dir", "-s", exists=True, file_okay=False, dir_okay=True, readable=True,
        help="Dossier contenant les HTMLs de vendeurs (un fichier par vendeur).",
    ),
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", exists=True, readable=True,
        help="Fichier de configuration YAML.",
    ),
    output_dir: Path = typer.Option(
        Path("reports"), "--output-dir", "-o",
        help="Dossier de sortie pour les rapports MD/CSV.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Lance l'optimisation et écrit les rapports."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s :: %(message)s",
    )

    cfg = load_config(config)
    brackets = brackets_from_config(cfg["shipping"]["brackets"])
    filt = cfg.get("filters", {})

    # --- Wantlist
    console.print(f"[bold]→ Wantlist[/bold] : {wantlist}")
    wants = parse_wantlist(wantlist)
    meta = parse_wantlist_meta(wantlist)
    title = meta.get("title")
    # Overrides locaux (wantlist_overrides.yaml à la racine du projet)
    overrides_path = Path("wantlist_overrides.yaml")
    wants = apply_wantlist_overrides(wants, overrides_path)
    # `filters.foil: any` neutralise la contrainte foil de TOUS les wants : on
    # cherche le prix le plus bas, foil ou non. La clé est portée par le want
    # (et non par l'offre), d'où la réécriture ici.
    if str(filt.get("foil", "")).lower() == "any":
        import dataclasses

        from .models import Foil

        wants = [dataclasses.replace(w, foil=Foil.ANY) for w in wants]
        console.print("  [dim]foil: any — la contrainte foil des wants est ignorée[/dim]")
    console.print(
        f"  {len(wants)} wants / {sum(w.quantity for w in wants)} cartes"
        f" — titre : {title!r}"
    )

    # --- Offres : on accepte 2 layouts dans sellers_dir
    #   layout A (legacy, 1 fichier par vendeur) : data/sellers/<pseudo>.html
    #   layout B (paginatée, écrite par `fetch`) : data/sellers/<pseudo>/page1.html, page2.html…
    console.print(f"[bold]→ Vendeurs[/bold] : {sellers_dir}")
    raw_offers: list[Offer] = []
    sellers_seen: list[str] = []
    sources: list[tuple[str, Path]] = []
    for sub in sorted(sellers_dir.iterdir()):
        if sub.is_dir():
            sources.append(("dir", sub))
        elif sub.is_file() and sub.suffix == ".html":
            sources.append(("file", sub))

    for kind, path in sources:
        try:
            if kind == "dir":
                seller, offers = parse_seller_offers_dir(path)
            else:
                seller, offers = parse_seller_offers(path)
        except Exception as e:
            console.print(f"  [yellow]⚠[/yellow] {path.name} : ignoré ({e})")
            continue
        sellers_seen.append(seller)
        raw_offers.extend(offers)
        suffix = "" if kind == "file" else f" ({sum(1 for _ in path.glob('page*.html'))} pages)"
        console.print(f"  · {seller:<20s}  {len(offers):>4d} offres{suffix}")
    console.print(
        f"  TOTAL : {len(sellers_seen)} vendeur(s), {len(raw_offers)} offres brutes"
    )
    if not raw_offers:
        console.print("[red]Aucune offre à optimiser — abandon.[/red]")
        raise typer.Exit(code=2)

    # --- Filtrage global
    offers = filter_offers(
        raw_offers,
        excluded_sellers=filt.get("excluded_sellers", []),
        min_condition=filt.get("min_condition"),
        languages=filt.get("languages"),
        foil=filt.get("foil"),
        max_offer_price=filt.get("max_offer_price"),
    )
    console.print(
        f"  Après filtres globaux : {len(offers)} offres "
        f"({len(raw_offers) - len(offers)} écartées)"
    )

    # --- Prix aberrants : coupe les offres très au-dessus du marché, que le
    #     solveur retiendrait sinon pour économiser un port.
    offers, aberrantes = drop_price_outliers(
        offers,
        filt.get("max_price_ratio"),
        floor=filt.get("max_price_floor", "1.50"),
    )
    if aberrantes:
        console.print(
            f"  Prix aberrants écartés : {len(aberrantes)} offres "
            f"(≥ {filt.get('max_price_floor', 1.50)} € ET > "
            f"{filt['max_price_ratio']}× le prix mini de la carte)"
        )
        if verbose:
            for o, mini in sorted(aberrantes, key=lambda x: -(x[0].price / x[1]))[:15]:
                console.print(
                    f"      {o.card_name} — {o.price} € chez {o.seller} "
                    f"(mini {mini} €, ×{o.price / mini:.1f})"
                )

    # --- Optimisation par scénario
    from decimal import Decimal
    solutions: list[Solution] = []
    for sc in cfg["optimization"]["scenarios"]:
        vendor_fixed_cost = Decimal(str(sc.get("vendor_fixed_cost", 0)))
        sol = solve(
            wants=wants,
            offers=offers,
            brackets=brackets,
            max_vendors=sc.get("max_vendors"),
            scenario_name=sc["name"],
            vendor_fixed_cost=vendor_fixed_cost,
        )
        solutions.append(sol)
        _print_scenario_summary(sol)

    # --- Écriture rapports
    md_path, csv_path = write_reports(solutions, wants, out_dir=output_dir, title=title)
    # Wantlist CSV à côté, pour audit/consultation
    from datetime import datetime
    stamp = datetime.now().strftime("%Y_%m_%d_%H-%M")
    wantlist_csv_path = output_dir / f"{stamp}_WantList.csv"
    write_wantlist_csv(wants, wantlist_csv_path)

    console.print(f"\n[bold green]✓ Rapport MD[/bold green]   : {md_path}")
    console.print(f"[bold green]✓ Rapport CSV[/bold green]  : {csv_path}")
    console.print(f"[bold green]✓ Wantlist CSV[/bold green] : {wantlist_csv_path}")


def _print_scenario_summary(sol: Solution) -> None:
    t = Table(title=f"Scénario : {sol.scenario_name}", show_lines=False, expand=False)
    t.add_column("Vendeur", style="cyan", no_wrap=True)
    t.add_column("Cartes", justify="right")
    t.add_column("Sous-total", justify="right")
    t.add_column("FDP", justify="right")
    t.add_column("Total", justify="right", style="bold")
    for b in sol.baskets:
        t.add_row(
            b.seller,
            str(b.total_units),
            f"{b.cards_subtotal:.2f} €",
            f"{b.shipping_cost:.2f} €",
            f"{b.grand_total:.2f} €",
        )
    t.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{sum(b.total_units for b in sol.baskets)}[/bold]",
        f"[bold]{sol.cards_total:.2f} €[/bold]",
        f"[bold]{sol.shipping_total:.2f} €[/bold]",
        f"[bold]{sol.grand_total:.2f} €[/bold]",
    )
    console.print(t)
    if sol.unmet_wants:
        miss = sum(w.quantity for w in sol.unmet_wants)
        console.print(
            f"[yellow]⚠ {len(sol.unmet_wants)} wants non couverts ({miss} cartes)[/yellow]"
        )


# ---- Commande : parse (debug) -----------------------------------------------

@app.command()
def parse(
    html: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    kind: str = typer.Option(
        "auto", "--kind", "-k",
        help="auto | wantlist | seller",
    ),
) -> None:
    """Parse un HTML et affiche le contenu (debug)."""
    if kind == "auto":
        # Heuristique : la wantlist contient "WantsListTable", les offres contiennent "article-row".
        text = html.read_text(encoding="utf-8", errors="ignore")
        kind = "wantlist" if "WantsListTable" in text else "seller"

    if kind == "wantlist":
        wants = parse_wantlist(html)
        meta = parse_wantlist_meta(html)
        console.print(f"[bold]Wantlist[/bold] {meta.get('title')!r} — {meta.get('header_raw')}")
        console.print(f"Parsés : {len(wants)} wants / {sum(w.quantity for w in wants)} cartes")
        for w in wants[:30]:
            console.print(
                f"  qty={w.quantity:>2d}  {w.card_name!r:<55s}  "
                f"set={(w.set_label or 'ANY'):<25s}  ≥{w.min_condition.value}  langs={w.languages}"
            )
    elif kind == "seller":
        seller, offers = parse_seller_offers(html)
        console.print(f"[bold]Vendeur[/bold] : {seller!r} — {len(offers)} offres")
        for o in offers[:30]:
            console.print(
                f"  {o.card_name!r:<55s}  set={o.set_label:<25s}  "
                f"{o.condition.value} {o.language}  {o.price} € x{o.quantity_available}"
            )
    else:
        raise typer.BadParameter(f"kind inconnu : {kind!r}")


# ---- Commande : login (headed) ----------------------------------------------

@app.command()
def login(
    headless: bool = typer.Option(
        False, "--headless/--headed",
        help="Headless seulement si tu sais qu'aucun CAPTCHA n'apparaît. Par défaut headed.",
    ),
) -> None:
    """
    Connexion à Cardmarket et sauvegarde de la session dans .auth/storage_state.json.

    - Si .env contient CARDMARKET_USER et CARDMARKET_PASS → pré-remplit les champs
      automatiquement, tu n'as plus qu'à cliquer 'Login' (+ CAPTCHA éventuel).
    - Sinon → mode interactif : tu tapes tout dans la fenêtre Chromium.
    """
    from .scraper.auth import login as _login

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s :: %(message)s")
    _login(headless=headless)


# ---- Commande : fetch -------------------------------------------------------

@app.command()
def fetch(
    sellers_file: Path = typer.Option(
        Path("data/vendeurs_liste/vendeurs.yaml"), "--sellers-file", "-f",
        exists=True, dir_okay=False, readable=True,
        help="YAML listant wantlist_id + sellers: [...].",
    ),
    output_dir: Path = typer.Option(
        Path("data/sellers"), "--output-dir", "-o",
        help="Racine où écrire <pseudo>/page<N>.html pour chaque vendeur.",
    ),
    refresh: bool = typer.Option(
        False, "--refresh",
        help="Force le re-fetch même si des HTMLs sont déjà en cache.",
    ),
    headless: bool = typer.Option(
        True, "--headless/--headed",
        help="Mode headless (par défaut) ou headed (pour debug visuel).",
    ),
    min_delay: int = typer.Option(800, "--min-delay-ms"),
    max_delay: int = typer.Option(1500, "--max-delay-ms"),
    only: Optional[str] = typer.Option(
        None, "--only",
        help="Ne traiter que ce vendeur (utile pour tester sur 1 cas).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """
    Récupère les offres paginées de chaque vendeur de `data/vendeurs_liste/vendeurs.yaml`
    (filtrées par ta wantlist via le paramètre natif MKM `?idWantslist=...`).

    Nécessite d'avoir lancé `mkm-optim login` au préalable.
    """
    import yaml
    from .scraper.fetch import FetchOptions, fetch_all_sellers

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s :: %(message)s",
    )

    cfg = yaml.safe_load(sellers_file.read_text(encoding="utf-8"))
    wantlist_id = int(cfg["wantlist_id"])
    sellers: list[str] = list(cfg["sellers"])
    if only:
        if only not in sellers:
            console.print(f"[yellow]⚠ {only!r} pas dans la liste, on tente quand même[/yellow]")
        sellers = [only]
    console.print(
        f"[bold]→ Fetch[/bold] wantlist={wantlist_id}  vendeurs={len(sellers)}  "
        f"rate={min_delay}-{max_delay} ms  output={output_dir}"
    )

    opts = FetchOptions(
        wantlist_id=wantlist_id,
        output_dir=output_dir,
        min_delay_ms=min_delay,
        max_delay_ms=max_delay,
        refresh=refresh,
    )
    stats = fetch_all_sellers(sellers, opts, headless=headless)

    # Récap
    t = Table(title="Récap fetch", show_lines=False)
    t.add_column("Vendeur", style="cyan")
    t.add_column("Pages", justify="right")
    t.add_column("Offres ~", justify="right")
    t.add_column("Durée", justify="right")
    t.add_column("Statut")
    for s in stats:
        if s.skipped:
            status = "[yellow]skip (cache)[/yellow]"
        elif s.error:
            status = f"[red]{s.error}[/red]"
        else:
            status = "[green]ok[/green]"
        t.add_row(
            s.seller,
            str(s.pages_fetched),
            str(s.total_results_announced or "?"),
            f"{s.duration_seconds:.1f}s",
            status,
        )
    console.print(t)


# ---- Commande : wantlist-csv (export standalone) ----------------------------

@app.command("wantlist-csv")
def wantlist_csv(
    wantlist: Path = typer.Option(
        ..., "--wantlist", "-w", exists=True, dir_okay=False, readable=True,
        help="HTML de la page wantlist Cardmarket.",
    ),
    output: Path = typer.Option(
        Path("reports/wantlist.csv"), "--output", "-o",
        help="Chemin du CSV à écrire.",
    ),
) -> None:
    """
    Parse une wantlist HTML et écrit un CSV lisible (utile pour audit
    indépendamment d'une optimisation).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s :: %(message)s")
    wants = parse_wantlist(wantlist)
    meta = parse_wantlist_meta(wantlist)
    write_wantlist_csv(wants, output)
    console.print(
        f"[bold green]✓[/bold green] Wantlist [bold]{meta.get('title')!r}[/bold] "
        f"({len(wants)} wants / {sum(w.quantity for w in wants)} cartes) "
        f"→ {output}"
    )


# ---- Commande : check-cart --------------------------------------------------

@app.command("check-cart")
def check_cart(
    cart: Path = typer.Option(
        Path("data/panier/Panier.html"), "--cart", "-c",
        exists=True, dir_okay=False, readable=True,
        help="HTML SingleFile du panier Cardmarket.",
    ),
    report: Optional[Path] = typer.Option(
        None, "--report", "-r",
        help="CSV d'optimisation. Par défaut : dernier *_WantListOptimized.csv dans reports/.",
    ),
    scenario: str = typer.Option(
        "max_7_vendeurs", "--scenario", "-s",
        help="Nom du scénario à utiliser comme référence.",
    ),
    output_dir: Path = typer.Option(
        Path("reports"), "--output-dir", "-o",
        help="Dossier de sortie pour le rapport Markdown.",
    ),
) -> None:
    """
    Compare le panier Cardmarket (HTML SingleFile) avec la recommandation du solveur.
    Génère un rapport Markdown listant les divergences.
    """
    from .cart_checker import check_cart as _check, write_check_report

    # Auto-détection du dernier rapport si non fourni
    if report is None:
        candidates = sorted(Path("reports").glob("*_WantListOptimized.csv"))
        if not candidates:
            console.print("[red]Aucun rapport CSV trouvé dans reports/. Précise --report.[/red]")
            raise typer.Exit(code=2)
        report = candidates[-1]
        console.print(f"[dim]→ Rapport utilisé : {report}[/dim]")

    result = _check(cart_path=cart, csv_path=report, scenario=scenario)

    # Affichage console
    status = "[bold green]✅ Panier conforme[/bold green]" if result.all_ok \
        else f"[bold yellow]⚠ {result.issue_count} divergence(s)[/bold yellow]"
    console.print(f"\n{status}  —  scénario [bold]{scenario}[/bold]")

    r_total = result.report_total_price + result.report_total_shipping
    c_total = result.cart_total_price + result.cart_total_shipping
    t = Table(show_lines=False, box=None)
    t.add_column("", style="dim")
    t.add_column("Rapport", justify="right")
    t.add_column("Panier", justify="right")
    t.add_column("Écart", justify="right")
    t.add_row("Cartes",
              str(result.report_total_qty), str(result.cart_total_qty),
              f"{result.cart_total_qty - result.report_total_qty:+d}")
    t.add_row("Total €",
              f"{r_total:.2f} €", f"{c_total:.2f} €",
              f"{c_total - r_total:+.2f} €")
    console.print(t)

    for sr in result.seller_results:
        if not sr.ok:
            console.print(f"  [yellow]⚠[/yellow] {sr.seller_name} : {len(sr.issues)} problème(s)")
            for issue in sr.issues:
                label = f"[dim]{issue.card_name}[/dim] — " if issue.card_name else ""
                console.print(f"      {label}{issue.detail}")

    out_path = write_check_report(result, output_dir)
    console.print(f"\n[bold green]✓ Rapport[/bold green] : {out_path}")


# ---- Commande : discover ----------------------------------------------------

@app.command()
def discover(
    wantlist: Path = typer.Option(
        ..., "--wantlist", "-w", exists=True, dir_okay=False, readable=True,
        metavar="<file>", help="HTML de la page wantlist Cardmarket.",
    ),
    sellers_file: Path = typer.Option(
        Path("data/vendeurs_liste/vendeurs.yaml"), "--sellers-file", "-f",
        metavar="<file>",
        help="YAML des vendeurs connus. Sert de référence, et de cible avec --write.",
    ),
    discovery_dir: Path = typer.Option(
        Path("data/discovery"), "--discovery-dir", "-d",
        help="Où stocker les fiches produit téléchargées.",
    ),
    min_cards: int = typer.Option(
        10, "--min-cards",
        help="Nb minimum de cartes de la wantlist que le vendeur doit proposer.",
    ),
    min_sales: int = typer.Option(
        5000, "--min-sales",
        help="Nb minimum de ventes réalisées par le vendeur (évite les petits vendeurs).",
    ),
    clicks: int = typer.Option(
        2, "--clicks",
        help="Clics sur « montrer plus de résultats » (50 offres de plus par clic).",
    ),
    skip_fetch: bool = typer.Option(
        False, "--skip-fetch",
        help="Ne re-télécharge rien, réagrège les fiches déjà présentes.",
    ),
    write: bool = typer.Option(
        False, "--write",
        help="Écrit les vendeurs retenus dans le YAML (fusion + tri alphabétique).",
    ),
    csv_out: Path = typer.Option(
        Path("reports/vendeurs_decouverte.csv"), "--csv",
        help="CSV de tous les vendeurs rencontrés.",
    ),
    no_csv: bool = typer.Option(False, "--no-csv", help="N'écrit pas le CSV."),
    headless: bool = typer.Option(True, "--headless/--headed"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """
    Découvre des vendeurs à ajouter, à partir des fiches produit de la wantlist.

    Part des cartes voulues (et non d'une liste de vendeurs) : ouvre la fiche de
    chaque carte, relève qui la vend, et retient ceux qui dépassent les deux
    seuils. Utilisable pour amorcer un `vendeurs.yaml` vide.
    """
    import csv as _csv

    import yaml

    from .scraper.discover import aggregate, fetch_product_pages, select

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s :: %(message)s",
    )

    wants = parse_wantlist(wantlist)
    console.print(
        f"[bold]→ Discover[/bold] {len(wants)} wants  seuils: ≥{min_cards} cartes "
        f"et ≥{min_sales} ventes  clics={clicks}"
    )

    if not skip_fetch:
        ok, ko = fetch_product_pages(wants, discovery_dir, clicks=clicks, headless=headless)
        console.print(f"  fiches récupérées : {ok} ok, {ko} en échec")

    found = aggregate(discovery_dir)
    if not found:
        console.print("[yellow]⚠ Aucune fiche produit exploitable.[/yellow]")
        raise typer.Exit(1)

    # `vendeurs.yaml` peut ne pas exister encore (amorçage depuis zéro).
    known: set[str] = set()
    wantlist_id: int | None = None
    if sellers_file.exists():
        cfg = yaml.safe_load(sellers_file.read_text(encoding="utf-8")) or {}
        known = {s.strip() for s in (cfg.get("sellers") or []) if s and s.strip()}
        wantlist_id = cfg.get("wantlist_id")

    retenus = select(found, known, min_cards=min_cards, min_sales=min_sales)

    if not no_csv:
        csv_out.parent.mkdir(parents=True, exist_ok=True)
        with csv_out.open("w", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh, delimiter=";")
            w.writerow(["vendeur", "ventes", "nb_cartes", "prix_median", "deja_dans_liste"])
            for d in found:
                w.writerow([
                    d.seller, d.sales or "", d.n_cards,
                    f"{d.median_price:.2f}" if d.median_price is not None else "",
                    "oui" if d.seller in known else "non",
                ])
        console.print(f"  CSV complet : {csv_out}")

    t = Table(title=f"Vendeurs à ajouter (≥{min_cards} cartes, ≥{min_sales} ventes)")
    t.add_column("Vendeur", style="cyan")
    t.add_column("Cartes", justify="right")
    t.add_column("Ventes", justify="right")
    t.add_column("Prix médian", justify="right")
    for d in retenus:
        t.add_row(d.seller, str(d.n_cards), str(d.sales or "?"),
                  f"{d.median_price:.2f} €" if d.median_price is not None else "?")
    console.print(t)
    console.print(f"[bold]{len(retenus)}[/bold] vendeur(s) retenu(s) sur {len(found)} rencontré(s).")

    if not write:
        console.print("[dim]--write pour les ajouter à vendeurs.yaml[/dim]")
        return
    if not retenus:
        console.print("[yellow]Rien à écrire.[/yellow]")
        return

    final = sorted(known | {d.seller for d in retenus}, key=str.casefold)
    sellers_file.parent.mkdir(parents=True, exist_ok=True)

    if sellers_file.exists():
        # Réécriture conservatrice : on garde l'en-tête, les commentaires et les
        # vendeurs mis en sourdine (lignes `# - Pseudo`), on ne touche qu'au bloc
        # des vendeurs actifs.
        lines = sellers_file.read_text(encoding="utf-8").splitlines()
        i = next(n for n, l in enumerate(lines) if l.startswith("sellers:"))
        entete = lines[:i + 1]
        muets = [l for l in lines[i + 1:] if re.match(r"\s*#\s*-\s+\S", l)]
        nouvelles = entete + [f"  - {s}" for s in final] + muets
    else:
        # Amorçage depuis zéro : `fetch` a besoin du wantlist_id, on le relit
        # dans l'HTML de la wantlist plutôt que de produire un fichier inutilisable.
        if wantlist_id is None:
            m = re.search(r"/Wants/(\d+)", wantlist.read_text(encoding="utf-8"))
            wantlist_id = int(m.group(1)) if m else None
        nouvelles = ["# Liste des vendeurs Cardmarket à scraper.", ""]
        if wantlist_id is not None:
            nouvelles += [f"wantlist_id: {wantlist_id}", ""]
        else:
            nouvelles += ["# wantlist_id introuvable dans l'HTML — à renseigner à la main.",
                          "wantlist_id:", ""]
        nouvelles += ["sellers:"] + [f"  - {s}" for s in final]

    sellers_file.write_text("\n".join(nouvelles) + "\n", encoding="utf-8")
    console.print(
        f"[bold green]✓[/bold green] {sellers_file} : {len(known)} → {len(final)} vendeurs actifs"
    )


if __name__ == "__main__":
    app()
