/* Kursside i administrasjonen: redigeringsvisningen. KJERNEN (tilstand, lagring, angre, kladd, økt, status, forhåndsvisning).
   Filene som hører sammen (lastes i denne rekkefølgen, alle med defer og nonce fra kursside_admin.html):
     kursside-admin.js           kjernen: tilstand S, hjelpere, nettverk, autolagring, angre/gjør om, lokal kladd, hjerteslag, status, forhåndsvisning
     kursside-admin-liste.js     blokklisten: kort, flytting (dra, piltaster, meny), rask omorganisering, legg til, dupliser, slett
     kursside-admin-blokker.js   innholdet i hver blokktype (tekst med rik tekst, viktig, program, filer, lenker, tabell, kontakt, bilde)
     kursside-admin-filer.js     opplasting (rå kropp, fremdrift, nedskalering av bilder) og filradene
     kursside-admin-dialoger.js  publisering, konflikt, versjoner, hent fra annet kurs, mal, finn og erstatt, innstillinger, lenke, tekstmodus
   Alt henger på ett felles objekt, window.KS. Ingen tekst fra databasen står i disse filene: dataene ligger som JSON i #ks-data, og fast tekst
   og skjelett står i malene (kursside_admin.html og _kursside_admin_*.html).
   REGLER (testes med regex i tests/test_kursside_admin.py): ingen av DOM-API-ene som setter inn HTML fra tekst (tilordning til innerHTML og lignende),
   ingen skriving direkte til dokumentstrømmen, ingen kode fra tekst (evaluering, Function-konstruktøren, tidsavbrudd med tekst). DOM bygges med createElement, textContent,
   cloneNode og replaceChildren; rik tekst leses ut ved å gå gjennom DOM-en (KS.rtTilHtml), aldri ved å lese innerHTML.
   VALG FOR RIK TEKST (SPEC §6.10, «Plan B»): contenteditable + document.execCommand er beholdt (prøvd i Edge/Chrome; Firefox og Safari er
   ikke prøvd av byggerne). Alt som limes inn og alt som leses ut går gjennom en egen filtrering til p, br, strong, em, ul, ol, li og a[href]
   (KS.renHtml), og serveren renser på nytt ved lagring og ved visning. Skulle execCommand vise seg ustabil, byttes bare KS.byggTekstFelt ut med
   et tekstfelt + enkel Markdown som gir samme lagrede HTML (data.html); serverkontrakten er uendret. */
