"""The six other product languages of the sector starter content (01/10):
the sector journeys every new agency receives, and their demo dossiers,
existed in French only, so an English or Hungarian agency started with French
journeys. FR stays the source in sector_seed / demo_case_seed; this module
carries en/es/ru/pt/it/hu, generated from the reviewed translation files and
kept out of the hand-written seeds. Same rules as the FR content: sector
neutral, no country-specific institution, no em/en dash.

- EXAMPLE_PREFIX_I18N: the « [Exemple] » tag (journey-name prefix and demo
  client first name), per language.
- SECTOR_I18N: sector -> {"name": {lang}, "steps": [{"name": {lang},
  "note": {lang}, "docs": [{lang}, ...]}]}, steps and docs PARALLEL by
  position to SECTOR_TEMPLATES.
- DEMO_I18N: sector -> {"last_name": {lang}, "text_fields": {key: {lang}}}
  (select values are mapped by option position, never translated here).
- DEMO_COMMON_I18N: the demo dossier's own labels (source, tag, profession).
"""

from typing import Any

EXAMPLE_PREFIX_I18N: dict[str, str] = {
    "en": "[Example]",
    "es": "[Ejemplo]",
    "ru": "[Пример]",
    "pt": "[Exemplo]",
    "it": "[Esempio]",
    "hu": "[Példa]",
}

