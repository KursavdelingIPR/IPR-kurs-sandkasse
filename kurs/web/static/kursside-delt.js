/* Kursside for deltakere: tre små ting, ingen bibliotek og ingen kode fra tekst (CSP: bare denne filen, lastet med nonce).
     1. Utskrift: alle programdager åpnes før utskrift og lukkes igjen etterpå. «Skriv ut siden»-knappen (data-skriv-ut) åpner utskriftsvinduet.
     2. «Hopp til»-knappene og «På denne siden» markerer blokken du leser (aria-current), og knappen rulles til syne i raden.
   Siden virker fullt ut uten denne filen (alle lenker er vanlige #anker). Forhåndsvisningen i en ramme laster ikke skript. */
(function () {
  "use strict";

  var apnet = [];
  function apneAlle() {
    apnet = [];
    document.querySelectorAll("details.dp-pr-dag").forEach(function (d) { if (!d.open) { apnet.push(d); d.open = true; } });
  }
  function lukkIgjen() {
    apnet.forEach(function (d) { d.open = false; });
    apnet = [];
  }
  window.addEventListener("beforeprint", apneAlle);
  window.addEventListener("afterprint", lukkIgjen);
  document.querySelectorAll("[data-skriv-ut]").forEach(function (knapp) {
    knapp.addEventListener("click", function () { window.print(); });
  });

  var seksjoner = Array.prototype.slice.call(document.querySelectorAll(".dp-hoved > section[id^='blokk-']"));
  var lenker = Array.prototype.slice.call(document.querySelectorAll(".dp-chips a[href^='#blokk-'], .dp-meny a[href^='#blokk-']"));
  if (!seksjoner.length || !lenker.length) { return; }

  var ventende = false;
  function oppdater() {
    ventende = false;
    var aktiv = null;
    seksjoner.forEach(function (s) { if (s.getBoundingClientRect().top <= 120) { aktiv = s.id; } });
    lenker.forEach(function (a) {
      var er = aktiv !== null && a.getAttribute("href") === "#" + aktiv;
      a.classList.toggle("dp-aktiv", er);
      if (er) { a.setAttribute("aria-current", "true"); } else { a.removeAttribute("aria-current"); }
      var rad = a.parentNode;
      if (er && rad && rad.classList && rad.classList.contains("dp-chips") && rad.scrollWidth > rad.clientWidth) {
        rad.scrollLeft = Math.max(0, a.offsetLeft - (rad.clientWidth - a.offsetWidth) / 2);
      }
    });
  }
  function planlegg() {
    if (!ventende) { ventende = true; window.requestAnimationFrame(oppdater); }
  }
  window.addEventListener("scroll", planlegg, { passive: true });
  window.addEventListener("resize", planlegg);
  oppdater();
})();
