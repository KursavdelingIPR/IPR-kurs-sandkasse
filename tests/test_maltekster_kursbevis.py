"""Fase 12B2C-3: `kursbevis_klar` migrert til maltekstsystemet - ALENE, pga. egen sideeffektrisiko
(kursbevisfil + dokumentrad kan bli opprettet FOR en senere malfeil).

Kritisk regel: en ugyldig `kursbevis_klar`-mal skal ALDRI foere til at systemet ender med kursbevisfil
og/eller dokumentrad uten at e-posten faktisk ble sendt, slik at kandidaten deretter ikke blir valgt
igjen. Preflight (Kjoring.render_for_sending) skjer derfor FOER filskriving/dokumentrad i kursbevis.kjor().
"""
import json
from datetime import date

import pytest

from kurs import config, db, kursbevis, maltekster
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from kurs.maltekster import DB_LESEFEIL, MALER, TOM, UKJENT_FELT, UKJENT_KODE, MalFeil
from navnehjelp import navnedeler

IDAG = date(2027, 6, 1)
STD_EMNE = "Kursbevis: Veiledning i praksis"


def _n(html: str) -> str:
    return " ".join(html.split())


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "ROT", tmp_path)
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def sendt(monkeypatch):
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: ut.append((til, emne, html)))
    return ut


def _kurs(con, kode="K1", datoer=("2027-03-01", "2027-03-02"), status="avsluttet", **kw):
    kid = db.opprett_kurs(con, kode=kode, navn=kw.pop("navn", "Veiledning i praksis"), datoer=list(datoer),
                          sharepoint_mappe=f"Kurs/{kode}", status=status,
                          **{"pris_nok": 0, "sted": "Oslo", "timer_pr_dag": 6, **kw})
    con.commit()
    return kid


def _deltaker(con, kid, navn="Ola Nordmann", epost_="ola@x.no"):
    status = con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kid,))
    pid, _ = db.meld_paa(con, kid, epost=epost_, **navnedeler(navn))
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    for dag in db.kursdager(con, kid):
        db.registrer_oppmote(con, pid, dag["id"], "manuell")
    con.commit()
    return pid


def _innhold(con, kode, deltaker_id):
    """Det lagrede kursbeviset (tabellen dokument_innhold), eller None hvis det ikke er utstedt."""
    rad = con.execute("""SELECT i.innhold FROM dokument_innhold i JOIN dokument d ON d.id=i.dokument_id
                         JOIN kurs k ON k.id=d.kurs_id WHERE d.type='kursbevis' AND k.kode=? AND d.deltaker_id=?""",
                      (kode, deltaker_id)).fetchone()
    return rad["innhold"] if rad else None


def _utsted(con):
    kursbevis.kjor(Kjoring(con, idag=IDAG))
    con.commit()


def _mine(sendt, adresse="ola@x.no"):
    return [m for m in sendt if m[0] == adresse]


def _lagre(con, felt, tekst):
    maltekster.lagre_maltekst(con, "kursbevis_klar", felt, tekst, aktor="admin:test")
    con.commit()


def _direkte_data(navn="Ola Nordmann", kursnavn="Veiledning i praksis"):
    return dict(navn=navn, fornavn=navnedeler(navn)["fornavn"], kurs={"navn": kursnavn})


# ============================ aktivering ============================

def test_kursbevis_klar_er_aktivert():
    assert "kursbevis_klar" in maltekster.AKTIVE_MALER
    assert maltekster._VERDIER["kursbevis_klar"] is maltekster._kursbevis_klar_verdier


def test_verdibygger_eksponerer_kun_navn_og_kursnavn():
    verdier = maltekster._kursbevis_klar_verdier({"navn": "Ola Nordmann", "fornavn": "Ola",
                                                  "kurs": {"navn": "K", "pris_nok": 1500}})
    assert verdier == {"fornavn": "Ola", "navn": "Ola Nordmann", "kursnavn": "K"}
    tillatt = set().union(*(f.kode for f in MALER["kursbevis_klar"].felt.values())) - maltekster.SYSTEMKODER
    assert tillatt == set(verdier)


# ============================ A-D: standard og override gjennom ekte flyt ============================

