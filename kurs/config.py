"""Konfigurasjon lest fra miljovariabler / .env.

MODUS=demo  -> ingen ekte integrasjoner. E-post havner i utboks/, Visma/Zoom/SharePoint logges.
MODUS=prod  -> ekte API-kall. Krever at nokler er satt (se .env.example).
"""
import os
from pathlib import Path

ROT = Path(__file__).resolve().parent.parent


def _last_env() -> None:
    env = ROT / ".env"
    if not env.exists():
        return
    for linje in env.read_text(encoding="utf-8").splitlines():
        linje = linje.strip()
        if linje and not linje.startswith("#") and "=" in linje:
            k, v = linje.split("=", 1)
            v = v.split(" #", 1)[0].strip()  # fjern kommentar på slutten av linjen
            os.environ.setdefault(k.strip(), v)


_last_env()


def get(navn: str, standard: str = "") -> str:
    return os.environ.get(navn, standard)


MODUS = get("MODUS", "demo")
DEMO = MODUS != "prod"
# Demo har bare oppdiktede virksomheter og gjør ingen nettverkskall (CLAUDE.md regel 3). BRREG_LIVE=1 er et bevisst unntak KUN
# for Enhetsregisteret (åpne data, ingen nøkler): nummer og navn som ikke er en oppdiktet demovirksomhet, slås opp i det ekte
# registeret, så skjemaet kan prøves med ekte organisasjonsnumre. Alt annet i demo er uendret. Standard: av (og aldri i tester).
BRREG_LIVE = get("BRREG_LIVE", "0") == "1"
DB_STI = Path(get("DB_STI", str(ROT / "data" / "kurs.db")))
# Tom (standard) = SQLite i DB_STI, som i den lokale sandkassen. Satt = PostgreSQL, f.eks.
# postgresql://bruker:passord@server.postgres.database.azure.com:5432/ipr?sslmode=require (hemmelig: Key Vault i drift).
DATABASE_URL = get("DATABASE_URL")
UTBOKS = ROT / "utboks"
BASE_URL = get("BASE_URL", "http://127.0.0.1:5000")
HEMMELIG_NOKKEL = get("HEMMELIG_NOKKEL", "bytt-meg-i-prod")
# Brukes kun til aa opprette den aller forste admin-brukeren (se db.init). Etter det logger
# hver ansatt inn med sin egen bruker og sitt eget passord, opprettet under Admin -> Brukere.
ADMIN_BRUKERNAVN = get("ADMIN_BRUKERNAVN", "admin")
ADMIN_PASSORD = get("ADMIN_PASSORD", "demo")

AVSENDER_EPOST = get("AVSENDER_EPOST", "kurs@ipr.no")
# Kontaktadressen for Terapiakademiet-kurs (kurs.merke): deltakerne skal kontakte post@terapiakademiet.no, IPR-kurs kurs@ipr.no
# (Camilla 02.10 og 09.10.2026, tema.kontakt_epost). Avsenderen av e-postene er fortsatt AVSENDER_EPOST.
TERAPIAKADEMIET_EPOST = get("TERAPIAKADEMIET_EPOST", "post@terapiakademiet.no")
AVSENDER_NAVN = get("AVSENDER_NAVN", "IPR Påmeldingssystem")
ADMIN_EPOST = get("ADMIN_EPOST", "admin@ipr.no")
# Morgen-e-posten om sjekklistene for planlagte kurs (daglig.py): alltid til kurspostboksen (Camilla 02.10.2026)
SJEKKLISTE_EPOST = get("SJEKKLISTE_EPOST", "kurs@ipr.no")
# «Klar til sending» (kurs/godkjenning.py, Camilla 05.10.2026): automatiske e-poster utenom bekreftelse og venteliste venter på
# godkjenning før de sendes. 0 = alt går automatisk, slik det var før.
GODKJENN_EPOSTER = get("GODKJENN_EPOSTER", "1") != "0"

# Microsoft 365 (e-post via Graph + SharePoint) – én app-registrering i Entra ID
M365_TENANT_ID = get("M365_TENANT_ID")
M365_CLIENT_ID = get("M365_CLIENT_ID")
M365_CLIENT_SECRET = get("M365_CLIENT_SECRET")
# Sertifikat i stedet for client secret (Serit anbefaler det, 04.10.2026): Key Vault-referanse til sertifikatets
# hemmelighet - PEM-tekst eller base64-kodet PFX. Brukes framfor M365_CLIENT_SECRET når det er satt.
M365_SERTIFIKAT = get("M365_SERTIFIKAT")
M365_SERTIFIKAT_PASSORD = get("M365_SERTIFIKAT_PASSORD")       # bare for en PFX med passord (Key Vault lager dem uten)

