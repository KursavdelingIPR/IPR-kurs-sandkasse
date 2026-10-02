/* Oppmøtelisten (admin_oppmoteliste.html). Uten dette skriptet fungerer siden med vanlige skjemaknapper (hver rad er et skjema, og
 * «Merk alle» og «Fjern alle» går via en bekreftelsesside). Med skriptet:
 *   - trykk på en rad veksler oppmøtet uten å laste siden på nytt (fetch med CSRF-hode). Siden ber om en TILSTAND («til» eller «fra»),
 *     ikke «bytt», så samme forespørsel to ganger gir samme resultat. Trykkes en rad flere ganger raskt, sendes én forespørsel om gangen
 *     per rad og siste trykk vinner. Feiler det, går raden tilbake og feilen står tydelig nederst (og i raden).
 *   - oppmøte fra Zoom fjernes bare etter en ekstra bekreftelse (serveren krever det også: bekreft_zoom=1)
 *   - søkefeltet filtrerer listen mens man skriver (navn og e-post, uten hensyn til store/små bokstaver og æøå; alle ordene må treffe)
 *   - «Merk alle til stede» og «Fjern alle merkede» spør først. De er avslått mens søket er i bruk, så de aldri gjelder «bare de viste».
 *   - listen oppdateres av seg selv hvert 20. sekund (noen kan ha skannet QR-koden), og telleren følger radene.
 * All tekst fra tjeneren settes med textContent - aldri innerHTML. Ingen persondata i adresser (bare POST-kropp og id-er).
 */
