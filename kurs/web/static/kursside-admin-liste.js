/* Kursside i administrasjonen: BLOKKLISTEN (kort, åpne/lukke, flytte, dupliser, slett, skjul, rask omorganisering, legg til blokk, popover-menyer).
   Hører sammen med kursside-admin.js (kjernen) og kursside-admin-blokker.js (innholdet i hver blokktype). Se toppkommentaren i kjernefilen for regler. */
(function () {
  "use strict";
  var KS = window.KS;
  if (!KS) { return; }
  var S = KS.S, D = KS.data;
  var liste = null;
  KS.typeNavn = { tekst: "Tekst", viktig: "Viktig", program: "Program", filer: "Filer", lenker: "Lenker", tabell: "Grupper/tabell", kontakt: "Kontakt", bilde: "Bilde" };

  /* ================= tekst om innholdet ================= */
  KS.tekstAvHtml = function (html) {
    var doc = new DOMParser().parseFromString("<body>" + (html || "") + "</body>", "text/html");
    return (doc.body.textContent || "").replace(/ /g, " ");
  };
  /* Lik regel som serveren (sideinnhold.blokk_er_tom): en tom blokk vises ikke for deltakerne. */
  KS.blokkErTom = function (b) {
    var d = b.data || {}, t = b.type;
    if (t === "tekst") { return !KS.tekstAvHtml(d.html).trim(); }
    if (t === "viktig") { return !(d.tekst || "").trim(); }
    if (t === "program") { return !(d.dager || []).some(function (x) { return (x.punkter || []).length > 0; }); }
    if (t === "filer") { return !(d.filer || []).length && !d.sharepoint; }
    if (t === "lenker") { return !(d.lenker || []).some(function (e) { return e.url || e.kilde === "zoom"; }); }
    if (t === "tabell") { return !(d.rader || []).some(function (r) { return r.some(function (c) { return (c || "").trim(); }); }); }
    if (t === "kontakt") { return !(d.personer || []).some(function (p) { return ["navn", "rolle", "telefon", "epost"].some(function (f) { return (p[f] || "").trim(); }); }); }
    if (t === "bilde") { return d.fil_id === null || d.fil_id === undefined; }
    return true;
  };
  KS.sammendrag = function (b) {
    var d = b.data || {}, t = b.type;
    if (t === "tekst") { var n = KS.tekstAvHtml(d.html).trim().length; return n ? n + " tegn" : "Ingen tekst ennå"; }
    if (t === "viktig") { var l = (d.tekst || "").split("\n")[0]; return l ? l : "Ingen tekst ennå"; }
    if (t === "program") {
      var dager = d.dager || [], p = dager.reduce(function (a, x) { return a + (x.punkter || []).length; }, 0);
      return dager.length ? KS.flertall(dager.length, "dag", "dager") + " · " + KS.flertall(p, "punkt", "punkter") : "Ingen dager ennå";
    }
    if (t === "filer") {
      var f = (d.filer || []).length;
      return (f ? KS.flertall(f, "fil", "filer") : "Ingen filer ennå") + (d.sharepoint ? " + kursholders filer" : "");
    }
    if (t === "lenker") { return KS.flertall((d.lenker || []).length, "lenke", "lenker"); }
    if (t === "tabell") { return KS.flertall((d.rader || []).length, "rad", "rader"); }
    if (t === "kontakt") { return KS.flertall((d.personer || []).length, "person", "personer"); }
    if (t === "bilde") {
      var fil = S.filer.filter(function (x) { return x.id === d.fil_id; })[0];
      return fil ? fil.filnavn + (d.alt ? " · alternativ tekst er fylt ut" : " · mangler alternativ tekst") : "Ikke valgt ennå";
    }
    return "";
  };
  KS.nyBlokk = function (type) {
    var data = {
      tekst: { html: "" }, viktig: { niva: "info", tekst: "" },
      program: { dager: D.kursdager.map(function (kd, i) { return { kursdag_id: kd.id, dato: kd.dato, tittel: "Dag " + (i + 1), punkter: [] }; }) },
      filer: { filer: [], sharepoint: false, vis_kommende: true }, lenker: { lenker: [] },
      tabell: { kolonner: ["Gruppe", "Deltakere"], rader: [["", ""]] }, kontakt: { personer: [] },
      bilde: { fil_id: null, alt: "", tekst: "", plassering: "bred" }
    }[type];
    return { id: KS.nyId(), type: type, tittel: KS.standardTittel[type], skjult: false, vis_fra: null, i_meny: true, data: data };
  };

  /* ================= kort ================= */
  function kortFor(id) { return liste ? liste.querySelector('[data-ks="blokk"][data-id="' + id + '"]') : null; }
  KS.kortFor = kortFor;
  function alleKort() { return KS.$$('[data-ks="blokk"]', liste); }
  function merkeEl(klasse, ikon, tekst) { return KS.el("span", { klasse: "ks-merke " + klasse, barn: [ikon ? KS.ikon(ikon) : null, tekst] }); }
  function oppdaterKortTekst(kort, b) {
    var tittel = b.tittel || KS.standardTittel[b.type];
    KS.felt("tittel-tekst", kort).textContent = tittel;
    KS.felt("sammendrag", kort).textContent = KS.sammendrag(b);
    kort.setAttribute("aria-label", "Blokk: " + tittel);
    kort.classList.toggle("skjult", !!b.skjult);
    var m = KS.felt("merker", kort);
    KS.tom(m);
    if (b.skjult) { m.appendChild(merkeEl("", "oyeav", "Skjult")); }
    if (b.vis_fra) { m.appendChild(merkeEl("fra", "klokke", "Vises fra " + KS.datoKort(b.vis_fra))); }
    if (KS.blokkErTom(b)) { m.appendChild(merkeEl("", null, "Tom – vises ikke")); }
    var st = KS.blokkStatus(b);
    if (st.ny) { m.appendChild(merkeEl("ny", "pluss", "Ny")); } else if (st.endret) { m.appendChild(merkeEl("endret", "hake", "Endret")); }
  }
  KS.oppdaterKort = function (id) { var k = kortFor(id), b = KS.finnBlokk(id); if (k && b) { oppdaterKortTekst(k, b); } };
  KS.oppdaterMerker = function () { if (!liste) { return; } alleKort().forEach(function (k) { var b = KS.finnBlokk(k.getAttribute("data-id")); if (b) { oppdaterKortTekst(k, b); } }); };

  function byggKort(b) {
    var kort = KS.klon("ks-mal-blokk");
    kort.setAttribute("data-id", b.id);
    var typeEl = KS.felt("type", kort);
    typeEl.classList.add("ks-t-" + b.type);
    typeEl.replaceChild(KS.ikon(KS.blokkIkon[b.type]), KS.felt("type-ikon", kort));
    KS.felt("type-navn", kort).textContent = KS.typeNavn[b.type];
    var tittel = KS.ks("tittel", kort);
    tittel.value = b.tittel;
    tittel.setAttribute("placeholder", KS.standardTittel[b.type]);
    tittel.addEventListener("input", function () { b.tittel = tittel.value; oppdaterKortTekst(kort, b); KS.endret({ blokkId: b.id }); });
    var velg = KS.ks("velg", kort);
    velg.checked = S.valgt.has(b.id);
    kort.classList.toggle("valgt", velg.checked);
    oppdaterKortTekst(kort, b);
    KS.laas(kort);
    if (!KS.skriv) { tittel.disabled = true; velg.disabled = true; }
    return kort;
  }

  function settApen(kort, apen, fokusTittel) {
    var id = kort.getAttribute("data-id"), b = KS.finnBlokk(id);
    var innhold = KS.ks("innhold", kort);
    kort.classList.toggle("apen", apen);
    innhold.hidden = !apen;
    if (apen) {
      S.apen.add(id);
      if (!kort.__bygget) { kort.__bygget = true; KS.byggBody(b, innhold); }
    } else { S.apen.delete(id); }
    var fold = KS.ks("fold", kort), hode = KS.ks("sammendrag", kort);
    fold.setAttribute("aria-expanded", apen ? "true" : "false");
    fold.setAttribute("aria-label", apen ? "Lukk blokken" : "Åpne blokken");
    hode.setAttribute("aria-expanded", apen ? "true" : "false");
    if (apen && fokusTittel) { var t = KS.ks("tittel", kort); t.focus(); t.select(); }
    oppdaterFoldAlle();
  }
  KS.apneBlokk = function (id, fokusTittel) {
    var k = kortFor(id);
    if (!k) { return; }
    if (S.rask) { KS.setRask(false); k = kortFor(id); }
    settApen(k, true, fokusTittel);
    k.scrollIntoView({ block: "center", behavior: "smooth" });
  };
  /* Bygger innholdet i en åpen blokk på nytt (etter at data er endret utenfra, for eksempel en opplasting). */
  KS.tegnBody = function (id) {
    var k = kortFor(id), b = KS.finnBlokk(id);
    if (!k || !b) { return; }
    var innhold = KS.ks("innhold", k);
    KS.tom(innhold);
    k.__bygget = false;
    if (k.classList.contains("apen")) { k.__bygget = true; KS.byggBody(b, innhold); }
    oppdaterKortTekst(k, b);
  };

  function mellom(indeks) {
    var d = KS.el("div", { klasse: "ks-mellom" });
    var b = KS.el("button", { klasse: "", attr: { type: "button", "data-ks": "innsett", "data-indeks": String(indeks), "aria-label": "Legg til en blokk her", title: "Legg til en blokk her" }, barn: [KS.ikon("pluss")] });
    d.appendChild(b);
    return d;
  }

  KS.tegnListe = function (opt) {
    opt = opt || {};
    if (!liste) { return; }
    var aktivt = document.activeElement, fokusId = null;
    if (aktivt && liste.contains(aktivt)) { var k = aktivt.closest('[data-ks="blokk"]'); fokusId = k ? k.getAttribute("data-id") : null; }
    KS.tom(liste);
    liste.classList.toggle("ks-rask", S.rask);
    var kanRedigere = KS.skriv && !S.rask;
    S.dok.blokker.forEach(function (b, i) {
      if (kanRedigere) { liste.appendChild(mellom(i)); }
      var kort = byggKort(b);
      liste.appendChild(kort);
      if (S.apen.has(b.id) && !S.rask) { settApen(kort, true); }
    });
    if (kanRedigere && S.dok.blokker.length) { liste.appendChild(mellom(S.dok.blokker.length)); }
    var n = S.dok.blokker.length;
    KS.ks("antall").textContent = KS.flertall(n, "blokk", "blokker") + (S.rask && S.valgt.size ? " · " + S.valgt.size + " valgt" : "");
    KS.ks("tom").hidden = n > 0;
    oppdaterBulk(); oppdaterFoldAlle();
    var fokus = opt.fokus || fokusId;
    if (fokus && kortFor(fokus)) { kortFor(fokus).focus({ preventScroll: !!opt.ikkeRull }); }
  };

  function oppdaterFoldAlle() {
    var knapp = KS.ks("fold-alle");
    if (!knapp) { return; }
    var noenApne = S.dok.blokker.some(function (b) { return S.apen.has(b.id); });
    knapp.setAttribute("data-tilstand", noenApne ? "lukk" : "apne");
    var tekst = knapp.querySelector("span");
    tekst.textContent = noenApne ? "Fold sammen alle" : "Fold ut alle";
    knapp.replaceChild(KS.ikon(noenApne ? "opp" : "ned"), knapp.querySelector("svg"));
    knapp.disabled = !S.dok.blokker.length || S.rask;
  }

  /* ================= flytte, duplisere, slette ================= */
  function announcePlass(id) {
    var i = KS.blokkIndeks(id);
    KS.si("Flyttet til plass " + (i + 1) + " av " + S.dok.blokker.length);
  }
  /* Flytter blokkene `ider` (i dokumentets rekkefølge) slik at de står samlet foran elementet med indeks `til` i den opprinnelige listen. */
  KS.flyttBlokker = function (ider, til) {
    KS.snapshot();
    var bl = S.dok.blokker, valgt = bl.filter(function (b) { return ider.indexOf(b.id) >= 0; });
    var foran = bl.slice(0, til).filter(function (b) { return ider.indexOf(b.id) >= 0; }).length;
    var rest = bl.filter(function (b) { return ider.indexOf(b.id) < 0; });
    var ny = til - foran;
    S.dok.blokker = rest.slice(0, ny).concat(valgt, rest.slice(ny));
    KS.endret({ struktur: true, blokkId: valgt[0].id });
    KS.tegnListe({ fokus: valgt[0].id, ikkeRull: true });
    announcePlass(valgt[0].id);
  };
  function flyttEn(id, retning) {
    var i = KS.blokkIndeks(id), n = S.dok.blokker.length;
    var ny = i + retning;
    if (ny < 0 || ny >= n) { KS.si(retning < 0 ? "Blokken står allerede øverst" : "Blokken står allerede nederst"); return; }
    KS.flyttBlokker([id], retning < 0 ? i - 1 : i + 2);
  }
  KS.flyttEn = flyttEn;
  KS.dupliser = function (ider) {
    KS.snapshot();
    var ut = [], siste = null;
    S.dok.blokker.forEach(function (b) {
      ut.push(b);
      if (ider.indexOf(b.id) >= 0) {
        var kopi = KS.kopi(b);
        kopi.id = KS.nyId();
        var t = (b.tittel || KS.standardTittel[b.type]) + " (kopi)";
        kopi.tittel = t.length > D.grenser.tittel ? t.slice(0, D.grenser.tittel) : t;
        ut.push(kopi); siste = kopi;
      }
    });
    if (ut.length > D.grenser.blokker) { KS.toast("Siden kan ha høyst " + D.grenser.blokker + " blokker.", { niva: "feil" }); return; }
    S.dok.blokker = ut;
    S.valgt = new Set();
    KS.endret({ struktur: true, blokkId: siste && siste.id });
    KS.tegnListe({ fokus: siste && siste.id });
    KS.toast(ider.length === 1 ? "Blokken er duplisert." : ider.length + " blokker er duplisert.", { angre: KS.angre });
  };
  KS.slettBlokker = function (ider) {
    KS.snapshot();
    var navn = ider.length === 1 ? (KS.finnBlokk(ider[0]).tittel || KS.standardTittel[KS.finnBlokk(ider[0]).type]) : "";
    S.dok.blokker = S.dok.blokker.filter(function (b) { return ider.indexOf(b.id) < 0; });
    ider.forEach(function (id) { S.apen.delete(id); S.valgt.delete(id); });
    KS.endret({ struktur: true });
    KS.tegnListe();
    KS.toast(ider.length === 1 ? "Blokken «" + navn + "» er slettet." : ider.length + " blokker er slettet.", { angre: KS.angre });
  };
  function settSkjult(ider, verdi) {
    KS.snapshot();
    S.dok.blokker.forEach(function (b) { if (ider.indexOf(b.id) >= 0) { b.skjult = verdi; } });
    KS.endret({ struktur: true });
    KS.tegnListe();
    KS.toast(ider.length === 1 ? (verdi ? "Blokken er skjult for deltakerne." : "Blokken vises for deltakerne.") : ider.length + " blokker er " + (verdi ? "skjult." : "vist."), { angre: KS.angre });
  }
  KS.leggTil = function (type, indeks) {
    if (S.dok.blokker.length >= D.grenser.blokker) { KS.toast("Siden kan ha høyst " + D.grenser.blokker + " blokker.", { niva: "feil" }); return; }
    KS.snapshot();
    var b = KS.nyBlokk(type);
    var i = indeks;
    if (i === undefined || i === null) { var fi = S.fokusId ? KS.blokkIndeks(S.fokusId) : -1; i = fi >= 0 ? fi + 1 : S.dok.blokker.length; }
    S.dok.blokker.splice(i, 0, b);
    S.apen.add(b.id);
    S.fokusId = b.id;
    KS.endret({ struktur: true, blokkId: b.id });
    if (S.rask) { S.rask = false; syncRaskUi(); }
    KS.tegnListe({ ikkeRull: true });
    var k = kortFor(b.id);
    if (k) { settApen(k, true, true); k.scrollIntoView({ block: "center", behavior: "smooth" }); }
    KS.si("Blokken «" + b.tittel + "» er lagt til. Du står i tittelfeltet.");
  };

  /* ================= popover-meny ================= */
  var meny = null, menyAnker = null;
  function lukkMeny(returFokus) {
    if (meny) { meny.parentNode.removeChild(meny); meny = null; }
    if (menyAnker) { menyAnker.setAttribute("aria-expanded", "false"); if (returFokus) { menyAnker.focus(); } menyAnker = null; }
    document.removeEventListener("mousedown", utenforMeny, true);
  }
  KS.lukkMeny = lukkMeny;
  function utenforMeny(e) { if (meny && !meny.contains(e.target) && e.target !== menyAnker) { lukkMeny(false); } }
  KS.apneMeny = function (anker, poster) {
    lukkMeny(false);
    meny = KS.el("div", { klasse: "ks-meny", attr: { role: "menu" } });
    poster.forEach(function (p) {
      if (p.skille) { meny.appendChild(KS.el("hr")); return; }
      var b = KS.el("button", { klasse: p.fare ? "fare" : "", attr: { type: "button", role: "menuitem", "data-ks": p.krok || "menypost" }, barn: [p.ikon ? KS.ikon(p.ikon) : null, p.tekst] });
      if (p.kbd) { b.appendChild(KS.el("kbd", { tekst: p.kbd })); }
      if (p.av) { b.disabled = true; }
      b.addEventListener("click", function () { lukkMeny(true); p.fn(); });
      meny.appendChild(b);
    });
    document.body.appendChild(meny);
    var r = anker.getBoundingClientRect(), mb = meny.getBoundingClientRect();
    var venstre = Math.max(8, Math.min(window.innerWidth - mb.width - 8, r.right - mb.width));
    var topp = r.bottom + 4;
    if (topp + mb.height > window.innerHeight - 8) { topp = Math.max(8, r.top - mb.height - 4); }
    meny.style.setProperty("left", venstre + "px"); meny.style.setProperty("top", topp + "px");
    menyAnker = anker; anker.setAttribute("aria-expanded", "true");
    var knapper = KS.$$("button:not(:disabled)", meny);
    if (knapper[0]) { knapper[0].focus(); }
    meny.addEventListener("keydown", function (e) {
      var i = knapper.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); knapper[(i + 1) % knapper.length].focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); knapper[(i - 1 + knapper.length) % knapper.length].focus(); }
      else if (e.key === "Home") { e.preventDefault(); knapper[0].focus(); }
      else if (e.key === "End") { e.preventDefault(); knapper[knapper.length - 1].focus(); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); lukkMeny(true); }
      else if (e.key === "Tab") { lukkMeny(false); }
    });
    document.addEventListener("mousedown", utenforMeny, true);
  };
  function blokkMeny(anker, kort) {
    var id = kort.getAttribute("data-id"), b = KS.finnBlokk(id), i = KS.blokkIndeks(id), n = S.dok.blokker.length;
    KS.apneMeny(anker, [
      { tekst: "Flytt opp", ikon: "pilopp", kbd: "Alt ↑", av: i === 0, fn: function () { flyttEn(id, -1); } },
      { tekst: "Flytt ned", ikon: "pilned", kbd: "Alt ↓", av: i === n - 1, fn: function () { flyttEn(id, 1); } },
      { tekst: "Flytt til toppen", ikon: "pilopp", av: i === 0, fn: function () { KS.flyttBlokker([id], 0); } },
      { tekst: "Flytt til bunnen", ikon: "pilned", av: i === n - 1, fn: function () { KS.flyttBlokker([id], n); } },
      { skille: true },
      { tekst: "Dupliser", ikon: "kopi", krok: "dupliser", fn: function () { KS.dupliser([id]); } },
      { tekst: b.skjult ? "Vis for deltakerne" : "Skjul for deltakerne", ikon: b.skjult ? "oye" : "oyeav", fn: function () { settSkjult([id], !b.skjult); } },
      { tekst: "Vis for deltakerne fra dato …", ikon: "klokke", fn: function () {
        KS.apneBlokk(id);
        setTimeout(function () { var f = kortFor(id) && kortFor(id).querySelector('[data-felt="vis_fra"]'); if (f) { f.focus(); } }, 350);
      } },
      { skille: true },
      { tekst: "Slett blokken", ikon: "slett", fare: true, krok: "slett", fn: function () { KS.slettBlokker([id]); } }
    ]);
  }
  KS.typeMeny = function (anker, indeks) {
    KS.apneMeny(anker, Object.keys(KS.typeNavn).map(function (t) {
      return { tekst: KS.typeNavn[t], ikon: KS.blokkIkon[t], fn: function () { KS.leggTil(t, indeks); } };
    }));
  };

  /* ================= dra og slipp (pekerhendelser, ingen bibliotek) ================= */
  /* beholder: elementet som inneholder radene. elementSel: velger for en rad. grepSel: velger for håndtaket. opt.samle(rad) -> rader som flyttes
     sammen (standard: bare raden), opt.klasse (settes på beholderen mens du drar), opt.ved(raderSomFlyttes, indeks): kalles når du slipper. Escape avbryter. */
  KS.aktiverDra = function (beholder, elementSel, grepSel, opt) {
    opt = opt || {};
    beholder.addEventListener("pointerdown", function (e) {
      if (e.button !== undefined && e.button !== 0) { return; }
      var grep = e.target.closest(grepSel);
      if (!grep || !beholder.contains(grep)) { return; }
      var rad = grep.closest(elementSel);
      if (!rad) { return; }
      e.preventDefault();
      start(rad, grep, e);
    });
    function radene() { return Array.prototype.slice.call(beholder.querySelectorAll(elementSel)); }
    function start(rad, grep, e) {
      var dratt = opt.samle ? opt.samle(rad) : [rad];
      var pid = e.pointerId, siste = e.clientY, mal = null, avbrutt = false, raf = 0;
      try { grep.setPointerCapture(pid); } catch (x) { /* eldre nettlesere */ }
      var rr = rad.getBoundingClientRect(), forskyv = e.clientY - rr.top;
      var skygge = KS.el("div", { klasse: "ks-skygge-kort" });
      var hode = rad.querySelector(".ks-bh");
      if (opt.skyggeTekst || !hode) {
        skygge.appendChild(KS.el("div", { klasse: "ks-bh", barn: [KS.ikon("grep"), KS.el("b", { tekst: (opt.skyggeTekst ? opt.skyggeTekst(rad) : "").trim() || "Rad" })] }));
      } else { skygge.appendChild(hode.cloneNode(true)); }
      skygge.style.setProperty("width", rr.width + "px"); skygge.style.setProperty("left", rr.left + "px"); skygge.style.setProperty("top", rr.top + "px");
      document.body.appendChild(skygge);
      beholder.classList.add(opt.klasse || "dras");
      dratt.forEach(function (x) { x.classList.add("flyttes"); });
      var linje = KS.el("div", { klasse: "ks-slipplinje" });
      beholder.appendChild(linje);
      function beregn() {
        var alle = radene(), idx = alle.length, ref = null, y = siste;
        for (var i = 0; i < alle.length; i++) {
          if (dratt.indexOf(alle[i]) >= 0) { continue; }
          var r = alle[i].getBoundingClientRect();
          if (y < r.top + r.height / 2) { idx = i; ref = alle[i]; break; }
        }
        var b = beholder.getBoundingClientRect(), topp;
        if (ref) { topp = ref.getBoundingClientRect().top - b.top; }
        else {
          var sist = alle.filter(function (x) { return dratt.indexOf(x) < 0; }).pop();
          topp = sist ? sist.getBoundingClientRect().bottom - b.top + 4 : 0;
        }
        linje.style.setProperty("top", topp + "px");
        mal = idx;
      }
      function ramme() {
        if (siste < 90) { window.scrollBy(0, -14); } else if (siste > window.innerHeight - 90) { window.scrollBy(0, 14); }
        skygge.style.setProperty("top", (siste - forskyv) + "px");
        beregn();
        raf = window.requestAnimationFrame(ramme);
      }
      function flytt(ev) { siste = ev.clientY; }
      function rydd() {
        window.cancelAnimationFrame(raf);
        grep.removeEventListener("pointermove", flytt); grep.removeEventListener("pointerup", slipp); grep.removeEventListener("pointercancel", avbryt);
        document.removeEventListener("keydown", tast, true);
        try { grep.releasePointerCapture(pid); } catch (x) { /* ignoreres */ }
        if (skygge.parentNode) { skygge.parentNode.removeChild(skygge); }
        if (linje.parentNode) { linje.parentNode.removeChild(linje); }
        beholder.classList.remove(opt.klasse || "dras");
        dratt.forEach(function (x) { x.classList.remove("flyttes"); });
      }
      function slipp() { beregn(); var m = mal; rydd(); if (!avbrutt && m !== null && opt.ved) { opt.ved(dratt, m); } }
      function avbryt() { avbrutt = true; rydd(); }
      function tast(ev) { if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); avbryt(); KS.si("Flyttingen er avbrutt"); } }
      grep.addEventListener("pointermove", flytt); grep.addEventListener("pointerup", slipp); grep.addEventListener("pointercancel", avbryt);
      document.addEventListener("keydown", tast, true);
      raf = window.requestAnimationFrame(ramme);
    }
  };

  /* ================= rask omorganisering og flervalg ================= */
  function valgteIder() { return S.dok.blokker.filter(function (b) { return S.valgt.has(b.id); }).map(function (b) { return b.id; }); }
  function syncRaskUi() {
    var knapp = KS.ks("rask");
    if (knapp) { knapp.setAttribute("aria-pressed", S.rask ? "true" : "false"); }
    var info = KS.ks("rask-info"); if (info) { info.hidden = !S.rask; }
    var leggTil = KS.ks("legg-til-omrade"); if (leggTil) { leggTil.hidden = S.rask; }
  }
  KS.setRask = function (pa) {
    if (!KS.skriv || S.rask === pa) { return; }
    S.rask = pa; S.valgt = new Set();
    syncRaskUi();
    KS.tegnListe();
    KS.si(pa ? "Rask omorganisering er på. Alle blokkene er foldet sammen." : "Rask omorganisering er avsluttet.");
    if (pa) { var f = KS.$$('[data-ks="blokk"]', liste)[0]; if (f) { f.focus({ preventScroll: true }); } }
  };
  function oppdaterBulk() {
    var bar = KS.ks("handlingsstolpe");
    if (!bar) { return; }
    bar.hidden = !S.rask;
    var n = valgteIder().length;
    KS.ks("bulk-antall", bar).textContent = n + " valgt";
    KS.$$("[data-bulk]", bar).forEach(function (k) {
      var h = k.getAttribute("data-bulk");
      if (h === "alle") { k.disabled = n === S.dok.blokker.length; } else { k.disabled = n === 0; }
    });
  }
  function bulk(handling) {
    var ider = valgteIder();
    if (handling === "alle") { S.dok.blokker.forEach(function (b) { S.valgt.add(b.id); }); KS.tegnListe(); return; }
    if (handling === "ingen") { S.valgt = new Set(); KS.tegnListe(); return; }
    if (!ider.length) { return; }
    if (handling === "skjul") { settSkjult(ider, true); }
    else if (handling === "vis") { settSkjult(ider, false); }
    else if (handling === "dupliser") { KS.dupliser(ider); }
    else if (handling === "topp") { KS.flyttBlokker(ider, 0); }
    else if (handling === "bunn") { KS.flyttBlokker(ider, S.dok.blokker.length); }
    else if (handling === "slett") { KS.slettBlokker(ider); }
  }

  /* ================= oppkobling ================= */
  KS.init.push(function () {
    liste = KS.ks("blokkliste");
    rot_lyttere();
  });
  function rot_lyttere() {
    var rot = KS.rot;
    rot.addEventListener("focusin", function (e) { var k = e.target.closest ? e.target.closest('[data-ks="blokk"]') : null; if (k) { S.fokusId = k.getAttribute("data-id"); } });

    liste.addEventListener("click", function (e) {
      var kn = e.target.closest("[data-ks]");
      if (!kn || !liste.contains(kn)) { return; }
      var kort = kn.closest('[data-ks="blokk"]'), navn = kn.getAttribute("data-ks");
      if (navn === "innsett") { KS.typeMeny(kn, parseInt(kn.getAttribute("data-indeks"), 10)); return; }
      if (!kort) { return; }
      var id = kort.getAttribute("data-id");
      if (navn === "fold" || navn === "sammendrag") { settApen(kort, !kort.classList.contains("apen")); }
      else if (navn === "opp") { flyttEn(id, -1); }
      else if (navn === "ned") { flyttEn(id, 1); }
      else if (navn === "dupliser") { KS.dupliser([id]); }
      else if (navn === "meny") { blokkMeny(kn, kort); }
    });
    liste.addEventListener("change", function (e) {
      if (e.target.getAttribute && e.target.getAttribute("data-ks") === "velg") {
        var kort = e.target.closest('[data-ks="blokk"]'), id = kort.getAttribute("data-id");
        if (e.target.checked) { S.valgt.add(id); } else { S.valgt.delete(id); }
        kort.classList.toggle("valgt", e.target.checked);
        oppdaterBulk();
        KS.ks("antall").textContent = KS.flertall(S.dok.blokker.length, "blokk", "blokker") + (S.valgt.size ? " · " + S.valgt.size + " valgt" : "");
      }
    });
    /* tastatur: Alt + pil flytter blokken du står i; Enter på et lukket kort åpner det */
    rot.addEventListener("keydown", function (e) {
      if (!KS.skriv) { return; }
      var kort = e.target.closest ? e.target.closest('[data-ks="blokk"]') : null;
      var paGrep = e.target.getAttribute && e.target.getAttribute("data-ks") === "grep";
      if ((e.altKey || paGrep) && !e.ctrlKey && (e.key === "ArrowUp" || e.key === "ArrowDown") && kort && liste.contains(kort)) {
        e.preventDefault(); flyttEn(kort.getAttribute("data-id"), e.key === "ArrowUp" ? -1 : 1); return;
      }
      if (e.key === "Escape" && S.rask && !document.querySelector("dialog[open]") && !meny) { e.preventDefault(); KS.setRask(false); }
    });
    KS.aktiverDra(liste, '[data-ks="blokk"]', '[data-ks="grep"]', {
      klasse: "dras",
      samle: function (rad) {
        var id = rad.getAttribute("data-id");
        if (S.rask && S.valgt.has(id)) { return alleKort().filter(function (k) { return S.valgt.has(k.getAttribute("data-id")); }); }
        return [rad];
      },
      ved: function (rader, indeks) { KS.flyttBlokker(rader.map(function (r) { return r.getAttribute("data-id"); }), indeks); }
    });

    var pal = KS.$$('[data-ks="legg-til"]');
    pal.forEach(function (k) { k.addEventListener("click", function () { KS.leggTil(k.getAttribute("data-type")); }); });
    var rask = KS.ks("rask");
    if (rask) { rask.addEventListener("click", function () { KS.setRask(!S.rask); }); }
    var ferdig = KS.ks("rask-ferdig");
    if (ferdig) { ferdig.addEventListener("click", function () { KS.setRask(false); }); }
    var bar = KS.ks("handlingsstolpe");
    if (bar) { KS.$$("[data-bulk]", bar).forEach(function (k) { k.addEventListener("click", function () { bulk(k.getAttribute("data-bulk")); }); }); }
    KS.ks("fold-alle").addEventListener("click", function () {
      var lukk = KS.ks("fold-alle").getAttribute("data-tilstand") === "lukk";
      S.apen = lukk ? new Set() : new Set(S.dok.blokker.map(function (b) { return b.id; }));
      KS.tegnListe();
      KS.si(lukk ? "Alle blokkene er foldet sammen" : "Alle blokkene er åpnet");
    });
    var malTom = KS.ks("mal-tom");
    if (malTom) { malTom.addEventListener("click", function () { if (KS.apneMal) { KS.apneMal(); } }); }
  }
})();
