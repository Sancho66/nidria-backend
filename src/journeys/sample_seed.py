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
BG_EU_NAME = "Bulgarie — Enregistrement de résidence UE"
BG_RET_NAME = "Bulgarie — Résidence retraité hors-UE"
BG_DN_NAME = "Bulgarie — Visa nomade digital (hors-UE)"
BG_FL_NAME = "Bulgarie — Freelance / profession libérale (hors-UE)"
BG_CO_NAME = "Bulgarie — Création de société (EOOD / OOD)"
HU_EU_NAME = "Hongrie — Enregistrement de séjour UE"
HU_WC_NAME = "Hongrie — White Card (nomade digital, hors-UE)"
HU_GI_NAME = "Hongrie — Guest Investor (golden visa, hors-UE)"
HU_SP_NAME = "Hongrie — Autorisation unique (salarié hors-UE)"
HU_CO_NAME = "Hongrie — Création de société (Kft.)"
AE_GV_NAME = "Dubaï (EAU) — Golden Visa (10 ans)"
AE_FZ_NAME = "Dubaï (EAU) — Résidence par société free zone"
AE_RE_NAME = "Dubaï (EAU) — Visa immobilier (2 ans)"
AE_RW_NAME = "Dubaï (EAU) — Visa remote work (1 an)"
AE_RET_NAME = "Dubaï (EAU) — Visa retraité (5 ans, 55 ans et +)"
AE_CO_NAME = "Dubaï (EAU) — Création de société (free zone / mainland)"

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

# Bulgarie — enregistrement de résidence d'un citoyen UE/EEE/Suisse (> 3 mois).
_BG_EU_STEPS: list[_Step] = [
    (
        "Enregistrer l'adresse à la municipalité",
        5,
        "Enregistrement de l'adresse de résidence auprès de la municipalité.",
        "provides_documents",
        ["Passeport / CNI", "Preuve de logement"],
    ),
    (
        "Demande de certificat de séjour (Direction Migration)",
        3,
        "Certificat valable jusqu'à 5 ans, souvent émis en ≈ 3 jours ouvrés.",
        "executant",
        ["Passeport / CNI", "Preuve de logement", "Assurance santé (EHIC ou locale)"],
    ),
    (
        "Obtention du numéro personnel (LNCh)",
        None,
        "🟠 Les citoyens UE reçoivent un LNCh (et non un EGN), ce qui peut créer des "
        "obstacles administratifs (banque, services publics). Requis pour banque, "
        "fisc, bail, santé.",
        "executant",
        [],
    ),
]

# Bulgarie — résidence retraité hors-UE (art. 24(1)(10) ЗЧРБ).
_BG_RET_STEPS: list[_Step] = [
    (
        "Demande de visa D au consulat bulgare",
        30,
        "🔴 Moyens de subsistance ≥ pension/salaire minimum (≈ 620 €/mois en 2026, "
        "indexé SMIC, post-euro) — montant indicatif, revérifier la source "
        "officielle. Les pensions privées (ex. 401k) peuvent être refusées sans "
        "document officiel de pension d'État. Frais visa ≈ 100 €.",
        "provides_documents",
        [
            "Passeport (≥ 3 mois, 2 pages vierges)",
            "2 photos",
            "Preuve de logement",
            "Casier apostillé / traduit",
            "Assurance santé",
            "Justificatif officiel de pension",
        ],
    ),
    (
        "Entrée en Bulgarie & enregistrement de l'adresse (sous 5 j)",
        5,
        "Enregistrement de l'adresse sous 5 jours après l'entrée.",
        "executant",
        [],
    ),
    (
        "Dépôt du permis de séjour prolongé (Direction Migration)",
        14,
        "Permis valable jusqu'à 1 an, renouvelable. Ne donne pas accès au marché du travail.",
        "executant",
        [
            "Formulaire",
            "Passeport + copie visa/tampon",
            "Assurance",
            "Justificatif de pension",
            "Preuve de logement",
        ],
    ),
]

