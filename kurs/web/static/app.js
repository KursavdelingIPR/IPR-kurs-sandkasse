/* Felles, liten hjelper for admin og offentlige sider. Ingen rammeverk.
 * All oppførsel styres av data-attributter i HTML - aldri inline-JS (CSP), og aldri tekst fra databasen inne i JS.
 *
 *   <form data-bekreft="Melde av?">            spør før innsending (teksten er ren tekst, satt av Jinja med escaping)
 *   <select data-send-ved-endring>             sender skjemaet når verdien endres
 *   <input type=radio data-vis="org" ...>      viser elementet med id=org når valgt, data-skjul="org" skjuler det
 *   <select data-skjul-hvis="samlet" data-mal="delfelt">   skjuler #delfelt når verdien er 'samlet'
 *   <form data-en-gang>                        knappen låses etter første innsending (dobbeltklikk-vern)
 *   <form data-status-bekreft>                 «Endre status til X?» der X er det valgte navnet i select[name=status]; har
 *                                              den valgte option en egen data-bekreft, brukes den teksten i stedet. Har skjemaet
 *                                              data-status-tekster (JSON: én tekst per ny status, deltakervinduet), brukes den
 *   <form data-status-bekreft data-full-bekreft="…">   kurset er fullt: velges Påmeldt (den eneste statusen som tar en plass;
 *                                              Ekstradeltaker tar ingen), spørres det med denne teksten i stedet, og bare ved ja
 *                                              settes input[name=overbooking] til 1
 *   <form data-status-bekreft data-ekstra-steg>   kurset har flere samlinger: velges Ekstradeltaker, spørres det ikke her - serveren
 *                                              sender videre til steget for samlingsvalg (med egen forklaring) før noe endres
 *   <fieldset data-samlingsvalg>               samlingsvalget for ekstradeltaker: krysses en samling av, velges «Bare utvalgte
 *                                              samlinger» av seg selv, og «Hele kurset» fjerner avkrysningene; i «Legg til deltaker»
 *                                              krysses «Registrer som ekstradeltaker» av samtidig. Uten JavaScript må valget gjøres
 *                                              for hånd, og serveren avviser motstridende valg (aldri en stille tolkning)
 *   <form data-radstatus data-naa="paameldt">  statusvelgeren i hver rad i deltakerlisten (i tillegg til data-status-bekreft):
 *                                              «Endre» vises først når en annen status er valgt, ingenting sendes uten at
 *                                              knappen trykkes, og input[name=neste] settes til listen slik den står nå
 *                                              (søk og statusvalg), så admin kommer tilbake til den. Ruten sender tilbake til
 *                                              raden (adressen slutter på #rad-<id>): siden hopper dit, meldingene legges over
 *                                              raden, og avkryssede rader huskes over omlastingen
 *   <button type=button data-skriv-ut>         åpner nettleserens utskrift (der kan man også velge «Lagre som PDF»)
 *   <div data-ulagret-vakt>                    rundt flere skjema med data-skjemanavn="…": advarer før ett av dem sendes (eller
 *                                              vinduet lukkes) når et ANNET har endringer som ellers går tapt
 *   <a data-deltaker-vindu href="…">           åpner deltakersiden i vinduet <dialog id="deltaker-vindu"> over listen
 *                                              (uten JS, eller med Ctrl/Cmd-klikk, er det en vanlig lenke)
 *   <a data-i-vindu href="…">                  inne i deltakervinduet: siden lastes i vinduet (f.eks. én e-post)
 *   <button type=button data-sett-inn="{fornavn}" data-felt="f-tekst">   flettefelt: setter teksten inn der markøren
 *                                              står i feltet (eller erstatter det som er markert)
 *   <form data-deltakerfilter>                 deltakerlisten: søket (input[name=sok]) og statusvalgene (input[name=status])
 *                                              viser/skjuler tr[data-filtrer] etter data-sok og data-status mens man skriver
 *   <form data-deltakersok data-json="/admin/sok.json" data-csrf="...">   globalt deltakersøk (Oversikten og
 *                                              søkesiden): rullegardin med de beste treffene mens man skriver (POST, teksten
 *                                              i kroppen og ikke i adressen), og «/» hopper til søkefeltet (fra andre adminsider
 *                                              til Oversikten, adressen står i header[data-sok-adresse])
 *   <form data-kursskjema>                     kursskjemaet (Opprett kurs og Oppsett, se templates/_kursskjema.html): format,
 *                                              betaling, samlinger ([data-samlinger]), «Vis dager» og påmeldingsfristen. I Oppsett for
 *                                              et kurs med fakturaer også avkryssingen som bekrefter endret pris eller fakturaoppsett
 *                                              ([data-okonomi-bekreft]: vises først når et felt i [data-okonomi] er endret)
 *   <div data-vis-naar="navn" data-vis-verdi="v">  påmeldingsskjemaet: vises bare når feltet «navn» har verdien v; ellers
 *                                              skjult, og feltene i den er deaktivert (sendes ikke, krav gjelder ikke)
 *   <div data-enhetsoppslag="/kurs/KODE/enhet">    organisasjonsnummer: søk og oppslag i Brønnøysundregistrene, fyller
 *                                              input[data-enhet] (firmanavn, adresse, postnummer, poststed)
 *   <button type=button data-kopier="id">      kopierer verdien i feltet med id (f.eks. lenken til påmeldingsskjemaet)
 *   <form data-skjemabygger>                   skjemabyggeren: ▲▼ (data-flytt) flytter feltet innenfor sin del, «Mer»
 *                                              (data-mer="id") åpner/lukker detaljene
 *   <form data-adressegruppe> med input[data-adressefelt]   deltakervinduet, person uten komplett adresse: adressen er ikke
 *                                              påkrevd som helhet, men endres den og er noe fylt ut, blir alle tre feltene påkrevd
 *   <article data-sjekkrad> med <a data-sjekk-veksle aria-controls="id">   kursoversikten i Kalender: pila (eller et klikk
 *                                              på raden) folder sjekklisten (#id) ut og inn; uten JavaScript er pila en lenke
 *   <span data-varsel title="tekst">            rødt utropstegn i kursoversikten: teksten vises i en boble (.varselboble)
 *                                              når musa er over tegnet eller det har fokus; Esc skjuler den
 *   <td class="kh-celle"><button>             kursholderoversikten: åpner <dialog id="rolle-dialog"> med radens samling/dag
 *                                              (tr[data-samling], [data-dag]) og kolonnens kursholder (th[data-kh])
 *   <button type=button data-lukk-dialog>      lukker <dialog> knappen står i
 */
