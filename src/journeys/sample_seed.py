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
MU_OPI_NAME = "Maurice — Occupation Permit Investor (entrepreneur)"
MU_OPP_NAME = "Maurice — Occupation Permit Professional (salarié)"
MU_OPS_NAME = "Maurice — Occupation Permit Self-Employed (consultant solo)"
MU_PV_NAME = "Maurice — Premium Visa (nomade / revenu passif étranger)"
MU_RE_NAME = "Maurice — Résidence par investissement immobilier (≥ 375k USD)"
MU_CO_NAME = "Maurice — Création de société (Domestic / GBC / Authorised)"
TH_DTV_NAME = "Thaïlande — Destination Thailand Visa (DTV, nomade)"
TH_LTR_NAME = "Thaïlande — Long-Term Resident (LTR, 10 ans)"
TH_OA_NAME = "Thaïlande — Visa retraité (O-A, 50 ans et +)"
TH_PRIV_NAME = "Thaïlande — Thailand Privilege (carte de séjour payante)"
TH_NONB_NAME = "Thaïlande — Non-B + Work Permit (salarié)"
TH_CO_NAME = "Thaïlande — Création de société (FBA : 100 % / BOI / Amity / FBL)"
ID_RW_NAME = "Indonésie — Remote Worker KITAS (E33G, nomade)"
ID_SH_NAME = "Indonésie — Second Home Visa (rentier)"
ID_RET_NAME = "Indonésie — Retirement KITAS (E33F, 55 ans et +)"
ID_WORK_NAME = "Indonésie — Work KITAS (E23, salarié)"
ID_INV_NAME = "Indonésie — Investor KITAS (E28A) + PT PMA"
ID_CO_NAME = "Indonésie — Création de société (PT PMA)"
PT_CRUE_NAME = "Portugal — Enregistrement de résidence UE (CRUE)"
PT_D7_NAME = "Portugal — Visa D7 (revenu passif / retraité, hors-UE)"
PT_D8_NAME = "Portugal — Visa D8 (nomade digital, hors-UE)"
PT_GV_NAME = "Portugal — Golden Visa / ARI (investisseur passif, post-2023)"
PH_SRRV_NAME = "Philippines — SRRV (résidence par dépôt, via PRA)"
PH_SIRV_NAME = "Philippines — SIRV (visa investisseur, via BOI)"
PH_13A_NAME = "Philippines — Visa 13(a) (conjoint de ressortissant·e philippin·e)"
PH_CO_NAME = "Philippines — Création de société (60/40 / FINL / export / DME)"
VN_WP_NAME = "Vietnam — Work Permit + TRC (salarié)"
VN_INV_NAME = "Vietnam — Investor TRC (DT1-DT4)"
VN_TT_NAME = "Vietnam — TRC familiale (TT, conjoint de Vietnamien·ne)"
VN_RO_NAME = "Vietnam — Representative Office (bureau de représentation)"
US_E2_NAME = "États-Unis — Visa E-2 (investisseur de traité)"
US_L1_NAME = "États-Unis — Visa L-1 (transfert intra-entreprise)"
US_O1_NAME = "États-Unis — Visa O-1 (capacités extraordinaires)"
US_H1B_NAME = "États-Unis — Visa H-1B (specialty occupation)"
US_EB5_NAME = "États-Unis — Green card EB-5 (investisseur immigrant)"
US_NIW_NAME = "États-Unis — Green card EB-2 NIW / EB-1A (par le mérite)"
US_CO_NAME = "États-Unis — Création de société (LLC / C-Corp)"
CH_BNA_NAME = "Suisse — Permis B non-actif (rentier/retraité UE/AELE)"
CH_EMP_NAME = "Suisse — Permis L/B salarié (UE/AELE)"
CH_IND_NAME = "Suisse — Indépendant / entrepreneur (UE/AELE)"
CH_RET_NAME = "Suisse — Rentier hors-UE (55 ans et +, art. 28 LEI)"
CH_TCN_NAME = "Suisse — Salarié hors-UE (art. 18-23 LEI)"
CH_CO_NAME = "Suisse — Création de société (Sàrl / SA)"
CA_EE_NAME = "Canada — Express Entry (résidence permanente fédérale)"
CA_PNP_NAME = "Canada — Provincial Nominee Program (PNP)"
CA_QC_NAME = "Québec — PSTQ / Arrima (sélection québécoise, puis RP)"
CA_WP_NAME = "Canada — Permis de travail → expérience canadienne → RP"
CA_SUV_NAME = "Canada — Start-up Visa (SUV, entrepreneur)"

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

