/* Tester for redigeringsvisningens JavaScript (autolagring, versjonsnummer, konflikt, opplastingsrekkefølge, filtittel) og plakatens omlasting.
   Kjøres av tests/test_kursside_js.py (hoppes over uten Node). Ingen nettleser: kursside-admin*.js lastes i en `vm` med et lite falskt DOM
   (elementer som bare husker barn og attributter), falske tidtakere (ingenting kjører før testen sier det) og en falsk `fetch` som svarer med det
   testen har lagt i køen. Bare oppdiktede data. Skriver «OK» og avslutter med 0, ellers en melding og avslutter med 1. */
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = process.env.KS_STATIC || path.join(__dirname, "..", "..", "kurs", "web", "static");     // KS_STATIC: en endret kopi (mutasjonsprøve)

function lagElement(tag) {
  return {
    tagName: String(tag || "div").toUpperCase(), children: [], attrs: {}, style: { setProperty() {}, removeProperty() {} }, hidden: false, className: "",
    textContent: "", value: "", disabled: false, parentNode: null, lyttere: {}, classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) { this.children.splice(i, 1); } c.parentNode = null; return c; },
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    hasAttribute(k) { return k in this.attrs; }, removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(t, f) { (this.lyttere[t] = this.lyttere[t] || []).push(f); }, removeEventListener() {},
    querySelector(sel) { const m = /^\[data-banner="(.+)"\]$/.exec(sel); return m ? (this.children.find((c) => c.attrs["data-banner"] === m[1]) || null) : null; },
    querySelectorAll() { return []; }, contains() { return false; }, focus() {}, click() {}, replaceChildren() { this.children = []; },
    get firstChild() { return this.children[0] || null; },
  };
}

/* Et element som gir SAMME barn hver gang det spørres med samme velger ([data-felt="x"], [data-ks="x"]), så en dialog kan fylles ut og knappene
   trykkes uten et ekte DOM. Brukes for rot og dialoger når lagVindu får { dom: true }. */
function lagAutoElement(tag) {
  const e = lagElement(tag), barn = {};
  e.querySelector = (sel) => barn[sel] || (barn[sel] = lagAutoElement("div"));
  e.open = false; e.checked = false;
  e.showModal = () => { e.open = true; };
  e.show = () => { e.open = true; };
  e.close = () => { e.open = false; (e.lyttere.close || []).forEach((f) => f()); };
  return e;
}

/* Et falskt nettleservindu med kjernen (kursside-admin.js) og ev. flere av filene lastet.
   valg.dom: rot og dialogene (#ks-dialog-<navn>) er lagAutoElement, og init-funksjonene til filene som er lastet kjøres (som ved DOMContentLoaded). */