SECTOR_I18N: dict[str, dict[str, Any]] = {
    "legal": {
        "name": {
            "en": "Civil litigation",
            "es": "Litigio civil",
            "ru": "Гражданский судебный спор",
            "pt": "Contencioso civil",
            "it": "Contenzioso civile",
            "hu": "Polgári peres eljárás",
        },
        "steps": [
            {
                "name": {
                    "en": "Initial consultation and case opening",
                    "es": "Consulta inicial y apertura del expediente",
                    "ru": "Первичная консультация и открытие дела",
                    "pt": "Consulta inicial e abertura do processo",
                    "it": "Consulenza iniziale e apertura della pratica",
                    "hu": "Első konzultáció és az ügy megnyitása",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Documents relating to the dispute",
                        "es": "Documentos del litigio",
                        "ru": "Документы по спору",
                        "pt": "Documentos do litígio",
                        "it": "Documenti relativi alla controversia",
                        "hu": "A jogvitához kapcsolódó iratok",
                    },
                    {
                        "en": "Proof of identity",
                        "es": "Justificante de identidad",
                        "ru": "Документ, удостоверяющий личность",
                        "pt": "Comprovativo de identidade",
                        "it": "Documento d'identità",
                        "hu": "Személyazonosság igazolása",
                    },
                    {
                        "en": "Fee agreement",
                        "es": "Contrato de honorarios",
                        "ru": "Соглашение о гонораре",
                        "pt": "Acordo de honorários",
                        "it": "Accordo sugli onorari",
                        "hu": "Díjmegállapodás",
                    },
                ],
            },
            {
                "name": {
                    "en": "Amicable settlement attempt / formal notice",
                    "es": "Intento de solución amistosa / requerimiento formal",
                    "ru": "Попытка мирного урегулирования / досудебная претензия",
                    "pt": "Tentativa de resolução amigável / interpelação",
                    "it": "Tentativo di accordo bonario / messa in mora",
                    "hu": "Peren kívüli rendezési kísérlet / felszólítás",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Formal notice letter",
                        "es": "Carta de requerimiento",
                        "ru": "Претензионное письмо",
                        "pt": "Carta de interpelação",
                        "it": "Lettera di messa in mora",
                        "hu": "Felszólító levél",
                    },
                ],
            },
            {
                "name": {
                    "en": "Commencement of proceedings (originating document)",
                    "es": "Inicio del procedimiento (escrito de demanda)",
                    "ru": "Предъявление иска (исковое заявление)",
                    "pt": "Propositura da ação (petição inicial)",
                    "it": "Introduzione del giudizio (atto introduttivo)",
                    "hu": "Az eljárás megindítása (keresetlevél)",
                },
                "note": {
                    "en": (
                        "Step also involving a competent judicial officer (service / "
                        "notification), to be linked from the agency directory."
                    ),
                    "es": (
                        "Etapa en la que también interviene un agente judicial "
                        "competente (emplazamiento / notificación), que debe "
                        "vincularse desde el directorio de la agencia."
                    ),
                    "ru": (
                        "На этом этапе также участвует компетентное судебное "
                        "должностное лицо (вручение / уведомление); контакт следует "
                        "привязать из справочника агентства."
                    ),
                    "pt": (
                        "Etapa que envolve também um oficial de justiça competente "
                        "(citação / notificação), a associar a partir do diretório da "
                        "agência."
                    ),
                    "it": (
                        "Fase che coinvolge anche un ufficiale giudiziario competente "
                        "(notificazione / comunicazione), da collegare dalla rubrica "
                        "dell'agenzia."
                    ),
                    "hu": (
                        "Ebben a lépésben egy illetékes igazságügyi tisztviselő "
                        "(kézbesítés / értesítés) is közreműködik; a kapcsolatot az "
                        "iroda címtárából kell hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Originating document",
                        "es": "Escrito de demanda",
                        "ru": "Исковое заявление",
                        "pt": "Petição inicial",
                        "it": "Atto introduttivo del giudizio",
                        "hu": "Keresetlevél",
                    },
                    {
                        "en": "Schedule of exhibits",
                        "es": "Índice de documentos aportados",
                        "ru": "Опись приложенных документов",
                        "pt": "Lista de documentos juntos",
                        "it": "Indice dei documenti prodotti",
                        "hu": "Csatolt iratok jegyzéke",
                    },
                ],
            },
            {
                "name": {
                    "en": "Pre-trial phase (exchange of written submissions)",
                    "es": "Fase de alegaciones (intercambio de escritos)",
                    "ru": "Подготовка дела к слушанию (обмен письменными позициями)",
                    "pt": "Fase de instrução (troca de articulados)",
                    "it": "Fase istruttoria (scambio di memorie)",
                    "hu": "Előkészítő szakasz (beadványok váltása)",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Written submissions",
                        "es": "Escritos de alegaciones",
                        "ru": "Письменные позиции сторон",
                        "pt": "Articulados",
                        "it": "Memorie",
                        "hu": "Írásbeli beadványok",
                    },
                    {
                        "en": "Disclosed exhibits",
                        "es": "Documentos aportados",
                        "ru": "Представленные документы",
                        "pt": "Documentos apresentados",
                        "it": "Documenti prodotti",
                        "hu": "Benyújtott bizonyítékok",
                    },
                ],
            },
            {
                "name": {
                    "en": "Hearing (oral arguments)",
                    "es": "Vista oral",
                    "ru": "Судебное заседание (прения сторон)",
                    "pt": "Audiência de julgamento",
                    "it": "Udienza di discussione",
                    "hu": "Tárgyalás és perbeszédek",
                },
                "note": {},
                "docs": [],
            },
            {
                "name": {
                    "en": "Judgment and notification",
                    "es": "Sentencia y notificación",
                    "ru": "Решение суда и уведомление",
                    "pt": "Sentença e notificação",
                    "it": "Sentenza e notificazione",
                    "hu": "Ítélet és kézbesítés",
                },
                "note": {
                    "en": (
                        "Step handled by the competent judicial authority, to be "
                        "linked from the agency directory."
                    ),
                    "es": (
                        "Etapa a cargo de la autoridad judicial competente, que debe "
                        "vincularse desde el directorio de la agencia."
                    ),
                    "ru": (
                        "Этот этап проводит компетентный судебный орган; контакт "
                        "следует привязать из справочника агентства."
                    ),
                    "pt": (
                        "Etapa a cargo da autoridade judicial competente, a associar a "
                        "partir do diretório da agência."
                    ),
                    "it": (
                        "Fase a cura dell'autorità giudiziaria competente, da "
                        "collegare dalla rubrica dell'agenzia."
                    ),
                    "hu": (
                        "A lépést az illetékes igazságügyi hatóság végzi; a "
                        "kapcsolatot az iroda címtárából kell hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Judgment",
                        "es": "Sentencia",
                        "ru": "Решение суда",
                        "pt": "Sentença",
                        "it": "Sentenza",
                        "hu": "Ítélet",
                    },
                ],
            },
            {
                "name": {
                    "en": "Enforcement or appeals",
                    "es": "Ejecución o recursos",
                    "ru": "Исполнение решения или обжалование",
                    "pt": "Execução ou recurso",
                    "it": "Esecuzione o impugnazione",
                    "hu": "Végrehajtás vagy jogorvoslat",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Notice of judgment",
                        "es": "Notificación de la sentencia",
                        "ru": "Уведомление о вынесенном решении",
                        "pt": "Notificação da sentença",
                        "it": "Notificazione della sentenza",
                        "hu": "Értesítés az ítéletről",
                    },
                ],
            },
        ],
    },
    "accounting": {
        "name": {
            "en": "Annual accounts preparation",
            "es": "Elaboración de las cuentas anuales",
            "ru": "Подготовка годовой бухгалтерской отчётности",
            "pt": "Elaboração das contas anuais",
            "it": "Redazione del bilancio d'esercizio",
            "hu": "Éves beszámoló elkészítése",
        },
        "steps": [
            {
                "name": {
                    "en": "Engagement letter and document collection",
                    "es": "Carta de encargo y recopilación de documentos",
                    "ru": "Письмо-обязательство и сбор документов",
                    "pt": "Carta de compromisso e recolha de documentos",
                    "it": "Lettera d'incarico e raccolta dei documenti",
                    "hu": "Megbízási levél és a bizonylatok összegyűjtése",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Engagement letter",
                        "es": "Carta de encargo",
                        "ru": "Письмо-обязательство",
                        "pt": "Carta de compromisso",
                        "it": "Lettera d'incarico",
                        "hu": "Megbízási levél",
                    },
                    {
                        "en": "Bank statements",
                        "es": "Extractos bancarios",
                        "ru": "Банковские выписки",
                        "pt": "Extratos bancários",
                        "it": "Estratti conto bancari",
                        "hu": "Bankszámlakivonatok",
                    },
                    {
                        "en": "Invoices",
                        "es": "Facturas",
                        "ru": "Счета-фактуры",
                        "pt": "Faturas",
                        "it": "Fatture",
                        "hu": "Számlák",
                    },
                    {
                        "en": "Accounting journals",
                        "es": "Libros diarios",
                        "ru": "Бухгалтерские журналы",
                        "pt": "Diários contabilísticos",
                        "it": "Registri contabili",
                        "hu": "Könyvelési naplók",
                    },
                ],
            },
            {
                "name": {
                    "en": "Bookkeeping and accounting review",
                    "es": "Registro y revisión contable",
                    "ru": "Отражение операций и проверка учёта",
                    "pt": "Lançamento e revisão contabilística",
                    "it": "Registrazione e revisione contabile",
                    "hu": "Könyvelési adatrögzítés és felülvizsgálat",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Accounting source documents",
                        "es": "Justificantes contables",
                        "ru": "Первичные бухгалтерские документы",
                        "pt": "Documentos contabilísticos",
                        "it": "Documenti contabili",
                        "hu": "Számviteli bizonylatok",
                    },
                    {
                        "en": "Bank reconciliations",
                        "es": "Conciliaciones bancarias",
                        "ru": "Банковские сверки",
                        "pt": "Reconciliações bancárias",
                        "it": "Riconciliazioni bancarie",
                        "hu": "Bankegyeztetések",
                    },
                ],
            },
            {
                "name": {
                    "en": "Preparation of the balance sheet and income statement",
                    "es": "Elaboración del balance y de la cuenta de resultados",
                    "ru": "Составление бухгалтерского баланса и отчёта о финансовых результатах",
                    "pt": "Elaboração do balanço e da demonstração de resultados",
                    "it": "Redazione dello stato patrimoniale e del conto economico",
                    "hu": "A mérleg és az eredménykimutatás elkészítése",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Trial balance",
                        "es": "Balance de sumas y saldos",
                        "ru": "Оборотно-сальдовая ведомость",
                        "pt": "Balancete",
                        "it": "Bilancio di verifica",
                        "hu": "Főkönyvi kivonat",
                    },
                    {
                        "en": "General ledger",
                        "es": "Libro mayor",
                        "ru": "Главная книга",
                        "pt": "Razão geral",
                        "it": "Libro mastro",
                        "hu": "Főkönyv",
                    },
                ],
            },
            {
                "name": {
                    "en": "Preparation of tax returns",
                    "es": "Preparación de las declaraciones fiscales",
                    "ru": "Подготовка налоговых деклараций",
                    "pt": "Preparação das declarações fiscais",
                    "it": "Predisposizione delle dichiarazioni fiscali",
                    "hu": "Az adóbevallások elkészítése",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Annual tax returns",
                        "es": "Declaraciones fiscales anuales",
                        "ru": "Годовые налоговые декларации",
                        "pt": "Declarações fiscais anuais",
                        "it": "Dichiarazioni fiscali annuali",
                        "hu": "Éves adóbevallások",
                    },
                ],
            },
            {
                "name": {
                    "en": "Client approval and finalization of the accounts",
                    "es": "Validación del cliente y formulación de las cuentas",
                    "ru": "Согласование с клиентом и утверждение отчётности",
                    "pt": "Validação pelo cliente e fecho das contas",
                    "it": "Approvazione del cliente e chiusura dei conti",
                    "hu": "Ügyfél általi jóváhagyás és a beszámoló lezárása",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Approved annual accounts",
                        "es": "Cuentas anuales validadas",
                        "ru": "Утверждённая годовая отчётность",
                        "pt": "Contas anuais validadas",
                        "it": "Bilancio d'esercizio approvato",
                        "hu": "Jóváhagyott éves beszámoló",
                    },
                ],
            },
            {
                "name": {
                    "en": "Electronic submission and statutory filing",
                    "es": "Presentación telemática y depósito de cuentas",
                    "ru": "Электронная передача и обязательное представление отчётности",
                    "pt": "Transmissão eletrónica e depósito de contas",
                    "it": "Trasmissione telematica e deposito obbligatorio",
                    "hu": "Elektronikus beküldés és kötelező letétbe helyezés",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Filing receipt",
                        "es": "Justificante de depósito",
                        "ru": "Квитанция о приёме",
                        "pt": "Comprovativo de depósito",
                        "it": "Ricevuta di deposito",
                        "hu": "Befogadási igazolás",
                    },
                    {
                        "en": "Submitted returns",
                        "es": "Declaraciones presentadas",
                        "ru": "Направленные декларации",
                        "pt": "Declarações submetidas",
                        "it": "Dichiarazioni trasmesse",
                        "hu": "Beküldött bevallások",
                    },
                ],
            },
        ],
    },
    "real_estate": {
        "name": {
            "en": "Property sale",
            "es": "Venta de un inmueble",
            "ru": "Продажа объекта недвижимости",
            "pt": "Venda de um imóvel",
            "it": "Vendita di un immobile",
            "hu": "Ingatlan értékesítése",
        },
        "steps": [
            {
                "name": {
                    "en": "Valuation and signing of the sales mandate",
                    "es": "Valoración y firma del mandato de venta",
                    "ru": "Оценка и подписание агентского договора",
                    "pt": "Avaliação e assinatura do contrato de mediação",
                    "it": "Valutazione e firma dell'incarico di vendita",
                    "hu": "Értékbecslés és a megbízási szerződés aláírása",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Sales mandate",
                        "es": "Mandato de venta",
                        "ru": "Агентский договор на продажу",
                        "pt": "Contrato de mediação imobiliária",
                        "it": "Incarico di vendita",
                        "hu": "Eladási megbízási szerződés",
                    },
                    {
                        "en": "Title deed",
                        "es": "Título de propiedad",
                        "ru": "Правоустанавливающий документ",
                        "pt": "Título de propriedade",
                        "it": "Titolo di proprietà",
                        "hu": "Tulajdonjogot igazoló okirat",
                    },
                ],
            },
            {
                "name": {
                    "en": "Preparing the file and publishing the listing",
                    "es": "Preparación del expediente y publicación del anuncio",
                    "ru": "Подготовка пакета документов и публикация объявления",
                    "pt": "Preparação do processo e divulgação do anúncio",
                    "it": "Preparazione della documentazione e pubblicazione dell'annuncio",
                    "hu": "A dokumentáció összeállítása és a hirdetés közzététele",
                },
                "note": {
                    "en": (
                        "Step also involving a property inspector, to be linked from "
                        "the agency directory."
                    ),
                    "es": (
                        "Etapa en la que también interviene un técnico certificador "
                        "inmobiliario, que debe vincularse desde el directorio de la "
                        "agencia."
                    ),
                    "ru": (
                        "На этом этапе также участвует специалист по техническому "
                        "обследованию недвижимости; контакт следует привязать из "
                        "справочника агентства."
                    ),
                    "pt": (
                        "Etapa que envolve também um técnico de certificação "
                        "imobiliária, a associar a partir do diretório da agência."
                    ),
                    "it": (
                        "Fase che coinvolge anche un tecnico certificatore "
                        "immobiliare, da collegare dalla rubrica dell'agenzia."
                    ),
                    "hu": (
                        "Ebben a lépésben egy ingatlan-műszaki szakértő is "
                        "közreműködik; a kapcsolatot az iroda címtárából kell "
                        "hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Technical inspection reports",
                        "es": "Informes de inspección técnica",
                        "ru": "Пакет технических заключений",
                        "pt": "Relatórios de inspeção técnica",
                        "it": "Fascicolo delle verifiche tecniche",
                        "hu": "Műszaki szakvélemények",
                    },
                    {
                        "en": "Energy performance certificate",
                        "es": "Certificado de eficiencia energética",
                        "ru": "Сертификат энергоэффективности",
                        "pt": "Certificado energético",
                        "it": "Attestato di prestazione energetica",
                        "hu": "Energetikai tanúsítvány",
                    },
                    {
                        "en": "Photos",
                        "es": "Fotografías",
                        "ru": "Фотографии",
                        "pt": "Fotografias",
                        "it": "Fotografie",
                        "hu": "Fényképek",
                    },
                ],
            },
            {
                "name": {
                    "en": "Viewings and negotiation",
                    "es": "Visitas y negociación",
                    "ru": "Показы и переговоры",
                    "pt": "Visitas e negociação",
                    "it": "Visite e trattativa",
                    "hu": "Ingatlanbemutatások és tárgyalás",
                },
                "note": {},
                "docs": [],
            },
            {
                "name": {
                    "en": "Purchase offer accepted",
                    "es": "Oferta de compra aceptada",
                    "ru": "Предложение о покупке принято",
                    "pt": "Proposta de compra aceite",
                    "it": "Proposta d'acquisto accettata",
                    "hu": "Elfogadott vételi ajánlat",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Purchase offer",
                        "es": "Oferta de compra",
                        "ru": "Предложение о покупке",
                        "pt": "Proposta de compra",
                        "it": "Proposta d'acquisto",
                        "hu": "Vételi ajánlat",
                    },
                ],
            },
            {
                "name": {
                    "en": "Signing of the preliminary sale agreement",
                    "es": "Firma del contrato preliminar",
                    "ru": "Подписание предварительного договора",
                    "pt": "Assinatura do contrato-promessa",
                    "it": "Firma del contratto preliminare",
                    "hu": "Az előszerződés aláírása",
                },
                "note": {
                    "en": "Step handled by a notary, to be linked from the agency directory.",
                    "es": (
                        "Etapa a cargo de un notario, que debe vincularse desde el "
                        "directorio de la agencia."
                    ),
                    "ru": (
                        "Этот этап проводит нотариус; контакт следует привязать из "
                        "справочника агентства."
                    ),
                    "pt": (
                        "Etapa a cargo de um notário, a associar a partir do diretório da agência."
                    ),
                    "it": "Fase a cura di un notaio, da collegare dalla rubrica dell'agenzia.",
                    "hu": (
                        "A lépést közjegyző végzi; a kapcsolatot az iroda címtárából "
                        "kell hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Preliminary sale agreement",
                        "es": "Contrato preliminar de compraventa",
                        "ru": "Предварительный договор купли-продажи",
                        "pt": "Contrato-promessa de compra e venda",
                        "it": "Contratto preliminare di vendita",
                        "hu": "Adásvételi előszerződés",
                    },
                    {
                        "en": "Buyer's documents",
                        "es": "Documentación del comprador",
                        "ru": "Документы покупателя",
                        "pt": "Documentos do comprador",
                        "it": "Documenti dell'acquirente",
                        "hu": "A vevő dokumentumai",
                    },
                ],
            },
            {
                "name": {
                    "en": "Fulfillment of conditions precedent (financing)",
                    "es": "Cumplimiento de las condiciones suspensivas (financiación)",
                    "ru": "Выполнение отлагательных условий (финансирование)",
                    "pt": "Cumprimento das condições suspensivas (financiamento)",
                    "it": "Avveramento delle condizioni sospensive (finanziamento)",
                    "hu": "A felfüggesztő feltételek teljesülése (finanszírozás)",
                },
                "note": {
                    "en": (
                        "Step also involving the lending institution, to be linked "
                        "from the agency directory."
                    ),
                    "es": (
                        "Etapa en la que también interviene la entidad prestamista, "
                        "que debe vincularse desde el directorio de la agencia."
                    ),
                    "ru": (
                        "На этом этапе также участвует кредитная организация; контакт "
                        "следует привязать из справочника агентства."
                    ),
                    "pt": (
                        "Etapa que envolve também a instituição de crédito, a associar "
                        "a partir do diretório da agência."
                    ),
                    "it": (
                        "Fase che coinvolge anche l'istituto di credito, da collegare "
                        "dalla rubrica dell'agenzia."
                    ),
                    "hu": (
                        "Ebben a lépésben a hitelező pénzintézet is közreműködik; a "
                        "kapcsolatot az iroda címtárából kell hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Loan offer",
                        "es": "Oferta de préstamo",
                        "ru": "Кредитное предложение",
                        "pt": "Proposta de empréstimo",
                        "it": "Offerta di mutuo",
                        "hu": "Hitelajánlat",
                    },
                ],
            },
            {
                "name": {
                    "en": "Signing of the final deed of sale",
                    "es": "Firma de la escritura definitiva",
                    "ru": "Подписание основного договора купли-продажи",
                    "pt": "Assinatura da escritura de compra e venda",
                    "it": "Firma dell'atto definitivo",
                    "hu": "A végleges adásvételi szerződés aláírása",
                },
                "note": {
                    "en": "Step handled by a notary, to be linked from the agency directory.",
                    "es": (
                        "Etapa a cargo de un notario, que debe vincularse desde el "
                        "directorio de la agencia."
                    ),
                    "ru": (
                        "Этот этап проводит нотариус; контакт следует привязать из "
                        "справочника агентства."
                    ),
                    "pt": (
                        "Etapa a cargo de um notário, a associar a partir do diretório da agência."
                    ),
                    "it": "Fase a cura di un notaio, da collegare dalla rubrica dell'agenzia.",
                    "hu": (
                        "A lépést közjegyző végzi; a kapcsolatot az iroda címtárából "
                        "kell hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Final deed of sale",
                        "es": "Escritura de compraventa definitiva",
                        "ru": "Основной договор купли-продажи",
                        "pt": "Escritura definitiva de compra e venda",
                        "it": "Atto di vendita definitivo",
                        "hu": "Végleges adásvételi szerződés",
                    },
                ],
            },
        ],
    },
    "wealth": {
        "name": {
            "en": "Wealth assessment and recommendations",
            "es": "Diagnóstico patrimonial y recomendaciones",
            "ru": "Анализ капитала и рекомендации",
            "pt": "Diagnóstico patrimonial e recomendações",
            "it": "Analisi patrimoniale e raccomandazioni",
            "hu": "Vagyonfelmérés és javaslatok",
        },
        "steps": [
            {
                "name": {
                    "en": "Discovery meeting and know-your-client information gathering",
                    "es": (
                        "Entrevista inicial y recopilación de información de "
                        "conocimiento del cliente"
                    ),
                    "ru": "Ознакомительная встреча и сбор сведений о клиенте",
                    "pt": "Reunião inicial e recolha de informação sobre o cliente",
                    "it": "Colloquio conoscitivo e profilatura del cliente",
                    "hu": "Megismerő beszélgetés és ügyfélmegismerési adatok gyűjtése",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Identity document",
                        "es": "Documento de identidad",
                        "ru": "Документ, удостоверяющий личность",
                        "pt": "Documento de identificação",
                        "it": "Documento d'identità",
                        "hu": "Személyazonosító okmány",
                    },
                    {
                        "en": "Tax assessment notice",
                        "es": "Notificación de liquidación del impuesto",
                        "ru": "Налоговое уведомление",
                        "pt": "Nota de liquidação de imposto",
                        "it": "Avviso di liquidazione dell'imposta",
                        "hu": "Adómegállapítási értesítő",
                    },
                    {
                        "en": "Account statements",
                        "es": "Extractos de cuentas",
                        "ru": "Выписки по счетам",
                        "pt": "Extratos de contas",
                        "it": "Estratti conto",
                        "hu": "Számlakivonatok",
                    },
                    {
                        "en": "Know-your-client questionnaire",
                        "es": "Cuestionario de conocimiento del cliente",
                        "ru": "Анкета для изучения клиента",
                        "pt": "Questionário de conhecimento do cliente",
                        "it": "Questionario di profilatura del cliente",
                        "hu": "Ügyfélmegismerési kérdőív",
                    },
                ],
            },
            {
                "name": {
                    "en": "Wealth analysis and audit",
                    "es": "Análisis y auditoría patrimonial",
                    "ru": "Анализ и аудит капитала",
                    "pt": "Análise e auditoria patrimonial",
                    "it": "Analisi e audit patrimoniale",
                    "hu": "Vagyonelemzés és vagyonaudit",
                },
                "note": {},
                "docs": [],
            },
            {
                "name": {
                    "en": "Delivery of recommendations (written report)",
                    "es": "Entrega de las recomendaciones (informe escrito)",
                    "ru": "Передача рекомендаций (письменный отчёт)",
                    "pt": "Entrega das recomendações (relatório escrito)",
                    "it": "Consegna delle raccomandazioni (relazione scritta)",
                    "hu": "A javaslatok átadása (írásos jelentés)",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Wealth audit report",
                        "es": "Informe de auditoría patrimonial",
                        "ru": "Отчёт об аудите капитала",
                        "pt": "Relatório de auditoria patrimonial",
                        "it": "Relazione di audit patrimoniale",
                        "hu": "Vagyonaudit-jelentés",
                    },
                    {
                        "en": "Pre-contractual information document",
                        "es": "Documento de información precontractual",
                        "ru": "Преддоговорный информационный документ",
                        "pt": "Documento de informação pré-contratual",
                        "it": "Documento informativo precontrattuale",
                        "hu": "Szerződéskötést megelőző tájékoztató dokumentum",
                    },
                ],
            },
            {
                "name": {
                    "en": "Implementation of the solutions",
                    "es": "Implementación de las soluciones",
                    "ru": "Реализация решений",
                    "pt": "Implementação das soluções",
                    "it": "Attuazione delle soluzioni",
                    "hu": "A megoldások megvalósítása",
                },
                "note": {
                    "en": (
                        "Step also involving an insurer or a financial institution, to "
                        "be linked from the agency directory."
                    ),
                    "es": (
                        "Etapa en la que también interviene una aseguradora o una "
                        "entidad financiera, que debe vincularse desde el directorio "
                        "de la agencia."
                    ),
                    "ru": (
                        "На этом этапе также участвует страховщик или финансовая "
                        "организация; контакт следует привязать из справочника "
                        "агентства."
                    ),
                    "pt": (
                        "Etapa que envolve também uma seguradora ou uma instituição "
                        "financeira, a associar a partir do diretório da agência."
                    ),
                    "it": (
                        "Fase che coinvolge anche un assicuratore o un istituto "
                        "finanziario, da collegare dalla rubrica dell'agenzia."
                    ),
                    "hu": (
                        "Ebben a lépésben egy biztosító vagy pénzügyi intézmény is "
                        "közreműködik; a kapcsolatot az iroda címtárából kell "
                        "hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Subscription forms",
                        "es": "Boletines de suscripción",
                        "ru": "Заявления на заключение договоров",
                        "pt": "Boletins de subscrição",
                        "it": "Moduli di sottoscrizione",
                        "hu": "Jegyzési lapok",
                    },
                ],
            },
            {
                "name": {
                    "en": "Annual review and update",
                    "es": "Seguimiento anual y actualización",
                    "ru": "Ежегодное сопровождение и актуализация",
                    "pt": "Acompanhamento anual e atualização",
                    "it": "Monitoraggio annuale e aggiornamento",
                    "hu": "Éves felülvizsgálat és frissítés",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Know-your-client file update",
                        "es": "Actualización del expediente de conocimiento del cliente",
                        "ru": "Актуализация досье по изучению клиента",
                        "pt": "Atualização do dossiê de conhecimento do cliente",
                        "it": "Aggiornamento del profilo del cliente",
                        "hu": "Az ügyfélmegismerési dokumentáció frissítése",
                    },
                ],
            },
        ],
    },
    "hr_mobility": {
        "name": {
            "en": "International employee mobility",
            "es": "Movilidad internacional de un empleado",
            "ru": "Международная мобильность сотрудника",
            "pt": "Mobilidade internacional de um trabalhador",
            "it": "Mobilità internazionale di un dipendente",
            "hu": "Munkavállaló nemzetközi mobilitása",
        },
        "steps": [
            {
                "name": {
                    "en": "Project scoping and choice of assignment scheme",
                    "es": "Definición del proyecto y elección del régimen",
                    "ru": "Определение параметров проекта и выбор режима",
                    "pt": "Enquadramento do projeto e escolha do regime",
                    "it": "Definizione del progetto e scelta del regime",
                    "hu": "A projekt kereteinek meghatározása és a kiküldetési forma kiválasztása",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Scoping sheet",
                        "es": "Ficha de definición del proyecto",
                        "ru": "Карточка проекта",
                        "pt": "Ficha de enquadramento",
                        "it": "Scheda di inquadramento",
                        "hu": "Projektkeret-adatlap",
                    },
                    {
                        "en": "Job description",
                        "es": "Descripción del puesto",
                        "ru": "Описание должности",
                        "pt": "Descrição de funções",
                        "it": "Descrizione della posizione",
                        "hu": "Munkaköri leírás",
                    },
                ],
            },
            {
                "name": {
                    "en": "Compensation package simulation",
                    "es": "Simulación del paquete retributivo",
                    "ru": "Моделирование компенсационного пакета",
                    "pt": "Simulação do pacote remuneratório",
                    "it": "Simulazione del pacchetto retributivo",
                    "hu": "A javadalmazási csomag szimulációja",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Package simulation",
                        "es": "Simulación del paquete",
                        "ru": "Расчёт компенсационного пакета",
                        "pt": "Simulação do pacote",
                        "it": "Simulazione del pacchetto",
                        "hu": "Csomagszimuláció",
                    },
                ],
            },
            {
                "name": {
                    "en": "Contractual formalities",
                    "es": "Formalidades contractuales",
                    "ru": "Договорное оформление",
                    "pt": "Formalidades contratuais",
                    "it": "Adempimenti contrattuali",
                    "hu": "Szerződéses ügyintézés",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Expatriation addendum / local contract",
                        "es": "Adenda de expatriación / contrato local",
                        "ru": "Дополнительное соглашение об экспатриации / местный договор",
                        "pt": "Aditamento de expatriação / contrato local",
                        "it": "Addendum di espatrio / contratto locale",
                        "hu": "Kiküldetési szerződésmódosítás / helyi munkaszerződés",
                    },
                ],
            },
            {
                "name": {
                    "en": "Immigration and social security formalities",
                    "es": "Trámites de inmigración y de protección social",
                    "ru": "Иммиграционные формальности и социальное страхование",
                    "pt": "Formalidades de imigração e de proteção social",
                    "it": "Pratiche di immigrazione e di previdenza sociale",
                    "hu": "Bevándorlási és társadalombiztosítási ügyintézés",
                },
                "note": {
                    "en": (
                        "Step also involving an immigration / social security "
                        "provider, to be linked from the agency directory."
                    ),
                    "es": (
                        "Etapa en la que también interviene un proveedor de servicios "
                        "de inmigración / protección social, que debe vincularse desde "
                        "el directorio de la agencia."
                    ),
                    "ru": (
                        "На этом этапе также участвует подрядчик по иммиграции / "
                        "социальному страхованию; контакт следует привязать из "
                        "справочника агентства."
                    ),
                    "pt": (
                        "Etapa que envolve também um prestador de serviços de "
                        "imigração / proteção social, a associar a partir do diretório "
                        "da agência."
                    ),
                    "it": (
                        "Fase che coinvolge anche un fornitore di servizi di "
                        "immigrazione / previdenza sociale, da collegare dalla rubrica "
                        "dell'agenzia."
                    ),
                    "hu": (
                        "Ebben a lépésben egy bevándorlási / társadalombiztosítási "
                        "szolgáltató is közreműködik; a kapcsolatot az iroda "
                        "címtárából kell hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Work permit",
                        "es": "Autorización de trabajo",
                        "ru": "Разрешение на работу",
                        "pt": "Autorização de trabalho",
                        "it": "Autorizzazione al lavoro",
                        "hu": "Munkavállalási engedély",
                    },
                    {
                        "en": "Social security coverage certificate",
                        "es": "Certificado de cobertura de seguridad social",
                        "ru": "Сертификат о социальном страховании",
                        "pt": "Certificado de cobertura social",
                        "it": "Certificato di copertura previdenziale",
                        "hu": "Társadalombiztosítási jogviszony igazolása",
                    },
                    {
                        "en": "Social security registration (international)",
                        "es": "Afiliación a la protección social (internacional)",
                        "ru": "Регистрация в системе социального страхования (международная)",
                        "pt": "Inscrição na proteção social (internacional)",
                        "it": "Iscrizione alla previdenza sociale (internazionale)",
                        "hu": "Társadalombiztosítási bejelentkezés (nemzetközi)",
                    },
                ],
            },
            {
                "name": {
                    "en": "Relocation and local integration",
                    "es": "Instalación e integración local",
                    "ru": "Обустройство и интеграция на месте",
                    "pt": "Instalação e integração local",
                    "it": "Insediamento e integrazione locale",
                    "hu": "Letelepedés és helyi beilleszkedés",
                },
                "note": {
                    "en": (
                        "Step also involving a local relocation provider, to be linked "
                        "from the agency directory."
                    ),
                    "es": (
                        "Etapa en la que también interviene un proveedor local de "
                        "servicios de instalación, que debe vincularse desde el "
                        "directorio de la agencia."
                    ),
                    "ru": (
                        "На этом этапе также участвует местный подрядчик по релокации; "
                        "контакт следует привязать из справочника агентства."
                    ),
                    "pt": (
                        "Etapa que envolve também um prestador local de serviços de "
                        "instalação, a associar a partir do diretório da agência."
                    ),
                    "it": (
                        "Fase che coinvolge anche un fornitore locale di servizi di "
                        "insediamento, da collegare dalla rubrica dell'agenzia."
                    ),
                    "hu": (
                        "Ebben a lépésben egy helyi letelepedést segítő szolgáltató is "
                        "közreműködik; a kapcsolatot az iroda címtárából kell "
                        "hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Lease",
                        "es": "Contrato de arrendamiento",
                        "ru": "Договор аренды",
                        "pt": "Contrato de arrendamento",
                        "it": "Contratto di locazione",
                        "hu": "Bérleti szerződés",
                    },
                    {
                        "en": "Bank account opening",
                        "es": "Apertura de cuenta bancaria",
                        "ru": "Открытие банковского счёта",
                        "pt": "Abertura de conta bancária",
                        "it": "Apertura del conto bancario",
                        "hu": "Bankszámlanyitás",
                    },
                ],
            },
            {
                "name": {
                    "en": "Assignment follow-up and return preparation",
                    "es": "Seguimiento de la misión y preparación del regreso",
                    "ru": "Сопровождение командировки и подготовка к возвращению",
                    "pt": "Acompanhamento da missão e preparação do regresso",
                    "it": "Monitoraggio della missione e preparazione del rientro",
                    "hu": "A kiküldetés nyomon követése és a hazatérés előkészítése",
                },
                "note": {},
                "docs": [
                    {
                        "en": "End-of-assignment review",
                        "es": "Balance de fin de misión",
                        "ru": "Итоговый отчёт о командировке",
                        "pt": "Balanço de fim de missão",
                        "it": "Bilancio di fine missione",
                        "hu": "Kiküldetés-záró értékelés",
                    },
                ],
            },
        ],
    },
    "immigration": {
        "name": {
            "en": "First residence permit application (employee)",
            "es": "Primera solicitud de permiso de residencia (trabajador asalariado)",
            "ru": "Первичное заявление на вид на жительство (наёмный работник)",
            "pt": "Primeiro pedido de título de residência (trabalhador por conta de outrem)",
            "it": "Prima richiesta di permesso di soggiorno (lavoratore dipendente)",
            "hu": "Első tartózkodási engedély iránti kérelem (munkavállaló)",
        },
        "steps": [
            {
                "name": {
                    "en": "Eligibility assessment and choice of permit",
                    "es": "Evaluación de la elegibilidad y elección del permiso",
                    "ru": "Оценка соответствия требованиям и выбор вида разрешения",
                    "pt": "Avaliação da elegibilidade e escolha do título",
                    "it": "Valutazione dei requisiti e scelta del permesso",
                    "hu": "Jogosultság felmérése és az engedélytípus kiválasztása",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Passport",
                        "es": "Pasaporte",
                        "ru": "Паспорт",
                        "pt": "Passaporte",
                        "it": "Passaporto",
                        "hu": "Útlevél",
                    },
                    {
                        "en": "Long-stay entry visa",
                        "es": "Visado de entrada de larga duración",
                        "ru": "Долгосрочная въездная виза",
                        "pt": "Visto de entrada de longa duração",
                        "it": "Visto d'ingresso per lungo soggiorno",
                        "hu": "Hosszú távú tartózkodásra jogosító beutazási vízum",
                    },
                ],
            },
            {
                "name": {
                    "en": "Preparing the application file and collecting documents",
                    "es": "Preparación del expediente y recopilación de documentos",
                    "ru": "Формирование досье и сбор документов",
                    "pt": "Preparação do processo e recolha de documentos",
                    "it": "Preparazione della pratica e raccolta dei documenti",
                    "hu": "A kérelem dokumentációjának összeállítása és az iratok összegyűjtése",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Proof of address",
                        "es": "Justificante de domicilio",
                        "ru": "Подтверждение места жительства",
                        "pt": "Comprovativo de morada",
                        "it": "Attestazione di domicilio",
                        "hu": "Lakcímigazolás",
                    },
                    {
                        "en": "Employment contract",
                        "es": "Contrato de trabajo",
                        "ru": "Трудовой договор",
                        "pt": "Contrato de trabalho",
                        "it": "Contratto di lavoro",
                        "hu": "Munkaszerződés",
                    },
                    {
                        "en": "Work permit",
                        "es": "Autorización de trabajo",
                        "ru": "Разрешение на работу",
                        "pt": "Autorização de trabalho",
                        "it": "Autorizzazione al lavoro",
                        "hu": "Munkavállalási engedély",
                    },
                    {
                        "en": "Translated civil status documents",
                        "es": "Certificados del registro civil traducidos",
                        "ru": "Переведённые акты гражданского состояния",
                        "pt": "Certidões de registo civil traduzidas",
                        "it": "Atti di stato civile tradotti",
                        "hu": "Lefordított anyakönyvi iratok",
                    },
                ],
            },
            {
                "name": {
                    "en": "Submission of the application",
                    "es": "Presentación de la solicitud",
                    "ru": "Подача заявления",
                    "pt": "Apresentação do pedido",
                    "it": "Presentazione della domanda",
                    "hu": "A kérelem benyújtása",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Proof of submission",
                        "es": "Justificante de presentación",
                        "ru": "Подтверждение подачи",
                        "pt": "Comprovativo de apresentação",
                        "it": "Ricevuta di presentazione",
                        "hu": "Benyújtási igazolás",
                    },
                ],
            },
            {
                "name": {
                    "en": "Processing and follow-up with the competent authority",
                    "es": "Tramitación y seguimiento ante la autoridad competente",
                    "ru": "Рассмотрение и взаимодействие с компетентным органом",
                    "pt": "Instrução e acompanhamento junto da autoridade competente",
                    "it": "Istruttoria e monitoraggio presso l'autorità competente",
                    "hu": "A kérelem elbírálása és nyomon követése az illetékes hatóságnál",
                },
                "note": {
                    "en": (
                        "Step handled by the authority responsible for residence "
                        "matters, to be linked from the agency directory."
                    ),
                    "es": (
                        "Etapa a cargo de la autoridad competente en materia de "
                        "residencia, que debe vincularse desde el directorio de la "
                        "agencia."
                    ),
                    "ru": (
                        "Этот этап проводит компетентный орган по вопросам пребывания; "
                        "контакт следует привязать из справочника агентства."
                    ),
                    "pt": (
                        "Etapa a cargo da autoridade competente em matéria de "
                        "residência, a associar a partir do diretório da agência."
                    ),
                    "it": (
                        "Fase a cura dell'autorità competente in materia di soggiorno, "
                        "da collegare dalla rubrica dell'agenzia."
                    ),
                    "hu": (
                        "A lépést a tartózkodási ügyekben illetékes hatóság végzi; a "
                        "kapcsolatot az iroda címtárából kell hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Proof of submission / extension of processing",
                        "es": "Justificante de presentación / de prórroga de la tramitación",
                        "ru": "Подтверждение подачи / продления срока рассмотрения",
                        "pt": "Comprovativo de apresentação / de prorrogação da instrução",
                        "it": "Ricevuta di presentazione / di proroga dell'istruttoria",
                        "hu": "Igazolás a benyújtásról / az eljárás meghosszabbításáról",
                    },
                ],
            },
            {
                "name": {
                    "en": "Decision and appointment notice",
                    "es": "Resolución y citación",
                    "ru": "Решение и приглашение на приём",
                    "pt": "Decisão e convocatória",
                    "it": "Decisione e convocazione",
                    "hu": "Döntés és behívó",
                },
                "note": {
                    "en": (
                        "Step handled by the authority responsible for residence "
                        "matters, to be linked from the agency directory."
                    ),
                    "es": (
                        "Etapa a cargo de la autoridad competente en materia de "
                        "residencia, que debe vincularse desde el directorio de la "
                        "agencia."
                    ),
                    "ru": (
                        "Этот этап проводит компетентный орган по вопросам пребывания; "
                        "контакт следует привязать из справочника агентства."
                    ),
                    "pt": (
                        "Etapa a cargo da autoridade competente em matéria de "
                        "residência, a associar a partir do diretório da agência."
                    ),
                    "it": (
                        "Fase a cura dell'autorità competente in materia di soggiorno, "
                        "da collegare dalla rubrica dell'agenzia."
                    ),
                    "hu": (
                        "A lépést a tartózkodási ügyekben illetékes hatóság végzi; a "
                        "kapcsolatot az iroda címtárából kell hozzárendelni."
                    ),
                },
                "docs": [
                    {
                        "en": "Notice of favorable decision",
                        "es": "Notificación de resolución favorable",
                        "ru": "Уведомление о положительном решении",
                        "pt": "Notificação de decisão favorável",
                        "it": "Notifica di decisione favorevole",
                        "hu": "Kedvező döntésről szóló értesítés",
                    },
                ],
            },
            {
                "name": {
                    "en": "Permit issuance and settling in",
                    "es": "Entrega del permiso e instalación",
                    "ru": "Получение документа и обустройство",
                    "pt": "Entrega do título e instalação",
                    "it": "Consegna del permesso e insediamento",
                    "hu": "Az engedély átvétele és letelepedés",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Residence permit",
                        "es": "Permiso de residencia",
                        "ru": "Вид на жительство",
                        "pt": "Título de residência",
                        "it": "Permesso di soggiorno",
                        "hu": "Tartózkodási engedély",
                    },
                    {
                        "en": "Integration commitment (depending on the host country)",
                        "es": "Compromiso de integración (según el país de acogida)",
                        "ru": "Обязательство об интеграции (в зависимости от принимающей страны)",
                        "pt": "Compromisso de integração (consoante o país de acolhimento)",
                        "it": "Impegno di integrazione (a seconda del paese ospitante)",
                        "hu": "Beilleszkedési kötelezettségvállalás (a fogadó országtól függően)",
                    },
                ],
            },
        ],
    },
    "consulting": {
        "name": {
            "en": "Consulting engagement",
            "es": "Misión de consultoría",
            "ru": "Консалтинговый проект",
            "pt": "Missão de consultoria",
            "it": "Incarico di consulenza",
            "hu": "Tanácsadási megbízás",
        },
        "steps": [
            {
                "name": {
                    "en": "Scoping note and kick-off",
                    "es": "Nota de alcance y lanzamiento",
                    "ru": "Определение рамок и запуск проекта",
                    "pt": "Nota de enquadramento e arranque",
                    "it": "Nota di inquadramento e avvio",
                    "hu": "Hatókör-meghatározás és projektindítás",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Scoping note",
                        "es": "Nota de alcance",
                        "ru": "Документ о рамках проекта",
                        "pt": "Nota de enquadramento",
                        "it": "Nota di inquadramento",
                        "hu": "Hatókör-meghatározó feljegyzés",
                    },
                    {
                        "en": "Engagement letter",
                        "es": "Carta de encargo",
                        "ru": "Договор на оказание услуг",
                        "pt": "Carta de compromisso",
                        "it": "Lettera d'incarico",
                        "hu": "Megbízási levél",
                    },
                ],
            },
            {
                "name": {
                    "en": "Diagnosis / current-state assessment",
                    "es": "Diagnóstico / análisis de la situación actual",
                    "ru": "Диагностика / анализ текущего состояния",
                    "pt": "Diagnóstico / levantamento da situação atual",
                    "it": "Diagnosi / analisi della situazione attuale",
                    "hu": "Diagnózis / helyzetfelmérés",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Diagnostic report",
                        "es": "Informe de diagnóstico",
                        "ru": "Диагностический отчёт",
                        "pt": "Relatório de diagnóstico",
                        "it": "Rapporto di diagnosi",
                        "hu": "Diagnosztikai jelentés",
                    },
                ],
            },
            {
                "name": {
                    "en": "Recommendations / proposed actions",
                    "es": "Recomendaciones / propuestas",
                    "ru": "Рекомендации / предложения",
                    "pt": "Recomendações / propostas",
                    "it": "Raccomandazioni / proposte",
                    "hu": "Ajánlások / javaslatok",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Recommendations report",
                        "es": "Informe de recomendaciones",
                        "ru": "Отчёт с рекомендациями",
                        "pt": "Relatório de recomendações",
                        "it": "Rapporto di raccomandazioni",
                        "hu": "Ajánlásokat tartalmazó jelentés",
                    },
                ],
            },
            {
                "name": {
                    "en": "Rollout / implementation",
                    "es": "Despliegue / implementación",
                    "ru": "Внедрение / реализация",
                    "pt": "Implantação / execução",
                    "it": "Implementazione / attuazione",
                    "hu": "Bevezetés / megvalósítás",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Action plan",
                        "es": "Plan de acción",
                        "ru": "План действий",
                        "pt": "Plano de ação",
                        "it": "Piano d'azione",
                        "hu": "Cselekvési terv",
                    },
                ],
            },
            {
                "name": {
                    "en": "Steering committee and monitoring",
                    "es": "Comité de dirección y seguimiento",
                    "ru": "Управляющий комитет и мониторинг",
                    "pt": "Comité de pilotagem e acompanhamento",
                    "it": "Comitato di pilotaggio e monitoraggio",
                    "hu": "Irányítóbizottság és nyomon követés",
                },
                "note": {},
                "docs": [
                    {
                        "en": "Steering committee minutes",
                        "es": "Acta del comité de dirección",
                        "ru": "Протокол заседания управляющего комитета",
                        "pt": "Ata do comité de pilotagem",
                        "it": "Verbale del comitato di pilotaggio",
                        "hu": "Irányítóbizottsági ülés jegyzőkönyve",
                    },
                ],
            },
            {
                "name": {
                    "en": "Closure and engagement review",
                    "es": "Cierre y balance de la misión",
                    "ru": "Завершение и подведение итогов проекта",
                    "pt": "Encerramento e balanço da missão",
                    "it": "Chiusura e bilancio dell'incarico",
                    "hu": "Lezárás és a megbízás értékelése",
                },
                "note": {},
                "docs": [
                    {
                        "en": "End-of-engagement review",
                        "es": "Balance de fin de misión",
                        "ru": "Итоговый отчёт по проекту",
                        "pt": "Balanço de fim de missão",
                        "it": "Bilancio di fine incarico",
                        "hu": "Megbízás-záró értékelés",
                    },
                ],
            },
        ],
    },
}

