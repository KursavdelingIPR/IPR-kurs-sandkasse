"""Fase 12B2C-5: `purring` migrert til maltekstsystemet - siste av de aatte redigerbare malene.

Kritisk retting samtidig med migreringen (se docstring i daglig._purring): FOER denne fasen sjekket koden om
"purring-7" var sendt for AA AVGJORE om et helt ANNET trinn (purring-2/-0) skulle forsokes paa nytt. En render-
/malfeil noeyaktig paa igjen==2 eller igjen==0 (naar purring-7 alt var sendt) hadde da INGEN retry-mulighet og
gikk permanent tapt. Naa sjekkes DAGENS EGET trinn foer det forsokes, saa hvert trinn faar samme flerdagers
retry-vindu som purring-7 alt hadde - unntatt trinn 0, som (som dagfor) uansett bare har EN dag igjen foer
fristen passerer og eskalering til admin overtar (kjent, akseptert begrensning, ikke rettet her).
"""
import json
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, maltekster
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from kurs.maltekster import DB_LESEFEIL, MALER, TOM, UKJENT_FELT, UKJENT_KODE, MalFeil

IDAG = date(2027, 3, 1)


def _n(html):
    return " ".join(html.split())


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def ut(monkeypatch):
    kall = {"epost": []}
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: kall["epost"].append((til, emne, html)))
    return kall


def _kurs(con, kode="P1", **kw):
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i praksis", datoer=[(date.today() + timedelta(days=90)).isoformat()],
                          sharepoint_mappe="Kurs/" + kode, **kw)
    con.commit()
    return kid


def _krav(con, kid, frist: date, navn="Per Psykolog", epost_="per@x.no", beskrivelse="Kurspresentasjon"):
    krav_id = db.sett_inn(
        con, "INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, beskrivelse, frist) VALUES (?,?,?,?,?)",
        (kid, navn, epost_, beskrivelse, frist.isoformat()))
    con.commit()
    return krav_id


def _daglig(con, idag):
    daglig._purring(Kjoring(con, idag=idag))
    con.commit()


def _mail(ut, til="per@x.no"):
    m = [x for x in ut["epost"] if x[0] == til]
    (t, emne, html), = m
    return emne, _n(html)


def _lagre(con, felt, tekst):
    maltekster.lagre_maltekst(con, "purring", felt, tekst, aktor="admin:test")
    con.commit()


def _korrupt(con, felt, tekst):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('purring', ?, ?) "
                "ON CONFLICT (mal, felt) DO UPDATE SET tekst=excluded.tekst", (felt, tekst))
    con.commit()


