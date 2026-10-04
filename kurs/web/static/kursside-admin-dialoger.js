/* Kursside i administrasjonen: DIALOGENE (publisering, konflikt, tidligere versjoner, hent fra annet kurs, start fra mal, innstillinger, finn og erstatt,
   lenke i rik tekst, redigering som tekst, innliming fra Excel, deltakernavn). Dialogene står ferdig skrevet i _kursside_admin_dialoger.html og åpnes med
   showModal() (finn og erstatt med show(), så siden bak kan brukes). Denne filen fyller dem med data og kobler knappene; aldri med HTML fra tekst.
   Hører sammen med kursside-admin.js (kjernen). */
(function () {
  "use strict";
  var KS = window.KS;
  if (!KS) { return; }
  var S = KS.S, D = KS.data;

  function dlg(navn) { return document.getElementById("ks-dialog-" + navn); }
  function apne(d, opener) {
    d.__opener = opener || document.activeElement;
    if (!d.open) { d.showModal(); }
  }
  function forbered(d) {
    KS.$$("[data-lukk]", d).forEach(function (b) { b.addEventListener("click", function () { d.close(); }); });
    d.addEventListener("close", function () {
      var o = d.__opener;
      d.__opener = null;
      if (o && document.contains(o) && typeof o.focus === "function") { try { o.focus(); } catch (e) { /* ignoreres */ } }
    });
  }
  function felt(d, navn) { return KS.felt(navn, d); }
  function rens(node) { return KS.tom(node); }

  /* ================= konflikt: noen andre har lagret ================= */
  var DIFF_SYM = { ny: ["ny", "+"], endret: ["end", "~"], fjernet: ["bort", "−"], flyttet: ["fl", "↕"] };
  function diffListe(ul, diff, lukkDialog) {
    rens(ul);
    if (!diff.length) { ul.appendChild(KS.el("li", { klasse: "tom", tekst: "Ingen endringer å vise." })); return; }
    diff.forEach(function (p) {
      var sym = DIFF_SYM[p.art] || DIFF_SYM.endret;
      var li = KS.el("li", { klasse: sym[0] });
      li.appendChild(KS.el("span", { klasse: "sym", tekst: sym[1], attr: { "aria-hidden": "true" } }));
      li.appendChild(KS.el("span", { barn: [KS.el("b", { tekst: p.tittel }), KS.el("small", { tekst: p.tekst })] }));
      if (lukkDialog && p.art !== "fjernet") {
        var v = KS.el("button", { klasse: "lenkeknapp", tekst: "Vis", attr: { type: "button", "aria-label": "Vis «" + p.tittel + "»" } });
        v.addEventListener("click", function () {
          lukkDialog();
          if (p.id && p.id.charAt(0) === "b") { KS.apneBlokk(p.id); }
          else { var t = KS.$(".ks-topp"); if (t) { t.scrollIntoView({ block: "center", behavior: "smooth" }); } }
        });
        li.appendChild(v);
      } else { li.appendChild(KS.el("span")); }
      ul.appendChild(li);
    });
  }
  KS.visKonflikt = function (j, mitt) {
    var d = dlg("konflikt");
    S.konflikt = { j: j, mitt: mitt };
    var navn = j.endret_av || "Noen andre";
    felt(d, "tittel").textContent = navn + " har lagret siden mens du redigerte";
    diffListe(felt(d, "diff"), j.diff || [], null);
    KS.banner("konflikt", "Siden er lagret av " + navn + (j.endret ? " kl. " + j.endret : "") + ". Lagringen er satt på pause til du har valgt hva som skal skje.", {
      niva: "feil", alarm: true, fet: "Ikke lagret.", knapper: [{ tekst: "Velg hva som skal skje", sekundar: false, fn: function () { apne(d); } }]
    });
    apne(d);
  };
  function konfliktValg() {
    var d = dlg("konflikt");
    KS.ks("konflikt-behold", d).addEventListener("click", function () {
      var k = S.konflikt; if (!k) { return; }
      S.versjon = k.j.versjon; S.konflikt = null; KS.fjernBanner("konflikt"); S.ulagret = true;
      d.close(); KS.lagre();
    });
    KS.ks("konflikt-deres", d).addEventListener("click", function () {
      var k = S.konflikt; if (!k) { return; }
      var gammelVersjon = S.versjon, mitt = KS.kopi(S.dok);
      S.konflikt = null; KS.fjernBanner("konflikt");
      KS.settDokument(k.j.dokument, k.j.versjon, true);
      S.ulagret = false;
      KS.skrivKladdMed(mitt, gammelVersjon);
      KS.oppdaterStatus();
      d.close();
      KS.toast("Deres versjon er hentet. Din versjon er tatt vare på i denne nettleseren.", {});
    });
    KS.ks("konflikt-last-ned", d).addEventListener("click", function () { var k = S.konflikt; KS.lastNedJson(k ? k.mitt : S.dok, "kursside-min-kopi.json"); });
  }

  /* ================= publisering ================= */
  function fyllPubliser(j) {
    var d = dlg("publiser");
    var n = j.antall_deltakere, deltakere = n === 1 ? "1 deltaker med bekreftet påmelding ser" : n + " deltakere med bekreftet påmelding ser";
    var ingen = j.ingen_endringer === true;
    var ingress;
    if (ingen) { ingress = "Ingen endringer siden forrige publisering."; }
    else if (j.forrige_publisert) { ingress = deltakere + " endringene med en gang. Forrige versjon ble publisert " + j.forrige_publisert + "."; }
    else { ingress = "Siden er ikke publisert ennå. " + deltakere + " den med en gang."; }
    felt(d, "ingress").textContent = ingress;
    diffListe(felt(d, "diff"), ingen ? [] : (j.diff || []), function () { d.close(); });
    var ul = rens(felt(d, "kontroll")), feil = j.feil || [], adv = j.advarsler || [];
    function post(klasse, ikon, tekst) { ul.appendChild(KS.el("li", { barn: [KS.el("span", { klasse: klasse, barn: [KS.ikon(ikon)] }), KS.el("span", { tekst: tekst })] })); }
    feil.forEach(function (f) { post("fei", "x", f.tekst); });
    var ventende = KS.ventendeOpplastinger ? KS.ventendeOpplastinger() : [];
    ventende.forEach(function (o) { post("adv", "varsel", "«" + o.navn + "» er ikke ferdig lastet opp (" + o.prosent + " %) og blir ikke med."); });
    adv.forEach(function (a) { post("adv", "varsel", a.tekst); });
    function har(liste, ord) { return liste.some(function (x) { return x.tekst.indexOf(ord) >= 0; }); }
    if (!har(feil, "alternativ tekst")) { post("ok", "hake", "Alle bilder har alternativ tekst"); }
    if (!har(adv, "gyldig adresse")) { post("ok", "hake", "Alle lenker har gyldig adresse"); }
    if (!har(adv, "er tom")) { post("ok", "hake", "Ingen tomme blokker"); }
    if (!har(feil, "e-postadressen til en deltaker")) { post("ok", "hake", "Ingen e-postadresser til deltakere"); }
    var knapp = KS.ks("publiser-nå", d);
    knapp.disabled = feil.length > 0 || ingen;
    knapp.title = feil.length ? "Rett feilene først" : (ingen ? "Ingen endringer å publisere" : "");
    felt(d, "se-som-deltaker").setAttribute("href", D.urls.forhandsvis + "?versjon=utkast");
  }
  KS.publiser = function () {
    var knapp = KS.ks("publiser");
    if (knapp) { knapp.disabled = true; }
    function fri() { if (knapp) { knapp.disabled = false; } }
    KS.sikreLagret().then(function (ok) {
      if (!ok) { fri(); KS.toast("Siden er ikke lagret, så den kan ikke publiseres ennå.", { niva: "feil" }); return; }
      return KS.hent(D.urls.sjekk).then(function (r) {
        fri();
        if (r.utenJson) { KS.loggetUt(); return; }
        if (r.status === 409) { KS.toast("Lagre siden først.", { niva: "feil" }); return; }
        fyllPubliser(r.json);
        apne(dlg("publiser"), knapp);
      });
    });
  };
  function publiserNa() {
    var d = dlg("publiser"), knapp = KS.ks("publiser-nå", d);
    knapp.disabled = true;
    KS.jsonKall(D.urls.publiser, { versjon: S.versjon }).then(function (r) {
      if (r.utenJson) { knapp.disabled = false; KS.loggetUt(); d.close(); return; }
      var j = r.json || {};
      if (r.status === 200) {
        S.publisertVersjon = j.publisert_versjon;
        try { S.publisert = KS.normaliserDok(JSON.parse(S.sistLagretJson)); } catch (e) { S.publisert = KS.normaliserDok(KS.kopi(S.dok)); }
        S.serverEndringer = 0; S.versjoner = j.versjoner || S.versjoner;
        KS.oppdaterStatus();
        d.close();
        var kl = j.publisert || "";
        KS.toast(j.ingen_endringer ? "Ingen endringer å publisere." : "Publisert" + (kl ? " kl. " + kl : "") + ".", { niva: "ok", angre: function () { window.open(D.urls.forhandsvis + "?versjon=publisert", "_blank", "noopener"); }, angreTekst: "Åpne som deltaker ↗", varighet: 9000 });
        return;
      }
      knapp.disabled = false;
      if (r.status === 409 && j.status === "konflikt") { d.close(); KS.visKonflikt(j, KS.kopi(S.dok)); return; }
      if (r.status === 422 && j.feil) { fyllPubliser({ antall_deltakere: 0, diff: [], feil: j.feil, advarsler: [], ingen_endringer: false }); felt(d, "ingress").textContent = j.melding || "Siden kan ikke publiseres før feilene er rettet."; return; }
      KS.toast(KS.feilTekst(r, "Kunne ikke publisere."), { niva: "feil" });
    });
  }

  /* ================= tidligere versjoner ================= */
  function hentFerskeVersjoner() {
    /* Listen kan ha endret seg (sikkerhetskopier tas ved mal, hent fra annet kurs og gjenoppretting): leser den fra redigeringssiden selv (skriver ingenting). */
    return fetch(D.urls.side, { credentials: "same-origin" }).then(function (r) { return r.text(); }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, "text/html"), el = doc.getElementById("ks-data");
      return el ? JSON.parse(el.textContent).versjoner : null;
    }).catch(function () { return null; });
  }
  function tegnVersjoner(d) {
    var ul = rens(felt(d, "liste"));
    if (!S.versjoner.length) { ul.appendChild(KS.el("li", { barn: [KS.el("span", { tekst: "Ingen tidligere versjoner ennå. De kommer når du publiserer, eller før noe erstattes." })] })); return; }
    S.versjoner.forEach(function (v) {
      var tekst = KS.el("span", { barn: [KS.el("b", { tekst: (v.arsak === "publisert" ? "Publisert" : "Sikkerhetskopi") + " · " + v.opprettet }),
        KS.el("small", { tekst: [v.opprettet_av, v.merknad].filter(Boolean).join(" · ") })] });
      var li = KS.el("li", { barn: [tekst] });
      if (KS.skriv) {
        var kn = KS.el("button", { klasse: "sekundar liten", tekst: "Gjenopprett som utkast", attr: { type: "button" } });
        kn.addEventListener("click", function () { gjenopprett(v, d); });
        li.appendChild(kn);
      }
      ul.appendChild(li);
    });
  }
  function gjenopprett(v, d) {
    KS.sikreLagret().then(function (ok) {
      if (!ok) { return; }
      KS.jsonKall(D.urls.gjenopprett.replace("{id}", v.id), { versjon: S.versjon }).then(function (r) {
        if (r.utenJson) { KS.loggetUt(); return; }
        var j = r.json || {};
        if (r.status === 200) {
          KS.settDokument(j.dokument, j.versjon);
          KS.visMerknader(j.merknader || []);
          d.close();
          KS.toast("Versjonen er lagt inn som utkast (ikke publisert). Utkastet du hadde er tatt vare på under Tidligere versjoner.", { niva: "ok", varighet: 8000 });
        } else if (r.status === 409 && j.status === "konflikt") { d.close(); KS.visKonflikt(j, KS.kopi(S.dok)); }
        else { KS.toast(KS.feilTekst(r, "Kunne ikke gjenopprette."), { niva: "feil" }); }
      });
    });
  }
  KS.apneVersjoner = function () {
    var d = dlg("versjoner");
    tegnVersjoner(d);
    apne(d);
    hentFerskeVersjoner().then(function (v) { if (v) { S.versjoner = v; tegnVersjoner(d); } });
  };

  /* ================= alle filer på kurset (slett for godt, med bekreftelse) ================= */
  /* {fil_id: tittel på første blokk som bruker filen} i utkastet */
  function filbruk() {
    var ut = {};
    S.dok.blokker.forEach(function (b) {
      var ider = b.type === "filer" ? b.data.filer.map(function (e) { return e.fil_id; }) : (b.type === "bilde" ? [b.data.fil_id] : []);
      ider.forEach(function (id) { if (id !== null && id !== undefined && !(id in ut)) { ut[id] = b.tittel || KS.standardTittel[b.type]; } });
    });
    return ut;
  }
  function tegnFiler(d) {
    var ul = rens(felt(d, "liste")), bruk = filbruk();
    felt(d, "kvote-tekst").textContent = KS.flertall(S.kvote.antall, "fil", "filer") + " i bruk · " + (S.kvote.bytes / 1048576).toFixed(1).replace(".", ",") + " MB av "
      + Math.round(S.kvote.maks_bytes / 1048576) + " MB. En fil som brukes på siden kan ikke slettes før den er fjernet derfra.";
    if (!S.filer.length) { ul.appendChild(KS.el("li", { barn: [KS.el("span", { tekst: "Ingen filer er lastet opp ennå." })] })); return; }
    S.filer.forEach(function (f) {
      var kode = KS.filtypeKode(f.filnavn, f.type), tid = KS.norskTid(f.opprettet);
      var tekst = KS.el("span", { klasse: "ks-fo-tekst", barn: [KS.el("b", { tekst: f.filnavn }),
        KS.el("small", { tekst: [KS.storrelse(f.storrelse), tid ? "lastet opp " + tid.dato : "", (f.id in bruk) ? "Brukes i «" + bruk[f.id] + "»" : "Ikke brukt på siden"].filter(Boolean).join(" · ") })] });
      var li = KS.el("li", { barn: [KS.el("span", { klasse: "ft " + kode[1], tekst: kode[0], attr: { "aria-hidden": "true" } }), tekst] });
      if (KS.skriv) {
        var handling = KS.el("span", { klasse: "ks-fo-handling" });
        var slett = KS.el("button", { klasse: "sekundar liten fare", tekst: "Slett for godt", attr: { type: "button", "aria-label": "Slett «" + f.filnavn + "» for godt" } });
        if (f.id in bruk) { slett.disabled = true; slett.title = "Brukes i «" + bruk[f.id] + "». Fjern den fra siden først."; }
        slett.addEventListener("click", function () {
          KS.tom(handling);
          handling.appendChild(KS.el("span", { klasse: "ks-fo-spor", tekst: "Slette «" + f.filnavn + "» for godt? Filen kan ikke hentes tilbake." }));
          var ja = KS.el("button", { klasse: "fare liten", tekst: "Ja, slett", attr: { type: "button" } });
          var nei = KS.el("button", { klasse: "sekundar liten", tekst: "Avbryt", attr: { type: "button" } });
          nei.addEventListener("click", function () { tegnFiler(d); });
          ja.addEventListener("click", function () { slettFil(f, d); });
          handling.appendChild(ja); handling.appendChild(nei); ja.focus();
        });
        handling.appendChild(slett);
        li.appendChild(handling);
      }
      ul.appendChild(li);
    });
  }
  function slettFil(f, d) {
    KS.jsonKall(D.urls.fil_slett.replace("{id}", f.id), {}).then(function (r) {
      if (r.utenJson) { KS.loggetUt(); return; }
      var j = r.json || {};
      if (r.status === 200) {
        S.filer = S.filer.filter(function (x) { return x.id !== f.id; });
        S.kvote = j.kvote;
        tegnFiler(d);
        S.dok.blokker.forEach(function (b) { if (b.type === "filer" && KS.kortFor(b.id) && KS.kortFor(b.id).classList.contains("apen")) { KS.tegnBody(b.id); } });
        KS.toast("«" + f.filnavn + "» er slettet for godt.", { niva: "ok" });
      } else {
        tegnFiler(d);
        KS.toast(KS.feilTekst(r, "Kunne ikke slette filen."), { niva: "feil" });
      }
    });
  }
  KS.apneFiler = function () { var d = dlg("filer"); tegnFiler(d); apne(d); };

  /* ================= start fra mal / hent fra annet kurs ================= */
  function svarMedNyttUtkast(r, d, tekst) {
    if (r.utenJson) { KS.loggetUt(); return; }
    var j = r.json || {};
    if (r.status === 200) {
      if (j.filer) { S.filer = j.filer; }
      KS.settDokument(j.dokument, j.versjon);
      KS.visMerknader(j.merknader || []);
      d.close();
      KS.toast(tekst, { niva: "ok", angre: null, varighet: 8000 });
    } else if (r.status === 409 && j.status === "konflikt") { d.close(); KS.visKonflikt(j, KS.kopi(S.dok)); }
    else { KS.toast(KS.feilTekst(r, "Kunne ikke utføre handlingen."), { niva: "feil" }); }
  }
  KS.apneMal = function () { apne(dlg("mal")); };
  function malOk() {
    var d = dlg("mal"), valgt = d.querySelector('input[name="ks-mal-valg"]:checked'), knapp = KS.ks("mal-ok", d);
    knapp.disabled = true;
    KS.sikreLagret().then(function (ok) {
      if (!ok) { knapp.disabled = false; return; }
      KS.jsonKall(D.urls.mal, { versjon: S.versjon, mal: valgt.value }).then(function (r) {
        knapp.disabled = false;
        svarMedNyttUtkast(r, d, "Utkastet er erstattet med malen. Forrige utkast er tatt vare på under Tidligere versjoner.");
      });
    });
  }
  function apneHent() {
    var d = dlg("hent"), sel = felt(d, "kurs"), status = felt(d, "status"), blokker = felt(d, "blokker"), liste = felt(d, "liste"), ok = KS.ks("hent-ok", d);
    ok.disabled = true; blokker.hidden = true; rens(liste);
    while (sel.options.length > 1) { sel.remove(1); }
    status.textContent = "Henter kurs …";
    apne(d);
    KS.hent(D.urls.kilder).then(function (r) {
      if (r.utenJson) { KS.loggetUt(); d.close(); return; }
      S.kilder = (r.json && r.json.kurs) || [];
      status.textContent = S.kilder.length ? "" : "Ingen andre kurs har en Min side ennå.";
      S.kilder.forEach(function (k) { sel.appendChild(KS.el("option", { tekst: k.navn + " (" + KS.flertall(k.blokker.length, "blokk", "blokker") + ")", attr: { value: String(k.id) } })); });
    });
  }
  function hentValg() {
    var d = dlg("hent"), sel = felt(d, "kurs"), blokker = felt(d, "blokker"), liste = rens(felt(d, "liste")), ok = KS.ks("hent-ok", d);
    var k = (S.kilder || []).filter(function (x) { return String(x.id) === sel.value; })[0];
    blokker.hidden = !k; ok.disabled = true;
    if (!k) { return; }
    k.blokker.forEach(function (b) {
      var cb = KS.el("input", { attr: { type: "checkbox", value: b.id } });
      cb.addEventListener("change", function () { ok.disabled = !KS.$$("input:checked", liste).length; });
      liste.appendChild(KS.el("label", { barn: [cb, KS.el("span", { tekst: (b.tittel || "(uten tittel)") + " · " + (KS.typeNavn[b.type] || b.type) })] }));
    });
  }
  /* «Velg alle» / «Fjern alle»: kopiere en hel side krevde ett klikk per blokk */
  function hentVelgAlle(avkrysset) {
    var d = dlg("hent"), ok = KS.ks("hent-ok", d);
    KS.$$("input[type=checkbox]", felt(d, "liste")).forEach(function (c) { c.checked = avkrysset; });
    ok.disabled = !avkrysset || !KS.$$("input:checked", felt(d, "liste")).length;
  }
  function hentOk() {
    var d = dlg("hent"), sel = felt(d, "kurs"), knapp = KS.ks("hent-ok", d);
    var ider = KS.$$("input:checked", felt(d, "liste")).map(function (c) { return c.value; });
    if (!sel.value || !ider.length) { return; }
    knapp.disabled = true;
    KS.sikreLagret().then(function (ok) {
      if (!ok) { knapp.disabled = false; return; }
      KS.jsonKall(D.urls.hent_fra, { versjon: S.versjon, fra_kurs_id: parseInt(sel.value, 10), blokk_ider: ider }).then(function (r) {
        knapp.disabled = false;
        svarMedNyttUtkast(r, d, KS.flertall(ider.length, "blokk er", "blokker er") + " hentet og lagt sist. Forrige utkast er tatt vare på under Tidligere versjoner.");
      });
    });
  }

  /* ================= stor forhåndsvisning (PC i full størrelse) ================= */
  /* Den lille PC-visningen ved siden av redigeringen er skalert ned til rundt en tredjedel og bare egnet til å se oppsettet. Her vises siden i full
     bredde (samme rute og samme «Vis som dag» som den lille), så teksten kan leses. */
  function lastForhStor() {
    var d = dlg("forh-stor"), ramme = felt(d, "ramme");
    ramme.setAttribute("src", KS.forhUrl());
  }
  function apneForhStor() {
    var d = dlg("forh-stor");
    KS.sikreLagret().then(function () { lastForhStor(); apne(d); });
  }

  /* ================= innstillinger for siden ================= */
  /* Åpningstiden etter kurset: fire valg (standard, antall dager, ingen tidsbegrensning, bestemt dato). Bare ett gjelder om gangen. */
  var APNING = ["standard", "dager", "ingen", "dato"];
  function standardDager() { return S.innst.standard_dager || 180; }
  function dagerMin() { return S.innst.apen_dager_min || 1; }
  function dagerMaks() { return S.innst.apen_dager_maks || 3650; }
  function plussDager(iso, dager) {
    var p = iso.split("-");
    return new Date(Date.UTC(parseInt(p[0], 10), parseInt(p[1], 10) - 1, parseInt(p[2], 10) + dager)).toISOString().slice(0, 10);
  }
  /* Det dialogen står på nå: {modus, stenges, apen_dager, feil, hjelp}. stenges «» og apen_dager null = tilbake til standard. */
  function apningLes(d) {
    var modus = APNING.filter(function (m) { return felt(d, "apning-" + m).checked; })[0] || "standard";
    var r = { modus: modus, stenges: "", apen_dager: null, feil: "", hjelp: "" };
    var siste = S.innst.siste_kursdag;
    function fraSiste(dager) { return siste ? "Åpen til " + KS.datoLang(plussDager(siste, dager)) + "." : "Åpen " + dager + " dager etter siste kursdag. Datoen vises når kursdagene er lagt inn."; }
    if (modus === "standard") { r.hjelp = fraSiste(standardDager()); }
    else if (modus === "dager") {
      var tekst = felt(d, "apen-dager").value.trim();
      var n = /^\d{1,5}$/.test(tekst) ? parseInt(tekst, 10) : null;
      if (n === null || n < dagerMin() || n > dagerMaks()) { r.feil = "Skriv et helt antall dager fra " + dagerMin() + " til " + dagerMaks() + "."; r.hjelp = r.feil; }
      else { r.apen_dager = n; r.hjelp = fraSiste(n); }
    } else if (modus === "ingen") { r.apen_dager = 0; r.hjelp = "Åpen uten tidsbegrensning: alle med bekreftet påmelding kommer inn så lenge kurset finnes."; }
    else {
      var dato = felt(d, "stenges").value;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(dato)) { r.feil = "Velg en dato."; r.hjelp = r.feil; }
      else { r.stenges = dato; r.hjelp = "Åpen til " + KS.datoLang(dato) + "."; }
    }
    return r;
  }
  function oppdaterApningHjelp() { var d = dlg("innstillinger"); felt(d, "apning-hjelp").textContent = apningLes(d).hjelp; }
  function velgApning(d, modus) { felt(d, "apning-" + modus).checked = true; oppdaterApningHjelp(); }
  KS.apneInnstillinger = function (fokusKode) {
    var d = dlg("innstillinger");
    felt(d, "aktiv").checked = S.aktiv;
    felt(d, "apning-standard-tekst").textContent = "Standard: " + standardDager() + " dager";
    felt(d, "apen-dager").min = String(dagerMin()); felt(d, "apen-dager").max = String(dagerMaks());
    felt(d, "apen-dager").value = S.innst.apen_dager ? String(S.innst.apen_dager) : "";
    felt(d, "stenges").value = S.innst.stenges || "";
    velgApning(d, KS.apningModus(S.innst));
    var kode = felt(d, "krever-kode"), info = D.innsjekk_info || {};
    kode.checked = S.innst.innsjekk_krever_kode;
    var digital = info.type === "digital";
    kode.disabled = digital;
    felt(d, "kode-hjelp").textContent = digital
      ? (info.zoom_import ? "Digitale kurs med Zoom-import: oppmøte registreres automatisk fra Zoom dagen etter. Ingen knapp vises for deltakerne." : "Digitale kurs uten Zoom-import: deltakerne får én knapp for å registrere oppmøte, uten kode.")
      : "Gjelder fysiske og hybride kurs. Koden står på skjermen i kurslokalet («Vis QR på skjerm» under Oppsett).";
    var f = felt(d, "feil"); f.hidden = true;
    apne(d);
    if (fokusKode && !digital) { kode.focus(); }
  };
  /* Åpningstiden slik siden står på nå, i samme form som apningLes gir (stenges «» og apen_dager null = standard). */
  function apningNaa() {
    var modus = KS.apningModus(S.innst);
    return { stenges: modus === "dato" ? S.innst.stenges : "", apen_dager: modus === "dager" ? S.innst.apen_dager : (modus === "ingen" ? 0 : null) };
  }
  /* Bare det administratoren faktisk har endret sendes til serveren. Dialogen kan være åpnet fra en side som er blitt utdatert (en annen fane eller en
     annen administrator har endret åpningstiden i mellomtiden): da må en urelatert endring, som kodekravet, aldri skrive den gamle verdien tilbake. */
  function innstillingerSomErEndret(kode, apning) {
    var last = {}, na = apningNaa();
    if (kode !== !!S.innst.innsjekk_krever_kode) { last.innsjekk_krever_kode = kode; }
    if (apning.stenges !== na.stenges || apning.apen_dager !== na.apen_dager) { last.stenges = apning.stenges; last.apen_dager = apning.apen_dager; }
    return last;
  }
  function innstillingerOk() {
    var d = dlg("innstillinger"), knapp = KS.ks("innst-ok", d), feil = felt(d, "feil");
    var aktiv = felt(d, "aktiv").checked, kode = felt(d, "krever-kode").checked, apning = apningLes(d);
    if (apning.feil) { feil.textContent = apning.feil; feil.hidden = false; return; }
    knapp.disabled = true; feil.hidden = true;
    KS.sikreLagret().then(function (ok) {
      if (!ok) { knapp.disabled = false; return; }
      var lofte = aktiv !== S.aktiv ? KS.jsonKall(D.urls.aktiv, { aktiv: aktiv }) : Promise.resolve({ status: 200, json: { aktiv: aktiv } });
      lofte.then(function (r) {
        if (r.utenJson) { KS.loggetUt(); knapp.disabled = false; return; }
        if (r.status !== 200) { knapp.disabled = false; feil.textContent = KS.feilTekst(r, "Kunne ikke lagre."); feil.hidden = false; return; }
        S.aktiv = !!r.json.aktiv;
        var last = innstillingerSomErEndret(kode, apning);
        if (!Object.keys(last).length) {                                          // ingen endring i kodekrav eller åpningstid: ingenting å sende
          knapp.disabled = false; KS.oppdaterStatus(); d.close();
          KS.toast("Innstillingene er lagret.", { niva: "ok" });
          return;
        }
        return KS.jsonKall(D.urls.innstillinger, last).then(function (r2) {
          knapp.disabled = false;
          if (r2.utenJson) { KS.loggetUt(); return; }
          if (r2.status !== 200) { feil.textContent = KS.feilTekst(r2, "Kunne ikke lagre innstillingene."); feil.hidden = false; return; }
          /* Serverens svar er fasit for alt (også det denne dialogen ikke sendte): siden vises da riktig selv om den var utdatert. */
          S.innst = Object.assign({}, S.innst, { innsjekk_krever_kode: r2.json.innsjekk_krever_kode === undefined ? kode : !!r2.json.innsjekk_krever_kode,
            stenges: r2.json.stenges === undefined ? (apning.stenges || null) : r2.json.stenges,
            apen_dager: r2.json.apen_dager === undefined ? apning.apen_dager : r2.json.apen_dager, stenges_effektiv: r2.json.stenges_effektiv,
            siste_kursdag: r2.json.siste_kursdag === undefined ? S.innst.siste_kursdag : r2.json.siste_kursdag });
          KS.oppdaterInnstillingerVisning(); KS.oppdaterStatus();
          d.close();
          KS.toast("Innstillingene er lagret.", { niva: "ok" });
        });
      });
    });
  }

  /* ================= finn og erstatt ================= */
  function regex(d) {
    var q = felt(d, "sok").value;
    if (!q) { return null; }
    return new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), felt(d, "ikke-skill").checked ? "gi" : "g");
  }
  /* Alle tekstfeltene som kan søkes i: {id: blokk-id eller null, html: bool, hent(), sett(v)} */
  function sokeFelter() {
    var ut = [];
    function f(obj, nokkel, id, html) { ut.push({ id: id, html: !!html, hent: function () { return obj[nokkel] || ""; }, sett: function (v) { obj[nokkel] = v; } }); }
    f(S.dok, "tittel", null); f(S.dok, "ingress", null); f(S.dok, "rom", null); f(S.dok, "godkjent", null); f(S.dok.melding, "tekst", null);
    S.dok.blokker.forEach(function (b) {
      var d = b.data;
      f(b, "tittel", b.id);
      if (b.type === "tekst") { f(d, "html", b.id, true); }
      else if (b.type === "viktig") { f(d, "tekst", b.id); }
      else if (b.type === "program") { d.dager.forEach(function (dag) { f(dag, "tittel", b.id); dag.punkter.forEach(function (p) { f(p, "tema", b.id); f(p, "sted", b.id); f(p, "hvem", b.id); }); }); }
      else if (b.type === "filer") { d.filer.forEach(function (e) { f(e, "tittel", b.id); }); }
      else if (b.type === "lenker") { d.lenker.forEach(function (l) { f(l, "tittel", b.id); f(l, "tekst", b.id); }); }
      else if (b.type === "tabell") { d.kolonner.forEach(function (_k, i) { f(d.kolonner, i, b.id); }); d.rader.forEach(function (r) { r.forEach(function (_c, i) { f(r, i, b.id); }); }); }
      else if (b.type === "kontakt") { d.personer.forEach(function (p) { ["navn", "rolle", "telefon", "epost"].forEach(function (n) { f(p, n, b.id); }); }); }
      else if (b.type === "bilde") { f(d, "alt", b.id); f(d, "tekst", b.id); }
    });
    return ut;
  }
  function telleTreff(re, tekst) { var m = tekst.match(re); return m ? m.length : 0; }
  function erstattIHtml(html, re, ny) {
    var doc = new DOMParser().parseFromString("<body>" + html + "</body>", "text/html"), antall = 0;
    var walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT), noder = [];
    while (walker.nextNode()) { noder.push(walker.currentNode); }
    noder.forEach(function (n) { var t = n.nodeValue; n.nodeValue = t.replace(re, function () { antall++; return ny; }); });
    return { html: antall ? KS.rtTilHtml(doc.body) : html, antall: antall };
  }
  function treffIFelt(felt_, re) { return felt_.html ? telleTreff(re, KS.tekstAvHtml(felt_.hent())) : telleTreff(re, String(felt_.hent())); }
  var finnPeker = -1;
  function oppdaterFinn() {
    var d = dlg("finn"), re = regex(d), teller = felt(d, "teller");
    finnPeker = -1;
    if (!re) { teller.textContent = "Skriv det du vil finne."; return; }
    var antall = 0, blokker = {};
    sokeFelter().forEach(function (x) { var n = treffIFelt(x, re); if (n) { antall += n; blokker[x.id || "_topp"] = true; } });
    var nb = Object.keys(blokker).length;
    teller.textContent = antall ? antall + (antall === 1 ? " treff i " : " treff i ") + KS.flertall(nb, "blokk", "blokker") : "Ingen treff.";
  }
  function finnNeste() {
    var d = dlg("finn"), re = regex(d);
    if (!re) { return; }
    var ider = [];
    sokeFelter().forEach(function (x) { if (treffIFelt(x, re)) { var id = x.id || "_topp"; if (ider.indexOf(id) < 0) { ider.push(id); } } });
    if (!ider.length) { return; }
    finnPeker = (finnPeker + 1) % ider.length;
    var id = ider[finnPeker];
    if (id === "_topp") { var t = KS.$(".ks-topp"); t.scrollIntoView({ block: "center", behavior: "smooth" }); return; }
    KS.apneBlokk(id);
    setTimeout(function () { var k = KS.kortFor(id); if (k) { k.classList.remove("treff"); void k.offsetWidth; k.classList.add("treff"); } }, 250);
  }
  function erstattAlle() {
    var d = dlg("finn"), re = regex(d), ny = felt(d, "ny").value;
    if (!re) { return; }
    KS.snapshot();
    var antall = 0, blokker = {};
    sokeFelter().forEach(function (x) {
      var gammel = String(x.hent()), nyVerdi, n;
      if (x.html) { var r = erstattIHtml(gammel, re, ny); nyVerdi = r.html; n = r.antall; }
      else { n = 0; nyVerdi = gammel.replace(re, function () { n++; return ny; }); }
      if (n) { x.sett(nyVerdi); antall += n; blokker[x.id || "_topp"] = true; }
    });
    if (!antall) { KS.toast("Ingen treff å erstatte.", {}); return; }
    KS.endret({ struktur: true });
    KS.tegnAlt();
    felt(d, "teller").textContent = "Erstattet " + antall + ".";
    KS.toast(antall + (antall === 1 ? " erstatning" : " erstatninger") + " i " + KS.flertall(Object.keys(blokker).length, "blokk", "blokker") + ".", { angre: KS.angre });
    oppdaterFinn();
  }
  KS.apneFinn = function () {
    var d = dlg("finn");
    if (!d.open) { d.__opener = document.activeElement; d.show(); }
    var sel = window.getSelection ? window.getSelection().toString() : "";
    var inp = felt(d, "sok");
    if (sel && sel.length < 80 && sel.indexOf("\n") < 0 && !inp.value) { inp.value = sel; }
    inp.focus(); inp.select(); oppdaterFinn();
  };

  /* ================= lenke i rik tekst ================= */
  KS.lenkeDialog = function (opt) {
    var d = dlg("lenke"), adresse = felt(d, "adresse"), tekst = felt(d, "tekst"), feil = felt(d, "feil");
    adresse.value = ""; tekst.value = ""; feil.hidden = true;
    felt(d, "tekstfelt").hidden = !!opt.tekst;
    d.__opt = opt; d.__ok = false;
    apne(d);
    adresse.focus();
  };
  function lenkeOk() {
    var d = dlg("lenke"), adresse = felt(d, "adresse"), feil = felt(d, "feil"), opt = d.__opt;
    var url = KS.trygUrl(adresse.value);
    if (!url) { feil.textContent = "Adressen må starte med https://, http://, mailto: eller tel:."; feil.hidden = false; adresse.setAttribute("aria-invalid", "true"); adresse.focus(); return; }
    adresse.removeAttribute("aria-invalid");
    d.__ok = true; d.close();
    opt.ved(url, felt(d, "tekst").value.trim());
  }

  /* ================= redigering som tekst ================= */
  KS.tekstDialog = function (opt) {
    var d = dlg("tekst");
    felt(d, "tittel").textContent = opt.tittel;
    felt(d, "hjelp").textContent = opt.hjelp;
    felt(d, "ledetekst").textContent = opt.ledetekst;
    felt(d, "tekst").value = opt.tekst;
    rens(felt(d, "feil"));
    d.__opt = opt;
    apne(d);
    felt(d, "tekst").focus();
  };
  function tekstOk() {
    var d = dlg("tekst"), opt = d.__opt, ta = felt(d, "tekst"), feilEl = rens(felt(d, "feil"));
    var feil = opt.sjekk(ta.value);
    if (feil.length) { feil.forEach(function (f) { feilEl.appendChild(KS.el("li", { tekst: f })); }); return; }
    d.close();
    opt.ok(ta.value);
  }

  /* ================= innliming fra Excel ================= */
  KS.excelDialog = function (opt) {
    var d = dlg("excel"), feil = felt(d, "feil");
    felt(d, "tekst").value = ""; feil.hidden = true;
    d.__opt = opt;
    apne(d);
    felt(d, "tekst").focus();
  };
  function excelOk() {
    var d = dlg("excel"), feil = felt(d, "feil"), tekst = felt(d, "tekst").value;
    if (!tekst.trim()) { feil.textContent = "Lim inn cellene fra Excel først."; feil.hidden = false; return; }
    var svar = d.__opt.ok(tekst, felt(d, "overskrifter").checked);
    if (svar) { feil.textContent = svar; feil.hidden = false; return; }
    d.close();
  }

  /* ================= deltakernavn til gruppetabellen ================= */
  KS.navnDialog = function (opt) {
    var d = dlg("navn"), status = felt(d, "status"), liste = rens(felt(d, "liste")), ok = KS.ks("navn-ok", d);
    ok.disabled = true; status.textContent = "Henter navn …"; d.__opt = opt;
    apne(d);
    KS.hent(D.urls.deltakernavn).then(function (r) {
      if (r.utenJson) { KS.loggetUt(); d.close(); return; }
      if (r.status !== 200) { status.textContent = "Du har ikke tilgang til å hente navn."; return; }
      var navn = r.json.navn || [];
      status.textContent = navn.length ? "Kryss av de som skal med." : "Ingen deltakere med bekreftet påmelding ennå.";
      navn.forEach(function (n) {
        var cb = KS.el("input", { attr: { type: "checkbox", value: n } });
        cb.addEventListener("change", function () { ok.disabled = !KS.$$("input:checked", liste).length; });
        liste.appendChild(KS.el("label", { barn: [cb, KS.el("span", { tekst: n })] }));
      });
    });
  };
  function navnOk() {
    var d = dlg("navn"), valgt = KS.$$("input:checked", felt(d, "liste")).map(function (c) { return c.value; });
    if (!valgt.length) { return; }
    d.close();
    d.__opt.ok(valgt);
  }

  /* ================= oppkobling ================= */
  KS.init.push(function () {
    ["publiser", "konflikt", "lenke", "tekst", "excel", "navn", "mal", "hent", "forh-stor", "versjoner", "innstillinger", "finn", "filer"].forEach(function (n) { forbered(dlg(n)); });
    dlg("lenke").addEventListener("close", function () { var d = dlg("lenke"); if (!d.__ok && d.__opt && d.__opt.tilbake) { d.__opt.tilbake(); } });
    dlg("finn").addEventListener("keydown", function (e) { if (e.key === "Escape") { e.preventDefault(); dlg("finn").close(); } });
    KS.ks("publiser-nå", dlg("publiser")).addEventListener("click", publiserNa);
    KS.ks("lenke-ok", dlg("lenke")).addEventListener("click", lenkeOk);
    dlg("lenke").addEventListener("keydown", function (e) { if (e.key === "Enter" && e.target.tagName === "INPUT") { e.preventDefault(); lenkeOk(); } });
    KS.ks("tekst-ok", dlg("tekst")).addEventListener("click", tekstOk);
    KS.ks("excel-ok", dlg("excel")).addEventListener("click", excelOk);
    KS.ks("navn-ok", dlg("navn")).addEventListener("click", navnOk);
    KS.ks("mal-ok", dlg("mal")).addEventListener("click", malOk);
    KS.ks("hent-ok", dlg("hent")).addEventListener("click", hentOk);
    felt(dlg("hent"), "kurs").addEventListener("change", hentValg);
    KS.ks("innst-ok", dlg("innstillinger")).addEventListener("click", innstillingerOk);
    var di = dlg("innstillinger");
    APNING.forEach(function (m) { felt(di, "apning-" + m).addEventListener("change", oppdaterApningHjelp); });
    felt(di, "apen-dager").addEventListener("input", function () { velgApning(di, "dager"); });
    felt(di, "stenges").addEventListener("input", function () { velgApning(di, "dato"); });
    konfliktValg();
    var fd = dlg("finn");
    felt(fd, "sok").addEventListener("input", oppdaterFinn);
    felt(fd, "ikke-skill").addEventListener("change", oppdaterFinn);
    KS.ks("finn-neste", fd).addEventListener("click", finnNeste);
    KS.ks("finn-erstatt", fd).addEventListener("click", erstattAlle);
    KS.ks("versjoner").addEventListener("click", function () { KS.apneVersjoner(); });
    var mal = KS.ks("mal"), hent = KS.ks("hent-fra"), finn = KS.ks("finn"), innst = KS.ks("innstillinger"), innstKode = KS.ks("innstillinger-kode");
    if (mal) { mal.addEventListener("click", KS.apneMal); }
    if (hent) { hent.addEventListener("click", apneHent); }
    var hentAlle = KS.ks("hent-alle"), hentIngen = KS.ks("hent-ingen");
    if (hentAlle) { hentAlle.addEventListener("click", function () { hentVelgAlle(true); }); }
    if (hentIngen) { hentIngen.addEventListener("click", function () { hentVelgAlle(false); }); }
    var stor = KS.ks("forh-stor"), storLast = KS.ks("forh-stor-last");
    if (stor) { stor.addEventListener("click", apneForhStor); }
    if (storLast) { storLast.addEventListener("click", lastForhStor); }
    if (finn) { finn.addEventListener("click", function () { KS.apneFinn(); }); }
    if (innst) { innst.addEventListener("click", function () { KS.apneInnstillinger(false); }); }
    if (innstKode) { innstKode.addEventListener("click", function () { KS.apneInnstillinger(true); }); }
  });
})();
