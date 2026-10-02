// Oppmøtelisten (kurs/web/static/oppmoteliste.js) i en ekte nettleser (Edge/Chrome uten vindu): søk, trykk uten sideomlasting, hurtige
// trykk (siste vinner), Zoom-bekreftelse, «Merk alle» og «Fjern alle merkede», feil (raden går tilbake, tydelig melding), automatisk
// oppdatering, fast topplinje, 390 og 1440 px, lesetilgang og siden uten JavaScript. Startes av tests/test_oppmoteliste_nettleser.py, som
// lager testdata og tjener. Bruk: node oppmote_scenario.js <port> <debugport> <brukernavn> <passord> <ider-som-json>
// Skriver «RESULTAT_JSON:[{navn, ok, detalj}, ...]» som siste linje. Alle data er oppdiktede.
// Valgfritt: miljøvariabelen BILDEMAPPE gir skjermbilder (krever en cdp.js med bilde()); CDP_STI peker på en annen cdp.js.
const { start, vent } = require(process.env.CDP_STI || "./cdp.js");

const [PORT, DEBUGPORT, BRUKER, PASSORD, IDER] = process.argv.slice(2);
const D = JSON.parse(IDER);
const BASE = `http://127.0.0.1:${PORT}`;
const BILDER = process.env.BILDEMAPPE || "";
const resultat = [];
const sjekk = (navn, ok, detalj = "") => { resultat.push({ navn, ok: !!ok, detalj: String(detalj) }); };
const side = (dag) => `${BASE}/admin/kurs/${D.kid}/oppmote/${dag}`;
const norsk = (s) => s.replace(/[øØ]/g, "o").replace(/[åÅ]/g, "a").replace(/[æÆ]/g, "ae");

async function ventTil(s, uttrykk, ms = 5000) {
  let v = false;
  for (let t = 0; t < ms; t += 50) {
    v = await s.js(uttrykk);
    if (v) return v;
    await vent(50);
  }
  return v;
}
const rader = (s) => s.js(`Array.from(document.querySelectorAll('[data-rad]')).map(r => ({ pid: r.dataset.pid, navn: r.querySelector('.opp-navn').textContent,
  til: r.dataset.tilstede, kilde: r.dataset.kilde, info: r.querySelector('.opp-info').textContent, skjult: r.hidden, h: r.getBoundingClientRect().height,
  feil: r.dataset.feil || '', opptatt: r.getAttribute('aria-busy') || '', trykk: (r.querySelector('button.opp-knapp') || {getAttribute: () => null}).getAttribute('aria-pressed') }))`);
const rad = async (s, pid) => (await rader(s)).find((r) => r.pid === String(pid));
const teller = (s) => s.js(`document.getElementById('opp-teller').innerText.replace(/\\s+/g, ' ').trim()`);
const synlige = async (s) => (await rader(s)).filter((r) => !r.skjult).map((r) => r.navn);
const tilstand = (s) => s.js(`fetch(document.querySelector('[data-oppmoteliste]').dataset.statusUrl, {headers: {Accept: 'application/json'}, credentials: 'same-origin'}).then(r => r.json())`);
const skjult = (s, velger) => s.js(`(() => { const e = document.querySelector(${JSON.stringify(velger)}); return !e || e.hidden || getComputedStyle(e).display === 'none'; })()`);
const tekst = (s, velger) => s.js(`(document.querySelector(${JSON.stringify(velger)}) || {}).textContent || ''`);
const sokVerdi = (s, v) => s.js(`(() => { const f = document.getElementById('opp-sok'); f.focus(); f.value = ${JSON.stringify(v)}; f.dispatchEvent(new Event('input', {bubbles: true})); })()`);
// Bytter confirm med en spion som svarer `svar` og husker spørsmålene
const confirmSpion = (s, svar) => s.js(`(() => { window.__sporsmal = []; window.confirm = (t) => { window.__sporsmal.push(String(t)); return ${svar ? "true" : "false"}; }; })()`);
const sporsmal = (s) => s.js(`window.__sporsmal`);
// Bytter fetch med en stubb: kall mot POST svarer med `svar` (status og JSON) eller feiler som en nettfeil (svar = null)
const fetchStubb = (s, svar) => s.js(`(() => { if (!window.__origFetch) window.__origFetch = window.fetch;
  const svar = ${JSON.stringify(svar)};
  window.__kall = [];
  window.fetch = function (url, valg) { window.__kall.push({ url: String(url), metode: (valg || {}).method });
    if (svar === null) return Promise.reject(new TypeError('Failed to fetch'));
    return Promise.resolve(new Response(JSON.stringify(svar.data), { status: svar.status, headers: { 'Content-Type': 'application/json' } })); }; })()`);