def _logg(con):
    return [json.loads(r[0]) for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='purring_mal_feil'")]


def _claims(con, mottaker="per@x.no"):
    return [tuple(r) for r in con.execute(
        "SELECT type, status FROM utsending_logg WHERE mottaker=? ORDER BY type", (mottaker,))]


# ============================ A/B: aktivering ============================

def test_a_purring_er_aktiv():
    assert "purring" in maltekster.AKTIVE_MALER
    assert maltekster._VERDIER["purring"] is maltekster._purring_verdier


def test_b_alle_atte_redigerbare_maler_er_aktive():
    assert maltekster.AKTIVE_MALER == frozenset(MALER)
    assert len(MALER) == 8
    for mal, felt in MALER.items():
        for f in felt.felt:
            # skal kunne lagres for ALLE 8 - ingen IKKE_AKTIVERT lenger
            maltekster.valider(mal, f, maltekster.standard_tekst(mal, f))


# ============================ C: standardtekst uendret (gullstandard) ============================

@pytest.mark.parametrize("igjen,emne_forventet", [
    (7, "Påminnelse: Kurspresentasjon til Veiledning i praksis – frist om 7 dager"),
    (2, "Påminnelse: Kurspresentasjon til Veiledning i praksis – frist om 2 dager"),
    (0, "Frist i dag: Kurspresentasjon til Veiledning i praksis"),
])
def test_c_standardtekst_uendret_for_alle_varianter_via_ekte_daglig_flyt(con, ut, igjen, emne_forventet):
    kid = _kurs(con)
    frist = IDAG + timedelta(days=igjen)
    _krav(con, kid, frist)
    _daglig(con, IDAG)
    emne, html = _mail(ut)
    assert emne == emne_forventet
    assert "<p>Hei Per Psykolog,</p>" in html
    assert ("Vi minner om at <strong>kurspresentasjon</strong> til <strong>Veiledning i praksis</strong> "
            f"skal leveres innen <strong>{frist.isoformat()}</strong>.") in html
    assert f"<a href=\"{config.BASE_URL}/lever/" in html and "Last opp her" in html
    assert "Vennlig hilsen<br>Kursadministrasjonen, Institutt for Psykologisk Rådgivning<br>" in html
    m = {"ansvarlig_navn": "Per Psykolog", "beskrivelse": "Kurspresentasjon", "kursnavn": "Veiledning i praksis",
         "frist": frist.isoformat(), "id": con.execute("SELECT id FROM materiell_krav").fetchone()[0]}
    direkte = epost.render("purring", m=m, igjen=igjen)[1]
    assert _n(direkte) == html


# ============================ hardening: entall/flertall i {dager_igjen_tekst} ============================

@pytest.mark.parametrize("igjen,tekst", [(7, "7 dager"), (2, "2 dager"), (1, "1 dag"), (0, "0 dager")])
def test_dager_igjen_tekst_har_riktig_entall_flertall(igjen, tekst):
    verdier = maltekster._purring_verdier({"m": {"ansvarlig_navn": "P", "kursnavn": "K", "beskrivelse": "B", "frist": "2027-01-01"},
                                           "igjen": igjen})
    assert verdier["dager_igjen_tekst"] == tekst
    assert verdier["dager_igjen"] == igjen                                 # UENDRET semantikk: fortsatt raatallet, ikke tekst


@pytest.mark.parametrize("igjen,emne_forventet", [
    (7, "Påminnelse: Kurspresentasjon til Veiledning i praksis – frist om 7 dager"),
    (2, "Påminnelse: Kurspresentasjon til Veiledning i praksis – frist om 2 dager"),
])
def test_normal_purring_sier_dager_flertall_via_ekte_daglig_flyt(con, ut, igjen, emne_forventet):
    kid = _kurs(con)
    _krav(con, kid, IDAG + timedelta(days=igjen))
    _daglig(con, IDAG)
    emne, _ = _mail(ut)
    assert emne == emne_forventet


def test_dager_igjen_kan_fortsatt_brukes_som_raatall_i_en_override(con, ut):
    """Bakoverkompatibilitet: en eksisterende override som bruker {dager_igjen} skal fortsatt faa raatallet, ikke tekst."""
    _lagre(con, "emne_frist_om_dager", "{dager_igjen} dager til frist for {beskrivelse}")
    kid = _kurs(con)
    _krav(con, kid, IDAG + timedelta(days=7))
    _daglig(con, IDAG)
    emne, _ = _mail(ut)
    assert emne == "7 dager til frist for Kurspresentasjon"


def test_retry_paa_dag_1_sier_frist_om_1_dag_ikke_1_dager(con, ut):
    """Den konkrete regresjonen fra forrige rapport: retry av purring-2 fra igjen==2 til igjen==1 skal si
    "frist om 1 dag", ikke det grammatisk gale "frist om 1 dager"."""
    _korrupt(con, "innledning", "Hei {ukjent}")
    kid = _kurs(con)
    _krav(con, kid, IDAG + timedelta(days=2))
    _daglig(con, IDAG)                                                     # dag X: igjen=2, MalFeil, 0 sendt
    assert ut["epost"] == [] and _claims(con) == []
    con.execute("DELETE FROM mal_tekst")
    con.commit()
    _daglig(con, IDAG + timedelta(days=1))                                 # dag X+1: igjen=1, rettet -> sendes
    emne, _ = _mail(ut)
    assert emne == "Påminnelse: Kurspresentasjon til Veiledning i praksis – frist om 1 dag"
    assert "frist om 1 dager" not in emne
    assert _claims(con) == [("purring-2", "sendt")]
    _daglig(con, IDAG + timedelta(days=1))                                 # samme dag igjen: ingen duplikat
    assert len([e for e in ut["epost"] if e[1] == emne]) == 1


# ============================ D/E: override ============================

def test_d_override_kun_emne_gir_nytt_emne_og_standard_body(con, ut):
    _lagre(con, "emne_frist_om_dager", "Frist naerer seg: {beskrivelse} til {kursnavn} ({dager_igjen} dager)")
    kid = _kurs(con)
    _krav(con, kid, IDAG + timedelta(days=7))
    _daglig(con, IDAG)
    emne, html = _mail(ut)
    assert emne == "Frist naerer seg: Kurspresentasjon til Veiledning i praksis (7 dager)"
    assert "Vi minner om at <strong>kurspresentasjon</strong>" in html


def test_e_override_innledning_og_avslutning_beholder_laast_opplastingslenke(con, ut):
    _lagre(con, "innledning", "Hei {navn}!" + chr(10) + chr(10) + "{beskrivelse} til {kursnavn} har frist {frist} ({dager_igjen} dager igjen).")
    _lagre(con, "avslutning", "Ta kontakt om noe er uklart, {navn}.")
    kid = _kurs(con)
    _krav(con, kid, IDAG + timedelta(days=2))
    _daglig(con, IDAG)
    _, html = _mail(ut)
    assert "<p>Hei Per Psykolog!</p>" in html
    assert "Kurspresentasjon til <strong>Veiledning i praksis</strong> har frist" in html
    assert "Vi minner om at" not in html
    assert "Last opp her" in html                                          # laast blokk uendret
    assert "Ta kontakt om noe er uklart, Per Psykolog." in html
    assert html.index("Last opp her") < html.index("Ta kontakt om noe er uklart")   # avslutning kommer ETTER laast blokk


# ============================ F/G/H/I: ugyldig override ============================

def _korrupt_ugyldig_kode(con):
    _korrupt(con, "innledning", "Hei {ukjent}")


def _korrupt_uferdig_klamme(con):
    _korrupt(con, "innledning", "Hei {navn")


def _korrupt_ukjent_felt(con):
    _korrupt(con, "finnes_ikke", "Noe")


def _db_lesefeil(con):
    con.execute("DROP TABLE mal_tekst")
    con.commit()


ALLE_SKADER = [(_korrupt_ugyldig_kode, UKJENT_KODE), (_korrupt_uferdig_klamme, "uferdig_klamme"),
              (_korrupt_ukjent_felt, UKJENT_FELT), (_db_lesefeil, DB_LESEFEIL)]


@pytest.mark.parametrize("skade,grunn", ALLE_SKADER)
def test_fg_hi_ugyldig_override_gir_malfeil_ikke_fallback(con, skade, grunn):
    skade(con)
    with pytest.raises(MalFeil) as e:
        maltekster.effektiv_tekst(con, "purring", "innledning")
    assert e.value.grunn == grunn


# ============================ J/K/L: ingen fallback, ingen claim, ingen mail ============================

@pytest.mark.parametrize("skade,grunn", ALLE_SKADER)
def test_jkl_korrupt_mal_gir_ingen_mail_ingen_claim_trygg_logg(con, ut, monkeypatch, skade, grunn):
    def forbudt(*a, **kw):
        raise AssertionError("claim ble forsokt selv om malen er ugyldig")
    monkeypatch.setattr(db, "reserver_sending", forbudt)
    monkeypatch.setattr(db, "reserver_sending_pa_nytt", forbudt)
    skade(con)
    kid = _kurs(con)
    krav_id = _krav(con, kid, IDAG + timedelta(days=7))
    _daglig(con, IDAG)                                                     # kaster IKKE - se test M
    assert ut["epost"] == [] and _claims(con) == []
    (h,) = _logg(con)
    assert set(h) == {"kurs_id", "materiell_id", "mal", "felt", "grunn"}
    assert h["kurs_id"] == kid and h["materiell_id"] == krav_id and h["mal"] == "purring" and h["grunn"] == grunn


# ============================ M: MalFeil stopper ikke senere daglig-steg ============================

def _gammelt_kurs_med_sensitivt(con):
    kid = db.opprett_kurs(con, kode="GAMMEL", navn="Gammelt kurs", datoer=["2026-01-10"], sharepoint_mappe="Kurs/G",
                          type="fysisk", sted="Oslo")
    pid, _ = db.meld_paa(con, kid, epost="gammel@x.no", navn="Gammel",
                        sensitivt={"allergier": "nøtter", "tilrettelegging": "rullestol"})
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0] == 1
    return pid


