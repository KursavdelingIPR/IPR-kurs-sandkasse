/* Test av adressegruppen i deltakervinduet (static/app.js, «data-adressegruppe»): feltene er ikke påkrevd fra serveren for en person uten
   komplett adresse, og blir påkrevd (alle tre) først når noen skriver noe i dem. Kjøres av tests/test_adresse_valgfri.py (hoppes over uten Node).
   Ingen nettleser: bare den ene lytteren hentes ut av app.js og kjøres i en `vm` med et lite falskt DOM. KS_STATIC: en endret kopi
   (mutasjonsprøve). Bare oppdiktede data. Skriver «OK» og avslutter med 0, ellers en melding og avslutter med 1. */
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = process.env.KS_STATIC || path.join(__dirname, "..", "..", "kurs", "web", "static");
const kilde = fs.readFileSync(path.join(STATIC, "app.js"), "utf8").replace(/\r\n/g, "\n");

// Lytteren: fra «document.addEventListener("input", ...» der den leter etter form[data-adressegruppe], til første «\n  });»
const start = kilde.indexOf('document.addEventListener("input", function (e) {\n    var gruppe');
assert.ok(start >= 0, "fant ikke lytteren for data-adressegruppe i app.js");
const slutt = kilde.indexOf("\n  });", start);
assert.ok(slutt > start, "fant ikke slutten på lytteren");
const blokk = kilde.slice(start, slutt + "\n  });".length);

class Element {}
let lytter = null, antallLyttere = 0;
const dokument = { addEventListener(type, f) { assert.strictEqual(type, "input"); lytter = f; antallLyttere++; } };
vm.runInNewContext(blokk, { document: dokument, Element });
assert.strictEqual(antallLyttere, 1, "blokken skal registrere nøyaktig én input-lytter");

function skjema(verdier) {
  const s = new Element();
  s.closest = (sel) => (sel === "form[data-adressegruppe]" ? s : null);
  s.felt = verdier.map((v) => {
    const f = new Element();
    f.value = v; f.defaultValue = v; f.required = false;
    f.closest = s.closest;
    return f;
  });
  s.querySelectorAll = (sel) => { assert.strictEqual(sel, "input[data-adressefelt]"); return { forEach: (fn) => s.felt.forEach(fn) }; };
  return s;
}
const krav = (s) => s.felt.map((f) => f.required);
const skriv = (s, i, verdi) => { s.felt[i].value = verdi; lytter({ target: s.felt[i] }); };

// 1. Ingen adresse fra før: ikke påkrevd før noen skriver; da blir alle tre påkrevd; tømmes de igjen, er ingen påkrevd.
let s = skjema(["", "", ""]);
assert.deepStrictEqual(krav(s), [false, false, false]);
skriv(s, 0, "Eksempelveien 1");
assert.deepStrictEqual(krav(s), [true, true, true], "en utfylt del gjør alle tre påkrevd");
skriv(s, 0, "");
assert.deepStrictEqual(krav(s), [false, false, false], "tømt igjen (tilbake til utgangspunktet): ingen påkrevd");

// 2. Bare mellomrom teller ikke som utfylt.
s = skjema(["", "", ""]);
skriv(s, 2, "   ");
assert.deepStrictEqual(krav(s), [false, false, false]);

// 3. Utfylt uten endring (samme verdi som ved innlasting): ikke påkrevd (en person med delvis adresse kan lagre andre felt).
s = skjema(["Vei 1", "", ""]);
lytter({ target: s.felt[0] });
assert.deepStrictEqual(krav(s), [false, false, false], "uendret adresse blir ikke påkrevd");
skriv(s, 1, "0150");
assert.deepStrictEqual(krav(s), [true, true, true], "endres noe, blir alle tre påkrevd");
skriv(s, 1, "");
assert.deepStrictEqual(krav(s), [false, false, false], "endringen angret: ikke påkrevd");

// 4. Tømmes en delvis adresse helt, kreves ingenting fra nettleseren (serveren avviser det, se test_adresse_valgfri.py).
s = skjema(["Vei 1", "", ""]);
skriv(s, 0, "");
assert.deepStrictEqual(krav(s), [false, false, false]);
skriv(s, 0, "   ");
assert.deepStrictEqual(krav(s), [false, false, false], "bare mellomrom er det samme som tomt");

// 5. Andre felt i vinduet (utenfor gruppen) og hendelser som ikke kommer fra et element, rører ingenting og krasjer ikke.
s = skjema(["", "", ""]);
const telefon = new Element();
telefon.closest = () => null;
lytter({ target: telefon });
lytter({ target: {} });
lytter({ target: null });
assert.deepStrictEqual(krav(s), [false, false, false]);

console.log("OK adressegruppen: 5 scenarier");