DEMO_I18N: dict[str, dict[str, Any]] = {
    "real_estate": {
        "last_name": {
            "en": "Property - Residence Horizon",
            "es": "Inmueble - Residence Horizon",
            "ru": "Объект недвижимости - Residence Horizon",
            "pt": "Imóvel - Residence Horizon",
            "it": "Immobile - Residence Horizon",
            "hu": "Ingatlan - Residence Horizon",
        },
        "text_fields": {},
    },
    "wealth": {
        "last_name": {
            "en": "Wealth client - A. Meyer",
            "es": "Cliente patrimonial - A. Meyer",
            "ru": "Клиент по управлению капиталом - A. Meyer",
            "pt": "Cliente patrimonial - A. Meyer",
            "it": "Cliente patrimoniale - A. Meyer",
            "hu": "Vagyonkezelési ügyfél - A. Meyer",
        },
        "text_fields": {
            "held_products": {
                "en": "Life insurance, securities accounts, rental property",
                "es": "Seguro de vida, cuentas de valores, inmuebles en alquiler",
                "ru": "Страхование жизни, счета ценных бумаг, недвижимость для сдачи в аренду",
                "pt": "Seguro de vida, contas de títulos, imóveis para arrendamento",
                "it": "Assicurazione vita, conti titoli, immobili in locazione",
                "hu": "Életbiztosítás, értékpapírszámlák, bérbe adott ingatlanok",
            },
        },
    },
    "consulting": {
        "last_name": {
            "en": "Consulting client - Nord Digital",
            "es": "Cliente de consultoría - Nord Digital",
            "ru": "Клиент консалтинга - Nord Digital",
            "pt": "Cliente de consultoria - Nord Digital",
            "it": "Cliente di consulenza - Nord Digital",
            "hu": "Tanácsadási ügyfél - Nord Digital",
        },
        "text_fields": {
            "expected_deliverables": {
                "en": (
                    "Diagnostic report, prioritized roadmap, presentation to the steering committee"
                ),
                "es": (
                    "Informe de diagnóstico, hoja de ruta priorizada, presentación de "
                    "resultados al comité de dirección"
                ),
                "ru": (
                    "Диагностический отчёт, приоритизированная дорожная карта, "
                    "презентация результатов управляющему комитету"
                ),
                "pt": (
                    "Relatório de diagnóstico, roteiro com prioridades, apresentação "
                    "de resultados ao comité de pilotagem"
                ),
                "it": (
                    "Rapporto di diagnosi, roadmap con priorità, presentazione dei "
                    "risultati al comitato di pilotaggio"
                ),
                "hu": (
                    "Diagnosztikai jelentés, prioritások szerint rendezett ütemterv, "
                    "eredmények bemutatása az irányítóbizottságnak"
                ),
            },
        },
    },
    "legal": {
        "last_name": {
            "en": "Litigation case - Voss / Delta Trading",
            "es": "Expediente contencioso - Voss / Delta Trading",
            "ru": "Судебное дело - Voss / Delta Trading",
            "pt": "Processo contencioso - Voss / Delta Trading",
            "it": "Pratica di contenzioso - Voss / Delta Trading",
            "hu": "Peres ügy - Voss / Delta Trading",
        },
        "text_fields": {},
    },
    "accounting": {
        "last_name": {
            "en": "Accounting client - Brightwave GmbH",
            "es": "Cliente de contabilidad - Brightwave GmbH",
            "ru": "Клиент по бухгалтерскому учёту - Brightwave GmbH",
            "pt": "Cliente de contabilidade - Brightwave GmbH",
            "it": "Cliente di contabilità - Brightwave GmbH",
            "hu": "Könyvelési ügyfél - Brightwave GmbH",
        },
        "text_fields": {},
    },
    "hr_mobility": {
        "last_name": {
            "en": "Employee on assignment - J. Almeida",
            "es": "Empleado en movilidad - J. Almeida",
            "ru": "Командируемый сотрудник - J. Almeida",
            "pt": "Trabalhador em mobilidade - J. Almeida",
            "it": "Dipendente in mobilità - J. Almeida",
            "hu": "Kiküldött munkavállaló - J. Almeida",
        },
        "text_fields": {
            "job_title": {
                "en": "Senior software engineer",
                "es": "Ingeniero de software sénior",
                "ru": "Старший инженер-программист",
                "pt": "Engenheiro de software sénior",
                "it": "Ingegnere software senior",
                "hu": "Szenior szoftvermérnök",
            },
        },
    },
    "immigration": {
        "last_name": {
            "en": "Applicant - S. Okoro",
            "es": "Solicitante - S. Okoro",
            "ru": "Заявитель - S. Okoro",
            "pt": "Requerente - S. Okoro",
            "it": "Richiedente - S. Okoro",
            "hu": "Kérelmező - S. Okoro",
        },
        "text_fields": {},
    },
}

DEMO_COMMON_I18N: dict[str, dict[str, str]] = {
    "source": {
        "fr": "Dossier d'exemple",
        "en": "Example case",
        "es": "Expediente de ejemplo",
        "ru": "Пример дела",
        "pt": "Processo de exemplo",
        "it": "Pratica di esempio",
        "hu": "Példaügy",
    },
    "tag": {
        "fr": "exemple",
        "en": "example",
        "es": "ejemplo",
        "ru": "пример",
        "pt": "exemplo",
        "it": "esempio",
        "hu": "példa",
    },
    "profession": {
        "fr": "Client exemple",
        "en": "Example client",
        "es": "Cliente de ejemplo",
        "ru": "Клиент для примера",
        "pt": "Cliente de exemplo",
        "it": "Cliente di esempio",
        "hu": "Példaügyfél",
    },
}
