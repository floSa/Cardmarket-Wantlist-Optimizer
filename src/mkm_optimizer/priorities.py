"""
Gestion des priorités et annotations de la wantlist via Markdown (.md) et Excel (.xlsx).

Permet à l'utilisateur de définir un coefficient de priorité (entre 0.0 et 1.0,
ou via des mots-clés) pour chaque carte de sa wantlist dans un tableau Markdown ou Excel.
Le solveur MIP utilise ces poids pour privilégier les cartes indispensables.
"""

from __future__ import annotations

import dataclasses
import logging
from decimal import Decimal
from pathlib import Path
from typing import Optional, Sequence

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .models import WantEntry

log = logging.getLogger(__name__)

DEFAULT_PRIORITIES_MD_PATH = Path("data/wantlist_priorities.md")
DEFAULT_PRIORITIES_XLSX_PATH = Path("data/wantlist_priorities.xlsx")

# Alias textuels reconnus pour la colonne priorité
PRIORITY_ALIASES: dict[str, float] = {
    "haute": 1.0,
    "haut": 1.0,
    "urgente": 1.0,
    "urgent": 1.0,
    "high": 1.0,
    "max": 1.0,
    "normale": 1.0,
    "normal": 1.0,
    "standard": 1.0,
    "medium": 1.0,
    "moyenne": 0.5,
    "moyen": 0.5,
    "secondaire": 0.3,
    "basse": 0.3,
    "bas": 0.3,
    "low": 0.3,
    "faible": 0.3,
    "bonus": 0.0,
    "optionnel": 0.0,
    "optionnelle": 0.0,
    "zero": 0.0,
}


def parse_priority_value(raw_val: object) -> float:
    """
    Convertit une valeur brute (nombre, chaîne ou vide)
    en coefficient float (>= 0.0). Par défaut 1.0 si non spécifié ou invalide.
    """
    if raw_val is None:
        return 1.0
    if isinstance(raw_val, (int, float)):
        return max(0.0, float(raw_val))

    s = str(raw_val).strip().lower().replace(",", ".")
    if not s:
        return 1.0

    if s in PRIORITY_ALIASES:
        return PRIORITY_ALIASES[s]

    try:
        val = float(s)
        return max(0.0, val)
    except ValueError:
        log.warning("Valeur de priorité non reconnue %r — repli sur 1.0", raw_val)
        return 1.0


# ---- Gestion Markdown (.md) -------------------------------------------------

def load_priorities_markdown(
    md_path: Path = DEFAULT_PRIORITIES_MD_PATH,
) -> dict[str, tuple[float, Optional[str]]]:
    """
    Charge les priorités et commentaires depuis un tableau Markdown.
    Retourne : nom_normalisé -> (priorité, commentaire).
    """
    if not md_path.exists():
        return {}

    content = md_path.read_text(encoding="utf-8")
    lines = [l.strip() for l in content.splitlines() if l.strip().startswith("|")]
    if len(lines) < 2:
        return {}

    # Header parsing
    headers = [h.strip().lower() for h in lines[0].split("|")[1:-1]]
    col_map: dict[str, int] = {}
    for idx, h in enumerate(headers):
        if "carte" in h or "nom" in h:
            col_map["card_name"] = idx
        elif "priorit" in h or "poids" in h:
            col_map["priority"] = idx
        elif "commentaire" in h or "note" in h:
            col_map["notes"] = idx

    card_idx = col_map.get("card_name", 0)
    prio_idx = col_map.get("priority", 2)
    max_price_idx = None
    for k, idx in col_map.items():
        if "prix" in k or "price" in k:
            max_price_idx = idx
            break
    if max_price_idx is None and len(headers) > 6:
        max_price_idx = 6
    notes_idx = col_map.get("notes", 7 if len(headers) > 7 else len(headers) - 1)

    priorities: dict[str, tuple[float, Optional[str], Optional[Decimal]]] = {}
    for line in lines[2:]:  # Saute l'en-tête et le séparateur |---|---|
        parts = [p.strip() for p in line.split("|")[1:-1]]
        if len(parts) <= card_idx:
            continue
        card_name = parts[card_idx]
        if not card_name:
            continue
        norm_key = card_name.lower()

        p_raw = parts[prio_idx] if prio_idx < len(parts) else "1.0"
        priority = parse_priority_value(p_raw)

        max_price = None
        if max_price_idx is not None and max_price_idx < len(parts):
            px_raw = parts[max_price_idx].replace("€", "").replace("\xa0", "").strip().replace(",", ".")
            if px_raw and px_raw != "-":
                try:
                    max_price = Decimal(px_raw)
                except Exception:
                    pass

        note_raw = parts[notes_idx] if notes_idx < len(parts) else ""
        notes = note_raw if note_raw else None

        priorities[norm_key] = (priority, notes, max_price)

    return priorities