# Bulgarie — visa nomade digital hors-UE (art. 24p ЗЧРБ, régime récent).
_BG_DN_STEPS: list[_Step] = [
    (
        "Vérifier le régime (récent) & réunir le dossier",
        5,
        "🔴 RÉGIME TRÈS RÉCENT — base légale art. 24p ЗЧРБ, demandes ouvertes le "
        "20/12/2025. Détails d'application encore évolutifs : revérifier auprès du "
        "consulat / de la Direction Migration avant toute promesse.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Demande de visa D au consulat",
        45,
        "🔴 Seuil ≈ 31 000 €/an indexé SMIC, post-euro — indicatif, revérifier. "
        "Interdiction de travailler pour des clients/employeurs bulgares.",
        "provides_documents",
        [
            "Passeport",
            "Preuve de revenu (≥ 50× salaire minimum mensuel, ≈ 31 000 €/an)",
            "Assurance santé (≥ 30 000 € de couverture)",
            "Casier",
            "Logement",
        ],
    ),
    (
        "Entrée & permis de séjour (Direction Migration, sous 14 j)",
        28,
        "Permis 1 an, renouvelable 1 an (max ≈ 2 ans). Ne mène PAS à la résidence permanente.",
        "executant",
        [],
    ),
]

# Bulgarie — freelance / profession libérale hors-UE (art. 24a ЗЧРБ).
_BG_FL_STEPS: list[_Step] = [
    (
        "Obtenir le permis d'activité freelance (Agence pour l'emploi)",
        30,
        "🟠 Le permis est délivré par l'AGENCE POUR L'EMPLOI (relevant du MTSP), PAS "
        "par la Direction Migration — erreur de nommage fréquente. Bulgare B1 requis.",
        "provides_documents",
        [
            "Plan d'activité détaillé",
            "Preuve de ≥ 2 ans d'expérience professionnelle",
            "Moyens financiers",
            "Preuve de niveau de bulgare B1",
        ],
    ),
    (
        "Demande de visa D au consulat",
        30,
        "Demande de visa D sur la base du permis freelance.",
        "provides_documents",
        [
            "Permis freelance",
            "Passeport",
            "Casier apostillé",
            "Assurance",
            "Logement",
            "Paiement ≈ 100 €",
        ],
    ),
    (
        "Permis de séjour (Direction Migration)",
        14,
        "Permis 12 mois renouvelable. Pas de seuil de revenu statutaire fixe publié "
        "(évalué sur le plan d'activité). Indicatif.",
        "executant",
        [],
    ),
]

# Bulgarie — création de société (EOOD = associé unique, OOD = ≥ 2 associés ;
# 100 % étranger possible, résidence non requise).
_BG_CO_STEPS: list[_Step] = [
    (
        "Vérifier / réserver le nom & choisir la structure",
        3,
        "EOOD = 1 associé · OOD = ≥ 2 associés (acte constitutif notarié + "
        "déclaration UBO). Capital minimum ≈ 1 € (2 BGN). Le nombre d'associés est "
        "le seul paramètre stable de ce parcours.",
        "executant",
        [],
    ),
    (
        "Rédiger les statuts & déposer le capital",
        5,
        "Siège bulgare requis ; si gérant non-résident, personne de contact locale nécessaire.",
        "executant",
        [],
    ),
    (
        "Immatriculation au Registre du Commerce",
        7,
        "Obtention de l'EIK / BULSTAT (code unique). 3 à 10 jours ouvrés (2 à 4 "
        "semaines en remote).",
        "executant",
        ["Statuts", "Preuve de dépôt du capital", "Déclaration UBO"],
    ),
    (
        "TVA, compte bancaire & mise en route",
        14,
        "🔴 IS 10 % (le plus bas de l'UE), dividendes 5 % — taux indicatifs, "
        "revérifier (post-euro 2026). TVA si CA > ≈ 51 000 €. Goulot connu : "
        "ouverture de compte bancaire (KYC, parfois présence requise). ⚠️ Détenir à "
        "distance ≠ s'installer : le 10 % d'IS ne tient que si la société est "
        "réellement pilotée DEPUIS la Bulgarie (substance).",
        "executant",
        [],
    ),
]

