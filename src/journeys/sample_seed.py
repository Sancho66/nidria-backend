"""Library SAMPLE journeys, seeded at boot (idempotent), like the system
roles: agency_id NULL + is_sample=true → shared, read-only for agencies, an
agency consumes one by CLONING it.

CONSTRAINT: on an agency-less sample, the only NAMEABLE participant is the
client (type=expat) — a type=agent participant needs an agent_id and a sample
has no agency, hence no agents. So agency / provider doers (escribano, Muhtar,
sworn translator…) are carried as a content_note "à assigner au dossier"; the
agency names them on the CLONE. The validator is "the agency"
(validated_by_type='agent', agent_id NULL = "agency in general"). Amounts and
delays are indicative, never a rule.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.journey import (
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateStep,
    StepPrerequisite,
)
from shared.models.step_requirement import StepRequirement

# A step: (name, estimated_days | None, content_note, client_role | None,
# [doc labels]). estimated_days None ⇒ open-ended (e.g. a multi-year backlog
# wait). client_role None ⇒ an agency/provider doer (a content_note, no
# participant on the sample). Steps form a linear AND chain (each requires the
# previous). The validator is the agency on every step.
type _Step = tuple[str, int | None, str, str | None, list[str]]

PY1_NAME = "Paraguay — Résidence temporaire + Cédula"
PY1_COUNTRY = "PY"
RUC_NAME = "Paraguay — Création de société (RUC)"
RUC_COUNTRY = "PY"
CY_NAME = "Chypre — Enregistrement de résidence UE (Yellow Slip, MEU1)"
CY_COUNTRY = "CY"
PERM_NAME = "Paraguay — Résidence permanente (changement de catégorie)"
PERM_COUNTRY = "PY"
CYF_NAME = "Chypre — Résidence hors-UE revenus passifs (Pink Slip + Catégorie F)"
CYF_COUNTRY = "CY"
DNV_NAME = "Chypre — Digital Nomad Visa (hors-UE)"
DNV_COUNTRY = "CY"
LTD_NAME = "Chypre — Création de société (LTD)"
LTD_COUNTRY = "CY"
FIC_NAME = "Chypre — Société LTD + permis dirigeant hors-UE (FIC/BFU)"
FIC_COUNTRY = "CY"
PA_FN_NAME = "Panama — Résidence Nations Amies (Friendly Nations)"
PA_PEN_NAME = "Panama — Visa Pensionado (retraité)"
PA_GV_NAME = "Panama — Investisseur Qualifié (Golden Visa)"
PA_DN_NAME = "Panama — Visa nomade numérique (Trabajador Remoto)"
PA_CO_NAME = "Panama — Création de société (S.A. / SRL)"

_PY1_STEPS: list[_Step] = [
    (
        "Constitution du dossier",
        15,
        "Réunissez les pièces : acte de naissance apostillé, casier judiciaire "
        "apostillé, passeport valide. L'apostille se demande auprès de l'autorité "
        "compétente de votre pays d'origine.",
        "provides_documents",
        ["Acte de naissance apostillé", "Casier judiciaire apostillé", "Passeport"],
    ),
    (
        "Traduction assermentée des documents",
        7,
        "Traduction par un traducteur assermenté inscrit. À assigner au dossier : "
        "le prestataire externe se nomme sur le dossier, pas sur ce modèle partagé.",
        "provides_documents",
        [],
    ),
    (
        "Dépôt du dossier à l'immigration (DNM)",
        10,
        "Dépôt effectué par l'agence auprès de la Dirección Nacional de Migraciones. "
        "Taxe DNM ≈ 2 700 000 Gs (montant indicatif, non figé).",
        None,
        [],
    ),
    (
        "Obtention de la résidence temporaire",
        45,
        "Délai administratif de la DNM, variable (≈ 30 à 45 jours, indicatif).",
        None,
        [],
    ),
    (
        "Demande de la cédula (carte d'identité)",
        20,
        "Prise d'empreintes et photo au bureau d'identification. Étape débloquée "
        "une fois la résidence temporaire obtenue.",
        "provides_documents",
        ["Photo d'identité"],
    ),
    (
        "Remise de la cédula",
        90,
        "Délai de fabrication de la cédula, variable (≈ 3 à 9 mois, indicatif).",
        None,
        [],
    ),
]

# Paraguay — Création de société, voie rapide EAS (Empresa por Acciones
# Simplificadas, Loi 6480/2020).
_RUC_STEPS: list[_Step] = [
    (
        "Préparation identité électronique & statut",
        5,
        "EAS : pas de capital minimum ni de dépôt. Si l'étranger n'a pas de cédula "
        "PY, il constitue via un représentant légal qui en a une.",
        "provides_documents",
        [
            "Cédula paraguayenne du représentant légal",
            "Statut (proforma généré OU personnalisé avec certification de firme par escribano)",
            "Pouvoir spécial apostillé et traduit (si donné à l'étranger)",
        ],
    ),
    (
        "Constitution en ligne via SUACE (eas.mic.gov.py)",
        3,
        "Constitution en 72 h (souvent 24–48 h) avec statut proforma ; ≈ 8 jours "
        "ouvrables avec statut personnalisé. L'escribano peut réaliser l'étape — "
        "à assigner au dossier.",
        "executant",
        [
            "Formulaire unique SUACE",
            "Documents joints",
            "Signature (firme digitale par token OU firme manuscrite scannée)",
        ],
    ),
    (
        "Inscriptions automatiques (RUC / IPS / MTESS)",
        2,
        "L'inscription génère automatiquement le RUC (Finances), l'IPS (sécurité "
        "sociale) et le MTESS (travail). Pas d'inscription au Registro Público de "
        "Comercio nécessaire pour opérer.",
        "executant",
        [],
    ),
]

# Chypre — Enregistrement de résidence d'un citoyen UE/EEE/Suisse (Yellow Slip,
# formulaire MEU1) pour un séjour > 3 mois.
_CY_STEPS: list[_Step] = [
    (
        "Réunir les documents (formulaire MEU1)",
        14,
        "Demande à déposer dans les 4 mois suivant l'entrée. Les relevés de banques "
        "fintech (Revolut, Wise, N26) peuvent être refusés.",
        "provides_documents",
        [
            "Formulaire MEU1",
            "Passeport / CNI + copie",
            "Preuve d'adresse (bail certifié par un Muhtar + timbré au fisc)",
            "Justificatif d'emploi (lettre employeur) OU ressources suffisantes + assurance santé",
        ],
    ),
    (
        "Rendez-vous au CRMD (Immigration Unit de district)",
        21,
        "Bureaux de Nicosie / Limassol / Larnaca / Paphos. Réserver ≈ 3 à 4 semaines à l'avance.",
        "executant",
        [],
    ),
    (
        "Dépôt en personne + délivrance du certificat",
        7,
        "Présence requise (photo sur place). Certificat souvent émis le jour même ou "
        "sous quelques jours ; il n'expire pas. Montant indicatif (une source cite "
        "85 €, à confirmer au guichet).",
        "executant",
        ["Dossier MEU1 complet", "Paiement de la redevance (≈ 20 €, indicatif)"],
    ),
]

# Paraguay — Résidence permanente (changement de catégorie), suite logique de
# PY-1 après ≈ 2 ans de temporaire (parcours séparé, non inliné).
_PERM_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité et le timing",
        7,
        "Déposer dans les 90 jours précédant l'expiration du carnet temporaire de "
        "2 ans (possible jusqu'à 1 mois après expiration, avec amende). Ne pas "
        "s'être absenté plus d'un an cumulé sur les 2 ans. Aucune exigence "
        "d'investissement pour la conversion.",
        None,  # réalisé par l'agence — non nommable sur un sample (content_note)
        [],
    ),
    (
        "Constituer le dossier de changement de catégorie",
        21,
        "Réunir les pièces du changement de catégorie. Les preuves de solvabilité "
        "diffèrent : contrat de travail (salariés) ou actes de société + registre "
        "d'actionnaires (entrepreneurs).",
        "provides_documents",
        [
            "Carnet de résidence temporaire",
            "Informe de movimiento migratorio (absence < 1 an cumulé)",
            "Cédula paraguayenne",
            "Certificats d'antécédents (Interpol Paraguay, police, « vie et résidence »)",
            (
                "Preuves de solvabilité (contrat de travail OU actes de société "
                "+ registre d'actionnaires)"
            ),
        ],
    ),
    (
        "Dépôt à la DNM",
        30,
        "Dépôt en personne à la Dirección Nacional de Migraciones.",
        "executant",
        [],
    ),
    (
        "Émission du carnet permanent + renouvellement de la cédula",
        30,
        "Carnet permanent définitif, à renouveler tous les 10 ans. Le résident "
        "permanent ne doit pas s'absenter plus de 3 ans consécutifs sans "
        "justification. Conversion accessible après ≈ 21 à 24 mois de temporaire.",
        "executant",
        [],
    ),
]

# Chypre — résidence hors-UE à revenus passifs (retraité/rentier). Le Pink Slip
# couvre la résidence annuelle pendant l'instruction (très longue) de la
# Catégorie F (résidence permanente).
_CYF_STEPS: list[_Step] = [
    (
        "Préparation & entrée légale à Chypre",
        14,
        "Revenu étranger ≈ 24 000 €/an pour le Pink Slip (+20 % conjoint, "
        "+15 %/enfant). Demande à déposer ≈ 7 jours après l'arrivée. Relevés de "
        "banques fintech (Revolut/Wise/N26) parfois refusés. Montants indicatifs.",
        "provides_documents",
        [
            "Passeport",
            "Preuves de revenus étrangers stables",
            "Bail / titre de propriété",
            "Attestation de dépôt bancaire à Chypre (≈ 10 000 €, indicatif)",
        ],
    ),
    (
        "Examen médical à Chypre",
        7,
        "Tests hépatites B/C, VIH, syphilis + radio tuberculose ; certificat "
        "< 4 mois. Assurance santé requise.",
        "executant",
        [],
    ),
    (
        "Dépôt du Pink Slip (titre de résidence annuel)",
        180,
        "Le récépissé fait foi de séjour légal pendant l'instruction. Valable 1 an, "
        "renouvelable. Le numéro ARC reste identique tout du long.",
        "executant",
        [
            "Formulaire",
            "Passeport",
            "Certificat médical",
            "Casier judiciaire (< 6 mois)",
            "Preuves de revenus",
            "Bail",
            "Attestation bancaire",
            "Paiement 70 € + 70 € (indicatif)",
        ],
    ),
    (
        "Dépôt de la Catégorie F (résidence permanente)",
        30,
        "🟠 Seuils réglementaires (relevés en 2023). À déposer tôt : l'instruction "
        "est très longue.",
        "executant",
        [
            "Passeports",
            "CV",
            "Certificats mariage/naissance apostillés",
            "Casier traduit / apostillé",
            ("Preuves de revenus étrangers (≥ 9 568,17 € + 4 613,22 €/dépendant, indicatif)"),
            "Preuve de logement",
            "Dépôt bancaire non gagé (15–20 000 €, indicatif)",
        ],
    ),
    (
        "Attente & renouvellement annuel du Pink Slip (backlog Catégorie F)",
        None,
        "🔴 Backlog Catégorie F estimé 5–7 ans (dossiers de 2020 encore en cours). "
        "Renouveler le Pink Slip CHAQUE année jusqu'à délivrance de la PR. Ne "
        "jamais promettre une PR rapide par cette voie.",
        "executant",
        [],
    ),
    (
        "Délivrance de la Catégorie F (résidence permanente)",
        None,
        "Permis permanent, carte à renouveler tous les 10 ans.",
        "executant",
        [],
    ),
]

# Chypre — Digital Nomad Visa (hors-UE), travail à distance pour des
# employeurs/clients HORS Chypre.
_DNV_STEPS: list[_Step] = [
    (
        "Vérifier la disponibilité du quota AVANT toute démarche",
        3,
        "🔴 CRITIQUE. Quota officiel = 500 permis, atteint dès 2023 ; le « 1 000 » "
        "n'est PAS confirmé. Vérifier la disponibilité réelle auprès du Deputy "
        "Ministry of Migration AVANT toute promesse client.",
        None,  # réalisé par l'agence — non nommable sur un sample (content_note)
        [],
    ),
    (
        "Réunir les documents & entrer à Chypre",
        14,
        "Demande dans les 3 mois suivant l'entrée. Montant indicatif.",
        "provides_documents",
        [
            "Formulaire",
            "Passeport (validité ≥ 3 mois)",
            "CV",
            "Contrat d'emploi / preuve d'auto-emploi (entité hors Chypre)",
            "Relevés bancaires (revenu net ≥ 3 500 €/mois, +20 % conjoint, +15 %/enfant)",
            "Casier judiciaire",
            "Assurance santé",
            "Preuve de logement",
            "Tests médicaux",
        ],
    ),
    (
        "Dépôt au CRMD (Nicosie) + biométrie",
        49,
        "Traitement ≈ 5 à 7 semaines.",
        "executant",
        ["Dossier complet", "Paiement 70 € + 70 € (indicatif)"],
    ),
    (
        "Délivrance du permis DNV",
        None,
        "Permis 1 an, renouvelable jusqu'à 2 ans. ⚠️ Le temps passé en DNV ne compte "
        "PAS pour la naturalisation. Au-delà de 183 j/an = résidence fiscale chypriote.",
        "executant",
        [],
    ),
]

# Chypre — Création de société (LTD), constitution possible à distance.
_LTD_STEPS: list[_Step] = [
    (
        "Approbation du nom & KYC",
        5,
        "Avocat chypriote obligatoire pour la constitution.",
        "provides_documents",
        [
            "2 à 3 noms alternatifs",
            "KYC des bénéficiaires (passeports certifiés, domicile, référence bancaire non-UE)",
        ],
    ),
    (
        "Rédaction des statuts (Memorandum & Articles of Association)",
        5,
        "≥ 1 administrateur (un administrateur résident aide à la substance "
        "fiscale), 1 secrétaire (≠ administrateur unique), siège à Chypre. Pas de "
        "capital minimum (1 000 € usuel).",
        "executant",
        [],
    ),
    (
        "Dépôt au Registrar of Companies & émission des certificats",
        10,
        "Certificats d'incorporation / administrateurs / actionnaires / siège.",
        "executant",
        ["Statuts", "Paiement des frais d'État (≈ 165 €, indicatif)"],
    ),
    (
        "Enregistrement fiscal & compte bancaire",
        60,
        "TIN sous 60 j, TVA si applicable, registre UBO, ouverture de compte. "
        "IS 15 % (depuis 1/1/2026) ; dividendes ≈ 2,65 % effectif en non-dom. Coûts "
        "récurrents ≈ 2 800–4 500 €/an (audit obligatoire). Chiffres indicatifs.",
        "executant",
        [],
    ),
]

# Chypre — LTD + permis dirigeant hors-UE via FIC/BFU (Foreign Interest Company
# / Business Facilitation Unit) : la voie qui débloque le permis de travail du
# dirigeant hors-UE.
_FIC_STEPS: list[_Step] = [
    (
        "Constitution de la LTD",
        15,
        "Voir le parcours « Création de société (LTD) » pour le détail (nom, "
        "statuts, Registrar). La société est le préalable au statut FIC/BFU.",
        "executant",
        [],
    ),
    (
        "Enregistrement FIC/BFU (Foreign Interest Company)",
        21,
        "🟠 Dépôt 200 000 €, bureaux indépendants requis. Ratio d'emploi local "
        "70:30 évalué dès le 2/1/2027. Seuils indicatifs.",
        "executant",
        [
            "Preuve du dépôt de 200 000 € (capital étranger)",
            "Justificatif de bureaux physiques indépendants à Chypre",
            "Documents de la société",
        ],
    ),
    (
        "Demande du permis de séjour + travail du dirigeant",
        30,
        "Via la BFU, PAS de test du marché de l'emploi → permis ≈ 1 mois. Le "
        "salaire ≥ 2 500 €/mois ouvre aussi l'éligibilité à la naturalisation "
        "accélérée (3 ans grec B1 / 4 ans A2).",
        "executant",
        [
            "Contrat de travail (salaire clé ≥ 2 500 €/mois)",
            "Passeport",
            "Casier judiciaire",
            "Diplômes",
            "Dossier société FIC",
        ],
    ),
    (
        "Délivrance du permis & démarrage d'activité",
        None,
        "Permis renouvelable. Fiscalité : IS 15 %, dividendes ≈ 2,65 % non-dom, "
        "exonération 50 % IR si salaire > 55 000 €/an.",
        "executant",
        [],
    ),
]

# Panama — Résidence Nations Amies (ressortissant d'un des ~50 pays « amis »).
_PA_FN_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité (nationalité amie) & préparer le dossier",
        14,
        "🟠 La liste des ~50 pays amis est modifiable par décret — revérifier sur "
        "migracion.gob.pa avant le dossier. Avocat panaméen obligatoire.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [
            "Passeport",
            "Casier judiciaire apostillé (< 6 mois)",
            "Preuve du lien économique (emploi, immobilier ou dépôt à terme ≥ 200 000 USD)",
        ],
    ),
    (
        "Entrée au Panama & dépôt de la résidence provisoire (SNM)",
        30,
        "Carte de résident provisoire (6 mois pendant l'instruction).",
        "executant",
        [
            "Pouvoir notarié",
            "3 photos",
            "Copie cotejada du passeport",
            "Casier apostillé",
            "Certificat de santé",
            "Paiement 250 USD (Tesoro) + 800 USD (dépôt rapatriement SNM)",
        ],
    ),
    (
        "Octroi de la résidence provisoire (2 ans)",
        180,
        "🟠 Depuis 2021, les Nations Amies ne donnent plus la permanente immédiate : "
        "résidence PROVISOIRE de 2 ans d'abord, et sans droit de travailler "
        "(permis MITRADEL = démarche séparée).",
        "executant",
        [],
    ),
    (
        "Demande de résidence permanente (après 2 ans)",
        180,
        "Instruction jusqu'à 6 mois.",
        "executant",
        ["Documents mis à jour", "Résidence provisoire"],
    ),
    (
        "Cédula E (Tribunal Electoral)",
        15,
        "Carte d'identité de résident permanent. Renouvellement tous les 10 ans.",
        "executant",
        ["Passeport", "Résolution SNM", "Nota de cédula", "Paiement 100 USD"],
    ),
]

# Panama — Visa Pensionado (retraité à pension à vie, toutes nationalités).
_PA_PEN_STEPS: list[_Step] = [
    (
        "Préparation du dossier (via avocat)",
        14,
        "Pension ≥ 1 000 USD/mois (ou ≥ 750 USD/mois AVEC immobilier panaméen "
        "≥ 100 000 USD). +250 USD/mois par personne à charge. Travail interdit "
        "sous ce statut. Seuils indicatifs.",
        "provides_documents",
        [
            "Lettre de pension certifiant le caractère « à vie », apostillée",
            "Passeport",
            "Casier apostillé",
            "Certificat de santé",
        ],
    ),
    (
        "Dépôt de la demande au SNM",
        30,
        "Les pensionados sont souvent exemptés du dépôt de rapatriement.",
        "executant",
        [],
    ),
    (
        "Octroi de la résidence permanente",
        120,
        "Résidence permanente DIRECTE (pas de période provisoire). Avantages : carte "
        "de réductions pensionado (transports, santé, loisirs).",
        "executant",
        [],
    ),
    (
        "Cédula E (Tribunal Electoral)",
        15,
        "Carte d'identité de résident permanent.",
        "executant",
        ["Passeport", "Résolution SNM", "Nota de cédula", "Paiement 100 USD"],
    ),
]

# Panama — Investisseur Qualifié (Golden Visa), toutes nationalités, PR rapide.
_PA_GV_STEPS: list[_Step] = [
    (
        "Préparation & choix du véhicule d'investissement",
        14,
        "🔴 SEUIL VOLATIL CRITIQUE. Immobilier ≥ 300 000 USD dans une fenêtre "
        "annoncée jusqu'à octobre 2026, puis remontée probable à 500 000 USD. "
        "Alternatives : titres en bourse panaméenne ≥ 500 000 USD, ou dépôt à terme "
        "≥ 750 000 USD (5 ans). VÉRIFIER le seuil en vigueur sur migracion.gob.pa.",
        "provides_documents",
        ["Passeport apostillé", "Casier apostillé", "5 photos", "Certificat de santé"],
    ),
    (
        "Réalisation de l'investissement (transfert depuis l'étranger)",
        30,
        "Fonds d'origine étrangère, via canaux bancaires.",
        "executant",
        [],
    ),
    (
        "Dépôt de la demande au SNM",
        45,
        "Octroi de la résidence permanente en 30 à 45 jours ouvrables.",
        "executant",
        [
            "Preuve d'investissement",
            "Paiement 5 000 USD (SNM) + 5 000 USD (Tesoro) (+ 1 000 USD/dépendant)",
        ],
    ),
    (
        "Cédula E (Tribunal Electoral)",
        15,
        "⚠️ Le Golden Visa n'impose aucune présence pour garder la résidence, mais la "
        "naturalisation exige une résidence effective — à arbitrer avec l'avocat.",
        "executant",
        ["Passeport", "Résolution SNM", "Nota de cédula", "Paiement 100 USD"],
    ),
]

# Panama — Visa nomade numérique (Trabajador Remoto), court terme, non résident.
_PA_DN_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité & réunir le dossier",
        14,
        "Revenu de source étrangère ≥ 36 000 USD/an. Seuil indicatif.",
        "provides_documents",
        [
            "Passeport",
            "Contrat avec entreprise étrangère OU preuve d'activité indépendante",
            "Lettre employeur (revenu ≥ 3 000 USD/mois)",
            "Certificat d'existence de l'entreprise étrangère",
            "Assurance santé couvrant le Panama",
            "Casier apostillé",
            "Déclaration sous serment de non-concurrence",
        ],
    ),
    (
        "Entrée au Panama & dépôt à la Ventanilla de Trámites Especiales (SNM)",
        20,
        "Dépôt à la Ventanilla de Trámites Especiales du SNM.",
        "executant",
        [
            "Pouvoir notarié",
            "3 photos",
            "Copie cotejada du passeport",
            "Certificat de santé",
            "Paiement 250 USD (SNM)",
        ],
    ),
    (
        "Émission du carné de nomade numérique",
        None,
        "⚠️ 9 mois, renouvelable une fois (18 mois max). Catégorie NON résident : ne "
        "mène NI à la résidence NI à la naturalisation. Pour une installation "
        "durable, basculer vers Nations Amies / Pensionado / Golden Visa.",
        "executant",
        [],
    ),
]

# Panama — Création de société (S.A. / SRL). Toutes nationalités (détention
# 100 % possible) ; la détention seule ne donne PAS le droit de travailler.
_PA_CO_STEPS: list[_Step] = [
    (
        "Qualifier l'activité & choisir la structure",
        5,
        "🔴 PIÈGE DU DÉTAIL (art. 293) : le commerce au détail au consommateur "
        "panaméen (boutique, e-commerce B2C local, distribution, franchise) est "
        "FERMÉ à un actionnaire étranger (ni même directeur). Ouverts : B2B, "
        "conseil, gros, import-export, SaaS/tech, holding, clients étrangers. "
        "Qualifier AVANT de créer. S.A. = 1 associé, propriétaires confidentiels ; "
        "SRL = 2 associés min, associés publics.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Rédaction du pacte social (avocat)",
        5,
        "S.A. = conseil d'au moins 3 administrateurs (peuvent être étrangers / non-résidents).",
        "executant",
        [],
    ),
    (
        "Inscription au Registre public du Panama",
        7,
        "Société constituée en 3 à 7 jours.",
        "executant",
        [],
    ),
    (
        "Aviso de Operación & inscription fiscale (RUC / DGI)",
        7,
        "Capital investi < 10 000 USD → exonéré d'IAO ; au-delà, IAO = 2 % du capital "
        "net (min 100 / max 60 000 USD/an), uniquement si activité au Panama. "
        "Inscription CSS si embauche. Montants indicatifs.",
        "executant",
        [],
    ),
    (
        "Permis de travail MITRADEL (si le dirigeant travaille dans la société)",
        30,
        "🟠 Démarche SÉPARÉE de la résidence. Quotas : max 10 % de personnel étranger "
        "ordinaire / 15 % spécialisé. ~56 professions réservées aux nationaux "
        "(médecine, droit, ingénierie, comptabilité, architecture…) restent "
        "interdites tant que non naturalisé. Détenir/superviser depuis l'étranger = "
        "aucun permis ; travailler sur place = ce permis.",
        "executant",
        [],
    ),
]

# (name, country, steps) — every library sample, seeded idempotently by name.
_SAMPLES: list[tuple[str, str, list[_Step]]] = [
    (PY1_NAME, PY1_COUNTRY, _PY1_STEPS),
    (RUC_NAME, RUC_COUNTRY, _RUC_STEPS),
    (CY_NAME, CY_COUNTRY, _CY_STEPS),
    (PERM_NAME, PERM_COUNTRY, _PERM_STEPS),
    (CYF_NAME, CYF_COUNTRY, _CYF_STEPS),
    (DNV_NAME, DNV_COUNTRY, _DNV_STEPS),
    (LTD_NAME, LTD_COUNTRY, _LTD_STEPS),
    (FIC_NAME, FIC_COUNTRY, _FIC_STEPS),
    (PA_FN_NAME, "PA", _PA_FN_STEPS),
    (PA_PEN_NAME, "PA", _PA_PEN_STEPS),
    (PA_GV_NAME, "PA", _PA_GV_STEPS),
    (PA_DN_NAME, "PA", _PA_DN_STEPS),
    (PA_CO_NAME, "PA", _PA_CO_STEPS),
]


async def _seed_one(db: AsyncSession, name: str, country: str, steps: list[_Step]) -> None:
    """Idempotent: keyed on (agency_id IS NULL, is_sample, name). If it exists,
    only refresh the country in place (no duplicate, no re-create)."""
    existing = (
        await db.execute(
            select(JourneyTemplate).where(
                JourneyTemplate.agency_id.is_(None),
                JourneyTemplate.is_sample.is_(True),
                JourneyTemplate.name == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.country != country:
            existing.country = country
            await db.commit()
        return

    tpl = JourneyTemplate(
        id=uuid.uuid4(), agency_id=None, is_sample=True, name=name, country=country
    )
    db.add(tpl)
    await db.flush()  # template before its children FK it

    step_ids: list[uuid.UUID] = []
    for position, (step_name, days, note, _role, _docs) in enumerate(steps):
        sid = uuid.uuid4()
        step_ids.append(sid)
        db.add(
            JourneyTemplateStep(
                id=sid,
                template_id=tpl.id,
                name=step_name,
                position=position,
                estimated_days=days,
                content_note=note,
                default_validated_by_type="agent",  # validé par l'agence
            )
        )
    await db.flush()  # steps before prerequisites / participants / requirements

    # Linear AND chain: step i requires step i-1.
    for i in range(1, len(step_ids)):
        db.add(StepPrerequisite(step_id=step_ids[i], prerequisite_step_id=step_ids[i - 1]))

    for i, (_step_name, _days, _note, role, docs) in enumerate(steps):
        if role is not None:
            db.add(
                JourneyStepParticipant(step_id=step_ids[i], type="expat", agent_id=None, role=role)
            )
        for position, label in enumerate(docs):
            db.add(
                StepRequirement(
                    step_id=step_ids[i],
                    kind="document",
                    reference=label,
                    scope="principal",
                    position=position,
                )
            )
    await db.commit()


async def seed_sample_journeys(db: AsyncSession) -> None:
    """Seed every library sample (idempotent, relaunchable, no duplicate)."""
    for name, country, steps in _SAMPLES:
        await _seed_one(db, name, country, steps)