def export_priorities_markdown(
    wants: Sequence[WantEntry],
    md_path: Path = DEFAULT_PRIORITIES_MD_PATH,
) -> Path:
    """
    Génère ou met à jour le tableau Markdown de priorités.
    Préserve les priorités et commentaires existants.
    """
    md_path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_priorities_markdown(md_path)
    if not existing and DEFAULT_PRIORITIES_XLSX_PATH.exists():
        existing = load_priorities_excel(DEFAULT_PRIORITIES_XLSX_PATH)

    sorted_wants = sorted(wants, key=lambda w: w.card_name.lower())

    lines = [
        "# Priorités Wantlist",
        "",
        "Ce tableau vous permet de noter vos cartes pour l'optimiseur :",
        "- **1.0** (ou `haute`) : Carte indispensable (recherche prioritaire).",
        "- **0.3** (ou `basse` / `secondaire`) : Carte utile mais sacrifiable si le quota de vendeurs est atteint.",
        "- **0.0** (ou `bonus`) : Carte achetée uniquement si un vendeur déjà sélectionné l'a en stock.",
        "",
        "| Carte | Quantité | Priorité | Édition | Langues | État min | Prix Max (€) | Commentaires |",
        "|---|---:|---:|---|---|---|---:|---|",
    ]

    for w in sorted_wants:
        norm_key = w.card_name.lower()
        if norm_key in existing:
            priority, notes = existing[norm_key]
        else:
            priority = w.priority
            notes = w.notes or ""

        prio_str = f"{priority:.1f}" if priority != 1.0 else "1.0"
        px_str = f"{w.max_price:.2f} €" if w.max_price is not None else "-"
        ed_str = w.set_label or ("Toutes éditions" if w.is_metacard else "-")
        lang_str = "/".join(w.languages) if w.languages else "Toutes"

        lines.append(
            f"| {w.card_name} | {w.quantity} | {prio_str} | {ed_str} | {lang_str} | {w.min_condition.value} | {px_str} | {notes or ''} |"
        )

    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Tableau de priorités Markdown enregistré dans %s (%d cartes)", md_path, len(sorted_wants))
    return md_path


# ---- Gestion Excel (.xlsx) --------------------------------------------------

def load_priorities_excel(
    xlsx_path: Path = DEFAULT_PRIORITIES_XLSX_PATH,
) -> dict[str, tuple[float, Optional[str]]]:
    """
    Charge les priorités et commentaires depuis un fichier Excel.
    Retourne un dictionnaire : nom_normalisé -> (priorité, commentaire).
    """
    if not xlsx_path.exists():
        return {}

    try:
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    except Exception as e:
        log.error("Impossible de lire le fichier Excel %s : %s", xlsx_path, e)
        return {}

    ws = wb.active
    if ws is None:
        return {}

    col_map: dict[str, int] = {}
    for col_idx in range(1, ws.max_column + 1):
        val = ws.cell(row=1, column=col_idx).value
        if val:
            norm_header = str(val).strip().lower()
            if "carte" in norm_header or "nom" in norm_header:
                col_map["card_name"] = col_idx
            elif "priorit" in norm_header or "poids" in norm_header:
                col_map["priority"] = col_idx
            elif "commentaire" in norm_header or "note" in norm_header:
                col_map["notes"] = col_idx

    card_col = col_map.get("card_name", 1)
    priority_col = col_map.get("priority", 3)
    max_price_col = None
    for k, idx in col_map.items():
        if "prix" in k or "price" in k:
            max_price_col = idx
            break
    if max_price_col is None:
        max_price_col = 7
    notes_col = col_map.get("notes", 8)

    priorities: dict[str, tuple[float, Optional[str], Optional[Decimal]]] = {}
    for row_idx in range(2, ws.max_row + 1):
        card_name_cell = ws.cell(row=row_idx, column=card_col).value
        if not card_name_cell:
            continue
        card_name = str(card_name_cell).strip()
        norm_key = card_name.lower()

        p_val = ws.cell(row=row_idx, column=priority_col).value
        priority = parse_priority_value(p_val)

        px_val = ws.cell(row=row_idx, column=max_price_col).value
        max_price = None
        if px_val is not None and str(px_val).strip() not in ("", "-"):
            try:
                raw_px = str(px_val).replace("€", "").replace("\xa0", "").strip().replace(",", ".")
                max_price = Decimal(raw_px)
            except Exception:
                pass

        note_val = ws.cell(row=row_idx, column=notes_col).value
        notes = str(note_val).strip() if note_val is not None and str(note_val).strip() else None

        priorities[norm_key] = (priority, notes, max_price)

    return priorities


