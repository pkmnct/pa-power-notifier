"""
Optional Puppeteer (TypeScript) bridge for pages that block plain HTTP (e.g. Imperva Incapsula).

Install once:  cd browser && npm install

Env (inherited by subprocess — set in shell or .env loaded by Python):

  DUQUESNE_USE_PUPPETEER   — 1/true: always try browser for Duquesne fetches when relevant
  DUQUESNE_PUPPETEER_TIMEOUT — seconds (default 150)

Puppeteer-only (see browser/src/fetch-html.ts):

  DUQUESNE_PUPPETEER_HEADLESS — 0 = visible Chrome window; shell/new = newer headless (default)
  DUQUESNE_PUPPETEER_USER_AGENT — override Chromium default UA

Plain Puppeteer does not guarantee bypassing Imperva; if still blocked, try headed mode
(DUQUESNE_PUPPETEER_HEADLESS=0) or puppeteer-extra-plugin-stealth.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    import requests

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent
_BROWSER_DIR = _REPO_ROOT / "browser"
_TS_SCRIPT = _BROWSER_DIR / "src" / "fetch-html.ts"


def html_looks_like_bot_challenge(html: str) -> bool:
    """Heuristic for CDN bot walls (Imperva Incapsula returns tiny placeholder HTML)."""
    if not html:
        return True
    if "_Incapsula_Resource" in html or "Incapsula incident ID" in html:
        return True
    if len(html) < 800 and ("Incapsula" in html or "incident id" in html.lower()):
        return True
    return False


def _session_cookies_for_puppeteer(session: "requests.Session", page_url: str) -> List[dict]:
    from urllib.parse import urlparse

    host = (urlparse(page_url).hostname or "").lstrip(".")
    rows: List[dict] = []
    for c in session.cookies:
        domain = c.domain
        if not domain:
            domain = host
        rows.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": domain,
                "path": getattr(c, "path", None) or "/",
            }
        )
    return rows


def _puppeteer_timeout() -> int:
    # `or`: Compose passes unset vars through as "" rather than leaving them unset.
    return int(os.getenv("DUQUESNE_PUPPETEER_TIMEOUT") or "150")


def _tsx_binary() -> Optional[Path]:
    p = _BROWSER_DIR / "node_modules" / ".bin" / "tsx"
    return p if p.is_file() else None


def fetch_html_via_puppeteer(
    url: str,
    session: Optional["requests.Session"] = None,
) -> Optional[str]:
    """
    Run browser/src/fetch-html.ts via tsx; return page HTML or None on failure.
    """
    if not _TS_SCRIPT.is_file():
        logger.debug("Puppeteer script missing at %s", _TS_SCRIPT)
        return None

    tsx = _tsx_binary()
    if tsx is None:
        logger.warning(
            "Puppeteer bridge not installed — run: cd browser && npm install "
            "(needed for fetch-html.ts / tsx)"
        )
        return None

    cookies: List[dict] = []
    if session is not None:
        cookies = _session_cookies_for_puppeteer(session, url)

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as tf:
            json.dump(cookies, tf)
            tmp_path = tf.name

        cmd: List[str] = [
            str(tsx),
            str(_TS_SCRIPT),
            "--url",
            url,
            "--cookies-file",
            tmp_path,
        ]
        logger.info("Fetching with Puppeteer (headless Chrome): %s", url)
        proc = subprocess.run(
            cmd,
            cwd=str(_BROWSER_DIR),
            capture_output=True,
            text=True,
            timeout=_puppeteer_timeout(),
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            logger.warning("Puppeteer fetch failed (%s): %s", proc.returncode, err[:500])
            return None
        html = proc.stdout
        if html_looks_like_bot_challenge(html):
            logger.warning(
                "Puppeteer still received a bot-challenge or empty page — "
                "you may need to run non-headless once or check DUQUESNE credentials."
            )
        return html
    except subprocess.TimeoutExpired:
        logger.warning("Puppeteer fetch timed out for %s", url)
        return None
    except Exception as e:
        logger.warning("Puppeteer fetch error: %s", e)
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def puppeteer_eager_enabled() -> bool:
    return os.getenv("DUQUESNE_USE_PUPPETEER", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def fetch_duquesne_bill_pdf_via_puppeteer(
    session: "requests.Session",
    out_pdf: str,
    bill_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Download the latest (or specified) Duquesne bill PDF via Puppeteer.

    Returns {"billId", "pdfPath", "bytes"} or None on failure.
    Requires browser/ npm install + Chrome for Testing.
    """
    script = _BROWSER_DIR / "src" / "fetch-bill-pdf.ts"
    if not script.is_file():
        logger.debug("Bill PDF script missing at %s", script)
        return None

    tsx = _tsx_binary()
    if tsx is None:
        logger.warning(
            "Puppeteer bridge not installed — run: cd browser && npm install "
            "(needed to fetch Duquesne bill PDFs for current EGS rate)"
        )
        return None

    cookies = _session_cookies_for_puppeteer(
        session, "https://www.duquesnelight.com/account-billing/account-summary"
    )
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as tf:
            json.dump(cookies, tf)
            tmp_path = tf.name

        cmd: List[str] = [
            str(tsx),
            str(script),
            "--cookies-file",
            tmp_path,
            "--out-pdf",
            out_pdf,
        ]
        if bill_id:
            cmd.extend(["--bill-id", bill_id])

        logger.info("Fetching Duquesne bill PDF via Puppeteer (bill_id=%s)", bill_id or "latest")
        proc = subprocess.run(
            cmd,
            cwd=str(_BROWSER_DIR),
            capture_output=True,
            text=True,
            timeout=_puppeteer_timeout(),
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            logger.warning("Bill PDF fetch failed (%s): %s", proc.returncode, err[:500])
            return None

        line = (proc.stdout or "").strip().splitlines()
        if not line:
            logger.warning("Bill PDF fetch returned no summary JSON")
            return None
        return json.loads(line[-1])
    except subprocess.TimeoutExpired:
        logger.warning("Bill PDF fetch timed out")
        return None
    except Exception as e:
        logger.warning("Bill PDF fetch error: %s", e)
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