(function () {
  "use strict";

  // Navnene på skjema i området som har endringer som ikke er lagret (unntatt `unntatt`), f.eks. "Personopplysninger".
  function ulagredeSkjema(omraade, unntatt) {
    if (!omraade) return "";
    var navn = [];
    omraade.querySelectorAll("form[data-skjemanavn]").forEach(function (f) {
      if (f !== unntatt && f.dataset.endret) navn.push(f.getAttribute("data-skjemanavn"));
    });
    return navn.join(", ");
  }

  function merkEndret(e) {
    var el = e.target;
    if (el && el.form && el.form.hasAttribute("data-skjemanavn")) el.form.dataset.endret = "1";
  }
  document.addEventListener("input", merkEndret);
  document.addEventListener("change", merkEndret);

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute("data-ingen-innsending")) { e.preventDefault(); return; }
    if (form.hasAttribute("data-en-gang") && form.dataset.sendt) { e.preventDefault(); return; }
    var tekst = form.getAttribute("data-bekreft");
    var overbooking = form.querySelector("input[name=overbooking]");
    var fulltSpoersmaal = false;
    if (form.hasAttribute("data-status-bekreft")) {
      var valg = form.querySelector("select[name=status]");
      // Bare Påmeldt tar en plass (data-full-bekreft står bare på de som ikke er Påmeldt fra før). Ekstradeltaker tar ingen plass.
      fulltSpoersmaal = form.hasAttribute("data-full-bekreft") && !!valg && valg.value === "paameldt";
      var valgtAlt = valg && valg.selectedIndex >= 0 ? valg.options[valg.selectedIndex] : null;
      var valgtNavn = valgtAlt ? valgtAlt.text.toLowerCase() : "";
      var altTekst = valgtAlt ? valgtAlt.getAttribute("data-bekreft") : "";      // i listen: hva som skjer med akkurat denne
      if (!altTekst && valg && form.hasAttribute("data-status-tekster")) {       // i deltakervinduet: én tekst per ny status (JSON)
        try { altTekst = JSON.parse(form.getAttribute("data-status-tekster"))[valg.value] || ""; } catch (feil) { altTekst = ""; }
      }
      tekst = fulltSpoersmaal ? form.getAttribute("data-full-bekreft") : (altTekst || "Endre status til " + valgtNavn + "?");
      if (form.hasAttribute("data-ekstra-steg") && valg && valg.value === "ekstradeltaker") tekst = "";   // samlingsvalg-steget forklarer og spør
      if (overbooking) overbooking.value = "";
      if (form.hasAttribute("data-radstatus") && valg && valg.value === form.getAttribute("data-naa")) {
        e.preventDefault();                                          // samme status som nå: ingenting å sende
        return;
      }
    }
    if (tekst && !window.confirm(tekst)) {
      e.preventDefault();
      if (form.hasAttribute("data-radstatus")) {                   // avbrutt: raden viser fortsatt den gamle statusen
        var tilbake = form.querySelector("select[name=status]");
        if (tilbake) { tilbake.value = form.getAttribute("data-naa"); tilbake.dispatchEvent(new Event("change", { bubbles: true })); }
      }
      return;
    }
    if (fulltSpoersmaal && overbooking) overbooking.value = "1";   // admin har svart ja på «Kurset er fullt …»
    var neste = form.hasAttribute("data-radstatus") ? form.querySelector("input[name=neste]") : null;
    if (neste) {
      neste.value = window.location.pathname + window.location.search;   // listen med søk og statusvalg akkurat nå
      lagreValgte();
    }
    var ulagret = ulagredeSkjema(form.closest("[data-ulagret-vakt]"), form);
    if (ulagret && !window.confirm("Du har endringer som ikke er lagret i: " + ulagret +
        ". De går tapt hvis du fortsetter. Vil du fortsette?")) {
      e.preventDefault();
      return;
    }
    if (form.hasAttribute("data-type-sted-vakt")) {
      var harPaameldte = form.getAttribute("data-type-sted-vakt");
      var opprType = form.getAttribute("data-opprinnelig-type") || "";
      var opprSted = form.getAttribute("data-opprinnelig-sted") || "";
      if (harPaameldte !== "0" && (form.type.value !== opprType || form.sted.value !== opprSted)) {
        // Påmeldte og ekstradeltakere hver for seg (tallene kommer fra malen; ordene står i db.PAAMELDINGSSTATUSER)
        var paameldte = form.getAttribute("data-type-sted-paameldte");
        var ekstra = parseInt(form.getAttribute("data-type-sted-ekstra") || "0", 10);
        var hvem = [];
        if (paameldte === null) hvem.push(harPaameldte + " " + (form.getAttribute("data-type-sted-ord") || "") + " deltakere");
        else if (parseInt(paameldte, 10) > 0) hvem.push(paameldte + " " + (form.getAttribute("data-type-sted-ord") || "") + " deltakere");
        if (ekstra > 0) hvem.push(ekstra + " " + (form.getAttribute("data-type-sted-ekstra-ord") || ""));
        if (!window.confirm("Dette kurset har allerede " + hvem.join(" og ") + ". Å endre type eller sted " +
            "kan gjøre informasjon som allerede er sendt til dem (f.eks. Zoom-lenke eller sted) utdatert. Fortsette?")) {
          e.preventDefault();
          return;
        }
      }
    }
    if (form.hasAttribute("data-en-gang")) {
      form.dataset.sendt = "1";
      var knapp = form.querySelector("button:not([type=button])");
      if (knapp) { knapp.disabled = true; knapp.textContent = knapp.getAttribute("data-venter") || "Vent litt …"; }
    }
  });

  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!(el instanceof HTMLElement)) return;
    if (el.hasAttribute("data-send-ved-endring") && el.form) { el.form.submit(); return; }
    if (el.hasAttribute("data-vis")) { var v = document.getElementById(el.getAttribute("data-vis")); if (v) v.classList.remove("skjul"); }
    if (el.hasAttribute("data-skjul")) { var s = document.getElementById(el.getAttribute("data-skjul")); if (s) s.classList.add("skjul"); }
    if (el.hasAttribute("data-skjul-hvis")) {
      var mal = document.getElementById(el.getAttribute("data-mal"));
      if (mal) mal.classList.toggle("skjul", el.value === el.getAttribute("data-skjul-hvis"));
    }
  });

  document.addEventListener("click", function (e) {
    if (e.target instanceof Element && e.target.closest("[data-skriv-ut]")) { e.preventDefault(); window.print(); }
  });

  // Kursoversikten i Kalender: sjekklisten under et planlagt kurs foldes ut og inn med pila til høyre på raden eller et
  // klikk et annet sted på raden. Lenker, knapper og skjema i raden, og alt i selve sjekklisten, gjør det de ellers gjør.
  document.addEventListener("click", function (e) {
    if (!(e.target instanceof Element) || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
    var rad = e.target.closest("[data-sjekkrad]");
    var pil = rad && rad.querySelector("[data-sjekk-veksle]");
    if (!pil) return;
    if (!pil.contains(e.target) && (e.target.closest("a, button, input, select, textarea, label, summary, form, .osjekk, [data-varsel]") ||
                                    String(window.getSelection()).length)) return;
    var panel = document.getElementById(pil.getAttribute("aria-controls"));
    if (!panel) return;
    e.preventDefault();
    var aapne = panel.hidden;
    panel.hidden = !aapne;
    pil.setAttribute("aria-expanded", aapne ? "true" : "false");
    rad.classList.toggle("sk-apen", aapne);
  });

  // Kursoversikten i Kalender: et rødt utropstegn ved navnet (data-varsel) betyr at et annet kurs går samme dag. Teksten vises
  // i en boble under tegnet når musa er over det eller det har fokus (Tab, eller et trykk på mobil), og Esc skjuler den.
  // Boblen ligger rett i <body> med position:fixed, så raden (overflow:hidden) ikke klipper den. Teksten flyttes fra title
  // (nettleserens egen boble, uten JavaScript) til data-varsel, så det ikke kommer to bobler. Den leses opp fra aria-label.
  var varselboble = null, varselEier = null;
  function visVarsel(el) {
    if (el.hasAttribute("title")) { el.setAttribute("data-varsel", el.getAttribute("title")); el.removeAttribute("title"); }
    if (!varselboble) {
      varselboble = document.createElement("div");
      varselboble.className = "varselboble";
      varselboble.setAttribute("aria-hidden", "true");
      document.body.appendChild(varselboble);
    }
    varselboble.textContent = el.getAttribute("data-varsel");
    varselboble.style.display = "block";
    var r = el.getBoundingClientRect(), b = varselboble.getBoundingClientRect();
    var topp = r.bottom + 8;
    if (topp + b.height > window.innerHeight - 8) topp = Math.max(8, r.top - b.height - 8);
    varselboble.style.left = Math.max(8, Math.min(r.left + r.width / 2 - b.width / 2, window.innerWidth - b.width - 8)) + "px";
    varselboble.style.top = topp + "px";
    varselEier = el;
  }
  function skjulVarsel() {
    if (varselboble) varselboble.style.display = "none";
    varselEier = null;
  }
  document.querySelectorAll("[data-varsel][title]").forEach(function (el) {
    el.setAttribute("data-varsel", el.getAttribute("title"));
    el.removeAttribute("title");
  });
  document.addEventListener("mouseover", function (e) {
    var el = e.target instanceof Element && e.target.closest("[data-varsel]");
    if (el && el !== varselEier) visVarsel(el);
  });
  document.addEventListener("mouseout", function (e) {
    var el = e.target instanceof Element && e.target.closest("[data-varsel]");
    if (el && el === varselEier && document.activeElement !== el) skjulVarsel();
  });
  document.addEventListener("focusin", function (e) {
    var el = e.target instanceof Element && e.target.closest("[data-varsel]");
    if (el) visVarsel(el);
    else if (varselEier) skjulVarsel();
  });
  document.addEventListener("focusout", function (e) {
    if (varselEier && e.target === varselEier) skjulVarsel();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && varselEier) skjulVarsel();
  });
  // Ruller siden (også når fokus flytter tegnet inn i bildet), følger boblen med tegnet
  window.addEventListener("scroll", function () { if (varselEier) visVarsel(varselEier); }, true);
  window.addEventListener("resize", function () { if (varselEier) visVarsel(varselEier); });

  // Kursholderoversikten: en rute åpner dialogen med rollen og Psybase-krysset for kursholderen (kolonnen) på samlingen
  // eller veiledningsdagen (raden). Rød skrift i ruta (psy-0) = ikke krysset av for Psybase.
  document.addEventListener("click", function (e) {
    var knapp = e.target instanceof Element && e.target.closest("td.kh-celle > button");
    var dialog = document.getElementById("rolle-dialog");
    if (!knapp || !dialog || typeof dialog.showModal !== "function") return;
    var td = knapp.parentElement, tr = td.parentElement;
    var th = tr.closest("table").tHead.rows[0].cells[td.cellIndex];
    var f = dialog.querySelector("form");
    f.elements.samling_id.value = tr.getAttribute("data-samling");
    f.elements.dag.value = tr.getAttribute("data-dag") || "";
    f.elements.kursholder_id.value = th.getAttribute("data-kh");
    var rolle = knapp.textContent.trim();
    Array.prototype.forEach.call(f.querySelectorAll("input[name=rolle]"), function (r) { r.checked = r.value === rolle; });
    f.elements.psybase.checked = rolle !== "" && !td.classList.contains("psy-0");
    dialog.querySelector("[data-rolle-tittel]").textContent = th.textContent.trim() + " · " + tr.getAttribute("data-tittel");
    dialog.showModal();
  });

  document.addEventListener("click", function (e) {
    var knapp = e.target instanceof Element && e.target.closest("[data-lukk-dialog]");
    var dialog = knapp && knapp.closest("dialog");
    if (dialog) dialog.close();
  });

  // Samlingsvalget for ekstradeltaker: en avkrysset samling betyr «Bare utvalgte samlinger», og «Hele kurset» fjerner avkrysningene
  // (ellers sto det to motstridende valg, som serveren avviser). I «Legg til deltaker» gjelder samlingene bare en ekstradeltaker:
  // velges samlinger, krysses «Registrer som ekstradeltaker» av samtidig.
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!(el instanceof HTMLInputElement)) return;
    var gruppe = el.closest("[data-samlingsvalg]");
    if (!gruppe) return;
    var erSamling = el.type === "checkbox" && el.name === "samling";
    var erUtvalg = el.type === "radio" && el.name === "utvalg";
    if (!erSamling && !erUtvalg) return;
    var velgerSamlinger = (erSamling && el.checked) || (erUtvalg && el.value === "noen");
    if (erSamling && el.checked) {
      var noen = gruppe.querySelector("input[type=radio][name=utvalg][value=noen]");
      if (noen) noen.checked = true;
    }
    if (erUtvalg && el.value === "hele") {
      gruppe.querySelectorAll("input[type=checkbox][name=samling]").forEach(function (b) { b.checked = false; });
    }
    var ekstra = velgerSamlinger && el.form ? el.form.querySelector("input[type=checkbox][name=ekstradeltaker]") : null;
    if (ekstra && !ekstra.checked) ekstra.checked = true;
  });

  // Flettefelt-knapper: koden settes inn der markøren sto (feltet husker markøren selv om knappen fikk fokus), og markøren
  // havner rett etter koden. Feltets makslengde respekteres. input-hendelsen gjør at endringen merkes som ulagret.
  document.addEventListener("click", function (e) {
    var knapp = e.target instanceof Element && e.target.closest("[data-sett-inn]");
    if (!knapp) return;
    e.preventDefault();
    var felt = document.getElementById(knapp.getAttribute("data-felt"));
    if (!felt || typeof felt.setRangeText !== "function") return;
    var kode = knapp.getAttribute("data-sett-inn");
    var start = felt.selectionStart, slutt = felt.selectionEnd;
    if (start === null || slutt === null) { start = slutt = felt.value.length; }
    felt.focus();
    if (felt.maxLength > 0 && felt.value.length - (slutt - start) + kode.length > felt.maxLength) return;
    felt.setRangeText(kode, start, slutt, "end");
    felt.dispatchEvent(new Event("input", { bubbles: true }));
  });

  // Deltakerlisten: «Velg alle synlige», live-søk og statusvalg. Radene har data-filtrer (deltaker/oppmote), data-status
  // og data-sok (navn og e-post med små bokstaver, satt av serveren). Regelen er den samme som _treffer i app.py: delvis
  // treff, og ingen status valgt betyr alle. Handlingene (send e-post, eksporter, behandle valgte) gjelder bare synlige,
  // avkryssede deltakere: en avkrysset rad som skjules av søket eller filteret, mister avkrysningen.
  var velgAlle = document.getElementById("velg-alle-synlige");
  var synligeValg = function () {
    return Array.prototype.filter.call(document.querySelectorAll(".valgt-deltaker"), function (b) {
      var rad = b.closest("tr");
      return !(rad && rad.hidden);
    });
  };
  var oppdaterValgte = function () {
    if (!velgAlle) return;
    var synlige = synligeValg();
    var valgt = synlige.filter(function (b) { return b.checked; }).length;
    var visning = document.getElementById("antall-valgt");
    if (visning) visning.textContent = valgt ? "– " + valgt + " valgt" : "";
    velgAlle.checked = synlige.length > 0 && valgt === synlige.length;
    velgAlle.indeterminate = valgt > 0 && valgt < synlige.length;
    velgAlle.disabled = synlige.length === 0;
  };
  if (velgAlle) {
    velgAlle.addEventListener("change", function () {
      var velg = velgAlle.checked;
      synligeValg().forEach(function (b) { b.checked = velg; });
      oppdaterValgte();
    });
    document.querySelectorAll(".valgt-deltaker").forEach(function (b) { b.addEventListener("change", oppdaterValgte); });
  }

  // Statusvelgeren i hver rad: «Endre» er skjult til en annen status er valgt. Valget sender ingenting av seg selv (piltastene
  // bytter verdi uten å åpne nedtrekket): admin trykker «Endre» og bekrefter dialogen. Uten JavaScript er knappen alltid synlig.
  document.querySelectorAll("form[data-radstatus]").forEach(function (skjema) {
    var velger = skjema.querySelector("select[name=status]");
    var knapp = skjema.querySelector("button");
    if (!velger || !knapp) return;
    var oppdater = function () { knapp.hidden = velger.value === skjema.getAttribute("data-naa"); };
    velger.addEventListener("change", oppdater);
    window.addEventListener("pageshow", oppdater);         // «tilbake» kan gjenopprette et valg nettleseren husket
    oppdater();
  });

  var filter = document.querySelector("form[data-deltakerfilter]");
  if (filter) {
    var sokefelt = filter.querySelector("input[name=sok]");
    var statusvalg = filter.querySelectorAll("input[name=status]");
    var nullstill = filter.querySelector("[data-nullstill-filter]");
    var eksport = document.getElementById("eksporter-treff");
    var antallSynlige = document.getElementById("antall-synlige");
    var tidtaker = null;
    var treffer = function (rad, sok, valgte) {
      return (!sok || (rad.getAttribute("data-sok") || "").indexOf(sok) >= 0) &&
             (!valgte.length || valgte.indexOf(rad.getAttribute("data-status")) >= 0);
    };
    var brukFilter = function () {
      var tekst = sokefelt.value.trim();
      var sok = tekst.toLowerCase();
      var valgte = [];
      statusvalg.forEach(function (b) { if (b.checked) valgte.push(b.value); });
      var synlige = { deltaker: 0, oppmote: 0 }, totalt = { deltaker: 0, oppmote: 0 }, perStatus = {};
      document.querySelectorAll("tr[data-filtrer]").forEach(function (rad) {
        var type = rad.getAttribute("data-filtrer"), vis = treffer(rad, sok, valgte);
        rad.hidden = !vis;
        totalt[type] = (totalt[type] || 0) + 1;
        if (vis) synlige[type] = (synlige[type] || 0) + 1;
        var boks = rad.querySelector(".valgt-deltaker");
        if (boks && !vis) boks.checked = false;
        if (type === "deltaker" && treffer(rad, sok, [])) {     // tallet bak statusen: treff på søket med den statusen
          var s = rad.getAttribute("data-status");
          perStatus[s] = (perStatus[s] || 0) + 1;
        }
      });
      document.querySelectorAll("[data-antall]").forEach(function (el) {
        el.textContent = "(" + (perStatus[el.getAttribute("data-antall")] || 0) + ")";
      });
      document.querySelectorAll("tr[data-ingen-treff]").forEach(function (rad) {
        var type = rad.getAttribute("data-ingen-treff");
        rad.hidden = !(totalt[type] > 0 && synlige[type] === 0);
      });
      if (antallSynlige) {
        var alle = totalt.deltaker, vist = synlige.deltaker;
        antallSynlige.textContent = "Viser " + (vist !== alle ? vist + " av " : "") + alle + (alle === 1 ? " deltaker." : " deltakere.");
      }
      var aktivt = tekst !== "" || valgte.length > 0;
      if (nullstill) nullstill.hidden = !aktivt;
      var parametre = new URLSearchParams();
      if (tekst) parametre.set("sok", tekst);
      valgte.forEach(function (s) { parametre.append("status", s); });
      var sporring = parametre.toString() ? "?" + parametre.toString() : "";
      if (eksport) {
        eksport.href = eksport.getAttribute("data-grunnadresse") + sporring;
        eksport.textContent = aktivt ? "Eksporter treff (Excel)" : "Eksporter deltakerliste (Excel)";
      }
      // Filteret står i adressen: det overlever omlasting (f.eks. når deltakervinduet lukkes etter en lagring)
      if (window.history && history.replaceState) history.replaceState(history.state, "", location.pathname + sporring + location.hash);
      oppdaterValgte();
    };
    sokefelt.addEventListener("input", function () {
      clearTimeout(tidtaker);
      tidtaker = setTimeout(brukFilter, 250);
    });
    statusvalg.forEach(function (b) { b.addEventListener("change", brukFilter); });
    filter.addEventListener("submit", function (e) { e.preventDefault(); clearTimeout(tidtaker); brukFilter(); });
    if (nullstill) {
      nullstill.addEventListener("click", function (e) {
        e.preventDefault();
        sokefelt.value = "";
        statusvalg.forEach(function (b) { b.checked = false; });
        brukFilter();
        sokefelt.focus();
      });
    }
    // Nettleseren kan fylle inn skjemaet på nytt (omlasting, «tilbake»): vis alltid det som faktisk står i feltene
    window.addEventListener("pageshow", function (e) { if (e.persisted) brukFilter(); });
    brukFilter();
  }
  oppdaterValgte();

  // Statusendring i listen: siden lastes på nytt med adressen …#rad-<id> (admin_deltaker_status sender tilbake til raden).
  // Da hopper nettleseren til raden (den kan ha flyttet seg til en annen statusgruppe), meldingene fra serveren legges rett
  // over raden (øverst på siden ville de ligget utenfor bildet), og radene som var avkrysset før omlastingen krysses av
  // igjen (de er husket i nettleseren til nå: «Send e-post til valgte» osv. skal ikke miste utvalget av en statusendring).
  var VALGT_NOKKEL = "kurs-valgte-rader";
  function lagreValgte() {
    try {
      var ider = [];
      document.querySelectorAll(".valgt-deltaker:checked").forEach(function (b) { ider.push(b.value); });
      window.sessionStorage.setItem(VALGT_NOKKEL, JSON.stringify({ sti: window.location.pathname, ider: ider }));
    } catch (feil) { /* lagring er ikke tillatt: utvalget nullstilles som før */ }
  }
  var radAnker = /^#rad-\d+$/.test(window.location.hash) ? document.querySelector(window.location.hash) : null;
  if (radAnker) {
    try {
      var husket = JSON.parse(window.sessionStorage.getItem(VALGT_NOKKEL) || "null");
      window.sessionStorage.removeItem(VALGT_NOKKEL);
      if (husket && husket.sti === window.location.pathname) {
        synligeValg().forEach(function (b) { if (husket.ider.indexOf(b.value) >= 0) b.checked = true; });
        oppdaterValgte();
      }
    } catch (feil) { /* ingenting husket */ }
    var meldinger = document.querySelectorAll("#innhold > .flash");
    if (!radAnker.hidden) {                          // en rad som filteret skjuler, har ingenting å hoppe til
      var meldingsrad = null;
      if (meldinger.length) {
        meldingsrad = document.createElement("tr");
        meldingsrad.className = "radmelding";
        var celle = meldingsrad.insertCell();
        celle.colSpan = radAnker.cells.length;
        meldinger.forEach(function (m) { celle.appendChild(m); });
        radAnker.parentNode.insertBefore(meldingsrad, radAnker);
      }
      // Meldingen og raden midt i bildet. Nettleserens egen hopp til #rad-<id> legger raden øverst (og meldingen over den,
      // utenfor bildet), og skjer etter dette skriptet: derfor hoppes det en gang til når siden er ferdig lastet.
      var hoppTilRaden = function () { (meldingsrad || radAnker).scrollIntoView({ block: "center" }); };
      hoppTilRaden();
      window.addEventListener("load", function () { setTimeout(hoppTilRaden, 0); });
    }
  }

  // Bedriftspåmelding: legg til / fjern deltakerrader
  var rader = document.getElementById("deltaker-rader");
  var leggTil = document.getElementById("legg-til-rad");
  if (rader && leggTil) {
    var oppdaterFjern = function () {
      var flere = document.querySelectorAll(".deltaker-rad").length > 1;
      document.querySelectorAll(".fjern-rad").forEach(function (k) { k.hidden = !flere; });
    };
    leggTil.addEventListener("click", function () {
      var mal = document.getElementById("rad-mal");
      rader.appendChild(mal.content.cloneNode(true));
      oppdaterFjern();
    });
    rader.addEventListener("click", function (e) {
      if (!e.target.classList.contains("fjern-rad")) return;
      if (document.querySelectorAll(".deltaker-rad").length > 1) {
        e.target.closest(".deltaker-rad").remove();
        oppdaterFjern();
      }
    });
    oppdaterFjern();
  }

  // Globalt deltakersøk (søkefeltet i toppmenyen, på Oversikt og på søkesiden). Skjemaet er et vanlig GET-skjema mot
  // /admin/sok og virker uten dette. Her legges rullegardinen på: minst 2 tegn, 200 ms ventetid, bare siste svar vises.
  // Treffene kommer som JSON fra data-json og tegnes med textContent/createTextNode - aldri innerHTML med data fra
  // databasen. Piltaster velger, Enter åpner valgt treff (uten valg: hele søkeresultatet), Esc lukker. Søketeksten
  // sendes som POST (i kroppen, med CSRF-hodet), så den ikke havner i URL-logger for hvert tastetrykk, og lagres ikke noe
  // sted i nettleseren (bare i feltet og adressen til resultatsiden).
  var tekstMedTreff = function (el, deler) {
    deler.forEach(function (del) {
      if (del[1]) {
        var m = document.createElement("mark");
        m.textContent = del[0];
        el.appendChild(m);
      } else {
        el.appendChild(document.createTextNode(del[0]));
      }
    });
  };
  document.querySelectorAll("form[data-deltakersok]").forEach(function (skjema) {
    var felt = skjema.querySelector("input[name=q]");
    var liste = skjema.querySelector(".sok-liste");
    var melding = skjema.querySelector("[data-sok-status]");
    var adresse = skjema.getAttribute("data-json"), csrf = skjema.getAttribute("data-csrf");
    if (!felt || !liste || !adresse || !csrf || !window.fetch || !window.AbortController) return;
    var tidtaker = null, avbryt = null, forespoersel = 0, aktiv = -1;
    var utdatert = false;                               // listen viser svar på en eldre tekst: piltaster og Enter gjelder ikke den
    var valg = function () { return liste.querySelectorAll("a[role=option]"); };
    var si = function (tekst) { if (melding) melding.textContent = tekst; };
    var nullstillValg = function () {
      aktiv = -1;
      valg().forEach(function (a) { a.setAttribute("aria-selected", "false"); });
      felt.removeAttribute("aria-activedescendant");
    };
    var lukk = function () {
      liste.hidden = true;
      felt.setAttribute("aria-expanded", "false");
      nullstillValg();
    };
    var aapne = function () {
      liste.hidden = false;
      felt.setAttribute("aria-expanded", valg().length ? "true" : "false");
    };
    var merk = function (nr) {
      var alle = valg();
      if (!alle.length) return;
      aktiv = (nr + alle.length) % alle.length;
      alle.forEach(function (a, i) { a.setAttribute("aria-selected", i === aktiv ? "true" : "false"); });
      felt.setAttribute("aria-activedescendant", alle[aktiv].id);
      alle[aktiv].scrollIntoView({ block: "nearest" });
    };
    var tomListe = function (tekst) {
      var boks = document.createElement("div");
      boks.className = "sok-tom";
      boks.textContent = tekst;
      liste.removeAttribute("role");                  // en melding er ingen «option»: en listbox skal bare inneholde valg
      liste.replaceChildren(boks);
      utdatert = false;
      nullstillValg();
      aapne();
      si(tekst);
    };
    var lagValg = function (id, url, klasse) {
      var a = document.createElement("a");
      a.id = id;
      a.setAttribute("role", "option");
      a.setAttribute("aria-selected", "false");
      a.tabIndex = -1;                            // velges med piltastene; Tab går videre til «Søk»-knappen
      a.href = url;
      if (klasse) a.className = klasse;
      return a;
    };
    var vis = function (data) {
      var deler = document.createDocumentFragment(), antall = 0;
      data.treff.forEach(function (t) {
        if (typeof t.url !== "string" || t.url.indexOf("/admin/") !== 0) return;   // bare adresser på dette nettstedet
        var a = lagValg(felt.id + "-v" + antall, t.url);
        var navn = document.createElement("span");
        navn.className = "sok-navn";
        tekstMedTreff(navn, t.navn_deler);
        var epost = document.createElement("span");
        epost.className = "sok-linje";
        tekstMedTreff(epost, t.epost_deler);
        var grunn = document.createElement("span");
        grunn.className = "sok-linje";
        grunn.textContent = t.grunn + " · " + t.antall + (t.antall === 1 ? " påmelding" : " påmeldinger") +
          (t.siste_kurs ? " · siste: " + t.siste_kurs : "");
        a.appendChild(navn);
        a.appendChild(epost);
        a.appendChild(grunn);
        deler.appendChild(a);
        antall++;
      });
      if (data.for_kort) { tomListe("Skriv minst ett ord med to tegn (eller et tall)."); return; }
      if (!antall) { tomListe(data.totalt ? "Ingen treff å vise. Trykk Enter for å se søkeresultatet." : "Ingen treff"); return; }
      if (typeof data.alle_url === "string" && data.alle_url.indexOf("/admin/") === 0) {
        var alle = lagValg(felt.id + "-alle", data.alle_url, "sok-alle");
        alle.textContent = data.totalt > antall ? "Åpne resultatsiden (" + data.totalt + " treff) →" : "Åpne søkeresultatet med alle kurs →";
        deler.appendChild(alle);
      }
      liste.setAttribute("role", "listbox");
      liste.replaceChildren(deler);
      utdatert = false;
      nullstillValg();
      aapne();
      si(data.totalt + " treff. Bruk pil opp og ned for å velge, Enter for å åpne.");
    };
    var sok = function () {
      var tekst = felt.value.trim();
      if (avbryt) avbryt.abort();
      if (tekst.length < 2) { lukk(); si(""); return; }
      var nr = ++forespoersel;
      avbryt = new AbortController();
      fetch(adresse, {
        method: "POST", credentials: "same-origin", cache: "no-store", referrerPolicy: "no-referrer", signal: avbryt.signal,
        headers: { "Accept": "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ q: tekst })
      })
        .then(function (svar) {
          if (svar.status === 401) throw "ut";
          if (svar.status === 429) throw "mange";
          if (svar.status === 400) throw "gammel";      // CSRF-tokenet stemmer ikke lenger (utlogget i en annen fane)
          if (!svar.ok) throw "feil";
          return svar.json();
        })
        .then(function (data) { if (nr === forespoersel) vis(data); })
        .catch(function (feil) {
          if (nr !== forespoersel || (feil && feil.name === "AbortError")) return;
          tomListe(feil === "ut" ? "Du er logget ut. Last siden på nytt og logg inn." :
                   feil === "gammel" ? "Siden har stått åpen for lenge. Last siden på nytt." :
                   feil === "mange" ? "For mange søk på kort tid. Vent et øyeblikk." :
                   "Kunne ikke søke akkurat nå. Trykk Enter for å søke likevel.");
        });
    };
    felt.addEventListener("input", function () {
      clearTimeout(tidtaker);
      si("");
      nullstillValg();                              // et valg gjaldt forrige tekst: Enter skal søke på det som står nå
      utdatert = true;
      if (felt.value.trim().length < 2) {           // for kort: lukk og forkast et svar som er på vei
        if (avbryt) avbryt.abort();
        forespoersel++;
        lukk();
        return;
      }
      tidtaker = setTimeout(sok, 200);
    });
    felt.addEventListener("focus", function () {
      if (valg().length && felt.value.trim().length >= 2) aapne();
    });
    felt.addEventListener("keydown", function (e) {
      if (e.isComposing) return;
      var n = valg().length;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!n || utdatert) return;                 // ingen valg i en liste som er på vei til å bli byttet ut
        e.preventDefault();
        if (liste.hidden) aapne();
        merk(aktiv < 0 ? (e.key === "ArrowDown" ? 0 : n - 1) : aktiv + (e.key === "ArrowDown" ? 1 : -1));
      } else if (e.key === "Enter") {
        if (!liste.hidden && aktiv >= 0) { e.preventDefault(); valg()[aktiv].click(); }
        else clearTimeout(tidtaker);                  // vanlig innsending: hele søkeresultatet
      } else if (e.key === "Escape") {
        if (!liste.hidden) {
          e.preventDefault();
          clearTimeout(tidtaker);                      // et svar som er på vei, skal ikke åpne listen igjen
          if (avbryt) avbryt.abort();
          forespoersel++;
          lukk();
        }
      }
    });
    liste.addEventListener("mousedown", function (e) { e.preventDefault(); });   // fokus blir i feltet, klikket virker likevel
    skjema.addEventListener("focusout", function (e) {
      if (e.relatedTarget !== felt && !liste.contains(e.relatedTarget)) lukk();     // fokus forlot feltet og listen
    });
    document.addEventListener("click", function (e) { if (!skjema.contains(e.target)) lukk(); });
    window.addEventListener("pageshow", function (e) { if (e.persisted) lukk(); });
  });

  // «/» hopper til deltakersøket fra hvilken som helst adminside, med mindre man skriver i et felt eller et vindu er åpent:
  // til feltet på siden (Oversikten og søkesiden), og ellers til Oversikten, med markøren i feltet (adressen er #sok-deltaker).
  if (window.location.hash === "#sok-deltaker") {
    var komFraAnnenSide = document.getElementById("sok-deltaker");
    if (komFraAnnenSide) komFraAnnenSide.focus();
  }
  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || e.isComposing || e.defaultPrevented) return;
    var her = document.activeElement, tag = her ? her.tagName : "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (her && her.isContentEditable)) return;
    if (document.querySelector("dialog[open]")) return;
    var felt = document.querySelector("form.stor[data-deltakersok] input[name=q]") ||
               document.querySelector("form[data-deltakersok] input[name=q]");
    if (!felt) {                                                                  // siden har ikke selv noe deltakersøk
      var topp = document.querySelector("header[data-sok-adresse]");
      var dit = topp ? topp.getAttribute("data-sok-adresse") : "";
      if (dit.indexOf("/admin") !== 0) return;                                    // bare adresser på dette nettstedet
      e.preventDefault();
      window.location.href = dit;
      return;
    }
    e.preventDefault();
    felt.focus();
    felt.select();
  });

  // Deltakervinduet: et klikk på et navn i deltakerlisten åpner deltakersiden i et vindu over listen. Innholdet er
  // #deltaker-innhold fra selve siden (hentet med fetch fra samme server - CSP tillater ingen iframe). Faner og skjema
  // i vinduet lastes og sendes i vinduet, og listen lastes på nytt når vinduet lukkes etter en endring.
  var vindu = document.getElementById("deltaker-vindu");
  if (vindu && typeof vindu.showModal === "function" && window.fetch && window.DOMParser) {
    var vinduInnhold = vindu.querySelector(".vindu-innhold");
    var listenErEndret = false;
    var trykkPaaBakgrunn = false;
    var sisteForespoersel = 0;           // bare svaret på det siste klikket vises (et tregt svar kan ikke overskrive et nytt)

    var vinduMelding = function (tekst, lenke) {
      var melding = document.createElement("div");
      melding.className = "flash feil";
      melding.setAttribute("role", "alert");
      melding.textContent = tekst;
      var kropp = vinduInnhold.querySelector(".deltakerkort-kropp");
      if (kropp) {                       // behold det som vises, og legg meldingen øverst
        kropp.insertBefore(melding, kropp.firstChild);
        kropp.scrollTop = 0;
        return;
      }
      var boks = document.createElement("div");
      boks.className = "vindu-melding";
      boks.appendChild(melding);
      if (lenke) {
        var a = document.createElement("a");
        a.href = lenke;
        a.textContent = "Åpne som egen side";
        boks.appendChild(a);
      }
      vinduInnhold.replaceChildren(boks);
    };

    var lastInn = function (url, valg) {
      var nr = ++sisteForespoersel;
      if (valg && valg.method === "POST") listenErEndret = true;      // listen oppdateres når vinduet lukkes
      vindu.setAttribute("aria-busy", "true");
      fetch(url, valg || { credentials: "same-origin" })
        .then(function (svar) { return svar.text().then(function (html) { return { svar: svar, html: html }; }); })
        .then(function (r) {
          if (nr !== sisteForespoersel || !vindu.open) return;         // et nyere klikk, eller vinduet er lukket
          vindu.removeAttribute("aria-busy");
          if (!r.svar.ok) {
            vinduMelding(r.svar.status === 403 ? "Du har ikke tilgang til å gjøre dette." :
              "Noe gikk galt (feil " + r.svar.status + "). Prøv igjen, eller åpne deltakeren som egen side.", url);
            return;
          }
          var side = new DOMParser().parseFromString(r.html, "text/html");
          var nytt = side.getElementById("deltaker-innhold");
          if (!nytt) { window.location.href = r.svar.url; return; }   // f.eks. utlogget: vis siden vi havnet på
          var meldinger = document.createDocumentFragment();
          side.querySelectorAll("main > .flash").forEach(function (m) { meldinger.appendChild(m); });
          var kropp = nytt.querySelector(".deltakerkort-kropp");
          if (kropp) kropp.insertBefore(meldinger, kropp.firstChild);
          vinduInnhold.replaceChildren(document.adoptNode(nytt));
          var tittel = document.getElementById("deltaker-tittel");
          if (tittel) tittel.focus();
        })
        .catch(function () {
          if (nr !== sisteForespoersel || !vindu.open) return;
          vindu.removeAttribute("aria-busy");
          vinduMelding("Fikk ikke kontakt med serveren. Prøv igjen.", url);
        });
    };

    var aapne = function (url) {
      listenErEndret = false;
      var laster = document.createElement("p");
      laster.className = "vindu-laster dempet";
      laster.textContent = "Laster …";
      vinduInnhold.replaceChildren(laster);
      document.documentElement.classList.add("vindu-aapent");
      vindu.showModal();
      lastInn(url);
    };

    // Rydder med en gang vinduet lukkes. close-hendelsen kommer først litt senere - er vinduet åpnet igjen innen da,
    // skal den ikke tømme det nye innholdet.
    var ryddOpp = function () {
      if (vindu.open) return;
      vindu.removeAttribute("aria-busy");
      document.documentElement.classList.remove("vindu-aapent");
      vinduInnhold.replaceChildren();
      if (listenErEndret) {                              // vis ny status/nye opplysninger i listen
        listenErEndret = false;
        window.location.reload();
      }
    };

    var lukk = function () {
      if (ulagredeSkjema(vinduInnhold, null) &&
          !window.confirm("Du har endringer som ikke er lagret. Vil du lukke vinduet likevel?")) return;
      vindu.close();
      ryddOpp();
    };

    vindu.addEventListener("close", ryddOpp);           // også når nettleseren lukker vinduet selv
    vindu.addEventListener("cancel", function (e) { e.preventDefault(); lukk(); });   // Esc
    vindu.addEventListener("mousedown", function (e) { trykkPaaBakgrunn = e.target === vindu; });
    vindu.addEventListener("click", function (e) {
      if (e.target === vindu && trykkPaaBakgrunn) lukk();  // klikk på den mørke bakgrunnen utenfor vinduet
    });

    document.addEventListener("click", function (e) {
      if (!(e.target instanceof Element) || e.defaultPrevented) return;
      var nyFane = e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey;
      var lenke = e.target.closest("a[data-deltaker-vindu]");
      if (lenke) {
        if (!nyFane) { e.preventDefault(); aapne(lenke.href); }
        return;
      }
      if (!vindu.open) return;
      if (e.target.closest("[data-lukk-vindu]")) { lukk(); return; }
      var iVindu = e.target.closest("a[data-i-vindu]");
      if (iVindu && !nyFane && vinduInnhold.contains(iVindu)) { e.preventDefault(); lastInn(iVindu.href); return; }
      var fane = e.target.closest(".faner a");
      if (fane && !nyFane && vinduInnhold.contains(fane)) {
        e.preventDefault();
        if (ulagredeSkjema(vinduInnhold, null) &&
            !window.confirm("Du har endringer som ikke er lagret. Vil du bytte fane likevel?")) return;
        lastInn(fane.href);
      }
    });

    // Skjema i vinduet sendes i bakgrunnen. Kjører etter bekreftelsene over (data-bekreft, ulagrede endringer).
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (e.defaultPrevented || !vindu.open || !(form instanceof HTMLFormElement) || !vinduInnhold.contains(form)) return;
      if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
      e.preventDefault();
      var data;
      try { data = new FormData(form, e.submitter || null); } catch (feil) { data = new FormData(form); }
      lastInn(form.action, { method: "POST", body: data, credentials: "same-origin" });
    });
  }

  // Kursskjemaet («Opprett kurs» og Oppsett, form[data-kursskjema]). Serveren kontrollerer alt på nytt - dette er bare
  // hjelp mens man fyller ut: format styrer sted og Zoom-lenke, Faktura styrer fakturavalgene, samlinger kan legges til,
  // kopieres og fjernes, «Vis dager» viser én rad per dato, oppsummeringen regnes ut, og påmeldingsfristen foreslås
  // (samme regler som kursdatoer.py: én kalendermåned før første kursdag, men aldri før i dag).
  var DAG_MS = 86400000;
  var UKEDAGER = ["sø.", "ma.", "ti.", "on.", "to.", "fr.", "lø."];
  var MAKS_DAGER = 31;
  var DAGETIKETT = { start: "Fra kl. ", slutt: "Til kl. ", timer: "Timer ", merknad: "Merknad ", fjern: "Ikke med: " };

  var toSifre = function (n) { return (n < 10 ? "0" : "") + n; };
  var lesDato = function (verdi) {             // "2027-09-20" -> Date (UTC midnatt), ellers null
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(verdi || "");
    if (!m) return null;
    var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return d.getUTCMonth() === +m[2] - 1 && d.getUTCDate() === +m[3] ? d : null;
  };
  var isoDato = function (d) { return d.toISOString().slice(0, 10); };
  var pluss = function (d, dager) { return new Date(d.getTime() + dager * DAG_MS); };
  var kortDato = function (d) { return toSifre(d.getUTCDate()) + "." + toSifre(d.getUTCMonth() + 1) + "." + d.getUTCFullYear(); };
  var dagtekst = function (d) { return UKEDAGER[d.getUTCDay()] + " " + kortDato(d); };
  var periode = function (fra, til) {          // 15.10.2027 · 20.–23.09.2027 · 30.09.–02.10.2027 · 30.12.2027–02.01.2028
    if (fra.getTime() === til.getTime()) return kortDato(fra);
    if (fra.getUTCFullYear() !== til.getUTCFullYear()) return kortDato(fra) + "–" + kortDato(til);
    if (fra.getUTCMonth() !== til.getUTCMonth()) return toSifre(fra.getUTCDate()) + "." + toSifre(fra.getUTCMonth() + 1) + ".–" + kortDato(til);
    return toSifre(fra.getUTCDate()) + ".–" + kortDato(til);
  };
  var tall = function (verdi) {
    var t = parseFloat(String(verdi || "").trim().replace(",", "."));
    return isNaN(t) ? null : t;
  };
  var timerTekst = function (t) {
    return String(Math.round(t * 100) / 100).replace(".", ",") + (t === 1 ? " time" : " timer");
  };
  var enMaanedFor = function (d) {             // 15.10 -> 15.09, 31.03 -> 28./29.02
    var aar = d.getUTCFullYear(), maaned = d.getUTCMonth() - 1;
    if (maaned < 0) { maaned = 11; aar -= 1; }
    var sisteDag = new Date(Date.UTC(aar, maaned + 1, 0)).getUTCDate();
    return new Date(Date.UTC(aar, maaned, Math.min(d.getUTCDate(), sisteDag)));
  };
  var foreslaattFrist = function (forste, idag) {
    var frist = enMaanedFor(forste);
    if (frist >= idag) return frist;
    var dagenFor = pluss(forste, -1);
    return dagenFor > idag ? dagenFor : idag;
  };
  var nesteNavn = function (navn) {           // «Samling 1» -> «Samling 2», «1. samling» -> «2. samling» (et tall på 1-2 sifre)
    var m = /^(.*?\D|)(\d{1,2})(\D*)$/.exec(navn || "");
    return m ? m[1] + (parseInt(m[2], 10) + 1) + m[3] : (navn || "");
  };

  var samlingsfelt = function (rad, navn) { return rad.querySelector("[data-samlingsfelt=" + navn + "]"); };
  var samlingsnr = function (rad) {           // tallet i feltnavnene, s-<nr>-...
    var m = /^s-(\d+)-id$/.exec(samlingsfelt(rad, "id").name);
    return m ? m[1] : "0";
  };
  // Alle datoene fra fra-dato til til-dato (tom til-dato = én dag). null når samlingen er for lang (serveren sier fra).
  var samlingsdatoer = function (rad) {
    var fra = lesDato(samlingsfelt(rad, "fra").value);
    if (!fra) return [];
    var til = lesDato(samlingsfelt(rad, "til").value);
    if (!til || til < fra) til = fra;
    var antall = Math.round((til - fra) / DAG_MS) + 1;
    if (antall > MAKS_DAGER) return null;
    var ut = [];
    for (var i = 0; i < antall; i += 1) ut.push(pluss(fra, i));
    return ut;
  };

  var lagDagrad = function (container, nr, d) {
    var mal = container.querySelector("template[data-dagmal]");
    var tr = mal.content.querySelector("tr").cloneNode(true);
    var iso = isoDato(d), tekst = dagtekst(d);
    tr.setAttribute("data-dag", iso);
    tr.querySelector("[data-dagtekst]").textContent = tekst;
    tr.querySelectorAll("input").forEach(function (felt) {
      felt.name = felt.name.replace("__N__", nr).replace("__DATO__", iso);
      felt.setAttribute("aria-label", (DAGETIKETT[felt.getAttribute("data-dagfelt")] || "") + tekst);
    });
    return tr;
  };

  // Én rad per dato under «Vis dager». Datoer som fortsatt er med, beholder det som er fylt inn.
  var byggDager = function (rad) {
    var container = rad.closest("[data-samlinger]");
    var tbody = rad.querySelector("[data-dagrader]");
    var datoer = samlingsdatoer(rad) || [];
    var finnes = {};
    tbody.querySelectorAll("tr[data-dag]").forEach(function (tr) { finnes[tr.getAttribute("data-dag")] = tr; });
    var nr = samlingsnr(rad);
    var nye = document.createDocumentFragment();
    datoer.forEach(function (d) { nye.appendChild(finnes[isoDato(d)] || lagDagrad(container, nr, d)); });
    tbody.replaceChildren(nye);
    rad.querySelector("[data-dagtabell]").hidden = datoer.length === 0;
    rad.querySelector("[data-ingen-dager]").hidden = datoer.length > 0;
  };

  // «3 samlinger · 12 kursdager · 20.09.2027–11.05.2028 · 72 timer» (samme tekst som kursdatoer.oppsummering).
  // Returnerer første kursdag (eller null).
  var oppsummer = function (container) {
    var samlinger = 0, dager = 0, timer = 0, forste = null, siste = null;
    container.querySelectorAll("[data-samling]").forEach(function (rad) {
      var datoer = samlingsdatoer(rad);
      if (!datoer) return;
      var standard = tall(samlingsfelt(rad, "timer").value);
      var med = 0;
      datoer.forEach(function (d) {
        var tr = rad.querySelector('tr[data-dag="' + isoDato(d) + '"]');
        if (tr && tr.querySelector("[data-dagfelt=fjern]").checked) return;
        var egne = tr ? tall(tr.querySelector("[data-dagfelt=timer]").value) : null;
        med += 1;
        timer += egne !== null ? egne : (standard || 0);
        if (!forste || d < forste) forste = d;
        if (!siste || d > siste) siste = d;
      });
      if (med) { samlinger += 1; dager += med; }
    });
    var felt = container.querySelector("[data-samlingsoppsummering]");
    if (felt) {
      felt.textContent = dager ? [samlinger + " samling" + (samlinger !== 1 ? "er" : ""),
        dager + " kursdag" + (dager !== 1 ? "er" : ""), periode(forste, siste), timerTekst(timer)].join(" · ") : "";
    }
    return forste;
  };

  var nummerer = function (container) {
    var rader = container.querySelectorAll("[data-samling]");
    rader.forEach(function (rad, i) {
      var nr = rad.querySelector("[data-samling-nr]");
      if (nr) nr.textContent = String(i + 1);
      var fjern = rad.querySelector("[data-fjern-samling]");
      if (fjern) fjern.hidden = rader.length <= 1;
    });
  };

  // Påmeldingsfristen: forslaget vises på knappen «Bruk forslaget». Står fristen på automatisk (ikke endret av admin),
  // følger feltet datoene i skjemaet - men bare når skjemaet selv har datoene (Opprett kurs). I Oppsett lagres datoene
  // for seg, og fristen flyttes da av serveren; her endres den bare med «Bruk forslaget».
  var fristforslag = function (skjema) {
    var boks = skjema.querySelector("[data-frist]");
    if (!boks) return null;
    var samlinger = skjema.querySelector("[data-samlinger]");
    var forste = samlinger ? oppsummer(samlinger) : lesDato(boks.getAttribute("data-forste-kursdag"));
    var idag = lesDato(boks.getAttribute("data-idag"));
    return forste && idag ? foreslaattFrist(forste, idag) : null;
  };
  var visFrist = function (skjema, fyllInn) {
    var boks = skjema.querySelector("[data-frist]");
    if (!boks) return;
    var forslag = fristforslag(skjema);
    var manuell = boks.querySelector("[data-frist-manuell]").value === "1";
    var knapp = boks.querySelector("[data-frist-forslag]");
    if (knapp) knapp.textContent = "Bruk forslaget" + (forslag ? " (" + kortDato(forslag) + ")" : "");
    if (fyllInn && !manuell && forslag) boks.querySelector("[data-frist-felt]").value = isoDato(forslag);
    boks.querySelector("[data-frist-auto]").hidden = manuell;
    boks.querySelector("[data-frist-egen]").hidden = !manuell;
  };

  var visFormat = function (skjema) {
    var valgt = skjema.querySelector("input[name=type]:checked");
    var type = valgt ? valgt.value : "";
    var sted = skjema.querySelector("[data-sted-felt]");
    if (sted) {
      sted.hidden = type === "digital";
      var stedfelt = sted.querySelector("input");
      if (stedfelt) stedfelt.required = type !== "digital";
    }
    var zoom = skjema.querySelector("[data-zoom-felt]");
    if (zoom) zoom.hidden = type === "fysisk";
  };

  var visBetaling = function (skjema) {
    var faktura = skjema.querySelector("[data-faktura-valg]");
    var valg = skjema.querySelector("[data-fakturavalg]");
    if (faktura && valg) valg.hidden = !faktura.checked;
    var plan = skjema.querySelector("input[data-fakturaplan]:checked");
    var dagerFor = skjema.querySelector("[data-dager-for]");
    if (dagerFor) dagerFor.hidden = !plan || plan.value === "samlet";
  };

  // Oppsett for et kurs som har fakturaer: avkryssingen «Jeg forstår dette …» ([data-okonomi-bekreft]) vises først når et felt i
  // [data-okonomi] (pris og fakturaoppsett) er endret fra det som er lagret, og er da påkrevd. Uten JavaScript står den alltid synlig.
  // Serveren krever bekreftelsen uansett (bekreft_okonomi), dette er bare hjelp.
  var visOkonomiBekreftelse = function (skjema) {
    var bekreft = skjema.querySelector("[data-okonomi-bekreft]");
    var boks = bekreft && bekreft.querySelector("input[type=checkbox]");
    if (!boks) return;
    var endret = Array.prototype.some.call(skjema.querySelectorAll("[data-okonomi] input"), function (el) {
      return el.type === "checkbox" || el.type === "radio" ? el.checked !== el.defaultChecked : el.value !== el.defaultValue;
    });
    bekreft.hidden = !endret;
    boks.required = endret;
    if (!endret) boks.checked = false;
  };

  var oppdaterSamlinger = function (skjema) {
    var container = skjema.querySelector("[data-samlinger]");
    if (!container) return;
    nummerer(container);
    oppsummer(container);
    visFrist(skjema, true);
  };

  var nySamling = function (container) {
    var nr = parseInt(container.getAttribute("data-neste"), 10) || 0;
    container.setAttribute("data-neste", String(nr + 1));
    var holder = document.createElement("div");
    holder.innerHTML = container.querySelector("template[data-samlingsmal]").innerHTML.replace(/__N__/g, String(nr));
    var rad = holder.querySelector("[data-samling]");
    container.querySelector("[data-samlingsliste]").appendChild(rad);
    return rad;
  };

  var settVarighet = function (rad, antall) {
    rad.setAttribute("data-varighet", antall ? String(antall) : "");
    var tekst = rad.querySelector("[data-varighetstekst]");
    if (tekst) tekst.textContent = antall > 1 ? " Kopien varer " + antall + " dager – til-dato fylles inn når du velger fra-dato." : "";
  };

  document.querySelectorAll("form[data-kursskjema]").forEach(function (skjema) {
    visFormat(skjema);
    visBetaling(skjema);
    visOkonomiBekreftelse(skjema);
    var container = skjema.querySelector("[data-samlinger]");
    if (container) {
      container.querySelectorAll("[data-samling]").forEach(function (rad) {
        if (rad.getAttribute("data-varighet") && !samlingsfelt(rad, "til").value) rad.setAttribute("data-til-auto", "1");
      });
      nummerer(container);
      oppsummer(container);
    }
    visFrist(skjema, !!container);

    skjema.addEventListener("change", function (e) {
      var el = e.target;
      if (!(el instanceof HTMLElement)) return;
      if (el.name === "type") visFormat(skjema);
      if (el.hasAttribute("data-faktura-valg") || el.hasAttribute("data-fakturaplan")) visBetaling(skjema);
      if (el.closest("[data-okonomi]")) visOkonomiBekreftelse(skjema);
    });

    skjema.addEventListener("input", function (e) {
      var el = e.target;
      if (!(el instanceof HTMLElement)) return;
      if (el.closest("[data-okonomi]")) {                 // pris eller fakturaoppsett endres: vis avkryssingen (bare kurs med fakturaer)
        visOkonomiBekreftelse(skjema);
        return;
      }
      if (el.hasAttribute("data-frist-felt")) {          // admin har satt fristen selv
        skjema.querySelector("[data-frist-manuell]").value = "1";
        visFrist(skjema, false);
        return;
      }
      var rad = el.closest("[data-samling]");
      if (!rad) return;
      var hva = el.getAttribute("data-samlingsfelt");
      if (hva === "fra") {
        var varighet = parseInt(rad.getAttribute("data-varighet"), 10) || 0;
        var fra = lesDato(el.value);
        var til = samlingsfelt(rad, "til");
        if (fra && varighet > 1 && (!til.value || rad.getAttribute("data-til-auto") === "1")) {
          til.value = isoDato(pluss(fra, varighet - 1));
          rad.setAttribute("data-til-auto", "1");
        }
      }
      if (hva === "til") rad.removeAttribute("data-til-auto");
      if (hva === "fra" || hva === "til") byggDager(rad);
      oppdaterSamlinger(skjema);
    });

    skjema.addEventListener("click", function (e) {
      var knapp = e.target instanceof Element && e.target.closest("button[type=button]");
      if (!knapp || !skjema.contains(knapp)) return;
      if (knapp.hasAttribute("data-frist-forslag")) {
        var forslag = fristforslag(skjema);
        var boks = skjema.querySelector("[data-frist]");
        if (forslag) boks.querySelector("[data-frist-felt]").value = isoDato(forslag);
        boks.querySelector("[data-frist-manuell]").value = "0";
        visFrist(skjema, false);
        skjema.dataset.endret = "1";
        return;
      }
      if (!container) return;
      var rad = knapp.closest("[data-samling]");
      if (knapp.hasAttribute("data-legg-til-samling")) {
        var ny = nySamling(container);
        oppdaterSamlinger(skjema);
        samlingsfelt(ny, "fra").focus();
      } else if (rad && knapp.hasAttribute("data-kopier-samling")) {
        var kopi = nySamling(container);
        ["start", "slutt", "timer"].forEach(function (f) { samlingsfelt(kopi, f).value = samlingsfelt(rad, f).value; });
        samlingsfelt(kopi, "navn").value = nesteNavn(samlingsfelt(rad, "navn").value);
        var datoer = samlingsdatoer(rad);
        var antall = datoer && datoer.length ? datoer.length : (parseInt(rad.getAttribute("data-varighet"), 10) || 0);
        settVarighet(kopi, antall);
        if (antall > 1) kopi.setAttribute("data-til-auto", "1");
        oppdaterSamlinger(skjema);
        samlingsfelt(kopi, "fra").focus();
      } else if (rad && knapp.hasAttribute("data-fjern-samling")) {
        if (container.querySelectorAll("[data-samling]").length <= 1) return;
        var forrige = rad.previousElementSibling || rad.nextElementSibling;
        rad.remove();
        oppdaterSamlinger(skjema);
        var fokus = forrige && forrige.querySelector("[data-kopier-samling]");
        if (fokus) fokus.focus();
      } else {
        return;
      }
      skjema.dataset.endret = "1";
    });
  });
  // ---------- Påmeldingsskjemaet (kurs.html) ----------
  // Felt og grupper med data-vis-naar vises bare når det andre feltet har verdien i data-vis-verdi. Skjulte felt er
  // deaktivert, så de verken sendes eller kreves - serveren gjør samme vurdering (ekstrafelt.tolk_svar, kursside).
  var verdierFor = function (form, navn) {
    var el = form.elements.namedItem(navn);
    if (!el) return [];
    var liste = el.tagName ? [el] : Array.prototype.slice.call(el);
    var ut = [];
    liste.forEach(function (x) {
      if (x.disabled) return;
      if (x.type === "radio" || x.type === "checkbox") { if (x.checked) ut.push(x.value); }
      else ut.push(x.value);
    });
    return ut;
  };
  var oppdaterBetingelser = function (form) {
    form.querySelectorAll("[data-vis-naar]").forEach(function (el) {
      var vis = verdierFor(form, el.getAttribute("data-vis-naar")).indexOf(el.getAttribute("data-vis-verdi")) >= 0;
      if (el.hidden === !vis) return;
      el.hidden = !vis;
      el.querySelectorAll("input, select, textarea").forEach(function (felt) { felt.disabled = !vis; });
    });
  };
  document.querySelectorAll("form").forEach(function (form) {
    if (!form.querySelector("[data-vis-naar]")) return;
    form.addEventListener("change", function () { oppdaterBetingelser(form); });
    window.addEventListener("pageshow", function () { oppdaterBetingelser(form); });
    oppdaterBetingelser(form);
  });

  // Organisasjonsnummer: skriv nummeret, eller søk på firmanavnet og velg i listen. Firmanavn og adresse hentes fra
  // Brønnøysundregistrene (via serveren) og vises i grå felt som ikke kan endres - deltakeren skriver dem aldri selv.
  // Svarer ikke registeret, beholdes organisasjonsnummeret, feltene står tomme, og deltakeren kan likevel melde seg på:
  // administrator kontrollerer firmaopplysningene etterpå. Serveren henter opplysningene på nytt ved innsending.
  var oppslag = document.querySelector("[data-enhetsoppslag]");
  if (oppslag && window.fetch) {
    var adresse = oppslag.getAttribute("data-enhetsoppslag");
    var orgnr = oppslag.querySelector("input[name=org_nr]");
    var forslag = document.getElementById("pm-forslag");
    var status = document.getElementById("pm-oppslag-status");
    var skjema = orgnr.form;
    var enhetsfelt = {};
    skjema.querySelectorAll("input[data-enhet]").forEach(function (el) { enhetsfelt[el.getAttribute("data-enhet")] = el; });
    var fraRegisteret = { org_navn: "navn", org_adresse: "adresse", org_postnr: "postnr", org_sted: "poststed" };
    var treff = [], aktiv = -1, tidtaker = null, sisteOppslag = 0;

    var settStatus = function (tekst, type) {
      status.textContent = tekst || "";
      status.className = "pm-status" + (type ? " " + type : "");
    };
    var fyll = function (enhet) {
      Object.keys(enhetsfelt).forEach(function (navn) {
        enhetsfelt[navn].value = enhet ? (enhet[fraRegisteret[navn]] || "") : "";
      });
    };
    var settPlassholder = function (tekst) {
      Object.keys(enhetsfelt).forEach(function (navn) { enhetsfelt[navn].placeholder = tekst; });
    };
    var lukkListe = function () {
      forslag.hidden = true;
      forslag.replaceChildren();
      orgnr.setAttribute("aria-expanded", "false");
      orgnr.removeAttribute("aria-activedescendant");
      treff = [];
      aktiv = -1;
    };
    var merk = function (i) {
      aktiv = i;
      forslag.querySelectorAll("li[role=option]").forEach(function (li, j) {
        li.setAttribute("aria-selected", j === i ? "true" : "false");
        if (j === i) { orgnr.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({ block: "nearest" }); }
      });
    };
    var velg = function (enhet) {
      orgnr.value = enhet.orgnr;
      fyll(enhet);
      lukkListe();
      settStatus("Hentet fra Brønnøysundregistrene.", "ok");
    };
    var hent = function (sporring) {
      var nr = ++sisteOppslag;
      return fetch(adresse + "?" + sporring, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (svar) {
          if (svar.status === 429) return { status: "for_mange" };
          return svar.json().catch(function () { return { status: "utilgjengelig" }; });
        })
        .catch(function () { return { status: "utilgjengelig" }; })
        .then(function (data) { return nr === sisteOppslag ? data : null; });   // bare svaret på siste oppslag brukes
    };
    var ikkeSvar = function (data, soek) {
      lukkListe();
      if (data.status === "for_mange") { settStatus("For mange oppslag på kort tid. Vent litt og prøv igjen.", "feil"); return; }
      if (soek) {
        settStatus("Søket i Brønnøysundregistrene virker ikke akkurat nå. Skriv organisasjonsnummeret (9 siffer) i stedet.", "advarsel");
        return;
      }
      fyll(null);
      settPlassholder("Hentes senere");
      settStatus("Firmaopplysningene kunne ikke hentes akkurat nå. Organisasjonsnummeret beholdes, og du kan likevel melde deg på – vi kontrollerer firmaopplysningene i etterkant.", "advarsel");
    };
    var visTreff = function (liste) {
      forslag.replaceChildren();
      treff = liste || [];
      aktiv = -1;
      if (!treff.length) {
        var tom = document.createElement("li");
        tom.className = "tom";
        tom.textContent = "Ingen treff. Prøv et annet navn, eller skriv organisasjonsnummeret.";
        forslag.appendChild(tom);
      }
      treff.forEach(function (enhet, i) {
        var li = document.createElement("li");
        li.id = "pm-forslag-" + i;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");
        var navn = document.createElement("span");
        navn.className = "navn";
        navn.textContent = enhet.navn;
        var mer = document.createElement("span");
        mer.className = "dempet";
        mer.textContent = "Org.nr. " + enhet.orgnr + (enhet.poststed ? " · " + enhet.poststed : "") +
          (enhet.underenhet ? " · underenhet" : "");
        li.appendChild(navn);
        li.appendChild(mer);
        li.addEventListener("mousedown", function (e) { e.preventDefault(); velg(enhet); });
        forslag.appendChild(li);
      });
      forslag.hidden = false;
      orgnr.setAttribute("aria-expanded", "true");
      settStatus(treff.length ? treff.length + " treff. Velg firmaet i listen." : "");
    };
    var slaaOpp = function () {
      var tekst = orgnr.value.trim();
      var siffer = tekst.replace(/[\s.]/g, "");
      if (/^\d{9}$/.test(siffer)) {
        lukkListe();
        settStatus("Henter firmaopplysninger …");
        hent("orgnr=" + encodeURIComponent(siffer)).then(function (data) {
          if (!data) return;
          if (data.status === "funnet") velg(data.enhet);
          else if (data.status === "ikke_funnet") { fyll(null); settStatus("Fant ikke organisasjonsnummeret i Brønnøysundregistrene. Kontroller nummeret.", "feil"); }
          else if (data.status === "ugyldig") { fyll(null); settStatus("Organisasjonsnummeret er ikke gyldig. Det har 9 siffer.", "feil"); }
          else ikkeSvar(data);
        });
      } else if (tekst.length >= 3 && /[^\d\s.]/.test(tekst)) {
        settStatus("Søker …");
        hent("sok=" + encodeURIComponent(tekst)).then(function (data) {
          if (!data) return;
          if (data.status === "ok") visTreff(data.treff);
          else ikkeSvar(data, true);
        });
      } else {
        lukkListe();
        settStatus(/^\d+$/.test(siffer) && siffer.length > 9 ? "Et organisasjonsnummer har 9 siffer." : "");
      }
    };
    orgnr.addEventListener("input", function () {
      fyll(null);
      settPlassholder("Fylles ut automatisk");
      clearTimeout(tidtaker);
      tidtaker = setTimeout(slaaOpp, 350);
    });
    orgnr.addEventListener("keydown", function (e) {
      if (forslag.hidden || !treff.length) return;
      if (e.key === "ArrowDown") { e.preventDefault(); merk(aktiv < treff.length - 1 ? aktiv + 1 : 0); }
      else if (e.key === "ArrowUp") { e.preventDefault(); merk(aktiv > 0 ? aktiv - 1 : treff.length - 1); }
      else if (e.key === "Enter" && aktiv >= 0) { e.preventDefault(); velg(treff[aktiv]); }
      else if (e.key === "Escape") { e.preventDefault(); lukkListe(); }
    });
    orgnr.addEventListener("blur", function () { setTimeout(lukkListe, 150); });
    // Siden vises på nytt med et gyldig nummer, men uten firmaopplysninger (f.eks. etter en feil): hent dem.
    if (/^\d{9}$/.test(orgnr.value.replace(/[\s.]/g, "")) && enhetsfelt.org_navn && !enhetsfelt.org_navn.value) slaaOpp();
  }

  // «Kopier lenke»: kopierer verdien i feltet knappen viser til. Faller tilbake til å markere teksten.
  document.addEventListener("click", function (e) {
    var knapp = e.target instanceof Element && e.target.closest("[data-kopier]");
    if (!knapp) return;
    var felt = document.getElementById(knapp.getAttribute("data-kopier"));
    if (!felt) return;
    if (!knapp.dataset.tekst) knapp.dataset.tekst = knapp.textContent;
    var ferdig = function () {
      knapp.textContent = "Kopiert!";
      setTimeout(function () { knapp.textContent = knapp.dataset.tekst; }, 2000);
    };
    var marker = function () {
      felt.focus();
      felt.select();
      try { if (document.execCommand("copy")) ferdig(); } catch (x) { /* teksten er markert - kopier med Ctrl+C */ }
    };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(felt.value).then(ferdig, marker);
    else marker();
  });

  // ---------- Skjemabyggeren (admin_kurs_paameldingsskjema.html) ----------
  // ▲▼ flytter feltet (tbody[data-flyttbar]) innenfor sin del av skjemaet (data-seksjon) og oppdaterer det skjulte
  // feltet rekkefolge_<del>; endringen lagres med «Lagre». Uten JavaScript sender knappene skjemaet (lagre + flytt).
  var bygger = document.querySelector("form[data-skjemabygger]");
  if (bygger) {
    var oppdaterRekkefolge = function (seksjon) {
      var nokler = Array.prototype.map.call(bygger.querySelectorAll('tbody[data-seksjon="' + seksjon + '"]'),
        function (t) { return t.getAttribute("data-flyttbar"); });
      var felt = bygger.elements.namedItem("rekkefolge_" + seksjon);
      if (felt) felt.value = nokler.join(",");
    };
    var oppdaterPiler = function () {
      bygger.querySelectorAll("tbody[data-flyttbar]").forEach(function (t) {
        var s = t.getAttribute("data-seksjon");
        var forrige = t.previousElementSibling, neste = t.nextElementSibling;
        var opp = t.querySelector('[data-flytt="opp"]'), ned = t.querySelector('[data-flytt="ned"]');
        if (opp) opp.disabled = !(forrige && forrige.getAttribute("data-seksjon") === s);
        if (ned) ned.disabled = !(neste && neste.getAttribute("data-seksjon") === s);
      });
    };
    bygger.addEventListener("click", function (e) {
      if (!(e.target instanceof Element)) return;
      var mer = e.target.closest("[data-mer]");
      if (mer) {
        var detaljer = document.getElementById(mer.getAttribute("data-mer"));
        if (!detaljer) return;
        detaljer.hidden = !detaljer.hidden;
        mer.setAttribute("aria-expanded", detaljer.hidden ? "false" : "true");
        mer.textContent = detaljer.hidden ? "Mer" : "Skjul";
        return;
      }
      var pil = e.target.closest("[data-flytt]");
      if (!pil) return;
      e.preventDefault();
      var felt = pil.closest("tbody[data-flyttbar]");
      var seksjon = felt.getAttribute("data-seksjon");
      var opp = pil.getAttribute("data-flytt") === "opp";
      var nabo = opp ? felt.previousElementSibling : felt.nextElementSibling;
      if (!nabo || nabo.getAttribute("data-seksjon") !== seksjon) return;
      if (opp) felt.parentNode.insertBefore(felt, nabo);
      else felt.parentNode.insertBefore(nabo, felt);
      oppdaterRekkefolge(seksjon);
      oppdaterPiler();
      bygger.dataset.endret = "1";
      felt.classList.add("sb-flyttet");
      setTimeout(function () { felt.classList.remove("sb-flyttet"); }, 700);
      if (pil.disabled) (opp ? felt.querySelector('[data-flytt="ned"]') : felt.querySelector('[data-flytt="opp"]')).focus();
      else pil.focus();
    });
    bygger.querySelectorAll("[data-mer]").forEach(function (knapp) {
      var detaljer = document.getElementById(knapp.getAttribute("data-mer"));
      if (detaljer && !detaljer.hidden) { knapp.setAttribute("aria-expanded", "true"); knapp.textContent = "Skjul"; }
    });
    oppdaterPiler();
  }

  // Adressen i deltakervinduet (person uten komplett adresse): feltene er ikke påkrevd fra serveren, så resten av vinduet kan lagres.
  // Endres adressen og er noe fylt ut, blir alle tre påkrevd (nettleseren peker på feltet som mangler); serveren kontrollerer det samme.
  document.addEventListener("input", function (e) {
    var gruppe = e.target instanceof Element ? e.target.closest("form[data-adressegruppe]") : null;
    if (!gruppe) return;
    var felt = gruppe.querySelectorAll("input[data-adressefelt]");
    var endret = false, noenFylt = false;
    felt.forEach(function (f) {
      if (f.value.trim() !== f.defaultValue.trim()) endret = true;
      if (f.value.trim() !== "") noenFylt = true;
    });
    felt.forEach(function (f) { f.required = endret && noenFylt; });
  });
})();
