"""
Thumbnail generation pour le viewer du dashboard.

Génère des miniatures 400x550 pour les fichiers PDF et ePub, avec un placeholder
générique pour les formats non supportés ou les fichiers sans cover.

Le cache est stocké dans `{INBOX}/.thumbnail-cache/{stem}.jpg`.
"""

from __future__ import annotations

import hashlib
import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from lib.logger import get_logger

log = get_logger()

# ── Constantes ──────────────────────────────────────────────────────────────
THUMBNAIL_WIDTH = 400
THUMBNAIL_HEIGHT = 550
THUMBNAIL_QUALITY = 80
PLACEHOLDER_BG = (26, 29, 36)        # #1a1d24 — gris foncé du dark theme
PLACEHOLDER_FG = (220, 220, 220)     # blanc cassé

# Number of bytes hashed to compute the content key. Same as
# lib.vision_cache so the two caches are conceptually aligned (file
# identity is "first 8 KiB of bytes"). Identical content under a
# different rel_path → same key → cache hit (survives renames).
_HEAD_BYTES = 8192


def compute_content_key(file_path: str | Path, length: int = 16) -> str | None:
    """Stable thumbnail cache key derived from the file's head bytes.

    Returns the first ``length`` hex chars of ``md5(first 8 KiB)``, or
    ``None`` if the file can't be read. Pure content-based so:
      - A rename does NOT invalidate the cache (rel_path-independent).
      - Two identical files under different paths share one thumbnail.

    Used by the dashboard's thumbnail cache directory layout
    ``profile/.cache/thumbnails/<key>/{1..n}.jpg``.
    """
    try:
        with open(file_path, "rb") as f:
            head = f.read(_HEAD_BYTES)
    except OSError:
        return None
    if not head:
        return None
    return hashlib.md5(head).hexdigest()[:length]


def generate_thumbnail(
    source_path: Path,
    doc_dir: Path,
    n_pages: int = 1,
    start_page: int = 1,
) -> int:
    """Génère N pages de thumbnails pour le fichier source.

    Stocke les images dans `doc_dir/{1..n}.jpg`. Le dossier doc_dir est créé.

    Args:
        source_path: Chemin du fichier source (PDF ou ePub)
        doc_dir: Dossier de destination (sous-dossier par document)
        n_pages: Nombre de pages à générer (1-5)
        start_page: Numéro de la première page à générer (pour compléter)

    Returns:
        Nombre de pages effectivement générées (0 si échec total).
    """
    doc_dir.mkdir(parents=True, exist_ok=True)
    n_pages = max(1, min(5, n_pages))
    ext = source_path.suffix.lower()

    try:
        if ext == ".pdf":
            return _generate_pdf_thumbnails(source_path, doc_dir, n_pages, start_page)
        if ext == ".epub":
            return _generate_epub_thumbnails(source_path, doc_dir, n_pages, start_page)
        # Format non supporté → placeholder en page 1 uniquement
        if start_page == 1:
            _generate_placeholder_thumbnail(
                f"Format non supporté\n{ext}", doc_dir / "1.jpg")
            return 1
        return 0
    except Exception as e:
        log.warning(f"  ⚠ Erreur thumbnail {source_path.name}: {e}")
        try:
            _generate_placeholder_thumbnail("Erreur génération", doc_dir / "1.jpg")
            return 1
        except Exception:
            return 0


def save_pil_images_as_thumbnails(
    images: list['Image.Image'],
    doc_dir: Path,
    overwrite: bool = False,
) -> int:
    """Persist a list of in-memory PIL images as ``doc_dir/{1..N}.jpg``.

    Used by the LLM pipeline to "capitalize" on the cover extraction
    it already does: after sending images to the LLM, save them to
    the dashboard's thumbnail cache instead of throwing them away.

    Saves at the same 400×550 resolution + JPEG quality as the
    on-demand generator (``generate_thumbnail``), so the two paths
    produce visually identical caches.

    ``overwrite=False`` (default) skips pages whose file already
    exists — cheap idempotent re-runs. Returns the number of images
    actually written.
    """
    if not images:
        return 0
    doc_dir.mkdir(parents=True, exist_ok=True)
    n_written = 0
    for i, img in enumerate(images, start=1):
        dest = doc_dir / f"{i}.jpg"
        if dest.exists() and not overwrite:
            continue
        try:
            _resize_and_save(img, dest)
            n_written += 1
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(f"  ⚠ Erreur save thumbnail {dest.name}: {exc}")
    return n_written