(function () {
  "use strict";
  var rot = document.querySelector("[data-oppmoteliste]");
  if (!rot) return;

  var csrf = rot.getAttribute("data-csrf") || "";
  var radUrl = rot.getAttribute("data-rad-url");
  var alleUrl = rot.getAttribute("data-alle-url");
  var statusUrl = rot.getAttribute("data-status-url");
  var liste = document.getElementById("opp-liste");
  var rader = liste ? Array.prototype.slice.call(liste.querySelectorAll("[data-rad]")) : [];
  var tallAntall = document.getElementById("opp-antall");
  var tallTotalt = document.getElementById("opp-totalt");
  var feilBoks = document.getElementById("opp-feil");
  var meldingBoks = document.getElementById("opp-melding");
  var sokBoks = document.getElementById("opp-sok-boks");
  var sokFelt = document.getElementById("opp-sok");
  var sokStatus = document.getElementById("opp-sokstatus");
  var ingenTreff = document.getElementById("opp-ingen-treff");
  var alleKnapper = Array.prototype.slice.call(document.querySelectorAll("[data-oppmote-alle]"));
  var alleHint = document.getElementById("opp-alle-hint");
  var endretMelding = document.getElementById("opp-endret");
  var frakoblet = document.getElementById("opp-frakoblet");
  var meldingTidtaker = null;

  // ------------------------------------------------------------------ hjelpere

  function settTekst(el, tekst) { if (el) el.textContent = tekst; }
  function erTil(rad) { return rad.getAttribute("data-tilstede") === "1"; }
  function navnI(rad) { var n = rad.querySelector(".opp-navn"); return n ? n.textContent : ""; }

  function vis(rad, til, kilde, hvordan) {                       // tegner tilstanden i raden
    rad.setAttribute("data-tilstede", til ? "1" : "0");
    rad.setAttribute("data-kilde", kilde || "");
    var knapp = rad.querySelector("button.opp-knapp");
    if (knapp) knapp.setAttribute("aria-pressed", til ? "true" : "false");
    var onsket = rad.querySelector("input[name=onsket]");
    if (onsket) onsket.value = til ? "fra" : "til";
    settTekst(rad.querySelector(".opp-merketekst"), til ? "Til stede" : "Ikke registrert");
    settTekst(rad.querySelector(".opp-info"), hvordan || "");
  }

  function oppdaterTeller() {
    var n = rader.filter(erTil).length;
    settTekst(tallAntall, String(n));
    settTekst(tallTotalt, String(rader.length));
  }

  function skjulMelding() { if (meldingBoks) meldingBoks.hidden = true; }
  function melding(tekst) {                                       // kort bekreftelse nederst («5 merket som til stede.»)
    if (!meldingBoks) return;
    meldingBoks.textContent = tekst;
    meldingBoks.hidden = false;
    clearTimeout(meldingTidtaker);
    meldingTidtaker = setTimeout(skjulMelding, 4000);
  }
  function feil(tekst, rad) {                                     // en feil står til neste vellykkede lagring
    if (feilBoks) { feilBoks.textContent = tekst; feilBoks.hidden = false; }
    if (rad) {
      rad.setAttribute("data-feil", "1");
      var p = rad.querySelector(".opp-radfeil");
      if (p) { p.textContent = "Ikke lagret. Prøv igjen."; p.hidden = false; }
    }
  }
  function fjernFeil(rad) {
    if (rad) {
      rad.removeAttribute("data-feil");
      var p = rad.querySelector(".opp-radfeil");
      if (p) { p.hidden = true; p.textContent = ""; }
    }
    if (feilBoks && !rader.some(function (r) { return r.getAttribute("data-feil") === "1"; })) feilBoks.hidden = true;
  }

  // Feilteksten. `navn` er personen det gjelder; tom når det gjaldt hele listen («Merk alle»).
  function feiltekst(status, data, navn) {
    var ikkeLagret = navn ? " Oppmøtet for " + navn + " er ikke lagret." : " Oppmøtet er ikke endret.";
    if (status === 0) return "Fikk ikke kontakt med tjeneren. Sjekk nettforbindelsen og prøv igjen." + ikkeLagret;
    if (status === 401) return "Du er logget ut. Last siden på nytt og logg inn igjen." + ikkeLagret;
    if (status === 403) return "Du har ikke tilgang til å endre oppmøte.";
    if (status === 400 && !(data && data.melding)) return "Siden har vært åpen for lenge. Last siden på nytt og prøv igjen." + ikkeLagret;
    if (data && data.melding) return data.melding + (navn ? " (" + navn + ")" : "");
    return "Noe gikk galt (feil " + status + ")." + ikkeLagret;
  }

  // Ett kall mot tjeneren. Svarer alltid med {status, data}; en nettfeil gir status 0.
  function kall(url, metode, felt) {
    var valg = { method: metode, credentials: "same-origin", cache: "no-store",
                 headers: { "Accept": "application/json", "X-CSRF-Token": csrf } };
    if (felt) {
      var kropp = new URLSearchParams();
      Object.keys(felt).forEach(function (k) { kropp.set(k, felt[k]); });
      kropp.set("csrf_token", csrf);
      valg.body = kropp.toString();
      valg.headers["Content-Type"] = "application/x-www-form-urlencoded";
    }
    return fetch(url, valg).then(function (svar) {
      return svar.json().then(function (d) { return { status: svar.status, data: d }; },
                              function () { return { status: svar.status, data: null }; });
    }, function () { return { status: 0, data: null }; });
  }

  // ------------------------------------------------------------------ en rad om gangen, siste trykk vinner

  rader.forEach(function (rad) {
    rad._server = { til: erTil(rad), kilde: rad.getAttribute("data-kilde") || "", hvordan: (rad.querySelector(".opp-info") || {}).textContent || "" };
    rad._onsket = null;         // ønsket tilstand som ikke er sendt ennå (true = til stede)
    rad._sender = false;
    rad._bekreftZoom = false;
  });

  function visServerTilstand(rad) { vis(rad, rad._server.til, rad._server.kilde, rad._server.hvordan); }

  function send(rad) {
    var onsket = rad._onsket;
    rad._onsket = null;
    rad._sender = true;
    rad.setAttribute("aria-busy", "true");
    var felt = { paamelding_id: rad.getAttribute("data-pid"), onsket: onsket ? "til" : "fra" };
    if (rad._bekreftZoom) felt.bekreft_zoom = "1";
    kall(radUrl, "POST", felt).then(function (r) {
      rad._sender = false;
      rad.removeAttribute("aria-busy");
      if (r.status === 200 && r.data && r.data.status === "ok") {
        rad._server = { til: !!r.data.til_stede, kilde: r.data.kilde || "", hvordan: r.data.hvordan || "" };
        rad._bekreftZoom = false;
        fjernFeil(rad);
        if (rad._onsket !== null && rad._onsket !== rad._server.til) { send(rad); return; }     // et nyere trykk venter: send det
        rad._onsket = null;
        visServerTilstand(rad);
        oppdaterTeller();
        return;
      }
      if (r.status === 409 && r.data && r.data.status === "trenger_bekreftelse") {              // raden er fra Zoom (siden var utdatert)
        if (window.confirm(zoomSporsmal(navnI(rad)))) { rad._bekreftZoom = true; rad._onsket = false; send(rad); return; }
        rad._onsket = null;
        visServerTilstand(rad);
        oppdaterTeller();
        return;
      }
      rad._onsket = null;                                        // feilet: raden går tilbake til det tjeneren sist bekreftet
      visServerTilstand(rad);
      oppdaterTeller();
      feil(feiltekst(r.status, r.data, navnI(rad)), rad);
    });
  }

  function zoomSporsmal(navn) {
    return "Oppmøtet for " + navn + " er hentet fra Zoom. Vil du fjerne det likevel?\n\nDagen teller da ikke som oppmøte (det kan påvirke kursbeviset).";
  }

  function trykk(rad) {
    var onsket = !erTil(rad);
    if (!onsket && rad.getAttribute("data-kilde") === "zoom" && !window.confirm(zoomSporsmal(navnI(rad)))) return;
    if (!onsket && rad.getAttribute("data-kilde") === "zoom") rad._bekreftZoom = true;
    fjernFeil(rad);
    skjulMelding();
    vis(rad, onsket, onsket ? "manuell" : "", onsket ? "Lagrer …" : "");       // vises med en gang; tjenerens svar er fasit
    oppdaterTeller();
    rad._onsket = onsket;
    if (!rad._sender) send(rad);
  }

  document.addEventListener("submit", function (e) {
    var skjema = e.target;
    if (!(skjema instanceof Element) || !skjema.hasAttribute("data-rad-skjema")) return;
    e.preventDefault();
    var rad = skjema.closest("[data-rad]");
    if (rad) trykk(rad);
  });

  // ------------------------------------------------------------------ tilstand fra tjeneren (alle-knappene og automatisk oppdatering)

  function bruk(data) {
    var sett = {};
    var endret = false;
    rader.forEach(function (rad) {
      var s = data.rader && data.rader[rad.getAttribute("data-pid")];
      sett[rad.getAttribute("data-pid")] = true;
      if (!s) { endret = true; return; }
      if (rad._sender || rad._onsket !== null) return;           // et trykk er på vei: ikke overskriv det
      rad._server = { til: !!s.til_stede, kilde: s.kilde || "", hvordan: s.hvordan || "" };
      visServerTilstand(rad);
    });
    Object.keys(data.rader || {}).forEach(function (pid) { if (!sett[pid]) endret = true; });
    if (endretMelding) endretMelding.hidden = !endret;
    oppdaterTeller();
  }

  var oppdaterer = false, feilISvar = 0;
  function oppdater() {
    if (oppdaterer || rader.some(function (r) { return r._sender || r._onsket !== null; }) || rot.hasAttribute("data-opptatt")) return;
    oppdaterer = true;
    kall(statusUrl, "GET").then(function (r) {
      oppdaterer = false;
      if (r.status === 200 && r.data && r.data.rader) { feilISvar = 0; if (frakoblet) frakoblet.hidden = true; bruk(r.data); return; }
      feilISvar += 1;
      if (feilISvar >= 2 && frakoblet) frakoblet.hidden = false;
    });
  }
  if (rader.length) {
    setInterval(function () { if (document.visibilityState === "visible") oppdater(); }, 20000);
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") oppdater(); });
  }
  window.addEventListener("pageshow", function (e) { if (e.persisted) { oppdater(); filtrer(); } });

  // ------------------------------------------------------------------ Merk alle / Fjern alle

  function alleSporsmal(handling) {
    var tilStede = rader.filter(erTil);
    if (handling === "merk_alle") {
      var n = rader.length - tilStede.length;
      if (!n) return { tekst: null, ingen: "Alle er allerede registrert som til stede." };
      return { tekst: "Merke " + (n === 1 ? "den ene som ikke er registrert" : "alle " + n + " som ikke er registrert") + " som til stede?\n\nDe som allerede er registrert, endres ikke." };
    }
    var zoom = tilStede.filter(function (r) { return r.getAttribute("data-kilde") === "zoom"; }).length;
    var fjernes = tilStede.length - zoom;
    if (!fjernes) return { tekst: null, ingen: zoom ? "Bare oppmøte fra Zoom er registrert. Det fjernes én og én." : "Ingen registreringer å fjerne." };
    return { tekst: "Fjerne oppmøtet for " + (fjernes === 1 ? "én deltaker" : fjernes + " deltakere") + "?" +
                    (zoom ? "\n\n" + zoom + " fra Zoom beholdes (de fjernes én og én)." : "") };
  }

  function kjorAlle(handling) {
    if (rader.some(function (r) { return r._sender || r._onsket !== null; })) { melding("Vent til det du nettopp trykket er lagret."); return; }
    var sp = alleSporsmal(handling);
    if (!sp.tekst) { melding(sp.ingen); return; }
    if (!window.confirm(sp.tekst)) return;
    rot.setAttribute("data-opptatt", "1");
    kall(alleUrl, "POST", { handling: handling, bekreft: "1" }).then(function (r) {
      rot.removeAttribute("data-opptatt");
      if (r.status === 200 && r.data && r.data.status === "ok") {
        rader.forEach(function (rad) { fjernFeil(rad); });
        bruk(r.data);
        melding(r.data.melding || "Ferdig.");
        return;
      }
      feil(feiltekst(r.status, r.data, ""), null);
    });
  }

  alleKnapper.forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      if (a.getAttribute("aria-disabled") === "true") return;
      kjorAlle(a.getAttribute("data-oppmote-alle"));
    });
  });

  // ------------------------------------------------------------------ søk (samme regel som kurs/oppmoteliste.py: treffer)

  var ASCII = { "ø": "o", "æ": "ae", "å": "a", "ð": "d", "þ": "th", "ł": "l", "đ": "d" };
  function fold(s) { return String(s || "").normalize("NFC").toLowerCase().replace(/ß/g, "ss"); }
  function utenAksent(s) {                                        // «Sølvi» -> «solvi», «Håkon» -> «hakon» (som deltakersok._ascii)
    var ut = "";
    fold(s).split("").forEach(function (t) {
      var c = ASCII[t] !== undefined ? ASCII[t] : t;
      ut += c.normalize("NFD").replace(/[̀-ͯ]/g, "");
    });
    return ut;
  }
  function sokeord(tekst) { return fold(tekst.slice(0, 100)).split(/\s+/).filter(Boolean).slice(0, 6); }
  function treffer(rad, ord) {
    var a = rad.getAttribute("data-sok") || "", b = rad.getAttribute("data-sok-ascii") || "";
    return ord.every(function (o) { return a.indexOf(o) >= 0 || b.indexOf(utenAksent(o)) >= 0; });
  }

  function filtrer() {
    if (!sokFelt) return;
    var ord = sokeord(sokFelt.value);
    var synlige = 0;
    rader.forEach(function (rad) { var vis_ = treffer(rad, ord); rad.hidden = !vis_; if (vis_) synlige += 1; });
    if (ingenTreff) ingenTreff.hidden = !(ord.length && rader.length && synlige === 0);
    settTekst(sokStatus, ord.length ? (synlige === 0 ? "Ingen treff." : "Viser " + synlige + " av " + rader.length + ".") : "");
    var sokAktiv = ord.length > 0;                                 // «alle»-knappene gjelder alltid alle: ikke mens listen er filtrert
    alleKnapper.forEach(function (a) {
      if (sokAktiv) { a.setAttribute("aria-disabled", "true"); a.setAttribute("tabindex", "-1"); }
      else { a.removeAttribute("aria-disabled"); a.removeAttribute("tabindex"); }
    });
    if (alleHint) alleHint.hidden = !sokAktiv;
  }

  if (sokBoks && sokFelt) {
    sokBoks.hidden = false;                                        // søket vises bare når skriptet virker
    sokFelt.addEventListener("input", filtrer);
    sokFelt.addEventListener("search", filtrer);
    sokFelt.addEventListener("keydown", function (e) { if (e.key === "Escape" && sokFelt.value) { sokFelt.value = ""; filtrer(); } });
    filtrer();
  }
  oppdaterTeller();
})();