def test_A_uten_override_er_identisk_med_dagens_standardtekst(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    _utsted(con)
    (til, emne, html), = _mine(sendt)
    assert emne == STD_EMNE
    assert _n(html) == _n(epost.render("kursbevis_klar", **_direkte_data())[1])   # samme som direkte standard-render
    h = _n(html)
    assert "<p>Hei Ola,</p>" in h                                               # standardhilsenen: fornavn
    assert "Takk for deltakelsen på <strong>Veiledning i praksis</strong>. Kursbeviset ditt ligger nå på" in h
    assert f'<a href="{config.BASE_URL}/min-side">Mine kurs</a>' in h
    assert "Vennlig hilsen<br>Kursadministrasjonen, Institutt for Psykologisk Rådgivning<br>" in h


def test_B_override_kun_emne_gir_nytt_emne_og_standard_body(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    standard = _n(epost.render("kursbevis_klar", **_direkte_data())[1])
    _lagre(con, "emne", "Ditt kursbevis: {kursnavn}")
    _utsted(con)
    (til, emne, html), = _mine(sendt)
    assert emne == "Ditt kursbevis: Veiledning i praksis"
    assert _n(html) == standard


def test_C_override_kun_tekst_gir_standard_emne_og_ny_body(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    _lagre(con, "tekst", "Hei {navn}!" + chr(10) + chr(10) + "Kursbeviset for {kursnavn} ligger klart paa {min_side}.")
    _utsted(con)
    (til, emne, html), = _mine(sendt)
    assert emne == STD_EMNE
    h = _n(html)
    assert "<p>Hei Ola Nordmann!</p>" in h
    assert f'Kursbeviset for <strong>Veiledning i praksis</strong> ligger klart paa <a href="{config.BASE_URL}/min-side">Mine kurs</a>.' in h
    assert "Takk for deltakelsen" not in h
    assert "Vennlig hilsen" in h


def test_D_override_begge_felt_brukes(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    _lagre(con, "emne", "Bevis klart")
    _lagre(con, "tekst", "Ny tekst")
    _utsted(con)
    (til, emne, html), = _mine(sendt)
    assert emne == "Bevis klart" and "<p>Ny tekst</p>" in _n(html)


# ============================ sikkerhet og verdier ============================

def test_html_i_override_vises_escapet_og_er_aldri_aktiv(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    _lagre(con, "tekst", "Hei <script>alert(1)</script> <b>fet</b> <a href='http://ond.no'>klikk</a>")
    _lagre(con, "emne", "Emne <script>alert(1)</script>")
    _utsted(con)
    (til, emne, html), = _mine(sendt)
    assert "<script>" not in html and "<b>" not in html and "<a href='http://ond.no'>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert emne == "Emne <script>alert(1)</script>"


def test_min_side_er_systemets_lenke_aldri_fri_url_fra_admin(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    _lagre(con, "tekst", "Se {min_side}. Ond: http://ond.no/min-side og <a href='http://ond.no'>x</a>")
    _utsted(con)
    (til, emne, html), = _mine(sendt)
    assert f'<a href="{config.BASE_URL}/min-side">Mine kurs</a>' in html
    assert html.count("<a href") == 2                                     # rammens Mine kurs + {min_side} - ingen andre
    assert "http://ond.no/min-side" in html and 'href="http://ond.no' not in html
    assert "&lt;a href=&#39;http://ond.no&#39;&gt;x&lt;/a&gt;" in html


def test_placeholderverdi_med_spesialtegn_escapes_i_html_og_er_ren_tekst_i_emne(con, sendt):
    kid = _kurs(con, navn="Kurs A & B <x>")
    _deltaker(con, kid, navn="<i>Ola</i> & Co")
    _lagre(con, "emne", "Kursbevis: {kursnavn} for {navn}")
    _lagre(con, "tekst", "Hei {navn}, {kursnavn}")
    _utsted(con)
    (til, emne, html), = _mine(sendt)
    assert emne == "Kursbevis: Kurs A & B <x> for <i>Ola</i> & Co"
    h = _n(html)
    assert "<p>Hei &lt;i&gt;Ola&lt;/i&gt; &amp; Co, <strong>Kurs A &amp; B &lt;x&gt;</strong></p>" in h
    assert "<x>" not in html and "<i>" not in html


# ============================ direkte epost.render: standard, aldri DB ============================

def test_direkte_epost_render_gir_standard_selv_naar_db_har_override(con):
    _lagre(con, "emne", "OVERRIDE")
    emne, html = epost.render("kursbevis_klar", **_direkte_data())
    assert emne == STD_EMNE and "OVERRIDE" not in html


# ============================ KRITISK: MalFeil FOER varig sideeffekt (fil/dokumentrad/claim) ============================

def _korrupt_ugyldig_kode(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('kursbevis_klar', 'tekst', 'Hei {ukjent}')")
    con.commit()


def _korrupt_ukjent_felt(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('kursbevis_klar', 'finnes_ikke', 'Noe')")
    con.commit()


def _korrupt_tom_tekst(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('kursbevis_klar', 'tekst', '   ')")
    con.commit()


def _db_lesefeil(con):
    con.execute("DROP TABLE mal_tekst")
    con.commit()


ALLE_SKADER = [(_korrupt_ugyldig_kode, UKJENT_KODE, "tekst"), (_korrupt_ukjent_felt, UKJENT_FELT, None),
              (_korrupt_tom_tekst, TOM, "tekst"), (_db_lesefeil, DB_LESEFEIL, None)]


@pytest.mark.parametrize("skade,grunn,felt", ALLE_SKADER)
def test_korrupt_override_gir_ingen_fil_ingen_dokumentrad_ingen_claim_ingen_mail(con, sendt, monkeypatch, skade, grunn, felt):
    """Den kritiske regelen: MalFeil skal oppdages FOER kursbevisfil/dokumentrad, ikke etter."""
    kid = _kurs(con)
    pid = _deltaker(con, kid)
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    skade(con)

    def forbudt(*a, **kw):
        raise AssertionError("claim ble forsokt selv om kursbevis_klar-malen er ugyldig")
    monkeypatch.setattr(db, "reserver_sending", forbudt)
    monkeypatch.setattr(db, "reserver_sending_pa_nytt", forbudt)

    _utsted(con)   # MalFeil isoleres INNE i kursbevis.kjor() - skal IKKE unnslippe til kalleren (jf. daglig.py sitt moenster)

    assert sendt == []
    assert _innhold(con, "K1", did) is None                                                    # intet lagret bevis
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 0                      # ingen dokumentrad
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0                # 0 claims
    hendelser = [json.loads(r["detaljer"]) for r in con.execute(
        "SELECT detaljer FROM hendelse WHERE handling='kursbevis_mal_feil'")]
    assert hendelser == [{"kurs_id": kid, "paamelding_id": pid, "mal": "kursbevis_klar", "felt": felt, "grunn": grunn}]
    for forbudt_streng in ("epost", "ola@x.no", "Ola Nordmann", "Hei {"):                        # ingen PII/maltekst i loggen
        for h in con.execute("SELECT detaljer FROM hendelse"):
            assert forbudt_streng not in (h["detaljer"] or "")


@pytest.mark.parametrize("skade,grunn,_felt", ALLE_SKADER)
def test_korrupt_override_direkte_via_send_en_gang_gir_malfeil_foer_claim(con, sendt, monkeypatch, skade, grunn, _felt):
    """Samme kontrakt som de andre aktiverte malene: MalFeil skjer i render_for_sending, FOER claim."""
    skade(con)

    def forbudt(*a, **kw):
        raise AssertionError("claim ble forsokt selv om malteksten er ugyldig")
    monkeypatch.setattr(db, "reserver_sending", forbudt)
    monkeypatch.setattr(db, "reserver_sending_pa_nytt", forbudt)

    with pytest.raises(MalFeil) as e:
        Kjoring(con, idag=IDAG).send_en_gang("kurs:1", "ola@x.no", "kursbevis", "kursbevis_klar",
                                             navn="Ola Nordmann", fornavn="Ola", kurs={"navn": "K"})
    assert e.value.grunn == grunn and e.value.mal == "kursbevis_klar"
    assert sendt == [] and con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0


def test_rekkefolge_render_skjer_foer_fil_og_dokumentrad(con, sendt, monkeypatch):
    """sqlite3.Connection er en immutable C-type (kan ikke faa execute() spionert direkte) - observerer i stedet
    dokumentradantall + fileksistens FOER render_for_sending-kallet for aa bevise rekkefolgen."""
    kid = _kurs(con)
    pid = _deltaker(con, kid)
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    observert = []
    ekte_render = Kjoring.render_for_sending

    def spion(self, mal, **d):
        observert.append((self.con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0], _innhold(self.con, "K1", did) is not None))
        return ekte_render(self, mal, **d)

    monkeypatch.setattr(Kjoring, "render_for_sending", spion)
    _utsted(con)
    assert observert == [(0, False)]                        # render skjer FOER fil/dokumentrad - og KUN denne ene gangen


# ============================ KRITISK: NOEYAKTIG EN DB-aware rendering (TOCTOU-vakt) ============================

def test_eksakt_en_db_aware_rendering_per_kandidat(con, sendt, monkeypatch):
    """Regresjonsvakt for TOCTOU-hullet fra forrige runde: send_en_gang() (som selv rendrer) skal IKKE
    brukes til aa sende kursbeviset - kun send_ferdigrendret_en_gang() med det allerede rendrede resultatet.
    Feiler denne testen (mer enn 1 kall), er det fordi noen har koblet kursbevis.kjor() til send_en_gang()
    igjen, som rendrer paa nytt EFTER at fil/dokumentrad er opprettet."""
    kid = _kurs(con)
    _deltaker(con, kid)
    kall = []
    ekte = Kjoring.render_for_sending

    def spion(self, mal, **d):
        if mal == "kursbevis_klar":
            kall.append(1)
        return ekte(self, mal, **d)

    monkeypatch.setattr(Kjoring, "render_for_sending", spion)
    _utsted(con)
    assert len(kall) == 1
    assert len(_mine(sendt)) == 1


def test_send_ferdigrendret_en_gang_gjor_ingen_egen_rendering(con, sendt, monkeypatch):
    """send_ferdigrendret_en_gang() skal aldri kalle render_for_sending selv - kun bruke det den faar inn."""
    def forbudt(*a, **kw):
        raise AssertionError("send_ferdigrendret_en_gang skal ALDRI rendre selv")
    monkeypatch.setattr(Kjoring, "render_for_sending", forbudt)
    kid = _kurs(con)
    pid = _deltaker(con, kid)
    k = Kjoring(con, idag=IDAG)
    assert k.send_ferdigrendret_en_gang(f"kurs:{kid}", "ola@x.no", "kursbevis", "Emne", "<p>Html</p>", paamelding_id=pid)
    (til, emne, html), = sendt
    assert (til, emne, html) == ("ola@x.no", "Emne", "<p>Html</p>")
    kopi = con.execute("SELECT til, emne, html FROM sendt_epost WHERE paamelding_id=?", (pid,)).fetchone()
    assert tuple(kopi) == ("ola@x.no", "Emne", "<p>Html</p>")


def test_toctou_malendring_mellom_render_og_send_paavirker_ikke_allerede_rendret_resultat(con, sendt, monkeypatch):
    """Selve TOCTOU-regresjonsvakten: overrideren endres til noe KORRUPT rett ETTER at kursbevis_klar er
    rendret (tekst A), men FOER fil/dokument/send. Utsendingen skal likevel bruke A uendret - INGEN nytt
    DB-oppslag skal skje, og INGEN MalFeil skal oppstaa etter at rendringen er ferdig."""
    kid = _kurs(con)
    _deltaker(con, kid)
    _lagre(con, "tekst", "Tekst A for {navn}: {kursnavn} er ferdig, se {min_side}.")

    ekte = Kjoring.render_for_sending
    kall = []

    def spion(self, mal, **d):
        resultat = ekte(self, mal, **d)
        if mal == "kursbevis_klar":
            kall.append(resultat)
            # Simuler at admin lagrer en KORRUPT override RETT ETTER at render_for_sending returnerte -
            # men FOER kursbevis.kjor() naar frem til fil/dokument/send.
            self.con.execute("UPDATE mal_tekst SET tekst='Hei {ukjent}' WHERE mal='kursbevis_klar' AND felt='tekst'")
            self.con.commit()

            def sperre(*a, **kw):
                raise AssertionError("Nytt DB-avhengig maloppslag skjedde ETTER foerste rendering (TOCTOU)")
            monkeypatch.setattr(self, "render_for_sending", sperre)
        return resultat

    monkeypatch.setattr(Kjoring, "render_for_sending", spion)
    _utsted(con)

    assert len(kall) == 1                                                        # kun ETT DB-avhengig oppslag i det hele tatt
    (til, emne, html), = _mine(sendt)
    assert "Tekst A for Ola Nordmann" in _n(html)                                 # det FOERSTE (gyldige) resultatet ble brukt
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 1        # kursbeviset ble likevel utstedt
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kursbevis_mal_feil'").fetchone()[0] == 0


def test_preflight_har_ingen_sideeffekt(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    k = Kjoring(con, idag=IDAG)
    endringer = con.total_changes
    emne, html = k.render_for_sending("kursbevis_klar", navn="Ola Nordmann", fornavn="Ola",
                                      kurs={"navn": "Veiledning i praksis"})
    assert emne == STD_EMNE
    assert con.total_changes == endringer and not con.in_transaction and sendt == []
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 0


# ============================ retry etter retting ============================

@pytest.mark.parametrize("rett", ["slett_overstyring", "erstatt_med_gyldig"])
def test_retry_etter_retting_utsteder_kursbevis_uten_duplikat(con, sendt, rett):
    kid = _kurs(con)
    pid = _deltaker(con, kid)
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    _korrupt_ugyldig_kode(con)

    _utsted(con)                                                                # 1. forsok: MalFeil, ingen sideeffekt
    assert sendt == [] and con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 0
    assert _innhold(con, "K1", did) is None

    if rett == "slett_overstyring":
        con.execute("DELETE FROM mal_tekst")
    else:
        _lagre(con, "tekst", "Rettet tekst for {navn}: {kursnavn} er ferdig, se {min_side}.")
    con.commit()

    _utsted(con)                                                                # 2. nytt forsok: lykkes
    assert len(_mine(sendt)) == 1
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 1      # ett dokument, ikke to
    assert _innhold(con, "K1", did) is not None
    if rett == "erstatt_med_gyldig":
        assert "Rettet tekst for Ola Nordmann" in _n(_mine(sendt)[0][2])

    _utsted(con)                                                                # 3. kjores igjen: ingen duplikat
    assert len(_mine(sendt)) == 1
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kursbevis_mal_feil'").fetchone()[0] == 1  # kun fra forsok 1


def test_korrupt_rad_for_en_annen_mal_stopper_ikke_kursbevis_klar(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('venteliste', 'tekst', 'Hei {ukjent}')")
    con.commit()
    _utsted(con)
    assert len(_mine(sendt)) == 1                                               # kursbevis_klar leser kun sine egne rader


# ============================ Graph-feil: uendret fase-11-semantikk ============================

def test_graph_feil_gir_ukjent_status_dokument_finnes_men_ingen_automatisk_retry(con, sendt, monkeypatch):
    """Uendret fase-11-semantikk (rores ikke i denne migreringen): Kjoring.send_en_gang setter 'ukjent', committer,
    og kaster deretter feilen paa nytt (den klassifiseres aldri automatisk som "trygt feilet"). Kursbeviset er
    likevel reelt utstedt - det var kun VARSELET som fikk usikkert utfall, ikke selve kursbeviset."""
    kid = _kurs(con)
    pid = _deltaker(con, kid)
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    kall = []

    def boom(*a, **kw):
        kall.append(1)
        raise RuntimeError("Graph nede")
    monkeypatch.setattr(epost, "send", boom)

    with pytest.raises(RuntimeError):
        _utsted(con)                                          # Graph-feilen forplanter seg uendret - urort her
    assert len(kall) == 1
    assert con.execute("SELECT status FROM utsending_logg WHERE mottaker='ola@x.no'").fetchone()[0] == "ukjent"
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 1        # kursbeviset er reelt utstedt
    assert _innhold(con, "K1", did) is not None

    kursbevis.kjor(Kjoring(con, idag=IDAG))                                       # kjort paa nytt: ingen automatisk retry
    con.commit()
    assert len(kall) == 1
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 1        # ikke opprettet et nytt kursbevis
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 1  # ingen duplikatrad


# ============================ idempotens ============================

def test_dobbel_kjoring_med_override_gir_kun_ett_dokument_og_en_mail(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    _lagre(con, "tekst", "Egen tekst for {navn}")
    _utsted(con)
    _utsted(con)
    assert len(_mine(sendt)) == 1
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 1
    assert con.execute("SELECT status FROM utsending_logg WHERE mottaker='ola@x.no'").fetchone()[0] == "sendt"


def test_toer_modus_uendret_ingen_fil_ingen_dokument_ingen_mail_selv_med_korrupt_override(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    _korrupt_ugyldig_kode(con)
    kursbevis.kjor(Kjoring(con, idag=IDAG, tor=True))
    assert sendt == [] and con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 0
    assert not (config.ROT / "data").exists()