def _generate_pdf_thumbnails(
    pdf_path: Path, doc_dir: Path, n_pages: int, start_page: int,
) -> int:
    """Génère les pages start_page..start_page+n_pages-1 d'un PDF.

    Idempotent au niveau page : si ``doc_dir/N.jpg`` existe déjà pour
    une page N demandée, elle est skippée — pas de re-rendering, pas
    de ré-écriture. Seules les pages manquantes sont passées à
    Poppler. Permet :

      - reprises de runs interrompus sans gâcher le travail déjà fait
      - upgrades incrémentaux (``--pages 1`` puis ``--pages 5`` ne
        re-rend pas la page 1)
    """
    from lib.pdf_cover import get_cover_image

    pages_wanted = list(range(start_page, start_page + n_pages))
    pages_missing = [p for p in pages_wanted
                     if not (doc_dir / f"{p}.jpg").exists()]
    if not pages_missing:
        return 0   # tout est déjà en cache, rien à écrire

    # Une seule invocation Poppler couvrant les pages manquantes (le
    # span first..last est rendu d'un coup, get_cover_image filtre).
    images = get_cover_image(str(pdf_path), page_indices=tuple(pages_missing))
    if not images:
        if 1 in pages_missing:
            _generate_placeholder_thumbnail(
                "Erreur PDF\nExtraction impossible", doc_dir / "1.jpg")
            return 1
        return 0

    # get_cover_image retourne les images dans l'ordre des page_indices
    # SORTÉS. Si le PDF est plus court que ``last``, certaines pages
    # de la fin sont silencieusement absentes : zip() tronque
    # proprement à la liste la plus courte.
    n_written = 0
    for page_num, img in zip(pages_missing, images):
        _resize_and_save(img, doc_dir / f"{page_num}.jpg")
        n_written += 1
    return n_written


def _generate_epub_thumbnails(
    epub_path: Path, doc_dir: Path, _n_pages: int, start_page: int,
) -> int:
    """ePub : on n'a que la cover, donc page 1 uniquement (n_pages ignoré).
    Idempotent : skip si 1.jpg existe déjà."""
    if start_page > 1:
        # Pas d'extraction de pages internes pour ePub : on signale "page absente"
        return 0
    dest = doc_dir / "1.jpg"
    if dest.exists():
        return 0
    try:
        with zipfile.ZipFile(epub_path) as zf:
            cover_data = _extract_epub_cover(zf)
            if cover_data is None:
                _generate_placeholder_thumbnail(
                    "📖 ePub\nPas de couverture", dest)
                return 1
            img = Image.open(io.BytesIO(cover_data))
            _resize_and_save(img, dest)
            return 1
    except (zipfile.BadZipFile, OSError):
        _generate_placeholder_thumbnail(
            "📖 ePub\nFichier corrompu", dest)
        return 1


def _extract_epub_cover(zf: zipfile.ZipFile) -> bytes | None:
    """Extrait l'image de cover d'un ePub ouvert.

    Cherche dans cet ordre :
    1. Item du manifest avec properties="cover-image" (ePub 3)
    2. Item référencé par <meta name="cover" content="..."> (ePub 2)
    3. Premier item d'image trouvé en fallback
    """
    # 1. Trouver le .opf via container.xml
    try:
        container_xml = zf.read("META-INF/container.xml").decode("utf-8")
    except KeyError:
        return None

    container = ET.fromstring(container_xml)
    rootfile = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
    if rootfile is None:
        return None
    opf_path = rootfile.get("full-path")
    if not opf_path:
        return None

    try:
        opf_xml = zf.read(opf_path).decode("utf-8")
    except KeyError:
        return None

    opf = ET.fromstring(opf_xml)
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    opf_dir = str(Path(opf_path).parent) if "/" in opf_path else ""

    # 2. ePub 3 : properties="cover-image"
    cover_href = None
    for item in opf.findall(".//opf:manifest/opf:item", ns):
        props = item.get("properties", "")
        if "cover-image" in props.split():
            cover_href = item.get("href")
            break

    # 3. ePub 2 : <meta name="cover" content="cover-id">
    if not cover_href:
        meta = opf.find(".//opf:metadata/opf:meta[@name='cover']", ns)
        if meta is not None:
            cover_id = meta.get("content")
            if cover_id:
                item = opf.find(
                    f".//opf:manifest/opf:item[@id='{cover_id}']", ns)
                if item is not None:
                    cover_href = item.get("href")

    # 4. Fallback : premier item image
    if not cover_href:
        for item in opf.findall(".//opf:manifest/opf:item", ns):
            mt = item.get("media-type", "")
            if mt.startswith("image/"):
                cover_href = item.get("href")
                break

    if not cover_href:
        return None

    cover_path = f"{opf_dir}/{cover_href}" if opf_dir else cover_href
    cover_path = cover_path.lstrip("/")

    try:
        return zf.read(cover_path)
    except KeyError:
        return None


