"""
Authentification Cardmarket via Playwright.

Flux :
  1. Première fois → `mkm-optim login` ouvre Chromium en headed.
     Tu te logues manuellement (identifiant + mot de passe + éventuel 2FA).
     Une fois sur ton tableau de bord, on sauvegarde l'état du navigateur
     (cookies + localStorage) dans `.auth/storage_state.json`.
  2. Runs suivants → on relit ce fichier, Chromium démarre déjà connecté.
     Aucun mot de passe ne transite par notre code.

Pas de credentials en clair, jamais. C'est toi qui les tapes dans le navigateur.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from patchright.sync_api import (
    BrowserContext,
    Error as PWError,
    Playwright,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)


STORAGE_STATE_PATH = Path(".auth/storage_state.json")
MKM_BASE = "https://www.cardmarket.com"
LOGIN_URL = f"{MKM_BASE}/fr/Magic/Login"

# Page d'accueil utilisateur connecté : présence d'un lien /Account ou /Wants
LOGGED_IN_PROBE = f"{MKM_BASE}/fr/Magic"
LOGGED_IN_SELECTOR = "a[href*='/Account'], a[href*='/Wants/']"

# Sélecteurs du formulaire /Login (à recentraliser dans selectors.py si on
# change le DOM ; on les garde proche du code qui les utilise pour clarté)
LOGIN_USERNAME_INPUT = "input[name='username']"
LOGIN_PASSWORD_INPUT = "input[name='userPassword']"
LOGIN_SUBMIT_BUTTON = "input[type=submit], button[type=submit]"

# Variables d'environnement reconnues (chargées depuis .env si présent)
ENV_USER = "CARDMARKET_USER"
ENV_PASS = "CARDMARKET_PASS"

# User-Agent honnête (pas de spoof "real human"). On garde l'UA Playwright
# par défaut sauf si l'on souhaite forcer un Firefox stable. Cloudflare
# fingerprint surtout TLS + WebGL, pas l'UA pur.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

log = logging.getLogger(__name__)


def _load_env_credentials() -> tuple[str | None, str | None]:
    """
    Lit les credentials depuis l'environnement (et depuis .env si python-dotenv
    est dispo). Retourne (user, password) — chacun None si non défini.
    Ne logge JAMAIS le mot de passe ; logge juste le fait qu'il est chargé.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
        # cherche un .env dans le cwd, puis remonte (find_dotenv par défaut)
        load_dotenv(override=False)
    except ImportError:
        # python-dotenv pas installé : on lit juste les variables shell
        pass
    user = os.environ.get(ENV_USER) or None
    password = os.environ.get(ENV_PASS) or None
    if user and password:
        log.info("Credentials trouvés dans l'environnement (%s) — login automatique", ENV_USER)
    return user, password


# ---- Connexion (auto si .env, interactive sinon) ----------------------------

def login(
    headless: bool = False,
    storage_path: Path = STORAGE_STATE_PATH,
) -> None:
    """
    Point d'entrée unique pour le login.

    - Si CARDMARKET_USER et CARDMARKET_PASS sont définis (env ou .env) :
      auto-fill des champs + clic submit. L'utilisateur n'intervient que
      pour un éventuel CAPTCHA / email de vérification.
    - Sinon : mode interactif (l'utilisateur tape tout à la main).

    Headless par défaut DÉSACTIVÉ : si Cloudflare/Cf-Turnstile s'affiche, il
    faut une vraie fenêtre pour le résoudre. Mets `headless=True` seulement
    si tu sais que ton compte n'est jamais soumis au CAPTCHA.
    """
    user, password = _load_env_credentials()
    if user and password:
        _auto_login(user, password, storage_path=storage_path, headless=headless)
    else:
        interactive_login(storage_path=storage_path)


