/**
 * Shared Chromium launch for the Duquesne fetch scripts.
 *
 * The browser should look like what it is: a consistent Chrome on this machine's own
 * platform. A hard-coded macOS user agent on a Linux container is a mismatch bot walls
 * (Imperva Incapsula) can detect, so the user agent is the browser's own with the
 * "HeadlessChrome" marker removed, unless DUQUESNE_PUPPETEER_USER_AGENT overrides it.
 *
 * DUQUESNE_PUPPETEER_HEADLESS: 0/false/no = a visible window (browser_fetch.py runs it
 * under a virtual display where there is none); shell = the legacy headless binary;
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
  return puppeteer.launch({
    headless: parseHeadless(),
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

/** A page with a normal viewport and a user agent that matches the browser. */
export async function newPreparedPage(browser: Browser): Promise<Page> {
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 800 });
  const override = process.env.DUQUESNE_PUPPETEER_USER_AGENT?.trim();
  const ua = override || (await browser.userAgent()).replace(/HeadlessChrome/g, "Chrome");
  await page.setUserAgent(ua);
  await page.setExtraHTTPHeaders({ "Accept-Language": "en-US,en;q=0.9" });
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, "webdriver", {
      get: () => undefined,
    });
  });
  return page;
}
