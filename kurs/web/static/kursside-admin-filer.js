/* Kursside i administrasjonen: OPPLASTING AV FILER OG BILDER. Rå kropp (Content-Type: application/octet-stream, X-Filnavn prosentkodet, X-CSRF-Token),
   én fil per forespørsel, høyst tre samtidig, med fremdrift (XMLHttpRequest), avbryt, kontroll i nettleseren før opplasting (filendelse, størrelse, kvote)
   og nedskalering av store bilder (canvas; omkodingen fjerner også EXIF/GPS fra JPEG). Slippsoner tar imot mange filer samtidig.
   Hører sammen med kursside-admin.js (kjernen) og kursside-admin-blokker.js (blokkene som bruker filene). */
(function () {
  "use strict";
  var KS = window.KS;
  if (!KS) { return; }
  var S = KS.S, D = KS.data;
  var BILDEENDELSER = ["png", "jpg", "jpeg", "gif", "webp"];
  var MAKS_BREDDE = 1600, MAKS_SAMTIDIG = 3, teller = 0, aktive = 0;
  var partier = {};                          // blokk-id -> {totalt, ferdig, feil, neste, rekke}: for «3 filer lastet opp» og rekkefølgen filene ble valgt i

  function endelse(navn) { return (navn.split(".").pop() || "").toLowerCase(); }
  function ventende() { return S.opplastinger.filter(function (o) { return !o.ferdig && !o.feil; }); }
  KS.ventendeOpplastinger = ventende;

  /* ================= slippsoner ================= */
  function harFiler(e) { return !!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0); }
  KS.aktiverFilslipp = function (sone, fn) {
    if (!sone || !KS.skriv) { return; }
    ["dragenter", "dragover"].forEach(function (ev) {
      sone.addEventListener(ev, function (e) { if (harFiler(e)) { e.preventDefault(); e.stopPropagation(); sone.classList.add("over"); } });
    });
    sone.addEventListener("dragleave", function (e) { if (!sone.contains(e.relatedTarget)) { sone.classList.remove("over"); } });
    sone.addEventListener("drop", function (e) {
      if (!harFiler(e)) { return; }
      e.preventDefault(); e.stopPropagation();
      sone.classList.remove("over");
      var filer = Array.prototype.slice.call(e.dataTransfer.files);
      if (filer.length) { fn(filer); }
    });
  };
  KS.velgFilerTil = function (opt) {
    var inp = KS.el("input", { attr: { type: "file", hidden: "", "aria-label": "Velg fil" } });
    if (!opt.enkelt) { inp.multiple = true; }
    KS.rot.appendChild(inp);
    inp.addEventListener("change", function () {
      KS.lastOppFiler(Array.prototype.slice.call(inp.files), opt);
      if (inp.parentNode) { inp.parentNode.removeChild(inp); }
    });
    inp.click();
  };

  /* ================= kontroll før opplasting ================= */
  function sjekkFil(fil, type, ventendeStorrelse, ventendeAntall) {
    var e = endelse(fil.name), er_bilde = BILDEENDELSER.indexOf(e) >= 0;
    if (!e || D.grenser.endelser.indexOf(e) < 0) {
      return "Filtypen «" + (e ? "." + e : fil.name) + "» er ikke tillatt. Du kan laste opp PDF, PowerPoint, Word, Excel, ZIP, tekstfiler og bilder (PNG, JPEG, GIF, WEBP).";
    }
    if (type === "bilde" && !er_bilde) { return "«" + fil.name + "» er ikke et bilde. Bruk PNG, JPEG, GIF eller WEBP."; }
    if (fil.size === 0) { return "«" + fil.name + "» er tom."; }
    var maksMb = er_bilde ? D.grenser.bilde_mb : D.grenser.fil_mb;
    var kanMinskes = (e === "png" || e === "jpg" || e === "jpeg");
    if (fil.size > maksMb * 1048576 && !kanMinskes) { return "«" + fil.name + "» er for stor (maks " + maksMb + " MB). Prøv å komprimere bildene i filen (i PowerPoint: Fil › Lagre som › Verktøy › Komprimer bilder), eller legg filen i kursmappen i SharePoint."; }
    if (fil.size > 40 * 1048576) { return "«" + fil.name + "» er for stor (maks " + maksMb + " MB)."; }
    if (S.kvote.antall + ventendeAntall >= S.kvote.maks_antall) { return "Min side har nådd grensen på " + S.kvote.maks_antall + " filer. Slett filer som ikke brukes."; }
    if (S.kvote.bytes + ventendeStorrelse + fil.size > S.kvote.maks_bytes) { return "Min side har nådd grensen på " + Math.round(S.kvote.maks_bytes / 1048576) + " MB. Slett filer som ikke brukes."; }
    return null;
  }

  /* ================= nedskalering og omkoding av bilder ================= */
  function lesBilde(fil) {
    if (window.createImageBitmap) { return window.createImageBitmap(fil, { imageOrientation: "from-image" }); }
    return new Promise(function (ok, feil) {
      var url = URL.createObjectURL(fil), img = new Image();
      img.onload = function () { URL.revokeObjectURL(url); ok(img); };
      img.onerror = function () { URL.revokeObjectURL(url); feil(new Error("bilde")); };
      img.src = url;
    });
  }
  /* PNG og JPEG omkodes (JPEG mister da EXIF/GPS), og bredder over 1600 px minskes. GIF og WEBP sendes uendret. Går noe galt, sendes originalen. */
  function klargjorBilde(fil) {
    var e = endelse(fil.name);
    if (e !== "png" && e !== "jpg" && e !== "jpeg") { return Promise.resolve(fil); }
    return lesBilde(fil).then(function (bilde) {
      var b = bilde.width, h = bilde.height, k = b > MAKS_BREDDE ? MAKS_BREDDE / b : 1;
      var c = document.createElement("canvas");
      c.width = Math.round(b * k); c.height = Math.round(h * k);
      var g = c.getContext("2d");
      if (e !== "png") { g.fillStyle = "#ffffff"; g.fillRect(0, 0, c.width, c.height); }
      g.drawImage(bilde, 0, 0, c.width, c.height);
      if (bilde.close) { bilde.close(); }
      var mime = e === "png" ? "image/png" : "image/jpeg";
      return new Promise(function (ok) {
        c.toBlob(function (blob) {
          if (!blob) { ok(fil); return; }
          if (e === "png" && k === 1 && blob.size >= fil.size) { ok(fil); return; }       // PNG uten nedskalering: behold originalen hvis omkodingen ikke sparer noe
          ok(new File([blob], fil.name, { type: mime }));
        }, mime, 0.88);
      });
    }).catch(function () { return fil; });
  }

  /* ================= opplastingsrader ================= */
  function radeFor(o) {
    var r = KS.klon("ks-mal-fil-opplaster");
    var kode = KS.filtypeKode(o.navn, BILDEENDELSER.indexOf(endelse(o.navn)) >= 0 ? "bilde" : "dokument");
    KS.felt("ft", r).textContent = kode[0];
    KS.felt("navn", r).textContent = o.navn;
    KS.ks("avbryt-opplasting", r).addEventListener("click", function () {
      if (o.xhr && !o.ferdig) { o.avbrutt = true; o.xhr.abort(); }
      fjernRad(o);
    });
    o.el = r;
    visRad(o);
    return r;
  }
  function visRad(o) {
    if (!o.el) { return; }
    var p = KS.felt("prosent", o.el), pst = KS.felt("pst", o.el), under = KS.felt("under", o.el);
    p.value = o.prosent; pst.textContent = o.prosent + " %";
    o.el.classList.toggle("feil", !!o.feil);
    if (o.feil) { under.textContent = o.feil; pst.textContent = ""; p.hidden = true; KS.ks("avbryt-opplasting", o.el).setAttribute("aria-label", "Fjern meldingen"); }
    else { under.textContent = o.forbereder ? "Forbereder …" : (o.ko ? "Venter …" : "Laster opp …"); }
    if (o.bildeInfo) { o.bildeInfo.textContent = o.feil ? o.feil : (o.prosent < 100 ? "Laster opp … " + o.prosent + " %" : "Behandler …"); }
  }
  function fjernRad(o) {
    if (o.el && o.el.parentNode) { o.el.parentNode.removeChild(o.el); }
    S.opplastinger = S.opplastinger.filter(function (x) { return x !== o; });
  }
  function beholderFor(blokkId) {
    var k = KS.kortFor(blokkId);
    return k ? k.querySelector('[data-felt="opplastinger"]') : null;
  }
  KS.gjenopprettOpplastinger = function (b, kropp) {
    var beh = KS.felt("opplastinger", kropp);
    if (!beh) { return; }
    S.opplastinger.filter(function (o) { return o.blokkId === b.id && !o.ferdig; }).forEach(function (o) { beh.appendChild(radeFor(o)); });
  };

  /* ================= selve opplastingen ================= */
  var ko = [];
  KS.lastOppFiler = function (filer, opt) {
    if (!KS.skriv || !filer.length) { return; }
    var b = KS.finnBlokk(opt.blokkId);
    if (!b) { return; }
    var parti = partier[opt.blokkId] = { totalt: 0, ferdig: 0, feil: 0, neste: 0, rekke: {} };
    var venteBytes = ventende().reduce(function (a, o) { return a + o.storrelse; }, 0), venteAntall = ventende().length;
    filer.forEach(function (fil) {
      var feil = sjekkFil(fil, opt.type, venteBytes, venteAntall);
      if (feil) { KS.toast(feil, { niva: "feil" }); return; }
      var o = { id: ++teller, blokkId: opt.blokkId, navn: fil.name, storrelse: fil.size, fil: fil, prosent: 0, ferdig: false, feil: null, opt: opt, xhr: null, avbrutt: false, forbereder: false, ko: true, parti: parti, rekke: parti.neste++ };
      venteBytes += fil.size; venteAntall++; parti.totalt++;
      S.opplastinger.push(o);
      var beh = beholderFor(opt.blokkId);
      if (beh && opt.type !== "bilde") { beh.appendChild(radeFor(o)); }
      if (opt.type === "bilde") { var k = KS.kortFor(opt.blokkId); o.bildeInfo = k ? k.querySelector('[data-felt="bildeinfo"]') : null; visRad(o); }
      ko.push(o);
    });
    neste();
  };
  function neste() {
    while (aktive < MAKS_SAMTIDIG && ko.length) { start(ko.shift()); }
  }
  function start(o) {
    aktive++;
    o.ko = false; o.forbereder = true; visRad(o);
    klargjorBilde(o.fil).then(function (fil) {
      if (o.avbrutt || S.opplastinger.indexOf(o) < 0) { aktive--; neste(); return; }
      var maksMb = BILDEENDELSER.indexOf(endelse(fil.name)) >= 0 ? D.grenser.bilde_mb : D.grenser.fil_mb;
      if (fil.size > maksMb * 1048576) { feilet(o, "«" + o.navn + "» er for stor (maks " + maksMb + " MB), selv etter at bildet er minsket."); aktive--; neste(); return; }
      o.storrelse = fil.size; o.forbereder = false; visRad(o);
      var xhr = new XMLHttpRequest();
      o.xhr = xhr;
      xhr.open("POST", D.urls.fil_opp);
      xhr.setRequestHeader("X-CSRF-Token", KS.csrf);
      xhr.setRequestHeader("X-Filnavn", encodeURIComponent(o.navn));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = function (e) { if (e.lengthComputable) { o.prosent = Math.round(100 * e.loaded / e.total); visRad(o); } };
      xhr.onload = function () {
        aktive--;
        var type = xhr.getResponseHeader("Content-Type") || "", j = null;
        if (type.indexOf("json") >= 0) { try { j = JSON.parse(xhr.responseText); } catch (x) { j = null; } }
        if (!j) {
          if (xhr.status === 413) { feilet(o, "«" + o.navn + "» er for stor."); } else { feilet(o, "Ikke lastet opp – du er logget ut eller har ikke tilgang."); KS.loggetUt(); }
        } else if (xhr.status === 200 && j.status === "ok") { ferdig(o, j); }
        else { feilet(o, "«" + o.navn + "»: " + (j.melding || "kunne ikke lastes opp.")); }
        neste();
      };
      xhr.onerror = function () { aktive--; feilet(o, "«" + o.navn + "»: nettverksfeil. Prøv igjen."); neste(); };
      xhr.onabort = function () { aktive--; neste(); };
      xhr.send(fil);
    });
  }
  function feilet(o, tekst) {
    o.feil = tekst; o.parti.feil++;
    visRad(o);
    if (o.opt.type === "bilde") { KS.toast(tekst, { niva: "feil" }); setTimeout(function () { fjernRad(o); }, 100); }
  }
  /* Filene lastes opp samtidig og blir ferdige i tilfeldig rekkefølge (store filer sist). De settes inn i den rekkefølgen administratoren valgte:
     rett foran første fil fra samme opplasting som skulle stå etter denne, ellers sist. */
  KS.settInnIRekkefolge = settInnIRekkefolge;
  function settInnIRekkefolge(liste, element, o) {
    var rekke = o.parti.rekke, pos = liste.length;
    for (var i = 0; i < liste.length; i++) {
      var r = rekke[liste[i].fil_id];
      if (r !== undefined && r > o.rekke) { pos = i; break; }
    }
    liste.splice(pos, 0, element);
    rekke[element.fil_id] = o.rekke;
  }
  function ferdig(o, j) {
    o.ferdig = true; o.prosent = 100;
    var fil = j.fil;
    if (!S.filer.some(function (f) { return f.id === fil.id; })) { S.filer.push(fil); }
    S.kvote = j.kvote;
    var b = KS.finnBlokk(o.blokkId), opt = o.opt;
    fjernRad(o);
    o.parti.ferdig++;
    if (!b) { return; }
    KS.snapshot();
    if (opt.type === "bilde") { b.data.fil_id = fil.id; }
    else if (opt.erstatt) { opt.erstatt.fil_id = fil.id; }
    else if (!b.data.filer.some(function (e) { return e.fil_id === fil.id; })) {
      if (b.data.filer.length >= D.grenser.filer_per_blokk) { KS.toast("Blokken kan ha høyst " + D.grenser.filer_per_blokk + " filer.", { niva: "feil" }); return; }
      settInnIRekkefolge(b.data.filer, { fil_id: fil.id, tittel: KS.tittelFraFilnavn(fil.filnavn), gruppe: null, synlig_fra: null }, o);
    } else { KS.toast("«" + fil.filnavn + "» er allerede med i blokken.", {}); }
    if (j.duplikat) { KS.toast("Denne filen var allerede lastet opp som «" + fil.filnavn + "».", {}); }
    KS.endret({ struktur: true, blokkId: b.id });
    KS.tegnBody(b.id);
    var p = o.parti;
    if (p.ferdig + p.feil >= p.totalt && p.totalt > 0) {
      if (p.totalt > 1 || !j.duplikat) { KS.toast(p.ferdig === 1 ? "«" + fil.filnavn + "» er lastet opp." : p.ferdig + " filer er lastet opp." + (p.feil ? " " + p.feil + " ble ikke lastet opp." : ""), { niva: "ok" }); }
      partier[o.blokkId] = null;
    }
  }

  /* En fil eller et bilde som slippes utenfor en slippsone skal aldri åpnes i nettleseren og dermed kaste bort arbeidet. */
  KS.init.push(function () {
    if (!KS.skriv) { return; }
    window.addEventListener("dragover", function (e) { if (harFiler(e)) { e.preventDefault(); } });
    window.addEventListener("drop", function (e) {
      if (!harFiler(e)) { return; }
      e.preventDefault();
      KS.toast("Slipp filene i sonen «Slipp filene her» i en Filer-blokk (eller i en Bilde-blokk).", { niva: "feil" });
    });
  });
})();