# Hongrie — enregistrement de séjour d'un citoyen UE/EEE/Suisse (> 90 jours).
_HU_EU_STEPS: list[_Step] = [
    (
        "Déclaration de séjour (> 90 j) à l'Office de l'immigration",
        14,
        "🟠 Le montant « ressources suffisantes » en HUF est à revérifier (source "
        "primaire non confirmée). Travail et établissement autorisés sans titre.",
        "provides_documents",
        [
            "Passeport / CNI",
            "Ressources suffisantes + assurance maladie (inactif) OU preuve d'emploi (salarié)",
        ],
    ),
    (
        "Carte d'enregistrement (registration card)",
        7,
        "Résidence permanente accessible à 5 ans, naturalisation à 8 ans.",
        "executant",
        [],
    ),
    (
        "Identifiants d'installation (carte d'adresse, n° fiscal, TAJ santé)",
        None,
        "lakcímkártya (carte d'adresse) · adóazonosító jel (n° fiscal NAV) · TAJ "
        "(sécurité sociale NEAK). Blocages pratiques fréquents — à prévoir dès "
        "l'arrivée.",
        "executant",
        [],
    ),
]

# Hongrie — White Card (nomade digital hors-UE), travail à distance pour une
# entité hors Hongrie. Impasse : ne mène ni à la PR ni à la naturalisation.
_HU_WC_STEPS: list[_Step] = [
    (
        "Avertissement préalable & vérification du seuil",
        3,
        "🔴 FICHE NON VÉRIFIÉE EN SOURCE PRIMAIRE. ⚠️ IMPASSE : la White Card ne "
        "compte NI pour la résidence permanente NI pour la naturalisation — "
        "solution d'essai 1-2 ans. Pour s'installer durablement, basculer vers une "
        "autre voie. Interdit de travailler pour le marché hongrois. Revenu mensuel "
        "minimum 🔴 volatil, revérifier sur oif.gov.hu.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Demande de visa D / White Card",
        30,
        "Demande de visa D / White Card sur la base du dossier réuni.",
        "provides_documents",
        [
            "Passeport",
            "Preuve de revenu de source étrangère",
            "Preuve de travail à distance pour entité hors Hongrie",
            "Assurance santé",
            "Logement",
            "Casier",
        ],
    ),
    (
        "Titre de séjour & identifiants",
        21,
        "Carte d'adresse + n° fiscal + TAJ. Conditions de renouvellement et de "
        "regroupement familial 🔴 à vérifier.",
        "executant",
        [],
    ),
]

# Hongrie — Guest Investor (golden visa hors-UE), titre 10 ans à faible présence.
_HU_GI_STEPS: list[_Step] = [
    (
        "Avertissement & choix de l'option d'investissement",
        5,
        "🔴 FICHE NON VÉRIFIÉE EN SOURCE PRIMAIRE. Options (montants indicatifs, "
        "revérifier) : fonds agréés MNB ≈ 250 000 € (voie la moins chère) ; "
        "immobilier résidentiel direct ≈ 500 000 € (option possiblement REPORTÉE — "
        "vérifier si réellement ouverte) ; donation enseignement supérieur "
        "≈ 1 000 000 €. Vérifier la liste des fonds MNB réellement souscriptibles.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Réalisation de l'investissement",
        30,
        "Déploiement du capital selon l'option retenue.",
        "executant",
        [],
    ),
    (
        "Demande du titre Guest Investor",
        45,
        "Titre 10 ans, faible présence exigée. Voie patrimoniale.",
        "executant",
        ["Preuve d'investissement", "Passeport", "Casier", "Assurance"],
    ),
    (
        "Titre de séjour & identifiants",
        None,
        "Carte d'adresse + n° fiscal + TAJ.",
        "executant",
        [],
    ),
]

# Hongrie — autorisation unique (single permit) : titre de séjour + autorisation
# de travail en UNE procédure portée par l'employeur. Compte pour la PR et la
# naturalisation.
_HU_SP_STEPS: list[_Step] = [
    (
        "L'employeur initie la demande (single permit)",
        30,
        "Titre + autorisation de travail en UNE procédure, portée par l'employeur. "
        "🟠 Test du marché du travail éventuel + seuils salariaux à vérifier. Cette "
        "voie COMPTE pour la résidence permanente et la naturalisation.",
        "provides_documents",
        [],
    ),
    (
        "Demande de visa D au consulat",
        30,
        "Demande de visa D au consulat sur la base de l'autorisation obtenue.",
        "provides_documents",
        ["Passeport"],
    ),
]

