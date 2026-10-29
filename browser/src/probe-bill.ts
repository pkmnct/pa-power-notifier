
import * as fs from "node:fs";
import puppeteer from "puppeteer";

async function main() {
  const cookies = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const outFile = process.argv[3];
  const billId = process.argv[4]; // paid bill from history
  const browser = await puppeteer.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  await page.setUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36");
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  });
  for (const c of cookies) {
    try {
      await page.setCookie({ name: c.name, value: c.value, domain: c.domain, path: c.path || "/" });
    } catch {}
  }

  const captured: any[] = [];
  page.on("response", async (resp) => {
    const u = resp.url();
    if (/\.(js|css|png|svg|woff|jpg)/i.test(u)) return;
    if (/bill|Bill|pdf|PDF|kubra|Kubra|statement|invoice|supplier/i.test(u)) {
      try {
        const ct = resp.headers()["content-type"] || "";
        const body = await resp.text();
        if (body && !body.includes("_Incapsula_Resource")) {
          captured.push({ url: u, status: resp.status(), ct, body: body.slice(0, 200000) });
        }
      } catch {}
    }
  });

  await page.goto("https://www.duquesnelight.com/account-billing/account-summary", { waitUntil: "networkidle2", timeout: 120000 });
  await new Promise(r => setTimeout(r, 3000));

  // Inspect ViewBill definition
  const viewBillSrc = await page.evaluate(() => {
    // @ts-ignore
    const fn = (window as any).ViewBill;
    return fn ? Function.prototype.toString.call(fn) : "NO ViewBill on window";
  });

  // Also search page scripts for ViewBill=
  const scriptHits = await page.evaluate(() => {
    const out: string[] = [];
    for (const s of Array.from(document.scripts)) {
      const t = s.src || s.textContent || "";
      if (/ViewBill/i.test(t)) out.push((s.src || "inline").slice(0, 200));
    }
    return out;
  });

  // Try calling ViewBill with bill id
  const callResult = await page.evaluate(async (id: string) => {
    // @ts-ignore
    const fn = (window as any).ViewBill;
    if (!fn) return { ok: false, reason: "no fn" };
    try {
      const ret = fn(id);
      return { ok: true, ret: String(ret), type: typeof ret };
    } catch (e: any) {
      return { ok: false, reason: e.message };
    }
  }, billId);
  await new Promise(r => setTimeout(r, 8000));

  // Try common bill endpoints
  const probes = await page.evaluate(async (id: string) => {
    const urls = [
      `/account-billing/account-summary/ViewBill?id=${id}`,
      `/account-billing/account-summary/ViewBill/${id}`,
      `/account-billing/account-summary/GetBill?id=${id}`,
      `/account-billing/account-summary/GetBillPdf?id=${id}`,
      `/my-account/view-bill?id=${id}`,
      `/account-billing/view-bill?billId=${id}`,
      `/account-billing/account-summary/account-statement`,
    ];
    const out: Record<string, string> = {};
    for (const u of urls) {
      try {
        const r = await fetch(u, { credentials: "include", headers: { "X-Requested-With": "XMLHttpRequest" } });
        const t = await r.text();
        out[u] = `${r.status} ct=${r.headers.get("content-type")} len=${t.length} :: ${t.slice(0, 500)}`;
      } catch (e: any) {
        out[u] = "ERR " + e.message;
      }
    }
    return out;
  }, billId);

  // Fetch paymentBillHistory bundle and extract ViewBill
  const bundle = await page.evaluate(async () => {
    const r = await fetch("/bundles/paymentBillHistory?v=20231005", { credentials: "include" });
    return await r.text();
  });
  const m = bundle.match(/ViewBill\s*=\s*function\s*\([^)]*\)\s*\{[\s\S]{0,1200}\}/);
  const m2 = bundle.match(/function\s+ViewBill\s*\([^)]*\)\s*\{[\s\S]{0,1200}\}/);
  const accountSummaryBundle = await page.evaluate(async () => {
    const r = await fetch("/bundles/accountSummary?v=20231005", { credentials: "include" });
    return await r.text();
  });
  const m3 = accountSummaryBundle.match(/ViewBill[\s\S]{0,800}/);

  fs.writeFileSync(outFile, JSON.stringify({
    viewBillSrc, scriptHits, callResult, probes,
    viewBillFromBundle: m?.[0] || m2?.[0] || null,
    accountSummaryHit: m3?.[0] || null,
    captured: captured.map(c => ({ url: c.url, status: c.status, ct: c.ct, body: c.body.slice(0, 5000) })),
  }, null, 2));
  console.log("viewBillSrc", viewBillSrc.slice(0, 500));
  console.log("scriptHits", scriptHits);
  console.log("callResult", callResult);
  console.log("bundle fn", (m?.[0] || m2?.[0] || "").slice(0, 800));
  console.log("accountSummaryHit", (m3?.[0] || "").slice(0, 500));
  for (const [k,v] of Object.entries(probes)) console.log(k, String(v).slice(0, 200).replace(/\n/g, " "));
  console.log("captured", captured.length);
  for (const c of captured) console.log(c.status, c.ct, c.url.slice(0, 140));
  await browser.close();
}
main().catch(e => { console.error(e); process.exit(1); });
