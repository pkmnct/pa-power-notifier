
import * as fs from "node:fs";
import puppeteer from "puppeteer";

async function main() {
  const url = process.argv[2];
  const cookiesFile = process.argv[3];
  const outFile = process.argv[4];
  const cookies = JSON.parse(fs.readFileSync(cookiesFile, "utf8"));
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
      await page.setCookie({
        name: c.name,
        value: c.value,
        domain: c.domain?.startsWith(".") ? c.domain : c.domain,
        path: c.path || "/",
      });
    } catch {}
  }
  const captured: {url:string, status:number, body:string}[] = [];
  page.on("response", async (resp) => {
    const u = resp.url();
    if (/GetAccount|GetData|GetPayment|BillHistory|current-account|accounts|Bill|Statement|Program|supplier|Supplier|Rate/i.test(u) && !/\.(js|css|png|svg|woff)/i.test(u)) {
      try {
        const body = await resp.text();
        if (body && !body.includes("_Incapsula_Resource")) {
          captured.push({ url: u, status: resp.status(), body: body.slice(0, 100000) });
        }
      } catch {}
    }
  });
  await page.goto(url, { waitUntil: "networkidle2", timeout: 120000 });
  await new Promise(r => setTimeout(r, 5000));
  const apiResults = await page.evaluate(async () => {
    const urls = [
      "/account-billing/account-summary/GetAccountData/",
      "/account-billing/account-summary/GetData/",
      "/account-billing/account-summary/GetPaymentData/",
      "/account-billing/account-summary/GetAccountProgramData/",
      "/account-billing/account-summary/GetAccountBillHistory/",
      "/dlc/accountSelection/current-account",
      "/dlc/accountSelection/accounts",
    ];
    const out: Record<string, string> = {};
    for (const u of urls) {
      try {
        const r = await fetch(u, { credentials: "include", headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" } });
        out[u] = r.status + "::" + (await r.text()).slice(0, 20000);
      } catch (e: any) {
        out[u] = "ERR::" + e.message;
      }
    }
    try {
      const r = await fetch("/account-billing/account-summary/GetAccountBillHistory/", {
        method: "POST", credentials: "include",
        headers: { "X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json", Accept: "application/json" },
        body: "{}"
      });
      out["POST GetAccountBillHistory"] = r.status + "::" + (await r.text()).slice(0, 20000);
    } catch (e: any) { out["POST GetAccountBillHistory"] = "ERR::" + e.message; }
    return out;
  });
  const text = await page.evaluate(() => document.body.innerText);
  fs.writeFileSync(outFile, JSON.stringify({ apiResults, captured, text }, null, 2));
  console.log("wrote", outFile, "apis", Object.keys(apiResults).length, "xhr", captured.length);
  for (const [k,v] of Object.entries(apiResults)) console.log(k, String(v).slice(0,220).replace(/\n/g," "));
  console.log("AMERICAN in text?", /american/i.test(text));
  console.log(text.slice(0,2500));
  await browser.close();
}
main().catch(e => { console.error(e); process.exit(1); });
