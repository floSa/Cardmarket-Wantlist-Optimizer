"""
Récupération paginatée des offres vendeur via Playwright.

Pour chaque vendeur listé :
  - URL de départ : /fr/Magic/Users/<PSEUDO>/Offers/Singles?idWantslist=<ID>
                    (filtre natif MKM : ne renvoie que les offres qui matchent
                    ta wantlist, donc 1 à ~10 pages au lieu de 100+)
  - Boucle tant que le bouton "Page suivante" n'est pas `disabled`
  - Suit `&site=2`, `&site=3`, ... extraits du href du bouton
  - Sauvegarde chaque page HTML brut dans `data/sellers/<PSEUDO>/page<N>.html`
  - Rate limit aléatoire entre 800 et 1500 ms (config par défaut, prudent)
  - Backoff exponentiel sur HTTP 429
  - Arrêt immédiat si la session a expiré (redirection vers /Login)

Aucune logique métier ici — ce module ne sait que naviguer + sauvegarder.
Le parsing est délégué à `parser.seller_offers`.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from patchright.sync_api import (
    BrowserContext,
    Error as PWError,
    Page,
    Response,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

from ..parser.seller_offers import parse_pagination
from .auth import (
    LOGIN_URL,
    MKM_BASE,
    get_authenticated_context,
    is_session_valid,
)


_RE_PAGE_NUM = re.compile(r"page(\d+)\.html$", re.IGNORECASE)


log = logging.getLogger(__name__)


def _sleep_fn(min_ms: int, max_ms: int) -> Callable[[], None]:
    """Closure qui dort un nb de ms aléatoire dans [min_ms, max_ms]."""
    def _sleep() -> None:
        time.sleep(random.uniform(min_ms, max_ms) / 1000.0)
    return _sleep


@dataclass
class FetchStats:
    seller: str
    pages_fetched: int = 0
    total_pages_announced: int | None = None
    total_results_announced: int | None = None
    duration_seconds: float = 0.0
    error: str | None = None
    skipped: bool = False     # vrai si vendeur déjà en cache et --no-refresh

    @property
    def ok(self) -> bool:
        return self.error is None and not self.skipped


@dataclass
class FetchOptions:
    wantlist_id: int
    output_dir: Path
    min_delay_ms: int = 800
    max_delay_ms: int = 1500
    max_pages_per_seller: int = 30
    refresh: bool = False                  # True = re-fetch même si déjà en cache
    on_429_initial_backoff: float = 30.0   # secondes au 1er 429
    on_429_max_attempts: int = 3
    progress_cb: Callable[[FetchStats], None] | None = None


# ---- Fetch d'un seul vendeur ------------------------------------------------

def fetch_seller(
    seller: str,
    ctx: BrowserContext,
    opts: FetchOptions,
) -> FetchStats:
    """
    Récupère toutes les pages d'offres d'un vendeur filtrées par la wantlist.
    Sauvegarde chaque page HTML dans `<output_dir>/<seller>/pageN.html`.

    Renvoie un FetchStats. Si la session a expiré, error="auth_expired".
    """
    stats = FetchStats(seller=seller)
    seller_dir = opts.output_dir / seller
    base_url = (
        f"{MKM_BASE}/fr/Magic/Users/{seller}/Offers/Singles"
        f"?sortBy=name_asc&idWantslist={opts.wantlist_id}"
    )

    # --- Politique de cache --------------------------------------------------
    # 1) --refresh : on efface l'existant et on repart à la page 1.
    # 2) Pas de --refresh, pas de pages en cache : nouveau vendeur, page 1.
    # 3) Pas de --refresh, des pages en cache :
    #      - Si on a déjà page1.html, on lit la pagination dedans pour
    #        connaître total_pages.
    #      - Si on a toutes les pages → skip total (rien à faire).
    #      - Sinon → on RESUME à la 1re page manquante.
    start_idx = 1
    if opts.refresh and seller_dir.exists():
        for f in seller_dir.glob("page*.html"):
            f.unlink()
            log.debug("[%s] supprimé %s (refresh)", seller, f.name)
    elif seller_dir.exists():
        existing_pages = _existing_page_nums(seller_dir)
        if existing_pages:
            page1 = seller_dir / "page1.html"
            total_pages_known = None
            if page1.exists():
                try:
                    pstate = parse_pagination(page1)
                    total_pages_known = pstate.total_pages
                except Exception as e:
                    log.warning("[%s] impossible de lire page1.html pour resume (%s) — re-fetch complet", seller, e)
                    for f in seller_dir.glob("page*.html"):
                        f.unlink()
                    total_pages_known = None
            if total_pages_known and max(existing_pages) >= total_pages_known:
                stats.skipped = True
                stats.pages_fetched = len(existing_pages)
                stats.total_pages_announced = total_pages_known
                log.info("[%s] %d/%d pages en cache, skip complet (--refresh pour forcer)",
                         seller, len(existing_pages), total_pages_known)
                return stats
            # Resume : on reprend à la 1re page manquante après le bloc continu
            # ex : pages 1,2,3,4 en cache → on reprend à 5
            start_idx = max(existing_pages) + 1
            log.info("[%s] resume : %d pages déjà en cache, reprise à page %d",
                     seller, len(existing_pages), start_idx)

    seller_dir.mkdir(parents=True, exist_ok=True)
    sleep_fn = _sleep_fn(opts.min_delay_ms, opts.max_delay_ms)
    started = time.monotonic()

    page = ctx.new_page()
    page.set_default_navigation_timeout(45_000)

    try:
        # URL de départ : page courante (1 ou index de reprise)
        if start_idx == 1:
            current_url = base_url
        else:
            current_url = f"{base_url}&site={start_idx}"
        site_idx = start_idx

        while site_idx <= opts.max_pages_per_seller:
            log.info("[%s] page %d → %s", seller, site_idx, current_url)
            response = _goto_with_backoff(page, current_url, opts)
            if response is None:
                stats.error = "navigation_failed"
                return stats

            # Détection redirection vers /Login = session expirée
            final_url = page.url
            if "/Login" in final_url and "/Users/" not in final_url:
                stats.error = "auth_expired"
                log.error("[%s] redirection vers /Login : session expirée. "
                          "Relance `mkm-optim login`.", seller)
                return stats

            # Sauvegarde du HTML
            html = page.content()
            out_path = seller_dir / f"page{site_idx}.html"
            out_path.write_text(html, encoding="utf-8")
            stats.pages_fetched += 1

            # Détection pagination
            pstate = parse_pagination(html)
            if site_idx == 1:
                stats.total_pages_announced = pstate.total_pages
                stats.total_results_announced = pstate.total_results
                log.info("[%s] %s pages annoncées, %s offres au total",
                         seller, pstate.total_pages, pstate.total_results)

            if not pstate.has_next:
                break

            # Sécurité : si MKM nous renvoie un next_url vers une page <=
            # current_page, on stoppe pour éviter une boucle infinie.
            if pstate.current_page < site_idx:
                log.warning("[%s] pagination incohérente (current=%d, attendu=%d), stop",
                            seller, pstate.current_page, site_idx)
                break

            current_url = pstate.next_url
            site_idx += 1
            sleep_fn()

        if opts.progress_cb:
            opts.progress_cb(stats)
        return stats

    finally:
        try:
            page.close()
        except Exception:
            pass
        stats.duration_seconds = round(time.monotonic() - started, 2)


_CF_CHALLENGE_MARKERS = (
    "just a moment",
    "un instant",              # défi Cloudflare en français
    "veuillez patienter",
    "vérification",
    "attention required",
    "cf-browser-verification",
    "checking your browser",
    "verifying you are human",
)


def _is_cf_challenge(page: Page) -> bool:
    """Vrai si la page affichée est un interstitiel Cloudflare (pas le vrai contenu)."""
    try:
        title = (page.title() or "").lower()
    except Exception:
        return False
    return any(m in title for m in _CF_CHALLENGE_MARKERS)


def _wait_challenge_cleared(page: Page, timeout_s: int = 18) -> bool:
    """
    Le défi JS Cloudflare (« Just a moment… ») s'auto-résout et redirige vers le
    contenu réel en quelques secondes. On patiente jusqu'à `timeout_s` que le
    titre ne soit plus celui d'un interstitiel. Retourne True si résolu.
    """
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        time.sleep(2)
        if not _is_cf_challenge(page):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            return True
    return not _is_cf_challenge(page)


def _goto_with_backoff(page: Page, url: str, opts: FetchOptions) -> Response | None:
    """
    Navigue vers `url` en gérant timeout, erreurs réseau (ERR_NETWORK_CHANGED,
    ERR_INTERNET_DISCONNECTED…), HTTP 429/503 et les défis Cloudflare par
    backoff exponentiel. Retourne la Response (ou None si tous les essais ont échoué).
    """
    delay = opts.on_429_initial_backoff
    for attempt in range(1, opts.on_429_max_attempts + 1):
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except PWTimeoutError as e:
            log.warning("timeout navigation (tentative %d/%d) : %s",
                        attempt, opts.on_429_max_attempts, e)
            time.sleep(delay)
            delay *= 2
            continue
        except PWError as e:
            # net::ERR_NETWORK_CHANGED, ERR_INTERNET_DISCONNECTED, ERR_NAME_NOT_RESOLVED, etc.
            log.warning("erreur réseau (tentative %d/%d) : %s",
                        attempt, opts.on_429_max_attempts, e)
            time.sleep(delay)
            delay *= 2
            continue

        if response is None:
            return None
        status = response.status

        # Défi Cloudflare (« Just a moment… » / « Attention Required »), souvent
        # servi en 403 : on laisse le JS du défi se résoudre (auto-redirection)
        # avant d'abandonner. Si résolu, le vrai contenu est dans page.content().
        if status == 403 or _is_cf_challenge(page):
            if _wait_challenge_cleared(page):
                return response
            log.warning(
                "défi Cloudflare non résolu — backoff %.0fs (tentative %d/%d)",
                delay, attempt, opts.on_429_max_attempts,
            )
            time.sleep(delay)
            delay *= 2
            continue

        if status == 200:
            return response
        if status == 429 or status == 503:
            log.warning(
                "HTTP %d reçu — backoff %.0fs (tentative %d/%d)",
                status, delay, attempt, opts.on_429_max_attempts,
            )
            time.sleep(delay)
            delay *= 2
            continue
        # Autres statuts (404, 500…) : on remonte tel quel
        log.error("HTTP %d sur %s", status, url)
        return response
    log.error("Toutes les tentatives ont échoué (timeout/réseau/429/défi CF) pour %s", url)
    return None


def _existing_page_nums(seller_dir: Path) -> list[int]:
    """Retourne la liste des numéros de page déjà sauvegardés (triés)."""
    nums: list[int] = []
    for f in seller_dir.glob("page*.html"):
        m = _RE_PAGE_NUM.search(f.name)
        if m:
            nums.append(int(m.group(1)))
    return sorted(nums)


# ---- Fetch de plusieurs vendeurs --------------------------------------------

def fetch_all_sellers(
    sellers: Iterable[str],
    opts: FetchOptions,
    headless: bool = True,
) -> list[FetchStats]:
    """
    Boucle sur la liste de vendeurs, fetch séquentiel.
    Crée un contexte Playwright partagé pour économiser les ressources.

    Si la session est invalide AVANT le 1er fetch, lève RuntimeError —
    plus utile que de boucler sur 16 vendeurs en échec.
    """
    sellers = list(sellers)
    if not sellers:
        return []

    results: list[FetchStats] = []
    with sync_playwright() as p:
        browser, ctx = get_authenticated_context(p, headless=headless)
        try:
            log.info("Validation de la session...")
            if not is_session_valid(ctx):
                raise RuntimeError(
                    "Session expirée ou invalide. Relance `mkm-optim login`."
                )
            log.info("Session OK — début du scraping (%d vendeurs)", len(sellers))

            for i, seller in enumerate(sellers, start=1):
                log.info("[%d/%d] %s", i, len(sellers), seller)
                stats = fetch_seller(seller, ctx, opts)
                results.append(stats)
                if stats.error == "auth_expired":
                    log.error("Session expirée en cours de scraping — abandon.")
                    break
                # Pause entre vendeurs
                if i < len(sellers):
                    time.sleep(random.uniform(1.0, 2.0))
        finally:
            browser.close()
    return results
