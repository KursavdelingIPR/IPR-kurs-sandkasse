/* Felles, liten hjelper for admin og offentlige sider. Ingen rammeverk.
 * All oppførsel styres av data-attributter i HTML - aldri inline-JS (CSP), og aldri tekst fra databasen inne i JS.
 *
 *   <form data-bekreft="Melde av?">            spør før innsending (teksten er ren tekst, satt av Jinja med escaping)
 *   <select data-send-ved-endring>             sender skjemaet når verdien endres
 *   <input type=radio data-vis="org" ...>      viser elementet med id=org når valgt, data-skjul="org" skjuler det
 *   <select data-skjul-hvis="samlet" data-mal="delfelt">   skjuler #delfelt når verdien er 'samlet'
 *   <form data-en-gang>                        knappen låses etter første innsending (dobbeltklikk-vern)
 *   <form data-status-bekreft>                 «Endre status til X?» der X er valgt verdi i select[name=status]
 *   <button type=button data-skriv-ut>         åpner nettleserens utskrift (der kan man også velge «Lagre som PDF»)
 *   <div data-ulagret-vakt>                    rundt flere skjema med data-skjemanavn="…": advarer før ett av dem sendes (eller
 *                                              vinduet lukkes) når et ANNET har endringer som ellers går tapt
 *   <a data-deltaker-vindu href="…">           åpner deltakersiden i vinduet <dialog id="deltaker-vindu"> over listen
 *                                              (uten JS, eller med Ctrl/Cmd-klikk, er det en vanlig lenke)
 *   <button type=button data-sett-inn="{fornavn}" data-felt="f-tekst">   flettefelt: setter teksten inn der markøren
 *                                              står i feltet (eller erstatter det som er markert)
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
    if (form.hasAttribute("data-status-bekreft")) {
      var valg = form.querySelector("select[name=status]");
      tekst = "Endre status til " + (valg ? valg.value : "") + "?";
    }
    if (tekst && !window.confirm(tekst)) { e.preventDefault(); return; }
    var ulagret = ulagredeSkjema(form.closest("[data-ulagret-vakt]"), form);
    if (ulagret && !window.confirm("Du har endringer som ikke er lagret i: " + ulagret +
        ". De går tapt hvis du fortsetter. Vil du fortsette?")) {
      e.preventDefault();
      return;
    }
    if (form.hasAttribute("data-type-sted-vakt")) {
      var harBekreftede = form.getAttribute("data-type-sted-vakt");
      var opprType = form.getAttribute("data-opprinnelig-type") || "";
      var opprSted = form.getAttribute("data-opprinnelig-sted") || "";
      if (harBekreftede !== "0" && (form.type.value !== opprType || form.sted.value !== opprSted)) {
        if (!window.confirm("Dette kurset har allerede " + harBekreftede + " bekreftede deltakere. Å endre type eller sted " +
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

  // «Velg alle synlige» i deltakerlisten
  var velgAlle = document.getElementById("velg-alle-synlige");
  if (velgAlle) {
    var bokser = document.querySelectorAll(".valgt-deltaker");
    var visning = document.getElementById("antall-valgt");
    var oppdater = function () {
      var n = document.querySelectorAll(".valgt-deltaker:checked").length;
      if (visning) visning.textContent = n ? "– " + n + " valgt" : "";
    };
    velgAlle.addEventListener("change", function () {
      bokser.forEach(function (b) { b.checked = velgAlle.checked; });
      oppdater();
    });
    bokser.forEach(function (b) { b.addEventListener("change", oppdater); });
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
})();