function lagVindu(filer, data, valg) {
  valg = valg || {};
  const bannere = lagElement("div"), rot = valg.dom ? lagAutoElement("div") : lagElement("div");
  rot.attrs["data-skrivebeskyttet"] = "0";
  const dok = data.dokument || { v: 1, tittel: "", ingress: "", rom: "", melding: { tekst: "", niva: "info" }, blokker: [] };
  const D = Object.assign({ kurs: { id: 1 }, dokument: dok, versjon: 7, publisert: null, publisert_versjon: null, aktiv: true, innstillinger: {}, filer: [],
    kvote: { antall: 0, bytes: 0, maks_bytes: 1e8, maks_antall: 200 }, versjoner: [], kursdager: [], grenser: { tema: 200, punkter: 40, filer_per_blokk: 100 },
    urls: { lagre: "/lagre", logg_inn: "/logg-inn", token: "/token", redigerer: "/red" } }, data);
  const elementer = { "ks-rot": rot, "ks-data": { textContent: JSON.stringify(D) }, "ks-bannere": bannere };
  const timere = [];
  let tid = 0;
  const kall = [], svar = [];
  const sandbox = {
    console, JSON, Promise, Date, Math, Set, Map, Object, Array, Number, String, RegExp, Uint8Array, Intl, parseInt, parseFloat, isNaN, encodeURIComponent, Error,
    setTimeout(fn, ms) { const id = ++tid; timere.push({ id, fn, ms }); return id; },
    clearTimeout(id) { const i = timere.findIndex((t) => t.id === id); if (i >= 0) { timere.splice(i, 1); } },
    setInterval() { return 0; }, clearInterval() {},
    fetch(url, opp) {
      kall.push({ url, opp });
      const s = svar.shift() || { status: 200, json: { status: "ok", versjon: 8, endret: "12:00", merknader: [] } };
      return Promise.resolve({ status: s.status, redirected: !!s.omdirigert, headers: { get: () => (s.utenJson ? "text/html" : "application/json") }, json: () => Promise.resolve(s.json) });
    },
    localStorage: { setItem() {}, getItem() { return null; }, removeItem() {} },
    navigator: {},
  };
  sandbox.document = {
    getElementById: (id) => elementer[id] || (valg.dom && id.startsWith("ks-dialog-") ? (elementer[id] = lagAutoElement("dialog")) : null), querySelector: () => null, querySelectorAll: () => [], createElement: lagElement,
    createElementNS: (ns, t) => lagElement(t), createTextNode: (t) => ({ nodeType: 3, nodeValue: t }), addEventListener() {}, body: lagElement("body"),
    visibilityState: "visible",
  };
  sandbox.window = sandbox;
  sandbox.addEventListener = () => {};
  const ctx = vm.createContext(sandbox);
  filer.forEach((f) => vm.runInContext(fs.readFileSync(path.join(STATIC, f), "utf8"), ctx, { filename: f }));
  if (valg.dom) { sandbox.KS.init.forEach((f) => f()); }
  return {
    dialog: (navn) => sandbox.document.getElementById("ks-dialog-" + navn), rot,
    KS: sandbox.KS, kall, svar, bannere, timere,
    kjorTimere() { const alle = timere.splice(0).sort((a, b) => a.ms - b.ms); alle.forEach((t) => t.fn()); },
    venteLitt: () => new Promise((r) => setImmediate(r)),
  };
}
const tomt = () => new Promise((r) => setImmediate(r));
async function ferdig(p) { const r = await p; await tomt(); return r; }
const bannerIder = (v) => v.bannere.children.map((c) => c.attrs["data-banner"]);

const tester = [];
const test = (navn, fn) => tester.push([navn, fn]);

/* ---------------- autolagring (S01): en endring gir en planlagt lagring som faktisk sender siden ---------------- */
test("autolagring: en endring planlegger en lagring som sender utkastet til serveren", async () => {
  const v = lagVindu(["kursside-admin.js"], {});
  v.KS.S.dok.tittel = "Ny tittel";
  v.KS.endret({});
  assert.strictEqual(v.KS.S.ulagret, true, "endringen skal merkes som ulagret");
  assert.ok(v.timere.some((t) => t.ms === 4000), "lagringen skal være planlagt (4 sekunder)");
  v.kjorTimere();
  await tomt();
  const lagringer = v.kall.filter((k) => k.url === "/lagre");
  assert.strictEqual(lagringer.length, 1, "utkastet skal sendes til lagre-adressen");
  assert.strictEqual(lagringer[0].opp.method, "POST");
  assert.strictEqual(JSON.parse(lagringer[0].opp.body).dokument.tittel, "Ny tittel");
});

test("lagring uten endringer sender ingenting", async () => {
  const v = lagVindu(["kursside-admin.js"], {});
  assert.strictEqual(await ferdig(v.KS.lagre()), true);
  assert.strictEqual(v.kall.length, 0);
});

