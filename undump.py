#!/usr/bin/env python3
"""
cli.py
~~~~~~
Command‑line utility that reorganises an *archive/dump* of files into a
clean *collection/output* directory, driven by **RDF metadata**.

Directory assumptions
=====================
* **Archive**: ``ARCHIVE_DIR/<UUID>/<FILETYPE>/<original_filename>.<ext>``
  – there may be multiple file types (``html``, ``pdf``, ``docx`` …). The
  script treats them transparently: the original extension is preserved.
* **Metadata**: ``METADATA_DIR/<UUID>/tree_non_inferred.rdf``.  The RDF
  filename is configurable with the global ``RDF_FNAME`` constant.

New in this version
-------------------
* The RDF notice is discovered by matching the *UUID* segment shared
  between the archive and metadata trees.
* A ``--limit`` option allows deterministic, repeatable test runs by
  processing only the first *N* files (files are sorted alphabetically).
* The CLI continues to support folder/filename masks, verbosity flags,
  etc.

Dependencies::

    pip install click rdflib
"""
from __future__ import annotations

import logging
import shutil
import sys
import re
import unicodedata
from pathlib import Path
from typing import Dict, Mapping, List

import click
from rdflib import Graph, Namespace, Literal
from rdflib.namespace import OWL

###############################################################################
# Global configuration
###############################################################################

RDF_FNAME = "tree_non_inferred.rdf"  # change if your metadata file differs
MAX_SEGMENT_LEN = 30                 # max characters in any path segment

###############################################################################
# Namespaces used in Cellar RDF
###############################################################################

n_CDM = Namespace("http://publications.europa.eu/ontology/cdm#")
n_LANG = Namespace("http://publications.europa.eu/resource/authority/language/")

###############################################################################
# Slugification helpers
###############################################################################

def slugify(raw: str, max_len: int = MAX_SEGMENT_LEN) -> str:
    """Return *raw* normalised for safe filesystem use.

    Steps:
    1. Unicode → ASCII (remove diacritics).
    2. Replace any char not in [A‑Z a‑z 0‑9 _ . -] with ``_``.
    3. Trim leading/trailing punctuations ``._-``.
    4. Truncate to *max_len* characters.
    5. Provide fallback name ``unnamed`` if result empty.
    """
    # 1. transliterate
    ascii_str = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    # 2. replace bad chars
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_str)
    # 3. strip noise
    safe = safe.strip("._-")
    # 4. length guard
    safe = safe[:max_len].rstrip("._-")
    # 5. fallback
    return safe or "unnamed"

###############################################################################
# Metadata extraction helpers
###############################################################################

def parse_metadata(rdf_path: Path, language: str) -> Dict[str, str]:
    """Return a dict of values extracted from *rdf_path* using SPARQL.

    The UUID is inferred from the parent directory name, then injected
    into the `ROOT_URI` required by the Cellar data model.
    """
    uuid = rdf_path.parent.name
    root_uri = f"http://publications.europa.eu/resource/cellar/{uuid}"

    g = Graph()
    # Bind namespaces for readability in possible debug prints
    g.bind("cdm", n_CDM)
    g.bind("lang", n_LANG)
    g.bind("owl", OWL)
    g.parse(rdf_path)

    query = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX lang: <http://publications.europa.eu/resource/authority/language/>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
