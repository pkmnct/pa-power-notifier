"""
Optional Puppeteer (TypeScript) bridge for pages that block plain HTTP (e.g. Imperva Incapsula).

Install once:  cd browser && npm install

Env (inherited by subprocess — set in shell or .env loaded by Python):

  DUQUESNE_USE_PUPPETEER   — 1/true: always try browser for Duquesne fetches when relevant
  DUQUESNE_PUPPETEER_TIMEOUT — seconds (default 150)

Puppeteer-only (see browser/src/fetch-html.ts):

  DUQUESNE_BROWSER_MODE — auto (default): a visible Chrome window, on the real display or a
      virtual one (Xvfb) when there is none; headless if neither works. window: the same, but
      warns when it has to fall back. headless: headless Chrome. shell: the legacy headless
      binary. (DUQUESNE_PUPPETEER_HEADLESS is read as a fallback: 0/false/no = window,
      1/true/yes = headless.) The Node scripts only ever see DUQUESNE_PUPPETEER_HEADLESS = 0/1/shell,
      set here.
  DUQUESNE_PUPPETEER_USER_AGENT — override the user agent. Default: the browser's own,
      minus "HeadlessChrome", so it matches the platform it runs on.

Plain Puppeteer does not guarantee bypassing Imperva. A visible window (real or virtual)
passes far more often than headless Chrome.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, NamedTuple, Optional

if TYPE_CHECKING:
    import requests

from energy_shopper_core.utilities.duquesne import html_looks_like_bot_challenge  # noqa: F401 (re-exported)

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent
_BROWSER_DIR = _REPO_ROOT / "browser"


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


_HEADLESS_YES = {"1", "true", "yes", "on", "headless", "new"}
_HEADED_YES = {"0", "false", "no", "off", "headed", "window"}

_xvfb: Optional[subprocess.Popen] = None
_xvfb_display: Optional[str] = None


def _stop_virtual_display() -> None:
    global _xvfb
    if _xvfb is not None and _xvfb.poll() is None:
        try:
            os.killpg(_xvfb.pid, signal.SIGKILL)
        except OSError:
            pass
    _xvfb = None


def _virtual_display() -> Optional[str]:
    """
    DISPLAY of a shared Xvfb server, started on first use and reused by every fetch in this
    process (None if Xvfb isn't installed or won't start).
    """
    global _xvfb, _xvfb_display
    if _xvfb is not None and _xvfb.poll() is None:
        return _xvfb_display
    exe = shutil.which("Xvfb")
    if not exe:
        return None
    try:
        proc = subprocess.Popen(
            [exe, "-displayfd", "1", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        line = (proc.stdout.readline() if proc.stdout else "").strip()
        if proc.stdout:
            proc.stdout.close()
    except OSError as e:
        logger.warning("Could not start Xvfb: %s", e)
        return None
    if not line.isdigit():
        logger.warning("Xvfb did not report a display; not using a virtual display")
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        return None
    if _xvfb is None:
        atexit.register(_stop_virtual_display)
    _xvfb, _xvfb_display = proc, f":{line}"
    return _xvfb_display


def _display_usable(display: str, exists: Callable[[str], bool]) -> bool:
    """A local DISPLAY (:N) is usable only if its X socket exists; remote ones are trusted."""
    m = re.match(r":(\d+)", display)
    return exists(f"/tmp/.X11-unix/X{m.group(1)}") if m else True


class BrowserLaunch(NamedTuple):
    env: dict  # overrides for the browser subprocess's environment
    label: str  # for the log: "headless", "windowed" or "virtual-display"


def _browser_launch(
    environ=None,
    exists: Callable[[str], bool] = os.path.exists,
    start_display: Callable[[], Optional[str]] = _virtual_display,
) -> BrowserLaunch:
    """
    How to run a browser script. A visible Chrome window passes bot walls more often than
    headless Chrome, so unless headless is asked for, a window is used: the real display if
    it works, else a shared virtual one (Xvfb), when installed. With neither, headless.
    """
    environ = os.environ if environ is None else environ
    mode = (
        environ.get("DUQUESNE_BROWSER_MODE") or environ.get("DUQUESNE_PUPPETEER_HEADLESS") or ""
    ).strip().lower()
    if mode == "shell":
        return BrowserLaunch({"DUQUESNE_PUPPETEER_HEADLESS": "shell"}, "headless (shell)")
    if mode in _HEADLESS_YES:
        return BrowserLaunch({"DUQUESNE_PUPPETEER_HEADLESS": "1"}, "headless")
    display = environ.get("DISPLAY") or ""
    if display and _display_usable(display, exists):
        return BrowserLaunch({"DUQUESNE_PUPPETEER_HEADLESS": "0"}, "windowed")
    virtual = start_display()
    if virtual:
        return BrowserLaunch({"DUQUESNE_PUPPETEER_HEADLESS": "0", "DISPLAY": virtual}, "virtual-display")
    if mode in _HEADED_YES:
        logger.warning(
            "A visible browser window was requested but there is no usable display and Xvfb "
            "isn't available: running headless"
        )
    return BrowserLaunch({"DUQUESNE_PUPPETEER_HEADLESS": "1"}, "headless")


def _run_browser(cmd: List[str], env: dict) -> "subprocess.CompletedProcess[str]":
    """
    Run a browser script. It gets its own process group so that on timeout the whole tree
    (node and Chromium) is killed, not just the direct child; raises TimeoutExpired.
    """
    proc = subprocess.Popen(
        cmd,
        env={**os.environ, **env},
        cwd=str(_BROWSER_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=_puppeteer_timeout())
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def describe_challenge(html: str) -> str:
    """What a blocked page was, for the log: its title and the start of its visible text."""
    html = html or ""
    if not html.strip():
        return "empty page"
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    kind = "Incapsula" if "Incapsula" in html else "unknown"
    return f"{len(html)} bytes, title={title[:80]!r}, kind={kind}, text={text[:160]!r}"


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

        launch = _browser_launch()
        cmd: List[str] = [
            *script,
            "--url",
            url,
            "--cookies-file",
            tmp_path,
        ]
        logger.info("Fetching with Puppeteer (%s Chrome): %s", launch.label, url)
        proc = _run_browser(cmd, launch.env)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            logger.warning("Puppeteer fetch failed (%s): %s", proc.returncode, err[:500])
            return None
        html = proc.stdout
        if html_looks_like_bot_challenge(html):
            logger.warning(
                "Puppeteer still received a bot-challenge or empty page (%s). If this "
                "repeats, your IP may be blocked; check DUQUESNE credentials, or set "
                "DUQUESNE_BROWSER_MODE / DUQUESNE_PUPPETEER_USER_AGENT.",
                describe_challenge(html),
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

        launch = _browser_launch()
        cmd: List[str] = [*script, "--cookies-file", tmp_path, *args]
        proc = _run_browser(cmd, launch.env)
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