/* ---------------- versjonsnummer (S02): serverens versjon sendes med, og nytt nummer tas imot ---------------- */
test("lagring sender gjeldende versjon (ikke 0) og husker den nye versjonen fra svaret", async () => {
  const v = lagVindu(["kursside-admin.js"], { versjon: 7 });
  v.KS.S.ulagret = true;
  v.svar.push({ status: 200, json: { status: "ok", versjon: 8, endret: "12:01", merknader: [] } });
  assert.strictEqual(await ferdig(v.KS.lagre()), true);
  assert.strictEqual(JSON.parse(v.kall[0].opp.body).versjon, 7, "første lagring skal sende versjonen siden ble åpnet med");
  assert.strictEqual(v.KS.S.versjon, 8);
  assert.strictEqual(v.KS.S.ulagret, false);
  v.KS.S.ulagret = true;
  v.svar.push({ status: 200, json: { status: "ok", versjon: 9, endret: "12:02", merknader: [] } });
  await ferdig(v.KS.lagre());
  assert.strictEqual(JSON.parse(v.kall[1].opp.body).versjon, 8, "neste lagring skal sende den nye versjonen");
  assert.strictEqual(v.KS.S.versjon, 9);
});

test("CSRF-token og innloggingsinfo følger med lagringen", async () => {
  const v = lagVindu(["kursside-admin.js"], {});
  v.KS.S.ulagret = true;
  await ferdig(v.KS.lagre());
  assert.strictEqual(v.kall[0].opp.headers["Content-Type"], "application/json");
  assert.ok("X-CSRF-Token" in v.kall[0].opp.headers);
  assert.strictEqual(v.kall[0].opp.credentials, "same-origin");
});

/* ---------------- konflikt (S03): 409 vises som konflikt, aldri som en vanlig lagrefeil ---------------- */
test("409 med status «konflikt» åpner konfliktvisningen og gir ikke en vanlig lagrefeil", async () => {
  const v = lagVindu(["kursside-admin.js"], {});
  let visteKonflikt = null;
  v.KS.visKonflikt = (j, mitt) => { visteKonflikt = { j, mitt }; };
  v.KS.S.dok.tittel = "Min tittel";
  v.KS.S.ulagret = true;
  v.svar.push({ status: 409, json: { status: "konflikt", versjon: 9, diff: [], dokument: {} } });
  assert.strictEqual(await ferdig(v.KS.lagre()), false);
  assert.ok(visteKonflikt, "konfliktvisningen skal åpnes");
  assert.strictEqual(visteKonflikt.mitt.tittel, "Min tittel", "det som skulle lagres skal være med, så ingenting går tapt");
  assert.ok(!bannerIder(v).includes("lagrefeil"), "en konflikt er ikke en vanlig lagrefeil");
  assert.strictEqual(v.KS.S.versjon, 7, "versjonen skal ikke bli byttet ut før administratoren har valgt");
});

test("lagringen står på pause så lenge en konflikt ikke er løst", async () => {
  const v = lagVindu(["kursside-admin.js"], {});
  v.KS.S.ulagret = true;
  v.KS.S.konflikt = { j: {}, mitt: {} };
  assert.strictEqual(await ferdig(v.KS.lagre()), false);
  assert.strictEqual(v.kall.length, 0);
});

test("en annen feil fra serveren gir en synlig lagrefeil (ikke stille tap)", async () => {
  const v = lagVindu(["kursside-admin.js"], {});
  v.KS.S.ulagret = true;
  v.svar.push({ status: 422, json: { status: "feil", melding: "Teksten er for lang" } });
  assert.strictEqual(await ferdig(v.KS.lagre()), false);
  assert.ok(bannerIder(v).includes("lagrefeil"));
  assert.strictEqual(v.KS.S.ulagret, true, "siden skal fortsatt regnes som ulagret");
});

test("utlogget (svar uten JSON) gir advarsel og lagringen forsøkes ikke i en løkke", async () => {
  const v = lagVindu(["kursside-admin.js"], {});
  v.KS.S.ulagret = true;
  v.svar.push({ status: 200, utenJson: true, omdirigert: true });
  assert.strictEqual(await ferdig(v.KS.lagre()), false);
  assert.ok(bannerIder(v).includes("utlogget"));
  assert.strictEqual(v.KS.S.utlogget, true);
});

