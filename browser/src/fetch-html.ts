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
import { launchBrowser, newPreparedPage } from "./launch.js";

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

async function main(): Promise<void> {
  const { url, cookiesFile } = parseArgs();
  if (!url) {
    console.error(
      "Usage: fetch-html.ts --url <url> [--cookies-file <cookies.json>]",
    );
    process.exit(2);
  }

  const browser = await launchBrowser();

  try {
    const page = await newPreparedPage(browser);

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