# Hongrie — création de société (Kft.). Ouverte aux étrangers NON-RÉSIDENTS,
# ne donne AUCUN titre de séjour.
_HU_CO_STEPS: list[_Step] = [
    (
        "Préparer la constitution (avocat) & le capital",
        5,
        "⚠️ SOCIÉTÉ ≠ RÉSIDENCE. Constituer une Kft. ne donne aucun titre de "
        "séjour : un étranger pays-tiers peut piloter une Kft. À DISTANCE sans "
        "titre ; pour résider physiquement, c'est un titre distinct (et incertain "
        "post-réforme). Capital social ~3 M HUF (~7 600 €, apport différable si "
        "l'acte le prévoit). Montants indicatifs, reconvertir au taux du jour.",
        "provides_documents",
        [],
    ),
    (
        "Acte constitutif & enregistrement au registre du commerce (Cégbíróság)",
        7,
        "Enregistrement au Cégbíróság (registre du commerce).",
        "provides_documents",
        [
            "Acte constitutif (avocat obligatoire)",
            "Statuts",
            "Déclaration des bénéficiaires (UBO)",
            "Siège hongrois",
        ],
    ),
    (
        "Numéro fiscal, TVA & registres",
        7,
        "🟠 IS 9 % (le plus bas de l'UE), 0 % de retenue sur dividendes sortants — "
        "taux indicatifs à revérifier (NAV). Franchise TVA (ÁFA) sous ~18 M HUF/an "
        "(~45 000 €), sinon 27 %. Taxe locale (HIPA). KIVA possible si forte masse "
        "salariale.",
        "provides_documents",
        [],
    ),
    (
        "Compte bancaire professionnel",
        14,
        "🟠 GOULOT : ouverture de compte pour gérant/UBO étranger — présence "
        "physique souvent exigée, poste le plus lent.",
        "provides_documents",
        [],
    ),
]

# Dubaï (EAU) — Golden Visa 10 ans : seul parcours autonome (pas de sponsor),
# renouvelable, exempté de la règle d'absence de 6 mois.
_AE_GV_STEPS: list[_Step] = [
    (
        "Vérifier la porte d'éligibilité",
        5,
        "🟠 Portes (montants AED volatils, revérifier u.ae / icp.gov.ae) : "
        "investisseur ≥ 2 M AED (fonds agréé ou bien) · talents salaire "
        "≥ 30 000 AED/mois + diplôme + classification MOHRE 1er/2e niveau · "
        "entrepreneur projet ≥ 500 000 AED ou validation incubateur · immobilier "
        "≥ 2 M AED.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Constituer le dossier de nomination",
        14,
        "Dossier de nomination à constituer selon la porte d'éligibilité retenue.",
        "provides_documents",
        [
            "Passeport",
            "Preuve du critère (propriété / investissement / contrat+classification / projet)",
            "Photos",
            "Documents légalisés (apostille pour les ressortissants UE)",
        ],
    ),
    (
        "Examen médical & Emirates ID",
        10,
        "Examen médical + Emirates ID obligatoires (transverses à tout parcours "
        "EAU). Présence requise (biométrie).",
        "provides_documents",
        [],
    ),
    (
        "Émission du visa Golden 10 ans",
        None,
        "10 ans renouvelable, autonome (pas de sponsor), exempté de la règle "
        "d'absence de 6 mois — adapté aux profils très mobiles. Peut sponsoriser "
        "la famille.",
        "provides_documents",
        [],
    ),
]