/* ---------------- opplastede filer i valgt rekkefølge, og filtittel ---------------- */
test("opplastede filer settes inn i den rekkefølgen de ble valgt, uansett når de blir ferdige", async () => {
  const v = lagVindu(["kursside-admin.js", "kursside-admin-filer.js"], {});
  const liste = [{ fil_id: 99, tittel: "Fra før" }];
  const parti = { rekke: {} };
  const opp = (rekke, id) => ({ parti, rekke, id });
  // valgt rekkefølge: 0 (stor), 1, 2, 3. De ble ferdige i rekkefølgen 1, 3, 0, 2.
  [[1, 11], [3, 13], [0, 10], [2, 12]].forEach(([r, id]) => v.KS.settInnIRekkefolge(liste, { fil_id: id }, opp(r, id)));
  assert.deepStrictEqual(liste.map((e) => e.fil_id), [99, 10, 11, 12, 13]);
});

test("filtittel av filnavn: bindestrek med mellomrom rundt blir tankestrek, understrek og bindestrek i ord blir mellomrom", () => {
  const v = lagVindu(["kursside-admin.js", "kursside-admin-liste.js", "kursside-admin-blokker.js"], {});
  const t = v.KS.tittelFraFilnavn;
  assert.strictEqual(t("Presentasjon dag 1 - Emosjonelt fokusert terapi.pdf"), "Presentasjon dag 1 – Emosjonelt fokusert terapi");
  assert.strictEqual(t("dag_2_-_Prosess_og_intervensjon.pptx"), "Dag 2 – Prosess og intervensjon");
  assert.strictEqual(t("litteratur-liste_2027.pdf"), "Litteratur liste 2027");
  assert.strictEqual(t("Kompendium.pdf"), "Kompendium");
});

/* ---------------- plakaten: laster ikke siden på nytt mens den skrives ut (S05) ---------------- */
test("plakaten laster siden på nytt hvert N. sekund, men ikke mens den skrives ut", () => {
  const lyttere = {};
  let intervall = null, omlastinger = 0;
  const sandbox = {
    parseInt, window: {
      addEventListener(t, f) { lyttere[t] = f; },
      setInterval(fn, ms) { intervall = { fn, ms }; return 1; },
      location: { reload() { omlastinger++; } },
    },
    document: { body: { getAttribute: (n) => (n === "data-oppdater" ? "20" : null) } },
  };
  vm.runInContext(fs.readFileSync(path.join(STATIC, "innsjekk-plakat.js"), "utf8"), vm.createContext(sandbox), { filename: "innsjekk-plakat.js" });
  assert.ok(intervall, "plakaten skal ha et intervall");
  assert.strictEqual(intervall.ms, 20000);
  intervall.fn();
  assert.strictEqual(omlastinger, 1, "vanlig visning skal laste på nytt");
  lyttere.beforeprint();
  intervall.fn();
  assert.strictEqual(omlastinger, 1, "under utskrift skal siden IKKE lastes på nytt");
  lyttere.afterprint();
  intervall.fn();
  assert.strictEqual(omlastinger, 2, "etter utskrift skal den laste på nytt igjen");
});

test("plakaten uten data-oppdater laster aldri på nytt", () => {
  let intervall = null;
  const sandbox = { parseInt, window: { addEventListener() {}, setInterval(fn, ms) { intervall = { fn, ms }; }, location: { reload() {} } },
    document: { body: { getAttribute: () => null } } };
  vm.runInContext(fs.readFileSync(path.join(STATIC, "innsjekk-plakat.js"), "utf8"), vm.createContext(sandbox), { filename: "innsjekk-plakat.js" });
  assert.strictEqual(intervall, null);
});

