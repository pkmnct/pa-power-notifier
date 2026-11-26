/**
 * Shared Chromium launch for the Duquesne fetch scripts.
 *
 * The user agent is DEFAULT_USER_AGENT unless DUQUESNE_PUPPETEER_USER_AGENT overrides it.
 *
 * DUQUESNE_PUPPETEER_HEADLESS: 0/false/no = a visible window (browser_fetch.py sets this from
 * DUQUESNE_BROWSER_MODE, only for mode window, on a virtual display where there is none); shell = the legacy headless binary;
 * anything else = headless.
 */
import puppeteer, { type Browser, type Page } from "puppeteer";

export function parseHeadless(): boolean | "shell" {
  const v = process.env.DUQUESNE_PUPPETEER_HEADLESS?.trim().toLowerCase();
  if (v === "0" || v === "false" || v === "no") return false;
  if (v === "shell") return "shell";
  return true;
}

export async function launchBrowser(): Promise<Browser> {
  const headless = parseHeadless();
  return puppeteer.launch({
    headless,
    // A real window sizes its own viewport (--window-size); forcing one makes the inner
    // size equal the outer size, which no real browser has.
    defaultViewport: headless === false ? null : { width: 1280, height: 800 },
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      // Docker's default 64MB /dev/shm is too small for Chromium.
      "--disable-dev-shm-usage",
      // Reduces one common automation signal (not enough alone vs Imperva).
      "--disable-blink-features=AutomationControlled",
      "--window-size=1280,800",
      "--lang=en-US",
    ],
  });
}

/** Client hints (sec-ch-ua*, navigator.userAgentData) matching a Chrome user agent. */
function uaMetadata(ua: string) {
  const major = /Chrome\/(\d+)/.exec(ua)?.[1];
  const full = /Chrome\/([\d.]+)/.exec(ua)?.[1];
  if (!major || !full) return undefined;
  const platform = /Windows/.test(ua)
    ? "Windows"
    : /Macintosh/.test(ua)
      ? "macOS"
      : /Android/.test(ua)
        ? "Android"
        : "Linux";
  return {
    brands: [
      { brand: "Chromium", version: major },
      { brand: "Google Chrome", version: major },
      { brand: "Not=A?Brand", version: "24" },
    ],
    fullVersion: full,
    platform,
    platformVersion: "",
    architecture: process.arch === "arm64" ? "arm" : "x86",
    model: "",
    mobile: /Mobile/.test(ua),
  };
}

/**
 * Default user agent. Duquesne's bot protection (Imperva Incapsula) passed this string with
 * headless Chrome on 2026-11-22 (0.1.0) and blocked every run of 0.1.1, which used the
 * browser's own Linux user agent in a windowed Chrome. DUQUESNE_PUPPETEER_USER_AGENT overrides it.
 */
export const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

/** A page with a user agent (and client hints) that match the browser. */
export async function newPreparedPage(browser: Browser): Promise<Page> {
  const page = await browser.newPage();
  const override = process.env.DUQUESNE_PUPPETEER_USER_AGENT?.trim();
  const ua = override || DEFAULT_USER_AGENT;
  await page.setUserAgent(ua, uaMetadata(ua));
  await page.setExtraHTTPHeaders({ "Accept-Language": "en-US,en;q=0.9" });
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, "webdriver", {
      get: () => undefined,
    });
  });
  return page;
}