(function () {
  "use strict";
  var rot = document.getElementById("ks-rot");
  var dataEl = document.getElementById("ks-data");
  if (!rot || !dataEl) { return; }
  var KS = window.KS = { rot: rot, data: JSON.parse(dataEl.textContent), init: [] };
  var D = KS.data;
  KS.skriv = rot.getAttribute("data-skrivebeskyttet") !== "1";
  var metaTag = document.querySelector('meta[name="csrf-token"]');
  KS.csrf = metaTag ? metaTag.getAttribute("content") : "";
  var SVG = "http://www.w3.org/2000/svg";

  /* ================= hjelpere ================= */
  KS.$ = function (sel, ctx) { return (ctx || rot).querySelector(sel); };
  KS.$$ = function (sel, ctx) { return Array.prototype.slice.call((ctx || rot).querySelectorAll(sel)); };
  KS.ks = function (navn, ctx) { return (ctx || rot).querySelector('[data-ks="' + navn + '"]'); };
  KS.felt = function (navn, ctx) { return ctx.querySelector('[data-felt="' + navn + '"]'); };
  KS.tom = function (node) { while (node.firstChild) { node.removeChild(node.firstChild); } return node; };
  KS.el = function (tag, opt) {
    var e = document.createElement(tag);
    opt = opt || {};
    if (opt.klasse) { e.className = opt.klasse; }
    if (opt.tekst !== undefined && opt.tekst !== null) { e.textContent = opt.tekst; }
    if (opt.attr) { Object.keys(opt.attr).forEach(function (k) { e.setAttribute(k, opt.attr[k]); }); }
    (opt.barn || []).forEach(function (b) { if (b) { e.appendChild(typeof b === "string" ? document.createTextNode(b) : b); } });
    return e;
  };
  KS.ikon = function (navn, klasse) {
    var s = document.createElementNS(SVG, "svg");
    s.setAttribute("class", "ik" + (klasse ? " " + klasse : ""));
    s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false");
    var u = document.createElementNS(SVG, "use"); u.setAttribute("href", "#i-" + navn); s.appendChild(u);
    return s;
  };
  KS.klon = function (id) {
    var t = document.getElementById(id);
    return document.importNode(t.content.firstElementChild, true);
  };
  KS.kopi = function (x) { return JSON.parse(JSON.stringify(x)); };
  KS.utsett = function (fn, ms) {
    var t = null;
    var f = function () { clearTimeout(t); t = setTimeout(function () { t = null; fn(); }, ms); };
    f.avbryt = function () { clearTimeout(t); t = null; };
    f.flush = function () { if (t !== null) { clearTimeout(t); t = null; fn(); } };
    return f;
  };
  var TEGN = "abcdefghijklmnopqrstuvwxyz0123456789";
  KS.nyId = function () {
    var id;
    do {
      var s = "b_";
      var r = new Uint8Array(8);
      if (window.crypto && window.crypto.getRandomValues) { window.crypto.getRandomValues(r); } else { for (var i = 0; i < 8; i++) { r[i] = Math.floor(Math.random() * 256); } }
      for (var j = 0; j < 8; j++) { s += TEGN.charAt(r[j] % TEGN.length); }
      id = s;
    } while (S && S.dok && S.dok.blokker.some(function (b) { return b.id === id; }));
    return id;
  };
  KS.datoLang = function (iso) { return iso && /^\d{4}-\d{2}-\d{2}$/.test(iso) ? iso.slice(8, 10) + "." + iso.slice(5, 7) + "." + iso.slice(0, 4) : ""; };
  KS.datoKort = function (iso) { return iso && /^\d{4}-\d{2}-\d{2}$/.test(iso) ? iso.slice(8, 10) + "." + iso.slice(5, 7) : ""; };
  var UKEDAGER = ["søndag", "mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag"];
  var MAANEDER = ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "desember"];
  KS.datoTekst = function (iso) {
    if (!iso || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) { return ""; }
    var d = new Date(iso.slice(0, 4), parseInt(iso.slice(5, 7), 10) - 1, parseInt(iso.slice(8, 10), 10));
    return UKEDAGER[d.getDay()] + " " + d.getDate() + ". " + MAANEDER[d.getMonth()];
  };
  KS.idagIso = function () {
    var n = new Date();
    return n.getFullYear() + "-" + ("0" + (n.getMonth() + 1)).slice(-2) + "-" + ("0" + n.getDate()).slice(-2);
  };
  /* UTC-tidspunkt fra databasen («2026-11-27 07:55:00») -> {dato: «27.11.2026», tid: «08:55», idag: true/false} i norsk tid */
  KS.norskTid = function (utc) {
    if (!utc) { return null; }
    try {
      var d = new Date(utc.replace(" ", "T") + "Z");
      if (isNaN(d.getTime())) { return null; }
      var f = new Intl.DateTimeFormat("nb-NO", { timeZone: "Europe/Oslo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
      var p = {};
      f.formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
      var iso = p.year + "-" + p.month + "-" + p.day;
      var idag = new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Oslo" }).format(new Date());
      return { dato: p.day + "." + p.month + "." + p.year, tid: p.hour + ":" + p.minute, iso: iso, idag: iso === idag };
    } catch (e) { return null; }
  };
  KS.storrelse = function (byte) {
    if (byte >= 1048576) { return (byte / 1048576).toFixed(1).replace(".", ",") + " MB"; }
    return Math.max(1, Math.round(byte / 1024)) + " kB";
  };
  KS.flertall = function (n, entall, flertall) { return n + " " + (n === 1 ? entall : flertall); };
  KS.blokkNavn = { tekst: "Tekst", viktig: "Viktig", program: "Program", filer: "Filer", lenker: "Lenker", tabell: "Tabell", kontakt: "Kontakt", bilde: "Bilde" };
  KS.blokkIkon = { tekst: "tekst", viktig: "info", program: "kal", filer: "fil", lenker: "lenke", tabell: "gruppe", kontakt: "bruker", bilde: "bilde" };
  KS.standardTittel = { tekst: "Ny tekst", viktig: "Viktig", program: "Program", filer: "Presentasjoner og dokumenter", lenker: "Litteratur og lenker", tabell: "Gruppeinndeling", kontakt: "Kontakt", bilde: "Bilde" };

  /* Lik lenkeregel som serveren (sideinnhold.trygg_url): https, http, mailto, tel; en adresse uten skjema som ser ut som et domene får https:// */
  KS.trygUrl = function (tekst) {
    var t = (tekst || "").trim();
    if (!t || t.length > 2000 || /[\s\u0000-\u001f\u007f]/.test(t)) { return null; }
    var m = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(t);
    if (m) { return /^(https|http|mailto|tel)$/i.test(m[1]) && t.length > m[0].length ? t : null; }
    return /^[\w.-]+\.[a-zA-Z]{2,}(\/\S*)?$/.test(t) ? "https://" + t : null;
  };

  /* ================= tilstand ================= */
  var S = KS.S = {
    dok: null, versjon: D.versjon, publisert: D.publisert, publisertVersjon: D.publisert_versjon, aktiv: D.aktiv, innst: D.innstillinger,
    filer: D.filer.slice(), kvote: D.kvote, versjoner: D.versjoner, kilder: null,
    ulagret: false, lagrer: false, igjen: false, lofte: null, sistLagret: null, sistLagretJson: "", serverEndringer: null, merknader: [],
    stack: [], stackIdx: -1, apen: new Set(), valgt: new Set(), rask: false, sisteId: null, opplastinger: [], konflikt: null
  };
  function normaliserDok(dok) {
    dok = dok && typeof dok === "object" ? dok : {};
    dok.v = 1; dok.tittel = dok.tittel || ""; dok.ingress = dok.ingress || ""; dok.rom = dok.rom || ""; dok.godkjent = dok.godkjent || "";
    dok.melding = dok.melding && typeof dok.melding === "object" ? dok.melding : {};
    dok.melding.tekst = dok.melding.tekst || ""; dok.melding.niva = dok.melding.niva === "viktig" ? "viktig" : "info";
    dok.blokker = Array.isArray(dok.blokker) ? dok.blokker : [];
    dok.blokker.forEach(function (b) {
      b.tittel = b.tittel || ""; b.skjult = !!b.skjult; b.vis_fra = b.vis_fra || null; b.i_meny = b.i_meny !== false; b.data = b.data || {};
    });
    return dok;
  }
  KS.normaliserDok = normaliserDok;
  S.dok = normaliserDok(KS.kopi(D.dokument));
  KS.finnBlokk = function (id) { return S.dok.blokker.filter(function (b) { return b.id === id; })[0] || null; };
  KS.blokkIndeks = function (id) { return S.dok.blokker.map(function (b) { return b.id; }).indexOf(id); };

  /* ================= melding til skjermlesere, toast, bannere ================= */
  var live = document.getElementById("ks-live");
  KS.si = function (tekst) {
    if (!live) { return; }
    live.textContent = "";
    setTimeout(function () { live.textContent = tekst; }, 40);
  };
  KS.toast = function (tekst, opt) {
    opt = opt || {};
    var omr = KS.ks("toast-omrade");
    var t = KS.el("div", { klasse: "ks-toast" + (opt.niva ? " " + opt.niva : ""), attr: { role: opt.niva === "feil" ? "alert" : "status", "data-ks": "toast" }, barn: [KS.el("span", { tekst: tekst })] });
    var timer = null;
    function fjern() { clearTimeout(timer); if (t.parentNode) { t.parentNode.removeChild(t); } }
    if (opt.angre) {
      var b = KS.el("button", { tekst: opt.angreTekst || "Angre", attr: { type: "button" } });
      b.addEventListener("click", function () { fjern(); opt.angre(); });
      t.appendChild(b);
    }
    omr.appendChild(t);
    while (omr.children.length > 3) { omr.removeChild(omr.firstChild); }
    timer = setTimeout(fjern, opt.varighet || (opt.angre ? 8000 : (opt.niva === "feil" ? 9000 : 4500)));
    KS.si(tekst);
    return { fjern: fjern };
  };
  var bannereEl = document.getElementById("ks-bannere");
  /* Et banner øverst med fast id (samme id erstatter forrige). opt: niva (info|gul|feil|ok), knapper [{tekst, fn}], lenke {tekst, href}, liste [tekst], alarm */
  KS.banner = function (id, tekst, opt) {
    opt = opt || {};
    KS.fjernBanner(id);
    var b = KS.el("div", { klasse: "ks-banner ks-banner-" + (opt.niva || "info"), attr: { "data-banner": id, role: opt.alarm ? "alert" : "status" } });
    var span = KS.el("span");
    if (opt.fet) { span.appendChild(KS.el("strong", { tekst: opt.fet })); span.appendChild(document.createTextNode(" ")); }
    span.appendChild(document.createTextNode(tekst));
    if (opt.liste && opt.liste.length) { span.appendChild(KS.el("ul", { barn: opt.liste.map(function (t) { return KS.el("li", { tekst: t }); }) })); }
    b.appendChild(span);
    if (opt.lenke) { b.appendChild(KS.el("a", { tekst: opt.lenke.tekst, attr: { href: opt.lenke.href, target: "_blank", rel: "noopener" } })); }
    (opt.knapper || []).forEach(function (k) {
      var kn = KS.el("button", { tekst: k.tekst, klasse: k.sekundar === false ? "" : "sekundar", attr: { type: "button" } });
      kn.addEventListener("click", function () { k.fn(b); });
      b.appendChild(kn);
    });
    bannereEl.appendChild(b);
    return b;
  };
  KS.fjernBanner = function (id) {
    var e = bannereEl.querySelector('[data-banner="' + id + '"]');
    if (e) { e.parentNode.removeChild(e); }
  };

  /* ================= nettverk ================= */
  /* fetch som skiller JSON-svar fra alt annet (viderekobling til innlogging, feilsider): alt som ikke er JSON regnes som «logget ut». */
  KS.kall = function (url, metode, kropp, hoder) {
    var opp = { method: metode, credentials: "same-origin", headers: Object.assign({ "X-CSRF-Token": KS.csrf }, hoder || {}) };
    if (kropp !== undefined) { opp.body = kropp; }
    return fetch(url, opp).then(function (r) {
      var type = r.headers.get("Content-Type") || "";
      if (r.redirected || type.indexOf("json") < 0) { return { utenJson: true, status: r.status }; }
      return r.json().then(function (j) { return { status: r.status, json: j }; }, function () { return { utenJson: true, status: r.status }; });
    });
  };
  KS.jsonKall = function (url, kropp) { return KS.kall(url, "POST", JSON.stringify(kropp), { "Content-Type": "application/json" }); };
  KS.hent = function (url) { return KS.kall(url, "GET"); };
  KS.loggetUt = function () {
    S.utlogget = true;
    skrivKladd();
    visLagretStatus("Ikke lagret – du er logget ut", true);
    KS.banner("utlogget", "Åpne innloggingen i en ny fane og logg inn. Utkastet ditt er tatt vare på i denne nettleseren.", {
      niva: "feil", alarm: true, fet: "Ikke lagret – du er logget ut.", lenke: { tekst: "Åpne innlogging", href: D.urls.logg_inn },
      knapper: [{ tekst: "Prøv å lagre igjen", fn: proevIgjen, sekundar: false }]
    });
  };
  function proevIgjen() {
    KS.hent(D.urls.token).then(function (r) {
      if (r.utenJson || !r.json || !r.json.csrf) { KS.toast("Du er fortsatt logget ut. Logg inn i den andre fanen først.", { niva: "feil" }); return; }
      KS.csrf = r.json.csrf;
      if (metaTag) { metaTag.setAttribute("content", r.json.csrf); }
      S.utlogget = false; KS.fjernBanner("utlogget");
      S.ulagret = true; KS.lagre();
    });
  }
  KS.feilTekst = function (r, standard) {
    if (r.json && r.json.melding) { return r.json.melding; }
    if (r.status === 413) { return "Det du prøvde å lagre er for stort."; }
    if (r.status === 429) { return "For mange forsøk. Vent noen minutter og prøv igjen."; }
    return standard || "Noe gikk galt. Prøv igjen.";
  };

  /* ================= endringer, angre og gjør om ================= */
  var snapTimer = KS.utsett(function () { KS.snapshot(); }, 800);
  KS.snapshot = function () {
    snapTimer.avbryt();
    var j = JSON.stringify(S.dok);
    if (S.stack[S.stackIdx] === j) { return; }
    S.stack.length = S.stackIdx + 1;
    S.stack.push(j);
    if (S.stack.length > 50) { S.stack.shift(); }
    S.stackIdx = S.stack.length - 1;
    oppdaterAngreKnapper();
  };
  function oppdaterAngreKnapper() {
    var a = KS.ks("angre"), g = KS.ks("gjor-om");
    if (a) { a.disabled = !(S.stackIdx > 0); }
    if (g) { g.disabled = !(S.stackIdx < S.stack.length - 1); }
  }
  /* Kalles etter hver endring. opt.struktur: legg til/slett/flytt/dupliser/mal/hent (snapshot med en gang), ellers etter en tastepause. opt.blokkId: blokken du jobber i. */
  KS.endret = function (opt) {
    opt = opt || {};
    if (!KS.skriv) { return; }
    S.ulagret = true;
    if (opt.blokkId) { S.sisteId = opt.blokkId; }
    if (opt.struktur) { KS.snapshot(); } else { snapTimer(); var a = KS.ks("angre"); if (a) { a.disabled = false; } }
    planleggKladd();
    KS.planleggLagring();
    utsattStatus();
    visLagretStatus("Ikke lagret ennå");
  };
  var utsattStatus = KS.utsett(function () { KS.oppdaterStatus(); }, 250);       // beregner endringene mot publisert versjon (tungt med mange blokker)
  function gjenopprettFra(json) {
    S.dok = normaliserDok(JSON.parse(json));
    S.ulagret = true;
    KS.tegnAlt();
    planleggKladd(); KS.planleggLagring(); KS.oppdaterStatus();
    visLagretStatus("Ikke lagret ennå");
  }
  KS.angre = function () {
    KS.snapshot();
    if (S.stackIdx <= 0) { return false; }
    S.stackIdx--; gjenopprettFra(S.stack[S.stackIdx]); oppdaterAngreKnapper(); KS.si("Angret");
    return true;
  };
  KS.gjorOm = function () {
    snapTimer.flush();
    if (S.stackIdx >= S.stack.length - 1) { return false; }
    S.stackIdx++; gjenopprettFra(S.stack[S.stackIdx]); oppdaterAngreKnapper(); KS.si("Gjort om");
    return true;
  };
  /* Erstatter hele dokumentet (mal, hent fra annet kurs, gjenoppretting, «hent deres versjon») med serverens svar; versjonen er allerede lagret på serveren. */
  KS.settDokument = function (dok, versjon, lagret) {
    S.dok = normaliserDok(KS.kopi(dok));
    if (versjon !== undefined) { S.versjon = versjon; }
    S.apen = new Set(); S.valgt = new Set();
    S.ulagret = lagret === false;
    KS.snapshot();
    KS.tegnAlt();
    if (!S.ulagret) { S.sistLagretJson = JSON.stringify(S.dok); KS.slettKladd(); visLagretStatus(null); KS.planleggForhandsvisning(); }
    KS.oppdaterStatus();
  };

  /* ================= lagring ================= */
  var lagreTimer = null, forsteUlagret = 0;
  KS.planleggLagring = function (ms) {
    if (!KS.skriv || S.utlogget) { return; }
    if (!forsteUlagret) { forsteUlagret = Date.now(); }
    clearTimeout(lagreTimer);
    var ventetid = ms === undefined ? 4000 : ms;
    if (Date.now() - forsteUlagret > 26000) { ventetid = 0; }
    lagreTimer = setTimeout(function () { KS.lagre(); }, ventetid);
  };
  function visLagretStatus(tekst, feil) {
    var e = KS.ks("lagre-status");
    if (!e) { return; }
    KS.tom(e);
    e.classList.toggle("feil", !!feil);
    if (tekst === null) {
      e.appendChild(KS.ikon("hake"));
      e.appendChild(document.createTextNode(S.sistLagret ? "Lagret " + S.sistLagret : "Lagret"));
    } else if (tekst) { e.appendChild(document.createTextNode(tekst)); }
  }
  KS.visLagretStatus = visLagretStatus;
  /* Lagrer utkastet (hele dokumentet) med versjonskontroll. Ett kall om gangen; endres siden underveis, sendes den nyeste tilstanden rett etterpå. */
  KS.lagre = function (tvungen) {
    clearTimeout(lagreTimer);
    if (!KS.skriv) { return Promise.resolve(true); }
    if (S.konflikt) { return Promise.resolve(false); }                       // pause til konflikten er løst (dialogen «Siden er lagret av noen andre»)
    if (S.lagrer) { S.igjen = true; return S.lofte; }
    if (!S.ulagret && !(tvungen && S.versjon === 0)) { return Promise.resolve(true); }
    snapTimer.flush();
    var sendt = JSON.stringify(S.dok);
    S.lagrer = true; S.igjen = false;
    visLagretStatus("Lagrer …");
    S.lofte = KS.jsonKall(D.urls.lagre, { versjon: S.versjon, dokument: JSON.parse(sendt) }).then(function (r) {
      S.lagrer = false;
      if (r.utenJson && r.status !== 413) { KS.loggetUt(); return false; }
      if (r.utenJson || r.status === 413) { visLagretStatus("Ikke lagret – siden er for stor", true); KS.banner("lagrefeil", "Siden er for stor til å lagres. Fjern noe innhold.", { niva: "feil", alarm: true }); return false; }
      var j = r.json || {};
      if (r.status === 200) {
        forsteUlagret = 0;
        KS.fjernBanner("utlogget"); KS.fjernBanner("lagrefeil"); S.utlogget = false;
        S.versjon = j.versjon; S.sistLagret = j.endret; S.serverEndringer = j.endringer === undefined ? null : j.endringer;
        S.sistLagretJson = sendt;
        S.ulagret = JSON.stringify(S.dok) !== sendt;
        KS.visMerknader(j.merknader || []);
        if (S.ulagret) { visLagretStatus("Ikke lagret ennå"); KS.planleggLagring(S.igjen ? 0 : 1500); } else { KS.slettKladd(); visLagretStatus(null); }
        KS.oppdaterStatus(); KS.planleggForhandsvisning();
        return true;
      }
      if (r.status === 409 && j.status === "konflikt") { visLagretStatus("Ikke lagret – konflikt", true); KS.visKonflikt(j, JSON.parse(sendt)); return false; }
      visLagretStatus("Ikke lagret", true);
      KS.banner("lagrefeil", KS.feilTekst(r, "Kunne ikke lagre."), { niva: "feil", alarm: true, fet: "Ikke lagret.", knapper: [{ tekst: "Prøv igjen", fn: function () { KS.fjernBanner("lagrefeil"); KS.lagre(); }, sekundar: false }] });
      return false;
    }, function () {
      S.lagrer = false;
      visLagretStatus("Ikke lagret – prøver igjen", true);
      forsteUlagret = 0; setTimeout(function () { KS.lagre(); }, 8000);
      return false;
    });
    return S.lofte;
  };
  /* Sørger for at utkastet finnes og er lagret (også en helt ny, tom side) før noe som krever en lagret side. */
  KS.sikreLagret = function () { return (S.ulagret || S.versjon === 0) ? KS.lagre(true) : Promise.resolve(true); };
  /* Merknader fra serveren («Lenken … vises ikke») blir stående synlig til de er rettet. */
  KS.visMerknader = function (liste) {
    S.merknader = liste;
    if (!liste.length) { KS.fjernBanner("merknader"); return; }
    KS.banner("merknader", "Serveren rettet dette da siden ble lagret:", { niva: "gul", liste: liste, knapper: [{ tekst: "Skjul", fn: function () { KS.fjernBanner("merknader"); } }] });
  };

  /* ================= lokal kladd (localStorage, alt i try/catch) ================= */
  var kladdNokkel = "ks-kladd-" + D.kurs.id;
  var skrivKladd = function () {
    if (!KS.skriv || !S.ulagret) { return; }
    try { window.localStorage.setItem(kladdNokkel, JSON.stringify({ basis_versjon: S.versjon, tid: new Date().toISOString(), dokument: S.dok })); } catch (e) { /* uten lagring virker siden likevel */ }
  };
  var planleggKladd = KS.utsett(function () { skrivKladd(); }, 1000);
  KS.slettKladd = function () { planleggKladd.avbryt(); try { window.localStorage.removeItem(kladdNokkel); } catch (e) { /* ignoreres */ } };
  KS.lesKladd = function () { try { var t = window.localStorage.getItem(kladdNokkel); return t ? JSON.parse(t) : null; } catch (e) { return null; } };
  KS.lagreKladdNa = skrivKladd;
  KS.skrivKladdMed = function (dok, basis) {
    try { window.localStorage.setItem(kladdNokkel, JSON.stringify({ basis_versjon: basis, tid: new Date().toISOString(), dokument: dok })); } catch (e) { /* ignoreres */ }
  };
  function sjekkKladd() {
    var k = KS.lesKladd();
    if (!k || !k.dokument) { return; }
    var lik = JSON.stringify(normaliserDok(KS.kopi(k.dokument))) === JSON.stringify(S.dok);
    if (lik) { KS.slettKladd(); return; }
    var tid = k.tid ? new Date(k.tid) : null;
    var kl = tid && !isNaN(tid.getTime()) ? ("0" + tid.getHours()).slice(-2) + ":" + ("0" + tid.getMinutes()).slice(-2) : "";
    if (k.basis_versjon === S.versjon) {
      KS.banner("kladd", "Du har endringer" + (kl ? " fra " + kl : "") + " som ikke ble lagret.", { niva: "gul", knapper: [
        { tekst: "Gjenopprett", sekundar: false, fn: function () {
          S.dok = normaliserDok(KS.kopi(k.dokument)); KS.fjernBanner("kladd"); S.ulagret = true; KS.snapshot(); KS.tegnAlt(); KS.planleggLagring(); KS.oppdaterStatus();
        } },
        { tekst: "Forkast", fn: function () { KS.slettKladd(); KS.fjernBanner("kladd"); } }] });
    } else {
      KS.banner("kladd", "Du har en kladd" + (kl ? " fra " + kl : "") + " som ikke ble lagret, men siden er endret av noen andre siden den gang. Kladden kan ikke settes rett inn.", { niva: "gul", knapper: [
        { tekst: "Last ned kladden", fn: function () { KS.lastNedJson(k.dokument, "kursside-kladd.json"); } },
        { tekst: "Forkast", fn: function () { KS.slettKladd(); KS.fjernBanner("kladd"); } }] });
    }
  }
  KS.lastNedJson = function (obj, filnavn) {
    try {
      var blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
      var url = URL.createObjectURL(blob);
      var a = KS.el("a", { attr: { href: url, download: filnavn } });
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
    } catch (e) { KS.toast("Kunne ikke laste ned filen i denne nettleseren.", { niva: "feil" }); }
  };

  /* ================= status ================= */
  function normStreng(v) { return typeof v === "string" ? v.trim() : v; }
  function normObj(x) {
    if (Array.isArray(x)) { return x.map(normObj); }
    if (x && typeof x === "object") { var u = {}; Object.keys(x).sort().forEach(function (k) { u[k] = normObj(x[k]); }); return u; }
    return normStreng(x === "" ? null : x);
  }
  var likeBlokker = function (a, b) { return JSON.stringify(normObj(a)) === JSON.stringify(normObj(b)); };
  KS.blokkStatus = function (b) {
    if (!S.publisert) { return { ny: false, endret: false }; }
    var p = S.publisert.blokker.filter(function (x) { return x.id === b.id; })[0];
    if (!p) { return { ny: true, endret: false }; }
    return { ny: false, endret: !likeBlokker(p, b) };
  };
  KS.tellEndringer = function () {
    if (!S.publisert) { return null; }
    var n = 0, dok = S.dok, p = S.publisert;
    if (!likeBlokker({ t: dok.tittel, i: dok.ingress, r: dok.rom, g: dok.godkjent || "" }, { t: p.tittel, i: p.ingress, r: p.rom, g: p.godkjent || "" })) { n++; }
    if (!likeBlokker(dok.melding, p.melding)) { n++; }
    var pIder = p.blokker.map(function (b) { return b.id; }), dIder = dok.blokker.map(function (b) { return b.id; });
    dok.blokker.forEach(function (b) {
      var x = p.blokker.filter(function (y) { return y.id === b.id; })[0];
      if (!x || !likeBlokker(x, b)) { n++; }
    });
    pIder.forEach(function (id) { if (dIder.indexOf(id) < 0) { n++; } });
    var fellesP = pIder.filter(function (id) { return dIder.indexOf(id) >= 0; }), fellesD = dIder.filter(function (id) { return pIder.indexOf(id) >= 0; });
    if (fellesP.join() !== fellesD.join()) { n++; }
    return n;
  };
  KS.oppdaterStatus = function () {
    var s = KS.ks("status"), d = KS.ks("status-detalj"), p = KS.ks("prikk");
    if (!s) { return; }
    var tekst, detalj = "", klasse = "";
    if (!S.aktiv) { tekst = "Nedtatt for deltakerne"; detalj = "Siden er ikke åpen. Åpne den igjen under Innstillinger."; klasse = "feil"; }
    else if (!S.publisert || S.publisertVersjon === null || S.publisertVersjon === undefined) { tekst = "Ikke publisert ennå"; detalj = "Deltakerne ser ikke noe før du publiserer."; klasse = ""; }
    else {
      var n = S.ulagret || S.serverEndringer === null || S.serverEndringer === undefined ? KS.tellEndringer() : S.serverEndringer;
      if (S.ulagret === false && S.versjon === S.publisertVersjon) { n = 0; }
      if (n === 0) { tekst = "Publisert"; detalj = "Ingen endringer siden forrige publisering."; klasse = "ok"; }
      else { tekst = "Utkast"; detalj = KS.flertall(n, "endring", "endringer") + " er ikke publisert"; klasse = ""; }
    }
    s.textContent = tekst; d.textContent = detalj ? "· " + detalj : "";
    p.className = "ks-prikk" + (klasse ? " " + klasse : "");
    if (KS.oppdaterMerker) { KS.oppdaterMerker(); }
  };
  /* Åpningstiden etter kurset: «standard» (180 dager etter siste kursdag), «dager» (egendefinert antall), «ingen» (ingen tidsbegrensning) eller «dato». */
  KS.apningModus = function (i) {
    if (i.stenges) { return "dato"; }
    if (i.apen_dager === 0) { return "ingen"; }
    return i.apen_dager ? "dager" : "standard";
  };
  KS.apningTekst = function (i) {
    var modus = KS.apningModus(i);
    if (modus === "ingen") { return "Åpen uten tidsbegrensning"; }
    if (!i.stenges_effektiv) { return "Åpen til: satt når siste kursdag er kjent"; }
    return "Åpen til " + KS.datoLang(i.stenges_effektiv) + (modus === "dager" ? " (" + i.apen_dager + " dager etter siste kursdag)" : "");
  };
  KS.oppdaterInnstillingerVisning = function () {
    var a = KS.ks("innst-stenges"), k = KS.ks("innst-kode");
    if (a) { a.textContent = KS.apningTekst(S.innst); }
    if (k) {
      var info = D.innsjekk_info || {};
      if (info.type === "digital") { k.textContent = info.zoom_import ? "Innsjekk: oppmøte registreres automatisk fra Zoom" : "Innsjekk på nett: én knapp, uten kode"; }
      else { k.textContent = S.innst.innsjekk_krever_kode ? "Innsjekk på nett krever dagens kode" : "Innsjekk på nett uten kode"; }
    }
  };

  /* ================= sidens topp ================= */
  function fyllTopp() {
    KS.$$("[data-topp]").forEach(function (f) {
      var navn = f.getAttribute("data-topp");
      if (navn === "melding-niva") { f.checked = f.value === S.dok.melding.niva; }
      else if (navn === "melding-tekst") { f.value = S.dok.melding.tekst; }
      else { f.value = S.dok[navn] || ""; }
    });
  }
  function bindTopp() {
    KS.$$("[data-topp]").forEach(function (f) {
      var navn = f.getAttribute("data-topp");
      var ved = function () {
        if (navn === "melding-niva") { if (!f.checked) { return; } S.dok.melding.niva = f.value; }
        else if (navn === "melding-tekst") { S.dok.melding.tekst = f.value; }
        else { S.dok[navn] = f.value; }
        KS.endret({});
      };
      f.addEventListener(f.type === "radio" ? "change" : "input", ved);
    });
  }

  /* ================= forhåndsvisning ================= */
  var forhModus = "mobil";
  function forhUrl() {
    var dag = KS.ks("forh-dag") ? KS.ks("forh-dag").value : "";
    return D.urls.forhandsvis + "?versjon=utkast" + (dag ? "&dag=" + encodeURIComponent(dag) : "") + "&t=" + Date.now();
  }
  KS.forhUrl = forhUrl;
  KS.lastForhandsvisning = function () {
    var f = KS.ks("forh");
    if (!f || !f.offsetParent) { return; }
    var forrigeY = 0;
    try { forrigeY = f.contentWindow.scrollY || 0; } catch (e) { forrigeY = 0; }
    var id = S.sisteId;
    var ved = function () {
      f.removeEventListener("load", ved);
      try {
        var el = id ? f.contentDocument.getElementById("blokk-" + id) : null;
        if (el) { el.scrollIntoView({ block: "start" }); } else { f.contentWindow.scrollTo(0, forrigeY); }
      } catch (e) { /* utenfor samme nettsted: ingen rulling */ }
    };
    f.addEventListener("load", ved);
    try { f.contentWindow.location.replace(forhUrl()); } catch (e) { f.setAttribute("src", forhUrl()); }
  };
  KS.planleggForhandsvisning = KS.utsett(function () { KS.lastForhandsvisning(); }, 1000);
  function tilpassForhRamme() {
    var ramme = KS.ks("forh-ramme");
    if (!ramme) { return; }
    ramme.classList.toggle("mobil", forhModus === "mobil");
    ramme.classList.toggle("pc", forhModus === "pc");
    if (forhModus === "pc") {
      var bredde = KS.$(".ks-forh-h").clientWidth - 2;
      ramme.style.setProperty("--ks-skala", String(Math.min(1, Math.max(0.2, bredde / 1120))));
    }
    var m = KS.ks("forh-mobil"), p = KS.ks("forh-pc");
    m.classList.toggle("pa", forhModus === "mobil"); m.setAttribute("aria-pressed", forhModus === "mobil" ? "true" : "false");
    p.classList.toggle("pa", forhModus === "pc"); p.setAttribute("aria-pressed", forhModus === "pc" ? "true" : "false");
  }
  function bindForhandsvisning() {
    var m = KS.ks("forh-mobil"), p = KS.ks("forh-pc"), dag = KS.ks("forh-dag");
    if (!m) { return; }
    m.addEventListener("click", function () { forhModus = "mobil"; tilpassForhRamme(); });
    p.addEventListener("click", function () { forhModus = "pc"; tilpassForhRamme(); });
    dag.addEventListener("change", function () { KS.lastForhandsvisning(); });
    KS.$$("option", dag).forEach(function (o, i) {
      if (i === 0) { return; }
      var kd = D.kursdager.filter(function (k) { return String(k.id) === o.value; })[0];
      if (kd) { o.textContent = "Dag " + i + " · " + KS.datoTekst(kd.dato); }
    });
    window.addEventListener("resize", KS.utsett(tilpassForhRamme, 150));
    tilpassForhRamme();
  }

  /* ================= kopiering av lenker ================= */
  function bindKopier() {
    KS.$$("[data-kopier]").forEach(function (knapp) {
      knapp.addEventListener("click", function () {
        var felt = document.getElementById(knapp.getAttribute("data-kopier"));
        felt.focus(); felt.select();
        var ok = false;
        function ferdig(v) { KS.toast(v ? "Lenken er kopiert." : "Marker lenken og kopier med Ctrl+C.", { niva: v ? "ok" : undefined }); }
        if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(felt.value).then(function () { ferdig(true); }, function () { try { ok = document.execCommand("copy"); } catch (e) { ok = false; } ferdig(ok); }); return; }
        try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
        ferdig(ok);
      });
    });
  }

  /* ================= skrivebeskyttet ================= */
  KS.laas = function (ctx) {
    if (KS.skriv) { return; }
    KS.$$("input, select, textarea", ctx).forEach(function (e) { if (!e.hasAttribute("readonly")) { e.disabled = true; } });
    KS.$$("[contenteditable]", ctx).forEach(function (e) { e.setAttribute("contenteditable", "false"); });
    KS.$$("[data-endrer]", ctx).forEach(function (e) { e.hidden = true; });
  };

  /* ================= hjerteslag, avslutning, hurtigtaster ================= */
  function hjerteslag() {
    KS.jsonKall(D.urls.redigerer, {}).then(function (r) {
      if (r.utenJson) { KS.loggetUt(); return; }
      var j = r.json || {};
      if (j.status !== "ok") { return; }
      if (j.versjon > S.versjon && !S.ulagret) {
        KS.banner("nyere", (j.annen ? j.annen.navn : "Noen andre") + " har lagret nye endringer på siden.", { niva: "gul", knapper: [{ tekst: "Last inn på nytt", sekundar: false, fn: function () { window.location.reload(); } }] });
      }
      if (j.annen) { KS.banner("annen", j.annen.navn + " redigerer også nå. Lagringer fra dere begge kan gi konflikter.", { niva: "gul" }); } else { KS.fjernBanner("annen"); }
    }, function () { /* nettet er borte en stund: neste hjerteslag prøver igjen */ });
  }
  KS.hjerteslag = hjerteslag;
  function harFeltFokus(e) {
    var t = e.target;
    return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
  }
  function bindGlobalt() {
    document.addEventListener("keydown", function (e) {
      var ctrl = e.ctrlKey || e.metaKey;
      if (ctrl && !e.altKey && (e.key === "s" || e.key === "S")) { e.preventDefault(); if (KS.skriv) { KS.lagre(true); } return; }
      if (ctrl && !e.altKey && (e.key === "h" || e.key === "H") && KS.skriv && KS.apneFinn) { e.preventDefault(); KS.apneFinn(); return; }
      if (ctrl && !e.altKey && !harFeltFokus(e) && KS.skriv) {
        var k = e.key.toLowerCase();
        if (k === "z" && !e.shiftKey) { e.preventDefault(); KS.angre(); }
        else if ((k === "z" && e.shiftKey) || k === "y") { e.preventDefault(); KS.gjorOm(); }
      }
    });
    if (!KS.skriv) { return; }
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden" && S.ulagret) { KS.lagre(); } });
    window.addEventListener("blur", function () { if (S.ulagret) { KS.lagre(); } });
    rot.addEventListener("focusout", function (e) {
      if (S.ulagret && (!e.relatedTarget || !rot.contains(e.relatedTarget))) { KS.planleggLagring(400); }
    });
    window.addEventListener("beforeunload", function (e) {
      if (S.ulagret || S.opplastinger.some(function (o) { return !o.ferdig && !o.feil; })) { e.preventDefault(); e.returnValue = ""; }
    });
    window.setInterval(hjerteslag, 30000);
  }

  /* ================= oppstart ================= */
  KS.tegnAlt = function () {
    fyllTopp();
    if (KS.tegnListe) { KS.tegnListe(); }
    KS.oppdaterStatus();
  };
  /* Bannerne står fast rett under statuslinjen: høyden på statuslinjen (den vokser på mobil) legges i en CSS-variabel. */
  function folgVerktoylinjen() {
    var linje = KS.$(".ks-verktoy");
    if (!linje) { return; }
    var sett = function () { rot.style.setProperty("--ks-verktoy-h", (linje.offsetHeight + 8) + "px"); };
    sett();
    if (window.ResizeObserver) { new window.ResizeObserver(sett).observe(linje); } else { window.addEventListener("resize", sett); }
  }
  function start() {
    folgVerktoylinjen();
    fyllTopp(); bindTopp();
    KS.laas(KS.$(".ks-topp"));
    KS.snapshot();
    KS.init.forEach(function (f) { f(); });
    KS.tegnAlt();
    KS.oppdaterInnstillingerVisning();
    bindKopier(); bindForhandsvisning(); bindGlobalt();
    var a = KS.ks("angre"), g = KS.ks("gjor-om");
    if (a) { a.addEventListener("click", function () { KS.angre(); }); }
    if (g) { g.addEventListener("click", function () { KS.gjorOm(); }); }
    var pub = KS.ks("publiser");
    if (pub && KS.publiser) { pub.addEventListener("click", function () { KS.publiser(); }); }
    var fh = KS.ks("forhandsvis");
    if (fh) { fh.addEventListener("click", function () { if (S.ulagret) { KS.lagre(); } }); }
    if (KS.skriv) { sjekkKladd(); oppdaterAngreKnapper(); }
  }
  document.addEventListener("DOMContentLoaded", start);
})();