/* ---------------- åpningstid etter kurset: modus og klartekst i lenkeboksen ---------------- */
test("åpningstid: modus følger dato, dager og ingen tidsbegrensning (dato går foran)", () => {
  const { KS } = lagVindu(["kursside-admin.js"], {});
  assert.strictEqual(KS.apningModus({}), "standard");
  assert.strictEqual(KS.apningModus({ stenges: null, apen_dager: null }), "standard");
  assert.strictEqual(KS.apningModus({ apen_dager: 365 }), "dager");
  assert.strictEqual(KS.apningModus({ apen_dager: 0 }), "ingen", "0 dager betyr ingen tidsbegrensning");
  assert.strictEqual(KS.apningModus({ stenges: "2027-12-24", apen_dager: 30 }), "dato");
  assert.strictEqual(KS.apningModus({ stenges: "2027-12-24", apen_dager: 0 }), "dato", "en bestemt dato går også foran «ingen tidsbegrensning»");
});

test("åpningstid: lenkeboksen sier hva innstillingen betyr i klartekst", () => {
  const { KS } = lagVindu(["kursside-admin.js"], {});
  assert.strictEqual(KS.apningTekst({ stenges_effektiv: "2027-03-12" }), "Åpen til 12.03.2027");
  assert.strictEqual(KS.apningTekst({ apen_dager: 365, stenges_effektiv: "2028-03-11" }), "Åpen til 11.03.2028 (365 dager etter siste kursdag)");
  assert.strictEqual(KS.apningTekst({ stenges: "2027-12-24", stenges_effektiv: "2027-12-24" }), "Åpen til 24.12.2027");
  assert.strictEqual(KS.apningTekst({ apen_dager: 0, stenges_effektiv: null }), "Åpen uten tidsbegrensning");
  assert.strictEqual(KS.apningTekst({ stenges_effektiv: null }), "Åpen til: satt når siste kursdag er kjent");
});

/* ---------------- «Innstillinger for siden»: dialogen (kursside-admin-dialoger.js) fylles ut og OK trykkes i et falskt DOM ---------------- */
const APNING = ["standard", "dager", "ingen", "dato"];
const SVAR_INNST = (o) => ({ status: 200, json: Object.assign({ status: "ok", innsjekk_krever_kode: true, stenges: null, apen_dager: null, standard_dager: 180,
  siste_kursdag: "2027-03-01", stenges_effektiv: "2027-08-28" }, o || {}) });

/* Åpner dialogen for et kurs med de lagrede innstillingene `innst` (siste kursdag 1. mars 2027, standard 180 dager). Radioknappene oppfører seg som
   i nettleseren: bare én er valgt. */
function innstDialog(innst, kurs) {
  const v = lagVindu(["kursside-admin.js", "kursside-admin-dialoger.js"], {
    innstillinger: Object.assign({ innsjekk_krever_kode: true, stenges: null, apen_dager: null, standard_dager: 180, apen_dager_min: 1, apen_dager_maks: 3650,
      siste_kursdag: "2027-03-01", stenges_effektiv: "2027-08-28" }, innst),
    innsjekk_info: Object.assign({ type: "fysisk", zoom_import: false }, kurs),
    urls: { lagre: "/lagre", logg_inn: "/logg-inn", token: "/token", redigerer: "/red", aktiv: "/aktiv", innstillinger: "/innst" },
  }, { dom: true });
  const d = v.dialog("innstillinger");
  const f = (navn) => d.querySelector(`[data-felt="${navn}"]`);
  let valgt = null;
  APNING.forEach((m) => Object.defineProperty(f("apning-" + m), "checked", { get: () => valgt === m, set: (b) => { if (b) { valgt = m; } else if (valgt === m) { valgt = null; } } }));
  const hendelse = (el, type) => (el.lyttere[type] || []).forEach((fn) => fn({}));
  const h = {
    v, d, f, valgt: () => valgt,
    apne(medKode) { v.KS.apneInnstillinger(!!medKode); },
    velg(modus) { f("apning-" + modus).checked = true; hendelse(f("apning-" + modus), "change"); },
    dager(tekst) { f("apen-dager").value = tekst; hendelse(f("apen-dager"), "input"); },
    dato(tekst) { f("stenges").value = tekst; hendelse(f("stenges"), "input"); },
    kode(paa) { f("krever-kode").checked = paa; },
    aktiv(paa) { f("aktiv").checked = paa; },
    hjelp: () => f("apning-hjelp").textContent,
    feil: () => (f("feil").hidden ? "" : f("feil").textContent),
    async ok() { d.querySelector('[data-ks="innst-ok"]').lyttere.click.forEach((fn) => fn()); await tomt(); await tomt(); },
    kallTil: (url) => v.kall.filter((k) => k.url === url).map((k) => JSON.parse(k.opp.body)),
    visning: () => v.rot.querySelector('[data-ks="innst-stenges"]').textContent,
    toaster: () => v.rot.querySelector('[data-ks="toast-omrade"]').children.map((t) => t.children[0].textContent),
  };
  return h;
}

