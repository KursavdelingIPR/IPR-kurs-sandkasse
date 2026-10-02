/* Signatureditoren (E-postmaler → Signaturer, kursets signatur og «Send e-post»). Ingen tredjepartsbibliotek: nettleserens
 * innebygde redigering (contenteditable + execCommand). Serveren renser ALT som sendes inn (kurs/signaturer.py) - dette er
 * bare verktøyet. Ingen tekst fra databasen står i denne filen; alt kommer fra data-attributter og <template>-elementer.
 *
 *   <div data-signatureditor>                   verktøylinje + .signaturflate[contenteditable][data-felt=id på skjult felt]
 *   <select data-velg-signatur="flate-id">       bytter innholdet i editoren til <template data-signaturmal="verdi">
 *   <input type=radio data-signaturmal-kilde="standard">   samme, fra radioknappen (kursets signatur)
 */
(function () {
  "use strict";

  var lagret = new WeakMap();          // flate -> sist markerte område i den

  function felt(flate) { return document.getElementById(flate.getAttribute("data-felt")); }

  function synk(flate) {
    var f = felt(flate);
    if (!f) return;
    f.value = flate.innerHTML;
    f.dispatchEvent(new Event("input", { bubbles: true }));     // app.js merker skjemaet som endret
  }

  function husk(flate) {
    var s = window.getSelection();
    if (s && s.rangeCount && flate.contains(s.getRangeAt(0).commonAncestorContainer)) lagret.set(flate, s.getRangeAt(0).cloneRange());
  }

  function gjenopprett(flate) {
    flate.focus();
    var r = lagret.get(flate);
    if (r) { var s = window.getSelection(); s.removeAllRanges(); s.addRange(r); }
  }

  function kjor(flate, kommando, verdi, somCss) {
    gjenopprett(flate);
    try { document.execCommand("styleWithCSS", false, !!somCss); } catch (e) { /* eldre nettleser */ }
    document.execCommand(kommando, false, verdi == null ? null : verdi);
    try { document.execCommand("styleWithCSS", false, false); } catch (e) { /* eldre nettleser */ }
    husk(flate);
    endret(flate);
  }

  function endret(flate) {
    flate.dataset.endret = "1";
    synk(flate);
    var egen = flate.closest("form") && flate.closest("form").querySelector("input[type=radio][name=modus][value=egen]");
    if (egen && !egen.checked && flate.dataset.lastet !== "1") egen.checked = true;   // kursets signatur: egen versjon
  }

  function lastMal(flate, verdi) {
    var skjema = flate.closest("form") || document;
    var mal = skjema.querySelector('template[data-signaturmal="' + verdi + '"]') ||
              document.querySelector('template[data-signaturmal="' + verdi + '"]');
    if (!mal) return false;
    flate.innerHTML = mal.innerHTML;
    flate.dataset.lastet = "1";
    synk(flate);
    delete flate.dataset.lastet;
    delete flate.dataset.endret;
    return true;
  }

  function lenke(flate) {
    var url = window.prompt("Lenke (https://…, mailto:… eller tel:…):", "https://");
    if (!url) return;
    url = url.trim();
    if (!/^(https?:\/\/[^\s]+|mailto:[^\s]+|tel:[+\d() -]+)$/i.test(url)) {
      window.alert("Lenken må begynne med https://, http://, mailto: eller tel:");
      return;
    }
    kjor(flate, "createLink", url);
  }

  function settInnBilde(flate, valg) {
    var o = valg.options[valg.selectedIndex];
    var img = document.createElement("img");
    img.setAttribute("src", "/admin/signaturer/bilde/" + o.value);
    img.setAttribute("alt", o.getAttribute("data-alt") || "");
    var bredde = parseInt(o.getAttribute("data-bredde"), 10) || 200;
    img.setAttribute("width", String(Math.min(bredde, 300)));
    kjor(flate, "insertHTML", img.outerHTML);
  }

  document.addEventListener("DOMContentLoaded", function () {
    try { document.execCommand("defaultParagraphSeparator", false, "div"); } catch (e) { /* eldre nettleser */ }

    document.querySelectorAll("[data-signatureditor]").forEach(function (boks) {
      var flate = boks.querySelector(".signaturflate[contenteditable=true]");
      if (!flate) return;
      var breddevalg = boks.querySelector("[data-bildebredde]");
      var valgtBilde = null;

      flate.addEventListener("keyup", function () { husk(flate); });
      flate.addEventListener("mouseup", function () { husk(flate); });
      flate.addEventListener("input", function () { endret(flate); });
      flate.addEventListener("drop", function (e) {      // bilder fra disken må lastes opp i biblioteket først
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
          e.preventDefault();
          window.alert("Last opp bildet under Signaturer → Bilder, og sett det inn med «Sett inn bilde».");
        }
      });
      flate.addEventListener("click", function (e) {
        if (valgtBilde) valgtBilde.classList.remove("valgt");
        valgtBilde = e.target.tagName === "IMG" ? e.target : null;
        if (valgtBilde) valgtBilde.classList.add("valgt");
        if (breddevalg) breddevalg.disabled = !valgtBilde;
      });

      boks.addEventListener("mousedown", function (e) {  // behold markeringen i editoren når en knapp trykkes
        if (e.target.closest("button[data-kommando]")) e.preventDefault();
      });
      boks.addEventListener("click", function (e) {
        var knapp = e.target.closest("button[data-kommando]");
        if (!knapp) return;
        e.preventDefault();
        var k = knapp.getAttribute("data-kommando");
        if (k === "createLink") lenke(flate);
        else kjor(flate, k);
      });
      boks.addEventListener("change", function (e) {
        var s = e.target;
        if (!s.value && !s.matches("[data-bildebredde]")) return;
        if (s.matches("[data-skrift]")) kjor(flate, "fontName", s.value, true);
        else if (s.matches("[data-storrelse]")) kjor(flate, "fontSize", s.value, true);
        else if (s.matches("[data-farge]")) kjor(flate, "foreColor", s.value, true);
        else if (s.matches("[data-bilde]")) settInnBilde(flate, s);
        else if (s.matches("[data-bildebredde]") && valgtBilde) {
          valgtBilde.removeAttribute("height");
          if (s.value === "0") valgtBilde.removeAttribute("width"); else valgtBilde.setAttribute("width", s.value);
          endret(flate);
        } else return;
        if (!s.matches("[data-bildebredde]")) s.value = "";
      });
      var skjema = flate.closest("form");
      if (skjema) skjema.addEventListener("submit", function () {
        if (valgtBilde) valgtBilde.classList.remove("valgt");
        synk(flate);
      }, true);
    });

    // Bytte signatur: innholdet hentes fra <template data-signaturmal="…"> (renset på serveren)
    document.querySelectorAll("select[data-velg-signatur]").forEach(function (valg) {
      var forrige = valg.value;
      valg.addEventListener("change", function () {
        var flate = document.getElementById(valg.getAttribute("data-velg-signatur"));
        if (!flate || !valg.value) return;
        if (flate.dataset.endret && !window.confirm("Bytte signatur? Endringene du har gjort i signaturen forsvinner.")) {
          valg.value = forrige;
          return;
        }
        if (lastMal(flate, valg.value)) {
          forrige = valg.value;
          var bibliotek = valg.form && valg.form.querySelector("input[type=radio][name=modus][value=bibliotek]");
          if (bibliotek) bibliotek.checked = true;
        }
      });
    });
    document.querySelectorAll("input[type=radio][data-signaturmal-kilde]").forEach(function (radio) {
      radio.addEventListener("change", function () {
        var flate = radio.form && radio.form.querySelector(".signaturflate[contenteditable=true]");
        if (!flate || !radio.checked) return;
        if (flate.dataset.endret && !window.confirm("Bruke standardsignaturen? Endringene du har gjort i signaturen forsvinner.")) {
          var egen = radio.form.querySelector("input[type=radio][name=modus][value=egen]");
          if (egen) egen.checked = true;
          return;
        }
        lastMal(flate, radio.getAttribute("data-signaturmal-kilde"));
      });
    });
  });
})();
