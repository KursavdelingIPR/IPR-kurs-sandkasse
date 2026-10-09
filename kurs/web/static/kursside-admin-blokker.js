/* Kursside i administrasjonen: INNHOLDET I HVER BLOKKTYPE (tekst med rik tekst, viktig, program, filer, lenker, tabell, kontakt, bilde), radene i dem,
   tekstmodus (én linje per rad) og innliming fra Excel. Hører sammen med kursside-admin.js (kjernen) og kursside-admin-liste.js (blokklisten).
   Skjelettene ligger i _kursside_admin_maler.html (<template>) og fylles via data-felt. Ingen HTML fra tekst settes inn: DOM bygges med createElement/
   textContent, og rik tekst leses ut av DOM-en gjennom en egen filtrering (KS.renHtml / KS.rtTilHtml) til p, br, strong, em, ul, ol, li og a[href]. */
(function () {
  "use strict";
  var KS = window.KS;
  if (!KS) { return; }
  var S = KS.S, D = KS.data;
  var apneDager = new WeakSet();          // programdager som er åpne (identitet, ikke lagret i dokumentet)
  var sisteCelle = {};                    // blokk-id -> {r, c}: siste tabellcelle du sto i (til «Legg til deltakere»)

  /* ================= felles: binding av felt, endring, gjenoppbygging ================= */
  function endret(b, struktur) { KS.endret({ blokkId: b.id, struktur: !!struktur }); KS.oppdaterKort(b.id); }
  function strukturEndring(b, fn, fokus) {
    KS.snapshot();
    fn();
    endret(b, true);
    KS.tegnBody(b.id);
    if (fokus) { fokus(); }
  }
  /* Piltaster flytter en rad når du står på håndtaket (uten Ctrl), eller med Ctrl fra hvilket som helst felt i raden. */
  function flyttTast(e) {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") { return false; }
    var pahandtak = e.target.getAttribute && e.target.getAttribute("data-ks") === "rad-grep";
    return e.ctrlKey || pahandtak;
  }
  function tallEllerNull(v) { return v === "" || v === null || v === undefined ? null : parseInt(v, 10); }
  /* Binder felt med data-felt=navn under `rot` til obj[navn]. typer: {navn: "tall"|"tekstEllerNull"|"dato"}. Radioknapper: alle med samme data-felt. */
  function bind(rot, obj, felter, ved, typer) {
    typer = typer || {};
    felter.forEach(function (navn) {
      var els = KS.$$('[data-felt="' + navn + '"]', rot);
      els.forEach(function (f) {
        var type = typer[navn];
        var verdi = obj[navn];
        if (f.type === "checkbox") {
          f.checked = !!verdi;
          f.addEventListener("change", function () { obj[navn] = f.checked; ved(navn, obj[navn]); });
        } else if (f.type === "radio") {
          f.checked = f.value === verdi;
          f.addEventListener("change", function () { if (f.checked) { obj[navn] = f.value; ved(navn, obj[navn]); } });
        } else if (f.tagName === "SELECT") {
          f.value = verdi === null || verdi === undefined ? "" : String(verdi);
          f.addEventListener("change", function () { obj[navn] = type === "tall" ? tallEllerNull(f.value) : (f.value === "" ? null : f.value); ved(navn, obj[navn]); });
        } else {
          f.value = verdi === null || verdi === undefined ? "" : String(verdi);
          f.addEventListener("input", function () { obj[navn] = (type === "dato" && f.value === "") ? null : f.value; ved(navn, obj[navn]); });
        }
      });
    });
  }

  /* Synlighetslinjen nederst i hver blokk: vis fra dato, skjul foreløpig, med i «På denne siden». */
  function synlighet(b) {
    var r = KS.klon("ks-mal-synlighet");
    bind(r, b, ["vis_fra", "skjult", "i_meny"], function () { endret(b); }, { vis_fra: "dato" });
    return r;
  }

  /* ================= klokkeslett ================= */
  KS.normTid = function (v) {
    var t = (v || "").trim();
    if (!t) { return ""; }
    var m = /^(\d{1,2})[:.](\d{2})$/.exec(t) || /^(\d{1,2})(\d{2})$/.exec(t);
    if (!m) { return ""; }
    var h = parseInt(m[1], 10), min = parseInt(m[2], 10);
    if (h > 23 || min > 59) { return ""; }
    return ("0" + h).slice(-2) + ":" + m[2];
  };

  /* ================= rik tekst ================= */
  var FJERN = /^(SCRIPT|STYLE|IFRAME|OBJECT|EMBED|SVG|FORM|VIDEO|AUDIO|CANVAS|TEMPLATE|NOSCRIPT|HEAD|TITLE|META|LINK|BUTTON|SELECT|TEXTAREA|INPUT|MATH)$/;
  var BLOKK = /^(P|DIV|H[1-6]|BLOCKQUOTE|PRE|SECTION|ARTICLE|HEADER|FOOTER|ASIDE|MAIN|NAV|FIGURE|ADDRESS|TABLE|TBODY|THEAD|TR|TD|TH|DL|DT|DD|FIELDSET|HR)$/;
  function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
  function skilleTegn(s) { return s.replace(/ /g, " ").replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, ""); }
  function inl(node) {
    var ut = "";
    Array.prototype.forEach.call(node.childNodes, function (n) {
      if (n.nodeType === 3) { ut += esc(skilleTegn(n.nodeValue).replace(/\s+/g, " ")); return; }
      if (n.nodeType !== 1) { return; }
      var t = n.tagName.toUpperCase();
      if (FJERN.test(t)) { return; }
      if (t === "BR") { ut += "<br>"; }
      else if (t === "STRONG" || t === "B") { var s = inl(n); ut += s.trim() ? "<strong>" + s + "</strong>" : s; }
      else if (t === "EM" || t === "I") { var e = inl(n); ut += e.trim() ? "<em>" + e + "</em>" : e; }
      else if (t === "A") {
        var href = KS.trygUrl(n.getAttribute("href") || "");
        var innhold = inl(n);
        ut += href && innhold.trim() && !/^https?:\/\/$/i.test(href) ? '<a href="' + esc(href) + '">' + innhold + "</a>" : innhold;
      }
      else if (t === "UL" || t === "OL") { ut += ""; }                 // lister hører ikke hjemme inni linjer: håndteres i blokknivå
      else if (BLOKK.test(t)) { ut += inl(n) + "<br>"; }
      else { ut += inl(n); }
    });
    return ut;
  }
  function tilAvsnitt(s) {
    var t = s.replace(/^(<br>)+|(<br>)+$/g, "").replace(/(<br>){3,}/g, "<br><br>");
    return t.replace(/\s+/g, " ").trim() ? "<p>" + t.trim() + "</p>" : "";
  }
  function liste(el, dybde) {
    var tag = el.tagName.toLowerCase(), ut = "";
    Array.prototype.forEach.call(el.childNodes, function (n) {
      if (n.nodeType !== 1) { return; }
      var t = n.tagName.toUpperCase();
      if (t === "LI") {
        var kopi = n.ownerDocument.createElement("li"), under = "";       // samme dokument som noden (aldri den levende siden: se wrap)
        Array.prototype.forEach.call(n.childNodes, function (c) {
          if (c.nodeType === 1 && (c.tagName === "UL" || c.tagName === "OL") && dybde < 6) { under += liste(c, dybde + 1); } else { kopi.appendChild(c.cloneNode(true)); }
        });
        var tekst = inl(kopi).replace(/^(<br>)+|(<br>)+$/g, "").trim();
        if (tekst || under) { ut += "<li>" + tekst + under + "</li>"; }
      } else if (t === "UL" || t === "OL") { ut += liste(n, dybde + 1); }
    });
    return ut ? "<" + tag + ">" + ut + "</" + tag + ">" : "";
  }
  function blokker(node, dybde) {
    var ut = "", buffer = "";
    function tom() { ut += tilAvsnitt(buffer); buffer = ""; }
    Array.prototype.forEach.call(node.childNodes, function (n) {
      if (n.nodeType === 3) { buffer += esc(skilleTegn(n.nodeValue).replace(/\s+/g, " ")); return; }
      if (n.nodeType !== 1) { return; }
      var t = n.tagName.toUpperCase();
      if (FJERN.test(t)) { return; }
      if (t === "UL" || t === "OL") { tom(); ut += liste(n, 0); }
      else if ((t === "P" || /^H[1-6]$/.test(t) || t === "PRE" || t === "BLOCKQUOTE") && !n.querySelector("p,div,ul,ol,table,blockquote")) {
        tom(); ut += tilAvsnitt(/^H[1-6]$/.test(t) ? fet(inl(n)) : inl(n));       // overskrifter fra Word: fet tekst, så strukturen ikke forsvinner
      }
      else if (BLOKK.test(t) && dybde < 30) { tom(); ut += blokker(n, dybde + 1); }
      else { buffer += inl(wrap(n)); }
    });
    tom();
    return ut;
  }
  /* Innlimt HTML leses i et DOMParser-dokument (uten nettleserkontekst: ingen bilder hentes, ingen hendelsesattributter kjører). Klonen MÅ bli i det
     dokumentet: `document.createElement` her ville flyttet nodene inn i den levende siden, og et <img src=… onerror=…> ville blitt hentet og forsøkt
     kjørt før noe var renset bort (bare CSP stoppet det). Derfor opprettes beholderen med nodens eget ownerDocument. */
  function wrap(n) { var s = n.ownerDocument.createElement("span"); s.appendChild(n.cloneNode(true)); return s; }
  function fet(s) { s = s.replace(/<\/?strong>/g, ""); return s.trim() ? "<strong>" + s + "</strong>" : ""; }
  /* Renset HTML (bare p, br, strong, em, ul, ol, li og a[href]) fra hvilken som helst HTML-tekst: brukes ved innliming og når rik tekst leses ut. */
  KS.renHtml = function (html) {
    var doc = new DOMParser().parseFromString("<body>" + (html || "") + "</body>", "text/html");
    return blokker(doc.body, 0);
  };
  KS.rtTilHtml = function (el) { return blokker(el, 0); };
  function settInnHtml(el, html) {
    var doc = new DOMParser().parseFromString("<body>" + KS.renHtml(html) + "</body>", "text/html");
    el.replaceChildren.apply(el, Array.prototype.slice.call(doc.body.childNodes));
  }

  /* Én felles lytter for alle rik tekst-flater: hver post svarer «true» så lenge flaten finnes i siden, og fjernes ellers. */
  var rtLyttere = [];
  document.addEventListener("selectionchange", function () { rtLyttere = rtLyttere.filter(function (f) { return f(); }); });

  var bygger = {};
  bygger.tekst = function (b, innhold) {
    var r = KS.klon("ks-mal-tekst"); innhold.appendChild(r);
    var rt = KS.felt("html", r), teller = KS.felt("teller", r), verktoy = r.querySelector(".ks-rtb");
    var maks = D.grenser.tekst_tegn;
    settInnHtml(rt, b.data.html);
    try { document.execCommand("defaultParagraphSeparator", false, "p"); } catch (e) { /* ikke støttet: renseren tåler div */ }
    function visTeller() {
      var n = (b.data.html || "").length;
      teller.textContent = n.toLocaleString("nb-NO") + " av " + maks.toLocaleString("nb-NO") + " tegn";
      teller.className = n > maks ? "feil" : "";
    }
    visTeller();
    function synk() { b.data.html = KS.rtTilHtml(rt); visTeller(); endret(b); }
    rt.addEventListener("input", synk);
    rt.addEventListener("focus", function () { try { document.execCommand("defaultParagraphSeparator", false, "p"); } catch (e) { /* ignoreres */ } });
    rt.addEventListener("paste", function (e) {
      e.preventDefault();
      var cd = e.clipboardData;
      if (!cd) { return; }
      var html = cd.getData("text/html"), tekst = cd.getData("text/plain");
      if (!html && !tekst && cd.files && cd.files.length) { KS.toast("Bilder legges til med Bilde-blokken.", { niva: "feil" }); return; }
      var ren = html ? KS.renHtml(html) : "";
      if (ren) { document.execCommand("insertHTML", false, ren); } else if (tekst) { document.execCommand("insertText", false, tekst); }
      var omgjort = [];
      if (html && /<h[1-6][\s>]/i.test(html)) { omgjort.push("Overskrifter er gjort om til fet tekst."); }
      if (html && /<table[\s>]/i.test(html)) { omgjort.push("Tabeller er gjort om til vanlige avsnitt (bruk Tabell-blokken til gruppelister)."); }
      if (omgjort.length) { KS.toast("Innlimt fra et annet dokument. " + omgjort.join(" "), {}); }
    });
    rt.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) { e.preventDefault(); KS.toast("Bilder legges til med Bilde-blokken.", { niva: "feil" }); }
    });
    var markering = null;
    function lagreMarkering() {
      var s = window.getSelection();
      if (s.rangeCount && rt.contains(s.getRangeAt(0).commonAncestorContainer)) { markering = s.getRangeAt(0).cloneRange(); }
    }
    rtLyttere.push(function () {
      if (!document.body.contains(rt)) { return false; }
      var s = window.getSelection();
      if (!s.rangeCount || !rt.contains(s.anchorNode)) { return true; }
      lagreMarkering();
      KS.$$("[data-kommando]", verktoy).forEach(function (k) {
        var kom = k.getAttribute("data-kommando");
        if (k.hasAttribute("aria-pressed")) { try { k.setAttribute("aria-pressed", document.queryCommandState(kom) ? "true" : "false"); } catch (e) { /* ignoreres */ } }
      });
      return true;
    });
    verktoy.addEventListener("mousedown", function (e) { if (e.target.closest("button")) { e.preventDefault(); } });
    verktoy.addEventListener("click", function (e) {
      var k = e.target.closest("[data-kommando]");
      if (!k) { return; }
      var kom = k.getAttribute("data-kommando");
      if (document.activeElement !== rt) {
        rt.focus();
        if (markering) { var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(markering); }
      }
      if (kom === "lenke") {
        var valgt = window.getSelection().toString();
        KS.lenkeDialog({ tekst: valgt, ved: function (adresse, tekst) {
          rt.focus();
          if (markering) { var s2 = window.getSelection(); s2.removeAllRanges(); s2.addRange(markering); }
          if (valgt) { document.execCommand("createLink", false, adresse); }
          else { document.execCommand("insertHTML", false, '<a href="' + esc(adresse) + '">' + esc(tekst || adresse) + "</a>"); }
          synk();
        }, tilbake: function () { rt.focus(); } });
        return;
      }
      try { document.execCommand(kom, false, null); } catch (x) { /* ikke støttet */ }
      synk();
    });
    return r;
  };

  /* ================= viktig ================= */
  bygger.viktig = function (b, innhold) {
    var r = KS.klon("ks-mal-viktig"); innhold.appendChild(r);
    KS.$$('[data-felt="niva"]', r).forEach(function (f) { f.name = "ks-niva-" + b.id; });
    bind(r, b.data, ["niva", "tekst"], function () { endret(b); });
    return r;
  };

  /* ================= program ================= */
  function dagsdato(dag) {
    var kd = dag.kursdag_id ? D.kursdager.filter(function (k) { return k.id === dag.kursdag_id; })[0] : null;
    return kd ? kd.dato : dag.dato;
  }
  KS.formaterProgram = function (dag) {
    return (dag.punkter || []).map(function (p) {
      var tid = (p.fra || p.til) ? (p.fra || "") + "-" + (p.til || "") : "";
      var deler = [tid, p.tema || ""];
      if (p.pause) { if ((p.tema || "").toLowerCase() !== "pause") { deler.push("", "", "pause"); } }
      else if (p.sted || p.hvem) { deler.push(p.sted || ""); if (p.hvem) { deler.push(p.hvem); } }
      return deler.join(" | ");
    }).join("\n");
  };
  /* -> {punkter: [...], feil: ["Linje 3: …"]} */
  KS.parseProgram = function (tekst) {
    var punkter = [], feil = [];
    tekst.replace(/\r/g, "").split("\n").forEach(function (linje, i) {
      if (!linje.trim()) { return; }
      var d = linje.split("|").map(function (x) { return x.trim(); });
      var tid = d[0], fra = "", til = "";
      var m = /^(\d{1,2}[:.]?\d{2})?\s*[-–—]\s*(\d{1,2}[:.]?\d{2})?$/.exec(tid);
      if (m) { fra = m[1] ? KS.normTid(m[1]) : ""; til = m[2] ? KS.normTid(m[2]) : ""; if ((m[1] && !fra) || (m[2] && !til)) { feil.push("Linje " + (i + 1) + ": klokkeslettet er ugyldig. Skriv for eksempel 09:15-10:45."); return; } }
      else if (/^\d{1,2}[:.]?\d{2}$/.test(tid)) { fra = KS.normTid(tid); if (!fra) { feil.push("Linje " + (i + 1) + ": klokkeslettet «" + tid + "» er ugyldig."); return; } }
      else if (tid) { feil.push("Linje " + (i + 1) + ": start med klokkeslett, for eksempel 09:15-10:45 | Tema."); return; }
      var tema = d[1] || "";
      var pause = tema.toLowerCase() === "pause" || (d[4] || "").toLowerCase() === "pause";
      if (!tema && !pause) { feil.push("Linje " + (i + 1) + ": mangler tema."); return; }
      if (tema.length > D.grenser.tema) { feil.push("Linje " + (i + 1) + ": temaet er for langt (maks " + D.grenser.tema + " tegn)."); return; }
      punkter.push({ fra: fra, til: til, tema: tema, sted: pause ? "" : (d[2] || ""), hvem: pause ? "" : (d[3] || ""), pause: pause });
    });
    if (punkter.length > D.grenser.punkter) { feil.push("For mange linjer (maks " + D.grenser.punkter + " per dag)."); }
    return { punkter: punkter, feil: feil };
  };
  function nyttPunkt(forrige, pause) {
    return { fra: forrige && forrige.til ? forrige.til : "", til: "", tema: pause ? "Pause" : "", sted: "", hvem: "", pause: !!pause };
  }
  function byggProgramRad(b, dag, p, tegnDag) {
    var r = KS.klon("ks-mal-program-rad");
    bind(r, p, ["fra", "til", "tema", "sted", "hvem", "pause"], function (navn) {
      if (navn === "pause") { r.classList.toggle("pause", p.pause); }
      endret(b);
    });
    r.classList.toggle("pause", !!p.pause);
    ["fra", "til"].forEach(function (navn) {
      var f = KS.felt(navn, r);
      f.addEventListener("change", function () {
        var n = KS.normTid(f.value);
        if (n) { f.value = n; p[navn] = n; f.removeAttribute("aria-invalid"); }
        else if (f.value.trim()) { f.setAttribute("aria-invalid", "true"); f.title = "Skriv klokkeslett som 09:15"; }
        else { p[navn] = ""; f.removeAttribute("aria-invalid"); }
        endret(b);
      });
    });
    r.addEventListener("keydown", function (e) {
      var rader = dag.punkter, i = rader.indexOf(p);
      if (flyttTast(e)) {
        e.preventDefault();
        var ny = e.key === "ArrowUp" ? i - 1 : i + 1;
        if (ny < 0 || ny >= rader.length) { return; }
        var felt = e.target.getAttribute("data-felt");
        strukturEndring(b, function () { rader.splice(i, 1); rader.splice(ny, 0, p); }, function () { fokuser(b, dag, p, felt); });
        return;
      }
      if (e.key === "Enter" && e.target.tagName === "INPUT" && (e.ctrlKey || (i === rader.length - 1 && (e.target.getAttribute("data-felt") === "hvem" || (p.pause && e.target.getAttribute("data-felt") === "tema"))))) {
        e.preventDefault();
        if (rader.length >= D.grenser.punkter) { KS.toast("En dag kan ha høyst " + D.grenser.punkter + " rader.", { niva: "feil" }); return; }
        var nytt = nyttPunkt(p, false);
        strukturEndring(b, function () { rader.splice(i + 1, 0, nytt); }, function () { fokuser(b, dag, nytt, "til"); });
      }
    });
    r.addEventListener("paste", function (e) {
      var felt = e.target.getAttribute && e.target.getAttribute("data-felt");
      var tekst = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
      var kol = ["fra", "til", "tema", "sted", "hvem"];
      if (kol.indexOf(felt) < 0 || tekst.indexOf("\t") < 0 && tekst.indexOf("\n") < 0) { return; }
      e.preventDefault();
      var linjer = tekst.replace(/\r/g, "").replace(/\n$/, "").split("\n").map(function (l) { return l.split("\t"); });
      var start = dag.punkter.indexOf(p), kolStart = kol.indexOf(felt);
      strukturEndring(b, function () {
        linjer.forEach(function (celler, li) {
          var punkt = dag.punkter[start + li];
          if (!punkt) { if (dag.punkter.length >= D.grenser.punkter) { return; } punkt = nyttPunkt(null, false); dag.punkter.push(punkt); }
          celler.forEach(function (c, ci) {
            var navn = kol[kolStart + ci]; if (!navn) { return; }
            var v = c.trim();
            if (navn === "fra" || navn === "til") { v = KS.normTid(v) || v; }
            punkt[navn] = v.slice(0, navn === "tema" ? D.grenser.tema : (navn === "sted" || navn === "hvem" ? D.grenser.sted_hvem : 5));
          });
        });
      });
      KS.toast("Innlimt: " + KS.flertall(linjer.length, "rad", "rader") + " fylt ut.", { angre: KS.angre });
    });
    return r;
  }
  function fokuser(b, dag, p, felt) {
    var k = KS.kortFor(b.id);
    if (!k) { return; }
    var dager = KS.$$(".ks-dag", k), i = b.data.dager.indexOf(dag), dagEl = dager[i];
    if (!dagEl) { return; }
    var rad = KS.$$(".ks-pr", dagEl).filter(function (x) { return !x.classList.contains("hode"); })[dag.punkter.indexOf(p)];
    var f = rad && rad.querySelector('[data-felt="' + (felt || "tema") + '"]');
    if (f) { f.focus(); f.select && f.select(); }
  }
  function dagTittelTekst(dag, i) {
    var dato = dagsdato(dag), s = dag.tittel || "Dag " + (i + 1);
    return dato ? s + " · " + KS.datoTekst(dato) : s;
  }
  function byggDag(b, dag, i) {
    var el = KS.klon("ks-mal-program-dag");
    var kd = dag.kursdag_id ? D.kursdager.filter(function (k) { return k.id === dag.kursdag_id; })[0] : null;
    function tekstene() {
      KS.felt("dag-tittel", el).textContent = dagTittelTekst(dag, i);
      var tid = kd && kd.start ? "kursdag " + kd.start + (kd.slutt ? "–" + kd.slutt : "") + " ✓" : "";
      var n = KS.flertall(dag.punkter.length, "punkt", "punkter");
      KS.felt("dag-info", el).textContent = el.classList.contains("apen") ? tid : (tid ? tid + " · " + n : n);
    }
    var innholdEl = KS.felt("dag-innhold", el), fold = KS.ks("dag-fold", el);
    function sett(apen) {
      el.classList.toggle("apen", apen);
      innholdEl.hidden = !apen;
      fold.setAttribute("aria-expanded", apen ? "true" : "false");
      if (apen) { apneDager.add(dag); } else { apneDager.delete(dag); }
      tekstene();
    }
    bind(el, dag, ["tittel", "dato"], function (navn) {
      if (navn === "dato" && dag.dato) { dag.kursdag_id = null; }
      tekstene(); endret(b);
    }, { dato: "dato" });
    var datoFelt = KS.felt("dato-felt", el);
    datoFelt.hidden = !!dag.kursdag_id;
    var kopierKnapp = KS.ks("dag-kopier", el);
    if (kopierKnapp && i === 0) { kopierKnapp.hidden = true; }          // «Kopier fra dag 1» på dag 1 selv gjorde ingenting
    var rader = KS.felt("rader", innholdEl);
    dag.punkter.forEach(function (p) { rader.appendChild(byggProgramRad(b, dag, p)); });
    KS.aktiverDra(rader, ".ks-pr", '[data-ks="rad-grep"]', {
      klasse: "dras-rad", skyggeTekst: function (rad) { var t = KS.felt("tema", rad); return (KS.felt("fra", rad).value || "") + " " + (t ? t.value : ""); },
      ved: function (dratt, indeks) {
        var alle = KS.$$(".ks-pr", rader), fra = alle.indexOf(dratt[0]), p = dag.punkter[fra];
        strukturEndring(b, function () { var ny = indeks > fra ? indeks - 1 : indeks; dag.punkter.splice(fra, 1); dag.punkter.splice(ny, 0, p); }, function () { fokuser(b, dag, p, "tema"); });
      }
    });
    fold.addEventListener("click", function () { sett(!el.classList.contains("apen")); });
    el.addEventListener("click", function (e) {
      var k = e.target.closest("[data-ks]");
      if (!k) { return; }
      var navn = k.getAttribute("data-ks"), dager = b.data.dager;
      if (navn === "rad-slett") {
        var rad = k.closest(".ks-pr"), idx = KS.$$(".ks-pr", rader).indexOf(rad), borte = dag.punkter[idx];
        strukturEndring(b, function () { dag.punkter.splice(idx, 1); });
        KS.toast("Raden er fjernet.", { angre: KS.angre });
        void borte;
      } else if (navn === "ny-rad" || navn === "ny-pause") {
        if (dag.punkter.length >= D.grenser.punkter) { KS.toast("En dag kan ha høyst " + D.grenser.punkter + " rader.", { niva: "feil" }); return; }
        var siste = dag.punkter[dag.punkter.length - 1], nytt = nyttPunkt(siste, navn === "ny-pause");
        strukturEndring(b, function () { dag.punkter.push(nytt); }, function () { fokuser(b, dag, nytt, navn === "ny-pause" ? "til" : "til"); });
      } else if (navn === "dag-slett") {
        strukturEndring(b, function () { dager.splice(dager.indexOf(dag), 1); });
        KS.toast("Dagen «" + dagTittelTekst(dag, i) + "» er slettet.", { angre: KS.angre });
      } else if (navn === "dag-dupliser") {
        if (dager.length >= D.grenser.dager) { KS.toast("Programmet kan ha høyst " + D.grenser.dager + " dager.", { niva: "feil" }); return; }
        strukturEndring(b, function () { var k2 = KS.kopi(dag); k2.kursdag_id = null; k2.dato = null; k2.tittel = (dag.tittel || "Dag " + (i + 1)) + " (kopi)"; dager.splice(dager.indexOf(dag) + 1, 0, k2); apneDager.add(k2); });
      } else if (navn === "dag-kopier") {
        var forste = dager[0];
        if (forste === dag) { return; }
        if (!forste.punkter.length) { KS.toast("Dag 1 har ingen rader å kopiere.", { niva: "feil" }); return; }
        strukturEndring(b, function () { dag.punkter = KS.kopi(forste.punkter); });
        KS.toast("Radene fra dag 1 er kopiert hit.", { angre: KS.angre });
      } else if (navn === "dag-tekst") {
        KS.tekstDialog({
          tittel: "Rediger «" + dagTittelTekst(dag, i) + "» som tekst",
          hjelp: "Én linje per punkt: klokkeslett | tema | sted | hvem. Sted og hvem er valgfrie. En linje der temaet er «Pause» blir en pause.",
          ledetekst: "Program for dagen (eksempel: 09:15-10:45 | Emosjonell tilknytning | Rom 3 | Marte Solberg)",
          tekst: KS.formaterProgram(dag),
          sjekk: function (t) { var r = KS.parseProgram(t); return r.feil; },
          ok: function (t) { var r = KS.parseProgram(t); strukturEndring(b, function () { dag.punkter = r.punkter; }); KS.toast("Dagen er oppdatert fra teksten.", { angre: KS.angre }); }
        });
      }
    });
    if (apneDager.has(dag)) { sett(true); } else { sett(false); }
    return el;
  }
  bygger.program = function (b, innhold) {
    var r = KS.klon("ks-mal-program"); innhold.appendChild(r);
    var dagerEl = KS.felt("dager", r), dager = b.data.dager;
    if (dager.length && !dager.some(function (d) { return apneDager.has(d); })) { apneDager.add(dager[0]); }
    dager.forEach(function (dag, i) { dagerEl.appendChild(byggDag(b, dag, i)); });
    KS.ks("ny-dag", r).addEventListener("click", function () {
      if (dager.length >= D.grenser.dager) { KS.toast("Programmet kan ha høyst " + D.grenser.dager + " dager.", { niva: "feil" }); return; }
      var ny = { kursdag_id: null, dato: null, tittel: "Dag " + (dager.length + 1), punkter: [] };
      apneDager.add(ny);
      strukturEndring(b, function () { dager.push(ny); });
    });
    KS.ks("hent-dager", r).addEventListener("click", function () {
      if (!D.kursdager.length) { KS.toast("Kurset har ingen kursdager ennå. Legg dem inn under Oppsett.", { niva: "feil" }); return; }
      var mangler = D.kursdager.filter(function (kd) { return !dager.some(function (d) { return d.kursdag_id === kd.id; }); });
      if (!mangler.length) { KS.toast("Alle kursdagene er allerede med.", {}); return; }
      strukturEndring(b, function () {
        mangler.forEach(function (kd) { dager.push({ kursdag_id: kd.id, dato: kd.dato, tittel: "Dag " + (D.kursdager.indexOf(kd) + 1), punkter: [] }); });
        dager.sort(function (x, y) { return (dagsdato(x) || "9999") < (dagsdato(y) || "9999") ? -1 : 1; });
      });
      KS.toast(KS.flertall(mangler.length, "dag", "dager") + " lagt til fra kursdatoene.", { angre: KS.angre });
    });
    return r;
  };

  /* ================= filer ================= */
  KS.filtypeKode = function (filnavn, type) {
    if (type === "bilde") { return ["BILDE", "bilde"]; }
    var e = (filnavn.split(".").pop() || "").toLowerCase();
    var kart = { pdf: ["PDF", "pdf"], ppt: ["PPT", "ppt"], pptx: ["PPT", "ppt"], odp: ["PPT", "ppt"], key: ["PPT", "ppt"], doc: ["DOC", "doc"], docx: ["DOC", "doc"], odt: ["DOC", "doc"],
      xls: ["XLS", "xls"], xlsx: ["XLS", "xls"], ods: ["XLS", "xls"], zip: ["ZIP", "zip"], txt: ["TXT", "zip"] };
    return kart[e] || ["FIL", "zip"];
  };
  KS.filMeta = function (id) { return S.filer.filter(function (f) { return f.id === id; })[0] || null; };
  KS.tittelFraFilnavn = function (navn) {
    /* «Presentasjon dag 1 - Emosjonelt fokusert terapi.pdf» -> «Presentasjon dag 1 – Emosjonelt fokusert terapi» (bindestrek med mellomrom rundt blir tankestrek) */
    var t = navn.replace(/\.[^.]+$/, "").replace(/_+/g, " ").replace(/\s+-+\s+/g, " \u2013 ").replace(/-+/g, " ").replace(/\s+/g, " ").trim();
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : navn;
  };
  function lastetOppTekst(utc) {
    var t = KS.norskTid(utc);
    if (!t) { return ""; }
    return "lastet opp " + (t.idag ? "i dag " + t.tid : t.dato);
  }
  KS.visKvote = function (kort) {
    var k = S.kvote;
    KS.$$('[data-felt="kvote-tekst"]', kort || KS.rot).forEach(function (e) {
      e.textContent = KS.flertall(k.antall, "fil", "filer") + " i bruk · " + (k.bytes / 1048576).toFixed(1).replace(".", ",") + " MB av " + Math.round(k.maks_bytes / 1048576) + " MB";
    });
    KS.$$('[data-felt="kvote-stolpe"]', kort || KS.rot).forEach(function (e) { e.value = Math.min(100, Math.round(100 * k.bytes / k.maks_bytes)); });
  };
  function byggFilRad(b, el) {
    var r = KS.klon("ks-mal-fil-rad"), meta = KS.filMeta(el.fil_id);
    var ft = KS.felt("ft", r), kode = meta ? KS.filtypeKode(meta.filnavn, meta.type) : ["FIL", "zip"];
    ft.textContent = kode[0]; ft.classList.add(kode[1]);
    KS.felt("under", r).textContent = meta ? [meta.filnavn, KS.storrelse(meta.storrelse), lastetOppTekst(meta.opprettet)].filter(Boolean).join(" · ") : "Filen finnes ikke lenger";
    var gruppe = KS.felt("gruppe", r);
    D.kursdager.forEach(function (kd, i) { gruppe.appendChild(KS.el("option", { tekst: "Dag " + (i + 1) + " · " + KS.datoKort(kd.dato), attr: { value: String(kd.id) } })); });
    bind(r, el, ["tittel", "gruppe", "synlig_fra"], function () { endret(b); }, { gruppe: "tall", synlig_fra: "dato" });
    return r;
  }
  bygger.filer = function (b, innhold) {
    var r = KS.klon("ks-mal-filer"); innhold.appendChild(r);
    var rader = KS.felt("rader", r), d = b.data;
    d.filer.forEach(function (el) { rader.appendChild(byggFilRad(b, el)); });
    bind(r, d, ["vis_kommende"], function () { endret(b); });
    KS.visKvote(r);
    var eks = KS.felt("eksisterende", r);
    S.filer.filter(function (f) { return f.type === "dokument" && !d.filer.some(function (e) { return e.fil_id === f.id; }); })
      .forEach(function (f) { eks.appendChild(KS.el("option", { tekst: f.filnavn + " (" + KS.storrelse(f.storrelse) + ")", attr: { value: String(f.id) } })); });
    eks.addEventListener("change", function () {
      var id = parseInt(eks.value, 10), f = KS.filMeta(id);
      if (!f) { return; }
      strukturEndring(b, function () { d.filer.push({ fil_id: id, tittel: KS.tittelFraFilnavn(f.filnavn), gruppe: null, synlig_fra: null }); });
    });
    KS.aktiverDra(rader, ".ks-fil", '[data-ks="rad-grep"]', {
      klasse: "dras-rad", skyggeTekst: function (rad) { return KS.felt("tittel", rad).value; },
      ved: function (dratt, indeks) {
        var fra = KS.$$(".ks-fil", rader).indexOf(dratt[0]), el = d.filer[fra];
        strukturEndring(b, function () { var ny = indeks > fra ? indeks - 1 : indeks; d.filer.splice(fra, 1); d.filer.splice(ny, 0, el); });
      }
    });
    r.addEventListener("click", function (e) {
      var k = e.target.closest("[data-ks]");
      if (!k) { return; }
      var navn = k.getAttribute("data-ks"), rad = k.closest(".ks-fil"), idx = rad ? KS.$$(".ks-fil", rader).indexOf(rad) : -1;
      if (navn === "velg-filer") { KS.felt("filvalg", r).click(); }
      else if (navn === "filer-oversikt") { KS.apneFiler(); }
      else if (navn === "rad-opp" || navn === "rad-ned") {
        var ny = navn === "rad-opp" ? idx - 1 : idx + 1;
        if (ny < 0 || ny >= d.filer.length) { return; }
        strukturEndring(b, function () { var x = d.filer.splice(idx, 1)[0]; d.filer.splice(ny, 0, x); });
        KS.si("Flyttet til plass " + (ny + 1) + " av " + d.filer.length);
      }
      else if (navn === "fil-fjern") {
        strukturEndring(b, function () { d.filer.splice(idx, 1); });
        KS.toast("Filen er fjernet fra siden. Den ligger fortsatt i kursets filer.", { angre: KS.angre });
      }
      else if (navn === "fil-bytt") { KS.velgFilerTil({ blokkId: b.id, type: "dokument", erstatt: d.filer[idx], enkelt: true }); }
    });
    r.addEventListener("keydown", function (e) {
      if (!flyttTast(e)) { return; }
      var rad = e.target.closest(".ks-fil");
      if (!rad || rad.classList.contains("opplaster")) { return; }
      e.preventDefault();
      var idx = KS.$$(".ks-fil", rader).indexOf(rad), ny = e.key === "ArrowUp" ? idx - 1 : idx + 1;
      if (ny < 0 || ny >= d.filer.length) { return; }
      var felt = e.target.getAttribute("data-felt");
      strukturEndring(b, function () { var x = d.filer.splice(idx, 1)[0]; d.filer.splice(ny, 0, x); }, function () {
        var nyRad = KS.$$(".ks-fil", KS.felt("rader", KS.kortFor(b.id))).filter(function (x) { return !x.classList.contains("opplaster"); })[ny];
        var f = nyRad && nyRad.querySelector('[data-felt="' + (felt || "tittel") + '"]'); if (f) { f.focus(); }
      });
    });
    KS.aktiverFilslipp(KS.ks("filslipp", r), function (filer) { KS.lastOppFiler(filer, { blokkId: b.id, type: "dokument" }); });
    KS.felt("filvalg", r).addEventListener("change", function (e) { KS.lastOppFiler(Array.prototype.slice.call(e.target.files), { blokkId: b.id, type: "dokument" }); e.target.value = ""; });
    KS.gjenopprettOpplastinger(b, r);
    return r;
  };

  /* ================= lenker ================= */
  KS.formaterLenker = function (lenker) {
    return lenker.map(function (l) {
      var deler = [l.tittel || "", l.kilde === "zoom" ? "zoom" : (l.url || ""), l.tekst || ""];
      if (l.nivaa) { deler.push(l.nivaa); }
      while (deler.length > 2 && !deler[deler.length - 1]) { deler.pop(); }
      return deler.join(" | ");
    }).join("\n");
  };
  function domeneAv(url) { try { return new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return url; } }
  /* Én linje: «Tittel | https://… | Beskrivelse | obligatorisk», eller bare en adresse (tittel = domenet). -> lenke eller null */
  KS.parseLenkeLinje = function (linje) {
    var t = linje.trim();
    if (!t) { return null; }
    var d = t.split("|").map(function (x) { return x.trim(); });
    var l = { tittel: "", url: "", tekst: "", nivaa: null, kilde: null };
    if (d.length === 1) {
      var u = KS.trygUrl(d[0]);
      if (u) { l.url = u; l.tittel = domeneAv(u); } else { l.tittel = d[0]; }
    } else {
      l.tittel = d[0];
      if ((d[1] || "").toLowerCase() === "zoom") { l.kilde = "zoom"; } else { l.url = KS.trygUrl(d[1]) || d[1] || ""; }
      l.tekst = d[2] || "";
      var n = (d[3] || "").toLowerCase();
      l.nivaa = n === "obligatorisk" || n === "anbefalt" ? n : null;
      if (!l.tittel && l.url) { l.tittel = domeneAv(l.url); }
    }
    l.tittel = l.tittel.slice(0, D.grenser.tittel); l.tekst = l.tekst.slice(0, D.grenser.lenke_tekst);
    return l;
  };
  function byggLenkeRad(b, l) {
    var r = KS.klon("ks-mal-lenke-rad"), feilEl = KS.felt("feil", r), urlFelt = KS.felt("url", r), zoom = KS.felt("zoomtekst", r);
    bind(r, l, ["nivaa", "tittel", "url", "tekst"], function (navn) { if (navn === "url") { sjekkUrl(); } endret(b); });
    function sjekkUrl() {
      var ugyldig = l.kilde !== "zoom" && (l.url || "").trim() && !KS.trygUrl(l.url);
      feilEl.hidden = !ugyldig;
      if (ugyldig) { urlFelt.setAttribute("aria-invalid", "true"); } else { urlFelt.removeAttribute("aria-invalid"); }
    }
    if (l.kilde === "zoom") { urlFelt.hidden = true; zoom.hidden = false; KS.$$(".to2", r)[1].classList.add("kun-en"); }
    urlFelt.addEventListener("change", function () {
      var u = KS.trygUrl(urlFelt.value);
      if (u && u !== urlFelt.value.trim()) { urlFelt.value = u; l.url = u; endret(b); }
      sjekkUrl();
    });
    sjekkUrl();
    KS.felt("tittel", r).addEventListener("paste", function (e) {
      var tekst = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
      if (tekst.indexOf("\n") < 0 || !tekst.replace(/\r?\n$/, "").trim()) { return; }
      e.preventDefault();
      var nye = tekst.replace(/\r/g, "").split("\n").map(KS.parseLenkeLinje).filter(Boolean);
      var lenker = b.data.lenker, i = lenker.indexOf(l);
      strukturEndring(b, function () {
        var tom = !l.tittel && !l.url && !l.tekst;
        lenker.splice.apply(lenker, [i, tom ? 1 : 0].concat(nye.slice(0, D.grenser.liste - lenker.length + (tom ? 1 : 0))));
      });
      KS.toast(KS.flertall(nye.length, "lenke", "lenker") + " lagt til fra innlimt tekst.", { angre: KS.angre });
    });
    return r;
  }
  bygger.lenker = function (b, innhold) {
    var r = KS.klon("ks-mal-lenker"); innhold.appendChild(r);
    var rader = KS.felt("rader", r), lenker = b.data.lenker;
    lenker.forEach(function (l) { rader.appendChild(byggLenkeRad(b, l)); });
    KS.aktiverDra(rader, ".ks-lrad", '[data-ks="rad-grep"]', {
      klasse: "dras-rad", skyggeTekst: function (rad) { return KS.felt("tittel", rad).value; },
      ved: function (dratt, indeks) {
        var fra = KS.$$(".ks-lrad", rader).indexOf(dratt[0]), l = lenker[fra];
        strukturEndring(b, function () { var ny = indeks > fra ? indeks - 1 : indeks; lenker.splice(fra, 1); lenker.splice(ny, 0, l); });
      }
    });
    function leggTilLenke(l) {
      if (lenker.length >= D.grenser.liste) { KS.toast("Lenkeblokken kan ha høyst " + D.grenser.liste + " lenker.", { niva: "feil" }); return; }
      strukturEndring(b, function () { lenker.push(l); }, function () {
        var rr = KS.$$(".ks-lrad", KS.felt("rader", KS.kortFor(b.id))).pop(), f = rr && KS.felt("tittel", rr); if (f) { f.focus(); }
      });
    }
    KS.ks("ny-lenke", r).addEventListener("click", function () { leggTilLenke({ tittel: "", url: "", tekst: "", nivaa: null, kilde: null }); });
    KS.ks("lenke-zoom", r).addEventListener("click", function () {
      if (lenker.some(function (l) { return l.kilde === "zoom"; })) { KS.toast("Zoom-lenken er allerede med.", {}); return; }
      if (!D.kurs.har_zoom) { KS.toast("Kurset har ingen Zoom-lenke i Oppsett ennå. Lenken settes inn, men vises først når adressen er lagt inn der.", {}); }
      leggTilLenke({ tittel: "Zoom-møte", url: "", tekst: "", nivaa: null, kilde: "zoom" });
    });
    KS.ks("lenke-tekst", r).addEventListener("click", function () {
      KS.tekstDialog({
        tittel: "Rediger lenkene som tekst",
        hjelp: "Én linje per lenke: Tittel | https://adresse | Beskrivelse | obligatorisk. Beskrivelse og nivå (obligatorisk eller anbefalt) er valgfrie. Skriv «zoom» i stedet for adressen for å bruke kursets Zoom-lenke. En linje med bare en adresse får domenet som tittel.",
        ledetekst: "Lenker (én linje per lenke)",
        tekst: KS.formaterLenker(lenker),
        sjekk: function (t) {
          var feil = [], n = 0;
          t.replace(/\r/g, "").split("\n").forEach(function (linje, i) {
            var l = KS.parseLenkeLinje(linje);
            if (!l) { return; }
            n++;
            if (l.kilde !== "zoom" && l.url && !KS.trygUrl(l.url)) { feil.push("Linje " + (i + 1) + ": adressen må starte med https://"); }
            if (!l.tittel && !l.url && l.kilde !== "zoom") { feil.push("Linje " + (i + 1) + ": mangler tittel eller adresse."); }
          });
          if (n > D.grenser.liste) { feil.push("For mange lenker (maks " + D.grenser.liste + ")."); }
          return feil;
        },
        ok: function (t) {
          var nye = t.replace(/\r/g, "").split("\n").map(KS.parseLenkeLinje).filter(Boolean);
          strukturEndring(b, function () { b.data.lenker = nye; });
          KS.toast("Lenkene er oppdatert fra teksten.", { angre: KS.angre });
        }
      });
    });
    r.addEventListener("click", function (e) {
      var k = e.target.closest('[data-ks="rad-slett"]');
      if (!k) { return; }
      var idx = KS.$$(".ks-lrad", rader).indexOf(k.closest(".ks-lrad"));
      strukturEndring(b, function () { lenker.splice(idx, 1); });
      KS.toast("Lenken er fjernet.", { angre: KS.angre });
    });
    r.addEventListener("keydown", function (e) {
      if (!flyttTast(e)) { return; }
      var rad = e.target.closest(".ks-lrad");
      if (!rad) { return; }
      e.preventDefault();
      var idx = KS.$$(".ks-lrad", rader).indexOf(rad), ny = e.key === "ArrowUp" ? idx - 1 : idx + 1;
      if (ny < 0 || ny >= lenker.length) { return; }
      var felt = e.target.getAttribute("data-felt");
      strukturEndring(b, function () { var x = lenker.splice(idx, 1)[0]; lenker.splice(ny, 0, x); }, function () {
        var nyRad = KS.$$(".ks-lrad", KS.felt("rader", KS.kortFor(b.id)))[ny], f = nyRad && nyRad.querySelector('[data-felt="' + felt + '"]'); if (f) { f.focus(); }
      });
    });
    return r;
  };

  /* ================= tabell (gruppeinndeling) ================= */
  /* Tabulator- og linjeskilt tekst (Excel) -> rader av celler */
  KS.parseTabellTekst = function (tekst) {
    return tekst.replace(/\r/g, "").replace(/\n+$/, "").split("\n").map(function (l) { return l.split("\t").map(function (c) { return c.trim().replace(/^"(.*)"$/, "$1"); }); });
  };
  function tilpassTabell(d) {
    var n = d.kolonner.length;
    d.rader = d.rader.map(function (r) { return (r.concat(new Array(n).fill(""))).slice(0, n); });
  }
  function byggTabell(b, container) {
    var d = b.data;
    tilpassTabell(d);
    KS.tom(container);
    var tabell = KS.el("table", { klasse: "ks-tab" }), thead = KS.el("thead"), tbody = KS.el("tbody"), hr = KS.el("tr");
    hr.appendChild(KS.el("th", { klasse: "grep-celle", attr: { scope: "col" }, barn: [KS.el("span", { klasse: "skjul-visuelt", tekst: "Rad" })] }));
    d.kolonner.forEach(function (navn, c) {
      var inp = KS.el("input", { attr: { type: "text", "aria-label": "Kolonneoverskrift " + (c + 1), maxlength: String(D.grenser.kolonne_tekst), autocomplete: "off" } });
      inp.value = navn;
      inp.addEventListener("input", function () { d.kolonner[c] = inp.value; endret(b); });
      var kolCelle = KS.el("div", { klasse: "kol-celle", barn: [inp] });
      var celle = KS.el("th", { attr: { scope: "col" }, barn: [kolCelle] });
      if (d.kolonner.length > 1) {
        var fj = KS.el("button", { klasse: "ks-ib fare", attr: { type: "button", "data-endrer": "", "aria-label": "Fjern kolonnen «" + (navn || c + 1) + "»", title: "Fjern kolonnen" }, barn: [KS.ikon("x")] });
        fj.addEventListener("click", function () {
          strukturEndring(b, function () { d.kolonner.splice(c, 1); d.rader.forEach(function (r) { r.splice(c, 1); }); });
          KS.toast("Kolonnen er fjernet.", { angre: KS.angre });
        });
        kolCelle.appendChild(fj);
      }
      hr.appendChild(celle);
    });
    hr.appendChild(KS.el("th", { klasse: "slett-celle", attr: { scope: "col" }, barn: [KS.el("span", { klasse: "skjul-visuelt", tekst: "Handlinger" })] }));
    thead.appendChild(hr);
    d.rader.forEach(function (rad, r) {
      var tr = KS.el("tr");
      tr.appendChild(KS.el("td", { klasse: "grep-celle", barn: [KS.el("span", { klasse: "dempet liten", tekst: String(r + 1) })] }));
      rad.forEach(function (verdi, c) {
        var inp = KS.el("input", { attr: { type: "text", "aria-label": "Rad " + (r + 1) + ", " + (d.kolonner[c] || "kolonne " + (c + 1)), maxlength: String(D.grenser.celle), autocomplete: "off" } });
        inp.value = verdi;
        inp.addEventListener("input", function () { rad[c] = inp.value; endret(b); });
        inp.addEventListener("focus", function () { sisteCelle[b.id] = { r: r, c: c }; });
        inp.addEventListener("keydown", function (e) {
          if (e.key === "Enter" && r === d.rader.length - 1 && c === d.kolonner.length - 1 && d.rader.length < D.grenser.rader) {
            e.preventDefault();
            strukturEndring(b, function () { d.rader.push(new Array(d.kolonner.length).fill("")); }, function () { fokusCelle(b, d.rader.length - 1, 0); });
          }
        });
        inp.addEventListener("paste", function (e) {
          var tekst = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
          if (tekst.indexOf("\t") < 0 && tekst.indexOf("\n") < 0) { return; }
          e.preventDefault();
          var celler = KS.parseTabellTekst(tekst);
          strukturEndring(b, function () {
            celler.forEach(function (linje, li) {
              while (d.rader.length <= r + li && d.rader.length < D.grenser.rader) { d.rader.push(new Array(d.kolonner.length).fill("")); }
              var mal = d.rader[r + li]; if (!mal) { return; }
              linje.forEach(function (v, ci) { if (c + ci < d.kolonner.length) { mal[c + ci] = v.slice(0, D.grenser.celle); } });
            });
          });
          KS.toast("Innlimt fra Excel: " + KS.flertall(celler.length, "rad", "rader") + ".", { angre: KS.angre });
        });
        var td = KS.el("td", { attr: { "data-l": d.kolonner[c] || "Kolonne " + (c + 1) }, barn: [inp] });
        tr.appendChild(td);
      });
      var knapper = KS.el("td", { klasse: "slett-celle" });
      function knapp(ikon, tekst, fn, fare) {
        var k = KS.el("button", { klasse: "ks-ib" + (fare ? " fare" : ""), attr: { type: "button", "data-endrer": "", "aria-label": tekst + " (rad " + (r + 1) + ")", title: tekst }, barn: [KS.ikon(ikon)] });
        k.addEventListener("click", fn); knapper.appendChild(k);
      }
      knapp("opp", "Flytt raden opp", function () { if (r > 0) { strukturEndring(b, function () { d.rader.splice(r - 1, 0, d.rader.splice(r, 1)[0]); }); } });
      knapp("ned", "Flytt raden ned", function () { if (r < d.rader.length - 1) { strukturEndring(b, function () { d.rader.splice(r + 1, 0, d.rader.splice(r, 1)[0]); }); } });
      knapp("x", "Fjern raden", function () { strukturEndring(b, function () { d.rader.splice(r, 1); }); KS.toast("Raden er fjernet.", { angre: KS.angre }); }, true);
      tr.appendChild(knapper);
      tbody.appendChild(tr);
    });
    tabell.appendChild(thead); tabell.appendChild(tbody);
    container.appendChild(tabell);
    KS.laas(container);
  }
  function fokusCelle(b, r, c) {
    var k = KS.kortFor(b.id);
    var rad = k && KS.$$("tbody tr", k)[r], f = rad && KS.$$("input", rad)[c];
    if (f) { f.focus(); }
  }
  bygger.tabell = function (b, innhold) {
    var r = KS.klon("ks-mal-tabell"); innhold.appendChild(r);
    var flate = KS.felt("tabell", r), d = b.data;
    byggTabell(b, flate);
    KS.ks("ny-tabellrad", r).addEventListener("click", function () {
      if (d.rader.length >= D.grenser.rader) { KS.toast("Tabellen kan ha høyst " + D.grenser.rader + " rader.", { niva: "feil" }); return; }
      strukturEndring(b, function () { d.rader.push(new Array(d.kolonner.length).fill("")); }, function () { fokusCelle(b, d.rader.length - 1, 0); });
    });
    KS.ks("ny-kolonne", r).addEventListener("click", function () {
      if (d.kolonner.length >= D.grenser.kolonner) { KS.toast("Tabellen kan ha høyst " + D.grenser.kolonner + " kolonner.", { niva: "feil" }); return; }
      strukturEndring(b, function () { d.kolonner.push("Kolonne " + (d.kolonner.length + 1)); d.rader.forEach(function (rad) { rad.push(""); }); });
    });
    KS.ks("tabell-excel", r).addEventListener("click", function () {
      KS.excelDialog({ ok: function (tekst, overskrifter) {
        var rader = KS.parseTabellTekst(tekst);
        if (!rader.length || !rader[0].length) { return "Ingenting å lime inn."; }
        var kolonner = Math.min(D.grenser.kolonner, Math.max.apply(null, rader.map(function (x) { return x.length; })));
        var hode = overskrifter ? rader.shift() : null;
        if (rader.length > D.grenser.rader) { return "For mange rader (maks " + D.grenser.rader + ")."; }
        strukturEndring(b, function () {
          d.kolonner = hode ? hode.slice(0, kolonner).map(function (x) { return x.slice(0, D.grenser.kolonne_tekst); }) : d.kolonner.slice(0, kolonner);
          while (d.kolonner.length < kolonner) { d.kolonner.push("Kolonne " + (d.kolonner.length + 1)); }
          d.rader = rader.map(function (rad) { return (rad.concat(new Array(kolonner).fill(""))).slice(0, kolonner).map(function (x) { return x.slice(0, D.grenser.celle); }); });
        });
        KS.toast("Tabellen er erstattet med det du limte inn.", { angre: KS.angre });
        return null;
      } });
    });
    KS.ks("tabell-navn", r).addEventListener("click", function () {
      KS.navnDialog({ ok: function (navn) {
        var cel = sisteCelle[b.id];
        if (!cel || !d.rader[cel.r]) { cel = { r: 0, c: Math.min(1, d.kolonner.length - 1) }; }
        if (!d.rader.length) { d.rader.push(new Array(d.kolonner.length).fill("")); cel = { r: 0, c: Math.min(1, d.kolonner.length - 1) }; }
        strukturEndring(b, function () { var gammel = d.rader[cel.r][cel.c]; d.rader[cel.r][cel.c] = ((gammel ? gammel + ", " : "") + navn.join(", ")).slice(0, D.grenser.celle); });
      } });
    });
    return r;
  };

  /* ================= kontakt ================= */
  var TELEFON = /^[+0-9() -]{5,20}$/, EPOST = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  function initialer(navn) {
    var o = (navn || "").replace(/,/g, " ").split(/\s+/).filter(Boolean);
    return o.length ? ((o.length > 1 ? o[0][0] + o[o.length - 1][0] : o[0].slice(0, 2))).toUpperCase() : "?";
  }
  function byggPerson(b, p) {
    var r = KS.klon("ks-mal-person");
    bind(r, p, ["navn", "rolle", "telefon", "epost"], function (navn) {
      KS.felt("initialer", r).textContent = initialer(p.navn);
      sjekk();
      endret(b);
    });
    function sjekk() {
      var t = KS.felt("telefon", r), e = KS.felt("epost", r);
      var tf = p.telefon && !TELEFON.test(p.telefon), ef = p.epost && !EPOST.test(p.epost);
      if (tf) { t.setAttribute("aria-invalid", "true"); t.title = "Bare tall, mellomrom, + og parenteser"; } else { t.removeAttribute("aria-invalid"); t.removeAttribute("title"); }
      if (ef) { e.setAttribute("aria-invalid", "true"); e.title = "Skriv en gyldig e-postadresse"; } else { e.removeAttribute("aria-invalid"); e.removeAttribute("title"); }
    }
    KS.felt("initialer", r).textContent = initialer(p.navn);
    sjekk();
    return r;
  }
  bygger.kontakt = function (b, innhold) {
    var r = KS.klon("ks-mal-kontakt"); innhold.appendChild(r);
    var pl = KS.felt("personer", r), personer = b.data.personer;
    personer.forEach(function (p) { pl.appendChild(byggPerson(b, p)); });
    function leggTil(p) {
      if (personer.length >= D.grenser.personer) { KS.toast("Høyst " + D.grenser.personer + " personer i en kontaktblokk.", { niva: "feil" }); return; }
      strukturEndring(b, function () { personer.push(p); }, function () {
        var k = KS.$$(".ks-kontakt", KS.felt("personer", KS.kortFor(b.id))).pop(), f = k && KS.felt("navn", k); if (f) { f.focus(); }
      });
    }
    KS.ks("ny-person", r).addEventListener("click", function () { leggTil({ navn: "", rolle: "", telefon: "", epost: "" }); });
    KS.ks("hent-ansvarlig", r).addEventListener("click", function () {
      var a = D.ansvarlig;
      if (!a) { KS.toast("Kurset har ingen kursansvarlig i Oppsett ennå.", { niva: "feil" }); return; }
      leggTil({ navn: a.navn, rolle: "Kursansvarlig", telefon: "", epost: a.epost || "" });
    });
    r.addEventListener("click", function (e) {
      var k = e.target.closest('[data-ks="rad-slett"]');
      if (!k) { return; }
      var idx = KS.$$(".ks-kontakt", pl).indexOf(k.closest(".ks-kontakt"));
      strukturEndring(b, function () { personer.splice(idx, 1); });
      KS.toast("Personen er fjernet.", { angre: KS.angre });
    });
    return r;
  };

  /* ================= bilde ================= */
  bygger.bilde = function (b, innhold) {
    var r = KS.klon("ks-mal-bilde"); innhold.appendChild(r);
    var d = b.data, img = KS.felt("bilde", r), ingen = KS.felt("ingen", r), flate = KS.ks("filslipp", r);
    KS.$$('[data-felt="plassering"]', r).forEach(function (f) { f.name = "ks-plass-" + b.id; });
    function visBilde() {
      var m = d.fil_id !== null && d.fil_id !== undefined ? KS.filMeta(d.fil_id) : null;
      if (m) { img.src = D.urls.fil.replace("{id}", m.id); img.hidden = false; ingen.hidden = true; flate.classList.add("har-bilde"); }
      else { img.hidden = true; ingen.hidden = false; flate.classList.remove("har-bilde"); }
      KS.felt("bildeinfo", r).textContent = m ? [m.filnavn, m.bredde && m.hoyde ? m.bredde + " × " + m.hoyde : "", KS.storrelse(m.storrelse)].filter(Boolean).join(" · ") : "Ingen bildefil er valgt ennå.";
      KS.felt("velg-tekst", r).textContent = m ? "Bytt bilde …" : "Velg bilde …";
    }
    visBilde();
    bind(r, d, ["alt", "tekst", "plassering"], function () { endret(b); });
    KS.ks("velg-bilde", r).addEventListener("click", function () { KS.felt("filvalg", r).click(); });
    KS.felt("filvalg", r).addEventListener("change", function (e) { KS.lastOppFiler(Array.prototype.slice.call(e.target.files, 0, 1), { blokkId: b.id, type: "bilde" }); e.target.value = ""; });
    KS.aktiverFilslipp(flate, function (filer) { KS.lastOppFiler(filer.slice(0, 1), { blokkId: b.id, type: "bilde" }); });
    r.__visBilde = visBilde;
    return r;
  };

  /* ================= felles inngang ================= */
  KS.byggBody = function (b, innhold) {
    var f = bygger[b.type];
    if (!f) { return; }
    f(b, innhold);
    innhold.appendChild(synlighet(b));
    KS.laas(innhold);
  };
  KS.blokkBygger = bygger;
})();