test("dialogen: lagret tilstand velger riktig valg og fyller feltene (dato går foran dager)", () => {
  let h = innstDialog({}); h.apne();
  assert.strictEqual(h.valgt(), "standard"); assert.strictEqual(h.f("apning-standard-tekst").textContent, "Standard: 180 dager");
  assert.strictEqual(h.hjelp(), "Åpen til 28.08.2027.");
  h = innstDialog({ apen_dager: 90 }); h.apne();
  assert.strictEqual(h.valgt(), "dager"); assert.strictEqual(h.f("apen-dager").value, "90"); assert.strictEqual(h.hjelp(), "Åpen til 30.05.2027.");
  h = innstDialog({ apen_dager: 0 }); h.apne();
  assert.strictEqual(h.valgt(), "ingen"); assert.strictEqual(h.f("apen-dager").value, "", "0 er ikke et antall dager i feltet");
  h = innstDialog({ stenges: "2027-12-24", apen_dager: null }); h.apne();
  assert.strictEqual(h.valgt(), "dato"); assert.strictEqual(h.f("stenges").value, "2027-12-24"); assert.strictEqual(h.hjelp(), "Åpen til 24.12.2027.");
  assert.strictEqual(h.f("apen-dager").min, "1"); assert.strictEqual(h.f("apen-dager").max, "3650");
});

test("dialogen: antall dager sendes som tall, uten dato, og kodekravet sendes ikke når det ikke er endret", async () => {
  const h = innstDialog({}); h.apne();
  h.dager("365");
  assert.strictEqual(h.valgt(), "dager", "å skrive i feltet velger «antall dager»");
  assert.strictEqual(h.hjelp(), "Åpen til 29.02.2028.", "siste kursdag pluss nøyaktig 365 dager");
  h.v.svar.push(SVAR_INNST({ apen_dager: 365, stenges_effektiv: "2028-02-29" }));
  await h.ok();
  assert.deepStrictEqual(h.kallTil("/innst"), [{ stenges: "", apen_dager: 365 }]);
  assert.strictEqual(h.d.open, false, "dialogen lukkes etter lagring");
  assert.strictEqual(h.v.KS.S.innst.apen_dager, 365);
  assert.strictEqual(h.visning(), "Åpen til 29.02.2028 (365 dager etter siste kursdag)");
  assert.ok(h.toaster().includes("Innstillingene er lagret."));
});

test("dialogen: «Ingen tidsbegrensning» sender 0 dager, ikke tom verdi eller 1", async () => {
  const h = innstDialog({ apen_dager: 365 }); h.apne();
  h.velg("ingen");
  assert.ok(/uten tidsbegrensning/.test(h.hjelp()));
  h.v.svar.push(SVAR_INNST({ apen_dager: 0, stenges_effektiv: null }));
  await h.ok();
  assert.deepStrictEqual(h.kallTil("/innst"), [{ stenges: "", apen_dager: 0 }]);
  assert.strictEqual(h.visning(), "Åpen uten tidsbegrensning");
});