const fetchTilbake = (s) => s.js(`(() => { if (window.__origFetch) window.fetch = window.__origFetch; })()`);
const bilde = async (s, navn, helside = true) => { if (BILDER && s.bilde) await s.bilde(`${BILDER}/${navn}.png`, helside); };
const post = (s, dag, sti, felt) => s.js(`(() => { const k = document.querySelector('[data-oppmoteliste]'); const b = new URLSearchParams(${JSON.stringify(felt)});
  b.set('csrf_token', k.dataset.csrf);
  return fetch(${JSON.stringify(`${BASE}/admin/kurs/${D.kid}/oppmote/`)} + ${dag} + ${JSON.stringify(sti)}, { method: 'POST', credentials: 'same-origin', body: b,
    headers: { Accept: 'application/json', 'X-CSRF-Token': k.dataset.csrf } }).then(r => r.status); })()`);

async function logginn(s, bruker, passord) {
  await s.gaa(`${BASE}/admin/logg-inn`);
  await s.skriv("input[name=brukernavn]", bruker);
  await s.skriv("input[name=passord]", passord);
  await s.js(`document.querySelector('form button:not([type=button])').click()`);
  for (let i = 0; i < 40 && (await s.js(`location.pathname`)).includes("logg-inn"); i++) await vent(250);
}

