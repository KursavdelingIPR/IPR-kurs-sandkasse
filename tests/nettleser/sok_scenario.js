// Deltakersøkets JavaScript i en ekte nettleser (Edge/Chrome uten vindu): rullegardin, tastatur, «/»-hurtigtast, feilmeldinger,
// Referer og synlig kontrast. Startes av tests/test_deltakersok_nettleser.py, som lager testdata og tjener.
// Bruk: node sok_scenario.js <port> <debugport> <brukernavn> <passord>
// Skriver «RESULTAT_JSON:[{navn, ok, detalj}, ...]» som siste linje. Alle data er oppdiktede.
const { start, vent } = require("./cdp.js");

const [PORT, DEBUGPORT, BRUKER, PASSORD] = process.argv.slice(2);
const BASE = `http://127.0.0.1:${PORT}`;
const resultat = [];
const sjekk = (navn, ok, detalj = "") => { resultat.push({ navn, ok: !!ok, detalj: String(detalj) }); };

const FELT = "#sok-deltaker";
const LISTE = "#sok-deltaker-liste";
const tekst = (s, velger) => s.js(`document.querySelector(${JSON.stringify(velger)}).innerText`);
const valgAntall = (s) => s.js(`document.querySelectorAll('${LISTE} a[role=option]').length`);
const skjult = (s) => s.js(`document.querySelector('${LISTE}').hidden`);
const sti = (s) => s.js(`location.pathname + location.search + location.hash`);
const attr = (s, velger, navn) => s.js(`document.querySelector(${JSON.stringify(velger)}).getAttribute(${JSON.stringify(navn)})`);

// Venter til uttrykket blir sant (maks `ms`); returnerer siste verdi.
async function ventTil(s, uttrykk, ms = 4000) {
  let v = false;
  for (let t = 0; t < ms; t += 50) {
    v = await s.js(uttrykk);
    if (v) return v;
    await vent(50);
  }
  return v;
}
const ventListe = (s, ms) => ventTil(s, `document.querySelectorAll('${LISTE} a[role=option]').length > 0 && !document.querySelector('${LISTE}').hidden`, ms);
// Tømmer feltet uten å gå via tastaturet og lukker listen.
const tom = async (s) => {
  await s.js(`(() => { const f = document.querySelector('${FELT}'); f.value = ''; f.dispatchEvent(new Event('input', {bubbles: true}));
    document.querySelector('${LISTE}').replaceChildren(); })()`);
  await vent(60);
};
// Skriver ved å sette verdien og sende input-hendelse (som nettleseren gjør ved tasting), og deretter vente på svaret.
const sett = (s, verdi, felt = FELT) => s.js(`(() => { const f = document.querySelector('${felt}'); f.focus(); f.value = ${JSON.stringify(verdi)}; f.dispatchEvent(new Event('input', {bubbles: true})); })()`);
const tast = (s, key, code, kode, tekst, mod) => s.tast(key, code, kode, tekst, mod);
const NED = (s) => tast(s, "ArrowDown", "ArrowDown", 40);
const OPP = (s) => tast(s, "ArrowUp", "ArrowUp", 38);
const ENTER = (s) => tast(s, "Enter", "Enter", 13, "\r");
const aktivId = (s) => attr(s, FELT, "aria-activedescendant");

// Bytter fetch med en spion som husker kallene (og sender dem videre). Kalles på nytt etter hver sidelasting.
const spion = (s) => s.js(`(() => { window.__kall = []; const orig = window.fetch; window.__orig = orig;
  window.fetch = function (url, valg) { window.__kall.push({ url: String(url), metode: (valg || {}).method, body: (valg || {}).body,
    hoder: (valg || {}).headers, credentials: (valg || {}).credentials, cache: (valg || {}).cache, referrerPolicy: (valg || {}).referrerPolicy });
    return orig.apply(this, arguments); }; })()`);
const kall = (s) => s.js(`window.__kall`);

async function logginn(s) {
  await s.gaa(`${BASE}/admin/logg-inn`);
  await s.skriv("input[name=brukernavn]", BRUKER);
  await s.skriv("input[name=passord]", PASSORD);
  await s.js(`document.querySelector('form button:not([type=button])').click()`);
  for (let i = 0; i < 40 && (await s.js(`location.pathname`)).includes("logg-inn"); i++) await vent(250);
}

