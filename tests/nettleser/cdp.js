// Liten driver for Edge (eller Chrome) uten vindu via Chrome DevTools Protocol. Node 22+ har WebSocket innebygd.
// Brukes av tests/test_deltakersok_nettleser.py til å prøve JavaScript-en i deltakersøket i en ekte nettleser.
// Nettleseren finnes via miljøvariabelen IPR_NETTLESER_STI (settes av testen). Ingen avhengigheter.
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const NETTLESER = process.env.IPR_NETTLESER_STI;
const vent = (ms) => new Promise((r) => setTimeout(r, ms));

async function start(port) {
  if (!NETTLESER) throw new Error("IPR_NETTLESER_STI er ikke satt");
  const profil = fs.mkdtempSync(path.join(os.tmpdir(), "ipr-cdp-"));
  const edge = spawn(NETTLESER, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--hide-scrollbars", "--disable-extensions", `--remote-debugging-port=${port}`, `--user-data-dir=${profil}`, "about:blank"],
    { stdio: "ignore" });
  let avsluttet = false;
  edge.once("exit", () => { avsluttet = true; });
  const rydd = async () => {
    for (let i = 0; i < 20 && !avsluttet; i++) await vent(250);
    for (let i = 0; i < 10; i++) {
      try { fs.rmSync(profil, { recursive: true, force: true }); return; } catch (e) { await vent(300); }
    }
  };
  let mal = null;
  for (let i = 0; i < 80 && !mal; i++) {
    await vent(250);
    try {
      const liste = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      mal = liste.find((t) => t.type === "page");
    } catch (e) { /* ikke klar ennå */ }
  }
  if (!mal) { edge.kill(); await rydd(); throw new Error("Nettleseren startet ikke"); }
  const ws = new WebSocket(mal.webSocketDebuggerUrl);
  await new Promise((ok, feil) => { ws.onopen = ok; ws.onerror = feil; });
  let nr = 0;
  const venter = new Map(), lyttere = [];
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && venter.has(d.id)) {
      const { ok, feil } = venter.get(d.id);
      venter.delete(d.id);
      d.error ? feil(new Error(JSON.stringify(d.error))) : ok(d.result);
    } else if (d.method) {
      lyttere.forEach((l) => l(d));
    }
  };
  const send = (method, params = {}) => new Promise((ok, feil) => {
    const id = ++nr;
    venter.set(id, { ok, feil });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const hendelse = (navn, ms = 15000) => new Promise((ok, feil) => {
    const t = setTimeout(() => feil(new Error("tidsavbrudd: " + navn)), ms);
    const l = (d) => { if (d.method === navn) { clearTimeout(t); lyttere.splice(lyttere.indexOf(l), 1); ok(d.params); } };
    lyttere.push(l);
  });
  await send("Page.enable");
  await send("Runtime.enable");
  // konsoll: feil fra selve siden. utvidelser: feil fra nettleserens egne tillegg (egen, isolert sone - ikke vår kode).
  const konsoll = [], utvidelser = [], soner = new Map();
  lyttere.push((d) => {
    if (d.method === "Runtime.executionContextCreated") {
      const k = d.params.context;
      soner.set(k.id, { type: (k.auxData && k.auxData.type) || "?", opphav: k.origin || k.name || "" });
    }
    if (d.method === "Runtime.exceptionThrown") {
      const e = d.params.exceptionDetails;
      const kilde = e.url || (e.stackTrace && e.stackTrace.callFrames.length && e.stackTrace.callFrames[0].url) || "(ingen adresse)";
      const sone = soner.get(e.executionContextId) || { type: "?", opphav: "" };
      const tekst = "UNNTAK: " + JSON.stringify(e.exception && e.exception.description || e.text) + " – kilde: " + kilde;
      if (sone.type === "isolated" && sone.opphav.startsWith("chrome-extension://")) utvidelser.push(tekst);
      else konsoll.push(tekst);
    }
    if (d.method === "Runtime.consoleAPICalled" && d.params.type === "error") {
      const sone = soner.get(d.params.executionContextId) || { type: "?", opphav: "" };
      const tekst = "console.error: " + d.params.args.map((a) => a.value || a.description).join(" ");
      if (sone.type === "isolated" && sone.opphav.startsWith("chrome-extension://")) utvidelser.push(tekst);
      else konsoll.push(tekst);
    }
  });
  lyttere.push((d) => { if (d.method === "Page.javascriptDialogOpening") send("Page.handleJavaScriptDialog", { accept: true }); });

  return {
    konsoll,
    utvidelser,
    async storrelse(bredde, hoyde, mobil = false) {
      await send("Emulation.setDeviceMetricsOverride", { width: bredde, height: hoyde, deviceScaleFactor: 1, mobile: mobil });
    },
    async gaa(url) {
      const lastet = hendelse("Page.loadEventFired");
      await send("Page.navigate", { url });
      await lastet;
      await vent(300);
    },
    async js(uttrykk) {
      const r = await send("Runtime.evaluate", { expression: uttrykk, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error("JS-feil: " + JSON.stringify(r.exceptionDetails));
      return r.result.value;
    },
    async klikk(velger) {
      const ok = await this.js(`(() => { const el = document.querySelector(${JSON.stringify(velger)}); if (!el) return false; el.scrollIntoView({block: "center"}); el.click(); return true; })()`);
      if (!ok) throw new Error("fant ikke " + velger);
      await vent(150);
    },
    async skriv(velger, tekst) {
      await this.js(`document.querySelector(${JSON.stringify(velger)}).focus()`);
      await send("Input.insertText", { text: tekst });
      await vent(100);
    },
    // Ekte tastetrykk. `tekst` = tegnet som skrives (gir keyDown med tekst); `modifikatorer`: 1 = Alt, 2 = Ctrl, 4 = Meta, 8 = Skift.
    async tast(key, code, kode, tekst, modifikatorer = 0) {
      const ned = { type: tekst && !modifikatorer ? "keyDown" : "rawKeyDown", key, code, windowsVirtualKeyCode: kode, modifiers: modifikatorer };
      if (tekst && !modifikatorer) ned.text = tekst;
      await send("Input.dispatchKeyEvent", ned);
      await send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: kode, modifiers: modifikatorer });
      await vent(120);
    },
    // Et vilkårlig DevTools-kall (f.eks. Emulation.setScriptExecutionDisabled for å prøve siden uten JavaScript).
    async cdp(metode, params = {}) { return send(metode, params); },
    // Skjermbilde til fil (helside = hele siden, ellers bare det som vises).
    async bilde(fil, helside = true) {
      const hoyde = helside ? await this.js("Math.ceil(document.documentElement.scrollHeight)") : null;
      const bredde = await this.js("window.innerWidth");
      const r = await send("Page.captureScreenshot", helside
        ? { format: "png", captureBeyondViewport: true, clip: { x: 0, y: 0, width: bredde, height: hoyde, scale: 1 } }
        : { format: "png" });
      fs.writeFileSync(fil, Buffer.from(r.data, "base64"));
    },
    async lukk() {
      try { await send("Browser.close"); } catch (e) { /* allerede lukket */ }
      try { ws.close(); } catch (e) { /* ok */ }
      edge.kill();
      await rydd();
    },
  };
}

module.exports = { start, vent };
