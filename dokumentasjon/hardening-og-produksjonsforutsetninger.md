# Hardening-backlog og produksjonsforutsetninger

Sist oppdatert etter trinn 2.5 (rettelsesrunde etter audit). Dette er **kjente, bevisst utsatte** punkter -
ingenting her er glemt, men ingenting her er bygget ennå. Skal gås gjennom før `MODUS=prod`.

## A. Uavgjort: Graph/Visma exception-taksonomi (ikke gjett)

Vi vet **ikke** ennå hvilke feil fra Microsoft Graph (`sendMail`) og Visma som er *bevist sikkert feilet
før ekstern sideeffekt* (f.eks. avvist før noe ble opprettet) og hvilke som kan ha gått gjennom
(tidsavbrudd, 5xx, tapt forbindelse, feil ved lesing av svar).

**Regel inntil dette er avklart og testet mot sandbox:** alt som ikke eksplisitt er bevist sikkert feilet
klassifiseres som **`ukjent`**, og **ukjent prøves aldri automatisk på nytt** (verken e-post eller faktura).
`status='feilet'` og `reserver_*_pa_nytt()` finnes og er atomiske, men **ingen produksjonskode setter
`feilet` ennå** - retry-stien er i praksis inaktiv.

## B. Utsatt før prod (bevisst ikke gjort i trinn 2.5)

| # | Punkt | Hvorfor det betyr noe |
|---|---|---|
| 7 | **Refaktorer `Kjoring.send_admin_utsending`** (fase 5) til claim-/resultatmodellen | Holder skrivelås over nettverkskall fra mottaker nr. 2 (ingen commit mellom `marker_sendt` og neste `epost.send`), og har ingen atomisk claim - dobbeltklikk på «Send» kan i teorien sende to ganger. |
| 8 | **Per-melding feilhåndtering i `daglig`** (`_innkallinger`, `_purring`, `kursbevis`) | En Graph-feil i én melding avbryter hele dagens jobb, inkludert senere steg (også sletting av sensitive data). Meldingen som feilet blir dessuten permanent `ukjent`. Fang per melding, logg sikkert, fortsett. |
| 9 | **Visma reconciliation / ekstra provider-referanse** | Etter «Visma OK, lokal lagring feilet» (og ved `ukjent` fra Visma) finnes ingen lagret referanse (`visma_id`/`faktura_nr`) å avstemme mot. Trengs: idempotensnøkkel/eksternt referansefelt i Visma-kallet og en avstemmingsrutine. Avhenger av at Visma-produktet er bekreftet. |
| 10 | **Admin-UI for å løse uavklarte** (ukjent/gammel reservert) | Teller vises på forsiden, men det finnes ingen liste og ingen trygg handling («bekreft sendt» / «bekreft ikke sendt, tillat nytt forsøk»). Inntil da: manuell SQL etter kontroll i Graph/Visma. |

## C. Andre kjente begrensninger (etter rettelsesrunden)

- **Traceback-utskrift:** `send_en_gang` re-kaster originalfeilen. Fanges den av `sveiper` logges kun sikker
  tekst, men i `daglig`-løkkene (punkt 8) kan en ufanget traceback skrive rå feiltekst til stderr/driftslogg.
- **Uavklart e-post logges ikke som egen hendelse ved krasj** (kun `epost_ukjent` ved fanget feil). Live-telleren
  på forsiden fanger likevel både `ukjent` og gammel `reservert`, siden den leser tilstand, ikke hendelser.
- **`faktura_reservert_gammel`** logges på nytt ved hver kjøring så lenge raden står fast (teller ikke lenger,
  men fyller hendelsesloggen).
- **`sveip_feil`/`faktura_feil`** telles ikke lenger på forsiden (kun uavklart tilstand teller). De ligger
  fortsatt i hendelsesloggen.
- **`sveiper_kjort=1` committes ikke i `sveiper._en`** (kalleren committer). Uskyldig: neste kjøring finner alt
  ferdig og setter den igjen (idempotent).
- **Claim-committen tar med ventende arbeid** fra kalleren (gjelder alle `send_en_gang`-kall). Ingen kjent
  call-site avhenger av rollback av slikt arbeid; se rettelsesrunden.
- **Skrivelås ved Zoom-kall i `daglig`** (`_zoom`) kan holdes hvis forrige steg lot en transaksjon stå åpen
  (lest i koden, ikke målt). Ikke Graph/Visma, men samme klasse.
- **Normaliser `faktura_forsok`-timestamps til UTC** (akseptert som senere punkt). I dag lagres `opprettet` som lokal
  Python-tid, mens `utsending_logg.sendt_ts` er UTC. Hver tabell sammenlignes mot sin egen klokke, så 5-minutters-
  grensen er konsistent, men ved overgang til vintertid kan flagging av en fast `reservert` faktura forsinkes med
  opptil én time. Påvirker aldri sikkerheten (ukjent/reservert prøves aldri automatisk på nytt).
- **`sikker_feiltekst` gir kun type + HTTP-status.** Skal en kontrollert intern feilkode innføres, gjøres det her.

## D. Produksjonsforutsetninger (senere, ikke bekreftet)

- **Visma-produkt hos IPR er ubekreftet** (eAccounting vs. Visma.net vs. Business NXT). `visma.py` er et utgangspunkt.
  Endringer testes i demo og Visma sandbox før prod.
- **Azure / Entra ID / Graph / SharePoint** er senere produksjonspremisser (app-registrering, `Mail.Send` begrenset
  til kurs-postboksen, `Sites.Selected`, Key Vault for hemmeligheter, lagring av roterende Visma refresh-token).
- **Databaseendringer i drift** må være migreringer (ikke slette/lage tabeller på nytt); spør Jan før eksisterende
  kolonner endres. `faktura_forsok` er en ny tabell og krever ingen endring av `faktura`.
- **SQLite og samtidighet:** claim-modellen forutsetter én databasefil og korte transaksjoner. Ved flere
  samtidige prosesser i drift bør `busy_timeout`/WAL vurderes eksplisitt.