async function hovedscenario(s) {
  const N = D.navn.length;
  await s.storrelse(1440, 900);
  await logginn(s, BRUKER, PASSORD);
  await s.gaa(side(D.i_dag));

  // ---------- grunnbildet
  let r = await rader(s);
  sjekk("listen har alle deltakerne med plass", r.length === N, r.length);
  sjekk("listen er alfabetisk (norsk)", JSON.stringify(r.map((x) => x.navn)) === JSON.stringify(D.navn_sortert), r.map((x) => x.navn).join(","));
  sjekk("telleren viser «n av m til stede»", (await teller(s)) === `${D.stede_i_dag} av ${N} til stede`, await teller(s));
  sjekk("søkefeltet vises når JavaScript virker", !(await skjult(s, "#opp-sok-boks")));
  sjekk("søkefeltet har en synlig etikett for skjermlesere", await s.js(`document.querySelector('label[for=opp-sok]').textContent.length > 3`));
  await s.js(`window.__markor = 'ja'`);                     // forsvinner hvis siden lastes på nytt
  sjekk("alle rader er minst 56 px høye (1440)", r.every((x) => x.h >= 56), Math.min(...r.map((x) => x.h)));
  sjekk("raden viser hvordan det er registrert (QR/kode/Zoom/manuelt)", r.some((x) => x.kilde === "qr" && /^QR-kode kl\. \d\d:\d\d$/.test(x.info)) &&
    r.some((x) => x.kilde === "kode" && /^Innsjekk-kode kl\. \d\d:\d\d$/.test(x.info)) && r.some((x) => x.kilde === "zoom" && x.info === "Zoom, 95 min") &&
    r.some((x) => x.kilde === "manuell" && /^Manuelt kl\. \d\d:\d\d$/.test(x.info)), JSON.stringify(r.filter((x) => x.kilde).map((x) => x.info)));
  sjekk("av/på-tilstanden står i aria-pressed", r.every((x) => x.trykk === (x.til === "1" ? "true" : "false")));
  await bilde(s, "1440-dagens");

  // ---------- søk
  const sok = async (v) => { await sokVerdi(s, v); await vent(60); return synlige(s); };
  let v = await sok("solvi");
  sjekk("søk uten æøå finner «Sølvi»", v.length === 1 && v[0] === "Sølvi Tørresen", v.join(","));
  v = await sok("SØLVI");
  sjekk("søk med store bokstaver og ø finner «Sølvi»", v.length === 1 && v[0] === "Sølvi Tørresen", v.join(","));
  v = await sok("oystein");
  sjekk("«oystein» finner «Øystein»", v.length === 1 && v[0] === "Øystein Berg", v.join(","));
  v = await sok("håkon");
  sjekk("«håkon» finner «Håkon»", v.length === 1 && v[0] === "Håkon Åsen", v.join(","));
  v = await sok("hakon");
  sjekk("«hakon» finner «Håkon»", v.length === 1 && v[0] === "Håkon Åsen", v.join(","));
  v = await sok("kari han");
  sjekk("flere ord: alle må treffe", v.length === 1 && v[0] === "Kari Hansen", v.join(","));
  v = await sok("farouk mohammed");
  sjekk("ordene kan stå i vilkårlig rekkefølge", v.length === 1 && v[0] === "Mohammed Al-Farouk", v.join(","));
  v = await sok("nina.bakke@");
  sjekk("søk i deler av e-posten", v.length === 1 && v[0] === "Nina Bakke", v.join(","));
  v = await sok("eksempel.no");
  sjekk("felles del av e-posten treffer alle", v.length === N, v.length);
  v = await sok("qqq");
  sjekk("ingen treff gir «Ingen treff»", v.length === 0 && !(await skjult(s, "#opp-ingen-treff")) && (await tekst(s, "#opp-sokstatus")) === "Ingen treff.");
  v = await sok("sølvi");
  sjekk("søkestatus sier «Viser 1 av n»", (await tekst(s, "#opp-sokstatus")) === `Viser 1 av ${N}.`, await tekst(s, "#opp-sokstatus"));
  sjekk("«Merk alle» og «Fjern alle» er avslått mens søket er i bruk", await s.js(`Array.from(document.querySelectorAll('[data-oppmote-alle]')).every(a => a.getAttribute('aria-disabled') === 'true')`) &&
    !(await skjult(s, "#opp-alle-hint")));
  await confirmSpion(s, true);
  await s.klikk("[data-oppmote-alle=merk_alle]");
  sjekk("trykk på «Merk alle» under søk gjør ingenting", (await sporsmal(s)).length === 0 && (await tilstand(s)).antall === D.stede_i_dag);
  await bilde(s, "1440-sok", false);
  await s.tast("Escape", "Escape", 27);
  await vent(80);
  sjekk("Esc tømmer søket og viser alle", (await synlige(s)).length === N && (await s.js(`document.getElementById('opp-sok').value`)) === "");
  sjekk("«Merk alle» er på igjen når søket er tømt", await s.js(`Array.from(document.querySelectorAll('[data-oppmote-alle]')).every(a => !a.hasAttribute('aria-disabled'))`) &&
    (await skjult(s, "#opp-alle-hint")));

  // ---------- trykk på en rad: uten sideomlasting, tilstand fra tjeneren
  const nina = D.pids[D.navn.indexOf("Nina Bakke")], kari = D.pids[D.navn.indexOf("Kari Hansen")], solvi = D.pids[D.navn.indexOf("Sølvi Tørresen")];
  const start_ = D.stede_i_dag;
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${nina}').dataset.tilstede === '1' && /^Manuelt kl/.test(document.querySelector('#rad-${nina} .opp-info').textContent)`);
  let x = await rad(s, nina);
  sjekk("trykk registrerer oppmøte manuelt uten sideomlasting", x.til === "1" && x.kilde === "manuell" && x.trykk === "true" && /^Manuelt kl\. \d\d:\d\d$/.test(x.info) &&
    (await s.js(`window.__markor`)) === "ja", JSON.stringify(x));
  sjekk("telleren teller opp", (await teller(s)) === `${start_ + 1} av ${N} til stede`, await teller(s));
  let t = await tilstand(s);
  sjekk("tjeneren har det samme (kilde manuell)", t.rader[nina].til_stede === true && t.rader[nina].kilde === "manuell" && t.antall === start_ + 1);
  sjekk("adressen er uendret (ingen navn, ingen søketekst)", (await s.js(`location.pathname + location.search + location.hash`)) === `/admin/kurs/${D.kid}/oppmote/${D.i_dag}`);
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${nina}').dataset.tilstede === '0' && !document.querySelector('#rad-${nina}').getAttribute('aria-busy')`);
  x = await rad(s, nina);
  t = await tilstand(s);
  sjekk("nytt trykk fjerner oppmøtet igjen", x.til === "0" && x.info === "" && t.rader[nina].til_stede === false && (await teller(s)) === `${start_} av ${N} til stede`);

  // ---------- raske trykk: siste trykk vinner, og tjeneren ender likt
  await s.js(`(() => { const b = document.querySelector('#rad-${nina} button.opp-knapp'); b.click(); b.click(); })()`);        // på, av
  await ventTil(s, `!document.querySelector('#rad-${nina}').getAttribute('aria-busy')`);
  await vent(400);
  x = await rad(s, nina); t = await tilstand(s);
  sjekk("to raske trykk (på, av) ender som «ikke registrert» også på tjeneren", x.til === "0" && t.rader[nina].til_stede === false, JSON.stringify(x));
  await s.js(`(() => { const b = document.querySelector('#rad-${nina} button.opp-knapp'); b.click(); b.click(); b.click(); })()`);   // på, av, på
  await ventTil(s, `!document.querySelector('#rad-${nina}').getAttribute('aria-busy')`);
  await vent(400);
  x = await rad(s, nina); t = await tilstand(s);
  sjekk("tre raske trykk ender som «til stede» også på tjeneren", x.til === "1" && t.rader[nina].til_stede === true && t.rader[nina].kilde === "manuell" && t.antall === start_ + 1, JSON.stringify(x));
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${nina}').dataset.tilstede === '0' && !document.querySelector('#rad-${nina}').getAttribute('aria-busy')`);

  // ---------- QR-oppmøte kan fjernes uten spørsmål, og «til stede» overskriver aldri en QR-registrering
  await confirmSpion(s, true);
  await s.klikk(`#rad-${kari} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${kari}').dataset.tilstede === '0' && !document.querySelector('#rad-${kari}').getAttribute('aria-busy')`);
  sjekk("QR-oppmøte fjernes uten ekstra spørsmål", (await sporsmal(s)).length === 0 && (await tilstand(s)).rader[kari].til_stede === false);
  await s.klikk(`#rad-${kari} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${kari}').dataset.tilstede === '1' && !document.querySelector('#rad-${kari}').getAttribute('aria-busy')`);

  // ---------- Zoom: å fjerne krever ekstra bekreftelse
  await confirmSpion(s, false);
  await s.klikk(`#rad-${solvi} button.opp-knapp`);
  await vent(400);
  x = await rad(s, solvi); t = await tilstand(s);
  sjekk("Zoom: avbrutt bekreftelse lar oppmøtet stå (siden og tjeneren)", (await sporsmal(s)).length === 1 && /Zoom/.test((await sporsmal(s))[0]) && x.til === "1" && x.kilde === "zoom" &&
    t.rader[solvi].kilde === "zoom", JSON.stringify(await sporsmal(s)));
  await confirmSpion(s, true);
  await s.klikk(`#rad-${solvi} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${solvi}').dataset.tilstede === '0' && !document.querySelector('#rad-${solvi}').getAttribute('aria-busy')`);
  t = await tilstand(s);
  sjekk("Zoom: bekreftet fjerning fjerner oppmøtet", (await sporsmal(s)).length === 1 && t.rader[solvi].til_stede === false);
  await s.klikk(`#rad-${solvi} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${solvi}').dataset.tilstede === '1' && !document.querySelector('#rad-${solvi}').getAttribute('aria-busy')`);

  // ---------- feil: raden går tilbake, meldingen er tydelig
  const forTeller = await teller(s);
  await fetchStubb(s, null);
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${nina}').dataset.feil === '1'`);
  x = await rad(s, nina);
  sjekk("nettfeil: raden går tilbake til «ikke registrert»", x.til === "0" && x.trykk === "false" && x.info === "" && x.opptatt === "", JSON.stringify(x));
  sjekk("nettfeil: feilmeldingen er synlig, i en varselregion og sier at det ikke er lagret", !(await skjult(s, "#opp-feil")) &&
    (await s.js(`document.getElementById('opp-feil').getAttribute('role')`)) === "alert" && /Fikk ikke kontakt/.test(await tekst(s, "#opp-feil")) &&
    /Nina Bakke er ikke lagret/.test(await tekst(s, "#opp-feil")), await tekst(s, "#opp-feil"));
  sjekk("nettfeil: raden viser «Ikke lagret» og telleren er uendret", !(await skjult(s, `#rad-${nina} .opp-radfeil`)) && (await teller(s)) === forTeller);
  await bilde(s, "1440-feil", false);
  await fetchStubb(s, { status: 403, data: { feil: "ingen_tilgang" } });
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${nina}').dataset.feil === '1' && /tilgang/.test(document.getElementById('opp-feil').textContent)`);
  sjekk("403: «ingen tilgang» vises og raden går tilbake", (await rad(s, nina)).til === "0" && /ikke tilgang/.test(await tekst(s, "#opp-feil")), await tekst(s, "#opp-feil"));
  await fetchStubb(s, { status: 401, data: { feil: "ikke_innlogget" } });
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `/logget ut/.test(document.getElementById('opp-feil').textContent)`);
  sjekk("401: «du er logget ut» vises og raden går tilbake", (await rad(s, nina)).til === "0" && /logget ut/.test(await tekst(s, "#opp-feil")), await tekst(s, "#opp-feil"));
  await fetchStubb(s, { status: 500, data: null });
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `/feil 500/.test(document.getElementById('opp-feil').textContent)`);
  sjekk("500: feilen vises med kode og raden går tilbake", (await rad(s, nina)).til === "0" && /feil 500/.test(await tekst(s, "#opp-feil")), await tekst(s, "#opp-feil"));
  await fetchStubb(s, { status: 409, data: { status: "feil", melding: "Personen har ikke plass på kurset lenger." } });
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `/ikke plass/.test(document.getElementById('opp-feil').textContent)`);
  sjekk("409: tjenerens forklaring vises", /Personen har ikke plass på kurset lenger\. \(Nina Bakke\)/.test(await tekst(s, "#opp-feil")), await tekst(s, "#opp-feil"));
  await confirmSpion(s, true);
  await fetchStubb(s, null);
  await s.klikk("[data-oppmote-alle=merk_alle]");
  await ventTil(s, `/Fikk ikke kontakt/.test(document.getElementById('opp-feil').textContent) && !document.querySelector('[data-oppmoteliste]').hasAttribute('data-opptatt')`);
  sjekk("«Merk alle» som feiler: tydelig feil, ingenting endret og siden er ikke låst", /Oppmøtet er ikke endret/.test(await tekst(s, "#opp-feil")) && (await teller(s)) === forTeller,
    await tekst(s, "#opp-feil"));
  await fetchTilbake(s);
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${nina}').dataset.tilstede === '1' && !document.querySelector('#rad-${nina}').getAttribute('aria-busy')`);
  sjekk("etter en vellykket lagring forsvinner feilmeldingen", (await skjult(s, "#opp-feil")) && (await skjult(s, `#rad-${nina} .opp-radfeil`)) && !(await rad(s, nina)).feil);
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${nina}').dataset.tilstede === '0' && !document.querySelector('#rad-${nina}').getAttribute('aria-busy')`);

  // ---------- automatisk oppdatering (noen skannet QR-koden mens listen sto åpen)
  const petter = D.pids[D.navn.indexOf("Petter Dass")];
  sjekk("POST fra en annen økt (simulert skanning) lykkes", (await post(s, D.i_dag, "/rad", { paamelding_id: petter, onsket: "til" })) === 200);
  if ((await s.js(`document.visibilityState`)) === "visible") {
    await s.js(`document.dispatchEvent(new Event('visibilitychange'))`);
    await ventTil(s, `document.querySelector('#rad-${petter}').dataset.tilstede === '1'`);
    sjekk("listen oppdaterer seg fra tjeneren, og telleren følger", (await rad(s, petter)).til === "1" && (await teller(s)) === `${(await tilstand(s)).antall} av ${N} til stede`, await teller(s));
  }
  await s.klikk(`#rad-${petter} button.opp-knapp`);          // tilbake til det den var
  await ventTil(s, `document.querySelector('#rad-${petter}').dataset.tilstede === '0' && !document.querySelector('#rad-${petter}').getAttribute('aria-busy')`);
  await fetchStubb(s, { status: 200, data: { status: "ok", antall: 0, totalt: 1, rader: { "999999": { til_stede: false, kilde: "", hvordan: "" } } } });
  await s.js(`document.dispatchEvent(new Event('visibilitychange'))`);
  await ventTil(s, `!document.getElementById('opp-endret').hidden`);
  sjekk("endret deltakerliste: en tydelig beskjed om å laste siden på nytt", !(await skjult(s, "#opp-endret")) && /Last siden på nytt/.test(await tekst(s, "#opp-endret")));
  await fetchTilbake(s);

  // ---------- Merk alle og Fjern alle merkede
  const foerAlle = (await tilstand(s)).antall;
  await confirmSpion(s, false);
  await s.klikk("[data-oppmote-alle=merk_alle]");
  await vent(300);
  sjekk("«Merk alle» spør først, og avbrutt bekreftelse endrer ingenting", (await sporsmal(s)).length === 1 && /Merke alle \d+ som ikke er registrert/.test((await sporsmal(s))[0]) &&
    (await tilstand(s)).antall === foerAlle && (await rad(s, nina)).til === "0", JSON.stringify(await sporsmal(s)));
  await confirmSpion(s, true);
  await s.klikk("[data-oppmote-alle=merk_alle]");
  await ventTil(s, `document.getElementById('opp-antall').textContent === '${N}'`);
  t = await tilstand(s);
  sjekk("«Merk alle til stede» merker alle", (await teller(s)) === `${N} av ${N} til stede` && t.antall === N && (await rader(s)).every((y) => y.til === "1"), await teller(s));
  // Ola hadde kode-registrering fra før (den overskrives aldri); de andre var uregistrert nå, og er merket manuelt
  const kilder = (await rader(s)).map((y) => y.kilde);
  sjekk("«Merk alle»: den som var registrert fra før beholder kilden, de andre blir manuelle", (await rad(s, D.pids[D.navn.indexOf("Ola Nordmann")])).kilde === "kode" &&
    kilder.filter((k) => k === "manuell").length === N - 1, JSON.stringify(kilder));
  sjekk("«Merk alle» sier hvor mange som ble merket", !(await skjult(s, "#opp-melding")) && new RegExp(`${N - foerAlle} deltakere merket som til stede`).test(await tekst(s, "#opp-melding")), await tekst(s, "#opp-melding"));
  await bilde(s, "1440-alle-merket");
  await s.klikk("[data-oppmote-alle=merk_alle]");
  await vent(200);
  sjekk("«Merk alle» når alle er merket: sier fra i stedet for å spørre", (await sporsmal(s)).length === 1 && /allerede registrert/.test(await tekst(s, "#opp-melding")), await tekst(s, "#opp-melding"));
  await confirmSpion(s, false);
  await s.klikk("[data-oppmote-alle=fjern_alle]");
  await vent(300);
  sjekk("«Fjern alle merkede» spør først, og avbrutt bekreftelse endrer ingenting", (await sporsmal(s)).length === 1 && /Fjerne oppmøtet for/.test((await sporsmal(s))[0]) && (await tilstand(s)).antall === N);
  await confirmSpion(s, true);
  await s.klikk("[data-oppmote-alle=fjern_alle]");
  await ventTil(s, `document.getElementById('opp-antall').textContent === '0'`);
  t = await tilstand(s);
  sjekk("«Fjern alle merkede» fjerner alle", (await teller(s)) === `0 av ${N} til stede` && t.antall === 0 && (await rader(s)).every((y) => y.til === "0" && y.info === ""), await teller(s));
  await s.klikk("[data-oppmote-alle=fjern_alle]");
  sjekk("«Fjern alle» når ingen er merket: sier fra i stedet for å spørre", /Ingen registreringer å fjerne/.test(await tekst(s, "#opp-melding")), await tekst(s, "#opp-melding"));

  // ---------- fast topplinje (1440)
  await s.js(`window.scrollTo(0, 100000)`);
  await vent(150);
  let topp = await s.js(`(() => { const e = document.querySelector('.opp-topplinje').getBoundingClientRect(); return { top: e.top, h: e.height, sok: document.getElementById('opp-sok').getBoundingClientRect().top }; })()`);
  sjekk("topplinjen med teller og søk står fast når listen ruller (1440)", Math.abs(topp.top) <= 1 && topp.h > 40 && topp.sok >= 0, JSON.stringify(topp));
  await s.js(`window.scrollTo(0, 0)`);

  // ---------- dag som ikke er startet
  await s.gaa(side(D.i_morgen));
  sjekk("fremtidig dag: forklaring, ingen knapper og ingen liste", /ikke startet ennå/.test(await s.js(`document.body.innerText`)) && (await s.js(`document.querySelectorAll('[data-rad], button.opp-knapp, [data-oppmote-alle]').length`)) === 0);

  // ---------- 390 px: mobil
  await s.storrelse(390, 844, true);
  await s.gaa(side(D.i_dag));
  r = await rader(s);
  sjekk("mobil: alle rader er minst 56 px høye", r.length === N && r.every((y) => y.h >= 56), Math.min(...r.map((y) => y.h)));
  const bredde = await s.js(`({ innhold: document.documentElement.scrollWidth, vindu: window.innerWidth })`);
  sjekk("mobil: ingen sideveis rulling", bredde.innhold <= bredde.vindu, JSON.stringify(bredde));
  sjekk("mobil: menyen i toppen er skjult, «Ferdig» og søket er synlige", (await skjult(s, "header.oppmote-hode nav")) && !(await skjult(s, ".opp-ferdig")) && !(await skjult(s, "#opp-sok")));
  const ferdig = await s.js(`(() => { const b = document.querySelector('.opp-ferdig').getBoundingClientRect(); return { h: b.height, w: b.width }; })()`);
  sjekk("mobil: «Ferdig» er en stor trykkflate", ferdig.h >= 44 && ferdig.w >= 44, JSON.stringify(ferdig));
  await bilde(s, "390-dagens-topp", false);
  await bilde(s, "390-dagens-hel");
  await s.js(`window.scrollTo(0, 100000)`);
  await vent(150);
  topp = await s.js(`(() => { const e = document.querySelector('.opp-topplinje').getBoundingClientRect(); return { top: e.top, h: e.height }; })()`);
  sjekk("mobil: topplinjen med teller og søk står fast når listen ruller", Math.abs(topp.top) <= 1 && topp.h > 40, JSON.stringify(topp));
  await bilde(s, "390-rullet", false);
  await s.js(`window.scrollTo(0, 0)`);
  await sokVerdi(s, "ø");
  await vent(80);
  await bilde(s, "390-sok", false);
  await sokVerdi(s, "");
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${nina}').dataset.tilstede === '1' && !document.querySelector('#rad-${nina}').getAttribute('aria-busy')`);
  await bilde(s, "390-trykket", false);
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${nina}').dataset.tilstede === '0' && !document.querySelector('#rad-${nina}').getAttribute('aria-busy')`);
  await fetchStubb(s, null);
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `!document.getElementById('opp-feil').hidden`);
  const feilPos = await s.js(`(() => { const e = document.getElementById('opp-feil').getBoundingClientRect(); return { bunn: window.innerHeight - e.bottom, synlig: e.top >= 0 && e.bottom <= window.innerHeight }; })()`);
  sjekk("mobil: feilmeldingen ligger synlig nederst på skjermen", feilPos.synlig && feilPos.bunn <= 24, JSON.stringify(feilPos));
  await bilde(s, "390-feil", false);
  await fetchTilbake(s);

  // ---------- lesetilgang
  await s.storrelse(1440, 900);
  await s.gaa(`${BASE}/admin/logg-ut`);
  await logginn(s, D.lese, D.lese_passord);
  await s.gaa(side(D.i_dag));
  sjekk("lesetilgang: listen vises uten knapper og uten skjemaer", (await rader(s)).length === N &&
    (await s.js(`document.querySelectorAll('button.opp-knapp, [data-rad-skjema], [data-oppmote-alle]').length`)) === 0 && /lesetilgang/.test(await s.js(`document.body.innerText`)));
  await bilde(s, "1440-lesetilgang", false);
  await s.gaa(`${BASE}/admin/logg-ut`);
  await logginn(s, BRUKER, PASSORD);

  // ---------- uten JavaScript: vanlige skjemaknapper og bekreftelsesside
  await s.cdp("Emulation.setScriptExecutionDisabled", { value: true });
  await s.storrelse(390, 844, true);
  await s.gaa(side(D.i_dag));
  sjekk("uten JavaScript: søkefeltet er skjult (bare skript viser det)", await skjult(s, "#opp-sok-boks"));
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `location.hash === '#rad-${nina}'`);
  await vent(300);
  x = await rad(s, nina);
  sjekk("uten JavaScript: trykk på raden sender skjemaet og viser den nye tilstanden", x.til === "1" && x.kilde === "manuell" && (await s.js(`location.hash`)) === `#rad-${nina}`, JSON.stringify(x));
  sjekk("uten JavaScript: telleren er riktig etter innsending", (await teller(s)) === `1 av ${N} til stede`, await teller(s));
  await bilde(s, "390-uten-js", false);
  await s.klikk(`#rad-${nina} button.opp-knapp`);
  await ventTil(s, `document.querySelector('#rad-${nina}').dataset.tilstede === '0'`);
  await vent(300);
  sjekk("uten JavaScript: nytt trykk fjerner oppmøtet", (await rad(s, nina)).til === "0" && (await teller(s)) === `0 av ${N} til stede`);
  await s.klikk("[data-oppmote-alle=merk_alle]");
  await ventTil(s, `location.search === '?bekreft=merk_alle'`);
  await vent(300);
  sjekk("uten JavaScript: «Merk alle» viser først en bekreftelse (ingenting er endret ennå)", (await s.js(`document.getElementById('opp-bekreft-tittel') && document.getElementById('opp-bekreft-tittel').textContent`)) === "Merke alle som til stede?" &&
    (await teller(s)) === `0 av ${N} til stede`, await teller(s));
  await bilde(s, "390-uten-js-bekreft", false);
  await s.js(`document.querySelector('#bekreft button').click()`);
  await ventTil(s, `document.getElementById('opp-antall').textContent === '${N}' && location.search === ''`);
  await vent(300);
  sjekk("uten JavaScript: bekreftet «Merk alle» merker alle, med melding fra tjeneren", (await teller(s)) === `${N} av ${N} til stede` && /merket som til stede/.test(await s.js(`document.querySelector('main .flash') ? document.querySelector('main .flash').textContent : ''`)),
    await teller(s));
  await s.klikk("[data-oppmote-alle=fjern_alle]");
  await ventTil(s, `location.search === '?bekreft=fjern_alle'`);
  await vent(300);
  await s.js(`document.querySelector('#bekreft button').click()`);
  await ventTil(s, `document.getElementById('opp-antall').textContent === '0' && location.search === ''`);
  sjekk("uten JavaScript: «Fjern alle» går også via bekreftelsen", (await teller(s)) === `0 av ${N} til stede`, await teller(s));
  // Zoom uten JavaScript: dag i går har en Zoom-registrering (Mohammed)
  const mohammed = D.pids[D.navn.indexOf("Mohammed Al-Farouk")];
  await s.gaa(side(D.i_gaar));
  await s.klikk(`#rad-${mohammed} button.opp-knapp`);
  await ventTil(s, `location.search.indexOf('bekreft=zoom') >= 0`);
  await vent(300);
  sjekk("uten JavaScript: å fjerne Zoom-oppmøte gir en egen bekreftelse, og oppmøtet står ennå", /Zoom/.test(await s.js(`document.getElementById('opp-bekreft-tittel').textContent`)) &&
    (await rad(s, mohammed)).kilde === "zoom");
  await bilde(s, "390-uten-js-zoom", false);
  await s.js(`document.querySelector('#bekreft button').click()`);
  await ventTil(s, `document.querySelector('#rad-${mohammed}') && document.querySelector('#rad-${mohammed}').dataset.tilstede === '0'`);
  sjekk("uten JavaScript: bekreftet fjerning tar bort Zoom-oppmøtet", (await rad(s, mohammed)).til === "0");
  await s.cdp("Emulation.setScriptExecutionDisabled", { value: false });

  // ---------- 1440 uten JavaScript-feil i konsollen
  sjekk("ingen JavaScript-feil i konsollen", s.konsoll.length === 0, s.konsoll.join(" | "));
}

(async () => {
  const s = await start(parseInt(DEBUGPORT, 10));
  try {
    await hovedscenario(s);
  } catch (e) {
    sjekk("scenarioet gikk ikke ut på feil", false, e && e.stack || e);
  } finally {
    await s.lukk();
  }
  console.log("RESULTAT_JSON:" + JSON.stringify(resultat));
})();
