/**
 * Fetch the latest Duquesne Light bill PDF using an authenticated cookie jar.
 *
 * Usage:
 *   npx tsx src/fetch-bill-pdf.ts --cookies-file <cookies.json> --out-pdf <path.pdf>
 *
 * cookies-file: JSON array of { name, value, domain, path? } (Puppeteer cookie shape).
 * Prints a one-line JSON summary to stdout: { billId, pdfPath, bytes }.
 */
import * as fs from "node:fs";
import puppeteer from "puppeteer";

type CookieRow = {
  name: string;
  value: string;
  domain?: string;
  path?: string;
};

function parseArgs(): {
  cookiesFile: string;
  outPdf: string;
  billId: string | null;
} {
  const argv = process.argv.slice(2);
  let cookiesFile = "";
  let outPdf = "";
  let billId: string | null = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--cookies-file") cookiesFile = argv[++i] ?? "";
    else if (argv[i] === "--out-pdf") outPdf = argv[++i] ?? "";
    else if (argv[i] === "--bill-id") billId = argv[++i] ?? null;
  }
  return { cookiesFile, outPdf, billId };
}

function parseHeadless(): boolean | "shell" {
  const v = process.env.DUQUESNE_PUPPETEER_HEADLESS?.trim().toLowerCase();
  if (v === "0" || v === "false" || v === "no") return false;
  if (v === "shell") return "shell";
  return true;
}

async function main(): Promise<void> {
  const { cookiesFile, outPdf, billId: billIdArg } = parseArgs();
  if (!cookiesFile || !outPdf) {
    console.error(
      "Usage: fetch-bill-pdf.ts --cookies-file <cookies.json> --out-pdf <path.pdf> [--bill-id <id>]",
    );
    process.exit(2);
  }

  const cookies = JSON.parse(fs.readFileSync(cookiesFile, "utf8")) as CookieRow[];
  if (!Array.isArray(cookies)) {
    throw new Error("cookies-file must contain a JSON array");
  }

  const browser = await puppeteer.launch({
    headless: parseHeadless(),
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      // Docker's default 64MB /dev/shm is too small for Chromium.
      "--disable-dev-shm-usage",
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

    for (const c of cookies) {
      const domain =
        c.domain?.startsWith(".") ? c.domain : c.domain ?? "www.duquesnelight.com";
      try {
        await page.setCookie({
          name: c.name,
          value: c.value,
          domain,
          path: c.path || "/",
        });
      } catch {
        // ignore cookies that Puppeteer rejects (wrong domain, etc.)
      }
    }

    await page.goto(
      "https://www.duquesnelight.com/account-billing/account-summary",
      { waitUntil: "networkidle2", timeout: 120_000 },
    );

    let billId = billIdArg;
    if (!billId) {
      const history = await page.evaluate(async () => {
        const r = await fetch(
          "/account-billing/account-summary/GetAccountBillHistory/",
          {
            credentials: "include",
            headers: {
              "X-Requested-With": "XMLHttpRequest",
              Accept: "application/json",
            },
          },
        );
        if (!r.ok) {
          throw new Error(`GetAccountBillHistory HTTP ${r.status}`);
        }
        return (await r.json()) as {
          Success?: boolean;
          Payload?: Array<{ Id?: string; AmountDue?: number }>;
        };
      });
      const rows = history.Payload || [];
      // Prefer the most recent bill that already has an amount (skip current open bill if present).
      // History is newest-first; index 0 is often the open "amount due" row, index 1 the last closed bill.
      billId =
        rows.find((row, i) => i > 0 && row.Id)?.Id ||
        rows.find((row) => row.Id)?.Id ||
        null;
      if (!billId) {
        throw new Error("No bill Id found in GetAccountBillHistory payload");
      }
    }

    const path = `/account-billing/account-summary/DisplayBillPdf/${billId}`;
    const pdf = await page.evaluate(async (p: string) => {
      const r = await fetch(p, { credentials: "include" });
      const ab = await r.arrayBuffer();
      const u8 = new Uint8Array(ab);
      const head = new TextDecoder().decode(u8.slice(0, 5));
      let b64 = "";
      if (head === "%PDF-" || (r.headers.get("content-type") || "").includes("pdf")) {
        let s = "";
        for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
        b64 = btoa(s);
      }
      return {
        status: r.status,
        ct: r.headers.get("content-type") || "",
        len: ab.byteLength,
        head,
        b64,
      };
    }, path);

    if (!pdf.b64) {
      throw new Error(
        `DisplayBillPdf failed status=${pdf.status} ct=${pdf.ct} len=${pdf.len} head=${JSON.stringify(pdf.head)}`,
      );
    }

    fs.writeFileSync(outPdf, Buffer.from(pdf.b64, "base64"));
    process.stdout.write(
      JSON.stringify({ billId, pdfPath: outPdf, bytes: pdf.len }) + "\n",
    );
  } finally {
    await browser.close();
  }
}

main().catch((err: unknown) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