# Admin-innlogging med Microsoft Entra ID (fase 13). Egen app-registrering anbefales (delegert: openid profile email),
# men M365-registreringen kan gjenbrukes - tomme verdier faller tilbake til M365_*. Ingen verdier = Entra av.
ENTRA_TENANT_ID = get("ENTRA_TENANT_ID") or M365_TENANT_ID
ENTRA_CLIENT_ID = get("ENTRA_CLIENT_ID") or M365_CLIENT_ID
ENTRA_CLIENT_SECRET = get("ENTRA_CLIENT_SECRET") or M365_CLIENT_SECRET
# Sertifikat for innloggingen (som M365_SERTIFIKAT). Faller tilbake til M365-sertifikatet bare når innloggingen bruker
# samme app-registrering (ingen egen ENTRA_CLIENT_ID): et sertifikat hører til én registrering.
ENTRA_SERTIFIKAT = get("ENTRA_SERTIFIKAT") or ("" if get("ENTRA_CLIENT_ID") else M365_SERTIFIKAT)
ENTRA_SERTIFIKAT_PASSORD = get("ENTRA_SERTIFIKAT_PASSORD") or ("" if get("ENTRA_CLIENT_ID") else M365_SERTIFIKAT_PASSORD)
# App-roller (anbefalt) fra app-registreringen -> rolle i systemet. Format: "verdi=rolle,verdi=rolle".
ENTRA_ROLLER = get("ENTRA_ROLLER", "ipr.system=system,ipr.kursadmin=kursadmin,ipr.lese=lese")
# Alternativt/i tillegg: sikkerhetsgrupper (objekt-id) -> rolle. Format: "<gruppe-oid>=system,<gruppe-oid>=kursadmin".
ENTRA_GRUPPER = get("ENTRA_GRUPPER", "")
# Lokal innlogging med brukernavn/passord: paa i demo; i drift kun for noedbrukere naar dette settes til 1.
ADMIN_LOKAL_INNLOGGING = get("ADMIN_LOKAL_INNLOGGING", "1" if DEMO else "0") == "1"