test("dialogen: en bestemt dato sendes som dato uten dager, og tilbake til standard sender tomme verdier", async () => {
  let h = innstDialog({ apen_dager: 90 }); h.apne();
  h.dato("2027-12-24");
  assert.strictEqual(h.valgt(), "dato");
  h.v.svar.push(SVAR_INNST({ stenges: "2027-12-24", stenges_effektiv: "2027-12-24" }));
  await h.ok();
  assert.deepStrictEqual(h.kallTil("/innst"), [{ stenges: "2027-12-24", apen_dager: null }]);
  assert.strictEqual(h.visning(), "Åpen til 24.12.2027");
  h = innstDialog({ apen_dager: 90 }); h.apne();
  h.velg("standard");
  h.v.svar.push(SVAR_INNST());
  await h.ok();
  assert.deepStrictEqual(h.kallTil("/innst"), [{ stenges: "", apen_dager: null }]);
  h = innstDialog({ stenges: "2027-12-24" }); h.apne();
  h.dager("30");                                               // dager erstatter datoen: datoen tømmes i samme kall
  h.v.svar.push(SVAR_INNST({ apen_dager: 30, stenges_effektiv: "2027-03-31" }));
  await h.ok();
  assert.deepStrictEqual(h.kallTil("/innst"), [{ stenges: "", apen_dager: 30 }]);
});

test("dialogen: grensene 1 og 3650 godtas, alt annet stoppes i nettleseren uten kall til serveren", async () => {
  for (const gyldig of ["1", "3650", " 30 "]) {
    const h = innstDialog({}); h.apne(); h.dager(gyldig);
    h.v.svar.push(SVAR_INNST({ apen_dager: parseInt(gyldig, 10) }));
    await h.ok();
    assert.strictEqual(h.feil(), "", "«" + gyldig + "» er gyldig");
    assert.strictEqual(h.kallTil("/innst")[0].apen_dager, parseInt(gyldig, 10));
  }
  for (const ugyldig of ["0", "3651", "-1", "abc", "", "12.5", "1e3", "99999", "365 dager"]) {
    const h = innstDialog({}); h.apne(); h.dager(ugyldig);
    assert.strictEqual(h.hjelp(), "Skriv et helt antall dager fra 1 til 3650.", "hjelpen sier hva som er galt for «" + ugyldig + "»");
    await h.ok();
    assert.strictEqual(h.kallTil("/innst").length, 0, "«" + ugyldig + "» skal ikke sendes");
    assert.strictEqual(h.feil(), "Skriv et helt antall dager fra 1 til 3650.");
    assert.strictEqual(h.d.open, true, "dialogen står åpen så feilen kan rettes");
  }
  const h = innstDialog({}); h.apne(); h.velg("dato");
  await h.ok();
  assert.strictEqual(h.feil(), "Velg en dato."); assert.strictEqual(h.kallTil("/innst").length, 0);
});

test("dialogen: hjelpeteksten uten kursdager sier at datoen kommer senere", () => {
  const h = innstDialog({ siste_kursdag: null, stenges_effektiv: null }); h.apne();
  assert.strictEqual(h.hjelp(), "Åpen 180 dager etter siste kursdag. Datoen vises når kursdagene er lagt inn.");
  h.dager("60");
  assert.strictEqual(h.hjelp(), "Åpen 60 dager etter siste kursdag. Datoen vises når kursdagene er lagt inn.");
});

/* Funn fra kritikeren: dialogen sendte alltid åpningstiden fra det siden leste da den ble lastet. En utdatert side (annen fane, annen administrator) kunne
   dermed nullstille «Ingen tidsbegrensning» til standard ved en helt urelatert endring. Nå sendes bare det som er endret. */
test("utdatert side: å endre bare kodekravet sender ikke åpningstiden (og skriver ikke den gamle verdien tilbake)", async () => {
  const h = innstDialog({ apen_dager: null });                 // siden ble lastet med standard ...
  h.apne();                                                    // ... mens en annen administrator har satt «Ingen tidsbegrensning» i mellomtiden
  h.kode(false);
  h.v.svar.push(SVAR_INNST({ innsjekk_krever_kode: false, apen_dager: 0, stenges_effektiv: null }));
  await h.ok();
  assert.deepStrictEqual(h.kallTil("/innst"), [{ innsjekk_krever_kode: false }], "bare kodekravet sendes");
  assert.strictEqual(h.v.KS.S.innst.apen_dager, 0, "siden retter seg etter serverens svar");
  assert.strictEqual(h.visning(), "Åpen uten tidsbegrensning");
  assert.strictEqual(h.v.KS.S.innst.innsjekk_krever_kode, false);
});