def _accept_cookies_banner(page) -> None:
    """Ferme la bannière cookies si elle apparaît (sinon bloque les clics)."""
    for sel in (
        "button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button#CybotCookiebotDialogBodyButtonAccept",
        "button:has-text('Tout accepter')",
        "button:has-text('Accepter')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                log.info("Bannière cookies acceptée (%s)", sel)
                return
        except Exception:
            pass


def _auto_login(
    username: str,
    password: str,
    storage_path: Path = STORAGE_STATE_PATH,
    headless: bool = False,
) -> None:
    """
    Remplit + soumet le formulaire de login automatiquement.
    En cas de blocage (CAPTCHA, MFA, error), bascule sur l'interactif.
    Ne stocke jamais le mot de passe.
    """
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="fr-FR")
        page = ctx.new_page()

        log.info("Ouverture de %s", LOGIN_URL)
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        _accept_cookies_banner(page)

        try:
            page.wait_for_selector(LOGIN_USERNAME_INPUT, timeout=10_000)
            page.fill(LOGIN_USERNAME_INPUT, username)
            page.fill(LOGIN_PASSWORD_INPUT, password)
        except PWTimeoutError:
            log.error("Champs de login introuvables — bascule sur l'interactif.")
            _fallback_interactive(page, ctx, browser, storage_path)
            return

        log.info("Champs remplis, soumission automatique du formulaire...")
        # Le clic sur le bouton submit peut échouer si la bannière cookies
        # (Cybot) recouvre encore le bouton → timeout d'actionabilité. On
        # bascule alors sur un appui ENTRÉE dans le champ mot de passe, qui
        # soumet le formulaire sans dépendre de la position du bouton.
        try:
            page.click(LOGIN_SUBMIT_BUTTON, timeout=5_000)
        except PWTimeoutError:
            log.warning("Clic submit intercepté/introuvable — soumission via ENTRÉE.")
            try:
                page.press(LOGIN_PASSWORD_INPUT, "Enter")
            except PWTimeoutError:
                log.error("Soumission impossible — bascule sur l'interactif.")
                _fallback_interactive(page, ctx, browser, storage_path)
                return

        # Attente de la redirection hors de /Login
        try:
            page.wait_for_url(
                lambda url: "/Login" not in url,
                timeout=15_000,
            )
            log.info("Redirection détectée : %s", page.url)
        except PWTimeoutError:
            # On est resté sur /Login : CAPTCHA, mauvais creds, ou bannière qui bloque
            log.warning(
                "Toujours sur /Login après 15s — il y a probablement un CAPTCHA "
                "ou un message d'erreur. Bascule sur le mode interactif."
            )
            _fallback_interactive(page, ctx, browser, storage_path)
            return

        # Probe : on doit voir un lien Logout ou /Account
        if _detect_logged_in(page):
            log.info("✓ Session détectée comme valide.")
        else:
            log.warning(
                "Pas de marqueur 'logged-in' clair après redirection. "
                "URL courante: %s — on sauvegarde quand même, on vérifiera au fetch.",
                page.url,
            )

        ctx.storage_state(path=str(storage_path))
        browser.close()
        print(f"\n✓ Session sauvegardée dans {storage_path}")


def _detect_logged_in(page) -> bool:
    """Cherche un lien Logout ou /Account avec une tolérance courte."""
    for sel in (
        "a[href*='User_Logout']",
        "a[href*='Logout']",
        "a[href*='/Account']",
        "a[href*='/Wants/']",
    ):
        try:
            if page.locator(sel).first.is_visible(timeout=2000):
                return True
        except Exception:
            continue
    return False


def _fallback_interactive(page, ctx, browser, storage_path: Path) -> None:
    """Quand l'auto-login bute (CAPTCHA, sélecteur inconnu, etc.)."""
    print()
    print("=" * 70)
    print("  AUTO-LOGIN BLOQUÉ — termine la connexion manuellement.")
    print(f"  URL actuelle : {page.url}")
    print("  Dans la fenêtre Chromium : résous le CAPTCHA et/ou clique Connexion.")
    print("  Quand tu es sur ton tableau de bord MKM, reviens ici et appuie ENTRÉE.")
    print("=" * 70)
    try:
        input("  > ")
    except (EOFError, KeyboardInterrupt):
        print("Connexion annulée.")
        browser.close()
        return
    if _detect_logged_in(page):
        log.info("✓ Session détectée comme valide après interaction.")
    else:
        log.warning("Pas de marqueur logged-in détecté — sauvegarde quand même.")
    ctx.storage_state(path=str(storage_path))
    browser.close()
    print(f"\n✓ Session sauvegardée dans {storage_path}")


# ---- Première connexion (interactive, headed) -------------------------------

def interactive_login(storage_path: Path = STORAGE_STATE_PATH) -> None:
    """
    Ouvre Chromium headed sur la page de login MKM. L'utilisateur se logue
    manuellement, puis on sauvegarde l'état du browser.

    On considère le login "réussi" quand le bouton "Mon compte" / un lien
    /Account devient présent dans la page. On laisse 5 minutes max.
    """
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="fr-FR")
        page = ctx.new_page()

        log.info("Ouverture de %s — connecte-toi dans la fenêtre.", LOGIN_URL)
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        print()
        print("=" * 70)
        print("  CONNEXION CARDMARKET")
        print("=" * 70)
        print(f"  1. Tape ton identifiant + mot de passe dans la fenêtre Chromium")
        print(f"  2. Si MKM te demande un CAPTCHA / 2FA, fais-le manuellement")
        print(f"  3. Une fois sur ton tableau de bord, REVIENS ICI et appuie ENTRÉE")
        print("=" * 70)
        try:
            input("  > Appuie sur ENTRÉE quand tu es connecté(e)... ")
        except (EOFError, KeyboardInterrupt):
            print("Connexion annulée.")
            browser.close()
            return

        # Sanity check : on attend rapidement la présence d'un marqueur "logged-in"
        try:
            page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=5_000)
            log.info("Marqueur de session détecté.")
        except PWTimeoutError:
            log.warning(
                "Aucun marqueur de connexion détecté. "
                "On sauvegarde quand même la session — vérifie qu'elle marche "
                "avec `mkm-optim fetch` sur 1 vendeur."
            )

        ctx.storage_state(path=str(storage_path))
        browser.close()
        print(f"\n✓ Session sauvegardée dans {storage_path}")
        print("  Tu n'auras plus besoin de te reconnecter tant que MKM la conserve")
        print("  (typiquement 30 jours). Sinon relance `mkm-optim login`.\n")