def _resize_and_save(img: Image.Image, output_path: Path) -> None:
    """Redimensionne une image PIL à 400×550 (cover crop centré) et l'enregistre en JPEG."""
    if img.mode != "RGB":
        img = img.convert("RGB")

    src_w, src_h = img.size
    target_ratio = THUMBNAIL_WIDTH / THUMBNAIL_HEIGHT
    src_ratio = src_w / src_h

    # Cover crop centré : on remplit la zone cible quitte à rogner
    if src_ratio > target_ratio:
        # Image trop large : on rogne sur les côtés
        new_w = int(src_h * target_ratio)
        offset = (src_w - new_w) // 2
        img = img.crop((offset, 0, offset + new_w, src_h))
    else:
        # Image trop haute : on rogne en haut/bas
        new_h = int(src_w / target_ratio)
        offset = (src_h - new_h) // 2
        img = img.crop((0, offset, src_w, offset + new_h))

    img = img.resize((THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), Image.LANCZOS)
    img.save(output_path, format="JPEG", quality=THUMBNAIL_QUALITY)


def _generate_placeholder_thumbnail(message: str, output_path: Path) -> bool:
    """Crée un placeholder 400×550 gris foncé avec le message centré."""
    img = Image.new("RGB", (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), PLACEHOLDER_BG)
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
    except OSError:
        font = ImageFont.load_default()

    lines = message.split("\n")
    total_h = len(lines) * 28
    y = (THUMBNAIL_HEIGHT - total_h) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (THUMBNAIL_WIDTH - line_w) // 2
        draw.text((x, y), line, fill=PLACEHOLDER_FG, font=font)
        y += 28

    img.save(output_path, format="JPEG", quality=THUMBNAIL_QUALITY)
    return True


def get_doc_dir(cache_dir: Path, stem: str) -> Path:
    """Retourne le sous-dossier de cache pour un document donné."""
    return cache_dir / stem


def count_pages(cache_dir: Path, stem: str) -> int:
    """Compte les pages en cache pour un document.

    Returns:
        Nombre de fichiers {1,2,...}.jpg présents dans cache_dir/stem/
    """
    doc_dir = cache_dir / stem
    if not doc_dir.is_dir():
        return 0
    return sum(1 for p in doc_dir.glob("*.jpg") if p.stem.isdigit())


def clear_cache(cache_dir: Path) -> tuple[int, int]:
    """Supprime tout le contenu du cache (sous-dossiers + fichiers à plat).

    Returns:
        (image_count, total_bytes_freed)
    """
    if not cache_dir.exists():
        return (0, 0)

    import shutil
    count = 0
    freed = 0

    # Ancien format (legacy) : .jpg à plat
    for jpg in cache_dir.glob("*.jpg"):
        try:
            freed += jpg.stat().st_size
            jpg.unlink()
            count += 1
        except OSError:
            continue

    # Nouveau format : sous-dossiers par document
    for entry in cache_dir.iterdir():
        if not entry.is_dir():
            continue
        for jpg in entry.glob("*.jpg"):
            try:
                freed += jpg.stat().st_size
                count += 1
            except OSError:
                continue
        try:
            shutil.rmtree(entry)
        except OSError:
            continue

    return (count, freed)


def get_cache_stats(cache_dir: Path) -> dict:
    """Retourne les stats du cache (compte total d'images et taille)."""
    if not cache_dir.exists():
        return {"count": 0, "size_bytes": 0}

    count = 0
    size = 0

    # Ancien format
    for jpg in cache_dir.glob("*.jpg"):
        try:
            size += jpg.stat().st_size
            count += 1
        except OSError:
            continue

    # Nouveau format (sous-dossiers)
    for entry in cache_dir.iterdir():
        if not entry.is_dir():
            continue
        for jpg in entry.glob("*.jpg"):
            try:
                size += jpg.stat().st_size
                count += 1
            except OSError:
                continue

    return {"count": count, "size_bytes": size}
