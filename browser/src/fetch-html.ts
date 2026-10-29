/**
 * Load a URL in headless Chrome and print final HTML to stdout.
 * Used when Duquesne / Imperva Incapsula returns a bot wall to Python requests.
 *
 * Usage:
 *   npx tsx src/fetch-html.ts --url <https://...> [--cookies-file <path.json>]
 *
 * cookies-file: JSON array of { name, value, domain, path? } (Puppeteer cookie shape).
 */
import * as fs from "node:fs";
import puppeteer from "puppeteer";

type CookieRow = {
  name: string;
  value: string;
  domain?: string;
  path?: string;
};

function parseArgs(): { url: string; cookiesFile: string | null } {
  const argv = process.argv.slice(2);
  let url = "";
  let cookiesFile: string | null = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--url") {
      url = argv[++i] ?? "";
    } else if (argv[i] === "--cookies-file") {
      cookiesFile = argv[++i] ?? null;
    }
  }
  return { url, cookiesFile };
}

function parseHeadless(): boolean | "shell" {
  const v = process.env.DUQUESNE_PUPPETEER_HEADLESS?.trim().toLowerCase();
  if (v === "0" || v === "false" || v === "no") {
    return false;
  }
  if (v === "shell") {
    // Uses Chromium's separate headless-shell binary — install it first:
    //   npx puppeteer browsers install chrome-headless-shell
    return "shell";
  }
  // Default: Puppeteer v22+ "headless: true" already runs the modern ("new") headless
  // mode against the regular Chrome binary — no extra binary install needed, and it's
  // less distinguishable from a real browser than the legacy/"shell" headless mode.
  return true;
}

async function main(): Promise<void> {
  const { url, cookiesFile } = parseArgs();
  if (!url) {
    console.error(
      "Usage: fetch-html.ts --url <url> [--cookies-file <cookies.json>]",
    );
    process.exit(2);
  }

  const headless = parseHeadless();
  const browser = await puppeteer.launch({
    headless,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      // Docker's default 64MB /dev/shm is too small for Chromium.
      "--disable-dev-shm-usage",
      // Reduces one common automation signal (not enough alone vs Imperva).
      "--disable-blink-features=AutomationControlled",
    ],
  });

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 800 });
    const ua =
      process.env.DUQUESNE_PUPPETEER_USER_AGENT?.trim() ||
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
    await page.setUserAgent(ua);

    await page.evaluateOnNewDocument(() => {
      Object.defineProperty(navigator, "webdriver", {
        get: () => undefined,
      });
    });

    if (cookiesFile && fs.existsSync(cookiesFile)) {
      const raw = fs.readFileSync(cookiesFile, "utf8");
      const parsed = JSON.parse(raw) as CookieRow[];
      if (!Array.isArray(parsed)) {
        throw new Error("cookies-file must contain a JSON array");
      }
      for (const c of parsed) {
        const domain =
          c.domain?.startsWith(".") ? c.domain : c.domain ?? new URL(url).hostname;
        await page.setCookie({
          name: c.name,
          value: c.value,
          domain,
          path: c.path || "/",
        });
      }
    }

    await page.goto(url, { waitUntil: "networkidle2", timeout: 120_000 });
    const html = await page.content();
    process.stdout.write(html);
  } finally {
    await browser.close();
  }
}

main().catch((err: unknown) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
