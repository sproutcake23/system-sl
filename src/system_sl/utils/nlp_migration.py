"""One-time cleanup of the retired spaCy embedding backend.

Releases before this change built persona vectors with spaCy and depended on
``spacy`` plus the ``en_core_web_md`` model. The current release uses
``sentence-transformers`` with all-MiniLM-L6-v2 and never imports spaCy.

Frozen builds and AppImages replace their whole bundle on update, so the old
packages are gone with the old files. A pip or editable install is different:
upgrading installs the new requirements but does not uninstall packages that
are no longer listed as dependencies, so spaCy can linger. This module removes
those leftovers so an upgraded environment ends up like a clean install.

Every failure is logged and swallowed. A cleanup that cannot run must never
stop the app from starting.
"""

import importlib.metadata
import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

# Packages that existed only to provide the old spaCy embeddings. spaCy's
# generic transitive dependencies (thinc, srsly, ...) are deliberately left
# alone: some are shared with other tools, and removing them is pip's job.
_LEGACY_PACKAGES = ("spacy", "en_core_web_md", "spacy-legacy", "spacy-loggers")


def _installed(distribution: str) -> bool:
    """Return True when ``distribution`` is present in this interpreter."""
    try:
        importlib.metadata.distribution(distribution)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def legacy_packages_installed() -> list[str]:
    """Return the retired spaCy packages still present in this environment."""
    return [name for name in _LEGACY_PACKAGES if _installed(name)]


def cleanup_legacy_nlp() -> None:
    """Uninstall leftover spaCy packages from a pip or editable install.

    Frozen builds and AppImages carry their own packages, so there is nothing
    to clean up and this returns immediately. Outside a virtual environment the
    app does not modify the interpreter's packages at all; it only logs the
    command that removes them, so a system Python shared with other software is
    never touched.
    """
    if getattr(sys, "frozen", False) or os.environ.get("APPIMAGE"):
        return

    legacy = legacy_packages_installed()
    if not legacy:
        return

    in_virtualenv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if not in_virtualenv:
        log.info(
            "Retired NLP packages are still installed in the system Python "
            "(%s). Remove them with: python -m pip uninstall -y %s",
            ", ".join(legacy),
            " ".join(legacy),
        )
        return

    log.info("Removing retired NLP packages: %s", ", ".join(legacy))
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", *legacy],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must never break startup
        log.warning("Could not remove the retired spaCy packages: %s", exc)
        return

    if result.returncode != 0:
        # A uv-managed virtualenv may not include pip; that is not an error the
        # player needs to see, because ``uv sync`` already pruned these.
        log.info(
            "Could not remove the retired spaCy packages automatically "
            "(exit %d): %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )
    else:
        log.info(
            "Retired NLP packages removed; sentence-transformers is now the "
            "only embedding backend."
        )