# Dubaï (EAU) — résidence via société free zone : la société sponsorise le visa
# de son propriétaire (auto-sponsoring, 100 % propriété étrangère).
_AE_FZ_STEPS: list[_Step] = [
    (
        "Choisir la free zone & l'activité, réserver le nom",
        7,
        "Activité hors marché intérieur EAU / B2B international / holding / "
        "numérique. Pour vendre sur le marché local → mainland (autre parcours). "
        "Réalisé via un prestataire agréé, à assigner au dossier.",
        "provides_documents",
        [],
    ),
    (
        "Licence & establishment card",
        10,
        "🟠 Quota de visas selon la formule/bureau (~1 visa/9 m², variable selon "
        "l'autorité). Coûts = sources commerciales, recouper 2-3 prestataires.",
        "provides_documents",
        ["Passeport", "Dossier société", "Paiement package free zone"],
    ),
    (
        "Entry permit → examen médical → Emirates ID",
        10,
        "Médical + Emirates ID obligatoires. Présence requise.",
        "provides_documents",
        [],
    ),
    (
        "Visa de résidence apposé (2-3 ans, renouvelable)",
        None,
        "🟠 Le sponsor est la SOCIÉTÉ : tant qu'elle est active, le visa tient. "
        "Fiscalité : voir le parcours société (0 % QFZP NON automatique). Client "
        "UE : documenter la sortie fiscale du pays d'origine (côté France).",
        "provides_documents",
        [],
    ),
]

# Dubaï (EAU) — visa immobilier : le bien (750 000 – 2 M AED) sponsorise le visa.
_AE_RE_STEPS: list[_Step] = [
    (
        "Acquisition & qualification du bien",
        14,
        "🟠 Bien ≥ 750 000 AED (montant indicatif, DLD). NE PAS confondre avec le "
        "Golden Visa immobilier (≥ 2 M AED / 10 ans).",
        "provides_documents",
        ["Titre de propriété (Dubai Land Department)", "Passeport"],
    ),
    (
        "Demande de visa immobilier",
        10,
        "Demande déposée via un prestataire agréé, à assigner au dossier.",
        "provides_documents",
        [],
    ),
    (
        "Examen médical & Emirates ID",
        10,
        "Médical + Emirates ID obligatoires. Présence requise.",
        "provides_documents",
        [],
    ),
    (
        "Visa de résidence apposé (2 ans renouvelable)",
        None,
        "🟠 Le sponsor est le BIEN : la résidence tient tant que le bien est "
        "détenu. Au-delà de 2 M AED, préférer le Golden Visa (10 ans + exemption "
        "règle d'absence).",
        "provides_documents",
        [],
    ),
]

# Dubaï (EAU) — visa remote work : télétravail pour un employeur HORS EAU.
# Rapide mais court (1 an), ne mène pas à une résidence longue.
_AE_RW_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité & réunir le dossier",
        7,
        "🟠 Seuil indicatif. ⚠️ Le remote work ne mène PAS à une résidence longue "
        "(1 an) — pour une base durable + optimisation fiscale, préférer une "
        "société free zone dès le départ. Le dire avant que le client s'y enferme.",
        "provides_documents",
        [
            "Passeport",
            "Preuve de revenu étranger ≥ 3 500 USD/mois (~12 850 AED)",
            "Contrat de travail / preuve d'activité hors EAU",
            "Assurance santé",
        ],
    ),
    (
        "Demande du visa remote work",
        10,
        "Demande du visa remote work sur la base du dossier réuni.",
        "provides_documents",
        [],
    ),
    (
        "Examen médical & Emirates ID",
        10,
        "Médical + Emirates ID obligatoires. Présence requise.",
        "provides_documents",
        [],
    ),
    (
        "Émission du visa (1 an)",
        None,
        "Émission du visa remote work, valable 1 an.",
        "provides_documents",
        [],
    ),
]