test("utdatert side: å endre bare åpningstiden sender ikke kodekravet", async () => {
  const h = innstDialog({ innsjekk_krever_kode: true }); h.apne();
  h.dager("100");
  h.v.svar.push(SVAR_INNST({ innsjekk_krever_kode: false, apen_dager: 100 }));   // en annen har slått av kodekravet i mellomtiden
  await h.ok();
  assert.deepStrictEqual(h.kallTil("/innst"), [{ stenges: "", apen_dager: 100 }]);
  assert.strictEqual(h.v.KS.S.innst.innsjekk_krever_kode, false, "kodekravet vises slik serveren har det");
});

test("dialogen: endres begge deler, sendes begge; uendret innhold sender ingenting", async () => {
  let h = innstDialog({ apen_dager: 30 }); h.apne();
  h.kode(false); h.dager("45");
  h.v.svar.push(SVAR_INNST({ innsjekk_krever_kode: false, apen_dager: 45 }));
  await h.ok();
  assert.deepStrictEqual(h.kallTil("/innst"), [{ innsjekk_krever_kode: false, stenges: "", apen_dager: 45 }]);
  for (const innst of [{}, { apen_dager: 30 }, { apen_dager: 0 }, { stenges: "2027-12-24" }]) {
    h = innstDialog(innst); h.apne();
    await h.ok();
    assert.strictEqual(h.v.kall.filter((k) => k.url === "/innst" || k.url === "/aktiv").length, 0, "ingen endring: ingenting sendes (" + JSON.stringify(innst) + ")");
    assert.strictEqual(h.d.open, false); assert.ok(h.toaster().includes("Innstillingene er lagret."));
  }
});

test("dialogen: nødbremsen sendes til egen adresse, og åpningstiden røres ikke av den", async () => {
  const h = innstDialog({ apen_dager: 0 }); h.apne();
  h.aktiv(false);
  h.v.svar.push({ status: 200, json: { status: "ok", aktiv: false } });
  await h.ok();
  assert.deepStrictEqual(h.kallTil("/aktiv"), [{ aktiv: false }]);
  assert.strictEqual(h.kallTil("/innst").length, 0);
  assert.strictEqual(h.v.KS.S.aktiv, false);
});

test("dialogen: en feil fra serveren vises i dialogen, som blir stående åpen, og innstillingene i siden er uendret", async () => {
  const h = innstDialog({ apen_dager: 90 }); h.apne();
  h.dager("400");
  h.v.svar.push({ status: 422, json: { status: "feil", melding: "Antall dager må være et helt tall fra 1 til 3650, eller «Ingen tidsbegrensning»." } });
  await h.ok();
  assert.ok(/Antall dager må være/.test(h.feil()));
  assert.strictEqual(h.d.open, true);
  assert.strictEqual(h.v.KS.S.innst.apen_dager, 90);
});

test("dialogen: digitale kurs har kodekravet avslått i dialogen og sender det aldri", async () => {
  const h = innstDialog({}, { type: "digital", zoom_import: true }); h.apne();
  assert.strictEqual(h.f("krever-kode").disabled, true);
  h.dager("60");
  h.v.svar.push(SVAR_INNST({ apen_dager: 60 }));
  await h.ok();
  assert.deepStrictEqual(h.kallTil("/innst"), [{ stenges: "", apen_dager: 60 }]);
});

(async () => {
  let feil = 0;
  for (const [navn, fn] of tester) {
    try { await fn(); } catch (e) { feil++; console.log("FEIL: " + navn + "\n      " + String(e && e.message || e).split("\n").join("\n      ")); }
  }
  if (feil) { console.log(feil + " av " + tester.length + " tester feilet"); process.exit(1); }
  console.log("OK " + tester.length + " tester");
})();