async function rullegardin(s) {
  await s.gaa(`${BASE}/admin`);
  await spion(s);
  sjekk("Oversikten har ett deltakersøk og ingen søkefelt i menyen", (await s.js(`document.querySelectorAll('input[name=q]').length`)) === 1 &&
        (await s.js(`!document.querySelector('header input') && !document.querySelector('header form')`)));
  sjekk("feltet sier hva man kan søke på", (await attr(s, FELT, "placeholder")) === "Navn, e-post, telefon eller påmeldingsnr.");
  sjekk("deltakersøk og kurssøk står side ved side og ikke over hele bredden", await s.js(`(() => { const a = document.querySelector('#sok-deltaker').getBoundingClientRect(),
    b = document.querySelector('#sok-kurs').getBoundingClientRect(); return Math.abs(a.top - b.top) < 4 && a.right < b.left && (b.right - a.left) < 1100; })()`));

  // ---- terskel: ett tegn gir ingen forespørsel ----
  await sett(s, "k");
  await vent(600);
  sjekk("ett tegn: ingen forespørsel og lukket liste", (await kall(s)).length === 0 && (await skjult(s)));

  // ---- ventetid: bare siste tekst sendes, og det skjer raskt ----
  await s.js(`(async () => { const f = document.querySelector('${FELT}'); for (const v of ['ka', 'kari']) { f.value = v; f.dispatchEvent(new Event('input', {bubbles: true})); await new Promise(r => setTimeout(r, 80)); } })()`);
  sjekk("listen vises innen ett sekund (ventetid ca. 0,2 s)", await ventListe(s, 1000));
  const k = await kall(s);
  sjekk("tasting med 80 ms mellom gir bare ett kall (ventetid)", k.length === 1, JSON.stringify(k.map((x) => x.body)));
  const csrf = await attr(s, "form[data-deltakersok]", "data-csrf");
  sjekk("kallet er POST til /admin/sok.json uten spørrestreng", k[0] && k[0].metode === "POST" && k[0].url.endsWith("/admin/sok.json") && !k[0].url.includes("?"), k[0] && k[0].url);
  sjekk("søketeksten står i kroppen som JSON og ikke i adressen", k[0] && k[0].body === JSON.stringify({ q: "kari" }) && !k[0].url.includes("kari"), k[0] && k[0].body);
  sjekk("kallet har CSRF-hode, JSON, same-origin, no-store og no-referrer", k[0] && k[0].hoder["X-CSRF-Token"] === csrf && csrf.length > 20 &&
        k[0].hoder["Content-Type"] === "application/json" && k[0].credentials === "same-origin" && k[0].cache === "no-store" &&
        k[0].referrerPolicy === "no-referrer", JSON.stringify(k[0]));
  sjekk("søketeksten lagres ikke i nettleseren", await s.js(`localStorage.length === 0 && sessionStorage.length === 0 && document.cookie.indexOf('kari') < 0`));

  // ---- listen ----
  sjekk("8 treff pluss «åpne resultatsiden»-raden (9 valg)", (await valgAntall(s)) === 9 && (await s.js(`!!document.querySelector('${LISTE} #sok-deltaker-v7') && !document.querySelector('${LISTE} #sok-deltaker-v8') && !!document.querySelector('${LISTE} #sok-deltaker-alle')`)));
  const alle = await tekst(s, `${LISTE} #sok-deltaker-alle`);
  sjekk("raden lover ikke mer enn siden gir: «Åpne resultatsiden (N treff)»", /^Åpne resultatsiden \(\d+ treff\) →$/.test(alle.trim()), alle);
  sjekk("listen er en listbox og er åpen", (await attr(s, LISTE, "role")) === "listbox" && (await attr(s, FELT, "aria-expanded")) === "true");
  sjekk("treffet uthever det som traff med <mark>", await s.js(`document.querySelector('${LISTE} a[role=option] mark') && document.querySelector('${LISTE} a[role=option] mark').textContent.toLowerCase() === 'kari'`));
  const forste = await tekst(s, `${LISTE} a[role=option]`);
  sjekk("første treff viser navn, e-post, grunn og antall påmeldinger", /Kari/.test(forste) && /@eksempel\.no/.test(forste) && /Treff på navn · \d+ påmelding/.test(forste), forste.replace(/\n/g, " | "));
  sjekk("«åpne»-raden har lenkefarge (blå), ikke tekstfarge", (await s.js(`getComputedStyle(document.querySelector('${LISTE} #sok-deltaker-alle')).color`)) === "rgb(31, 92, 115)");
  sjekk("alternativene er ikke i tabulatorrekkefølgen", await s.js(`[...document.querySelectorAll('${LISTE} a')].every(a => a.tabIndex === -1)`));

  // ---- tastatur ----
  await s.js(`document.querySelector('${FELT}').focus()`);
  await NED(s); await NED(s);
  sjekk("pil ned to ganger velger nr. 2 (aria-activedescendant og aria-selected)", (await aktivId(s)) === "sok-deltaker-v1" &&
        (await attr(s, `${LISTE} #sok-deltaker-v1`, "aria-selected")) === "true" && (await attr(s, `${LISTE} #sok-deltaker-v0`, "aria-selected")) === "false");
  await OPP(s);
  sjekk("pil opp går tilbake til nr. 1", (await aktivId(s)) === "sok-deltaker-v0" && (await attr(s, `${LISTE} #sok-deltaker-v1`, "aria-selected")) === "false");
  await OPP(s);
  sjekk("pil opp fra første går rundt til siste rad", (await aktivId(s)) === "sok-deltaker-alle");
  await NED(s);
  sjekk("pil ned fra siste rad går rundt til første", (await aktivId(s)) === "sok-deltaker-v0");
  await tast(s, "Escape", "Escape", 27);
  sjekk("Esc lukker listen og fjerner valget", (await skjult(s)) && (await attr(s, FELT, "aria-expanded")) === "false" && (await aktivId(s)) === null);
  await NED(s);
  sjekk("pil ned åpner listen igjen med første treff valgt", !(await skjult(s)) && (await aktivId(s)) === "sok-deltaker-v0");
  await ENTER(s);
  await ventTil(s, `location.pathname === '/admin/sok'`);
  sjekk("Enter på valgt treff åpner søkesiden med treffet (#person-…)", /^\/admin\/sok\?q=kari#person-\d+$/.test(await sti(s)), await sti(s));

  // ---- Enter uten valg: hele resultatet ----
  await s.gaa(`${BASE}/admin`);
  await spion(s);
  await s.skriv(FELT, "kari hansen");
  await ventListe(s);
  await ENTER(s);
  await ventTil(s, `location.pathname === '/admin/sok'`);
  sjekk("Enter uten valg åpner hele resultatet (/admin/sok?q=kari+hansen)", (await sti(s)) === "/admin/sok?q=kari+hansen", await sti(s));

  // ---- rask Enter etter pilvalg: skal søke på det som står i feltet nå, ikke åpne det gamle treffet ----
  await s.gaa(`${BASE}/admin`);
  await s.skriv(FELT, "kari");
  await ventListe(s);
  await NED(s); await NED(s);
  const rask = await s.js(`(() => { const f = document.querySelector('${FELT}'); f.value = 'kari olsen'; f.dispatchEvent(new Event('input', {bubbles: true}));
    const enter = new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true});
    const ikkeStanset = f.dispatchEvent(enter);
    return {ikkeStanset, valgt: f.getAttribute('aria-activedescendant'), sti: location.pathname}; })()`);
  sjekk("Enter rett etter tasting bruker teksten i feltet (ikke det gamle valget)", rask.ikkeStanset && rask.valgt === null && rask.sti === "/admin", JSON.stringify(rask));
  const ned = await s.js(`(() => { const f = document.querySelector('${FELT}'); const e = new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true});
    return {ikkeStanset: f.dispatchEvent(e), valgt: f.getAttribute('aria-activedescendant')}; })()`);
  sjekk("pil ned i en liste som er på vei til å bli byttet ut, velger ingenting", ned.ikkeStanset && ned.valgt === null, JSON.stringify(ned));
  await s.gaa(`${BASE}/admin`);                                // samme med ekte tastetrykk
  await s.skriv(FELT, "kari");
  await ventListe(s);
  await NED(s); await NED(s);
  await sett(s, "kari olsen");
  await ENTER(s);
  await ventTil(s, `location.pathname === '/admin/sok'`);
  sjekk("ekte Enter rett etter endret tekst åpner /admin/sok?q=kari+olsen (ikke Kari Hansen)", (await sti(s)) === "/admin/sok?q=kari+olsen", await sti(s));

  // ---- IME: hendelser under sammensetning ignoreres ----
  await s.gaa(`${BASE}/admin`);
  await s.skriv(FELT, "kari");
  await ventListe(s);
  await NED(s);
  const ime = await s.js(`(() => { const f = document.querySelector('${FELT}'); const for_ = f.getAttribute('aria-activedescendant');
    const e1 = new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true, isComposing: true});
    const e2 = new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true, isComposing: true});
    const a = f.dispatchEvent(e1), b = f.dispatchEvent(e2);
    return {a, b, uendret: f.getAttribute('aria-activedescendant') === for_, sti: location.pathname}; })()`);
  sjekk("under IME-sammensetning ignoreres pil ned og Enter", ime.a && ime.b && ime.uendret && ime.sti === "/admin", JSON.stringify(ime));

  // ---- lukking: klikk utenfor, Tab, fokus ----
  await s.klikk("h1");
  sjekk("klikk utenfor lukker listen", await skjult(s));
  await s.js(`document.querySelector('${FELT}').blur(); document.querySelector('${FELT}').focus()`);
  sjekk("fokus i feltet åpner listen igjen (den har treff)", !(await skjult(s)));
  await tast(s, "Tab", "Tab", 9);
  sjekk("Tab lukker listen og flytter fokus til «Søk»-knappen", (await skjult(s)) && (await s.js(`document.activeElement.className`)) === "sok-knapp",
        `skjult=${await skjult(s)} aktivt=${await s.js(`document.activeElement.tagName + '.' + document.activeElement.className + '#' + document.activeElement.id`)}`);
  await s.js(`document.querySelector('${FELT}').focus()`);
  const md = await s.js(`(() => { const e = new MouseEvent('mousedown', {bubbles: true, cancelable: true}); return document.querySelector('${LISTE} a').dispatchEvent(e); })()`);
  sjekk("mousedown i listen holder fokus i feltet (preventDefault)", md === false);
  await s.js(`window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: false}))`);
  sjekk("pageshow uten cache lukker ikke listen", !(await skjult(s)));
  await s.js(`window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))`);
  sjekk("pageshow fra tilbake-knappens cache lukker listen", await skjult(s));
  await s.js(`document.querySelector('${FELT}').focus()`);
  await s.klikk(`${LISTE} a[role=option]`);
  await ventTil(s, `location.pathname === '/admin/sok'`);
  sjekk("klikk på et treff åpner søkesiden med #person-…", /^\/admin\/sok\?q=kari#person-\d+$/.test(await sti(s)), await sti(s));

  // ---- ingen treff, for kort og meldinger ----
  await s.gaa(`${BASE}/admin`);
  await s.skriv(FELT, "qqqqzz");
  await ventTil(s, `document.querySelector('${LISTE} .sok-tom') !== null`);
  sjekk("ingen treff: melding i listen", (await tekst(s, LISTE)).trim() === "Ingen treff");
  sjekk("meldingen ligger ikke i en listbox (aria-required-children) og listen regnes som lukket", (await attr(s, LISTE, "role")) === null &&
        (await attr(s, FELT, "aria-expanded")) === "false");
  sjekk("ingen treff: opplesing i live-regionen", (await tekst(s, "[data-sok-status]")) === "Ingen treff");
  await s.klikk("h1");
  await s.js(`document.querySelector('${FELT}').blur(); document.querySelector('${FELT}').focus()`);
  sjekk("fokus åpner ikke en liste som bare har en melding", await skjult(s));
  await tom(s);
  await s.skriv(FELT, "k h");
  await ventTil(s, `document.querySelector('${LISTE} .sok-tom') !== null`);
  sjekk("bare enkeltbokstaver: melding om å skrive minst ett ord med to tegn", (await tekst(s, LISTE)).includes("Skriv minst ett ord med to tegn"));
  await tom(s);
  await s.skriv(FELT, "kari hansen");
  await ventListe(s);
  sjekk("etter en melding er listen en listbox igjen", (await attr(s, LISTE, "role")) === "listbox" && (await attr(s, FELT, "aria-expanded")) === "true");
  const lukket = await s.js(`(() => { const f = document.querySelector('${FELT}'); f.value = 'k'; f.dispatchEvent(new Event('input', {bubbles: true}));
    return document.querySelector('${LISTE}').hidden; })()`);
  sjekk("når teksten kortes ned til ett tegn, lukkes listen med en gang", lukket === true);

  // ---- HTML i navn er tekst ----
  await tom(s);
  await s.skriv(FELT, "xena");
  await ventListe(s);
  sjekk("HTML i navn vises som tekst i listen (ingen <i>- eller <script>-element)", await s.js(`(() => { const l = document.querySelector('${LISTE}');
    return l.innerText.includes('<i>Kursiv</i>') && !l.querySelector('i') && !l.querySelector('script'); })()`));

  // ---- feilmeldinger (fetch byttes ut) ----
  const svar = (status, kropp) => `window.fetch = () => Promise.resolve(new Response(${JSON.stringify(kropp)}, {status: ${status}}))`;
  for (const [status, kropp, tekstbit, navn] of [[401, '{"feil":"ikke_innlogget"}', "Du er logget ut", "401 (utlogget)"],
       [429, '{"feil":"for_mange_sok"}', "For mange søk", "429 (for mange søk)"],
       [400, "<html>", "Siden har stått åpen for lenge", "400 (ugyldig CSRF-token)"],
       [500, "<html>", "Kunne ikke søke akkurat nå", "500 (serverfeil)"]]) {
    await tom(s);
    await s.js(svar(status, kropp));
    await s.skriv(FELT, "kari");
    await ventTil(s, `document.querySelector('${LISTE} .sok-tom') !== null`);
    sjekk(`${navn}: melding «${tekstbit}»`, (await tekst(s, LISTE)).includes(tekstbit), await tekst(s, LISTE));
  }
  await tom(s);
  await s.js(`window.fetch = () => Promise.reject(new TypeError('nett'))`);
  await s.skriv(FELT, "kari");
  await ventTil(s, `document.querySelector('${LISTE} .sok-tom') !== null`);
  sjekk("nettverksfeil: melding om å trykke Enter for å søke likevel", (await tekst(s, LISTE)).includes("Trykk Enter for å søke likevel"), await tekst(s, LISTE));

  // ---- adressevakt: treff med adresser utenfor /admin/ tegnes aldri ----
  await tom(s);
  await s.js(`window.fetch = () => Promise.resolve(new Response(JSON.stringify({totalt: 1, for_kort: false, alle_url: "//evil.example/y", treff: [{id: 1,
    navn_deler: [["Ond", false]], epost_deler: [["x", false]], grunn: "g", antall: 0, siste_kurs: "", url: "https://evil.example/x"}]}), {status: 200}))`);
  await s.skriv(FELT, "kari");
  await ventTil(s, `document.querySelector('${LISTE} .sok-tom') !== null`);
  sjekk("adresser utenfor /admin/ blir ikke lenker i listen", await s.js(`!document.querySelector('${LISTE} a[href*=evil]') && !document.querySelector('${LISTE} a')`));
  await tom(s);
  await s.js(`window.fetch = () => Promise.resolve(new Response(JSON.stringify({totalt: 1, for_kort: false, alle_url: "https://evil.example/y", treff: [{id: 1,
    navn_deler: [["Snill", false]], epost_deler: [["s", false]], grunn: "g", antall: 0, siste_kurs: "", url: "/admin/sok?q=snill#person-1"}]}), {status: 200}))`);
  await s.skriv(FELT, "kari");
  await ventListe(s);
  sjekk("«åpne»-adresse utenfor /admin/ gir ingen «åpne»-rad, men treffet vises", (await valgAntall(s)) === 1 &&
        await s.js(`!document.querySelector('${LISTE} a[href*=evil]') && !document.querySelector('${LISTE} #sok-deltaker-alle')`));

  // ---- nyeste svar vinner: et sent svar på en eldre tekst overskriver ikke ----
  await s.gaa(`${BASE}/admin`);
  await s.js(`(() => { const orig = window.fetch; window.fetch = function (url, valg) { const q = JSON.parse(valg.body).q;
    return orig.call(window, url, Object.assign({}, valg, {signal: undefined})).then((r) => new Promise((ok) => setTimeout(() => ok(r), q === 'ka' ? 900 : 0))); }; })()`);
  await s.skriv(FELT, "ka");
  await vent(400);                                             // «ka» er sendt (svaret er forsinket) ...
  await s.skriv(FELT, "ri hansen");                            // ... og så «kari hansen» (raskt)
  await ventListe(s, 3000);
  await vent(1200);                                            // det sene svaret på «ka» er kommet
  const l = await tekst(s, LISTE);
  sjekk("nyeste søk vises, ikke det sene svaret på en eldre tekst", l.includes("Kari Hansen") && !l.includes("Kari Fyll"), l.slice(0, 90).replace(/\n/g, " | "));
}

async function kurssok(s) {
  await s.gaa(`${BASE}/admin`);
  sjekk("standardlisten skjuler avsluttede kurs", await s.js(`document.body.innerText.includes('Skjematerapi') && !document.body.innerText.includes('Emosjonsfokusert terapi')`));
  await s.skriv("#sok-kurs", "emosjon");
  await ENTER(s);                                                  // Enter i kurssøket sender filterskjemaet (feltet hører til det via form=)
  await ventTil(s, `location.search.includes('sok=emosjon')`);
  sjekk("Enter i kurssøket sender filterskjemaet og hopper til kurslisten", (await sti(s)).includes("sok=emosjon") && (await sti(s)).endsWith("#kurs"), await sti(s));
  sjekk("søket finner også avsluttede kurs og feltet beholder teksten", await s.js(`document.body.innerText.includes('Emosjonsfokusert terapi') && document.querySelector('#sok-kurs').value === 'emosjon'`));
  sjekk("statusvalget viser at søket gjelder alle kurs", (await s.js(`document.querySelector('#f-status').value`)) === "alle");
  await s.js(`(() => { const v = document.querySelector('#f-antall'); v.value = '10'; v.dispatchEvent(new Event('change', {bubbles: true})); })()`);
  await ventTil(s, `location.search.includes('antall=10')`);
  sjekk("filtervalgene sender skjemaet av seg selv og beholder søket", (await sti(s)).includes("antall=10") && (await sti(s)).includes("sok=emosjon"), await sti(s));
  await s.gaa(`${BASE}/admin`);
  await s.skriv("#sok-kurs", "skjema");
  await s.klikk(".sokepanel button[form=kursfilter]");
  await ventTil(s, `location.search.includes('sok=skjema')`);
  sjekk("«Søk»-knappen ved kurssøket virker også", await s.js(`document.body.innerText.includes('Skjematerapi') && !document.body.innerText.includes('Veiledning i grupper')`));
}

async function hurtigtast(s) {
  // «/» fra en side uten fokus i noe felt: deltakersøket på siden (Oversikten, søkesiden), ellers går man til Oversikten
  await s.gaa(`${BASE}/admin`);
  await s.js(`document.activeElement.blur()`);
  await tast(s, "/", "Slash", 191, "/");
  sjekk("«/» på Oversikten hopper til deltakersøket", (await s.js(`document.activeElement.id`)) === "sok-deltaker" && (await s.js(`document.activeElement.value`)) === "");
  await s.gaa(`${BASE}/admin/rapporter`);
  sjekk("adminsidene har ikke søkefelt i menyen, men adressen «/» går til", (await s.js(`!document.querySelector('header input')`)) &&
        (await attr(s, "header", "data-sok-adresse")) === "/admin#sok-deltaker");
  await s.js(`document.activeElement.blur()`);
  await tast(s, "/", "Slash", 191, "/");
  await ventTil(s, `location.pathname === '/admin' && document.activeElement.id === 'sok-deltaker'`);
  sjekk("«/» på en side uten deltakersøk går til Oversikten med markøren i søkefeltet (og skriver ikke «/»)", (await sti(s)) === "/admin#sok-deltaker" &&
        (await s.js(`document.activeElement.id`)) === "sok-deltaker" && (await s.js(`document.activeElement.value`)) === "", await sti(s));
  await s.gaa(`${BASE}/admin/rapporter`);
  await s.gaa(`${BASE}/admin#sok-deltaker`);
  sjekk("adressen #sok-deltaker setter markøren i deltakersøket", (await s.js(`document.activeElement.id`)) === "sok-deltaker");
  await s.js(`document.activeElement.blur()`);
  await tast(s, "/", "Slash", 191, "/", 2);
  sjekk("Ctrl + «/» kapres ikke", (await s.js(`document.activeElement.tagName`)) === "BODY");
  await tast(s, "/", "Slash", 191, "/", 1);
  sjekk("Alt + «/» kapres ikke", (await s.js(`document.activeElement.tagName`)) === "BODY");
  const ime = await s.js(`(() => { document.activeElement.blur(); const e = new KeyboardEvent('keydown', {key: '/', bubbles: true, cancelable: true, isComposing: true});
    document.body.dispatchEvent(e); return document.activeElement.tagName; })()`);
  sjekk("«/» under IME-sammensetning kapres ikke", ime === "BODY");
  const dialog = await s.js(`(() => { const d = document.createElement('dialog'); d.id = 'prove'; d.setAttribute('open', ''); document.body.appendChild(d);
    document.activeElement.blur(); return true; })()`);
  await tast(s, "/", "Slash", 191, "/");
  sjekk("«/» kapres ikke mens et vindu (dialog) er åpent", dialog && (await s.js(`document.activeElement.tagName`)) === "BODY");
  await s.js(`document.getElementById('prove').remove()`);
  await s.gaa(`${BASE}/admin/kurs/1/deltakere`);
  await s.js(`document.querySelector('input[name=sok]').focus()`);
  await tast(s, "/", "Slash", 191, "/");
  sjekk("«/» i et annet tekstfelt skrives som vanlig tegn og hopper ikke", (await s.js(`document.activeElement.name`)) === "sok" && (await s.js(`document.activeElement.value`)) === "/");
  await s.gaa(`${BASE}/admin/sok?q=kari`);
  await s.js(`document.activeElement.blur()`);
  await tast(s, "/", "Slash", 191, "/");
  sjekk("«/» på søkesiden hopper til søkefeltet der", (await s.js(`document.activeElement.id`)) === "sok-side");
}

async function resultatside(s) {
  await s.gaa(`${BASE}/admin/sok`);
  sjekk("søkesiden uten søk har fokus i det store feltet", (await s.js(`document.activeElement.id`)) === "sok-side");
  await s.gaa(`${BASE}/admin/sok?q=kari+hansen`);
  const t = await s.js(`document.body.innerText`);
  sjekk("resultatsiden viser Kari Hansen med utstedt kursbevis", t.includes("Kari Hansen") && t.includes("Utstedt 03.03.2027"));
  sjekk("kursbeviset har lenke som åpnes i ny fane", await s.js(`(() => { const a = document.querySelector('a[href^="/dokument/"]'); return !!a && a.target === '_blank' && a.rel.includes('noopener') && a.innerText === 'Åpne kursbeviset'; })()`));
  sjekk("kursbevis-pillene har lesbar kontrast (minst 4,5:1, målt i nettleseren)", await s.js(`(() => {
    const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    const lum = (rgb) => { const m = rgb.match(/\\d+/g).map(Number); return 0.2126 * lin(m[0]) + 0.7152 * lin(m[1]) + 0.0722 * lin(m[2]); };
    const piller = [...document.querySelectorAll('.merke.brytes')];
    return piller.length >= 3 && piller.every((p) => { const cs = getComputedStyle(p); const a = lum(cs.color) + 0.05, b = lum(cs.backgroundColor) + 0.05;
      return Math.max(a, b) / Math.min(a, b) >= 4.5; }); })()`));
  // Referer: klikk videre fra søkesiden skal ikke gi målsiden adressen med søketeksten
  await s.gaa(`${BASE}/admin/sok?q=${encodeURIComponent("kari.hansen@eksempel.no")}`);
  sjekk("søkesiden har Referrer-Policy no-referrer", (await s.js(`fetch(location.href).then(r => r.headers.get('Referrer-Policy'))`)) === "no-referrer");
  await s.klikk("a[href^='/admin/rapporter/deltaker/']");
  await ventTil(s, `location.pathname.startsWith('/admin/rapporter/deltaker/')`);
  sjekk("etter klikk fra søkesiden er document.referrer tom (søketeksten følger ikke med)", (await s.js(`document.referrer`)) === "", await s.js(`document.referrer`));
  // veien tilbake til søket er nettleserens tilbake-knapp: resultatet kommer igjen som det var
  await s.js(`history.back()`);
  await ventTil(s, `location.pathname === '/admin/sok'`);
  sjekk("tilbake-knappen fra personsiden gir søkeresultatet igjen", (await s.js(`location.search`)) === "?q=kari.hansen%40eksempel.no" &&
        (await s.js(`document.body.innerText`)).includes("Kari Hansen") && (await s.js(`document.querySelector('input#sok-side').value`)) === "kari.hansen@eksempel.no");
}

async function bredder(s) {
  await s.storrelse(390, 844, true);
  for (const url of ["/admin", "/admin/sok", "/admin/sok?q=kari+hansen"]) {
    await s.gaa(`${BASE}${url}`);
    sjekk(`ingen vannrett rulling på ${url} (390 px)`, await s.js(`document.documentElement.scrollWidth <= window.innerWidth`), await s.js(`document.documentElement.scrollWidth + ' > ' + window.innerWidth`));
  }
  await s.gaa(`${BASE}/admin`);
  sjekk("deltakersøk og kurssøk stables under hverandre på mobil (390 px)", await s.js(`(() => { const a = document.querySelector('#sok-deltaker').getBoundingClientRect(),
    b = document.querySelector('#sok-kurs').getBoundingClientRect(); return b.top > a.bottom && Math.abs(a.left - b.left) < 4; })()`));
  await s.skriv(FELT, "kari");
  await ventListe(s);
  sjekk("rullegardinen ligger innenfor skjermen (390 px)", await s.js(`(() => { const r = document.querySelector('${LISTE}').getBoundingClientRect(); return r.left >= 0 && r.right <= window.innerWidth; })()`));
  await s.storrelse(1440, 900);
}

(async () => {
  const s = await start(DEBUGPORT);
  try {
    await s.storrelse(1440, 900);
    await logginn(s);
    await rullegardin(s);
    await kurssok(s);
    await hurtigtast(s);
    await resultatside(s);
    await bredder(s);
    await s.gaa(`${BASE}/logg-inn`);
    sjekk("offentlig side har ikke søkefeltet", await s.js(`!document.querySelector('input[name=q]') && !document.querySelector('form[data-deltakersok]')`));
    await s.gaa(`${BASE}/`);
    sjekk("startsiden sender innlogget administrator til Oversikten", (await sti(s)) === "/admin", await sti(s));
    await s.gaa(`${BASE}/admin/logg-ut`);
    await s.gaa(`${BASE}/`);
    sjekk("startsiden er innloggingen til Admin, uten søkefelt og uten meny", (await s.js(`document.querySelector('h1').innerText`)) === "Logg inn til Admin" &&
          (await s.js(`!document.querySelector('input[name=q]') && !document.querySelector('header nav a')`)), await sti(s));
    sjekk("ingen JavaScript-feil fra sidene våre", s.konsoll.length === 0, s.konsoll.join(" | "));
  } catch (e) {
    sjekk("scenario fullført", false, String((e && e.stack) || e));
  } finally {
    await s.lukk();
    console.log("RESULTAT_JSON:" + JSON.stringify(resultat));
    process.exit(0);
  }
})();