# ---- Réutilisation de la session pour les runs suivants ---------------------

def get_authenticated_context(
    playwright: Playwright,
    headless: bool = True,
    storage_path: Path = STORAGE_STATE_PATH,
) -> tuple[object, BrowserContext]:
    """
    Lance un navigateur Chromium en réutilisant le storage_state existant.
    Retourne (browser, context). À l'appelant de les fermer.

    Lève RuntimeError si pas de session enregistrée.
    """
    if not storage_path.exists():
        raise RuntimeError(
            f"Pas de session enregistrée ({storage_path}). "
            "Lance d'abord : mkm-optim login"
        )
    # Cloudflare (mi-2026) détecte Playwright via la fuite CDP `Runtime.enable`.
    # On utilise donc patchright (import en tête de module) qui patche ce leak,
    # ET on lance en mode HEADED — patchright reste détectable en headless.
    # En WSL sans WSLg fonctionnel, lancer sous un écran virtuel Xvfb :
    #   DISPLAY=:99 uv run mkm-optim fetch ...
    # Pas de user_agent ni d'args custom : patchright gère la furtivité seul.
    browser = playwright.chromium.launch(headless=False)
    ctx = browser.new_context(
        storage_state=str(storage_path),
        locale="fr-FR",
        viewport={"width": 1366, "height": 900},
    )
    return browser, ctx


def is_session_valid(
    ctx: BrowserContext,
    timeout_ms: int = 15_000,
    retries: int = 4,
) -> bool:
    """
    Vérifie que la session stockée est encore active.

    Méthode : on charge la page Wants (qui n'est accessible QUE connecté →
    MKM redirige sinon vers /Login). Si l'URL finale contient /Login, KO.
    Sinon on cherche un lien Logout / Account pour confirmer.

    Retry interne sur timeouts et erreurs réseau (ERR_NETWORK_CHANGED…) avec
    backoff léger, parce qu'un blip Wi-Fi pendant cette probe ne doit pas
    faire planter tout le fetch derrière.
    """
    probe_url = f"{MKM_BASE}/fr/Magic/Wants"
    page = ctx.new_page()
    try:
        # Boucle de retry pour la navigation initiale
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                page.goto(probe_url, wait_until="domcontentloaded", timeout=timeout_ms)
                last_err = None
                break
            except PWTimeoutError as e:
                last_err = e
                log.warning("Probe session timeout (%d/%d) : %s", attempt, retries, e)
            except PWError as e:
                last_err = e
                log.warning("Probe session erreur réseau (%d/%d) : %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(5 * attempt)  # 5s, 10s, 15s
        if last_err is not None:
            log.error("Probe session : %d tentatives échouées (%s)", retries, last_err)
            return False

        final_url = page.url
        log.info("Session probe : URL finale = %s", final_url)

        if "/Login" in final_url:
            log.warning("Redirection vers /Login → session invalide.")
            return False

        # On cherche un marqueur de session vivante
        for sel in (
            "a[href*='User_Logout']",
            "a[href*='Logout']",
            "a[href*='/Account']",
            "a[href*='/Wants/']",
            "h1:has-text('Wants')",   # titre de la page Wants
        ):
            try:
                if page.locator(sel).first.is_visible(timeout=2000):
                    log.info("Marqueur logged-in trouvé : %s", sel)
                    return True
            except Exception:
                continue

        # On n'a pas vu de marqueur. On dump quelques liens pour debug.
        try:
            sample_links = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]')).slice(0,15).map(a => a.href)"
            )
            log.warning("Aucun marqueur logged-in trouvé. URL=%s — exemples de liens : %s",
                        final_url, sample_links[:8])
        except Exception:
            pass
        return False
    except PWTimeoutError as e:
        log.error("Timeout sur la probe de session (%s) : %s", probe_url, e)
        return False
    finally:
        page.close()
