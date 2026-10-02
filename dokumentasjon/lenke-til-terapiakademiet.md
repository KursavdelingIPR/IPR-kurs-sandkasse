# Deltakerinngang på terapiakademiet.no – lenke og tekst

Deltakerne skal kunne komme til Min side for kurset sitt fra terapiakademiet.no. Det gjør du med **en vanlig lenke** (eller knapp) til innloggingssiden i påmeldingssystemet.
Innloggingssiden kan ikke bygges inn som skjema eller ramme på en annen nettside (det er bevisst, av sikkerhetsgrunner), så det må være en lenke som åpner siden.

## Adressen

`<systemets adresse>` er adressen påmeldingssystemet har i drift, altså verdien i innstillingen `BASE_URL` (den samme som står i lenkene i e-postene). Fyll den inn. **Planlagt adresse:** `https://minside.terapiakademiet.no` (Camilla har godkjent navnet 02.10.2026). Lenken og adressen legges ikke inn før påmeldingssystemet er helt klart; punktet står i sjekklisten i `DEPLOYMENT.md` avsnitt 6.

| Hva du vil | Lenke |
|---|---|
| **Én felles lenke for alle kurs** (anbefalt) | `https://<systemets adresse>/logg-inn` |
| Rett til ett bestemt kurs | `https://<systemets adresse>/logg-inn?neste=/kurs/<kode>/deltakerside` |

Den andre lenken finner du ferdig utfylt og med «Kopier lenke»-knapp øverst på fanen **Min side** på kurset. Med den felles lenken finner systemet selv riktig kurs:
har deltakeren ett kurs som pågår eller kommer, havner hen rett på Min side for kurset; har hen flere, havner hen på kurset som har kursdag i dag, ellers på Mine kurs, der hen velger kurs.

I sandkassen (demo) er adressen `http://127.0.0.1:5000/logg-inn`. Prøv den med `kari.nordmann@example.no`.

## Tekst som kan limes rett inn

> **Deltakerinngang**
>
> Er du påmeldt et kurs hos oss? Logg inn på Min side med e-postadressen du meldte deg på med. Du trenger ikke passord: vi sender deg en lenke som logger deg rett inn.
>
> **[ Logg inn på Min side → ]**   (lenke til `https://<systemets adresse>/logg-inn`)
>
> På Min side finner du program, presentasjoner, gruppeinndeling og litteratur for kurs som har Min side. Under «Mine kurs» ser du alle påmeldingene dine og kan registrere oppmøte på kursdagen.
>
> *Er du ikke påmeldt ennå? Se kursoversikten. Får du ikke logget inn? Sjekk søppelpost og at du bruker e-postadressen du meldte deg på med, eller kontakt kursadministrasjonen.*

På siden til et kurs som har Min side: **«Gå til Min side for dette kurset»** med lenken `https://<systemets adresse>/logg-inn?neste=/kurs/<kode>/deltakerside` (ferdig utfylt under fanen Min side på kurset). Kurs uten Min side får ikke denne lenken.

Hvis nettsiden godtar HTML:

```html
<h2>Deltakerinngang</h2>
<p>Er du påmeldt et kurs hos oss? Logg inn på Min side med e-postadressen du meldte deg på med.
Du trenger ikke passord: vi sender deg en lenke som logger deg rett inn.</p>
<p><a class="knapp" href="https://<systemets adresse>/logg-inn">Logg inn på Min side</a></p>
<p><small>Er du ikke påmeldt ennå? Se kursoversikten. Får du ikke logget inn? Sjekk søppelpost og at du bruker
e-postadressen du meldte deg på med, eller kontakt kursadministrasjonen.</small></p>
```

## Hva deltakeren opplever

1. Deltakeren klikker lenken på terapiakademiet.no og kommer til **Logg inn** i påmeldingssystemet.
2. Deltakeren skriver e-postadressen hen meldte seg på med. Er adressen påmeldt, får hen en e-post med en engangslenke (gyldig i 30 minutter, kan brukes én gang).
3. Deltakeren klikker lenken i e-posten og lander på Min side. Kommer hen fra en lenke til ett bestemt kurs, lander hen akkurat der.
4. Bare deltakere med **bekreftet påmelding på akkurat det kurset** ser Min side. Andre får en nøytral beskjed om at siden ikke finnes eller at de ikke har tilgang.
5. Har deltakeren bare kurs der Min side ikke er åpen (ikke publisert, tatt ned eller stengt), kommer hen likevel inn og ser kurset på «Mine kurs». Der står teksten «Min side er ikke åpen for dette kurset» i stedet for knappen «Åpne Min side».

Deltakerne trenger ikke denne lenken for å komme til Min side: **bekreftelsen og påminnelsene** fra systemet har en knapp «Åpne Min side» med en personlig lenke som virker uten innlogging
(til 30 dager etter siste kursdag). Innsjekk i kurslokalet trenger heller ikke lenken over: deltakerne skanner QR-koden i lokalet.