# Zoom Server-to-Server OAuth
ZOOM_ACCOUNT_ID = get("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID = get("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = get("ZOOM_CLIENT_SECRET")

# Visma (antatt Visma eAccounting / eRegnskap – MAA bekreftes i moetet)
VISMA_CLIENT_ID = get("VISMA_CLIENT_ID")
VISMA_CLIENT_SECRET = get("VISMA_CLIENT_SECRET")
VISMA_REFRESH_TOKEN = get("VISMA_REFRESH_TOKEN")
VISMA_API = get("VISMA_API", "https://eaccountingapi.vismaonline.com/v2")

# Visma Business NXT (GraphQL, klient-legitimasjon). Fase 1: KUN lesing av fakturastatus - se
# integrasjoner/business_nxt.py. Egne navn (BNXT_*), saa eAccounting-koden over aldri aktiveres ved en feil.
# Hemmeligheten settes som miljoevariabel lokalt og i Key Vault i drift - aldri i en fil, databasen eller en logg.
BNXT_CLIENT_ID = get("BNXT_CLIENT_ID")
BNXT_CLIENT_SECRET = get("BNXT_CLIENT_SECRET")
BNXT_KUNDENR = get("BNXT_KUNDENR")        # Visma.net-kundenummer - trengs bare for aa liste selskapene (bnxt_sjekk)
BNXT_SELSKAP = get("BNXT_SELSKAP")        # Visma.net-selskaps-ID: useCompany(no: ...)
BNXT_TOKEN_URL = get("BNXT_TOKEN_URL", "https://connect.visma.com/connect/token")
BNXT_API = get("BNXT_API", "https://business.visma.net/api/graphql-service")

# Mottak fra nettsidens skjema (erstatter innsending til Pindena)
WEBHOOK_HEMMELIG = get("WEBHOOK_HEMMELIG", "demo-webhook-hemmelighet")
# Deltakerens PRIVATE adresse (adresse, postnr, poststed) MÅ være med når noen melder seg på, uansett vei inn: det offentlige skjemaet,
# bedriftspåmeldingen, «Legg til deltaker», webhooken fra ipr.no og CSV-importen. Fakturaen sendes automatisk ved påmelding (tidligst
# seks måneder før første kursdag), og da må fakturaadressen være klar. Uten adresse svarer webhooken 400 med hvilke felt som mangler, og en
# CSV-fil uten adressekolonnene (eller en rad uten adresse) blokkeres i forhåndsvisningen. Ingenting registreres.
# Webhooken og CSV-importen har hver sin innstilling, og begge er PÅ som standard: bare verdien «0» slår kravet av. Tom eller ukjent
# verdi teller som PÅ, så en feilskrevet innstilling aldri kan fjerne kravet uten at noen har valgt det. AV (0) er en NØDBREMS som
# bare skal brukes midlertidig i overgangsperioden hvis noe stopper (for eksempel at skjemaet på ipr.no ikke sender adressen ennå).
# Webhook med nødbremsen på: påmeldingen går ikke tapt. Den tas inn selv om adressen mangler (en delvis adresse tas vare på som
# ufullstendige data), merkes «Privat adresse mangler/er ufullstendig», og en privat faktura holdes tilbake til alle tre feltene er
# komplette (kurs/privatadresse.py). CSV med nødbremsen på: adressen er
# valgfri, men er noen av de tre feltene fylt ut, kreves alle tre. Når ipr.no sender adressen riktig, settes begge tilbake til PÅ
# (fjern «0»). Før produksjon: skjemaet/pluginen på ipr.no MÅ sende adresse, postnr og poststed (OPERATIONS.md, «Adressekravet i
# webhook og CSV-import»).
ADRESSE_KREVES_I_WEBHOOK = get("ADRESSE_KREVES_I_WEBHOOK", "1").strip() != "0"
ADRESSE_KREVES_I_CSV = get("ADRESSE_KREVES_I_CSV", "1").strip() != "0"

# KI-assistent (Claude). Uten nokkel / i demo brukes enkel ordmatching mot kunnskapsbasen.
ANTHROPIC_API_KEY = get("ANTHROPIC_API_KEY")
ASSISTENT_MODELL = get("ASSISTENT_MODELL", "claude-opus-5")
# «Spør oss» og Kunnskapsbase er AV som standard (menyvalgene er borte, /sporsmal gir 404). Kode og data er beholdt: 1 slår begge på igjen.
ASSISTENT_AKTIV = get("ASSISTENT_AKTIV", "0") == "1"
ASSISTENT_MAKS_PER_DAG = int(get("ASSISTENT_MAKS_PER_DAG", "300"))  # kostnadsgrense: KI-kall per dag, deretter kun til adm
ASSISTENT_TIDSAVBRUDD_SEK = int(get("ASSISTENT_TIDSAVBRUDD_SEK", "20"))

# Innsjekk (QR): 0 (standard) = hvem som helst med QR-koden kan skrive en påmeldt persons e-postadresse og registrere oppmøtet hennes (raskt,
# men adressen er ikke bevist). 1 = uinnloggede må logge inn først (engangslenke på e-post, eller den personlige lenken til Min side fra e-postene)
# og trykker så én knapp: sikrere, men tregere i kurslokalet.
INNSJEKK_KREVER_INNLOGGING = get("INNSJEKK_KREVER_INNLOGGING", "0") == "1"

# Personvern
SLETT_SENSITIVT_ETTER_DAGER = int(get("SLETT_SENSITIVT_ETTER_DAGER", "14"))

# Kursside (siden deltakerne ser etter innlogging, én per kurs; kurs/sideinnhold.py og kurs/sidelager.py)
KURSSIDE_ETTERTILGANG_DAGER = int(get("KURSSIDE_ETTERTILGANG_DAGER", "180"))        # STANDARD: åpen til siste kursdag + så mange dager (per kurs: kursside.apen_dager)
KURSSIDE_TOM_TABELLER_ETTER_DAGER = int(get("KURSSIDE_TOM_TABELLER_ETTER_DAGER", "30"))  # gruppetabeller tømmes så mange dager etter siste kursdag
KURSSIDE_FIL_MAKS_MB = int(get("KURSSIDE_FIL_MAKS_MB", "15"))                       # største dokument
KURSSIDE_BILDE_MAKS_MB = int(get("KURSSIDE_BILDE_MAKS_MB", "5"))                    # største bilde
KURSSIDE_KURS_MAKS_MB = int(get("KURSSIDE_KURS_MAKS_MB", "100"))                    # alle filer på ett kurs til sammen
KURSSIDE_MAKS_FILER = int(get("KURSSIDE_MAKS_FILER", "200"))                        # antall filer på ett kurs