def test_m_malfeil_i_purring_stopper_ikke_kursbevis_eller_personvern(con, ut):
    _korrupt_ugyldig_kode(con)
    _gammelt_kurs_med_sensitivt(con)
    kid = _kurs(con)
    _krav(con, kid, IDAG + timedelta(days=7))
    daglig.kjor(Kjoring(con, idag=IDAG))                                   # HELE daglig.kjor - kaster IKKE
    assert not [m for m in ut["epost"] if m[0] == "per@x.no"]              # ingen purring-mail (urelatert bekreftelse ok)
    assert con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0] == 0   # steg 7 (personvern) kjorte likevel
    assert len(_logg(con)) == 1


# ============================ N: en korrupt kandidat stopper ikke andre gyldige ============================

def test_n_malfeil_i_en_kandidat_stopper_ikke_andre_gyldige_purringer(con, ut):
    _korrupt_ugyldig_kode(con)
    kid1 = _kurs(con, kode="P1")
    _krav(con, kid1, IDAG + timedelta(days=7), epost_="ugyldig@x.no")
    con.execute("DELETE FROM mal_tekst")                                   # rett FOER andre kurs opprettes... nei: vi vil ha KORRUPT for begge
    con.commit()
    _korrupt_ugyldig_kode(con)
    kid2 = _kurs(con, kode="P2")
    _krav(con, kid2, IDAG + timedelta(days=7), navn="Andre Ansvarlig", epost_="andre@x.no")
    daglig.kjor(Kjoring(con, idag=IDAG))
    assert ut["epost"] == []                                               # BEGGE feiler (samme globale mal)
    assert len(_logg(con)) == 2                                            # men EN hendelse per kandidat, ikke abort ved forste


