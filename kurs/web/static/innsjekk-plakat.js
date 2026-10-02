/* Innsjekk-plakaten (admin_qr.html): laster siden på nytt hvert N. sekund (data-oppdater på <body>), så tallet «sjekket inn» holder seg
   oppdatert på skjermen i kurslokalet. Ikke mens plakaten skrives ut. Ingen bibliotek, ingen kode fra tekst (CSP: lastet med nonce).
   Uten JavaScript sørger <noscript><meta refresh> i siden for det samme. */
(function () {
  "use strict";
  var skriverUt = false;
  window.addEventListener("beforeprint", function () { skriverUt = true; });
  window.addEventListener("afterprint", function () { skriverUt = false; });
  var sekunder = parseInt(document.body.getAttribute("data-oppdater") || "0", 10);
  if (sekunder > 0) {
    window.setInterval(function () { if (!skriverUt) { window.location.reload(); } }, sekunder * 1000);
  }
})();