def export_priorities_excel(
    wants: Sequence[WantEntry],
    xlsx_path: Path = DEFAULT_PRIORITIES_XLSX_PATH,
) -> Path:
    """
    Génère ou met à jour un fichier Excel avec la liste des cartes de la wantlist.
    """
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_priorities_excel(xlsx_path)
    if not existing and DEFAULT_PRIORITIES_MD_PATH.exists():
        existing = load_priorities_markdown(DEFAULT_PRIORITIES_MD_PATH)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Priorités Wantlist"

    headers = [
        "Nom de la carte",
        "Quantité",
        "Priorité (0.0 à 1.0)",
        "Édition",
        "État min",
        "Langues",
        "Prix max (€)",
        "Commentaires",
    ]

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    sorted_wants = sorted(wants, key=lambda w: w.card_name.lower())
    priority_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

    for row_idx, w in enumerate(sorted_wants, start=2):
        norm_key = w.card_name.lower()
        if norm_key in existing:
            priority, notes = existing[norm_key]
        else:
            priority = w.priority
            notes = w.notes or ""

        row_data = [
            w.card_name,
            w.quantity,
            priority,
            w.set_label or ("Toutes éditions" if w.is_metacard else ""),
            w.min_condition.value,
            " / ".join(w.languages) if w.languages else "Toutes",
            float(w.max_price) if w.max_price is not None else "",
            notes or "",
        ]
        ws.append(row_data)

        for col_idx in range(1, len(row_data) + 1):
            c = ws.cell(row=row_idx, column=col_idx)
            c.border = thin_border
            if col_idx == 2:
                c.alignment = Alignment(horizontal="center")
            elif col_idx == 3:
                c.alignment = Alignment(horizontal="center")
                c.fill = priority_fill
                c.number_format = "0.00"
            elif col_idx in (5, 6):
                c.alignment = Alignment(horizontal="center")
            elif col_idx == 7 and c.value != "":
                c.number_format = '#,##0.00 €'
                c.alignment = Alignment(horizontal="right")

    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["D"].width = 28
    ws.column_dimensions["E"].width = 12
    ws.column_dimensions["F"].width = 16
    ws.column_dimensions["G"].width = 15
    ws.column_dimensions["H"].width = 35

    ws.freeze_panes = "A2"
    wb.save(xlsx_path)
    log.info("Fichier de priorités Excel enregistré dans %s (%d cartes)", xlsx_path, len(sorted_wants))
    return xlsx_path


# ---- Application unifiée ----------------------------------------------------

def load_priorities(
    target_path: Optional[Path] = None,
) -> dict[str, tuple[float, Optional[str], Optional[Decimal]]]:
    """
    Charge les priorités en cherchant automatiquement dans l'ordre :
    1. Chemin spécifié explicitement
    2. Le fichier le plus récent entre data/wantlist_priorities.xlsx et data/wantlist_priorities.md
    """
    if target_path is not None and target_path.exists():
        if target_path.suffix.lower() == ".md":
            return load_priorities_markdown(target_path)
        return load_priorities_excel(target_path)

    md_exists = DEFAULT_PRIORITIES_MD_PATH.exists()
    xlsx_exists = DEFAULT_PRIORITIES_XLSX_PATH.exists()

    if md_exists and xlsx_exists:
        if DEFAULT_PRIORITIES_XLSX_PATH.stat().st_mtime >= DEFAULT_PRIORITIES_MD_PATH.stat().st_mtime:
            return load_priorities_excel(DEFAULT_PRIORITIES_XLSX_PATH)
        return load_priorities_markdown(DEFAULT_PRIORITIES_MD_PATH)
    elif xlsx_exists:
        return load_priorities_excel(DEFAULT_PRIORITIES_XLSX_PATH)
    elif md_exists:
        return load_priorities_markdown(DEFAULT_PRIORITIES_MD_PATH)

    return {}


def apply_priorities_to_wants(
    wants: list[WantEntry],
    target_path: Optional[Path] = None,
) -> list[WantEntry]:
    """
    Applique les coefficients de priorité, prix max souhaité et commentaires sur les WantEntry.
    """
    priorities = load_priorities(target_path)
    if not priorities:
        return wants

    updated: list[WantEntry] = []
    nb_custom = 0

    for w in wants:
        norm_key = w.card_name.lower()
        if norm_key in priorities:
            p_val, note_val, max_px_val = priorities[norm_key]
            if p_val != 1.0:
                nb_custom += 1
            new_kwargs = {"priority": p_val, "notes": note_val or w.notes}
            if max_px_val is not None:
                new_kwargs["max_price"] = max_px_val
            updated.append(
                dataclasses.replace(w, **new_kwargs)
            )
        else:
            updated.append(w)

    log.info(
        "Priorités appliquées : %d cartes avec priorité personnalisée sur %d wants",
        nb_custom,
        len(wants),
    )
    return updated