def test_n2_korrupt_mal_paavirker_ikke_gyldig_eskalering_for_annet_krav(con, ut):
    _korrupt_ugyldig_kode(con)
    kid = _kurs(con, kode="P1")
    _krav(con, kid, IDAG + timedelta(days=7))                              # korrupt purring-kandidat
    kid2 = _kurs(con, kode="P2")
    _krav(con, kid2, IDAG - timedelta(days=1), epost_="annen@x.no")        # overtid -> eskalering (LAAST mal, uendret)
    daglig.kjor(Kjoring(con, idag=IDAG))
    eskalering = [m for m in ut["epost"] if m[0] == config.ADMIN_EPOST]
    assert len(eskalering) == 1 and "Mangler materiell" in eskalering[0][1]
    assert len(_logg(con)) == 1


# ============================ O: retry etter retting - DEN KRITISKE REGRESJONSVAKTEN ============================

@pytest.mark.parametrize("dager_til_frist,type_", [(2, "purring-2"), (0, "purring-0"), (7, "purring-7")])
def test_o_retry_etter_retting_dagen_etter_naar_frist_ikke_er_passert(con, ut, dager_til_frist, type_):
    """Kjernen i 12B2C-5-rettelsen: en purring som IKKE fikk sendt paa sin eksakte dag (MalFeil) skal fortsatt
    kunne sendes NESTE dag saa lenge fristen ikke er passert - unntatt naar selve fristdagen (0) var sist mulige dag."""
    _korrupt_ugyldig_kode(con)
    kid = _kurs(con)
    frist = IDAG + timedelta(days=dager_til_frist)
    _krav(con, kid, frist)
    _daglig(con, IDAG)                                                     # dag X: MalFeil, 0 sendt
    assert ut["epost"] == [] and _claims(con) == []
    con.execute("DELETE FROM mal_tekst")
    con.commit()
    if dager_til_frist == 0:
        # kjent, akseptert begrensning (samme kategori som dagfor): frist-dagen er siste mulige dag for TRINN 0.
        # Dagen etter er materiellet overtid, og kursholderen faar IKKE purring-0 - kun admin faar (uendret) eskalering.
        _daglig(con, IDAG + timedelta(days=1))
        assert not [m for m in ut["epost"] if m[0] == "per@x.no"]
        assert [m[0] for m in ut["epost"]] == [config.ADMIN_EPOST]
        return
    _daglig(con, IDAG + timedelta(days=1))                                 # dag X+1: rettet -> sendes NA
    emne, _ = _mail(ut)
    assert emne
    assert _claims(con) == [(type_, "sendt")]
    _daglig(con, IDAG + timedelta(days=1))                                 # samme dag igjen: ingen duplikat
    assert len([e for e in ut["epost"] if e[1] == emne]) == 1
    assert _claims(con) == [(type_, "sendt")]