SELECT ?date ?eli ?type ?year ?title ?subtitle ?celex_identifier WHERE {{
    <{root_uri}> owl:sameAs ?work .
    ?work cdm:date_creation_legacy ?date ;
          cdm:resource_legal_eli ?eli ;
          cdm:work_has_resource-type ?type ;
          cdm:resource_legal_id_celex ?celex_identifier ;
          cdm:resource_legal_year ?year .

    ?exp cdm:expression_belongs_to_work ?work ;
         cdm:expression_uses_language lang:{language} ;
         cdm:expression_title ?title ;
         cdm:expression_subtitle ?subtitle .
}}
LIMIT 1
"""

    res = list(g.query(query))
    if not res:
        # Fallback – return minimal defaults so the rest of the pipeline continues
        return {
            "year": "1970",
            "month": "01",
            "day": "01",
            "date": "1970-01-01",
            "title": "Untitled",
            "subtitle": "",
            "type": "UNKNOWN",
            "eli": "",
            "celex_identifier": "",
            "default_identifier": uuid,
        }

    row = res[0]
    date_literal: Literal = row.date  # type: ignore[attr-defined]
    date_str = str(date_literal)
    try:
        year, month, day = date_str.split("-")
    except ValueError:
        year, month, day = "", "", ""

    return {
        "year": year,
        "month": month,
        "day": day,
        "date": date_str,
        "title": slugify(str(row.title)),          # type: ignore[attr-defined]
        "subtitle": str(row.subtitle),    # type: ignore[attr-defined]
        "type": str(row.type),            # type: ignore[attr-defined]
        "eli": str(row.eli),              # type: ignore[attr-defined]
        "celex_identifier": str(row.celex_identifier),
        "default_identifier": uuid,
    }
###############################################################################
# Template & filesystem helpers
###############################################################################

def render_mask(mask: str, values: Mapping[str, str]) -> str:
    """Safely substitute *mask* using *values* (missing keys → empty str)."""

    class _SafeDict(dict):
        def __missing__(self, key):  # noqa: D401 – single‑line docstring fine
            return f"[{key}NotFound]"

    return mask.format_map(_SafeDict(values))


def build_destination(
    output_root: Path,
    metadata: Mapping[str, str],
    folder_mask: str,
    file_mask: str,
    src_path: Path,
) -> Path:
    # ---- sub‑folder path (slugify each part) ------
    raw_sub = render_mask(folder_mask, metadata).strip("/\\")
    if raw_sub:
        dest_dir = output_root
        for segment in Path(raw_sub).parts:
            #dest_dir /= slugify(segment)
            dest_dir /= segment
    else:
        dest_dir = output_root

    # ---- file stem (slugified) ------
    raw_stem = render_mask(file_mask, metadata).strip() or metadata.get("default_identifier") or src_path.stem
    #file_stem = slugify(raw_stem)
    file_stem = raw_stem

    return dest_dir / f"{file_stem}{src_path.suffix}"

def ensure_unique_path(path: Path, fallback: str) -> Path:
    """Return a unique *path* by appending *fallback* & counter if needed."""
    candidate = path
    counter = 1
    while candidate.exists():
        candidate = candidate.with_stem(f"{path.stem}_{fallback}_{counter}")
        counter += 1
    return candidate


def copy_with_structure(
    src: Path,
    archive_root: Path,
    output_root: Path,
    metadata_root: Path,
    folder_mask: str,
    file_mask: str,
    language: str,
    logger: logging.Logger,
):
    """Process a single *src* file: find RDF, compute target path, copy."""

    # Derive UUID from archive path (first segment after archive_root)
    logger.debug("Applying folder and file mask %s and %s", folder_mask, file_mask)
    try:
        uuid = src.relative_to(archive_root).parts[0]
    except ValueError:
        logger.warning("Could not determine UUID for %s", src)
        return
    logger.debug("Found UUID  %s", uuid)

    rdf_path = metadata_root / uuid / RDF_FNAME
    if not rdf_path.is_file():
        logger.warning("No RDF for %s (expected %s)", src.name, rdf_path)
        return
    logger.debug("Found matching rdf %s", rdf_path)

    metadata = parse_metadata(rdf_path, language)
    logger.debug(metadata)
    dest_path = build_destination(
            output_root, 
            metadata, 
            folder_mask, 
            file_mask, 
            src)

    if dest_path.exists():
        logger.warning("Conflict: %s exists – appending default identifier", dest_path.name)
        dest_path = ensure_unique_path(dest_path, uuid)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_path)
    logger.debug("Copied %s → %s", src, dest_path)

###############################################################################
# CLI definition
###############################################################################

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("archive_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("output_dir", type=click.Path(file_okay=False, path_type=Path))
@click.argument("metadata_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--folder-mask",
    default="{year}/{month}",
    show_default=True,
    help="Template for destination sub‑folders. Empty string → flat structure.",
)
@click.option(
    "--file-mask",
    default="{title}",
    show_default=True,
    help="Template for destination filename (without extension).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    metavar="N",
    help="Process only the first N files (alphabetical order) – useful for testing.",
)
@click.option(
    "--language",
    default="ENG",
    show_default=True,
    help="Language of metadata values to be retrieved (Three letters, default ENG)",
)
@click.option("-v", "--verbose", count=True, help="Increase log verbosity (-v / -vv).")
@click.version_option(package_name="cli-renamer", prog_name="cli-renamer")
def main(
    archive_dir: Path,
    output_dir: Path,
    metadata_dir: Path,
    folder_mask: str,
    file_mask: str,
    limit: int | None,
    language: str,
    verbose: int,
):

    """Rename & copy files from ARCHIVE_DIR into OUTPUT_DIR using Cellar RDF.

    Variables available to masks: ``year``, ``month``, ``day``, ``date``,
    ``title``, ``subtitle``, ``type``, ``eli``, ``celex_identifier``,
    ``default_identifier``.
    """

    log_lvl = logging.WARNING - (10 * min(verbose, 2))
    logging.basicConfig(level=log_lvl, format="%(levelname)s: %(message)s", stream=sys.stderr)
    logger = logging.getLogger("cli-renamer")

    # Gather and sort archive files deterministically
    files: List[Path] = sorted(
        (p for p in archive_dir.rglob("*") if p.is_file()),
        key=lambda p: p.as_posix(),
    )
    if limit is not None and limit > 0:
        files = files[:limit]
        logger.info("Test mode: limiting to %d files", limit)

    logger.info("Processing %d file(s)…", len(files))

    for path in files:
        try:
            copy_with_structure(
                src=path,
                archive_root=archive_dir,
                output_root=output_dir,
                metadata_root=metadata_dir,
                folder_mask=folder_mask,
                file_mask=file_mask,
                language=language,
                logger=logger,
            )
        except Exception:  # noqa: BLE001 – broad except acceptable for CLI
            logger.exception("Failed to process %s", path)

    logger.info("Done.")


if __name__ == "__main__":
    main()
