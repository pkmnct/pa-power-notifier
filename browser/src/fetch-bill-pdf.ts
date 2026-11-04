/**
 * Fetch Duquesne Light bill PDFs using an authenticated cookie jar.
 *
 * Usage:
 *   npx tsx src/fetch-bill-pdf.ts --cookies-file <cookies.json> --out-pdf <path.pdf> [--bill-id <id>]
 *   npx tsx src/fetch-bill-pdf.ts --cookies-file <cookies.json> --all --out-dir <dir> [--skip-ids <id,id>]
 *
 * cookies-file: JSON array of { name, value, domain, path? } (Puppeteer cookie shape).
 * Single mode prints one JSON line: { billId, pdfPath, bytes }.
 * --all prints one JSON line per bill in the account's bill history:
 *   { billId, date, amountDue, pdfPath, bytes } or { billId, date, amountDue, skipped: true }
 *   for --skip-ids.
 */
import * as fs from "node:fs";
import puppeteer from "puppeteer";

type CookieRow = {
  name: string;
  value: string;
  domain?: string;
  path?: string;
};

type HistoryRow = { Id?: string; Date?: string; AmountDue?: number };

function parseArgs(): {
  cookiesFile: string;
  outPdf: string;
  billId: string | null;
  all: boolean;
  outDir: string;
  skipIds: Set<string>;
} {
  const argv = process.argv.slice(2);
  let cookiesFile = "";
  let outPdf = "";
  let billId: string | null = null;
  let all = false;
  let outDir = "";
  let skipIds = new Set<string>();
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--cookies-file") cookiesFile = argv[++i] ?? "";
    else if (argv[i] === "--out-pdf") outPdf = argv[++i] ?? "";
    else if (argv[i] === "--bill-id") billId = argv[++i] ?? null;
    else if (argv[i] === "--all") all = true;
    else if (argv[i] === "--out-dir") outDir = argv[++i] ?? "";
    else if (argv[i] === "--skip-ids") {
      skipIds = new Set((argv[++i] ?? "").split(",").filter(Boolean));
    }
  }
  return { cookiesFile, outPdf, billId, all, outDir, skipIds };
}

// "/Date(1790553600000)/" → "2026-09-28"
function aspNetDate(v?: string): string | null {
  const m = /\/Date\((\d+)/.exec(v ?? "");
  return m ? new Date(Number(m[1])).toISOString().slice(0, 10) : null;
}

function parseHeadless(): boolean | "shell" {
  const v = process.env.DUQUESNE_PUPPETEER_HEADLESS?.trim().toLowerCase();
  if (v === "0" || v === "false" || v === "no") return false;
  if (v === "shell") return "shell";
  return true;
}

async function main(): Promise<void> {
  const { cookiesFile, outPdf, billId: billIdArg, all, outDir, skipIds } = parseArgs();
  if (!cookiesFile || (all ? !outDir : !outPdf)) {
    console.error(
      "Usage: fetch-bill-pdf.ts --cookies-file <cookies.json> --out-pdf <path.pdf> [--bill-id <id>]\n" +
        "       fetch-bill-pdf.ts --cookies-file <cookies.json> --all --out-dir <dir> [--skip-ids <id,id>]",
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

    const getHistory = async (): Promise<HistoryRow[]> => {
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
        return (await r.json()) as { Success?: boolean; Payload?: HistoryRow[] };
      });
      return history.Payload || [];
    };

    const downloadPdf = async (billId: string, dest: string): Promise<number> => {
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
          `DisplayBillPdf ${billId} failed status=${pdf.status} ct=${pdf.ct} len=${pdf.len} head=${JSON.stringify(pdf.head)}`,
        );
      }
      fs.writeFileSync(dest, Buffer.from(pdf.b64, "base64"));
      return pdf.len;
    };

    if (all) {
      fs.mkdirSync(outDir, { recursive: true });
      for (const row of await getHistory()) {
        if (!row.Id) continue;
        const date = aspNetDate(row.Date);
        if (skipIds.has(row.Id)) {
          process.stdout.write(
            JSON.stringify({ billId: row.Id, date, amountDue: row.AmountDue, skipped: true }) + "\n",
          );
          continue;
        }
        const dest = `${outDir}/${row.Id}.pdf`;
        const bytes = await downloadPdf(row.Id, dest);
        process.stdout.write(
          JSON.stringify({ billId: row.Id, date, amountDue: row.AmountDue, pdfPath: dest, bytes }) + "\n",
        );
      }
      return;
    }

    // History is newest-first. The newest row is the latest issued bill (its amount is
    // still due, but its PDF exists); fall back to the next one if its PDF won't load.
    const candidates = billIdArg
      ? [billIdArg]
      : (await getHistory())
          .map((row) => row.Id)
          .filter((id): id is string => Boolean(id))
          .slice(0, 2);
    if (candidates.length === 0) {
      throw new Error("No bill Id found in GetAccountBillHistory payload");
    }

    let lastError: unknown;
    for (const billId of candidates) {
      try {
        const bytes = await downloadPdf(billId, outPdf);
        process.stdout.write(
          JSON.stringify({ billId, pdfPath: outPdf, bytes }) + "\n",
        );
        return;
      } catch (err) {
        lastError = err;
        console.error(err instanceof Error ? err.message : err);
      }
    }
    throw lastError;
  } finally {
    await browser.close();
  }
}

main().catch((err: unknown) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