def test_o2_purring_2_gapet_som_utloeste_rettelsen_er_lukket(con, ut):
    """Direkte regresjon for det konkrete hullet: purring-7 lykkes normalt, purring-2 feiler paa sin eksakte
    dag (igjen==2) og maa kunne tas igjen paa igjen==1 - selv om purring-7 for lengst er markert sendt."""
    kid = _kurs(con)
    frist = IDAG + timedelta(days=9)
    _krav(con, kid, frist)
    _daglig(con, frist - timedelta(days=7))                                # purring-7 sendes normalt
    assert _claims(con) == [("purring-7", "sendt")]
    ut["epost"].clear()
    _korrupt_ugyldig_kode(con)
    _daglig(con, frist - timedelta(days=2))                                # purring-2 feiler paa sin eksakte dag
    assert ut["epost"] == [] and _claims(con) == [("purring-7", "sendt")]
    con.execute("DELETE FROM mal_tekst")
    con.commit()
    _daglig(con, frist - timedelta(days=1))                                # dagen etter: rettet -> purring-2 tas igjen
    assert len(ut["epost"]) == 1
    assert sorted(_claims(con)) == [("purring-2", "sendt"), ("purring-7", "sendt")]


# ============================ P: Graph/ukjent ============================

def test_p_graph_feil_gir_ukjent_ingen_automatisk_duplikat(con, monkeypatch):
    forsok = []

    def feiler(*a, **kw):
        forsok.append(1)
        raise RuntimeError("Graph nede")
    monkeypatch.setattr(epost, "send", feiler)
    kid = _kurs(con)
    _krav(con, kid, IDAG + timedelta(days=7))
    with pytest.raises(RuntimeError):
        _daglig(con, IDAG)
    assert _claims(con) == [("purring-7", "ukjent")]
    _daglig(con, IDAG)                                                     # ny kjoring samme dag: ingen nytt forsok
    _daglig(con, IDAG + timedelta(days=1))
    assert forsok == [1]
    assert _logg(con) == []                                                # dette er IKKE en MalFeil


# ============================ Q: direkte epost.render uten DB ============================

def test_q_direkte_render_uten_db_bruker_standardtekst(con, monkeypatch):
    _korrupt_ugyldig_kode(con)                                             # ville feilet i en ekte utsending
    monkeypatch.setattr(db, "hent_overstyringer_for_mal", lambda *a, **k: (_ for _ in ()).throw(AssertionError("DB skal ikke leses")))
    m = {"ansvarlig_navn": "Per", "beskrivelse": "Ting", "kursnavn": "K", "frist": "2027-04-01", "id": 1}
    emne, html = epost.render("purring", m=m, igjen=5)
    assert "Hei Per," in html and emne == "Påminnelse: Ting til K – frist om 5 dager"


# ============================ R: override paavirker ikke andre maler ============================

def test_r_override_paavirker_ikke_andre_maler(con, ut):
    _lagre(con, "innledning", "PURRING-SPESIFIKK TEKST {navn}")
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="Ola")
    con.commit()
    from kurs import sveiper
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    bekreftelse = [m for m in ut["epost"] if m[0] == "a@x.no"]
    assert bekreftelse and "PURRING-SPESIFIKK" not in bekreftelse[0][2]
    _krav(con, kid, IDAG + timedelta(days=7))
    _daglig(con, IDAG)
    _, html = _mail(ut)
    assert "PURRING-SPESIFIKK TEKST Per Psykolog" in html


# ============================ S: PII-fri MalFeil-logg ============================

def test_s_pii_fri_malfeil_logg_over_alle_hendelser(con, ut):
    _korrupt_ugyldig_kode(con)
    kid = _kurs(con)
    _krav(con, kid, IDAG + timedelta(days=7), navn="Hemmelig Navnesen", epost_="hemmelig@x.no", beskrivelse="Hemmelig oppgave")
    _daglig(con, IDAG)
    for h in con.execute("SELECT detaljer FROM hendelse"):
        tekst = h["detaljer"] or ""
        for forbudt in ("Hemmelig Navnesen", "hemmelig@x.no", "Hemmelig oppgave", "Hei {"):
            assert forbudt not in tekst, (forbudt, tekst)
    (h,) = _logg(con)                                                      # grunn-koden ("ukjent_kode") er eksplisitt tillatt
    assert h["grunn"] == UKJENT_KODE