# Dubaï (EAU) — visa retraité (55 ans et +), un critère financier suffit.
_AE_RET_STEPS: list[_Step] = [
    (
        "Vérifier le critère financier (un seul suffit)",
        5,
        "🟠 Un des trois (montants indicatifs, revérifier) : revenu ≥ 20 000 AED/"
        "mois · OU épargne ≥ 1 M AED · OU bien ≥ 1 M AED. Réservé aux 55 ans et +. "
        "Au-delà de 2 M AED de patrimoine, préférer le Golden Visa (10 ans + "
        "exemption règle d'absence).",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Constituer le dossier",
        14,
        "Dossier de demande à constituer sur la base du critère financier retenu.",
        "provides_documents",
        ["Passeport", "Preuve du critère financier", "Assurance santé"],
    ),
    (
        "Examen médical & Emirates ID",
        10,
        "Médical + Emirates ID obligatoires. Présence requise.",
        "provides_documents",
        [],
    ),
    (
        "Visa retraité apposé (5 ans renouvelable)",
        None,
        "Visa retraité apposé, valable 5 ans renouvelable.",
        "provides_documents",
        [],
    ),
]

# Dubaï (EAU) — création de société : la décision n°1 est free zone vs mainland.
# Une société free zone sponsorise aussi le visa de son propriétaire.
_AE_CO_STEPS: list[_Step] = [
    (
        "Trancher free zone vs mainland (question filtre)",
        5,
        "QUESTION FILTRE : le client vend-il directement sur le marché intérieur "
        "des EAU ? OUI → mainland (DET) : accès onshore + marchés publics. NON "
        "(international / B2B / holding / numérique / objectif résidence) → free "
        "zone : 100 % propriété + auto-sponsoring du visa. 100 % propriété "
        "étrangère désormais permis pour beaucoup d'activités mainland (liste "
        "d'activités à impact stratégique encadrée — vérifier auprès du DET).",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Réservation du nom & approbation de l'activité",
        7,
        "Réservation du nom et approbation de l'activité via un prestataire agréé, "
        "à assigner au dossier.",
        "provides_documents",
        [],
    ),
    (
        "Licence & établissement",
        10,
        "🔴 Coûts (package free zone / frais DET / establishment card) = "
        "majoritairement sources commerciales → recouper 2-3 prestataires + "
        "autorités (DMCC, IFZA, Meydan, DET). Ne jamais devis-er sur un seul "
        "chiffre marketing.",
        "provides_documents",
        ["Passeport(s) actionnaire(s)", "Dossier", "Paiement de la licence"],
    ),
    (
        "Enregistrement fiscal (corporate tax / TVA) & compte bancaire",
        21,
        "🟢 Corporate tax 0 % jusqu'à 375 000 AED de bénéfice, 9 % au-delà. TVA "
        "5 % obligatoire si CA > 375 000 AED (volontaire dès 187 500). Small "
        "Business Relief si CA < 3 M AED (jusqu'aux exercices clos au 31/12/2026). "
        "⚠️ Le 0 % FREE ZONE (QFZP) n'est PAS automatique : exige substance, "
        "qualifying income B2B, respect du de minimis (min 5 M AED / 5 % du CA), "
        "prix de transfert, ÉTATS FINANCIERS AUDITÉS. Un trading B2C local en free "
        "zone n'y donne généralement pas droit. NE JAMAIS promettre le 0 % sans "
        "validation.",
        "provides_documents",
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
    (BG_EU_NAME, "BG", _BG_EU_STEPS),
    (BG_RET_NAME, "BG", _BG_RET_STEPS),
    (BG_DN_NAME, "BG", _BG_DN_STEPS),
    (BG_FL_NAME, "BG", _BG_FL_STEPS),
    (BG_CO_NAME, "BG", _BG_CO_STEPS),
    (HU_EU_NAME, "HU", _HU_EU_STEPS),
    (HU_WC_NAME, "HU", _HU_WC_STEPS),
    (HU_GI_NAME, "HU", _HU_GI_STEPS),
    (HU_SP_NAME, "HU", _HU_SP_STEPS),
    (HU_CO_NAME, "HU", _HU_CO_STEPS),
    (AE_GV_NAME, "AE", _AE_GV_STEPS),
    (AE_FZ_NAME, "AE", _AE_FZ_STEPS),
    (AE_RE_NAME, "AE", _AE_RE_STEPS),
    (AE_RW_NAME, "AE", _AE_RW_STEPS),
    (AE_RET_NAME, "AE", _AE_RET_STEPS),
    (AE_CO_NAME, "AE", _AE_CO_STEPS),
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
