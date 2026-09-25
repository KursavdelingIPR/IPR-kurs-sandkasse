/* Felles, liten hjelper for admin og offentlige sider. Ingen rammeverk.
 * All oppførsel styres av data-attributter i HTML - aldri inline-JS (CSP), og aldri tekst fra databasen inne i JS.
 *
 *   <form data-bekreft="Melde av?">            spør før innsending (teksten er ren tekst, satt av Jinja med escaping)
 *   <select data-send-ved-endring>             sender skjemaet når verdien endres
 *   <input type=radio data-vis="org" ...>      viser elementet med id=org når valgt, data-skjul="org" skjuler det
 *   <select data-skjul-hvis="samlet" data-mal="delfelt">   skjuler #delfelt når verdien er 'samlet'
 *   <form data-en-gang>                        knappen låses etter første innsending (dobbeltklikk-vern)
 *   <form data-status-bekreft>                 «Endre status til X?» der X er valgt verdi i select[name=status]
 */
(function () {
  "use strict";

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
})();