# Maurice — Occupation Permit Investor : titre unique (résidence + droit
# d'activité, pas de permis de travail séparé), jusqu'à 10 ans.
_MU_OPI_STEPS: list[_Step] = [
    (
        "Constituer la société & l'apport",
        14,
        "🟠 Apport ≥ 50 000 USD + CA ≥ 4 M MUR attendu dès l'an 3 (seuils "
        "indicatifs, révisés au Budget annuel ~juin). Choix du véhicule (Domestic "
        "/ GBC / Authorised) : voir le parcours société. Réalisé via un conseil "
        "mauricien, à assigner au dossier.",
        "provides_documents",
        [],
    ),
    (
        "Dépôt de la demande à l'EDB",
        21,
        "Dépôt de la demande d'Occupation Permit auprès de l'EDB.",
        "provides_documents",
        [
            "Passeport",
            "Documents de la société",
            "Preuve d'apport",
            "Plan d'affaires",
            "Casier",
            "Certificat médical",
        ],
    ),
    (
        "Octroi de l'Occupation Permit & enregistrement au PIO",
        30,
        "L'EDB instruit, le PIO délivre. Titre unique résidence + activité. "
        "Famille en titre dérivé adossé. Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Maurice — Occupation Permit Professional : seul titre autorisant l'emploi
# local, porté par l'employeur. Titre unique résidence + travail.
_MU_OPP_STEPS: list[_Step] = [
    (
        "Contrat & vérification du seuil de salaire",
        7,
        "🔴 Salaire minimum 30 000 vs 60 000 MUR/mois selon période/secteur — LE "
        "seuil le plus instable, revérifier impérativement. Exceptions TIC/BPO "
        "possiblement plus basses (🟠).",
        "provides_documents",
        [],
    ),
    (
        "Dépôt de la demande à l'EDB (portée par l'employeur)",
        21,
        "Demande portée par l'employeur auprès de l'EDB.",
        "provides_documents",
        ["Contrat de travail", "Passeport", "Diplômes", "Casier", "Médical"],
    ),
    (
        "Octroi de l'OP & enregistrement au PIO",
        30,
        "Titre unique résidence + travail (pas de permis séparé), jusqu'à 10 ans. "
        "Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Maurice — Occupation Permit Self-Employed : prestataire individuel, entrée plus
# basse que l'Investor. Titre unique résidence + activité.
_MU_OPS_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité & l'apport",
        7,
        "🟠 Apport ≈ 35 000 USD + revenu d'activité ≈ 800 000 MUR attendu (an "
        "2/3). Seuils indicatifs, révisés au Budget annuel.",
        "provides_documents",
        [],
    ),
    (
        "Dépôt de la demande à l'EDB",
        21,
        "Dépôt de la demande d'Occupation Permit auprès de l'EDB.",
        "provides_documents",
        [
            "Passeport",
            "Preuve d'apport",
            "Plan d'activité",
            "Contrats / preuves de prestation",
            "Casier",
            "Médical",
        ],
    ),
    (
        "Octroi de l'OP & enregistrement au PIO",
        30,
        "Titre unique résidence + activité, jusqu'à 10 ans. Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Maurice — Premium Visa : nomade / rentier à revenu étranger, sans activité
# locale. 1 an renouvelable, ne mène pas à une résidence longue.
_MU_PV_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité (revenu étranger)",
        5,
        "🟠 Revenu ≥ 1 500 USD/mois (+ ~500/dépendant). Annoncé gratuit. "
        "⚠️ Interdiction du marché local (revenu de source étrangère uniquement). "
        "Seuils indicatifs, Budget annuel.",
        "provides_documents",
        [],
    ),
    (
        "Demande en ligne (EDB)",
        14,
        "Demande de Premium Visa en ligne auprès de l'EDB.",
        "provides_documents",
        [
            "Passeport",
            "Preuve de revenu étranger",
            "Assurance santé/voyage",
            "Justificatif de logement",
        ],
    ),
    (
        "Octroi du Premium Visa",
        None,
        "1 an renouvelable. Ne donne pas accès à une résidence longue — pour la "
        "stabilité, envisager l'immobilier ≥ 375k USD (autre parcours).",
        "provides_documents",
        [],
    ),
]

# Maurice — résidence par investissement immobilier : la résidence est liée à la
# détention d'un bien dans un schéma qualifiant (IRS / RES / PDS / Smart City).
_MU_RE_STEPS: list[_Step] = [
    (
        "Sélection d'un bien qualifiant",
        21,
        "🟠 Seuil ≥ 375 000 USD ouvrant la résidence (schémas IRS/RES/PDS/Smart "
        "City/G+2 éligible). En dessous : achat possible mais SANS résidence "
        "automatique. Droits d'enregistrement ~5 % (à confirmer). Réalisé via un "
        "conseil, à assigner au dossier.",
        "provides_documents",
        [],
    ),
    (
        "Acquisition & enregistrement",
        30,
        "Acquisition du bien et enregistrement.",
        "provides_documents",
        ["Acte d'acquisition", "Preuve de transfert des fonds", "Passeport"],
    ),
    (
        "Demande de résidence (EDB) & enregistrement PIO",
        30,
        "Résidence tant que le bien est détenu (titre jusqu'à 20 ans pour "
        "l'immobilier ≥ 375k). Pas d'impôt sur les plus-values ni droits de "
        "succession à Maurice. Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Maurice — création de société : décision n°1 = vente locale vs internationale
# et besoin (ou non) des conventions fiscales (DTAA).
_MU_CO_STEPS: list[_Step] = [
    (
        "Choisir le véhicule (question filtre)",
        5,
        "MARCHÉ LOCAL → Domestic Company (IS 15 %, ≥ 1 administrateur résident). "
        "INTERNATIONAL + besoin des conventions DTAA → GBC (~3 % effectif via "
        "exemption partielle 80 %). INTERNATIONAL SANS besoin des DTAA → Authorised "
        "Company (0 % à Maurice, déclaration MRA, pas d'accès aux DTAA).",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Constitution & enregistrement (CBRD)",
        14,
        "GBC → 2 administrateurs résidents + management company agréée FSC "
        "obligatoire + audit annuel. Authorised → registered agent agréé, "
        "gestion/contrôle hors Maurice.",
        "provides_documents",
        [],
    ),
    (
        "Licence FSC (GBC) / enregistrement fiscal & TVA",
        21,
        "🟠 TVA standard 15 % (différer sous le seuil). CCR Levy 2 % au-delà d'un "
        "seuil de CA. Pas de plus-values ni droits de succession.",
        "provides_documents",
        [],
    ),
    (
        "Substance & gouvernance (si GBC)",
        None,
        "⚠️ NE JAMAIS monter une GBC comme boîte aux lettres : sans substance "
        "réelle (2 admins résidents, dépenses locales / CIGA, gouvernance à "
        "Maurice), l'exemption 80 % (~3 %) SAUTE et un risque de requalification "
        "existe.",
        "provides_documents",
        [],
    ),
]

# Thaïlande — Destination Thailand Visa : nomade / freelance pour employeur ou
# clients ÉTRANGERS. 5 ans multi-entrées, séjours 180 j. Pas de travail thaï, pas
# de PR.
_TH_DTV_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité & l'épargne",
        5,
        "🟠 Épargne ≥ 500 000 THB (indicatif, ~36 THB/USD). ⚠️ Le DTV N'AUTORISE "
        "PAS le travail pour un client/employeur THAÏ. ⚠️ Ne mène PAS à la "
        "résidence permanente (ni le DTV, ni la retraite, ni Privilege n'y "
        "comptent — seul Non-B + work permit y mène).",
        "provides_documents",
        [],
    ),
    (
        "Demande via le portail e-visa (MFA)",
        14,
        "🟠 Pratique hétérogène selon consulats (historique bancaire sur plusieurs "
        "mois parfois exigé). Tarif d'extension 180 j ≈ 10 000 THB (et NON ~1 900 "
        "— erreur fréquente des sources commerciales).",
        "provides_documents",
        [
            "Passeport",
            "Preuve d'épargne (500 k THB)",
            "Preuve d'activité à distance / contrats",
            "Justificatifs",
            "Paiement (~10 000 THB)",
        ],
    ),
    (
        "Émission du DTV",
        None,
        "5 ans, multi-entrées, 180 j par entrée (extensible une fois).",
        "provides_documents",
        [],
    ),
]

# Thaïlande — Long-Term Resident (10 ans), géré par le BOI, reporting allégé.
_TH_LTR_STEPS: list[_Step] = [
    (
        "Identifier la catégorie LTR",
        5,
        "🟠 4 catégories (seuils indicatifs, revérifier ltr.boi.go.th) : Wealthy "
        "Global Citizen (patrimoine élevé + investissement) · Wealthy Pensioner "
        "(50+, revenu passif ≥ 80 000 USD/an, ou 40-80k avec investissement 250k "
        "USD) · Work-from-Thailand Professional (revenu ≥ 80 000 USD/an + employeur "
        "coté ou > 150 M USD CA ; donne un work permit numérique) · Highly-Skilled "
        "Professional (secteurs ciblés). Relaxations 2024-2025 à confirmer.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Demande de qualification au BOI",
        30,
        "Demande de qualification déposée auprès du BOI.",
        "provides_documents",
        [
            "Passeport",
            "Preuves de revenu/patrimoine/emploi selon catégorie",
            "Assurance santé",
            "Casier",
        ],
    ),
    (
        "Émission du visa LTR & enregistrement",
        21,
        "10 ans, reporting annuel (au lieu de 90 j). Work-from-Thailand inclut un "
        "work permit numérique.",
        "provides_documents",
        [],
    ),
]

# Thaïlande — visa retraité O-A (50 ans et +), renouvelable annuellement.
_TH_OA_STEPS: list[_Step] = [
    (
        "Vérifier l'âge & le critère financier",
        5,
        "🟠 ≥ 50 ans + dépôt 800 000 THB OU revenu 65 000 THB/mois + assurance "
        "santé (couverture ~3 M THB). Seuils indicatifs. NOTE : le O-X (jusqu'à "
        "10 ans) existe pour certaines nationalités éligibles (US, Canada, "
        "Australie, UK, Japon…), seuil 3 M THB — liste à confirmer. ⚠️ Ne mène "
        "pas à la PR.",
        "provides_documents",
        [],
    ),
    (
        "Demande de visa O-A (consulat)",
        21,
        "Demande de visa O-A déposée au consulat.",
        "provides_documents",
        [
            "Passeport",
            "Preuve du critère financier",
            "Assurance santé conforme",
            "Casier",
            "Certificat médical",
        ],
    ),
    (
        "Émission & enregistrement à l'arrivée",
        None,
        "Reporting d'adresse tous les 90 j. Renouvelable annuellement.",
        "provides_documents",
        [],
    ),
]

# Thaïlande — Thailand Privilege : séjour longue durée clé en main, payant, sans
# condition de revenu ni activité locale. Pas de droit de travail, pas de PR.
_TH_PRIV_STEPS: list[_Step] = [
    (
        "Choisir le palier d'adhésion",
        5,
        "🟠 Paliers 2026 (indicatifs, thailandprivilege.co.th) : Bronze ~650k / "
        "Gold ~900k / Platinum ~1,5M / Diamond ~2,5M / Reserve ~5M THB. ⚠️ Ne "
        "donne PAS le droit de travailler. Ne mène PAS à la PR.",
        "provides_documents",
        [],
    ),
    (
        "Demande d'adhésion & paiement",
        30,
        "Demande d'adhésion et paiement du palier choisi.",
        "provides_documents",
        ["Passeport", "Paiement du palier", "Casier"],
    ),
    (
        "Émission de la carte & du visa Privilege",
        None,
        "Séjour longue durée selon le palier, services inclus (fast-track "
        "aéroport, assistance). Renouvellement de visa simplifié.",
        "provides_documents",
        [],
    ),
]

# Thaïlande — Non-B + Work Permit : salarié d'un employeur thaï. SEULE voie
# classique menant à la résidence permanente (après 3 ans).
_TH_NONB_STEPS: list[_Step] = [
    (
        "L'employeur vérifie capital & ratio",
        14,
        "🟠 Côté employeur : capital 2 M THB par poste étranger (1 M si marié à "
        "un·e Thaï·e) + ratio 4 employés thaïs : 1 étranger. Secteur S-Curve + "
        "salaire élevé → SMART Visa possible (SMART-T ≥ 100 000 THB/mois, sans "
        "work permit séparé — attention, beaucoup de sources citent encore 200k).",
        "provides_documents",
        [],
    ),
    (
        "Visa Non-B (consulat)",
        21,
        "Demande de visa Non-B déposée au consulat.",
        "provides_documents",
        [
            "Passeport",
            "Lettre/contrat de l'employeur",
            "Documents de la société",
            "Diplômes",
        ],
    ),
    (
        "Work Permit (Department of Employment) & enregistrement",
        14,
        "Reporting 90 j. Après 3 années consécutives sous Non-B + work permit → "
        "demande de PR possible (quota ~100/nationalité/an). Seule cette filière "
        "mène à la PR.",
        "provides_documents",
        [],
    ),
]

# Thaïlande — création de société : décision n°1 = le Foreign Business Act. Une
# société est "étrangère" dès ≥ 50 % de capital non-thaï, ce qui plafonne beaucoup
# de services à 49 % sauf dérogation (BOI / Amity / FBL).
_TH_CO_STEPS: list[_Step] = [
    (
        "Qualifier l'activité dans l'arbre FBA",
        7,
        "ARBRE DE DÉCISION : (a) activité HORS des 3 listes (industrie/manufacture) "
        "→ 100 % étranger, aucune dérogation. (b) activité Liste 3 (services, cas "
        "du consultant) → dérogation requise : citoyen US → US Treaty of Amity "
        "(100 %, sauf secteurs exclus) ; activité promouvable → BOI (100 % + "
        "exonération d'IS jusqu'à 8 ans/13 pour la pointe + work permits facilités, "
        "exempté du ratio 4:1) ; sinon → FBL (discrétionnaire, lent, capital 3 M "
        "THB) OU vrai associé thaï ≥ 51 %. (c) Liste 1 = interdit, Liste 2 = "
        'approbation du Cabinet (rare). 🔴 LE MONTAGE "ACTIONNAIRES THAÏS NOMINEE" '
        "EST ILLÉGAL (art. 36 FBA — amende et prison possibles, ordre de cession). "
        "NE JAMAIS le proposer.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Constitution & enregistrement (DBD)",
        14,
        "🟠 Frais DBD ~5 000-6 000 THB. Promotion BOI / FBL = procédure "
        "additionnelle selon la voie choisie en étape 1.",
        "provides_documents",
        ["≥ 2 actionnaires", "Memorandum", "Statuts", "Adresse", "Capital"],
    ),
    (
        "Enregistrement fiscal & TVA",
        14,
        "🟠 IS PME (capital libéré ≤ 5 M THB ET CA ≤ 30 M THB) : barème 0 % / 15 % "
        "/ 20 % ; sinon 20 % flat. TVA 7 % obligatoire si CA > 1,8 M THB/an. Taux "
        "indicatifs (rd.go.th).",
        "provides_documents",
        [],
    ),
    (
        "Visa/permis du dirigeant étranger",
        21,
        "Hors BOI, diriger sa société exige Non-B + work permit (capital 2 M THB/"
        "poste + ratio 4:1). BOI = work permits facilités, exempté du ratio.",
        "provides_documents",
        [],
    ),
]

# Indonésie — Remote Worker KITAS (E33G) : télétravail pour des clients/employeurs
# HORS Indonésie. ~1 an renouvelable, ne mène pas au KITAP.
_ID_RW_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité (revenu étranger)",
        5,
        "🟠 Revenu étranger ~60 000 USD/an (indicatif, evisa.imigrasi.go.id). "
        "⚠️ Travail UNIQUEMENT pour des clients/employeurs HORS Indonésie. Pas de "
        "tier supérieur type LTR — E33G est la voie unique du nomade.",
        "provides_documents",
        [],
    ),
    (
        "Demande e-visa (autosponsorisation par revenus)",
        14,
        "Demande e-visa, autosponsorisation par les revenus étrangers.",
        "provides_documents",
        [
            "Passeport",
            "Contrat avec entité étrangère",
            "Preuve de revenu",
            "Relevés bancaires",
            "Assurance santé",
        ],
    ),
    (
        "Émission du KITAS & enregistrement à l'arrivée",
        14,
        "~1 an renouvelable. Ne mène pas au KITAP. Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Indonésie — Second Home Visa : rentier / patrimoine, sans activité locale, sans
# condition d'âge. 5 ou 10 ans.
_ID_SH_STEPS: list[_Step] = [
    (
        "Vérifier le dépôt / proof of funds",
        5,
        "🟠 Dépôt ~IDR 2 mds (≈ 130 000 USD) — montant divergent selon les "
        "sources, revérifier evisa.imigrasi.go.id. Aucune condition d'âge. ⚠️ Pas "
        "de droit de travailler.",
        "provides_documents",
        [],
    ),
    (
        "Demande e-visa (autosponsorisation par fonds)",
        14,
        "Demande e-visa, autosponsorisation par les fonds déposés.",
        "provides_documents",
        ["Passeport", "Preuve du dépôt/fonds", "CV", "Justificatif de logement"],
    ),
    (
        "Émission du visa (5 ou 10 ans)",
        None,
        "5 ou 10 ans selon le dossier. Capital plus élevé + horizon long → "
        "comparer au Golden Visa.",
        "provides_documents",
        [],
    ),
]

# Indonésie — Retirement KITAS (E33F) : 55 ans et +, pension, sans activité
# locale. Agent sponsor agréé obligatoire ; risque sponsor.
_ID_RET_STEPS: list[_Step] = [
    (
        "Vérifier l'âge & mandater un agent sponsor agréé",
        7,
        "🟠 ≥ 55 ans + pension minimum + assurance santé. AGENT SPONSOR AGRÉÉ "
        "OBLIGATOIRE (parfois emploi d'un local exigé — pratique variable). ⚠️ Pas "
        "de droit de travailler.",
        "provides_documents",
        [],
    ),
    (
        "Demande de KITAS via l'agent",
        21,
        "Demande de KITAS portée par l'agent sponsor agréé.",
        "provides_documents",
        ["Passeport", "Preuve de pension", "Assurance santé", "Bail", "Casier"],
    ),
    (
        "Émission du KITAS & enregistrement",
        14,
        "1 an renouvelable, chaîne possible vers le KITAP. ⚠️ RISQUE SPONSOR : le "
        "titre tombe si le sponsor (agent) cesse — prévoir une voie de repli. "
        "Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Indonésie — Work KITAS (E23) : salarié d'un employeur indonésien. Seule voie qui
# autorise le travail salarié ET mène au KITAP.
_ID_WORK_STEPS: list[_Step] = [
    (
        "L'employeur obtient le RPTKA (plan d'emploi d'étrangers)",
        21,
        "Employeur sponsor OBLIGATOIRE, poste ouvert aux étrangers. DKP-TKA "
        "~100 USD/mois (~1 200/an) à la charge de l'employeur.",
        "provides_documents",
        [],
    ),
    (
        "Visa de travail & émission du KITAS",
        21,
        "Visa de travail puis émission du KITAS.",
        "provides_documents",
        ["Passeport", "RPTKA approuvé", "Contrat", "Diplômes"],
    ),
    (
        "Enregistrement & permis de travail",
        14,
        "6 mois à 2 ans renouvelable. Après 3-4 ans continus → KITAP possible. "
        "⚠️ RISQUE SPONSOR : le KITAS tombe à la fin du contrat — prévoir un "
        "repli. Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Indonésie — Investor KITAS (E28A) : entrepreneur détenant/dirigeant une PT PMA
# (société à capital étranger). La société sponsorise le visa de son dirigeant.
_ID_INV_STEPS: list[_Step] = [
    (
        "Constituer la PT PMA (prérequis)",
        21,
        'Voir le parcours "Société PT PMA" pour le détail (KBLI, capital). La '
        "société sponsorise le visa de son dirigeant. Réalisé via un notaire/"
        "conseil, à assigner au dossier.",
        "provides_documents",
        [],
    ),
    (
        "Vérifier le rôle & le seuil d'actionnariat",
        5,
        "🔴 Actionnariat ~IDR 1 md (parfois 1,125 md) — majoritairement source "
        "d'agences, revérifier. DIRECTEUR ACTIF → peut travailler (Investor "
        "KITAS) ; ACTIONNAIRE PASSIF → détention seule, pas de droit de "
        "travailler.",
        "provides_documents",
        [],
    ),
    (
        "Demande d'Investor KITAS (sponsor = PT PMA)",
        21,
        "Demande d'Investor KITAS, la PT PMA agissant comme sponsor.",
        "provides_documents",
        ["Passeport", "Documents PT PMA", "Preuve d'actionnariat"],
    ),
    (
        "Émission du KITAS & enregistrement",
        14,
        "1-2 ans renouvelable, chaîne vers KITAP. ⚠️ RISQUE SPONSOR : la "
        "dissolution de la PT PMA annule le KITAS. Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Indonésie — création de société PT PMA : décision n°1 = le statut KBLI de
# l'activité sur la Positive Investment List (ouvert / plafonné / fermé).
_ID_CO_STEPS: list[_Step] = [
    (
        "Identifier le KBLI & vérifier la Positive Investment List",
        7,
        "Identifier le code KBLI 2020 (5 chiffres). FERMÉE (~6 secteurs) → pas de "
        "PT PMA. PLAFONNÉE → % max étranger + partenaire local. OUVERTE (majorité "
        "des cas depuis Omnibus 2020) → 100 % étranger. 🔴 LE NOMINEE (prête-nom "
        "indonésien) EST ILLÉGAL ET NUL (art. 33 UU 25/2007) : nullité de la "
        "convention, perte possible de l'investissement, le prête-nom est "
        "légalement propriétaire. NE JAMAIS le proposer (risque max sur "
        "l'immobilier Bali).",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Vérifier le capital & le niveau de risque OSS",
        7,
        "🟠 Plan d'investissement > IDR 10 mds (hors terrain/bâtiment) par KBLI/"
        "localisation + capital libéré ~IDR 10 mds. ⚠️ NE PLUS UTILISER l'ancien "
        "seuil 2,5 mds (pré-2021) — erreur fréquente des agences. Niveau de risque "
        "OSS : faible → NIB suffit ; élevé → NIB + izin.",
        "provides_documents",
        [],
    ),
    (
        "Constitution (notaire) & enregistrement OSS (NIB)",
        14,
        "Constitution devant notaire et enregistrement OSS (NIB).",
        "provides_documents",
        ["Acte notarié", "Statuts", "Actionnaires", "NIB (OSS)"],
    ),
    (
        "Enregistrement fiscal & TVA",
        14,
        "🟠 IS 22 % standard (réduction art. 31E ≈ 11 % effectif si CA ≤ IDR 50 "
        "mds). Régime PME final 0,5 % du CA si CA ≤ IDR 4,8 mds (max 3 ans pour "
        "une PT). TVA obligatoire si CA > IDR 4,8 mds (~11 % effectif, point le "
        "plus volatil). Taux indicatifs (pajak.go.id).",
        "provides_documents",
        [],
    ),
]

# Philippines — SRRV : résidence par dépôt via la PRA. Résidence permanente de
# fait, entrées/sorties illimitées, dès 35 ans. Ne donne pas le droit de travailler.
_PH_SRRV_STEPS: list[_Step] = [
    (
        "Choisir la variante & le dépôt",
        5,
        "🟠 Variantes (dépôts USD, indicatifs, pra.gov.ph) : Smile 20k (non "
        "convertible immo) · Classic 35-49 ans 50k (convertible condo/bail) · "
        "Classic 50+ AVEC pension ≥ 800 USD/mois (1 000 couple) 10k · Classic 50+ "
        "sans pension 20k · Human Touch 10k (+1 500 USD/mois). ⚠️ RÉSIDER ≠ "
        "TRAVAILLER : le SRRV ne donne PAS le droit de travailler (AEP du DOLE "
        "requis en sus). NOTE : un Digital Nomad Visa (EO 86, 2025) existe sur le "
        "papier mais N'EST PAS opérationnel — ne pas le proposer tant que la "
        "délivrance n'est pas confirmée.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Constituer le dossier & transférer le dépôt",
        21,
        "Constitution du dossier et transfert du dépôt sur le compte PRA-désigné.",
        "provides_documents",
        [
            "Passeport",
            "Certificat médical",
            "Casier (NBI/police)",
            "Preuve de pension si variante concernée",
            "Dépôt sur compte PRA-désigné",
        ],
    ),
    (
        "Octroi du SRRV (PRA) & ID",
        30,
        "🟠 Frais PRA ~1 400 USD + ~300/dépendant + ~360/an. Le dépôt peut être "
        "converti en condo (pas en terrain : les étrangers ne peuvent pas détenir "
        "de terrain ; condos limités à 40 % de l'immeuble).",
        "provides_documents",
        [],
    ),
]

# Philippines — SIRV : visa investisseur via le BOI. Résidence tant que
# l'investissement admissible est conservé. Pas de droit de travail salarié.
_PH_SIRV_STEPS: list[_Step] = [
    (
        "Vérifier l'investissement admissible",
        7,
        "🟠 ~75 000 USD investis et maintenus (le simple achat immobilier ne "
        "qualifie généralement pas — actifs admissibles définis par le BOI). "
        "⚠️ RÉSIDER ≠ TRAVAILLER : statut investisseur, pas salarié — diriger sa "
        "société comme salarié exige un 9(g) + AEP.",
        "provides_documents",
        [],
    ),
    (
        "Réaliser l'investissement & déposer la demande (BOI)",
        30,
        "Réalisation de l'investissement et dépôt de la demande au BOI.",
        "provides_documents",
        ["Passeport", "Preuve d'investissement", "Casier", "Médical"],
    ),
    (
        "Octroi du SIRV (BI sur endossement BOI) & ID",
        30,
        "Résidence tant que l'investissement est conservé.",
        "provides_documents",
        [],
    ),
]

# Philippines — Visa 13(a) : conjoint étranger d'un·e Philippin·e. Résidence
# permanente après une probation d'un an. Soumis à réciprocité.
_PH_13A_STEPS: list[_Step] = [
    (
        "Vérifier la réciprocité & le mariage",
        7,
        "🟠 Le 13(a) est soumis à RÉCIPROCITÉ : ouvert aux ressortissants de pays "
        "accordant un droit équivalent aux Philippins (la plupart des pays "
        "occidentaux l'ont — à vérifier par nationalité). Mariage valide avec un·e "
        "Philippin·e requis.",
        "provides_documents",
        [],
    ),
    (
        "Dépôt de la demande (BI) — statut probatoire 1 an",
        30,
        "Dépôt de la demande auprès du BI ; statut probatoire d'un an.",
        "provides_documents",
        [
            "Passeport",
            "Acte de mariage",
            "Preuve de nationalité du conjoint",
            "Casier",
            "Médical",
        ],
    ),
    (
        "Conversion en résident permanent (après 1 an de probation)",
        30,
        "Résident permanent, exempté d'AEP pour travailler (à confirmer). ACR "
        "I-Card + Annual Report.",
        "provides_documents",
        [],
    ),
]

# Philippines — création de société : décision n°1 = triptyque FINL (activité
# ouverte ?) / marché intérieur vs export / capital requis.
_PH_CO_STEPS: list[_Step] = [
    (
        "Qualifier l'activité (FINL) & le mode de marché",
        7,
        "ARBRE : (a) activité en Liste A de la FINL (foncier, ressources, public "
        "utilities, médias, certaines professions) → 60/40 avec partenaire "
        "philippin majoritaire RÉEL. (b) export ≥ 60 % → 100 % étranger, exempté "
        "du seuil 200k USD (capital ~5 000 PHP ; obligation de maintenir 60 % "
        "d'export). (c) marché intérieur, étranger majoritaire → DME, capital "
        "200 000 USD (réductible à 100 000 si tech avancée/startup endossée/≥ 50 "
        "employés philippins). 🔴 ANTI-DUMMY LAW (CA 108) : le 60/40 de façade "
        "(prête-nom philippin, voting trust occulte, prêts adossés aux actions) "
        "est ILLÉGAL — sanctions pénales pour l'étranger ET le prête-nom. Le 60/40 "
        "doit refléter un contrôle économique philippin RÉEL. NE JAMAIS le "
        "proposer.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Constitution & enregistrement SEC",
        21,
        "Constitution et enregistrement auprès de la SEC.",
        "provides_documents",
        [
            "Statuts",
            "Actionnaires",
            "Capital libéré selon la voie",
            "Corporate Secretary philippin résident + Treasurer résident (RA 11232)",
        ],
    ),
    (
        "Enregistrement fiscal (BIR) & TVA",
        14,
        "🟠 CIT 25 % standard (20 % si revenu imposable ≤ 5 M PHP ET actifs ≤ "
        "100 M PHP hors terrain). TVA 12 % si CA > 3 M PHP (sinon percentage tax "
        "3 %). Taux indicatifs (bir.gov.ph).",
        "provides_documents",
        [],
    ),
    (
        "(Optionnel) incitations BOI/PEZA",
        30,
        "Si activité éligible (SIPP) : ITH 4-7 ans puis 5 % SCIT ou Enhanced "
        "Deductions. Lien possible avec le SIRV/9(g) du dirigeant.",
        "provides_documents",
        [],
    ),
]

# Portugal — enregistrement de résidence UE (CRUE) : citoyen UE/EEE/Suisse > 90 j.
# Circuit court par la mairie, hors backlog AIMA.
_PT_CRUE_STEPS: list[_Step] = [
    (
        "Obtenir le NIF (numéro fiscal)",
        7,
        "NIF requis pour bail, banque, démarches. NISS (sécurité sociale) selon activité.",
        "provides_documents",
        [],
    ),
    (
        "Demande de CRUE en mairie (Câmara Municipal)",
        14,
        "Certificat émis souvent le jour même. Délivré par la mairie, PAS par "
        "l'AIMA → hors backlog. Présence requise.",
        "provides_documents",
        [
            "Passeport/CNI",
            "Ressources suffisantes + assurance maladie OU preuve d'activité",
            "Justificatif de logement",
        ],
    ),
    (
        "Résidence permanente (à 5 ans)",
        None,
        "⚠️ Naturalisation à 5 ans aujourd'hui, mais réforme 2025 en cours pouvant "
        "allonger (7/10 ans) — risque réglementaire, pas un acquis.",
        "provides_documents",
        [],
    ),
]

# Portugal — Visa D7 : revenu PASSIF (pensions, dividendes, loyers, royalties,
# intérêts) pour un ressortissant hors-UE.
_PT_D7_STEPS: list[_Step] = [
    (
        "NIF + compte bancaire portugais",
        14,
        "Représentant fiscal requis pour un non-résident hors-UE.",
        "provides_documents",
        [],
    ),
    (
        "Demande de visa D7 au consulat",
        60,
        "🟠 Seuil indexé au SMN (~870 €/mois 2025, à confirmer ; SMN versé 14×/an "
        "— lever l'ambiguïté ×12/×14). ⚠️ D7 = revenu PASSIF uniquement (le "
        "télétravail actif relève du D8).",
        "provides_documents",
        [
            "Passeport",
            "Preuve de revenu passif ≥ ~1× SMN (+50 % conjoint / +30 % enfant)",
            "Épargne complémentaire",
            "Assurance",
            "Logement",
            "Casier",
        ],
    ),
    (
        "Conversion en titre de séjour à l'AIMA",
        180,
        "🔴 Délai AIMA réel (backlog massif) : mois à > 1 an, non garanti. "
        "Présenter en 2 horizons (consulaire vs AIMA réel). ⚠️ NHR supprimé : pas "
        "d'exonération fiscale personnelle pour un retraité/rentier ordinaire. "
        "Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Portugal — Visa D8 : revenu ACTIF de télétravail pour employeur/clients
# ÉTRANGERS (nomade digital hors-UE).
_PT_D8_STEPS: list[_Step] = [
    (
        "NIF + compte bancaire portugais",
        14,
        "Représentant fiscal requis pour un non-résident hors-UE.",
        "provides_documents",
        [],
    ),
    (
        "Demande de visa D8 au consulat",
        60,
        "🟠 Seuil ~4× SMN (~3 480 €/mois 2025, à confirmer). ⚠️ D8 = revenu ACTIF "
        "étranger (un revenu passif relève du D7). 2 variantes : séjour temporaire "
        "(~1 an) OU visa de résidence (compte vers les 5 ans). Choisir la variante "
        "résidence si projet d'installation.",
        "provides_documents",
        [
            "Passeport",
            "Preuve de revenu actif à distance ≥ ~4× SMN",
            "Contrat de travail/clients étrangers",
            "Assurance",
            "Logement",
            "Casier",
        ],
    ),
    (
        "Conversion en titre de séjour à l'AIMA",
        180,
        "🔴 Délai AIMA réel (backlog), mois à > 1 an, non garanti. Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Portugal — Golden Visa / ARI (post-2023) : investisseur passif, présence
# minimale (~7 j/an). L'immobilier a été retiré en 2023.
_PT_GV_STEPS: list[_Step] = [
    (
        "Choisir la voie d'investissement (post-2023)",
        7,
        "🟠 Voies ACTUELLES (montants indicatifs) : fonds qualifiés ≥ 500 000 € · "
        "création de 10 emplois · R&D ≥ 500 000 € · soutien culturel ≥ 250 000 € · "
        "capitalisation d'entreprise ≥ 500 000 €. ⚠️ L'IMMOBILIER et le simple "
        "transfert de capital ont été RETIRÉS en 2023 (loi Mais Habitação) — toute "
        "brochure citant l'achat immobilier (280k/350k/500k) est FAUSSE.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Réaliser l'investissement + NIF",
        30,
        "Réalisation de l'investissement choisi et obtention du NIF.",
        "provides_documents",
        [],
    ),
    (
        "Demande d'ARI à l'AIMA",
        180,
        "🔴 Frais ~5 300 € + ~600 €. Délai AIMA réel (backlog), non garanti. "
        "Présence minimale ~7 j/an. Le temps ARI compte vers la résidence "
        "permanente/nationalité (sous réserve de la réforme citoyenneté 2025).",
        "provides_documents",
        ["Preuve d'investissement", "Passeport", "Casier", "Assurance"],
    ),
]

# Vietnam — Work Permit + TRC : ancrage emploi (le Vietnam n'a ni visa retraité,
# ni nomade, ni golden visa). TRC adossée à l'employeur.
_VN_WP_STEPS: list[_Step] = [
    (
        "L'employeur obtient l'approbation du besoin de main-d'œuvre étrangère",
        21,
        "🔴 Autorité émettrice du work permit INCERTAINE depuis la réorganisation "
        "administrative 2025 (DOLISA → Ministère de l'Intérieur ?) — à confirmer "
        "province par province. Quota + qualification (~3 ans d'expérience pour "
        '"expert"). Exemption de permis (LD1) si capital apporté ≥ ~3 Md VND.',
        "provides_documents",
        [],
    ),
    (
        "Work Permit + visa LD2 (ou LD1 si exempté)",
        21,
        "Délivrance du Work Permit et du visa LD2 (LD1 si exempté).",
        "provides_documents",
        ["Passeport", "Diplômes", "Casier", "Certificat médical", "Contrat"],
    ),
    (
        "Carte de résidence temporaire (TRC)",
        14,
        "TRC jusqu'à 2 ans, adossée à l'employeur. Après 3 ans de TRC continue + "
        "sponsor → PRC possible (rare, discrétionnaire). ⚠️ Pour un besoin "
        '"retraite" ou "nomade", le Vietnam n\'a pas de voie — réorienter '
        "(Thaïlande/Indonésie/Philippines). Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Vietnam — Investor TRC (DT1-DT4) : ancrage investissement. Le montant du capital
# fixe directement la durée de la TRC.
_VN_INV_STEPS: list[_Step] = [
    (
        "Constituer la société (prérequis) & calibrer le capital",
        21,
        'Voir le parcours "Société (LLC FDI)" pour le détail (IRC→ERC, OMC, '
        "DICA). 🟠 CAPITAL ↔ TRC : DT1 ≥ 100 Md VND (~3,9 M USD) → TRC 10 ans "
        "(+ voie PRC) · DT2 50-100 Md → 5 ans · DT3 3-50 Md (~120k USD) → 3 ans "
        "(minimum pratique pour une TRC) · DT4 < 3 Md → PAS de TRC (visa ≤ 12 "
        "mois). Calibrer le capital sur l'horizon de résidence visé. Réalisé via "
        "un avocat, à assigner au dossier.",
        "provides_documents",
        [],
    ),
    (
        "Demande de visa investisseur (catégorie DTx)",
        21,
        "Demande de visa investisseur selon la catégorie DTx calibrée.",
        "provides_documents",
        ["Passeport", "IRC/ERC", "Preuve d'apport de capital (compte DICA)"],
    ),
    (
        "Carte de résidence temporaire (TRC)",
        14,
        "Durée selon la catégorie DTx. DT4 ne donne pas de TRC. Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Vietnam — TRC familiale (TT) : conjoint étranger d'un·e Vietnamien·ne. Ancrage
# familial, la voie la plus simple si elle s'applique.
_VN_TT_STEPS: list[_Step] = [
    (
        "Réunir les documents de mariage & sponsor",
        14,
        "Réunion des pièces de mariage et d'identité du sponsor vietnamien.",
        "provides_documents",
        [
            "Acte de mariage légalisé",
            "Preuve de nationalité du conjoint vietnamien (sponsor)",
            "Passeport",
        ],
    ),
    (
        "Demande de visa TT (sponsorisé par le conjoint)",
        21,
        "Demande de visa TT sponsorisée par le conjoint vietnamien.",
        "provides_documents",
        [],
    ),
    (
        "Carte de résidence temporaire (TRC familiale)",
        14,
        "TRC jusqu'à 3 ans. PRC accessible après 3 ans de TRC continue (sponsor "
        "familial vietnamien). Ne donne pas en soi le droit de travailler (work "
        "permit séparé requis pour une activité salariée). Biométrie requise.",
        "provides_documents",
        [],
    ),
]

# Vietnam — Representative Office : société étrangère existante voulant une présence
# de liaison, SANS activité commerciale génératrice de revenus.
_VN_RO_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité de la maison mère",
        7,
        "Maison mère existante depuis ≥ 1 an (Décret 07/2016). Le RO ne peut PAS "
        "générer de revenus commerciaux directs — fonction de liaison/"
        "représentation uniquement.",
        "provides_documents",
        [],
    ),
    (
        "Demande de licence de RO",
        21,
        "Dépôt de la demande de licence de Representative Office.",
        "provides_documents",
        [
            "Documents légalisés de la maison mère",
            "Bail",
            "Désignation du chef de bureau",
        ],
    ),
    (
        "Émission de la licence & enregistrement",
        14,
        "Licence 5 ans renouvelable. Le chef de bureau étranger obtient un visa/"
        "permis adossé au RO. Pour générer des revenus, basculer vers une LLC FDI "
        "(parcours dédié).",
        "provides_documents",
        [],
    ),
]

# États-Unis — Visa E-2 : investisseur d'un pays de traité (France éligible) dans
# une entreprise US réelle. Non-immigrant, renouvelable, PAS une green card.
_US_E2_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité de traité & structurer l'investissement",
        14,
        "🟠 La France est un pays de traité E-2. AUCUN seuil légal fixe : "
        'l\'investissement doit être "substantiel" relativement au coût de '
        "l'entreprise et NON MARGINAL (le ~100k USD souvent cité est observé, PAS "
        "une règle). Pas d'investissement passif/immobilier spéculatif. Avocat US "
        "indispensable (honoraires ~8-20k+ USD).",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Créer/acquérir l'entreprise US & engager les fonds",
        60,
        'Voir le parcours "Société (LLC / C-Corp)". La C-Corp facilite la '
        "démonstration d'une entreprise réelle. Fonds irrévocablement engagés "
        '("at risk").',
        "provides_documents",
        [],
    ),
    (
        "Dépôt de la demande (consulat US, DS-160 + DS-156E)",
        45,
        "Dépôt de la demande au consulat US (formulaires DS-160 + DS-156E).",
        "provides_documents",
        [
            "Dossier d'entreprise",
            "Business plan",
            "Preuve des fonds et de leur origine licite",
            "Preuve du contrôle ≥ 50 %",
        ],
    ),
    (
        "Entretien consulaire & délivrance",
        None,
        "🔴 Décision DISCRÉTIONNAIRE (substantialité/marginalité scrutées), jamais "
        "d'issue ni de délai ferme. Renouvelable tant que l'entreprise est active. "
        "Dual intent délicat (pas formellement admis). Présence requise.",
        "provides_documents",
        [],
    ),
]

# États-Unis — Visa L-1 : transfert intra-groupe (L-1A dirigeant / L-1B savoir
# spécialisé). Non-immigrant, dual intent admis.
_US_L1_STEPS: list[_Step] = [
    (
        "Vérifier la relation inter-entités & l'ancienneté",
        14,
        "1 an d'emploi continu à l'étranger dans l'entité liée sur les 3 dernières "
        "années. Relation qualifiante (maison mère/filiale/affiliée). L-1A "
        "dirigeant (≤ 7 ans) / L-1B savoir spécialisé (≤ 5 ans, plus scruté).",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Pétition I-129 à l'USCIS (portée par l'employeur US)",
        90,
        '"New office L-1" possible pour ouvrir une entité US (conditions '
        "renforcées, révision à 1 an).",
        "provides_documents",
        [
            "Preuve de la relation inter-entités",
            "Organigrammes",
            "Preuve du rôle et du savoir spécialisé",
            "Documents financiers",
        ],
    ),
    (
        "Visa au consulat & entrée",
        30,
        "🔴 Décision discrétionnaire (L-1B particulièrement scruté). Voie possible "
        "vers la green card EB-1C (dirigeant multinational).",
        "provides_documents",
        [],
    ),
]

# États-Unis — Visa O-1 : capacités extraordinaires (reconnaissance
# nationale/internationale). Non-immigrant.
_US_O1_STEPS: list[_Step] = [
    (
        "Évaluer le dossier de preuves",
        21,
        "🟠 Prix majeur reconnu OU au moins 3 critères réglementaires "
        "(publications, presse, rôle critique, rémunération élevée, jugement de "
        "pairs…). Qualité des preuves décisive. Sponsor US ou agent requis.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Pétition I-129 + consultation d'un peer group",
        60,
        "Pétition I-129 accompagnée de l'avis consultatif d'un peer group.",
        "provides_documents",
        [
            "Preuves d'excellence",
            "Lettres de recommandation",
            "Avis consultatif (peer/labor org)",
            "Contrat/itinéraire",
        ],
    ),
    (
        "Visa au consulat & entrée",
        30,
        "🔴 Décision discrétionnaire (qualité des preuves). Jusqu'à 3 ans, "
        "renouvelable. Dual intent délicat. Profil souvent transposable en EB-1A "
        "(green card, auto-pétition).",
        "provides_documents",
        [],
    ),
]

# États-Unis — Visa H-1B : poste spécialisé (diplôme requis) + employeur sponsor.
# Non-immigrant, dual intent admis, soumis à loterie.
_US_H1B_STEPS: list[_Step] = [
    (
        "Enregistrement à la loterie (employeur)",
        14,
        "🔴 Quota annuel 65 000 + 20 000 (master US) → LOTERIE : sélection NON "
        "garantie. ⚠️ Proclamation du 19/09/2025 imposant un droit de 100 000 USD "
        "— portée/exemptions/statut judiciaire INCERTAINS, point n°1 à vérifier. "
        "Frais d'enregistrement à confirmer (FY2027).",
        "provides_documents",
        [],
    ),
    (
        "(Si sélectionné) Labor Condition Application (DOL) + pétition I-129",
        90,
        "Après sélection : LCA au DOL puis pétition I-129.",
        "provides_documents",
        ["LCA certifiée", "Preuve du poste spécialisé", "Diplôme", "Contrat"],
    ),
    (
        "Visa au consulat & entrée",
        30,
        "3 ans + 3 ans. Lié à l'employeur. Voie possible vers green card (PERM → EB-2/EB-3).",
        "provides_documents",
        [],
    ),
]

# États-Unis — Green card EB-5 : investisseur immigrant créant 10 emplois. Voie
# directe vers la résidence permanente.
_US_EB5_STEPS: list[_Step] = [
    (
        "Structurer l'investissement & vérifier l'origine des fonds",
        30,
        "🟢 800 000 USD en zone ciblée (TEA) / 1 050 000 USD hors TEA + création "
        "de 10 emplois à temps plein. Réindexation prévue 1/1/2027. Traçabilité "
        "licite des fonds exigée (sévèrement scrutée). Investissement direct OU via "
        "Regional Center. Honoraires avocat ~15-50k+ USD.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Pétition I-526E (USCIS)",
        365,
        "Dépôt de la pétition I-526E auprès de l'USCIS.",
        "provides_documents",
        [
            "Preuve d'investissement engagé",
            "Plan de création d'emplois",
            "Dossier d'origine des fonds",
        ],
    ),
    (
        "Green card conditionnelle (2 ans) — consulat ou ajustement de statut",
        180,
        "Green card conditionnelle de 2 ans, par voie consulaire ou ajustement de statut.",
        "provides_documents",
        [],
    ),
    (
        "Levée de condition (I-829)",
        None,
        "Après ~2 ans, prouver le maintien de l'investissement et des 10 emplois "
        "→ green card permanente. Délais USCIS longs et variables.",
        "provides_documents",
        [],
    ),
]

# États-Unis — Green card EB-2 NIW / EB-1A : profil d'excellence ou d'intérêt
# national pouvant s'auto-pétitionner SANS employeur sponsor.
_US_NIW_STEPS: list[_Step] = [
    (
        "Qualifier la voie",
        21,
        "🟠 EB-1A = capacités extraordinaires (prix majeur OU ≥ 3 sur 10 "
        "critères). EB-2 NIW = diplôme avancé/aptitude exceptionnelle + 3 prongs "
        "Dhanasar (mérite & importance nationale, bonne position pour avancer, "
        "bénéfice de renoncer à l'offre d'emploi). Les deux permettent "
        "l'auto-pétition.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Constituer le dossier de preuves",
        60,
        "Constitution du dossier de preuves d'excellence/intérêt national.",
        "provides_documents",
        [
            "Publications/citations",
            "Presse",
            "Lettres d'experts",
            "Preuves d'impact",
            "Plan d'activité (NIW)",
        ],
    ),
    (
        "Pétition I-140 (USCIS)",
        240,
        "Dépôt de la pétition I-140 auprès de l'USCIS.",
        "provides_documents",
        [],
    ),
    (
        "Green card (visa bulletin / ajustement de statut)",
        None,
        "🔴 Décision discrétionnaire (qualité des preuves). Délais et arriérés "
        "selon le visa bulletin.",
        "provides_documents",
        [],
    ),
]

# États-Unis — création de société : décision n°1 = LLC vs C-Corp (la S-Corp est
# fermée aux non-résidents).
_US_CO_STEPS: list[_Step] = [
    (
        "Choisir la structure & l'État",
        5,
        "LLC (pass-through, simple) → structure opérationnelle légère sans "
        "installation (facturation US, e-commerce, conseil, holding). C-Corp "
        "(impôt fédéral 21 %, double imposition) → levée de fonds VC OU support "
        "d'un visa E-2/L-1 avec installation (standard Delaware). ⚠️ S-Corp FERMÉE "
        "aux non-résidents → choix réel = LLC vs C-Corp. État : Delaware (VC) / "
        "Wyoming (coûts bas, pas d'impôt d'État) / État d'activité réelle. "
        "⚠️ s'immatriculer au DE/WY ne dispense PAS de s'enregistrer là où la "
        "société opère (nexus).",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Formation & registered agent",
        7,
        "Formation de l'entité et désignation d'un registered agent.",
        "provides_documents",
        [
            "Articles",
            "Registered agent dans l'État",
            "Statuts/operating agreement",
        ],
    ),
    (
        "EIN, ITIN & compte bancaire",
        45,
        "EIN (Form SS-4 ; plusieurs semaines sans SSN, voie fax/courrier), ITIN "
        "(W-7) souvent nécessaire. Compte via fintech (Mercury/Wise/Relay) si pas "
        "de déplacement. Enregistrements fiscaux d'État.",
        "provides_documents",
        [],
    ),
    (
        "Conformité actionnariat étranger (dès l'an 1)",
        None,
        "🟢 Single-member LLC détenue par un étranger → Form 5472 + 1120 pro forma "
        "(échéance 15 avril, PÉNALITÉ 25 000 USD). C-Corp → 1120 ; 5472 si "
        "actionnaire étranger lié ≥ 25 % ; retenue dividendes 30 % → 15 % "
        "(convention US-France). 🔴 BOI/Corporate Transparency Act : règle FinCEN "
        "mars 2025 recentrée sur les entités étrangères — périmètre à vérifier sur "
        "fincen.gov/boi.",
        "provides_documents",
        [],
    ),
]

# Suisse — Permis B non-actif : ressortissant UE/AELE sans activité lucrative.
# Admission de plein droit sous ALCP.
_CH_BNA_STEPS: list[_Step] = [
    (
        "Réunir les preuves de moyens & d'assurance",
        7,
        "🟠 Moyens financiers suffisants (seuil indexé aux prestations "
        "complémentaires LPC, à confirmer par canton) + assurance maladie couvrant "
        "la Suisse. Aucune condition d'âge pour un ressortissant UE/AELE.",
        "provides_documents",
        [],
    ),
    (
        "Annonce d'arrivée à la commune (dans les 14 j)",
        14,
        "Annonce d'arrivée à la commune dans les 14 jours. Présence requise.",
        "provides_documents",
        [
            "Passeport/CNI",
            "Preuve de logement",
            "Preuve de moyens",
            "Assurance",
        ],
    ),
    (
        "Délivrance du permis B",
        21,
        "Permis B (5 ans). 🟠 Forfait fiscal disponible dans la plupart des "
        "cantons (régime FISCAL distinct, à négocier séparément avec le fisc "
        "cantonal — pas un droit de séjour). Le canton est déterminant (fiscalité).",
        "provides_documents",
        [],
    ),
]

# Suisse — Permis L/B salarié (UE/AELE) : contrat de travail suisse, pas de
# contingent.
_CH_EMP_STEPS: list[_Step] = [
    (
        "Contrat de travail signé",
        7,
        "Type de titre selon la durée du contrat : < 3 mois = annonce simple · "
        "3-12 mois = permis L · ≥ 12 mois = permis B (5 ans). Pas de contingent ni "
        "de test du marché pour un ressortissant UE/AELE.",
        "provides_documents",
        [],
    ),
    (
        "Annonce/demande à la commune & au canton",
        14,
        "Annonce/demande auprès de la commune et du canton.",
        "provides_documents",
        ["Passeport/CNI", "Contrat de travail", "Preuve de logement"],
    ),
    (
        "Délivrance du permis L ou B",
        21,
        "Permis C (établissement) à 5 ans pour les ressortissants UE/AELE "
        "(réciprocité). Le canton détermine la fiscalité personnelle.",
        "provides_documents",
        [],
    ),
]

# Suisse — Indépendant / entrepreneur (UE/AELE) : activité indépendante réelle et
# viable, admission sous ALCP.
_CH_IND_STEPS: list[_Step] = [
    (
        "Démontrer une activité indépendante réelle et viable",
        14,
        "Business plan, comptabilité prévisionnelle, locaux/clients — l'activité "
        "doit être effective (pas fictive). Affiliation AVS comme indépendant.",
        "provides_documents",
        [],
    ),
    (
        "Annonce à la commune & demande de permis B",
        14,
        "Annonce à la commune et demande de permis B (indépendant).",
        "provides_documents",
        ["Passeport/CNI", "Preuves d'activité indépendante", "Logement"],
    ),
    (
        "Délivrance du permis B (indépendant)",
        21,
        "Possibilité de constituer une Sàrl/SA en parallèle (voir le parcours "
        "société). Le canton détermine la charge fiscale.",
        "provides_documents",
        [],
    ),
]

# Suisse — Rentier hors-UE (≥ 55 ans, art. 28 LEI) : sans activité lucrative,
# transfert du centre de vie. Voie discrétionnaire et très cantonale.
_CH_RET_STEPS: list[_Step] = [
    (
        "Évaluer l'éligibilité & choisir un canton accueillant",
        14,
        "🔴 Art. 28 LEI / art. 25 OASA : ≥ 55 ans + attaches personnelles "
        "PARTICULIÈRES avec la Suisse + aucune activité lucrative + moyens "
        "suffisants + transfert effectif du centre de vie. TRÈS discrétionnaire : "
        "certains cantons accueillants, d'autres restrictifs — le choix du canton "
        "est déterminant. Un rentier non-UE de MOINS de 55 ans n'a pas de voie "
        "claire.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Dépôt de la demande à l'autorité cantonale des migrations",
        60,
        "Dépôt de la demande auprès de l'autorité cantonale des migrations.",
        "provides_documents",
        [
            "Passeport",
            "Preuve des attaches avec la Suisse",
            "Preuve de moyens",
            "Assurance maladie",
            "Projet de domiciliation",
        ],
    ),
    (
        "Octroi du permis B (hors activité) & forfait fiscal",
        30,
        "🟠 Public cible du forfait fiscal (régime fiscal distinct, à négocier par "
        "ruling cantonal AVANT installation — pas un titre en soi).",
        "provides_documents",
        [],
    ),
]

# Suisse — Salarié hors-UE (art. 18-23 LEI) : voie la plus restrictive
# (contingent + priorité du marché + qualification élevée).
_CH_TCN_STEPS: list[_Step] = [
    (
        "Vérifier les conditions (le goulot)",
        14,
        "🔴 Conditions cumulatives : intérêt économique + profil "
        "CADRE/SPÉCIALISTE/QUALIFIÉ + salaire et conditions usuels + PRIORITÉ du "
        "marché indigène/UE-AELE (l'employeur doit prouver l'absence de candidat "
        "suisse/UE) + CONTINGENT annuel (risque de blocage si quota épuisé). Sans "
        "employeur et sans profil cadre/spécialiste, cette voie est de fait "
        "FERMÉE.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "L'employeur dépose la demande (autorité cantonale + SEM)",
        60,
        "Demande portée par l'employeur auprès de l'autorité cantonale et du SEM.",
        "provides_documents",
        [
            "Contrat",
            "Preuve de la recherche prioritaire",
            "Diplômes",
            "Justification du poste",
        ],
    ),
    (
        "Visa D & permis L/B (imputé sur le contingent)",
        30,
        "🟠 Permis imputé sur le contingent annuel de l'État tiers.",
        "provides_documents",
        [],
    ),
]

# Suisse — création de société (Sàrl / SA) : deux décisions n°1 = le dirigeant
# résident suisse et le canton.
_CH_CO_STEPS: list[_Step] = [
    (
        "Trancher dirigeant résident, canton & structure",
        7,
        "⚠️ DIRIGEANT RÉSIDENT OBLIGATOIRE : au moins une personne domiciliée en "
        "Suisse avec pouvoir de signature (art. 814 al. 3 / 718 al. 4 CO) — "
        "recrutement local, administrateur fiduciaire, ou installation du "
        "fondateur. Sans lui, pas de société. CANTON = levier fiscal n°1 : impôt "
        "bénéfice ~11,5 % (Zoug/Nidwald) à ~21 % (Berne) ; Genève ~14 % (n'est "
        "PLUS un canton à forte imposition). Structure : Sàrl (capital 20 000 CHF "
        "libéré, associés inscrits) / SA (100 000 CHF souscrit, min 50 000 libéré, "
        "actionnaires non inscrits).",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Statuts par acte notarié & libération du capital",
        7,
        "Acte authentique obligatoire + dépôt du capital sur compte de "
        "consignation (attestation bancaire).",
        "provides_documents",
        [],
    ),
    (
        "Inscription au registre du commerce (Zefix)",
        7,
        "Inscription de la société au registre du commerce (Zefix).",
        "provides_documents",
        [],
    ),
    (
        "TVA & assurances sociales",
        14,
        "🟠 IFD 8,5 % statutaire (~7,83 % effectif) + cantonal/communal (voir "
        "étape 1). TVA 8,1 % si CA > 100 000 CHF. Impôt anticipé 35 % sur "
        "dividendes (taux résiduels par convention). Droit de timbre 1 % au-delà "
        "de 1 M CHF d'apport. NOTE forfait fiscal : régime pour rentier étranger "
        "sans activité (plancher fédéral 400 000 CHF / 7× loyer, ruling cantonal) "
        "— distinct, pas un titre de séjour ; aboli à "
        "Zurich/Bâle/Schaffhouse/Appenzell RE.",
        "provides_documents",
        [],
    ),
]

# Canada — Express Entry : RP fédérale via le système de points CRS (FSW / CEC /
# FST). Le français est un atout majeur.
_CA_EE_STEPS: list[_Step] = [
    (
        "Vérifier l'éligibilité & estimer le CRS",
        14,
        "🟠 FSW = note 67/100 minimum. CEC = ~1 an d'expérience qualifiée au "
        "Canada. Profession (niveau TEER), langue (CLB/NCLC), âge, diplômes notent "
        'le CRS (max 1200). ⚠️ FRANÇAIS = ATOUT MAJEUR : tirages "compétence en '
        'français" à seuils CRS nettement plus bas. Une nomination PNP ajoute '
        "+600 CRS (invitation quasi garantie). Pas de visa retraité/investisseur "
        "au Canada.",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Tests de langue, équivalence de diplômes (ECA) & profil dans le bassin",
        60,
        "Tests de langue, ECA des diplômes, et création du profil dans le bassin.",
        "provides_documents",
        [
            "Test linguistique (IELTS/TEF/TCF)",
            "ECA des diplômes",
            "Passeport",
            "Preuves d'expérience",
        ],
    ),
    (
        "Invitation à présenter une demande (ITA) & demande de RP",
        180,
        "🟠 Frais RP ~950 $ + RPRF 575 $ + biométrie 85 $. Seuils CRS des rondes "
        "très volatils (canada.ca/IRCC), à reconfirmer.",
        "provides_documents",
        ["Casier", "Examen médical", "Preuve de fonds d'établissement"],
    ),
]

# Canada — Provincial Nominee Program : nomination provinciale qui booste le
# dossier RP (+600 CRS en lien avec Express Entry).
_CA_PNP_STEPS: list[_Step] = [
    (
        "Identifier la province & le volet adapté au profil",
        14,
        "🔴 Chaque province a ses volets et critères propres (souvent liés à une "
        "profession en demande, une offre d'emploi locale, ou un lien avec la "
        "province). Allocation PNP 2025 réduite (~55 000) — disponibilité des "
        "volets volatile, à confirmer par province (OINP/BC PNP/AAIP…).",
        None,  # réalisé par l'agence — non nommable sur un sample
        [],
    ),
    (
        "Déclaration d'intérêt / candidature provinciale",
        90,
        "Déclaration d'intérêt ou candidature auprès de la province ciblée.",
        "provides_documents",
        [
            "Preuves de profession/expérience",
            "Langue",
            "Éventuelle offre d'emploi ou lien provincial",
        ],
    ),
    (
        "Nomination provinciale → demande de RP fédérale",
        180,
        "La nomination ajoute +600 CRS (via Express Entry, volet aligné) OU "
        'constitue une voie PNP "base" hors Express Entry, puis demande de RP à '
        "IRCC.",
        "provides_documents",
        [],
    ),
]

# Québec — PSTQ / Arrima : système de sélection DISTINCT du fédéral ; le français
# y est fortement valorisé. Sélection québécoise (CSQ) puis RP fédérale.
_CA_QC_STEPS: list[_Step] = [
    (
        "Créer un profil Arrima (déclaration d'intérêt)",
        14,
        "⚠️ Système québécois SÉPARÉ d'Express Entry. PSTQ = Programme de sélection "
        "des travailleurs qualifiés (volets distincts). 🟠 Le FRANÇAIS est un "
        "levier majeur (seuils et points). Libellés des volets et seuils à "
        "confirmer (Québec.ca/MIFI).",
        "provides_documents",
        [],
    ),
    (
        "Invitation du Québec & demande de CSQ (MIFI)",
        120,
        "🟠 Tarifs MIFI à confirmer. Le CSQ = Certificat de sélection du Québec "
        "(sélection provinciale).",
        "provides_documents",
        [
            "Preuves de français",
            "Diplômes",
            "Expérience",
            "Projet d'établissement",
        ],
    ),
    (
        "Demande de RP fédérale (IRCC) avec le CSQ",
        180,
        "La RP reste délivrée par le fédéral, mais la SÉLECTION est québécoise. "
        "NOTE : le PEQ (Programme de l'expérience québécoise) est une voie "
        "accélérée pour diplômés/travailleurs déjà au Québec.",
        "provides_documents",
        ["CSQ", "Casier", "Examen médical", "Preuve de fonds"],
    ),
]

# Canada — permis de travail → expérience canadienne → RP : le tremplin le plus
# courant (entrée temporaire, puis CEC).
_CA_WP_STEPS: list[_Step] = [
    (
        "Obtenir le permis de travail (IMP ou LMIA)",
        60,
        "Deux voies : IMP (LMIA-exempt : transfert intra-entreprise C12, accords "
        "commerciaux, jeunes pros/IEC-PVT pour les Français éligibles) OU TFWP "
        "(avec étude d'impact LMIA, plus lourde). 🟠 Le retrait des points CRS pour "
        "offre d'emploi (printemps 2025) rend le PNP plus central que l'offre "
        "d'emploi seule.",
        "provides_documents",
        [],
    ),
    (
        "Travailler au Canada & accumuler l'expérience qualifiée",
        365,
        "~1 an d'expérience qualifiée (TEER 0/1/2/3) ouvre la CEC (Canadian Experience Class).",
        "provides_documents",
        [],
    ),
    (
        "Demande de RP via Express Entry (CEC)",
        180,
        "La CEC est la voie la plus rapide vers la RP pour qui a déjà de "
        "l'expérience canadienne. Français = atout (tirages dédiés).",
        "provides_documents",
        [],
    ),
]

# Canada — Start-up Visa : projet innovant soutenu par une organisation désignée.
# Seule vraie voie entrepreneuriale vers la RP (l'investisseur a disparu).
_CA_SUV_STEPS: list[_Step] = [
    (
        "Obtenir le soutien d'une organisation désignée",
        90,
        "🟠 Organisation désignée : capital-risque ≥ 200 000 $ / investisseur "
        "providentiel ≥ 75 000 $ / incubateur (pas de fonds requis). Lettre de "
        "soutien requise. ⚠️ Pas de visa investisseur/golden au Canada — c'est la "
        "voie projet.",
        "provides_documents",
        [],
    ),
    (
        "Constituer le dossier SUV",
        60,
        "Constitution du dossier Start-up Visa.",
        "provides_documents",
        [
            "Langue CLB 5 (anglais ou français)",
            "Preuve de fonds d'établissement (~14 700 $ pour 1 personne)",
            "≥ 10 % des droits de vote",
            "Demandeurs + organisation détenant > 50 %",
        ],
    ),
    (
        "Demande de RP (et permis de travail temporaire en attendant)",
        365,
        "La RP est directe (pas conditionnelle). Un permis de travail peut être "
        "obtenu pour démarrer pendant l'instruction de la RP.",
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
    (MU_OPI_NAME, "MU", _MU_OPI_STEPS),
    (MU_OPP_NAME, "MU", _MU_OPP_STEPS),
    (MU_OPS_NAME, "MU", _MU_OPS_STEPS),
    (MU_PV_NAME, "MU", _MU_PV_STEPS),
    (MU_RE_NAME, "MU", _MU_RE_STEPS),
    (MU_CO_NAME, "MU", _MU_CO_STEPS),
    (TH_DTV_NAME, "TH", _TH_DTV_STEPS),
    (TH_LTR_NAME, "TH", _TH_LTR_STEPS),
    (TH_OA_NAME, "TH", _TH_OA_STEPS),
    (TH_PRIV_NAME, "TH", _TH_PRIV_STEPS),
    (TH_NONB_NAME, "TH", _TH_NONB_STEPS),
    (TH_CO_NAME, "TH", _TH_CO_STEPS),
    (ID_RW_NAME, "ID", _ID_RW_STEPS),
    (ID_SH_NAME, "ID", _ID_SH_STEPS),
    (ID_RET_NAME, "ID", _ID_RET_STEPS),
    (ID_WORK_NAME, "ID", _ID_WORK_STEPS),
    (ID_INV_NAME, "ID", _ID_INV_STEPS),
    (ID_CO_NAME, "ID", _ID_CO_STEPS),
    (PH_SRRV_NAME, "PH", _PH_SRRV_STEPS),
    (PH_SIRV_NAME, "PH", _PH_SIRV_STEPS),
    (PH_13A_NAME, "PH", _PH_13A_STEPS),
    (PH_CO_NAME, "PH", _PH_CO_STEPS),
    (PT_CRUE_NAME, "PT", _PT_CRUE_STEPS),
    (PT_D7_NAME, "PT", _PT_D7_STEPS),
    (PT_D8_NAME, "PT", _PT_D8_STEPS),
    (PT_GV_NAME, "PT", _PT_GV_STEPS),
    (VN_WP_NAME, "VN", _VN_WP_STEPS),
    (VN_INV_NAME, "VN", _VN_INV_STEPS),
    (VN_TT_NAME, "VN", _VN_TT_STEPS),
    (VN_RO_NAME, "VN", _VN_RO_STEPS),
    (US_E2_NAME, "US", _US_E2_STEPS),
    (US_L1_NAME, "US", _US_L1_STEPS),
    (US_O1_NAME, "US", _US_O1_STEPS),
    (US_H1B_NAME, "US", _US_H1B_STEPS),
    (US_EB5_NAME, "US", _US_EB5_STEPS),
    (US_NIW_NAME, "US", _US_NIW_STEPS),
    (US_CO_NAME, "US", _US_CO_STEPS),
    (CH_BNA_NAME, "CH", _CH_BNA_STEPS),
    (CH_EMP_NAME, "CH", _CH_EMP_STEPS),
    (CH_IND_NAME, "CH", _CH_IND_STEPS),
    (CH_RET_NAME, "CH", _CH_RET_STEPS),
    (CH_TCN_NAME, "CH", _CH_TCN_STEPS),
    (CH_CO_NAME, "CH", _CH_CO_STEPS),
    (CA_EE_NAME, "CA", _CA_EE_STEPS),
    (CA_PNP_NAME, "CA", _CA_PNP_STEPS),
    (CA_QC_NAME, "CA", _CA_QC_STEPS),
    (CA_WP_NAME, "CA", _CA_WP_STEPS),
    (CA_SUV_NAME, "CA", _CA_SUV_STEPS),
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
