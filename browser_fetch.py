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


def _script_command(name: str) -> Optional[List[str]]:
    """
    Command to run browser script `name` (e.g. "fetch-html"): tsx on the TypeScript
    source in a dev checkout, else node on the compiled JS (the image ships no tsx).
    """
    tsx = _tsx_binary()
    src = _BROWSER_DIR / "src" / f"{name}.ts"
    if tsx is not None and src.is_file():
        return [str(tsx), str(src)]
    dist = _BROWSER_DIR / "dist" / f"{name}.js"
    if dist.is_file():
        return ["node", str(dist)]
    return None


def fetch_html_via_puppeteer(
    url: str,
    session: Optional["requests.Session"] = None,
) -> Optional[str]:
    """
    Run browser/src/fetch-html.ts; return page HTML or None on failure.
    """
    script = _script_command("fetch-html")
    if script is None:
        logger.warning(
            "Puppeteer bridge not installed — run: cd browser && npm install "
            "(needed for fetch-html.ts)"
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
            *script,
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


def _run_bill_script(session: "requests.Session", args: List[str], label: str) -> Optional[List[str]]:
    """Run browser/src/fetch-bill-pdf.ts with the session's cookies; return stdout lines."""
    script = _script_command("fetch-bill-pdf")
    if script is None:
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

        cmd: List[str] = [*script, "--cookies-file", tmp_path, *args]
        proc = subprocess.run(
            cmd,
            cwd=str(_BROWSER_DIR),
            capture_output=True,
            text=True,
            timeout=_puppeteer_timeout(),
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            logger.warning("%s failed (%s): %s", label, proc.returncode, err[:500])
            return None
        return (proc.stdout or "").strip().splitlines()
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out", label)
        return None
    except Exception as e:
        logger.warning("%s error: %s", label, e)
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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
    args = ["--out-pdf", out_pdf]
    if bill_id:
        args.extend(["--bill-id", bill_id])
    logger.info("Fetching Duquesne bill PDF via Puppeteer (bill_id=%s)", bill_id or "latest")
    lines = _run_bill_script(session, args, "Bill PDF fetch")
    if lines is None:
        return None
    if not lines:
        logger.warning("Bill PDF fetch returned no summary JSON")
        return None
    try:
        return json.loads(lines[-1])
    except ValueError as e:
        logger.warning("Bill PDF fetch returned unreadable summary: %s", e)
        return None


def fetch_duquesne_bill_history_via_puppeteer(
    session: "requests.Session",
    out_dir: str,
    skip_ids: Optional[List[str]] = None,
) -> Optional[List[dict]]:
    """
    Download every bill PDF in the account's bill history except skip_ids.

    Returns one dict per history row — {"billId", "date", "pdfPath", "bytes"}, or
    {"billId", "date", "skipped": True} for skip_ids — or None on failure.
    """
    args = ["--all", "--out-dir", out_dir]
    if skip_ids:
        args.extend(["--skip-ids", ",".join(skip_ids)])
    logger.info(
        "Fetching Duquesne bill history via Puppeteer (%d already archived)", len(skip_ids or [])
    )
    lines = _run_bill_script(session, args, "Bill history fetch")
    if lines is None:
        return None
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            logger.debug("Skipping non-JSON bill history line: %s", line[:200])
    return rows
