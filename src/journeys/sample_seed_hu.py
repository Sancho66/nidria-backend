"""Hungarian overlay for the library samples (01/10): the last product
language the samples lacked (0/607 strings, against 607 for en/es/ru/pt/it).
Same shape as _SAMPLE_I18N_RUPTIT in sample_seed (keyed by the sample's FR
scalar name; per-step (name, content_note) pairs, PARALLEL to the spec by
position), kept in its own module so the generated literal stays out of the
hand-written seed. Merged by sample_seed._apply_sample_i18n."""

_SAMPLE_I18N_HU: dict[str, dict[str, object]] = {
    "Paraguay : Résidence temporaire + Cédula": {
        "name": {"hu": "Paraguay: Ideiglenes tartózkodás + Cédula"},
        "steps": [
            (
                {"hu": "A kérelem dokumentációjának összeállítása"},
                {
                    "hu": (
                        "Gyűjtse össze a dokumentumokat: apostille-jal ellátott születési "
                        "anyakönyvi kivonat, apostille-jal ellátott erkölcsi bizonyítvány, "
                        "érvényes útlevél. Az apostille-t a származási országa illetékes "
                        "hatóságától kell kérelmeznie."
                    )
                },
            ),
            (
                {"hu": "A dokumentumok hiteles fordítása"},
                {
                    "hu": (
                        "Fordítás nyilvántartásba vett hites fordító által. Az ügyhöz "
                        "rendelendő: a külső szolgáltatót az ügyben kell megnevezni, nem "
                        "ebben a megosztott sablonban."
                    )
                },
            ),
            (
                {"hu": "A kérelem benyújtása a bevándorlási hatósághoz (DNM)"},
                {
                    "hu": (
                        "A benyújtást az iroda végzi a Dirección Nacional de Migraciones "
                        "felé. DNM-illeték ≈ 2 700 000 Gs (tájékoztató jellegű, nem "
                        "rögzített összeg)."
                    )
                },
            ),
            (
                {"hu": "Az ideiglenes tartózkodási engedély megszerzése"},
                {"hu": "A DNM ügyintézési ideje változó (≈ 30-45 nap, tájékoztató jellegű)."},
            ),
            (
                {"hu": "A cédula igénylése (személyazonosító igazolvány)"},
                {
                    "hu": (
                        "Ujjlenyomatvétel és fényképkészítés az azonosítási hivatalban. A "
                        "lépés az ideiglenes tartózkodási engedély megszerzése után válik "
                        "elérhetővé."
                    )
                },
            ),
            (
                {"hu": "A cédula átvétele"},
                {"hu": "A cédula elkészítési ideje változó (≈ 3-9 hónap, tájékoztató jellegű)."},
            ),
        ],
    },
    "Paraguay : Création de société (RUC)": {
        "name": {"hu": "Paraguay: Cégalapítás (RUC)"},
        "steps": [
            (
                {"hu": "Az elektronikus azonosító és az alapszabály előkészítése"},
                {
                    "hu": (
                        "EAS: nincs minimális tőke, és letét sem kell. Ha a külföldinek "
                        "nincs paraguayi cédulája, olyan törvényes képviselőn keresztül "
                        "alapít, akinek van."
                    )
                },
            ),
            (
                {"hu": "Online alapítás a SUACE-on keresztül (eas.mic.gov.py)"},
                {
                    "hu": (
                        "Alapítás 72 óra alatt (gyakran 24-48 óra) proforma "
                        "alapszabállyal; ≈ 8 munkanap egyedi alapszabállyal. Ezt a lépést "
                        "az escribano (közjegyző) is elvégezheti: az ügyhöz rendelendő."
                    )
                },
            ),
            (
                {"hu": "Automatikus nyilvántartásba vételek (RUC / IPS / MTESS)"},
                {
                    "hu": (
                        "A bejegyzés automatikusan létrehozza a RUC-ot (pénzügy), az IPS-t "
                        "(társadalombiztosítás) és az MTESS-t (munkaügy). A működéshez "
                        "nincs szükség bejegyzésre a Registro Público de Comercio "
                        "nyilvántartásban."
                    )
                },
            ),
        ],
    },
    "Chypre : Enregistrement de résidence UE (Yellow Slip, MEU1)": {
        "name": {"hu": "Ciprus: Uniós tartózkodás regisztrációja (Yellow Slip, MEU1)"},
        "steps": [
            (
                {"hu": "A dokumentumok összegyűjtése (MEU1 nyomtatvány)"},
                {
                    "hu": (
                        "A kérelmet a belépést követő 4 hónapon belül kell benyújtani. A "
                        "fintech bankok (Revolut, Wise, N26) számlakivonatait "
                        "elutasíthatják."
                    )
                },
            ),
            (
                {"hu": "Időpontfoglalás a CRMD-nél (kerületi Immigration Unit)"},
                {
                    "hu": (
                        "Irodák: Nicosia / Limassol / Lárnaka / Páfosz. Foglaljon ≈ 3-4 "
                        "héttel előre."
                    )
                },
            ),
            (
                {"hu": "Személyes benyújtás + az igazolás kiadása"},
                {
                    "hu": (
                        "Személyes megjelenés szükséges (fénykép a helyszínen). Az "
                        "igazolást gyakran még aznap vagy néhány napon belül kiállítják; "
                        "nem jár le. Tájékoztató összeg (egy forrás 85 €-t említ, az "
                        "ügyfélablaknál megerősítendő)."
                    )
                },
            ),
        ],
    },
    "Paraguay : Résidence permanente (changement de catégorie)": {
        "name": {"hu": "Paraguay: Állandó tartózkodás (kategóriaváltás)"},
        "steps": [
            (
                {"hu": "A jogosultság és az időzítés ellenőrzése"},
                {
                    "hu": (
                        "A kérelmet a 2 éves ideiglenes carnet lejártát megelőző 90 napon "
                        "belül kell benyújtani (a lejárat után legfeljebb 1 hónapig is "
                        "lehetséges, bírság mellett). A 2 év alatt a távollétek összesen "
                        "nem haladhatják meg az egy évet. Az átváltáshoz nincs befektetési "
                        "követelmény."
                    )
                },
            ),
            (
                {"hu": "A kategóriaváltási dokumentáció összeállítása"},
                {
                    "hu": (
                        "A kategóriaváltáshoz szükséges dokumentumok összegyűjtése. A "
                        "fizetőképesség igazolása eltérő: munkaszerződés (munkavállalók) "
                        "vagy társasági okiratok + részvénykönyv (vállalkozók)."
                    )
                },
            ),
            (
                {"hu": "Benyújtás a DNM-nél"},
                {"hu": "Személyes benyújtás a Dirección Nacional de Migraciones hivatalánál."},
            ),
            (
                {"hu": "Az állandó carnet kiállítása + a cédula megújítása"},
                {
                    "hu": (
                        "Végleges állandó carnet, 10 évente megújítandó. Az állandó lakos "
                        "igazolás nélkül nem lehet távol 3 egymást követő évnél tovább. Az "
                        "átváltás ≈ 21-24 hónapnyi ideiglenes tartózkodás után lehetséges."
                    )
                },
            ),
        ],
    },
    "Chypre : Résidence hors-UE revenus passifs (Pink Slip + Catégorie F)": {
        "name": {
            "hu": ("Ciprus: EU-n kívüli tartózkodás passzív jövedelemből (Pink Slip + Category F)")
        },
        "steps": [
            (
                {"hu": "Előkészítés és legális beutazás Ciprusra"},
                {
                    "hu": (
                        "Külföldi jövedelem ≈ 24 000 €/év a Pink Sliphez (+20% házastárs, "
                        "+15%/gyermek). A kérelmet az érkezés után ≈ 7 nappal kell "
                        "benyújtani. A fintech bankok (Revolut/Wise/N26) számlakivonatait "
                        "olykor nem fogadják el. Tájékoztató összegek."
                    )
                },
            ),
            (
                {"hu": "Orvosi vizsgálat Cipruson"},
                {
                    "hu": (
                        "Hepatitis B/C-, HIV- és szifiliszteszt + tüdőröntgen "
                        "(tuberkulózis); az igazolás < 4 hónapos legyen. "
                        "Egészségbiztosítás szükséges."
                    )
                },
            ),
            (
                {"hu": "A Pink Slip benyújtása (éves tartózkodási engedély)"},
                {
                    "hu": (
                        "Az átvételi elismervény igazolja a jogszerű tartózkodást az "
                        "eljárás alatt. 1 évig érvényes, megújítható. Az ARC-szám végig "
                        "ugyanaz marad."
                    )
                },
            ),
            (
                {"hu": "A Category F benyújtása (állandó tartózkodás)"},
                {
                    "hu": (
                        "🟠 Jogszabályi küszöbértékek (2023-as adatok). Érdemes korán "
                        "benyújtani: az elbírálás nagyon hosszú."
                    )
                },
            ),
            (
                {"hu": "Várakozás és a Pink Slip éves megújítása (Category F ügyhátralék)"},
                {
                    "hu": (
                        "🔴 A Category F ügyhátraléka becslések szerint 5-7 év (2020-as "
                        "ügyek még folyamatban vannak). A Pink Slipet MINDEN évben meg "
                        "kell újítani a PR (állandó tartózkodás) kiadásáig. Soha ne "
                        "ígérjen gyors PR-t ezen az úton."
                    )
                },
            ),
            (
                {"hu": "A Category F kiadása (állandó tartózkodás)"},
                {"hu": "Állandó engedély, a kártyát 10 évente meg kell újítani."},
            ),
        ],
    },
    "Chypre : Digital Nomad Visa (hors-UE)": {
        "name": {"hu": "Ciprus: Digital Nomad Visa (EU-n kívüli)"},
        "steps": [
            (
                {"hu": "A kvóta elérhetőségének ellenőrzése MINDEN lépés ELŐTT"},
                {
                    "hu": (
                        "🔴 KRITIKUS. A hivatalos kvóta = 500 engedély, amely már 2023-ban "
                        "betelt; az „1 000” NEM megerősített. A tényleges elérhetőséget az "
                        "ügyfélnek tett bármilyen ígéret ELŐTT ellenőrizni kell a Deputy "
                        "Ministry of Migration-nél."
                    )
                },
            ),
            (
                {"hu": "A dokumentumok összegyűjtése és beutazás Ciprusra"},
                {
                    "hu": (
                        "A kérelmet a beutazást követő 3 hónapon belül kell benyújtani. "
                        "Tájékoztató jellegű összeg."
                    )
                },
            ),
            (
                {"hu": "Benyújtás a CRMD-nél (Nicosia) + biometria"},
                {"hu": "Ügyintézési idő ≈ 5-7 hét."},
            ),
            (
                {"hu": "A DNV-engedély kiadása"},
                {
                    "hu": (
                        "1 éves engedély, legfeljebb 2 évig megújítható. ⚠️ A DNV-vel "
                        "töltött idő NEM számít bele a honosításhoz szükséges időbe. Évi "
                        "183 napon túl = ciprusi adóügyi illetőség."
                    )
                },
            ),
        ],
    },
    "Chypre : Création de société (LTD)": {
        "name": {"hu": "Ciprus: Cégalapítás (LTD)"},
        "steps": [
            (
                {"hu": "Névjóváhagyás és KYC"},
                {"hu": "A cégalapításhoz ciprusi ügyvéd közreműködése kötelező."},
            ),
            (
                {"hu": "A létesítő okiratok megszövegezése (Memorandum & Articles of Association)"},
                {
                    "hu": (
                        "≥ 1 igazgató (egy rezidens igazgató segíti az adóügyi "
                        "szubsztancia igazolását), 1 titkár (≠ az egyedüli igazgató), "
                        "székhely Cipruson. Nincs minimális tőke (1 000 € a szokásos)."
                    )
                },
            ),
            (
                {"hu": "Benyújtás a Registrar of Companies-hoz és a tanúsítványok kiállítása"},
                {"hu": "Tanúsítványok: cégalapítás / igazgatók / részvényesek / székhely."},
            ),
            (
                {"hu": "Adóregisztráció és bankszámla"},
                {
                    "hu": (
                        "TIN 60 napon belül, áfa (ha alkalmazandó), UBO-nyilvántartás, "
                        "számlanyitás. Társasági adó: 15% (2026. január 1. óta); osztalék "
                        "≈ 2,65% effektív adóteher non-dom státuszban. Rendszeres "
                        "költségek ≈ 2 800-4 500 €/év (kötelező könyvvizsgálat). "
                        "Tájékoztató jellegű adatok."
                    )
                },
            ),
        ],
    },
    "Chypre : Société LTD + permis dirigeant hors-UE (FIC/BFU)": {
        "name": {"hu": "Ciprus: LTD társaság + EU-n kívüli vezető engedélye (FIC/BFU)"},
        "steps": [
            (
                {"hu": "Az LTD megalapítása"},
                {
                    "hu": (
                        "A részleteket (név, alapszabály, Registrar) lásd a „Cégalapítás "
                        "(LTD)” folyamatban. A társaság a FIC/BFU státusz előfeltétele."
                    )
                },
            ),
            (
                {"hu": "FIC/BFU nyilvántartásba vétel (Foreign Interest Company)"},
                {
                    "hu": (
                        "🟠 200 000 € letét, önálló irodahelyiség szükséges. A 70:30 arányú "
                        "helyi foglalkoztatást 2027. január 2-ától vizsgálják. Tájékoztató "
                        "küszöbértékek."
                    )
                },
            ),
            (
                {"hu": "A vezető tartózkodási és munkavállalási engedélyének kérelmezése"},
                {
                    "hu": (
                        "A BFU-n keresztül NINCS munkaerőpiaci teszt → engedély ≈ 1 hónap "
                        "alatt. A ≥ 2 500 €/hó fizetés a gyorsított honosításra való "
                        "jogosultságot is megnyitja (3 év görög B1 / 4 év A2 "
                        "nyelvtudással)."
                    )
                },
            ),
            (
                {"hu": "Az engedély kiadása és a tevékenység megkezdése"},
                {
                    "hu": (
                        "Megújítható engedély. Adózás: társasági adó 15%, osztalék ≈ "
                        "2,65% non-dom státusszal, 50%-os jövedelemadó-mentesség, ha a "
                        "fizetés > 55 000 €/év."
                    )
                },
            ),
        ],
    },
    "Panama : Résidence Nations Amies (Friendly Nations)": {
        "name": {"hu": "Panama: Baráti Nemzetek tartózkodási engedély (Friendly Nations)"},
        "steps": [
            (
                {
                    "hu": (
                        "A jogosultság (baráti állampolgárság) ellenőrzése és az iratanyag "
                        "előkészítése"
                    )
                },
                {
                    "hu": (
                        "🟠 A ~50 baráti ország listáját rendelettel módosíthatják: a "
                        "kérelem összeállítása előtt ellenőrizze újra a migracion.gob.pa "
                        "oldalon. Panamai ügyvéd kötelező."
                    )
                },
            ),
            (
                {"hu": "Beutazás Panamába és az ideiglenes tartózkodási kérelem benyújtása (SNM)"},
                {"hu": "Ideiglenes tartózkodási kártya (6 hónapra, az elbírálás idejére)."},
            ),
            (
                {"hu": "Az ideiglenes tartózkodás megadása (2 év)"},
                {
                    "hu": (
                        "🟠 2021 óta a Baráti Nemzetek program már nem ad azonnali állandó "
                        "tartózkodást: először 2 éves IDEIGLENES tartózkodás jár, "
                        "munkavállalási jog nélkül (a MITRADEL-engedély = külön eljárás)."
                    )
                },
            ),
            (
                {"hu": "Állandó tartózkodási kérelem (2 év után)"},
                {"hu": "Az elbírálás legfeljebb 6 hónap."},
            ),
            (
                {"hu": "Cédula E (Tribunal Electoral)"},
                {
                    "hu": (
                        "Állandó tartózkodásra jogosult személy személyazonosító "
                        "igazolványa. 10 évente megújítandó."
                    )
                },
            ),
        ],
    },
    "Panama : Visa Pensionado (retraité)": {
        "name": {"hu": "Panama: Pensionado vízum (nyugdíjas)"},
        "steps": [
            (
                {"hu": "Az iratanyag előkészítése (ügyvéden keresztül)"},
                {
                    "hu": (
                        "Nyugdíj ≥ 1 000 USD/hó (vagy ≥ 750 USD/hó ≥ 100 000 USD értékű "
                        "panamai ingatlannal EGYÜTT). Eltartottanként +250 USD/hó. Ebben a "
                        "státuszban tilos a munkavégzés. Tájékoztató küszöbértékek."
                    )
                },
            ),
            (
                {"hu": "A kérelem benyújtása az SNM-hez"},
                {
                    "hu": (
                        "A pensionado státuszú kérelmezők gyakran mentesülnek a "
                        "hazatelepítési letét alól."
                    )
                },
            ),
            (
                {"hu": "Az állandó tartózkodás megadása"},
                {
                    "hu": (
                        "KÖZVETLEN állandó tartózkodás (ideiglenes időszak nélkül). "
                        "Előnyök: pensionado kedvezménykártya (közlekedés, egészségügy, "
                        "szabadidő)."
                    )
                },
            ),
            (
                {"hu": "Cédula E (Tribunal Electoral)"},
                {"hu": "Állandó tartózkodásra jogosult személy személyazonosító igazolványa."},
            ),
        ],
    },
    "Panama : Investisseur Qualifié (Golden Visa)": {
        "name": {"hu": "Panama: Minősített befektető (Golden Visa)"},
        "steps": [
            (
                {"hu": "Előkészítés és a befektetési forma kiválasztása"},
                {
                    "hu": (
                        "🔴 KRITIKUS, VÁLTOZÉKONY KÜSZÖB. Ingatlan ≥ 300 000 USD egy 2026 "
                        "októberéig meghirdetett időablakban, utána valószínűleg 500 000 "
                        "USD-ra emelkedik. Alternatívák: panamai tőzsdén jegyzett "
                        "értékpapírok ≥ 500 000 USD, vagy lekötött betét ≥ 750 000 USD (5 "
                        "év). A hatályos küszöböt ELLENŐRIZNI kell a migracion.gob.pa "
                        "oldalon."
                    )
                },
            ),
            (
                {"hu": "A befektetés végrehajtása (átutalás külföldről)"},
                {"hu": "Külföldi eredetű pénzeszközök, banki csatornákon keresztül."},
            ),
            (
                {"hu": "A kérelem benyújtása az SNM-hez"},
                {"hu": "Az állandó tartózkodási engedély megadása 30-45 munkanap alatt."},
            ),
            (
                {"hu": "Cédula E (Tribunal Electoral)"},
                {
                    "hu": (
                        "⚠️ A Golden Visa a tartózkodási engedély megtartásához nem ír elő "
                        "jelenlétet, a honosításhoz viszont tényleges tartózkodás "
                        "szükséges: ezt az ügyvéddel kell mérlegelni."
                    )
                },
            ),
        ],
    },
    "Panama : Visa nomade numérique (Trabajador Remoto)": {
        "name": {"hu": "Panama: Digitális nomád vízum (Trabajador Remoto)"},
        "steps": [
            (
                {"hu": "A jogosultság ellenőrzése & a dokumentáció összegyűjtése"},
                {
                    "hu": (
                        "Külföldi forrásból származó jövedelem ≥ 36 000 USD/év. Tájékoztató küszöb."
                    )
                },
            ),
            (
                {
                    "hu": (
                        "Belépés Panamába & benyújtás a Ventanilla de Trámites Especiales "
                        "ügyfélablaknál (SNM)"
                    )
                },
                {"hu": "Benyújtás az SNM Ventanilla de Trámites Especiales ügyfélablakánál."},
            ),
            (
                {"hu": "A digitális nomád igazolvány (carné) kiadása"},
                {
                    "hu": (
                        "⚠️ 9 hónap, egyszer megújítható (max. 18 hónap). NEM rezidens "
                        "kategória: NEM vezet SEM tartózkodási joghoz, SEM honosításhoz. "
                        "Tartós letelepedéshez váltson a Baráti Nemzetek (Friendly "
                        "Nations) / Pensionado / Golden Visa útra."
                    )
                },
            ),
        ],
    },
    "Panama : Création de société (S.A. / SRL)": {
        "name": {"hu": "Panama: Cégalapítás (S.A. / SRL)"},
        "steps": [
            (
                {"hu": "A tevékenység minősítése és a társasági forma kiválasztása"},
                {
                    "hu": (
                        "🔴 KISKERESKEDELMI CSAPDA (293. cikk): a panamai fogyasztóknak "
                        "szóló kiskereskedelem (üzlet, helyi B2C e-kereskedelem, "
                        "forgalmazás, franchise) ZÁRVA van a külföldi részvényesek előtt "
                        "(még igazgatóként sem vehetnek részt benne). Nyitott: B2B, "
                        "tanácsadás, nagykereskedelem, import-export, SaaS/tech, holding, "
                        "külföldi ügyfelek. A tevékenységet a cégalapítás ELŐTT kell "
                        "minősíteni. S.A. = 1 tag, a tulajdonosok kiléte bizalmas; SRL = "
                        "legalább 2 tag, a tagok nyilvánosak."
                    )
                },
            ),
            (
                {"hu": "Az alapító okirat megszövegezése (ügyvéd)"},
                {
                    "hu": (
                        "S.A. = legalább 3 tagú igazgatótanács (a tagok lehetnek "
                        "külföldiek / nem rezidensek)."
                    )
                },
            ),
            (
                {"hu": "Bejegyzés a panamai Közhiteles Nyilvántartásba"},
                {"hu": "A társaság 3-7 nap alatt megalakul."},
            ),
            (
                {"hu": "Aviso de Operación és adóregisztráció (RUC / DGI)"},
                {
                    "hu": (
                        "Befektetett tőke < 10 000 USD → mentes az IAO alól; e felett IAO "
                        "= a nettó tőke 2%-a (min. 100 / max. 60 000 USD/év), kizárólag "
                        "panamai tevékenység esetén. CSS-regisztráció munkavállaló "
                        "felvétele esetén. Tájékoztató jellegű összegek."
                    )
                },
            ),
            (
                {"hu": "MITRADEL munkavállalási engedély (ha a vezető a társaságban dolgozik)"},
                {
                    "hu": (
                        "🟠 A tartózkodási engedélytől KÜLÖN eljárás. Kvóták: legfeljebb "
                        "10% általános / 15% szakképzett külföldi munkavállaló. A panamai "
                        "állampolgárok számára fenntartott ~56 szakma (orvoslás, jog, "
                        "mérnöki munka, könyvelés, építészet…) a honosításig tiltott "
                        "marad. Tulajdonlás/felügyelet külföldről = nincs szükség "
                        "engedélyre; munkavégzés a helyszínen = ez az engedély szükséges."
                    )
                },
            ),
        ],
    },
    "Bulgarie : Enregistrement de résidence UE": {
        "name": {"hu": "Bulgária: Uniós tartózkodás regisztrációja"},
        "steps": [
            (
                {"hu": "A lakcím bejelentése az önkormányzatnál"},
                {"hu": "A lakóhely címének bejelentése az önkormányzatnál."},
            ),
            (
                {"hu": "Tartózkodási igazolás igénylése (Direction Migration)"},
                {
                    "hu": (
                        "Az igazolás legfeljebb 5 évig érvényes, gyakran ≈ 3 munkanap "
                        "alatt kiállítják."
                    )
                },
            ),
            (
                {"hu": "A személyi szám (LNCh) megszerzése"},
                {
                    "hu": (
                        "🟠 Az uniós polgárok LNCh-t kapnak (nem EGN-t), ami adminisztratív "
                        "akadályokat okozhat (bank, közszolgáltatások). Szükséges a "
                        "bankhoz, az adóhatósághoz, a bérleti szerződéshez és az "
                        "egészségügyi ellátáshoz."
                    )
                },
            ),
        ],
    },
    "Bulgarie : Résidence retraité hors-UE": {
        "name": {"hu": "Bulgária: Tartózkodás nem uniós nyugdíjasoknak"},
        "steps": [
            (
                {"hu": "D vízum igénylése a bolgár konzulátuson"},
                {
                    "hu": (
                        "🔴 Megélhetési források ≥ minimálnyugdíj/minimálbér (2026-ban ≈ "
                        "620 €/hó, a minimálbérhez indexálva, az euró bevezetése után): "
                        "tájékoztató összeg, ellenőrizze újra a hivatalos forrásban. A "
                        "magánnyugdíjakat (pl. 401k) elutasíthatják, ha nincs hivatalos "
                        "állami nyugdíjigazolás. Vízumdíj ≈ 100 €."
                    )
                },
            ),
            (
                {"hu": "Belépés Bulgáriába & lakcímbejelentés (5 napon belül)"},
                {"hu": "Lakcímbejelentés a belépést követő 5 napon belül."},
            ),
            (
                {
                    "hu": (
                        "Hosszú távú tartózkodási engedély iránti kérelem benyújtása "
                        "(Direction Migration)"
                    )
                },
                {
                    "hu": (
                        "Az engedély legfeljebb 1 évig érvényes, megújítható. Nem biztosít "
                        "hozzáférést a munkaerőpiachoz."
                    )
                },
            ),
        ],
    },
    "Bulgarie : Visa nomade digital (hors-UE)": {
        "name": {"hu": "Bulgária: Digitális nomád vízum (EU-n kívüliek)"},
        "steps": [
            (
                {"hu": "A (friss) szabályozás ellenőrzése és az iratanyag összeállítása"},
                {
                    "hu": (
                        "🔴 NAGYON ÚJ SZABÁLYOZÁS: jogalap a ЗЧРБ 24p. cikke, a kérelmek "
                        "2025. 12. 20. óta nyújthatók be. Az alkalmazás részletei még "
                        "változnak: minden ígéret előtt ellenőrizze újra a konzulátuson / "
                        "a Direction Migrationnél."
                    )
                },
            ),
            (
                {"hu": "D vízum kérelmezése a konzulátuson"},
                {
                    "hu": (
                        "🔴 Küszöb ≈ 31 000 €/év, a minimálbérhez indexálva, az euró "
                        "bevezetése utáni érték: tájékoztató jellegű, ellenőrizze újra. "
                        "Tilos bolgár ügyfeleknek/munkáltatóknak dolgozni."
                    )
                },
            ),
            (
                {"hu": "Beutazás és tartózkodási engedély (Direction Migration, 14 napon belül)"},
                {
                    "hu": (
                        "1 éves engedély, 1 évvel megújítható (max. ≈ 2 év). NEM vezet "
                        "állandó tartózkodáshoz."
                    )
                },
            ),
        ],
    },
    "Bulgarie : Freelance / profession libérale (hors-UE)": {
        "name": {"hu": "Bulgária: Szabadúszó / szabadfoglalkozású tevékenység (EU-n kívülieknek)"},
        "steps": [
            (
                {
                    "hu": (
                        "A szabadúszó tevékenységi engedély megszerzése (Foglalkoztatási Ügynökség)"
                    )
                },
                {
                    "hu": (
                        "🟠 Az engedélyt a FOGLALKOZTATÁSI ÜGYNÖKSÉG (az MTSP alá tartozik) "
                        "adja ki, NEM a Direction Migration: gyakori elnevezési hiba. B1 "
                        "szintű bolgár nyelvtudás szükséges."
                    )
                },
            ),
            (
                {"hu": "D vízumkérelem a konzulátuson"},
                {"hu": "D vízumkérelem a szabadúszó engedély alapján."},
            ),
            (
                {"hu": "Tartózkodási engedély (Direction Migration)"},
                {
                    "hu": (
                        "12 hónapos, megújítható engedély. Nincs közzétett, rögzített "
                        "törvényi jövedelemküszöb (az üzleti terv alapján bírálják el). "
                        "Tájékoztató jellegű."
                    )
                },
            ),
        ],
    },
    "Bulgarie : Création de société (EOOD / OOD)": {
        "name": {"hu": "Bulgária: Cégalapítás (EOOD / OOD)"},
        "steps": [
            (
                {"hu": "A név ellenőrzése / lefoglalása & a cégforma kiválasztása"},
                {
                    "hu": (
                        "EOOD = 1 tag · OOD = ≥ 2 tag (közjegyző által hitelesített "
                        "alapító okirat + UBO-nyilatkozat). Minimális törzstőke ≈ 1 € (2 "
                        "BGN). A tagok száma e folyamat egyetlen állandó paramétere."
                    )
                },
            ),
            (
                {"hu": "A létesítő okirat elkészítése & a törzstőke befizetése"},
                {
                    "hu": (
                        "Bulgáriai székhely szükséges; ha az ügyvezető nem bulgáriai "
                        "lakos, helyi kapcsolattartó személy szükséges."
                    )
                },
            ),
            (
                {"hu": "Bejegyzés a Kereskedelmi Nyilvántartásba"},
                {
                    "hu": (
                        "Az EIK / BULSTAT (egyedi azonosító) megszerzése. 3-10 munkanap "
                        "(távolról 2-4 hét)."
                    )
                },
            ),
            (
                {"hu": "Áfa, bankszámla & a működés megkezdése"},
                {
                    "hu": (
                        "🔴 Társasági adó 10% (az EU-ban a legalacsonyabb), osztalék "
                        "5%: tájékoztató jellegű kulcsok, ellenőrizze újra (a 2026-os "
                        "euróbevezetés után). Áfakötelezettség, ha az árbevétel > ≈ 51 000 "
                        "€. Ismert szűk keresztmetszet: a bankszámlanyitás (KYC, néha "
                        "személyes megjelenés szükséges). ⚠️ Távolról birtokolni ≠ "
                        "letelepedni: a 10%-os társasági adó csak akkor érvényes, ha "
                        "a társaságot ténylegesen BULGÁRIÁBÓL irányítják (substance)."
                    )
                },
            ),
        ],
    },
    "Hongrie : Enregistrement de séjour UE": {
        "name": {"hu": "Magyarország: Uniós polgárok tartózkodásának regisztrációja"},
        "steps": [
            (
                {"hu": "Tartózkodás bejelentése (> 90 nap) az idegenrendészeti hatóságnál"},
                {
                    "hu": (
                        "🟠 Az „elegendő anyagi forrás” HUF-ban meghatározott összegét újra "
                        "ellenőrizni kell (elsődleges forrás nem erősítette meg). "
                        "Munkavállalás és letelepedés engedély nélkül megengedett."
                    )
                },
            ),
            (
                {"hu": "Regisztrációs igazolás (registration card)"},
                {"hu": "Állandó tartózkodás 5 év után, honosítás 8 év után kérelmezhető."},
            ),
            (
                {
                    "hu": (
                        "Letelepedéshez szükséges azonosítók (lakcímkártya, adóazonosító "
                        "jel, egészségügyi TAJ-szám)"
                    )
                },
                {
                    "hu": (
                        "lakcímkártya (lakcímet igazoló hatósági igazolvány) · "
                        "adóazonosító jel (NAV adóazonosító) · TAJ (NEAK "
                        "társadalombiztosítás). Gyakoriak a gyakorlati elakadások: már az "
                        "érkezéskor számoljon velük."
                    )
                },
            ),
        ],
    },
    "Hongrie : White Card (nomade digital, hors-UE)": {
        "name": {"hu": "Magyarország: White Card (digitális nomád, nem uniós állampolgároknak)"},
        "steps": [
            (
                {"hu": "Előzetes figyelmeztetés & a jövedelemküszöb ellenőrzése"},
                {
                    "hu": (
                        "🔴 AZ ADATLAP NINCS ELSŐDLEGES FORRÁSBÓL ELLENŐRIZVE. ⚠️ ZSÁKUTCA: "
                        "a White Card NEM számít bele SEM az állandó tartózkodásba, SEM a "
                        "honosításba: csak 1-2 éves kipróbálásra alkalmas megoldás. A tartós "
                        "letelepedéshez másik útra kell váltani. Tilos a magyar piacra "
                        "dolgozni. A minimális havi jövedelem 🔴 változékony, ellenőrizze "
                        "újra az oif.gov.hu oldalon."
                    )
                },
            ),
            (
                {"hu": "D vízum / White Card igénylése"},
                {"hu": "D vízum / White Card igénylése az összegyűjtött dokumentáció alapján."},
            ),
            (
                {"hu": "Tartózkodási engedély & azonosítók"},
                {
                    "hu": (
                        "Lakcímkártya + adóazonosító jel + TAJ-szám. A megújítás és a "
                        "családegyesítés feltételei 🔴 ellenőrizendők."
                    )
                },
            ),
        ],
    },
    "Hongrie : Guest Investor (golden visa, hors-UE)": {
        "name": {"hu": "Magyarország: Guest Investor (golden visa, nem uniós állampolgároknak)"},
        "steps": [
            (
                {"hu": "Figyelmeztetés & a befektetési forma kiválasztása"},
                {
                    "hu": (
                        "🔴 AZ ADATLAP NINCS ELSŐDLEGES FORRÁSBÓL ELLENŐRIZVE. Lehetőségek "
                        "(tájékoztató összegek, ellenőrizze újra): MNB által jóváhagyott "
                        "alapok ≈ 250 000 € (a legolcsóbb út); közvetlen "
                        "lakóingatlan-vásárlás ≈ 500 000 € (lehet, hogy ezt a lehetőséget "
                        "ELHALASZTOTTÁK: ellenőrizze, hogy valóban elérhető-e); felsőoktatási "
                        "adomány ≈ 1 000 000 €. Ellenőrizze, mely MNB-alapok jegyezhetők "
                        "ténylegesen."
                    )
                },
            ),
            (
                {"hu": "A befektetés megvalósítása"},
                {"hu": "A tőke befektetése a választott lehetőség szerint."},
            ),
            (
                {"hu": "A Guest Investor (vendégbefektetői) engedély igénylése"},
                {"hu": "10 éves engedély, csak csekély jelenlétet ír elő. Vagyonalapú út."},
            ),
            (
                {"hu": "Tartózkodási engedély & azonosítók"},
                {"hu": "Lakcímkártya + adóazonosító jel + TAJ-szám."},
            ),
        ],
    },
    "Hongrie : Autorisation unique (salarié hors-UE)": {
        "name": {"hu": "Magyarország: Összevont engedély (EU-n kívüli munkavállaló)"},
        "steps": [
            (
                {"hu": "A munkáltató indítja a kérelmet (single permit)"},
                {
                    "hu": (
                        "Tartózkodási engedély + munkavállalási engedély EGY eljárásban, a "
                        "munkáltató intézésében. 🟠 Esetleges munkaerőpiaci teszt + "
                        "bérküszöbök: ellenőrizendő. Ez az út BESZÁMÍT az állandó "
                        "tartózkodásba és a honosításba."
                    )
                },
            ),
            (
                {"hu": "D vízum kérelmezése a konzulátuson"},
                {"hu": "D vízum kérelmezése a konzulátuson a megszerzett engedély alapján."},
            ),
        ],
    },
    "Hongrie : Création de société (Kft.)": {
        "name": {"hu": "Magyarország: Cégalapítás (Kft.)"},
        "steps": [
            (
                {"hu": "Az alapítás (ügyvéd) és a tőke előkészítése"},
                {
                    "hu": (
                        "⚠️ CÉG ≠ TARTÓZKODÁS. Egy Kft. alapítása nem jár tartózkodási "
                        "engedéllyel: harmadik országbeli külföldi TÁVOLRÓL, engedély "
                        "nélkül is irányíthat Kft.-t; a fizikai tartózkodáshoz külön "
                        "engedély kell (amely a reform óta bizonytalan). Törzstőke ~3 M "
                        "HUF (~7 600 €, a befizetés halasztható, ha a létesítő okirat így "
                        "rendelkezik). Tájékoztató összegek, az aktuális árfolyamon "
                        "számolja át."
                    )
                },
            ),
            (
                {"hu": "Létesítő okirat és cégbejegyzés (Cégbíróság)"},
                {"hu": "Bejegyzés a Cégbíróságon (cégnyilvántartás)."},
            ),
            (
                {"hu": "Adószám, ÁFA és nyilvántartások"},
                {
                    "hu": (
                        "🟠 Társasági adó 9% (a legalacsonyabb az EU-ban), 0% "
                        "forrásadó a külföldre fizetett osztalékra: tájékoztató kulcsok, "
                        "ellenőrizze újra (NAV). Alanyi ÁFA-mentesség ~18 M HUF/év (~45 "
                        "000 €) alatt, egyébként 27%. Helyi adó (HIPA). Magas bérköltség "
                        "esetén KIVA választható."
                    )
                },
            ),
            (
                {"hu": "Üzleti bankszámla"},
                {
                    "hu": (
                        "🟠 SZŰK KERESZTMETSZET: számlanyitás külföldi ügyvezető/UBO "
                        "részére, gyakran személyes jelenlét szükséges, ez a leglassabb "
                        "tétel."
                    )
                },
            ),
        ],
    },
    "Dubaï (EAU) : Golden Visa (10 ans)": {
        "name": {"hu": "Dubaj (EAE): Golden Visa (10 év)"},
        "steps": [
            (
                {"hu": "A jogosultsági kategória ellenőrzése"},
                {
                    "hu": (
                        "🟠 Kategóriák (az AED-összegek változékonyak, újraellenőrizendő: "
                        "u.ae / icp.gov.ae): befektető ≥ 2 M AED (jóváhagyott alap vagy "
                        "ingatlan) · tehetségek: fizetés ≥ 30 000 AED/hó + diploma + MOHRE "
                        "szerinti 1./2. szintű besorolás · vállalkozó: projekt ≥ 500 000 "
                        "AED vagy inkubátori jóváhagyás · ingatlan ≥ 2 M AED."
                    )
                },
            ),
            (
                {"hu": "A jelölési dokumentáció összeállítása"},
                {
                    "hu": (
                        "A jelölési dokumentációt a választott jogosultsági kategória "
                        "szerint kell összeállítani."
                    )
                },
            ),
            (
                {"hu": "Orvosi vizsgálat és Emirates ID"},
                {
                    "hu": (
                        "Az orvosi vizsgálat + Emirates ID kötelező (minden "
                        "EAE-folyamatban közös elem). Személyes megjelenés szükséges "
                        "(biometria)."
                    )
                },
            ),
            (
                {"hu": "A 10 éves Golden Visa kiállítása"},
                {
                    "hu": (
                        "10 év, megújítható, önálló (nincs szükség szponzorra), mentes a 6 "
                        "hónapos távolléti szabály alól: a nagyon mobil profilokhoz "
                        "ideális. A családtagok szponzorálására is lehetőséget ad."
                    )
                },
            ),
        ],
    },
    "Dubaï (EAU) : Résidence par société free zone": {
        "name": {"hu": "Dubaj (EAE): Tartózkodás free zone cégen keresztül"},
        "steps": [
            (
                {"hu": "A free zone & a tevékenység kiválasztása, névfoglalás"},
                {
                    "hu": (
                        "Az EAE belső piacán kívüli tevékenység / nemzetközi B2B / holding "
                        "/ digitális. Helyi piaci értékesítéshez → mainland (külön "
                        "folyamat). Engedéllyel rendelkező szolgáltatón keresztül "
                        "történik, akit az ügyhöz kell rendelni."
                    )
                },
            ),
            (
                {"hu": "Licenc & establishment card"},
                {
                    "hu": (
                        "🟠 A vízumkvóta a csomagtól/irodától függ (~1 vízum/9 m², "
                        "hatóságonként eltérő). Költségek = kereskedelmi források, vesse "
                        "össze 2-3 szolgáltató ajánlatát."
                    )
                },
            ),
            (
                {"hu": "Entry permit → orvosi vizsgálat → Emirates ID"},
                {
                    "hu": (
                        "Az orvosi vizsgálat + az Emirates ID kötelező. Személyes "
                        "megjelenés szükséges."
                    )
                },
            ),
            (
                {"hu": "A tartózkodási vízum kiadása (2-3 év, megújítható)"},
                {
                    "hu": (
                        "🟠 A szponzor a CÉG: amíg működik, a vízum érvényes marad. Adózás: "
                        "lásd a cégalapítási folyamatot (a 0% QFZP NEM automatikus). Uniós "
                        "ügyfél: dokumentálja a származási országbeli adóilletőség "
                        "megszűnését (francia oldalon)."
                    )
                },
            ),
        ],
    },
    "Dubaï (EAU) : Visa immobilier (2 ans)": {
        "name": {"hu": "Dubaj (EAE): Ingatlanvízum (2 év)"},
        "steps": [
            (
                {"hu": "Az ingatlan megszerzése és minősítése"},
                {
                    "hu": (
                        "🟠 Ingatlan ≥ 750 000 AED (tájékoztató jellegű összeg, DLD). NEM "
                        "tévesztendő össze az ingatlanalapú Golden Visa programmal (≥ 2 M "
                        "AED / 10 év)."
                    )
                },
            ),
            (
                {"hu": "Ingatlanvízum-kérelem"},
                {"hu": "A kérelmet jóváhagyott szolgáltató nyújtja be, az ügyhöz rendelendő."},
            ),
            (
                {"hu": "Orvosi vizsgálat és Emirates ID"},
                {
                    "hu": (
                        "Az orvosi vizsgálat + Emirates ID kötelező. Személyes megjelenés "
                        "szükséges."
                    )
                },
            ),
            (
                {"hu": "A tartózkodási vízum beütése (2 év, megújítható)"},
                {
                    "hu": (
                        "🟠 A szponzor az INGATLAN: a tartózkodási jog addig áll fenn, amíg "
                        "az ingatlan a tulajdonában marad. 2 M AED felett a Golden Visa "
                        "előnyösebb (10 év + mentesség a távolléti szabály alól)."
                    )
                },
            ),
        ],
    },
    "Dubaï (EAU) : Visa remote work (1 an)": {
        "name": {"hu": "Dubaj (EAE): Remote work vízum (1 év)"},
        "steps": [
            (
                {"hu": "A jogosultság ellenőrzése és az iratanyag összeállítása"},
                {
                    "hu": (
                        "🟠 Tájékoztató küszöbérték. ⚠️ A remote work vízum NEM vezet "
                        "hosszú távú tartózkodáshoz (1 év): tartós bázishoz + "
                        "adóoptimalizáláshoz már az elején inkább free zone társaságot "
                        "válasszon. Ezt mondja el, mielőtt az ügyfél elköteleződne e "
                        "megoldás mellett."
                    )
                },
            ),
            (
                {"hu": "A remote work vízum kérelmezése"},
                {"hu": "A remote work vízum kérelmezése az összeállított iratanyag alapján."},
            ),
            (
                {"hu": "Orvosi vizsgálat és Emirates ID"},
                {"hu": "Orvosi vizsgálat + Emirates ID kötelező. Személyes jelenlét szükséges."},
            ),
            (
                {"hu": "A vízum kiadása (1 év)"},
                {"hu": "A remote work vízum kiadása, 1 évig érvényes."},
            ),
        ],
    },
    "Dubaï (EAU) : Visa retraité (5 ans, 55 ans et +)": {
        "name": {"hu": "Dubaj (EAE): Nyugdíjas vízum (5 év, 55 éves kortól)"},
        "steps": [
            (
                {"hu": "A pénzügyi feltétel ellenőrzése (egy is elegendő)"},
                {
                    "hu": (
                        "🟠 A három közül egy (tájékoztató jellegű összegek, "
                        "újraellenőrizendő): jövedelem ≥ 20 000 AED/hó · VAGY megtakarítás "
                        "≥ 1 M AED · VAGY ingatlan ≥ 1 M AED. Csak 55 éves kortól. 2 M AED "
                        "feletti vagyon esetén a Golden Visa előnyösebb (10 év + mentesség "
                        "a távolléti szabály alól)."
                    )
                },
            ),
            (
                {"hu": "A dokumentáció összeállítása"},
                {
                    "hu": (
                        "A kérelem dokumentációját a választott pénzügyi feltétel alapján "
                        "kell összeállítani."
                    )
                },
            ),
            (
                {"hu": "Orvosi vizsgálat és Emirates ID"},
                {
                    "hu": (
                        "Az orvosi vizsgálat + Emirates ID kötelező. Személyes megjelenés "
                        "szükséges."
                    )
                },
            ),
            (
                {"hu": "A nyugdíjas vízum beütése (5 év, megújítható)"},
                {"hu": "Beütött nyugdíjas vízum, 5 évig érvényes, megújítható."},
            ),
        ],
    },
    "Dubaï (EAU) : Création de société (free zone / mainland)": {
        "name": {"hu": "Dubaj (EAE): Cégalapítás (free zone / mainland)"},
        "steps": [
            (
                {"hu": "Döntés: free zone vagy mainland (szűrőkérdés)"},
                {
                    "hu": (
                        "SZŰRŐKÉRDÉS: az ügyfél közvetlenül az EAE belső piacán értékesít? "
                        "IGEN → mainland (DET): onshore piaci hozzáférés + közbeszerzések. "
                        "NEM (nemzetközi / B2B / holding / digitális / cél a tartózkodás) "
                        "→ free zone: 100%-os tulajdon + a vízum önszponzorálása. A "
                        "100%-os külföldi tulajdon ma már számos mainland tevékenységnél "
                        "megengedett (a stratégiai jelentőségű tevékenységek listája "
                        "szabályozott: egyeztessen a DET-tel)."
                    )
                },
            ),
            (
                {"hu": "Névfoglalás & a tevékenység jóváhagyása"},
                {
                    "hu": (
                        "Névfoglalás és a tevékenység jóváhagyása engedéllyel rendelkező "
                        "szolgáltatón keresztül, akit az ügyhöz kell rendelni."
                    )
                },
            ),
            (
                {"hu": "Licenc & létesítés"},
                {
                    "hu": (
                        "🔴 Költségek (free zone csomag / DET díjak / establishment card) = "
                        "többnyire kereskedelmi források → vesse össze 2-3 szolgáltató és "
                        "a hatóságok (DMCC, IFZA, Meydan, DET) adatait. Soha ne adjon "
                        "árajánlatot egyetlen marketingszám alapján."
                    )
                },
            ),
            (
                {"hu": "Adóregisztráció (corporate tax / áfa) & bankszámla"},
                {
                    "hu": (
                        "🟢 Corporate tax: 0% 375 000 AED nyereségig, felette 9%. 5% áfa "
                        "kötelező, ha az árbevétel > 375 000 AED (187 500-tól önkéntes). "
                        "Small Business Relief, ha az árbevétel < 3 M AED (a 2026. 12. "
                        "31-ig lezáruló üzleti évekig). ⚠️ A FREE ZONE 0% (QFZP) NEM "
                        "automatikus: valós gazdasági jelenlét (substance), B2B qualifying "
                        "income, a de minimis szabály betartása (min. 5 M AED / az "
                        "árbevétel 5%-a), transzferárazás és KÖNYVVIZSGÁLT PÉNZÜGYI "
                        "KIMUTATÁSOK szükségesek. A free zone-ban folytatott helyi B2C "
                        "kereskedelem erre általában nem jogosít. SOHA ne ígérje a 0%-ot "
                        "jóváhagyás nélkül."
                    )
                },
            ),
        ],
    },
    "Maurice : Occupation Permit Investor (entrepreneur)": {
        "name": {"hu": "Mauritius: Occupation Permit Investor (vállalkozó)"},
        "steps": [
            (
                {"hu": "A társaság megalapítása és a tőkehozzájárulás"},
                {
                    "hu": (
                        "🟠 Tőkehozzájárulás ≥ 50 000 USD + ≥ 4 M MUR árbevétel elvárt a 3. "
                        "évtől (tájékoztató jellegű küszöbök, az éves költségvetés "
                        "keretében, ~júniusban felülvizsgálják). A forma kiválasztása "
                        "(Domestic / GBC / Authorised): lásd a cégalapítási folyamatot. "
                        "Mauritiusi tanácsadó útján történik, az ügyhöz rendelendő."
                    )
                },
            ),
            (
                {"hu": "A kérelem benyújtása az EDB-hez"},
                {"hu": "Az Occupation Permit iránti kérelem benyújtása az EDB-hez."},
            ),
            (
                {"hu": "Az Occupation Permit megadása és regisztráció a PIO-nál"},
                {
                    "hu": (
                        "Az EDB bírálja el, a PIO adja ki. Egységes tartózkodási + "
                        "tevékenységi engedély. A családtagok ehhez kapcsolódó, "
                        "származtatott engedélyt kapnak. Biometrikus adatfelvétel "
                        "szükséges."
                    )
                },
            ),
        ],
    },
    "Maurice : Occupation Permit Professional (salarié)": {
        "name": {"hu": "Mauritius: Occupation Permit Professional (munkavállaló)"},
        "steps": [
            (
                {"hu": "Munkaszerződés és a fizetési küszöb ellenőrzése"},
                {
                    "hu": (
                        "🔴 Minimális fizetés 30 000 vs. 60 000 MUR/hó, időszaktól/ágazattól "
                        "függően: ez a LEGINKÁBB változó küszöb, feltétlenül újra kell "
                        "ellenőrizni. Az IKT/BPO-ágazatban a kivételek esetleg "
                        "alacsonyabbak (🟠)."
                    )
                },
            ),
            (
                {"hu": "A kérelem benyújtása az EDB-hez (a munkáltató nyújtja be)"},
                {"hu": "A kérelmet a munkáltató nyújtja be az EDB-hez."},
            ),
            (
                {"hu": "Az OP megadása és regisztráció a PIO-nál"},
                {
                    "hu": (
                        "Egységes tartózkodási + munkavállalási engedély (nincs külön "
                        "engedély), legfeljebb 10 évre. Biometrikus adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Maurice : Occupation Permit Self-Employed (consultant solo)": {
        "name": {"hu": "Mauritius: Occupation Permit Self-Employed (egyéni tanácsadó)"},
        "steps": [
            (
                {"hu": "A jogosultság & a befektetendő tőke ellenőrzése"},
                {
                    "hu": (
                        "🟠 Befektetendő tőke ≈ 35 000 USD + elvárt tevékenységi jövedelem "
                        "≈ 800 000 MUR (2./3. év). Tájékoztató küszöbök, az éves "
                        "költségvetésben (Budget) felülvizsgálják."
                    )
                },
            ),
            (
                {"hu": "A kérelem benyújtása az EDB-hez"},
                {"hu": "Az Occupation Permit iránti kérelem benyújtása az EDB-hez."},
            ),
            (
                {"hu": "Az OP megadása & regisztráció a PIO-nál"},
                {
                    "hu": (
                        "Egységes tartózkodási + tevékenységi engedély, legfeljebb 10 "
                        "évre. Biometrikus adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Maurice : Premium Visa (nomade / revenu passif étranger)": {
        "name": {"hu": "Mauritius: Premium Visa (nomád / külföldi passzív jövedelem)"},
        "steps": [
            (
                {"hu": "A jogosultság ellenőrzése (külföldi jövedelem)"},
                {
                    "hu": (
                        "🟠 Jövedelem ≥ 1 500 USD/hó (+ ~500 eltartottanként). A "
                        "meghirdetés szerint ingyenes. ⚠️ A helyi piacon tilos "
                        "tevékenykedni (kizárólag külföldi forrásból származó jövedelem). "
                        "Tájékoztató küszöbök, az éves költségvetés (Budget) szerint."
                    )
                },
            ),
            (
                {"hu": "Online kérelem (EDB)"},
                {"hu": "Premium Visa kérelem online benyújtása az EDB-hez."},
            ),
            (
                {"hu": "A Premium Visa megadása"},
                {
                    "hu": (
                        "1 év, megújítható. Nem biztosít hosszú távú tartózkodást: a "
                        "stabilitás érdekében fontolja meg a ≥ 375k USD "
                        "ingatlanbefektetést (külön folyamat)."
                    )
                },
            ),
        ],
    },
    "Maurice : Résidence par investissement immobilier (≥ 375k USD)": {
        "name": {"hu": "Mauritius: Tartózkodás ingatlanbefektetéssel (≥ 375k USD)"},
        "steps": [
            (
                {"hu": "Megfelelő ingatlan kiválasztása"},
                {
                    "hu": (
                        "🟠 A tartózkodási jogot megnyitó küszöb ≥ 375 000 USD "
                        "(IRS/RES/PDS/Smart City/jogosult G+2 konstrukciók). Ez alatt: a "
                        "vásárlás lehetséges, de automatikus tartózkodási jog NÉLKÜL. "
                        "Regisztrációs illeték ~5% (megerősítendő). Tanácsadón keresztül "
                        "történik, akit az ügyhöz kell rendelni."
                    )
                },
            ),
            (
                {"hu": "Vásárlás & bejegyzés"},
                {"hu": "Az ingatlan megvásárlása és bejegyzése."},
            ),
            (
                {"hu": "Tartózkodási kérelem (EDB) & PIO-regisztráció"},
                {
                    "hu": (
                        "Tartózkodási jog, amíg az ingatlan tulajdonban van (≥ 375k "
                        "ingatlan esetén legfeljebb 20 évre szóló engedély). Mauritiuson "
                        "nincs tőkenyereség-adó és öröklési illeték. Biometrikus "
                        "adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Maurice : Création de société (Domestic / GBC / Authorised)": {
        "name": {"hu": "Mauritius: Cégalapítás (Domestic / GBC / Authorised)"},
        "steps": [
            (
                {"hu": "A társasági forma kiválasztása (szűrőkérdés)"},
                {
                    "hu": (
                        "HELYI PIAC → Domestic Company (társasági adó 15%, ≥ 1 "
                        "helyben lakó igazgató). NEMZETKÖZI + szükség van a DTAA "
                        "egyezményekre → GBC (~3% effektív adó a 80%-os részleges "
                        "mentesség révén). NEMZETKÖZI, a DTAA-kra NINCS szükség → "
                        "Authorised Company (0% Mauritiuson, MRA-bevallás, nincs "
                        "hozzáférés a DTAA-khoz)."
                    )
                },
            ),
            (
                {"hu": "Megalakulás & bejegyzés (CBRD)"},
                {
                    "hu": (
                        "GBC → 2 helyben lakó igazgató + kötelező, FSC által engedélyezett "
                        "management company + éves könyvvizsgálat. Authorised → "
                        "engedéllyel rendelkező registered agent, irányítás/ellenőrzés "
                        "Mauritiuson kívül."
                    )
                },
            ),
            (
                {"hu": "FSC-licenc (GBC) / adóregisztráció & áfa"},
                {
                    "hu": (
                        "🟠 Általános áfakulcs 15% (a küszöb alatt halasztható). CCR Levy "
                        "2% egy árbevételi küszöb felett. Nincs tőkenyereség-adó és "
                        "öröklési illeték."
                    )
                },
            ),
            (
                {"hu": "Gazdasági jelenlét (substance) & irányítás (GBC esetén)"},
                {
                    "hu": (
                        "⚠️ SOHA ne hozzon létre GBC-t postafiókcégként: valós gazdasági "
                        "jelenlét nélkül (2 helyben lakó igazgató, helyi kiadások / CIGA, "
                        "irányítás Mauritiuson) a 80%-os mentesség (~3%) ELVÉSZ, és "
                        "fennáll az átminősítés kockázata."
                    )
                },
            ),
        ],
    },
    "Thaïlande : Destination Thailand Visa (DTV, nomade)": {
        "name": {"hu": "Thaiföld: Destination Thailand Visa (DTV, nomád)"},
        "steps": [
            (
                {"hu": "A jogosultság és a megtakarítás ellenőrzése"},
                {
                    "hu": (
                        "🟠 Megtakarítás ≥ 500 000 THB (tájékoztató jelleggel, ~36 "
                        "THB/USD). ⚠️ A DTV NEM ENGEDÉLYEZI a munkavégzést THAI "
                        "ügyfél/munkáltató részére. ⚠️ NEM vezet állandó tartózkodáshoz "
                        "(sem a DTV, sem a nyugdíjas vízum, sem a Privilege nem számít "
                        "bele: csak a Non-B + work permit vezet oda)."
                    )
                },
            ),
            (
                {"hu": "Kérelem az e-visa portálon (MFA)"},
                {
                    "hu": (
                        "🟠 A gyakorlat konzulátusonként eltér (néha több hónapos "
                        "bankszámlatörténetet kérnek). A 180 napos hosszabbítás díja ≈ 10 "
                        "000 THB (és NEM ~1 900, ez gyakori hiba a kereskedelmi "
                        "forrásokban)."
                    )
                },
            ),
            (
                {"hu": "A DTV kiadása"},
                {
                    "hu": (
                        "5 év, többszöri beutazásra jogosít, beutazásonként 180 nap "
                        "(egyszer meghosszabbítható)."
                    )
                },
            ),
        ],
    },
    "Thaïlande : Long-Term Resident (LTR, 10 ans)": {
        "name": {"hu": "Thaiföld: Long-Term Resident (LTR, 10 év)"},
        "steps": [
            (
                {"hu": "Az LTR-kategória meghatározása"},
                {
                    "hu": (
                        "🟠 4 kategória (tájékoztató küszöbök, ellenőrizze újra: "
                        "ltr.boi.go.th): Wealthy Global Citizen (jelentős vagyon + "
                        "befektetés) · Wealthy Pensioner (50+, passzív jövedelem ≥ 80 000 "
                        "USD/év, vagy 40-80k 250k USD befektetéssel) · Work-from-Thailand "
                        "Professional (jövedelem ≥ 80 000 USD/év + tőzsdén jegyzett vagy > "
                        "150 M USD árbevételű munkáltató; digitális work permitet ad) · "
                        "Highly-Skilled Professional (kiemelt ágazatok). A 2024-2025-ös "
                        "könnyítések megerősítésre szorulnak."
                    )
                },
            ),
            (
                {"hu": "Minősítési kérelem a BOI-nál"},
                {"hu": "Minősítési kérelem benyújtása a BOI-hoz."},
            ),
            (
                {"hu": "Az LTR-vízum kiadása & regisztráció"},
                {
                    "hu": (
                        "10 év, éves bejelentési kötelezettség (90 napos helyett). A "
                        "Work-from-Thailand digitális work permitet is tartalmaz."
                    )
                },
            ),
        ],
    },
    "Thaïlande : Visa retraité (O-A, 50 ans et +)": {
        "name": {"hu": "Thaiföld: Nyugdíjas vízum (O-A, 50 éves kortól)"},
        "steps": [
            (
                {"hu": "Az életkor és a pénzügyi feltétel ellenőrzése"},
                {
                    "hu": (
                        "🟠 ≥ 50 év + 800 000 THB betét VAGY 65 000 THB/hó jövedelem + "
                        "egészségbiztosítás (~3 M THB fedezet). Tájékoztató jellegű "
                        "küszöbök. MEGJEGYZÉS: az O-X (legfeljebb 10 évre) bizonyos "
                        "jogosult állampolgárságok számára elérhető (USA, Kanada, "
                        "Ausztrália, Egyesült Királyság, Japán…), 3 M THB küszöbbel: a "
                        "lista megerősítendő. ⚠️ Nem vezet állandó tartózkodási "
                        "engedélyhez (PR)."
                    )
                },
            ),
            (
                {"hu": "O-A vízumkérelem (konzulátus)"},
                {"hu": "Az O-A vízumkérelem benyújtása a konzulátuson."},
            ),
            (
                {"hu": "Kiállítás és regisztráció érkezéskor"},
                {"hu": "Lakcímbejelentés 90 naponta. Évente megújítható."},
            ),
        ],
    },
    "Thaïlande : Thailand Privilege (carte de séjour payante)": {
        "name": {"hu": "Thaiföld: Thailand Privilege (fizetős tartózkodási kártya)"},
        "steps": [
            (
                {"hu": "A tagsági szint kiválasztása"},
                {
                    "hu": (
                        "🟠 2026-os szintek (tájékoztató jellegű, thailandprivilege.co.th): "
                        "Bronze ~650k / Gold ~900k / Platinum ~1,5M / Diamond ~2,5M / "
                        "Reserve ~5M THB. ⚠️ NEM jogosít munkavállalásra. NEM vezet "
                        "állandó tartózkodási engedélyhez (PR)."
                    )
                },
            ),
            (
                {"hu": "Tagsági kérelem és fizetés"},
                {"hu": "Tagsági kérelem és a választott szint díjának megfizetése."},
            ),
            (
                {"hu": "A kártya és a Privilege vízum kiállítása"},
                {
                    "hu": (
                        "Hosszú távú tartózkodás a szinttől függően, a szolgáltatásokkal "
                        "együtt (repülőtéri fast-track, asszisztencia). Egyszerűsített "
                        "vízummegújítás."
                    )
                },
            ),
        ],
    },
    "Thaïlande : Non-B + Work Permit (salarié)": {
        "name": {"hu": "Thaiföld: Non-B + Work Permit (munkavállaló)"},
        "steps": [
            (
                {"hu": "A munkáltató ellenőrzi a tőkét és az arányt"},
                {
                    "hu": (
                        "🟠 Munkáltatói oldalon: 2 M THB tőke külföldi munkakörönként (1 M, "
                        "ha a munkavállaló thai állampolgárral házas) + arány: 4 thai "
                        "alkalmazott 1 külföldire. S-Curve ágazat + magas fizetés → SMART "
                        "Visa lehetséges (SMART-T ≥ 100 000 THB/hó, külön work permit "
                        "nélkül: figyelem, sok forrás még 200k-t említ)."
                    )
                },
            ),
            (
                {"hu": "Non-B vízum (konzulátus)"},
                {"hu": "Non-B vízumkérelem benyújtása a konzulátuson."},
            ),
            (
                {"hu": "Work Permit (Department of Employment) és nyilvántartásba vétel"},
                {
                    "hu": (
                        "90 napos bejelentkezési kötelezettség. 3 egymást követő év Non-B "
                        "+ work permit után → PR-kérelem (állandó tartózkodás) lehetséges "
                        "(kvóta ~100/állampolgárság/év). Csak ez az út vezet PR-hez."
                    )
                },
            ),
        ],
    },
    "Thaïlande : Création de société (FBA : 100 % / BOI / Amity / FBL)": {
        "name": {"hu": "Thaiföld: Cégalapítás (FBA: 100% / BOI / Amity / FBL)"},
        "steps": [
            (
                {"hu": "A tevékenység minősítése az FBA döntési fája alapján"},
                {
                    "hu": (
                        "DÖNTÉSI FA: (a) a 3 listán KÍVÜL eső tevékenység (ipar/gyártás) → "
                        "100% külföldi tulajdon, nincs szükség mentességre. (b) 3. listás "
                        "tevékenység (szolgáltatások, a tanácsadók esete) → mentesség "
                        "szükséges: amerikai állampolgár → US Treaty of Amity (100%, a "
                        "kizárt ágazatok kivételével); támogatható tevékenység → BOI (100% "
                        "+ társaságiadó-mentesség legfeljebb 8 évig, csúcstechnológia "
                        "esetén 13 évig + egyszerűsített munkavállalási engedélyek, "
                        "mentesség a 4:1 arány alól); egyébként → FBL (mérlegelésen "
                        "alapul, lassú, 3 M THB tőke) VAGY valódi thai partner ≥ 51%. (c) "
                        "1. lista = tiltott, 2. lista = a kormány (Cabinet) jóváhagyása "
                        "szükséges (ritka). 🔴 A „THAI STRÓMANN RÉSZVÉNYESEK” KONSTRUKCIÓ "
                        "ILLEGÁLIS (FBA 36. cikk: pénzbírság és szabadságvesztés is "
                        "lehetséges, a részesedés értékesítésének elrendelése). SOHA ne "
                        "javasolja."
                    )
                },
            ),
            (
                {"hu": "Cégalapítás és bejegyzés (DBD)"},
                {
                    "hu": (
                        "🟠 DBD-díjak ~5 000-6 000 THB. BOI-támogatás / FBL = további "
                        "eljárás az 1. lépésben választott úttól függően."
                    )
                },
            ),
            (
                {"hu": "Adóregisztráció és áfa"},
                {
                    "hu": (
                        "🟠 Kkv-k társasági adója (befizetett tőke ≤ 5 M THB ÉS árbevétel ≤ "
                        "30 M THB): 0% / 15% / 20%-os sávos adókulcs; egyébként egységesen "
                        "20%. A 7%-os áfa kötelező, ha az árbevétel > 1,8 M THB/év. "
                        "Tájékoztató jellegű kulcsok (rd.go.th)."
                    )
                },
            ),
            (
                {"hu": "A külföldi vezető vízuma/engedélye"},
                {
                    "hu": (
                        "BOI nélkül a saját cég vezetéséhez Non-B vízum + munkavállalási "
                        "engedély (work permit) szükséges (2 M THB tőke/munkakör + 4:1 "
                        "arány). BOI = egyszerűsített munkavállalási engedélyek, mentesség "
                        "az arány alól."
                    )
                },
            ),
        ],
    },
    "Indonésie : Remote Worker KITAS (E33G, nomade)": {
        "name": {"hu": "Indonézia: Remote Worker KITAS (E33G, nomád)"},
        "steps": [
            (
                {"hu": "A jogosultság ellenőrzése (külföldi jövedelem)"},
                {
                    "hu": (
                        "🟠 Külföldi jövedelem ~60 000 USD/év (tájékoztató jellegű, "
                        "evisa.imigrasi.go.id). ⚠️ KIZÁRÓLAG Indonézián KÍVÜLI "
                        "ügyfeleknek/munkáltatóknak lehet dolgozni. Nincs LTR-hez hasonló "
                        "magasabb szint: az E33G a digitális nomádok egyetlen útja."
                    )
                },
            ),
            (
                {"hu": "E-vízum igénylése (önszponzorálás jövedelem alapján)"},
                {"hu": "E-vízum igénylése, önszponzorálás a külföldi jövedelem alapján."},
            ),
            (
                {"hu": "A KITAS kiadása & regisztráció érkezéskor"},
                {
                    "hu": (
                        "~1 év, megújítható. Nem vezet KITAP-hoz. Biometrikus adatfelvétel "
                        "szükséges."
                    )
                },
            ),
        ],
    },
    "Indonésie : Second Home Visa (rentier)": {
        "name": {"hu": "Indonézia: Second Home Visa (járadékos)"},
        "steps": [
            (
                {"hu": "A letét / proof of funds ellenőrzése"},
                {
                    "hu": (
                        "🟠 Letét ~IDR 2 mrd (≈ 130 000 USD): az összeg forrásonként eltér, "
                        "ellenőrizze újra: evisa.imigrasi.go.id. Nincs életkori feltétel. "
                        "⚠️ Munkavállalási jog nincs."
                    )
                },
            ),
            (
                {"hu": "E-vízum-kérelem (önszponzorálás pénzeszközökkel)"},
                {"hu": "E-vízum-kérelem, önszponzorálás a letétbe helyezett pénzeszközökkel."},
            ),
            (
                {"hu": "A vízum kiadása (5 vagy 10 év)"},
                {
                    "hu": (
                        "5 vagy 10 év, a kérelemtől függően. Magasabb tőke + hosszú távú "
                        "horizont → hasonlítsa össze a Golden Visa lehetőségével."
                    )
                },
            ),
        ],
    },
    "Indonésie : Retirement KITAS (E33F, 55 ans et +)": {
        "name": {"hu": "Indonézia: Retirement KITAS (E33F, 55 éves kortól)"},
        "steps": [
            (
                {"hu": "Az életkor ellenőrzése & engedéllyel rendelkező szponzorügynök megbízása"},
                {
                    "hu": (
                        "🟠 ≥ 55 év + minimális nyugdíj + egészségbiztosítás. ENGEDÉLLYEL "
                        "RENDELKEZŐ SZPONZORÜGYNÖK KÖTELEZŐ (néha helyi munkavállaló "
                        "alkalmazását is előírják: a gyakorlat változó). ⚠️ Munkavállalási "
                        "jog nincs."
                    )
                },
            ),
            (
                {"hu": "KITAS-kérelem az ügynökön keresztül"},
                {"hu": "A KITAS-kérelmet az engedéllyel rendelkező szponzorügynök nyújtja be."},
            ),
            (
                {"hu": "A KITAS kiadása & regisztráció"},
                {
                    "hu": (
                        "1 év, megújítható, a KITAP felé továbbvezethet. ⚠️ "
                        "SZPONZORKOCKÁZAT: az engedély megszűnik, ha a szponzor (ügynök) "
                        "beszünteti tevékenységét: készítsen tartalék megoldást. "
                        "Biometrikus adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Indonésie : Work KITAS (E23, salarié)": {
        "name": {"hu": "Indonézia: Work KITAS (E23, munkavállaló)"},
        "steps": [
            (
                {
                    "hu": (
                        "A munkáltató megszerzi az RPTKA-t (külföldi munkavállalók "
                        "foglalkoztatási terve)"
                    )
                },
                {
                    "hu": (
                        "Szponzoráló munkáltató KÖTELEZŐ, külföldiek számára nyitott "
                        "munkakör. DKP-TKA ~100 USD/hó (~1 200/év), a munkáltatót terheli."
                    )
                },
            ),
            (
                {"hu": "Munkavállalási vízum és a KITAS kiadása"},
                {"hu": "Munkavállalási vízum, majd a KITAS kiadása."},
            ),
            (
                {"hu": "Nyilvántartásba vétel és munkavállalási engedély"},
                {
                    "hu": (
                        "6 hónaptól 2 évig, megújítható. 3-4 év folyamatos tartózkodás "
                        "után → KITAP lehetséges. ⚠️ SZPONZORKOCKÁZAT: a KITAS a szerződés "
                        "végén megszűnik, gondoskodjon tartalék megoldásról. Biometrikus "
                        "adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Indonésie : Investor KITAS (E28A) + PT PMA": {
        "name": {"hu": "Indonézia: Investor KITAS (E28A) + PT PMA"},
        "steps": [
            (
                {"hu": "A PT PMA megalapítása (előfeltétel)"},
                {
                    "hu": (
                        "A részleteket (KBLI, tőke) lásd a „PT PMA társaság” folyamatban. "
                        "A társaság szponzorálja vezetője vízumát. Közjegyző/tanácsadó "
                        "végzi, az ügyhöz rendelendő."
                    )
                },
            ),
            (
                {"hu": "A szerepkör és a részesedési küszöb ellenőrzése"},
                {
                    "hu": (
                        "🔴 Részesedés ~IDR 1 mrd (néha 1,125 mrd): főként ügynökségi "
                        "forrásból, ellenőrizze újra. AKTÍV IGAZGATÓ → dolgozhat (Investor "
                        "KITAS); PASSZÍV RÉSZVÉNYES → csak tulajdonlás, munkavállalási jog "
                        "nélkül."
                    )
                },
            ),
            (
                {"hu": "Investor KITAS kérelem (szponzor = PT PMA)"},
                {"hu": "Investor KITAS kérelem, amelyben a PT PMA jár el szponzorként."},
            ),
            (
                {"hu": "A KITAS kiadása és nyilvántartásba vétel"},
                {
                    "hu": (
                        "1-2 év, megújítható, továbbvezet a KITAP felé. ⚠️ "
                        "SZPONZORKOCKÁZAT: a PT PMA megszüntetése érvényteleníti a "
                        "KITAS-t. Biometrikus adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Indonésie : Création de société (PT PMA)": {
        "name": {"hu": "Indonézia: Cégalapítás (PT PMA)"},
        "steps": [
            (
                {"hu": "A KBLI azonosítása és a Positive Investment List ellenőrzése"},
                {
                    "hu": (
                        "A KBLI 2020 kód (5 számjegy) azonosítása. ZÁRT (~6 ágazat) → "
                        "nincs PT PMA. KORLÁTOZOTT → maximális külföldi részesedés (%) + "
                        "helyi partner. NYITOTT (a 2020-as Omnibus óta az esetek többsége) "
                        "→ 100% külföldi tulajdon. 🔴 A NOMINEE (indonéz strómann) "
                        "ILLEGÁLIS ÉS SEMMIS (UU 25/2007, 33. cikk): a megállapodás "
                        "semmis, a befektetés elveszhet, jogilag a strómann a tulajdonos. "
                        "SOHA ne javasolja (maximális kockázat a bali ingatlanoknál)."
                    )
                },
            ),
            (
                {"hu": "A tőke és az OSS-kockázati szint ellenőrzése"},
                {
                    "hu": (
                        "🟠 Befektetési terv > IDR 10 mrd (föld/épület nélkül) "
                        "KBLI-nként/helyszínenként + befizetett tőke ~IDR 10 mrd. ⚠️ A "
                        "régi, 2,5 mrd-os küszöböt (2021 előtti) NE HASZNÁLJA TOVÁBB: az "
                        "irodák gyakori hibája. OSS-kockázati szint: alacsony → elegendő a "
                        "NIB; magas → NIB + izin."
                    )
                },
            ),
            (
                {"hu": "Cégalapítás (közjegyző) és OSS-regisztráció (NIB)"},
                {"hu": "Cégalapítás közjegyző előtt és OSS-regisztráció (NIB)."},
            ),
            (
                {"hu": "Adóregisztráció és áfa"},
                {
                    "hu": (
                        "🟠 Általános társasági adókulcs: 22% (a 31E. cikk szerinti "
                        "kedvezménnyel ≈ 11% effektív, ha az árbevétel ≤ IDR 50 mrd). "
                        "Végleges kkv-adózás: az árbevétel 0,5%-a, ha az árbevétel ≤ IDR "
                        "4,8 mrd (PT esetén legfeljebb 3 évig). Az áfa kötelező, ha az "
                        "árbevétel > IDR 4,8 mrd (~11% effektív, ez a leginkább változó "
                        "pont). Tájékoztató jellegű kulcsok (pajak.go.id)."
                    )
                },
            ),
        ],
    },
    "Philippines : SRRV (résidence par dépôt, via PRA)": {
        "name": {"hu": "Fülöp-szigetek: SRRV (tartózkodás letét alapján, a PRA-n keresztül)"},
        "steps": [
            (
                {"hu": "A változat és a letét kiválasztása"},
                {
                    "hu": (
                        "🟠 Változatok (letétek USD-ben, tájékoztató jelleggel, "
                        "pra.gov.ph): Smile 20k (ingatlanra nem váltható át) · Classic "
                        "35-49 év 50k (condóra/bérleti jogra átváltható) · Classic 50+ "
                        "NYUGDÍJJAL, ha a nyugdíj ≥ 800 USD/hó (pároknak 1 000) 10k · "
                        "Classic 50+ nyugdíj nélkül 20k · Human Touch 10k (+1 500 USD/hó). "
                        "⚠️ TARTÓZKODÁS ≠ MUNKAVÉGZÉS: az SRRV NEM ad munkavállalási jogot "
                        "(ehhez külön a DOLE által kiadott AEP szükséges). MEGJEGYZÉS: a "
                        "Digital Nomad Visa (EO 86, 2025) papíron létezik, de NEM működik: "
                        "ne ajánlja, amíg a kiadását meg nem erősítik."
                    )
                },
            ),
            (
                {"hu": "Az iratanyag összeállítása és a letét átutalása"},
                {
                    "hu": (
                        "Az iratanyag összeállítása és a letét átutalása a PRA által "
                        "kijelölt számlára."
                    )
                },
            ),
            (
                {"hu": "Az SRRV megadása (PRA) és az ID-kártya"},
                {
                    "hu": (
                        "🟠 PRA-díjak ~1 400 USD + ~300/eltartott + ~360/év. A letét "
                        "condóra váltható át (földterületre nem: külföldiek nem "
                        "birtokolhatnak földet; condók esetében legfeljebb az épület "
                        "40%-áig)."
                    )
                },
            ),
        ],
    },
    "Philippines : SIRV (visa investisseur, via BOI)": {
        "name": {"hu": "Fülöp-szigetek: SIRV (befektetői vízum, a BOI-n keresztül)"},
        "steps": [
            (
                {"hu": "A jogosult befektetés ellenőrzése"},
                {
                    "hu": (
                        "🟠 ~75 000 USD befektetése és fenntartása (egy egyszerű "
                        "ingatlanvásárlás általában nem jogosít: a jogosult eszközöket a "
                        "BOI határozza meg). ⚠️ TARTÓZKODÁS ≠ MUNKAVÉGZÉS: befektetői, nem "
                        "munkavállalói státusz; a saját cég munkavállalóként történő "
                        "vezetéséhez 9(g) + AEP szükséges."
                    )
                },
            ),
            (
                {"hu": "A befektetés végrehajtása és a kérelem benyújtása (BOI)"},
                {"hu": "A befektetés végrehajtása és a kérelem benyújtása a BOI-hoz."},
            ),
            (
                {"hu": "A SIRV megadása (BI, a BOI jóváhagyása alapján) és ID-kártya"},
                {"hu": "Tartózkodási jog mindaddig, amíg a befektetés fennmarad."},
            ),
        ],
    },
    "Philippines : Visa 13(a) (conjoint de ressortissant·e philippin·e)": {
        "name": {"hu": "Fülöp-szigetek: 13(a) vízum (fülöp-szigeteki állampolgár házastársa)"},
        "steps": [
            (
                {"hu": "A viszonosság és a házasság ellenőrzése"},
                {
                    "hu": (
                        "🟠 A 13(a) VISZONOSSÁGHOZ kötött: azon országok állampolgárai "
                        "számára nyitott, amelyek egyenértékű jogot biztosítanak a "
                        "fülöp-szigeteki állampolgároknak (a legtöbb nyugati ország "
                        "biztosítja: állampolgárságonként ellenőrizendő). Érvényes "
                        "házasság szükséges fülöp-szigeteki állampolgárral."
                    )
                },
            ),
            (
                {"hu": "A kérelem benyújtása (BI): 1 éves próbaidős státusz"},
                {"hu": "A kérelem benyújtása a BI-nál; egyéves próbaidős státusz."},
            ),
            (
                {"hu": "Átváltás állandó lakos státuszra (1 év próbaidő után)"},
                {
                    "hu": (
                        "Állandó lakos, munkavégzéshez mentesül az AEP alól "
                        "(megerősítendő). ACR I-Card + Annual Report."
                    )
                },
            ),
        ],
    },
    "Philippines : Création de société (60/40 / FINL / export / DME)": {
        "name": {"hu": "Fülöp-szigetek: Cégalapítás (60/40 / FINL / export / DME)"},
        "steps": [
            (
                {"hu": "A tevékenység (FINL) & a piaci modell minősítése"},
                {
                    "hu": (
                        "DÖNTÉSI FA: (a) a tevékenység a FINL A listáján szerepel "
                        "(földtulajdon, természeti erőforrások, public utilities, média, "
                        "egyes szakmák) → 60/40 VALÓS többségi fülöp-szigeteki partnerrel. "
                        "(b) export ≥ 60% → 100% külföldi tulajdon, mentesül a 200k USD-s "
                        "küszöb alól (tőke ~5 000 PHP; a 60%-os exportarányt fenn kell "
                        "tartani). (c) belső piac, külföldi többség → DME, tőke 200 000 "
                        "USD (100 000-re csökkenthető fejlett technológia / támogatott "
                        "(endorsed) startup / ≥ 50 fülöp-szigeteki munkavállaló esetén). 🔴 "
                        "ANTI-DUMMY LAW (CA 108): a látszólagos 60/40 (fülöp-szigeteki "
                        "strómann, rejtett voting trust, részvényfedezetű kölcsönök) "
                        "JOGELLENES: büntetőjogi szankciók a külföldire ÉS a strómannra. A "
                        "60/40-nek VALÓS fülöp-szigeteki gazdasági irányítást kell "
                        "tükröznie. SOHA ne javasolja."
                    )
                },
            ),
            (
                {"hu": "Megalakulás & SEC-bejegyzés"},
                {"hu": "Megalakulás és bejegyzés a SEC-nél."},
            ),
            (
                {"hu": "Adóregisztráció (BIR) & áfa"},
                {
                    "hu": (
                        "🟠 A CIT általános kulcsa 25% (20%, ha az adóköteles jövedelem ≤ 5 "
                        "M PHP ÉS az eszközök a telek nélkül ≤ 100 M PHP). 12% áfa, ha az "
                        "árbevétel > 3 M PHP (egyébként 3% percentage tax). Tájékoztató "
                        "jellegű kulcsok (bir.gov.ph)."
                    )
                },
            ),
            (
                {"hu": "(Opcionális) BOI/PEZA kedvezmények"},
                {
                    "hu": (
                        "Jogosult tevékenység esetén (SIPP): ITH 4-7 évig, majd 5% SCIT "
                        "vagy Enhanced Deductions. Összekapcsolható a vezető SIRV/9(g) "
                        "vízumával."
                    )
                },
            ),
        ],
    },
    "Portugal : Enregistrement de résidence UE (CRUE)": {
        "name": {"hu": "Portugália: Uniós tartózkodás regisztrációja (CRUE)"},
        "steps": [
            (
                {"hu": "A NIF (adószám) megszerzése"},
                {
                    "hu": (
                        "A NIF szükséges a bérleti szerződéshez, a bankszámlához és az "
                        "ügyintézéshez. NISS (társadalombiztosítás) a tevékenységtől "
                        "függően."
                    )
                },
            ),
            (
                {"hu": "CRUE-kérelem a polgármesteri hivatalban (Câmara Municipal)"},
                {
                    "hu": (
                        "Az igazolást gyakran még aznap kiállítják. A polgármesteri "
                        "hivatal adja ki, NEM az AIMA → nem érinti az ügyhátralék. Személyes "
                        "megjelenés szükséges."
                    )
                },
            ),
            (
                {"hu": "Állandó tartózkodás (5 év után)"},
                {
                    "hu": (
                        "⚠️ A honosítás jelenleg 5 év után kérhető, de a folyamatban lévő "
                        "2025-ös reform meghosszabbíthatja (7/10 év): szabályozási "
                        "kockázat, nem garantált."
                    )
                },
            ),
        ],
    },
    "Portugal : Visa D7 (revenu passif / retraité, hors-UE)": {
        "name": {"hu": "Portugália: D7 vízum (passzív jövedelem / nyugdíjas, EU-n kívüli)"},
        "steps": [
            (
                {"hu": "NIF + portugál bankszámla"},
                {"hu": "EU-n kívüli, nem rezidens személy esetén adóképviselő szükséges."},
            ),
            (
                {"hu": "D7 vízumkérelem a konzulátuson"},
                {
                    "hu": (
                        "🟠 A küszöb az SMN-hez igazodik (~870 €/hó 2025-ben, "
                        "megerősítendő; az SMN-t évente 14 alkalommal fizetik, a ×12/×14 "
                        "kétértelműséget tisztázni kell). ⚠️ D7 = kizárólag PASSZÍV "
                        "jövedelem (az aktív távmunka a D8 hatálya alá tartozik)."
                    )
                },
            ),
            (
                {"hu": "Átváltás tartózkodási engedélyre az AIMA-nál"},
                {
                    "hu": (
                        "🔴 Az AIMA tényleges ügyintézési ideje (hatalmas ügyhátralék): "
                        "hónapoktól > 1 évig, nem garantált. 2 időtávban kell bemutatni "
                        "(konzuli vs. tényleges AIMA). ⚠️ Az NHR megszűnt: egy átlagos "
                        "nyugdíjas/járadékos számára nincs személyi adómentesség. "
                        "Biometrikus adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Portugal : Visa D8 (nomade digital, hors-UE)": {
        "name": {"hu": "Portugália: D8 vízum (digitális nomád, EU-n kívüli)"},
        "steps": [
            (
                {"hu": "NIF + portugál bankszámla"},
                {"hu": "EU-n kívüli, nem rezidens személy esetén adóképviselő szükséges."},
            ),
            (
                {"hu": "D8 vízumkérelem a konzulátuson"},
                {
                    "hu": (
                        "🟠 Küszöb ~4× SMN (~3 480 €/hó 2025-ben, megerősítendő). ⚠️ D8 = "
                        "AKTÍV külföldi jövedelem (a passzív jövedelem a D7 hatálya alá "
                        "tartozik). 2 változat: ideiglenes tartózkodás (~1 év) VAGY "
                        "tartózkodási vízum (beleszámít az 5 évbe). Letelepedési terv "
                        "esetén a tartózkodási változatot kell választani."
                    )
                },
            ),
            (
                {"hu": "Átváltás tartózkodási engedélyre az AIMA-nál"},
                {
                    "hu": (
                        "🔴 Az AIMA tényleges ügyintézési ideje (ügyhátralék): hónapoktól > "
                        "1 évig, nem garantált. Biometrikus adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Portugal : Golden Visa / ARI (investisseur passif, post-2023)": {
        "name": {"hu": "Portugália: Golden Visa / ARI (passzív befektető, 2023 után)"},
        "steps": [
            (
                {"hu": "A befektetési forma kiválasztása (2023 után)"},
                {
                    "hu": (
                        "🟠 JELENLEGI lehetőségek (tájékoztató összegek): minősített alapok "
                        "≥ 500 000 € · 10 munkahely létrehozása · K+F ≥ 500 000 € · "
                        "kulturális támogatás ≥ 250 000 € · vállalati tőkeemelés ≥ 500 000 "
                        "€. ⚠️ Az INGATLANVÁSÁRLÁST és az egyszerű tőkeátutalást 2023-ban "
                        "KIVEZETTÉK (Mais Habitação törvény): minden ingatlanvásárlást "
                        "említő tájékoztató (280k/350k/500k) TÉVES."
                    )
                },
            ),
            (
                {"hu": "A befektetés megvalósítása + NIF"},
                {"hu": "A választott befektetés megvalósítása és a NIF megszerzése."},
            ),
            (
                {"hu": "ARI-kérelem az AIMA-nál"},
                {
                    "hu": (
                        "🔴 Díjak ~5 300 € + ~600 €. Az AIMA tényleges ügyintézési ideje "
                        "(ügyhátralék) nem garantált. Minimális jelenlét ~7 nap/év. Az ARI-val "
                        "töltött idő beszámít az állandó tartózkodásba/állampolgárságba (a "
                        "2025-ös állampolgársági reform függvényében)."
                    )
                },
            ),
        ],
    },
    "Vietnam : Work Permit + TRC (salarié)": {
        "name": {"hu": "Vietnám: Work Permit + TRC (munkavállaló)"},
        "steps": [
            (
                {"hu": "A munkáltató megszerzi a külföldi munkaerőigény jóváhagyását"},
                {
                    "hu": (
                        "🔴 A work permitet kiadó hatóság a 2025-ös közigazgatási "
                        "átszervezés óta BIZONYTALAN (DOLISA → Belügyminisztérium?): "
                        "tartományonként ellenőrizendő. Kvóta + szakképzettség (~3 év "
                        "tapasztalat a „szakértő” kategóriához). Engedélymentesség (LD1), "
                        "ha a befektetett tőke ≥ ~3 Mrd VND."
                    )
                },
            ),
            (
                {"hu": "Work Permit + LD2 vízum (vagy LD1 mentesség esetén)"},
                {"hu": "A Work Permit és az LD2 vízum kiadása (mentesség esetén LD1)."},
            ),
            (
                {"hu": "Ideiglenes tartózkodási kártya (TRC)"},
                {
                    "hu": (
                        "TRC legfeljebb 2 évre, a munkáltatóhoz kötve. 3 év folyamatos TRC "
                        "+ szponzor után → PRC lehetséges (ritka, mérlegelésen alapul). ⚠️ "
                        "„Nyugdíjas” vagy „nomád” igényre Vietnámban nincs megfelelő út: "
                        "irányítsa át az ügyfelet (Thaiföld/Indonézia/Fülöp-szigetek). "
                        "Biometrikus adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Vietnam : Investor TRC (DT1-DT4)": {
        "name": {"hu": "Vietnám: Investor TRC (DT1-DT4)"},
        "steps": [
            (
                {"hu": "A társaság megalapítása (előfeltétel) és a tőke meghatározása"},
                {
                    "hu": (
                        "A részleteket lásd a „Társaság (LLC FDI)” folyamatban (IRC→ERC, "
                        "OMC, DICA). 🟠 TŐKE ↔ TRC: DT1 ≥ 100 mrd VND (~3,9 M USD) → 10 "
                        "éves TRC (+ PRC-út) · DT2 50-100 mrd → 5 év · DT3 3-50 mrd (~120k "
                        "USD) → 3 év (a TRC gyakorlati minimuma) · DT4 < 3 mrd → NINCS TRC "
                        "(vízum ≤ 12 hónap). A tőkét a tervezett tartózkodási időtávhoz "
                        "kell igazítani. Ügyvéd útján történik, az ügyhöz rendelendő."
                    )
                },
            ),
            (
                {"hu": "Befektetői vízum igénylése (DTx kategória)"},
                {"hu": "Befektetői vízum igénylése a meghatározott DTx kategóriának megfelelően."},
            ),
            (
                {"hu": "Ideiglenes tartózkodási kártya (TRC)"},
                {
                    "hu": (
                        "Időtartama a DTx kategóriától függ. A DT4 nem jogosít TRC-re. "
                        "Biometrikus adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Vietnam : TRC familiale (TT, conjoint de Vietnamien·ne)": {
        "name": {"hu": "Vietnám: Családi TRC (TT, vietnámi állampolgár házastársa)"},
        "steps": [
            (
                {"hu": "A házassági & szponzori dokumentumok összegyűjtése"},
                {
                    "hu": (
                        "A házassági iratok és a vietnámi szponzor személyazonossági "
                        "dokumentumainak összegyűjtése."
                    )
                },
            ),
            (
                {"hu": "TT vízum igénylése (a házastárs szponzorálásával)"},
                {"hu": "TT vízum igénylése a vietnámi házastárs szponzorálásával."},
            ),
            (
                {"hu": "Ideiglenes tartózkodási kártya (családi TRC)"},
                {
                    "hu": (
                        "TRC legfeljebb 3 évre. PRC 3 év folyamatos TRC után igényelhető "
                        "(vietnámi családtag szponzorral). Önmagában nem jogosít "
                        "munkavállalásra (alkalmazotti munkához külön work permit "
                        "szükséges). Biometrikus adatfelvétel szükséges."
                    )
                },
            ),
        ],
    },
    "Vietnam : Representative Office (bureau de représentation)": {
        "name": {"hu": "Vietnám: Representative Office (képviseleti iroda)"},
        "steps": [
            (
                {"hu": "Az anyavállalat jogosultságának ellenőrzése"},
                {
                    "hu": (
                        "Az anyavállalat ≥ 1 éve működik (07/2016. sz. rendelet). Az RO "
                        "NEM termelhet közvetlen kereskedelmi bevételt: kizárólag "
                        "kapcsolattartó/képviseleti funkciót lát el."
                    )
                },
            ),
            (
                {"hu": "RO-engedély kérelmezése"},
                {"hu": "A Representative Office engedélykérelmének benyújtása."},
            ),
            (
                {"hu": "Az engedély kiadása és nyilvántartásba vétel"},
                {
                    "hu": (
                        "5 éves, megújítható engedély. A külföldi irodavezető az RO-hoz "
                        "kötött vízumot/engedélyt kap. Bevételszerzéshez váltson FDI "
                        "LLC-re (külön folyamat)."
                    )
                },
            ),
        ],
    },
    "États-Unis : Visa E-2 (investisseur de traité)": {
        "name": {"hu": "Egyesült Államok: E-2 vízum (egyezményes befektető)"},
        "steps": [
            (
                {
                    "hu": (
                        "Az egyezmény szerinti jogosultság ellenőrzése & a befektetés strukturálása"
                    )
                },
                {
                    "hu": (
                        "🟠 Franciaország E-2 egyezményes ország. NINCS rögzített törvényi "
                        "küszöb: a befektetésnek a vállalkozás költségéhez mérten "
                        "„jelentősnek” és NEM MARGINÁLISNAK kell lennie (a gyakran "
                        "emlegetett ~100k USD tapasztalati érték, NEM szabály). Passzív "
                        "befektetés / spekulatív ingatlanbefektetés nem elfogadott. "
                        "Amerikai ügyvéd nélkülözhetetlen (díja ~8-20k+ USD)."
                    )
                },
            ),
            (
                {"hu": "Az amerikai vállalkozás létrehozása/megszerzése & a tőke lekötése"},
                {
                    "hu": (
                        "Lásd a „Cégalapítás (LLC / C-Corp)” folyamatot. A C-Corp "
                        "megkönnyíti a valós vállalkozás bizonyítását. A tőkét "
                        "visszavonhatatlanul le kell kötni („at risk”)."
                    )
                },
            ),
            (
                {"hu": "A kérelem benyújtása (amerikai konzulátus, DS-160 + DS-156E)"},
                {
                    "hu": (
                        "A kérelem benyújtása az amerikai konzulátuson (DS-160 + DS-156E "
                        "nyomtatványok)."
                    )
                },
            ),
            (
                {"hu": "Konzuli interjú & a vízum kiadása"},
                {
                    "hu": (
                        "🔴 MÉRLEGELÉSEN ALAPULÓ döntés (a befektetés "
                        "jelentőségét/marginalitását alaposan vizsgálják), sem az "
                        "eredmény, sem a határidő nem garantálható. Megújítható, amíg a "
                        "vállalkozás működik. A dual intent kényes kérdés (formálisan nem "
                        "elfogadott). Személyes megjelenés szükséges."
                    )
                },
            ),
        ],
    },
    "États-Unis : Visa L-1 (transfert intra-entreprise)": {
        "name": {"hu": "Egyesült Államok: L-1 vízum (vállalaton belüli áthelyezés)"},
        "steps": [
            (
                {
                    "hu": (
                        "A kapcsolt cégek közötti viszony & a munkaviszony időtartamának "
                        "ellenőrzése"
                    )
                },
                {
                    "hu": (
                        "Az elmúlt 3 évből 1 év folyamatos külföldi munkaviszony a "
                        "kapcsolt vállalkozásnál. Minősített kapcsolat "
                        "(anyavállalat/leányvállalat/társvállalat). L-1A vezető (≤ 7 év) / "
                        "L-1B speciális szaktudás (≤ 5 év, szigorúbban vizsgálják)."
                    )
                },
            ),
            (
                {"hu": "I-129 petíció az USCIS-hez (az amerikai munkáltató nyújtja be)"},
                {
                    "hu": (
                        "„New office L-1” lehetséges amerikai egység megnyitásához "
                        "(szigorúbb feltételek, felülvizsgálat 1 év után)."
                    )
                },
            ),
            (
                {"hu": "Vízum a konzulátuson & belépés"},
                {
                    "hu": (
                        "🔴 Mérlegelésen alapuló döntés (az L-1B-t különösen alaposan "
                        "vizsgálják). Lehetséges út az EB-1C green card felé "
                        "(multinacionális vállalat vezetője)."
                    )
                },
            ),
        ],
    },
    "États-Unis : Visa O-1 (capacités extraordinaires)": {
        "name": {"hu": "Egyesült Államok: O-1 vízum (rendkívüli képességek)"},
        "steps": [
            (
                {"hu": "A bizonyítékanyag értékelése"},
                {
                    "hu": (
                        "🟠 Jelentős, elismert díj VAGY legalább 3 jogszabályi kritérium "
                        "(publikációk, sajtó, kulcsszerep, magas javadalmazás, mások "
                        "munkájának szakmai bírálata…). A bizonyítékok minősége döntő. "
                        "Amerikai szponzor vagy agent szükséges."
                    )
                },
            ),
            (
                {"hu": "I-129 petíció + peer group konzultáció"},
                {"hu": "I-129 petíció egy peer group tanácsadó véleményével."},
            ),
            (
                {"hu": "Vízum a konzulátuson és beutazás"},
                {
                    "hu": (
                        "🔴 Mérlegelésen alapuló döntés (a bizonyítékok minősége). "
                        "Legfeljebb 3 év, megújítható. A dual intent kényes kérdés. A "
                        "profil gyakran átültethető EB-1A-ra (green card, önpetíció)."
                    )
                },
            ),
        ],
    },
    "États-Unis : Visa H-1B (specialty occupation)": {
        "name": {"hu": "Egyesült Államok: H-1B vízum (specialty occupation)"},
        "steps": [
            (
                {"hu": "Regisztráció a sorsolásra (munkáltató)"},
                {
                    "hu": (
                        "🔴 Éves kvóta 65 000 + 20 000 (amerikai mesterdiploma) → SORSOLÁS: "
                        "a kiválasztás NEM garantált. ⚠️ A 2025. 09. 19-i proklamáció 100 "
                        "000 USD-s díjat ír elő: hatálya/mentességei/bírósági státusza "
                        "BIZONYTALAN, ez az 1. számú ellenőrizendő pont. A regisztrációs "
                        "díj megerősítendő (FY2027)."
                    )
                },
            ),
            (
                {"hu": "(Kiválasztás esetén) Labor Condition Application (DOL) + I-129 petíció"},
                {"hu": "Kiválasztás után: LCA a DOL-nál, majd I-129 petíció."},
            ),
            (
                {"hu": "Vízum a konzulátuson és beutazás"},
                {
                    "hu": (
                        "3 év + 3 év. A munkáltatóhoz kötött. Lehetséges út a green card "
                        "felé (PERM → EB-2/EB-3)."
                    )
                },
            ),
        ],
    },
    "États-Unis : Green card EB-5 (investisseur immigrant)": {
        "name": {"hu": "Egyesült Államok: EB-5 green card (bevándorló befektető)"},
        "steps": [
            (
                {"hu": "A befektetés strukturálása & a források eredetének ellenőrzése"},
                {
                    "hu": (
                        "🟢 800 000 USD célzott övezetben (TEA) / 1 050 000 USD TEA-n kívül "
                        "+ 10 teljes munkaidős munkahely létrehozása. Újraindexálás "
                        "várhatóan 2027. január 1-jén. A források jogszerű eredetének "
                        "nyomon követhetősége kötelező (szigorúan vizsgálják). Közvetlen "
                        "befektetés VAGY Regional Centeren keresztül. Ügyvédi díj ~15-50k+ "
                        "USD."
                    )
                },
            ),
            (
                {"hu": "I-526E petíció (USCIS)"},
                {"hu": "Az I-526E petíció benyújtása az USCIS-hez."},
            ),
            (
                {"hu": "Feltételes green card (2 év): konzuli eljárás vagy státuszmódosítás"},
                {
                    "hu": (
                        "2 évre szóló feltételes green card, konzuli úton vagy "
                        "státuszmódosítással (adjustment of status)."
                    )
                },
            ),
            (
                {"hu": "A feltételek feloldása (I-829)"},
                {
                    "hu": (
                        "~2 év után igazolni kell a befektetés és a 10 munkahely "
                        "fennmaradását → állandó green card. Az USCIS ügyintézési ideje "
                        "hosszú és változó."
                    )
                },
            ),
        ],
    },
    "États-Unis : Green card EB-2 NIW / EB-1A (par le mérite)": {
        "name": {"hu": "Egyesült Államok: EB-2 NIW / EB-1A green card (érdemek alapján)"},
        "steps": [
            (
                {"hu": "A megfelelő út meghatározása"},
                {
                    "hu": (
                        "🟠 EB-1A = rendkívüli képességek (jelentős díj VAGY 10 "
                        "kritériumból ≥ 3). EB-2 NIW = magasabb fokú végzettség (advanced "
                        "degree)/kivételes képesség + a 3 Dhanasar-feltétel (érdem és "
                        "nemzeti jelentőség, jó helyzet a terv előmozdítására, az "
                        "állásajánlat alóli mentesítés előnye). Mindkettő lehetővé teszi "
                        "az önpetíciót (self-petition)."
                    )
                },
            ),
            (
                {"hu": "A bizonyítékokat tartalmazó dokumentáció összeállítása"},
                {"hu": "A kiválóságot/nemzeti érdeket alátámasztó bizonyítékok összeállítása."},
            ),
            (
                {"hu": "I-140 petíció (USCIS)"},
                {"hu": "Az I-140 petíció benyújtása az USCIS-hez."},
            ),
            (
                {"hu": "Green card (visa bulletin / státuszmódosítás)"},
                {
                    "hu": (
                        "🔴 Mérlegelésen alapuló döntés (a bizonyítékok minőségétől függ). "
                        "Átfutási idők és várakozási sorok a visa bulletin szerint."
                    )
                },
            ),
        ],
    },
    "États-Unis : Création de société (LLC / C-Corp)": {
        "name": {"hu": "Egyesült Államok: Cégalapítás (LLC / C-Corp)"},
        "steps": [
            (
                {"hu": "A társasági forma és az állam kiválasztása"},
                {
                    "hu": (
                        "LLC (pass-through, egyszerű) → könnyű működési struktúra "
                        "kiköltözés nélkül (amerikai számlázás, e-kereskedelem, "
                        "tanácsadás, holding). C-Corp (21% szövetségi adó, kettős "
                        "adóztatás) → kockázati tőke (VC) bevonása VAGY kiköltözéssel járó "
                        "E-2/L-1 vízum alapja (a bevett standard: Delaware). ⚠️ Az S-Corp "
                        "nem rezidensek számára ZÁRVA → a valódi választás = LLC vagy "
                        "C-Corp. Állam: Delaware (VC) / Wyoming (alacsony költségek, nincs "
                        "állami adó) / a tényleges tevékenység helye szerinti állam. ⚠️ a "
                        "DE/WY bejegyzés NEM mentesít a regisztráció alól abban az "
                        "államban, ahol a társaság ténylegesen működik (nexus)."
                    )
                },
            ),
            (
                {"hu": "Megalapítás és registered agent"},
                {
                    "hu": (
                        "A jogalany megalapítása és egy registered agent (kézbesítési "
                        "megbízott) kijelölése."
                    )
                },
            ),
            (
                {"hu": "EIN, ITIN és bankszámla"},
                {
                    "hu": (
                        "EIN (Form SS-4; SSN nélkül több hét, faxon/postán), gyakran ITIN "
                        "(W-7) is szükséges. Számlanyitás fintech-szolgáltatónál "
                        "(Mercury/Wise/Relay), ha nincs kiutazás. Állami adóügyi "
                        "regisztrációk."
                    )
                },
            ),
            (
                {"hu": "Megfelelés külföldi tulajdonos esetén (már az 1. évtől)"},
                {
                    "hu": (
                        "🟢 Külföldi tulajdonában álló single-member LLC → Form 5472 + pro "
                        "forma 1120 (határidő: április 15., BÍRSÁG: 25 000 USD). C-Corp → "
                        "1120; 5472, ha van ≥ 25%-os kapcsolt külföldi részvényes; "
                        "osztalék-forrásadó 30% → 15% (USA-Franciaország egyezmény). 🔴 "
                        "BOI/Corporate Transparency Act: a FinCEN 2025. márciusi szabálya "
                        "a külföldi jogalanyokra szűkítette a kört: a hatályt ellenőrizze "
                        "a fincen.gov/boi oldalon."
                    )
                },
            ),
        ],
    },
    "Suisse : Permis B non-actif (rentier/retraité UE/AELE)": {
        "name": {"hu": "Svájc: B engedély keresőtevékenység nélkül (EU/EFTA járadékos/nyugdíjas)"},
        "steps": [
            (
                {"hu": "Az anyagi eszközök és a biztosítás igazolásainak összegyűjtése"},
                {
                    "hu": (
                        "🟠 Elegendő anyagi eszközök (a küszöb az LPC szerinti kiegészítő "
                        "ellátásokhoz igazodik, kantononként megerősítendő) + Svájcra "
                        "kiterjedő egészségbiztosítás. EU/EFTA-állampolgár esetén nincs "
                        "életkori feltétel."
                    )
                },
            ),
            (
                {"hu": "Érkezés bejelentése a községnél (14 napon belül)"},
                {
                    "hu": (
                        "Érkezés bejelentése a községnél 14 napon belül. Személyes "
                        "megjelenés szükséges."
                    )
                },
            ),
            (
                {"hu": "A B engedély kiadása"},
                {
                    "hu": (
                        "B engedély (5 év). 🟠 Átalányadózás a legtöbb kantonban elérhető "
                        "(külön ADÓZÁSI rendszer, amelyről a kantoni adóhatósággal külön "
                        "kell megállapodni: nem tartózkodási jog). A kanton megválasztása "
                        "döntő (adózás)."
                    )
                },
            ),
        ],
    },
    "Suisse : Permis L/B salarié (UE/AELE)": {
        "name": {"hu": "Svájc: L/B engedély munkavállalóknak (EU/EFTA)"},
        "steps": [
            (
                {"hu": "Aláírt munkaszerződés"},
                {
                    "hu": (
                        "Az engedély típusa a szerződés időtartamától függ: < 3 hónap = "
                        "egyszerű bejelentés · 3-12 hónap = L engedély · ≥ 12 hónap = B "
                        "engedély (5 év). EU/EFTA-állampolgár esetén nincs kvóta és "
                        "munkaerőpiaci teszt sem."
                    )
                },
            ),
            (
                {"hu": "Bejelentés/kérelem a községnél és a kantonnál"},
                {"hu": "Bejelentés/kérelem a községnél és a kantonnál."},
            ),
            (
                {"hu": "Az L vagy B engedély kiadása"},
                {
                    "hu": (
                        "C engedély (letelepedés) 5 év után EU/EFTA-állampolgároknak "
                        "(viszonosság alapján). A személyes adózást a kanton határozza meg."
                    )
                },
            ),
        ],
    },
    "Suisse : Indépendant / entrepreneur (UE/AELE)": {
        "name": {"hu": "Svájc: Önfoglalkoztató / vállalkozó (EU/EFTA)"},
        "steps": [
            (
                {"hu": "Valós és életképes önálló tevékenység igazolása"},
                {
                    "hu": (
                        "Üzleti terv, pénzügyi előrejelzés, helyiség/ügyfelek: a "
                        "tevékenységnek ténylegesnek kell lennie (nem lehet fiktív). "
                        "AVS-tagság önfoglalkoztatóként."
                    )
                },
            ),
            (
                {"hu": "Bejelentés a községnél és B engedély kérelmezése"},
                {"hu": "Bejelentés a községnél és B engedély kérelmezése (önfoglalkoztatóként)."},
            ),
            (
                {"hu": "A B engedély kiadása (önfoglalkoztató)"},
                {
                    "hu": (
                        "Párhuzamosan Sàrl/SA is alapítható (lásd a cégalapítási "
                        "folyamatot). Az adóterhet a kanton határozza meg."
                    )
                },
            ),
        ],
    },
    "Suisse : Rentier hors-UE (55 ans et +, art. 28 LEI)": {
        "name": {"hu": "Svájc: EU-n kívüli járadékos (55 éves kortól, LEI 28. cikk)"},
        "steps": [
            (
                {"hu": "A jogosultság felmérése és befogadó kanton kiválasztása"},
                {
                    "hu": (
                        "🔴 LEI 28. cikk / OASA 25. cikk: ≥ 55 év + KÜLÖNLEGES személyes "
                        "kötődés Svájchoz + keresőtevékenység hiánya + elegendő anyagi "
                        "eszközök + az életvitel központjának tényleges áthelyezése. "
                        "NAGYMÉRTÉKBEN mérlegelésen alapul: egyes kantonok befogadóak, "
                        "mások szigorúak: a kanton megválasztása döntő. Az 55 évnél "
                        "FIATALABB, EU-n kívüli járadékos számára nincs egyértelmű út."
                    )
                },
            ),
            (
                {"hu": "A kérelem benyújtása a kantoni migrációs hatósághoz"},
                {"hu": "A kérelem benyújtása a kantoni migrációs hatóságnál."},
            ),
            (
                {"hu": "B engedély (keresőtevékenység nélkül) megadása és átalányadózás"},
                {
                    "hu": (
                        "🟠 Az átalányadózás célcsoportja (külön adózási rendszer, amelyről "
                        "a letelepedés ELŐTT kantoni ruling útján kell megállapodni: "
                        "önmagában nem tartózkodási jogcím)."
                    )
                },
            ),
        ],
    },
    "Suisse : Salarié hors-UE (art. 18-23 LEI)": {
        "name": {"hu": "Svájc: Nem uniós munkavállaló (LEI 18-23. cikk)"},
        "steps": [
            (
                {"hu": "A feltételek ellenőrzése (a szűk keresztmetszet)"},
                {
                    "hu": (
                        "🔴 Együttes feltételek: gazdasági érdek + "
                        "VEZETŐI/SZAKÉRTŐI/SZAKKÉPZETT profil + szokásos bérezés és "
                        "munkafeltételek + a hazai/EU-EFTA munkaerőpiac ELSŐBBSÉGE (a "
                        "munkáltatónak igazolnia kell, hogy nincs svájci/uniós jelölt) + "
                        "éves KVÓTA (elakadás kockázata, ha a kvóta kimerül). Munkáltató "
                        "és vezetői/szakértői profil nélkül ez az út gyakorlatilag ZÁRVA "
                        "van."
                    )
                },
            ),
            (
                {"hu": "A munkáltató benyújtja a kérelmet (kantoni hatóság + SEM)"},
                {"hu": "A kérelmet a munkáltató nyújtja be a kantoni hatósághoz és a SEM-hez."},
            ),
            (
                {"hu": "D vízum & L/B engedély (a kvóta terhére)"},
                {
                    "hu": (
                        "🟠 Az engedélyt a harmadik országbeliekre vonatkozó éves kvóta "
                        "terhére adják ki."
                    )
                },
            ),
        ],
    },
    "Suisse : Création de société (Sàrl / SA)": {
        "name": {"hu": "Svájc: Cégalapítás (Sàrl / SA)"},
        "steps": [
            (
                {"hu": "Döntés a helyben lakó vezetőről, a kantonról és a társasági formáról"},
                {
                    "hu": (
                        "⚠️ HELYBEN LAKÓ VEZETŐ KÖTELEZŐ: legalább egy svájci lakóhelyű, "
                        "cégjegyzési joggal rendelkező személy (CO 814. cikk (3) bek. / "
                        "718. cikk (4) bek.): helyi munkaerő-felvétel, fiduciárius "
                        "(bizalmi) igazgató vagy az alapító letelepedése. Nélküle nincs "
                        "társaság. KANTON = az 1. számú adózási tényező: a nyereségadó "
                        "~11,5% (Zug/Nidwalden) és ~21% (Bern) között mozog; Genf ~14% "
                        "(már NEM számít magas adóterhelésű kantonnak). Társasági forma: "
                        "Sàrl (20 000 CHF befizetett tőke, a tagok szerepelnek a "
                        "cégjegyzékben) / SA (100 000 CHF jegyzett tőke, ebből min. 50 000 "
                        "befizetve, a részvényesek nem szerepelnek a cégjegyzékben)."
                    )
                },
            ),
            (
                {"hu": "Alapszabály közjegyzői okiratban és a tőke befizetése"},
                {
                    "hu": (
                        "Közokirat kötelező + a tőke elhelyezése zárolt letéti számlán "
                        "(banki igazolás)."
                    )
                },
            ),
            (
                {"hu": "Bejegyzés a kereskedelmi cégjegyzékbe (Zefix)"},
                {"hu": "A társaság bejegyzése a kereskedelmi cégjegyzékbe (Zefix)."},
            ),
            (
                {"hu": "ÁFA és társadalombiztosítás"},
                {
                    "hu": (
                        "🟠 IFD 8,5% törvényi (~7,83% tényleges) + kantonális/községi adó "
                        "(lásd az 1. lépést). ÁFA 8,1%, ha az árbevétel > 100 000 CHF. 35% "
                        "forrásadó az osztalékokra (az egyezmények szerinti maradék "
                        "kulcsokkal). 1% illeték az 1 M CHF feletti tőkebevitelre. "
                        "MEGJEGYZÉS, átalányadózás (forfait fiscal): kereső tevékenységet "
                        "nem folytató külföldi járadékosok adózási rendszere (szövetségi "
                        "alsó határ 400 000 CHF / a lakbér 7×-ese, kantonális ruling): "
                        "külön konstrukció, nem tartózkodási jogcím; Zürichben, Bázelben, "
                        "Schaffhausenben és Appenzell Ausserrhodenben eltörölték."
                    )
                },
            ),
        ],
    },
    "Canada : Express Entry (résidence permanente fédérale)": {
        "name": {"hu": "Kanada: Express Entry (szövetségi állandó tartózkodás)"},
        "steps": [
            (
                {"hu": "A jogosultság ellenőrzése & a CRS-pontszám becslése"},
                {
                    "hu": (
                        "🟠 FSW = legalább 67/100 pont. CEC = ~1 év kanadai szakképzett "
                        "munkatapasztalat. A CRS-pontszámot (max. 1200) a foglalkozás "
                        "(TEER szint), a nyelvtudás (CLB/NCLC), az életkor és a végzettség "
                        "határozza meg. ⚠️ FRANCIA NYELV = JELENTŐS ELŐNY: a „francia "
                        "nyelvtudás” kategóriájú meghívási körökben a CRS-küszöbök jóval "
                        "alacsonyabbak. Egy PNP-jelölés +600 CRS-pontot ad (szinte biztos "
                        "meghívás). Kanadában nincs nyugdíjas- vagy befektetői vízum."
                    )
                },
            ),
            (
                {"hu": "Nyelvvizsgák, diploma-egyenértékűség (ECA) & profil a jelöltállományban"},
                {
                    "hu": (
                        "Nyelvvizsgák, a diplomák ECA-értékelése és a profil létrehozása a "
                        "jelöltállományban."
                    )
                },
            ),
            (
                {"hu": "Meghívás a kérelem benyújtására (ITA) & állandó tartózkodási (PR) kérelem"},
                {
                    "hu": (
                        "🟠 PR-díj ~950 $ + RPRF 575 $ + biometrikus adatfelvétel 85 $. A "
                        "meghívási körök CRS-küszöbei nagyon ingadozók (canada.ca/IRCC), "
                        "újra meg kell erősíteni."
                    )
                },
            ),
        ],
    },
    "Canada : Provincial Nominee Program (PNP)": {
        "name": {"hu": "Kanada: Provincial Nominee Program (PNP)"},
        "steps": [
            (
                {"hu": "A tartomány és a profilnak megfelelő alprogram (stream) azonosítása"},
                {
                    "hu": (
                        "🔴 Minden tartománynak saját alprogramjai és kritériumai vannak "
                        "(gyakran hiányszakmához, helyi állásajánlathoz vagy a "
                        "tartományhoz fűződő kötődéshez kapcsolódnak). A 2025-ös PNP-keret "
                        "csökkent (~55 000): az alprogramok elérhetősége változékony, "
                        "tartományonként megerősítendő (OINP/BC PNP/AAIP…)."
                    )
                },
            ),
            (
                {"hu": "Érdeklődési nyilatkozat / tartományi pályázat"},
                {
                    "hu": (
                        "Érdeklődési nyilatkozat vagy pályázat benyújtása a kiválasztott "
                        "tartományhoz."
                    )
                },
            ),
            (
                {"hu": "Tartományi jelölés → szövetségi állandó tartózkodási (PR) kérelem"},
                {
                    "hu": (
                        "A jelölés +600 CRS-pontot ad (Express Entry útján, összehangolt "
                        "alprogram esetén) VAGY Express Entry-n kívüli „alap” PNP-utat "
                        "jelent, ezt követi az állandó tartózkodási (PR) kérelem az "
                        "IRCC-nél."
                    )
                },
            ),
        ],
    },
    "Québec : PSTQ / Arrima (sélection québécoise, puis RP)": {
        "name": {"hu": "Québec: PSTQ / Arrima (québeci kiválasztás, majd állandó tartózkodás)"},
        "steps": [
            (
                {"hu": "Arrima-profil létrehozása (érdeklődési nyilatkozat)"},
                {
                    "hu": (
                        "⚠️ Az Express Entrytől KÜLÖNÁLLÓ québeci rendszer. PSTQ = "
                        "Programme de sélection des travailleurs qualifiés, a szakképzett "
                        "munkavállalók kiválasztási programja (külön ágakkal). 🟠 A FRANCIA "
                        "nyelvtudás jelentős előny (küszöbök és pontok). Az ágak "
                        "elnevezését és a küszöböket meg kell erősíteni (Québec.ca/MIFI)."
                    )
                },
            ),
            (
                {"hu": "Québeci meghívás és CSQ-kérelem (MIFI)"},
                {
                    "hu": (
                        "🟠 A MIFI díjait meg kell erősíteni. A CSQ = Certificat de "
                        "sélection du Québec, québeci kiválasztási igazolás (tartományi "
                        "szintű kiválasztás)."
                    )
                },
            ),
            (
                {"hu": "Szövetségi PR-kérelem (IRCC) a CSQ birtokában"},
                {
                    "hu": (
                        "A PR-t (állandó tartózkodás) továbbra is a szövetségi kormány "
                        "adja ki, de a KIVÁLASZTÁS québeci. MEGJEGYZÉS: a PEQ (Programme "
                        "de l'expérience québécoise) gyorsított út a már Québecben "
                        "tartózkodó diplomások/munkavállalók számára."
                    )
                },
            ),
        ],
    },
    "Canada : Permis de travail → expérience canadienne → RP": {
        "name": {
            "hu": ("Kanada: Munkavállalási engedély → kanadai tapasztalat → állandó tartózkodás")
        },
        "steps": [
            (
                {"hu": "A munkavállalási engedély megszerzése (IMP vagy LMIA)"},
                {
                    "hu": (
                        "Két út: IMP (LMIA-mentes: vállalaton belüli áthelyezés C12, "
                        "kereskedelmi megállapodások, fiatal szakemberek/IEC-PVT a "
                        "jogosult francia állampolgárok számára) VAGY TFWP (LMIA "
                        "hatásvizsgálattal, nehézkesebb). 🟠 Mivel az állásajánlatért járó "
                        "CRS-pontokat eltörölték (2025 tavaszán), a PNP központibb "
                        "szerepet kap, mint önmagában az állásajánlat."
                    )
                },
            ),
            (
                {"hu": "Munkavégzés Kanadában és minősített szakmai tapasztalat gyűjtése"},
                {
                    "hu": (
                        "~1 év minősített szakmai tapasztalat (TEER 0/1/2/3) megnyitja az "
                        "utat a CEC (Canadian Experience Class) felé."
                    )
                },
            ),
            (
                {"hu": "PR-kérelem (állandó tartózkodás) Express Entryn keresztül (CEC)"},
                {
                    "hu": (
                        "A CEC a leggyorsabb út a PR-hez annak, aki már rendelkezik "
                        "kanadai tapasztalattal. Francia nyelvtudás = előny (külön "
                        "meghívási körök)."
                    )
                },
            ),
        ],
    },
    "Canada : Start-up Visa (SUV, entrepreneur)": {
        "name": {"hu": "Kanada: Start-up Visa (SUV, vállalkozó)"},
        "steps": [
            (
                {"hu": "Kijelölt szervezet támogatásának megszerzése"},
                {
                    "hu": (
                        "🟠 Kijelölt szervezet: kockázatitőke-alap ≥ 200 000 $ / üzleti "
                        "angyal ≥ 75 000 $ / inkubátor (nincs tőkekövetelmény). Támogató "
                        "levél szükséges. ⚠️ Kanadában nincs befektetői/golden vízum: ez a "
                        "projektalapú út."
                    )
                },
            ),
            (
                {"hu": "A SUV-dokumentáció összeállítása"},
                {"hu": "A Start-up Visa dokumentációjának összeállítása."},
            ),
            (
                {
                    "hu": (
                        "Állandó tartózkodási (PR) kérelem (és addig ideiglenes "
                        "munkavállalási engedély)"
                    )
                },
                {
                    "hu": (
                        "A PR közvetlenül jár (nem feltételes). A PR-kérelem elbírálása "
                        "alatt munkavállalási engedély szerezhető a tevékenység "
                        "megkezdéséhez."
                    )
                },
            ),
        ],
    },
}
