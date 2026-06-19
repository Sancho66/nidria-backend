"""Library SAMPLE journeys, seeded at boot (idempotent), like the system
roles: agency_id NULL + is_sample=true → shared, read-only for agencies, an
agency consumes one by CLONING it.

DOER on an agency-less sample:
- client step  → participant type=expat (the case principal).
- agency step  → participant type=agent with agent_id NULL = "the agency in
  general" (no named member — symmetric to the validator). This is the `None`
  role below.
- a PROVIDER doer (escribano, sworn translator…) is NOT a sample participant
  (it is case-scoped): it is carried on the CLIENT step as `provides_documents`
  + a content_note "à assigner au dossier"; the agency names it on the CLONE.
The validator is always "the agency" (validated_by_type='agent', agent_id
NULL). Amounts and delays are indicative, never a rule.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.journey import (
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateStep,
    StepPrerequisite,
)
from shared.models.step_requirement import StepRequirement
from src.core.enums import StepParticipantRole

# A step: (name, estimated_days | None, content_note, role | None, [doc labels]).
# estimated_days None ⇒ open-ended (e.g. a multi-year backlog wait). role is the
# DOER: a StepParticipantRole string ⇒ the CLIENT (type=expat) with that role;
# None ⇒ the AGENCY does it (type=agent, agent_id NULL = "the agency in
# general"). Steps form a linear AND chain (each requires the previous). The
# validator is the agency on every step.
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
        None,  # acteur = l'agence (type=agent, agent_id NULL) (content_note)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL) (content_note)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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
        None,  # acteur = l'agence (type=agent, agent_id NULL)
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


def _add_participant(db: AsyncSession, step_id: uuid.UUID, role: str | None) -> None:
    """The step's DOER. role None ⇒ the agency in general (type=agent, agent_id
    NULL); a StepParticipantRole string ⇒ the client (type=expat)."""
    if role is None:
        db.add(
            JourneyStepParticipant(
                step_id=step_id,
                type="agent",
                agent_id=None,  # NULL = the agency in general
                role=StepParticipantRole.EXECUTANT.value,
            )
        )
    else:
        db.add(JourneyStepParticipant(step_id=step_id, type="expat", agent_id=None, role=role))


# EN + ES variants of the samples (BLOC 3). Keyed by the sample's SCALAR name
# (the idempotence anchor — never keyed on a blob). Per sample: the template
# `name` variants + a `steps` list PARALLEL to the spec by position, each a
# (step-name variants, content-note variants) pair. An ABSENT lang key = FR
# fallback (we never invent a translation). The FR scalar + "fr" blob key are
# never touched. Protected proper names (visa/permit names, legal codes,
# authority acronyms, fiscal sigles) and reliability markers (🟢🟠🔴⚠️) are kept
# verbatim across languages. Amounts/thresholds/delays are kept identical.
_SAMPLE_I18N: dict[str, dict[str, object]] = {
    PY1_NAME: {
        "name": {
            "en": "Paraguay — Temporary Residence + Cédula",
            "es": "Paraguay — Residencia temporal + Cédula",
        },
        "steps": [
            (
                {"en": "Build the application file", "es": "Preparación del expediente"},
                {
                    "en": (
                        "Gather the documents: apostilled birth certificate, apostilled criminal "
                        "record, valid passport. The apostille is requested from the competent "
                        "authority of your country of origin."
                    ),
                    "es": (
                        "Reúna los documentos: partida de nacimiento apostillada, antecedentes "
                        "penales apostillados, pasaporte válido. La apostilla se solicita ante la "
                        "autoridad competente de su país de origen."
                    ),
                },
            ),
            (
                {
                    "en": "Sworn translation of the documents",
                    "es": "Traducción jurada de los documentos",
                },
                {
                    "en": (
                        "Translation by a registered sworn translator. To assign on the dossier: "
                        "the external provider is named on the dossier, not on this shared "
                        "template."
                    ),
                    "es": (
                        "Traducción por un traductor jurado inscrito. A asignar en el expediente: "
                        "el proveedor externo se designa en el expediente, no en esta plantilla "
                        "compartida."
                    ),
                },
            ),
            (
                {
                    "en": "File submission to immigration (DNM)",
                    "es": "Presentación del expediente ante inmigración (DNM)",
                },
                {
                    "en": (
                        "Submission handled by the agency to the Dirección Nacional de "
                        "Migraciones. DNM fee ≈ 2 700 000 Gs (indicative amount, not fixed)."
                    ),
                    "es": (
                        "Presentación realizada por la agencia ante la Dirección Nacional de "
                        "Migraciones. Tasa DNM ≈ 2 700 000 Gs (importe indicativo, no fijo)."
                    ),
                },
            ),
            (
                {
                    "en": "Obtaining the temporary residence",
                    "es": "Obtención de la residencia temporal",
                },
                {
                    "en": (
                        "DNM administrative processing time, variable (≈ 30 to 45 days, "
                        "indicative)."
                    ),
                    "es": "Plazo administrativo de la DNM, variable (≈ 30 a 45 días, indicativo).",
                },
            ),
            (
                {
                    "en": "Cédula application (identity card)",
                    "es": "Solicitud de la cédula (documento de identidad)",
                },
                {
                    "en": (
                        "Fingerprints and photo at the identification office. Step unlocked once "
                        "the temporary residence is obtained."
                    ),
                    "es": (
                        "Toma de huellas y foto en la oficina de identificación. Etapa "
                        "desbloqueada una vez obtenida la residencia temporal."
                    ),
                },
            ),
            (
                {"en": "Issuance of the cédula", "es": "Entrega de la cédula"},
                {
                    "en": "Cédula production time, variable (≈ 3 to 9 months, indicative).",
                    "es": (
                        "Plazo de fabricación de la cédula, variable (≈ 3 a 9 meses, indicativo)."
                    ),
                },
            ),
        ],
    },
    TH_DTV_NAME: {
        "name": {
            "en": "Thailand — Destination Thailand Visa (DTV, nomad)",
            "es": "Tailandia — Destination Thailand Visa (DTV, nómada)",
        },
        "steps": [
            (
                {
                    "en": "Check eligibility & savings",
                    "es": "Verificar la elegibilidad y el ahorro",
                },
                {
                    "en": (
                        "🟠 Savings ≥ 500 000 THB (indicative, ~36 THB/USD). ⚠️ The DTV DOES NOT "
                        "AUTHORIZE work for a THAI client/employer. ⚠️ Does NOT lead to permanent "
                        "residence (neither the DTV, nor retirement, nor Privilege count toward "
                        "it — only Non-B + work permit does)."
                    ),
                    "es": (
                        "🟠 Ahorro ≥ 500 000 THB (indicativo, ~36 THB/USD). ⚠️ El DTV NO AUTORIZA "
                        "el trabajo para un cliente/empleador TAILANDÉS. ⚠️ NO conduce a la "
                        "residencia permanente (ni el DTV, ni la jubilación, ni Privilege cuentan "
                        "para ella — solo Non-B + work permit lo permite)."
                    ),
                },
            ),
            (
                {
                    "en": "Application via the e-visa portal (MFA)",
                    "es": "Solicitud a través del portal e-visa (MFA)",
                },
                {
                    "en": (
                        "🟠 Practice varies by consulate (several months of bank history sometimes "
                        "required). 180-day extension fee ≈ 10 000 THB (and NOT ~1 900 — a "
                        "frequent error in commercial sources)."
                    ),
                    "es": (
                        "🟠 Práctica heterogénea según el consulado (a veces se exige historial "
                        "bancario de varios meses). Tarifa de extensión de 180 días ≈ 10 000 THB "
                        "(y NO ~1 900 — error frecuente de las fuentes comerciales)."
                    ),
                },
            ),
            (
                {"en": "Issuance of the DTV", "es": "Emisión del DTV"},
                {
                    "en": "5 years, multiple-entry, 180 days per entry (extendable once).",
                    "es": (
                        "5 años, entradas múltiples, 180 días por entrada (prorrogable una vez)."
                    ),
                },
            ),
        ],
    },
    PT_D7_NAME: {
        "name": {
            "en": "Portugal — D7 Visa (passive income / retiree, non-EU)",
            "es": "Portugal — Visa D7 (renta pasiva / jubilado, fuera de la UE)",
        },
        "steps": [
            (
                {
                    "en": "NIF + Portuguese bank account",
                    "es": "NIF + cuenta bancaria portuguesa",
                },
                {
                    "en": "Tax representative required for a non-EU non-resident.",
                    "es": (
                        "Representante fiscal obligatorio para un no residente de fuera de la UE."
                    ),
                },
            ),
            (
                {
                    "en": "D7 visa application at the consulate",
                    "es": "Solicitud de visa D7 en el consulado",
                },
                {
                    "en": (
                        "🟠 Threshold indexed to the SMN (~870 €/month 2025, to be confirmed; SMN "
                        "paid 14×/year — clarify ×12/×14). ⚠️ D7 = PASSIVE income only (active "
                        "remote work falls under the D8)."
                    ),
                    "es": (
                        "🟠 Umbral indexado al SMN (~870 €/mes 2025, a confirmar; SMN pagado "
                        "14×/año — aclarar ×12/×14). ⚠️ D7 = renta PASIVA únicamente (el "
                        "teletrabajo activo corresponde al D8)."
                    ),
                },
            ),
            (
                {
                    "en": "Conversion to a residence permit at AIMA",
                    "es": "Conversión en permiso de residencia en AIMA",
                },
                {
                    "en": (
                        "🔴 Real AIMA processing time (massive backlog): months to > 1 year, not "
                        "guaranteed. Present in 2 horizons (consular vs real AIMA). ⚠️ NHR "
                        "abolished: no personal tax exemption for an ordinary retiree/rentier. "
                        "Biometrics required."
                    ),
                    "es": (
                        "🔴 Plazo real de AIMA (atraso masivo): de meses a > 1 año, no "
                        "garantizado. Presentar en 2 horizontes (consular vs AIMA real). ⚠️ NHR "
                        "suprimido: sin exención fiscal personal para un jubilado/rentista "
                        "ordinario. Biometría requerida."
                    ),
                },
            ),
        ],
    },
    RUC_NAME: {
        "name": {
            "en": "Paraguay — Company formation (RUC)",
            "es": "Paraguay — Creación de empresa (RUC)",
        },
        "steps": [
            (
                {
                    "en": "Electronic identity & status preparation",
                    "es": "Preparación de identidad electrónica y estatuto",
                },
                {
                    "en": (
                        "EAS: no minimum capital or deposit. If the foreigner has no Paraguayan "
                        "cédula, they incorporate through a legal representative who holds one."
                    ),
                    "es": (
                        "EAS: sin capital mínimo ni depósito. Si el extranjero no tiene cédula "
                        "paraguaya, constituye a través de un representante legal que la tenga."
                    ),
                },
            ),
            (
                {
                    "en": "Online incorporation via SUACE (eas.mic.gov.py)",
                    "es": "Constitución en línea vía SUACE (eas.mic.gov.py)",
                },
                {
                    "en": (
                        "Incorporation in 72 h (often 24–48 h) with a proforma statute; ≈ 8 "
                        "business days with a customized statute. The escribano may carry out "
                        "this step — to assign on the dossier."
                    ),
                    "es": (
                        "Constitución en 72 h (a menudo 24–48 h) con estatuto proforma; ≈ 8 días "
                        "hábiles con estatuto personalizado. El escribano puede realizar este "
                        "paso — a asignar en el expediente."
                    ),
                },
            ),
            (
                {
                    "en": "Automatic registrations (RUC / IPS / MTESS)",
                    "es": "Inscripciones automáticas (RUC / IPS / MTESS)",
                },
                {
                    "en": (
                        "Registration automatically generates the RUC (Finance), the IPS (social "
                        "security) and the MTESS (labor). No registration with the Registro "
                        "Público de Comercio is needed to operate."
                    ),
                    "es": (
                        "La inscripción genera automáticamente el RUC (Hacienda), el IPS "
                        "(seguridad social) y el MTESS (trabajo). No se necesita inscripción en "
                        "el Registro Público de Comercio para operar."
                    ),
                },
            ),
        ],
    },
    PERM_NAME: {
        "name": {
            "en": "Paraguay — Permanent residence (category change)",
            "es": "Paraguay — Residencia permanente (cambio de categoría)",
        },
        "steps": [
            (
                {
                    "en": "Check eligibility and timing",
                    "es": "Verificar la elegibilidad y el momento",
                },
                {
                    "en": (
                        "File within the 90 days before the 2-year temporary carnet expires "
                        "(possible up to 1 month after expiry, with a fine). Must not have been "
                        "absent more than one year cumulatively over the 2 years. No investment "
                        "requirement for the conversion."
                    ),
                    "es": (
                        "Presentar dentro de los 90 días previos al vencimiento del carnet "
                        "temporal de 2 años (posible hasta 1 mes después del vencimiento, con "
                        "multa). No haberse ausentado más de un año acumulado en los 2 años. Sin "
                        "requisito de inversión para la conversión."
                    ),
                },
            ),
            (
                {
                    "en": "Build the category-change file",
                    "es": "Preparar el expediente de cambio de categoría",
                },
                {
                    "en": (
                        "Gather the category-change documents. Proof of solvency differs: "
                        "employment contract (employees) or company deeds + shareholder register "
                        "(entrepreneurs)."
                    ),
                    "es": (
                        "Reunir los documentos del cambio de categoría. Las pruebas de solvencia "
                        "difieren: contrato de trabajo (asalariados) o actas de sociedad + "
                        "registro de accionistas (empresarios)."
                    ),
                },
            ),
            (
                {"en": "Submission to the DNM", "es": "Presentación ante la DNM"},
                {
                    "en": "In-person submission to the Dirección Nacional de Migraciones.",
                    "es": "Presentación en persona ante la Dirección Nacional de Migraciones.",
                },
            ),
            (
                {
                    "en": "Permanent carnet issuance + cédula renewal",
                    "es": "Emisión del carnet permanente + renovación de la cédula",
                },
                {
                    "en": (
                        "Definitive permanent carnet, to be renewed every 10 years. The permanent "
                        "resident must not be absent more than 3 consecutive years without "
                        "justification. Conversion available after ≈ 21 to 24 months of temporary "
                        "residence."
                    ),
                    "es": (
                        "Carnet permanente definitivo, a renovar cada 10 años. El residente "
                        "permanente no debe ausentarse más de 3 años consecutivos sin "
                        "justificación. Conversión accesible tras ≈ 21 a 24 meses de residencia "
                        "temporal."
                    ),
                },
            ),
        ],
    },
    CY_NAME: {
        "name": {
            "en": "Cyprus — EU residence registration (Yellow Slip, MEU1)",
            "es": "Chipre — Registro de residencia UE (Yellow Slip, MEU1)",
        },
        "steps": [
            (
                {
                    "en": "Gather the documents (MEU1 form)",
                    "es": "Reunir los documentos (formulario MEU1)",
                },
                {
                    "en": (
                        "Application to file within 4 months of entry. Fintech bank statements "
                        "(Revolut, Wise, N26) may be refused."
                    ),
                    "es": (
                        "Solicitud a presentar dentro de los 4 meses tras la entrada. Los "
                        "extractos de bancos fintech (Revolut, Wise, N26) pueden ser rechazados."
                    ),
                },
            ),
            (
                {
                    "en": "Appointment at the CRMD (district Immigration Unit)",
                    "es": "Cita en el CRMD (Immigration Unit del distrito)",
                },
                {
                    "en": (
                        "Offices in Nicosia / Limassol / Larnaca / Paphos. Book ≈ 3 to 4 weeks in "
                        "advance."
                    ),
                    "es": (
                        "Oficinas de Nicosia / Limassol / Larnaca / Paphos. Reservar ≈ 3 a 4 "
                        "semanas de antelación."
                    ),
                },
            ),
            (
                {
                    "en": "In-person submission + certificate issuance",
                    "es": "Presentación en persona + emisión del certificado",
                },
                {
                    "en": (
                        "In-person presence required (photo on site). Certificate often issued "
                        "the same day or within a few days; it does not expire. Indicative amount "
                        "(one source cites 85 €, to confirm at the counter)."
                    ),
                    "es": (
                        "Presencia requerida (foto en el lugar). Certificado a menudo emitido el "
                        "mismo día o en pocos días; no caduca. Importe indicativo (una fuente "
                        "cita 85 €, a confirmar en ventanilla)."
                    ),
                },
            ),
        ],
    },
    CYF_NAME: {
        "name": {
            "en": "Cyprus — Non-EU passive-income residence (Pink Slip + Category F)",
            "es": "Chipre — Residencia no-UE por renta pasiva (Pink Slip + Categoría F)",
        },
        "steps": [
            (
                {
                    "en": "Preparation & legal entry to Cyprus",
                    "es": "Preparación y entrada legal a Chipre",
                },
                {
                    "en": (
                        "Foreign income ≈ 24 000 €/year for the Pink Slip (+20 % spouse, +15 "
                        "%/child). Application to file ≈ 7 days after arrival. Fintech bank "
                        "statements (Revolut/Wise/N26) sometimes refused. Indicative amounts."
                    ),
                    "es": (
                        "Renta extranjera ≈ 24 000 €/año para el Pink Slip (+20 % cónyuge, +15 "
                        "%/hijo). Solicitud a presentar ≈ 7 días tras la llegada. Extractos de "
                        "bancos fintech (Revolut/Wise/N26) a veces rechazados. Importes "
                        "indicativos."
                    ),
                },
            ),
            (
                {"en": "Medical examination in Cyprus", "es": "Examen médico en Chipre"},
                {
                    "en": (
                        "Hepatitis B/C, HIV, syphilis tests + tuberculosis X-ray; certificate < 4 "
                        "months. Health insurance required."
                    ),
                    "es": (
                        "Pruebas de hepatitis B/C, VIH, sífilis + radiografía de tuberculosis; "
                        "certificado < 4 meses. Seguro de salud requerido."
                    ),
                },
            ),
            (
                {
                    "en": "Pink Slip submission (annual residence permit)",
                    "es": "Presentación del Pink Slip (permiso de residencia anual)",
                },
                {
                    "en": (
                        "The receipt evidences legal stay during processing. Valid 1 year, "
                        "renewable. The ARC number stays the same throughout."
                    ),
                    "es": (
                        "El comprobante acredita la estancia legal durante la tramitación. Válido "
                        "1 año, renovable. El número ARC se mantiene igual todo el tiempo."
                    ),
                },
            ),
            (
                {
                    "en": "Category F submission (permanent residence)",
                    "es": "Presentación de la Categoría F (residencia permanente)",
                },
                {
                    "en": (
                        "🟠 Regulatory thresholds (recorded in 2023). File early: processing is "
                        "very long."
                    ),
                    "es": (
                        "🟠 Umbrales reglamentarios (registrados en 2023). Presentar pronto: la "
                        "tramitación es muy larga."
                    ),
                },
            ),
            (
                {
                    "en": "Wait & annual Pink Slip renewal (Category F backlog)",
                    "es": "Espera y renovación anual del Pink Slip (atraso de la Categoría F)",
                },
                {
                    "en": (
                        "🔴 Category F backlog estimated at 5–7 years (2020 files still pending). "
                        "Renew the Pink Slip EVERY year until the PR is issued. Never promise a "
                        "fast PR through this route."
                    ),
                    "es": (
                        "🔴 Atraso de la Categoría F estimado en 5–7 años (expedientes de 2020 aún "
                        "en curso). Renovar el Pink Slip CADA año hasta la emisión de la PR. "
                        "Nunca prometer una PR rápida por esta vía."
                    ),
                },
            ),
            (
                {
                    "en": "Category F issuance (permanent residence)",
                    "es": "Emisión de la Categoría F (residencia permanente)",
                },
                {
                    "en": "Permanent permit, card to be renewed every 10 years.",
                    "es": "Permiso permanente, tarjeta a renovar cada 10 años.",
                },
            ),
        ],
    },
    DNV_NAME: {
        "name": {
            "en": "Cyprus — Digital Nomad Visa (non-EU)",
            "es": "Chipre — Digital Nomad Visa (no-UE)",
        },
        "steps": [
            (
                {
                    "en": "Check quota availability BEFORE any step",
                    "es": "Verificar la disponibilidad del cupo ANTES de cualquier gestión",
                },
                {
                    "en": (
                        "🔴 CRITICAL. Official quota = 500 permits, reached as early as 2023; the "
                        "“1 000” is NOT confirmed. Check real availability with the Deputy "
                        "Ministry of Migration BEFORE any client promise."
                    ),
                    "es": (
                        "🔴 CRÍTICO. Cupo oficial = 500 permisos, alcanzado ya en 2023; el “1 000” "
                        "NO está confirmado. Verificar la disponibilidad real ante el Deputy "
                        "Ministry of Migration ANTES de cualquier promesa al cliente."
                    ),
                },
            ),
            (
                {
                    "en": "Gather the documents & enter Cyprus",
                    "es": "Reunir los documentos y entrar a Chipre",
                },
                {
                    "en": "Application within 3 months of entry. Indicative amount.",
                    "es": "Solicitud dentro de los 3 meses tras la entrada. Importe indicativo.",
                },
            ),
            (
                {
                    "en": "Submission at the CRMD (Nicosia) + biometrics",
                    "es": "Presentación en el CRMD (Nicosia) + biometría",
                },
                {"en": "Processing ≈ 5 to 7 weeks.", "es": "Tramitación ≈ 5 a 7 semanas."},
            ),
            (
                {"en": "Issuance of the DNV permit", "es": "Emisión del permiso DNV"},
                {
                    "en": (
                        "1-year permit, renewable up to 2 years. ⚠️ Time spent on the DNV does "
                        "NOT count toward naturalization. Beyond 183 days/year = Cypriot tax "
                        "residence."
                    ),
                    "es": (
                        "Permiso de 1 año, renovable hasta 2 años. ⚠️ El tiempo en DNV NO cuenta "
                        "para la naturalización. Más allá de 183 días/año = residencia fiscal "
                        "chipriota."
                    ),
                },
            ),
        ],
    },
    LTD_NAME: {
        "name": {
            "en": "Cyprus — Company formation (LTD)",
            "es": "Chipre — Creación de empresa (LTD)",
        },
        "steps": [
            (
                {"en": "Name approval & KYC", "es": "Aprobación del nombre y KYC"},
                {
                    "en": "Cypriot lawyer required for the incorporation.",
                    "es": "Abogado chipriota obligatorio para la constitución.",
                },
            ),
            (
                {
                    "en": "Drafting the statutes (Memorandum & Articles of Association)",
                    "es": "Redacción de los estatutos (Memorandum & Articles of Association)",
                },
                {
                    "en": (
                        "≥ 1 director (a resident director helps tax substance), 1 secretary (≠ "
                        "sole director), registered office in Cyprus. No minimum capital (1 000 € "
                        "usual)."
                    ),
                    "es": (
                        "≥ 1 administrador (un administrador residente ayuda a la sustancia "
                        "fiscal), 1 secretario (≠ administrador único), domicilio social en "
                        "Chipre. Sin capital mínimo (1 000 € habitual)."
                    ),
                },
            ),
            (
                {
                    "en": "Filing with the Registrar of Companies & certificate issuance",
                    "es": "Presentación ante el Registrar of Companies y emisión de certificados",
                },
                {
                    "en": (
                        "Certificates of incorporation / directors / shareholders / registered "
                        "office."
                    ),
                    "es": (
                        "Certificados de incorporación / administradores / accionistas / domicilio."
                    ),
                },
            ),
            (
                {
                    "en": "Tax registration & bank account",
                    "es": "Registro fiscal y cuenta bancaria",
                },
                {
                    "en": (
                        "TIN within 60 days, VAT if applicable, UBO register, account opening. IS "
                        "15 % (since 1/1/2026); dividends ≈ 2.65 % effective under non-dom. "
                        "Recurring costs ≈ 2 800–4 500 €/year (audit mandatory). Indicative "
                        "figures."
                    ),
                    "es": (
                        "TIN en 60 días, VAT si aplica, registro UBO, apertura de cuenta. IS 15 % "
                        "(desde 1/1/2026); dividendos ≈ 2,65 % efectivo en non-dom. Costes "
                        "recurrentes ≈ 2 800–4 500 €/año (auditoría obligatoria). Cifras "
                        "indicativas."
                    ),
                },
            ),
        ],
    },
    FIC_NAME: {
        "name": {
            "en": "Cyprus — LTD company + non-EU director permit (FIC/BFU)",
            "es": "Chipre — Empresa LTD + permiso de directivo no-UE (FIC/BFU)",
        },
        "steps": [
            (
                {"en": "LTD incorporation", "es": "Constitución de la LTD"},
                {
                    "en": (
                        "See the “Company formation (LTD)” journey for the detail (name, "
                        "statutes, Registrar). The company is the prerequisite for the FIC/BFU "
                        "status."
                    ),
                    "es": (
                        "Ver el recorrido «Creación de empresa (LTD)» para el detalle (nombre, "
                        "estatutos, Registrar). La empresa es el requisito previo para el estatus "
                        "FIC/BFU."
                    ),
                },
            ),
            (
                {
                    "en": "FIC/BFU registration (Foreign Interest Company)",
                    "es": "Registro FIC/BFU (Foreign Interest Company)",
                },
                {
                    "en": (
                        "🟠 200 000 € deposit, independent offices required. Local employment "
                        "ratio 70:30 assessed from 2/1/2027. Indicative thresholds."
                    ),
                    "es": (
                        "🟠 Depósito de 200 000 €, oficinas independientes requeridas. Ratio de "
                        "empleo local 70:30 evaluado desde el 2/1/2027. Umbrales indicativos."
                    ),
                },
            ),
            (
                {
                    "en": "Director's residence + work permit application",
                    "es": "Solicitud del permiso de residencia y trabajo del directivo",
                },
                {
                    "en": (
                        "Via the BFU, NO labor-market test → permit ≈ 1 month. A salary ≥ 2 500 "
                        "€/month also opens eligibility for accelerated naturalization (3 years "
                        "Greek B1 / 4 years A2)."
                    ),
                    "es": (
                        "Vía la BFU, SIN prueba del mercado laboral → permiso ≈ 1 mes. Un salario "
                        "≥ 2 500 €/mes también abre la elegibilidad a la naturalización acelerada "
                        "(3 años griego B1 / 4 años A2)."
                    ),
                },
            ),
            (
                {
                    "en": "Permit issuance & start of activity",
                    "es": "Emisión del permiso e inicio de actividad",
                },
                {
                    "en": (
                        "Renewable permit. Taxation: IS 15 %, dividends ≈ 2.65 % non-dom, 50 % "
                        "income-tax exemption if salary > 55 000 €/year."
                    ),
                    "es": (
                        "Permiso renovable. Fiscalidad: IS 15 %, dividendos ≈ 2,65 % non-dom, "
                        "exención del 50 % del IR si salario > 55 000 €/año."
                    ),
                },
            ),
        ],
    },
    PA_FN_NAME: {
        "name": {
            "en": "Panama — Friendly Nations residence",
            "es": "Panamá — Residencia Friendly Nations",
        },
        "steps": [
            (
                {
                    "en": "Check eligibility (friendly nationality) & prepare the file",
                    "es": (
                        "Verificar la elegibilidad (nacionalidad amiga) y preparar el expediente"
                    ),
                },
                {
                    "en": (
                        "🟠 The list of ~50 friendly countries can be changed by decree — recheck "
                        "on migracion.gob.pa before the file. Panamanian lawyer mandatory."
                    ),
                    "es": (
                        "🟠 La lista de ~50 países amigos puede modificarse por decreto — "
                        "reverificar en migracion.gob.pa antes del expediente. Abogado panameño "
                        "obligatorio."
                    ),
                },
            ),
            (
                {
                    "en": "Entry to Panama & provisional residence filing (SNM)",
                    "es": "Entrada a Panamá y presentación de la residencia provisional (SNM)",
                },
                {
                    "en": "Provisional resident card (6 months during processing).",
                    "es": "Tarjeta de residente provisional (6 meses durante la tramitación).",
                },
            ),
            (
                {
                    "en": "Grant of provisional residence (2 years)",
                    "es": "Otorgamiento de la residencia provisional (2 años)",
                },
                {
                    "en": (
                        "🟠 Since 2021, Friendly Nations no longer grants immediate permanent "
                        "residence: a 2-year PROVISIONAL residence first, with no right to work "
                        "(MITRADEL permit = separate process)."
                    ),
                    "es": (
                        "🟠 Desde 2021, Friendly Nations ya no otorga la permanente inmediata: "
                        "primero una residencia PROVISIONAL de 2 años, sin derecho a trabajar "
                        "(permiso MITRADEL = trámite aparte)."
                    ),
                },
            ),
            (
                {
                    "en": "Permanent residence application (after 2 years)",
                    "es": "Solicitud de residencia permanente (después de 2 años)",
                },
                {"en": "Processing up to 6 months.", "es": "Tramitación de hasta 6 meses."},
            ),
            (
                {"en": "Cédula E (Tribunal Electoral)", "es": "Cédula E (Tribunal Electoral)"},
                {
                    "en": "Permanent-resident identity card. Renewal every 10 years.",
                    "es": (
                        "Documento de identidad de residente permanente. Renovación cada 10 años."
                    ),
                },
            ),
        ],
    },
    PA_PEN_NAME: {
        "name": {
            "en": "Panama — Pensionado Visa (retiree)",
            "es": "Panamá — Visa Pensionado (jubilado)",
        },
        "steps": [
            (
                {
                    "en": "File preparation (via lawyer)",
                    "es": "Preparación del expediente (vía abogado)",
                },
                {
                    "en": (
                        "Pension ≥ 1 000 USD/month (or ≥ 750 USD/month WITH Panamanian real "
                        "estate ≥ 100 000 USD). +250 USD/month per dependent. Work prohibited "
                        "under this status. Indicative thresholds."
                    ),
                    "es": (
                        "Pensión ≥ 1 000 USD/mes (o ≥ 750 USD/mes CON inmueble panameño ≥ 100 000 "
                        "USD). +250 USD/mes por persona a cargo. Trabajo prohibido bajo este "
                        "estatus. Umbrales indicativos."
                    ),
                },
            ),
            (
                {
                    "en": "Application submission to the SNM",
                    "es": "Presentación de la solicitud ante el SNM",
                },
                {
                    "en": "Pensionados are often exempt from the repatriation deposit.",
                    "es": "Los pensionados suelen estar exentos del depósito de repatriación.",
                },
            ),
            (
                {
                    "en": "Grant of permanent residence",
                    "es": "Otorgamiento de la residencia permanente",
                },
                {
                    "en": (
                        "DIRECT permanent residence (no provisional period). Benefits: pensionado "
                        "discount card (transport, health, leisure)."
                    ),
                    "es": (
                        "Residencia permanente DIRECTA (sin período provisional). Ventajas: "
                        "tarjeta de descuentos pensionado (transporte, salud, ocio)."
                    ),
                },
            ),
            (
                {"en": "Cédula E (Tribunal Electoral)", "es": "Cédula E (Tribunal Electoral)"},
                {
                    "en": "Permanent-resident identity card.",
                    "es": "Documento de identidad de residente permanente.",
                },
            ),
        ],
    },
    PA_GV_NAME: {
        "name": {
            "en": "Panama — Qualified Investor (Golden Visa)",
            "es": "Panamá — Inversionista Calificado (Golden Visa)",
        },
        "steps": [
            (
                {
                    "en": "Preparation & choice of investment vehicle",
                    "es": "Preparación y elección del vehículo de inversión",
                },
                {
                    "en": (
                        "🔴 CRITICAL VOLATILE THRESHOLD. Real estate ≥ 300 000 USD in a window "
                        "announced until October 2026, then a likely rise to 500 000 USD. "
                        "Alternatives: securities on the Panamanian stock exchange ≥ 500 000 USD, "
                        "or term deposit ≥ 750 000 USD (5 years). CHECK the threshold in force on "
                        "migracion.gob.pa."
                    ),
                    "es": (
                        "🔴 UMBRAL VOLÁTIL CRÍTICO. Inmueble ≥ 300 000 USD en una ventana "
                        "anunciada hasta octubre de 2026, luego probable subida a 500 000 USD. "
                        "Alternativas: títulos en la bolsa panameña ≥ 500 000 USD, o depósito a "
                        "plazo ≥ 750 000 USD (5 años). VERIFICAR el umbral vigente en "
                        "migracion.gob.pa."
                    ),
                },
            ),
            (
                {
                    "en": "Making the investment (transfer from abroad)",
                    "es": "Realización de la inversión (transferencia desde el extranjero)",
                },
                {
                    "en": "Funds of foreign origin, via banking channels.",
                    "es": "Fondos de origen extranjero, vía canales bancarios.",
                },
            ),
            (
                {
                    "en": "Application submission to the SNM",
                    "es": "Presentación de la solicitud ante el SNM",
                },
                {
                    "en": "Grant of permanent residence in 30 to 45 business days.",
                    "es": "Otorgamiento de la residencia permanente en 30 a 45 días hábiles.",
                },
            ),
            (
                {"en": "Cédula E (Tribunal Electoral)", "es": "Cédula E (Tribunal Electoral)"},
                {
                    "en": (
                        "⚠️ The Golden Visa requires no presence to keep the residence, but "
                        "naturalization requires effective residence — to weigh with the lawyer."
                    ),
                    "es": (
                        "⚠️ El Golden Visa no exige presencia para conservar la residencia, pero "
                        "la naturalización exige residencia efectiva — a valorar con el abogado."
                    ),
                },
            ),
        ],
    },
    PA_DN_NAME: {
        "name": {
            "en": "Panama — Digital nomad visa (Trabajador Remoto)",
            "es": "Panamá — Visa de nómada digital (Trabajador Remoto)",
        },
        "steps": [
            (
                {
                    "en": "Check eligibility & gather the file",
                    "es": "Verificar la elegibilidad y reunir el expediente",
                },
                {
                    "en": "Foreign-source income ≥ 36 000 USD/year. Indicative threshold.",
                    "es": "Ingresos de fuente extranjera ≥ 36 000 USD/año. Umbral indicativo.",
                },
            ),
            (
                {
                    "en": (
                        "Entry to Panama & filing at the Ventanilla de Trámites Especiales (SNM)"
                    ),
                    "es": (
                        "Entrada a Panamá y presentación en la Ventanilla de Trámites Especiales "
                        "(SNM)"
                    ),
                },
                {
                    "en": "Filing at the SNM's Ventanilla de Trámites Especiales.",
                    "es": "Presentación en la Ventanilla de Trámites Especiales del SNM.",
                },
            ),
            (
                {
                    "en": "Issuance of the digital nomad card",
                    "es": "Emisión del carné de nómada digital",
                },
                {
                    "en": (
                        "⚠️ 9 months, renewable once (18 months max). NON-resident category: "
                        "leads NEITHER to residence NOR to naturalization. For a durable "
                        "settlement, switch to Friendly Nations / Pensionado / Golden Visa."
                    ),
                    "es": (
                        "⚠️ 9 meses, renovable una vez (18 meses máx.). Categoría NO residente: "
                        "no conduce NI a la residencia NI a la naturalización. Para una "
                        "instalación duradera, cambiar a Friendly Nations / Pensionado / Golden "
                        "Visa."
                    ),
                },
            ),
        ],
    },
    PA_CO_NAME: {
        "name": {
            "en": "Panama — Company formation (S.A. / SRL)",
            "es": "Panamá — Creación de empresa (S.A. / SRL)",
        },
        "steps": [
            (
                {
                    "en": "Qualify the activity & choose the structure",
                    "es": "Calificar la actividad y elegir la estructura",
                },
                {
                    "en": (
                        "🔴 RETAIL TRAP (art. 293): retail trade to the Panamanian consumer (shop, "
                        "local B2C e-commerce, distribution, franchise) is CLOSED to a foreign "
                        "shareholder (not even as a director). Open: B2B, consulting, wholesale, "
                        "import-export, SaaS/tech, holding, foreign clients. Qualify BEFORE "
                        "incorporating. S.A. = 1 partner, confidential owners; SRL = min 2 "
                        "partners, public partners."
                    ),
                    "es": (
                        "🔴 TRAMPA DEL COMERCIO MINORISTA (art. 293): el comercio al por menor al "
                        "consumidor panameño (tienda, e-commerce B2C local, distribución, "
                        "franquicia) está CERRADO a un accionista extranjero (ni siquiera como "
                        "director). Abiertos: B2B, consultoría, mayoreo, import-export, "
                        "SaaS/tech, holding, clientes extranjeros. Calificar ANTES de crear. S.A. "
                        "= 1 socio, propietarios confidenciales; SRL = mín. 2 socios, socios "
                        "públicos."
                    ),
                },
            ),
            (
                {
                    "en": "Drafting the articles of incorporation (lawyer)",
                    "es": "Redacción del pacto social (abogado)",
                },
                {
                    "en": ("S.A. = board of at least 3 directors (may be foreign / non-resident)."),
                    "es": (
                        "S.A. = junta de al menos 3 directores (pueden ser extranjeros / no "
                        "residentes)."
                    ),
                },
            ),
            (
                {
                    "en": "Registration with the Panama Public Registry",
                    "es": "Inscripción en el Registro Público de Panamá",
                },
                {
                    "en": "Company incorporated in 3 to 7 days.",
                    "es": "Empresa constituida en 3 a 7 días.",
                },
            ),
            (
                {
                    "en": "Aviso de Operación & tax registration (RUC / DGI)",
                    "es": "Aviso de Operación e inscripción fiscal (RUC / DGI)",
                },
                {
                    "en": (
                        "Invested capital < 10 000 USD → exempt from IAO; above that, IAO = 2 % "
                        "of net capital (min 100 / max 60 000 USD/year), only if activity in "
                        "Panama. CSS registration if hiring. Indicative amounts."
                    ),
                    "es": (
                        "Capital invertido < 10 000 USD → exento de IAO; por encima, IAO = 2 % "
                        "del capital neto (mín. 100 / máx. 60 000 USD/año), solo si hay actividad "
                        "en Panamá. Inscripción CSS si se contrata. Importes indicativos."
                    ),
                },
            ),
            (
                {
                    "en": "MITRADEL work permit (if the director works in the company)",
                    "es": "Permiso de trabajo MITRADEL (si el directivo trabaja en la empresa)",
                },
                {
                    "en": (
                        "🟠 SEPARATE process from residence. Quotas: max 10 % ordinary foreign "
                        "staff / 15 % specialized. ~56 professions reserved for nationals "
                        "(medicine, law, engineering, accounting, architecture…) remain "
                        "off-limits until naturalized. Owning/supervising from abroad = no "
                        "permit; working on site = this permit."
                    ),
                    "es": (
                        "🟠 Trámite SEPARADO de la residencia. Cuotas: máx. 10 % de personal "
                        "extranjero ordinario / 15 % especializado. ~56 profesiones reservadas a "
                        "los nacionales (medicina, derecho, ingeniería, contabilidad, "
                        "arquitectura…) siguen prohibidas hasta naturalizarse. Poseer/supervisar "
                        "desde el extranjero = ningún permiso; trabajar en el lugar = este "
                        "permiso."
                    ),
                },
            ),
        ],
    },
    BG_EU_NAME: {
        "name": {
            "en": "Bulgaria — EU residence registration",
            "es": "Bulgaria — Registro de residencia UE",
        },
        "steps": [
            (
                {
                    "en": "Register the address with the municipality",
                    "es": "Registrar el domicilio en el municipio",
                },
                {
                    "en": "Registration of the residence address with the municipality.",
                    "es": "Registro del domicilio de residencia en el municipio.",
                },
            ),
            (
                {
                    "en": "Residence certificate application (Direction Migration)",
                    "es": "Solicitud de certificado de residencia (Direction Migration)",
                },
                {
                    "en": "Certificate valid up to 5 years, often issued in ≈ 3 business days.",
                    "es": "Certificado válido hasta 5 años, a menudo emitido en ≈ 3 días hábiles.",
                },
            ),
            (
                {
                    "en": "Obtaining the personal number (LNCh)",
                    "es": "Obtención del número personal (LNCh)",
                },
                {
                    "en": (
                        "🟠 EU citizens receive an LNCh (not an EGN), which can create "
                        "administrative obstacles (bank, public services). Required for bank, "
                        "tax, lease, healthcare."
                    ),
                    "es": (
                        "🟠 Los ciudadanos UE reciben un LNCh (y no un EGN), lo que puede crear "
                        "obstáculos administrativos (banco, servicios públicos). Requerido para "
                        "banco, fisco, alquiler, salud."
                    ),
                },
            ),
        ],
    },
    BG_RET_NAME: {
        "name": {
            "en": "Bulgaria — Non-EU retiree residence",
            "es": "Bulgaria — Residencia de jubilado no-UE",
        },
        "steps": [
            (
                {
                    "en": "Visa D application at the Bulgarian consulate",
                    "es": "Solicitud de visa D en el consulado búlgaro",
                },
                {
                    "en": (
                        "🔴 Means of subsistence ≥ minimum pension/wage (≈ 620 €/month in 2026, "
                        "indexed to the minimum wage, post-euro) — indicative amount, recheck the "
                        "official source. Private pensions (e.g. 401k) may be refused without an "
                        "official State pension document. Visa fee ≈ 100 €."
                    ),
                    "es": (
                        "🔴 Medios de subsistencia ≥ pensión/salario mínimo (≈ 620 €/mes en 2026, "
                        "indexado al salario mínimo, post-euro) — importe indicativo, reverificar "
                        "la fuente oficial. Las pensiones privadas (ej. 401k) pueden ser "
                        "rechazadas sin documento oficial de pensión estatal. Tasa de visa ≈ 100 "
                        "€."
                    ),
                },
            ),
            (
                {
                    "en": "Entry to Bulgaria & address registration (within 5 days)",
                    "es": "Entrada a Bulgaria y registro del domicilio (en 5 días)",
                },
                {
                    "en": "Address registration within 5 days of entry.",
                    "es": "Registro del domicilio dentro de los 5 días tras la entrada.",
                },
            ),
            (
                {
                    "en": "Long-stay residence permit submission (Direction Migration)",
                    "es": (
                        "Presentación del permiso de residencia prolongada (Direction Migration)"
                    ),
                },
                {
                    "en": (
                        "Permit valid up to 1 year, renewable. Does not grant access to the labor "
                        "market."
                    ),
                    "es": (
                        "Permiso válido hasta 1 año, renovable. No da acceso al mercado laboral."
                    ),
                },
            ),
        ],
    },
    BG_DN_NAME: {
        "name": {
            "en": "Bulgaria — Digital nomad visa (non-EU)",
            "es": "Bulgaria — Visa de nómada digital (no-UE)",
        },
        "steps": [
            (
                {
                    "en": "Check the (recent) regime & gather the file",
                    "es": "Verificar el régimen (reciente) y reunir el expediente",
                },
                {
                    "en": (
                        "🔴 VERY RECENT REGIME — legal basis art. 24p ЗЧРБ, applications opened on "
                        "20/12/2025. Implementation details still evolving: recheck with the "
                        "consulate / Direction Migration before any promise."
                    ),
                    "es": (
                        "🔴 RÉGIMEN MUY RECIENTE — base legal art. 24p ЗЧРБ, solicitudes abiertas "
                        "el 20/12/2025. Detalles de aplicación aún en evolución: reverificar ante "
                        "el consulado / la Direction Migration antes de cualquier promesa."
                    ),
                },
            ),
            (
                {
                    "en": "Visa D application at the consulate",
                    "es": "Solicitud de visa D en el consulado",
                },
                {
                    "en": (
                        "🔴 Threshold ≈ 31 000 €/year indexed to the minimum wage, post-euro — "
                        "indicative, recheck. Prohibition on working for Bulgarian "
                        "clients/employers."
                    ),
                    "es": (
                        "🔴 Umbral ≈ 31 000 €/año indexado al salario mínimo, post-euro — "
                        "indicativo, reverificar. Prohibición de trabajar para "
                        "clientes/empleadores búlgaros."
                    ),
                },
            ),
            (
                {
                    "en": "Entry & residence permit (Direction Migration, within 14 days)",
                    "es": "Entrada y permiso de residencia (Direction Migration, en 14 días)",
                },
                {
                    "en": (
                        "1-year permit, renewable for 1 year (max ≈ 2 years). Does NOT lead to "
                        "permanent residence."
                    ),
                    "es": (
                        "Permiso de 1 año, renovable 1 año (máx. ≈ 2 años). NO conduce a la "
                        "residencia permanente."
                    ),
                },
            ),
        ],
    },
    BG_FL_NAME: {
        "name": {
            "en": "Bulgaria — Freelance / liberal profession (non-EU)",
            "es": "Bulgaria — Freelance / profesión liberal (no-UE)",
        },
        "steps": [
            (
                {
                    "en": "Obtain the freelance activity permit (Employment Agency)",
                    "es": "Obtener el permiso de actividad freelance (Agencia de Empleo)",
                },
                {
                    "en": (
                        "🟠 The permit is issued by the EMPLOYMENT AGENCY (under the MTSP), NOT by "
                        "Direction Migration — a frequent naming error. Bulgarian B1 required."
                    ),
                    "es": (
                        "🟠 El permiso lo emite la AGENCIA DE EMPLEO (dependiente del MTSP), NO la "
                        "Direction Migration — error de denominación frecuente. Búlgaro B1 "
                        "requerido."
                    ),
                },
            ),
            (
                {
                    "en": "Visa D application at the consulate",
                    "es": "Solicitud de visa D en el consulado",
                },
                {
                    "en": "Visa D application on the basis of the freelance permit.",
                    "es": "Solicitud de visa D sobre la base del permiso freelance.",
                },
            ),
            (
                {
                    "en": "Residence permit (Direction Migration)",
                    "es": "Permiso de residencia (Direction Migration)",
                },
                {
                    "en": (
                        "12-month renewable permit. No fixed statutory income threshold published "
                        "(assessed on the business plan). Indicative."
                    ),
                    "es": (
                        "Permiso de 12 meses renovable. Sin umbral de ingresos estatutario fijo "
                        "publicado (evaluado sobre el plan de actividad). Indicativo."
                    ),
                },
            ),
        ],
    },
    BG_CO_NAME: {
        "name": {
            "en": "Bulgaria — Company formation (EOOD / OOD)",
            "es": "Bulgaria — Creación de empresa (EOOD / OOD)",
        },
        "steps": [
            (
                {
                    "en": "Check / reserve the name & choose the structure",
                    "es": "Verificar / reservar el nombre y elegir la estructura",
                },
                {
                    "en": (
                        "EOOD = 1 partner · OOD = ≥ 2 partners (notarized incorporation deed + "
                        "UBO declaration). Minimum capital ≈ 1 € (2 BGN). The number of partners "
                        "is the only stable parameter of this journey."
                    ),
                    "es": (
                        "EOOD = 1 socio · OOD = ≥ 2 socios (escritura de constitución notariada + "
                        "declaración UBO). Capital mínimo ≈ 1 € (2 BGN). El número de socios es "
                        "el único parámetro estable de este recorrido."
                    ),
                },
            ),
            (
                {
                    "en": "Draft the statutes & deposit the capital",
                    "es": "Redactar los estatutos y depositar el capital",
                },
                {
                    "en": (
                        "Bulgarian registered office required; if the manager is non-resident, a "
                        "local contact person is needed."
                    ),
                    "es": (
                        "Domicilio social búlgaro requerido; si el gerente es no residente, se "
                        "necesita una persona de contacto local."
                    ),
                },
            ),
            (
                {
                    "en": "Registration with the Commercial Register",
                    "es": "Inscripción en el Registro Mercantil",
                },
                {
                    "en": (
                        "Obtaining the EIK / BULSTAT (unique code). 3 to 10 business days (2 to 4 "
                        "weeks remotely)."
                    ),
                    "es": (
                        "Obtención del EIK / BULSTAT (código único). 3 a 10 días hábiles (2 a 4 "
                        "semanas en remoto)."
                    ),
                },
            ),
            (
                {
                    "en": "VAT, bank account & start-up",
                    "es": "IVA, cuenta bancaria y puesta en marcha",
                },
                {
                    "en": (
                        "🔴 IS 10 % (the lowest in the EU), dividends 5 % — indicative rates, "
                        "recheck (post-euro 2026). VAT if turnover > ≈ 51 000 €. Known "
                        "bottleneck: bank account opening (KYC, presence sometimes required). ⚠️ "
                        "Holding remotely ≠ settling: the 10 % IS only holds if the company is "
                        "genuinely run FROM Bulgaria (substance)."
                    ),
                    "es": (
                        "🔴 IS 10 % (el más bajo de la UE), dividendos 5 % — tasas indicativas, "
                        "reverificar (post-euro 2026). IVA si la facturación > ≈ 51 000 €. Cuello "
                        "de botella conocido: apertura de cuenta bancaria (KYC, a veces presencia "
                        "requerida). ⚠️ Poseer a distancia ≠ instalarse: el 10 % de IS solo se "
                        "mantiene si la empresa se dirige realmente DESDE Bulgaria (sustancia)."
                    ),
                },
            ),
        ],
    },
    HU_EU_NAME: {
        "name": {
            "en": "Hungary — EU residence registration",
            "es": "Hungría — Registro de residencia UE",
        },
        "steps": [
            (
                {
                    "en": "Residence declaration (> 90 days) at the Immigration Office",
                    "es": "Declaración de estancia (> 90 días) en la Oficina de Inmigración",
                },
                {
                    "en": (
                        "🟠 The “sufficient resources” amount in HUF is to be rechecked (primary "
                        "source not confirmed). Work and establishment allowed without a permit."
                    ),
                    "es": (
                        "🟠 El importe de «recursos suficientes» en HUF está por reverificar "
                        "(fuente primaria no confirmada). Trabajo y establecimiento permitidos "
                        "sin título."
                    ),
                },
            ),
            (
                {
                    "en": "Registration card",
                    "es": "Tarjeta de registro (registration card)",
                },
                {
                    "en": "Permanent residence available at 5 years, naturalization at 8 years.",
                    "es": (
                        "Residencia permanente accesible a los 5 años, naturalización a los 8 años."
                    ),
                },
            ),
            (
                {
                    "en": "Settlement identifiers (address card, tax number, TAJ health)",
                    "es": (
                        "Identificadores de instalación (tarjeta de domicilio, n.º fiscal, TAJ "
                        "salud)"
                    ),
                },
                {
                    "en": (
                        "lakcímkártya (address card) · adóazonosító jel (NAV tax number) · TAJ "
                        "(NEAK social security). Frequent practical blockers — to anticipate on "
                        "arrival."
                    ),
                    "es": (
                        "lakcímkártya (tarjeta de domicilio) · adóazonosító jel (n.º fiscal NAV) "
                        "· TAJ (seguridad social NEAK). Bloqueos prácticos frecuentes — a prever "
                        "desde la llegada."
                    ),
                },
            ),
        ],
    },
    HU_WC_NAME: {
        "name": {
            "en": "Hungary — White Card (digital nomad, non-EU)",
            "es": "Hungría — White Card (nómada digital, no-UE)",
        },
        "steps": [
            (
                {
                    "en": "Prior warning & threshold check",
                    "es": "Advertencia previa y verificación del umbral",
                },
                {
                    "en": (
                        "🔴 FICHE NOT VERIFIED IN PRIMARY SOURCE. ⚠️ DEAD END: the White Card "
                        "counts NEITHER toward permanent residence NOR naturalization — a 1-2 "
                        "year trial solution. To settle durably, switch to another route. "
                        "Forbidden to work for the Hungarian market. Minimum monthly income 🔴 "
                        "volatile, recheck on oif.gov.hu."
                    ),
                    "es": (
                        "🔴 FICHA NO VERIFICADA EN FUENTE PRIMARIA. ⚠️ CALLEJÓN SIN SALIDA: la "
                        "White Card no cuenta NI para la residencia permanente NI para la "
                        "naturalización — solución de prueba de 1-2 años. Para instalarse "
                        "durablemente, cambiar a otra vía. Prohibido trabajar para el mercado "
                        "húngaro. Ingreso mensual mínimo 🔴 volátil, reverificar en oif.gov.hu."
                    ),
                },
            ),
            (
                {
                    "en": "Visa D / White Card application",
                    "es": "Solicitud de visa D / White Card",
                },
                {
                    "en": "Visa D / White Card application on the basis of the assembled file.",
                    "es": "Solicitud de visa D / White Card sobre la base del expediente reunido.",
                },
            ),
            (
                {
                    "en": "Residence permit & identifiers",
                    "es": "Título de residencia e identificadores",
                },
                {
                    "en": (
                        "Address card + tax number + TAJ. Renewal and family-reunification "
                        "conditions 🔴 to be verified."
                    ),
                    "es": (
                        "Tarjeta de domicilio + n.º fiscal + TAJ. Condiciones de renovación y de "
                        "reagrupación familiar 🔴 por verificar."
                    ),
                },
            ),
        ],
    },
    HU_GI_NAME: {
        "name": {
            "en": "Hungary — Guest Investor (golden visa, non-EU)",
            "es": "Hungría — Guest Investor (golden visa, no-UE)",
        },
        "steps": [
            (
                {
                    "en": "Warning & choice of investment option",
                    "es": "Advertencia y elección de la opción de inversión",
                },
                {
                    "en": (
                        "🔴 FICHE NOT VERIFIED IN PRIMARY SOURCE. Options (indicative amounts, "
                        "recheck): MNB-approved funds ≈ 250 000 € (cheapest route); direct "
                        "residential real estate ≈ 500 000 € (option possibly POSTPONED — check "
                        "if actually open); higher-education donation ≈ 1 000 000 €. Check the "
                        "list of MNB funds actually subscribable."
                    ),
                    "es": (
                        "🔴 FICHA NO VERIFICADA EN FUENTE PRIMARIA. Opciones (importes "
                        "indicativos, reverificar): fondos aprobados MNB ≈ 250 000 € (vía más "
                        "barata); inmueble residencial directo ≈ 500 000 € (opción posiblemente "
                        "APLAZADA — verificar si realmente abierta); donación a la enseñanza "
                        "superior ≈ 1 000 000 €. Verificar la lista de fondos MNB realmente "
                        "suscribibles."
                    ),
                },
            ),
            (
                {
                    "en": "Making the investment",
                    "es": "Realización de la inversión",
                },
                {
                    "en": "Deployment of capital according to the chosen option.",
                    "es": "Despliegue del capital según la opción elegida.",
                },
            ),
            (
                {
                    "en": "Guest Investor permit application",
                    "es": "Solicitud del título Guest Investor",
                },
                {
                    "en": "10-year permit, low presence required. Wealth-based route.",
                    "es": "Título de 10 años, baja presencia exigida. Vía patrimonial.",
                },
            ),
            (
                {
                    "en": "Residence permit & identifiers",
                    "es": "Título de residencia e identificadores",
                },
                {
                    "en": "Address card + tax number + TAJ.",
                    "es": "Tarjeta de domicilio + n.º fiscal + TAJ.",
                },
            ),
        ],
    },
    HU_SP_NAME: {
        "name": {
            "en": "Hungary — Single permit (non-EU employee)",
            "es": "Hungría — Autorización única (asalariado no-UE)",
        },
        "steps": [
            (
                {
                    "en": "The employer initiates the application (single permit)",
                    "es": "El empleador inicia la solicitud (single permit)",
                },
                {
                    "en": (
                        "Residence permit + work authorization in ONE procedure, carried by the "
                        "employer. 🟠 Possible labor-market test + salary thresholds to verify. "
                        "This route COUNTS toward permanent residence and naturalization."
                    ),
                    "es": (
                        "Título + autorización de trabajo en UN solo procedimiento, llevado por "
                        "el empleador. 🟠 Posible prueba del mercado laboral + umbrales salariales "
                        "por verificar. Esta vía CUENTA para la residencia permanente y la "
                        "naturalización."
                    ),
                },
            ),
            (
                {
                    "en": "Visa D application at the consulate",
                    "es": "Solicitud de visa D en el consulado",
                },
                {
                    "en": (
                        "Visa D application at the consulate on the basis of the authorization "
                        "obtained."
                    ),
                    "es": (
                        "Solicitud de visa D en el consulado sobre la base de la autorización "
                        "obtenida."
                    ),
                },
            ),
        ],
    },
    HU_CO_NAME: {
        "name": {
            "en": "Hungary — Company formation (Kft.)",
            "es": "Hungría — Creación de empresa (Kft.)",
        },
        "steps": [
            (
                {
                    "en": "Prepare the incorporation (lawyer) & the capital",
                    "es": "Preparar la constitución (abogado) y el capital",
                },
                {
                    "en": (
                        "⚠️ COMPANY ≠ RESIDENCE. Forming a Kft. grants no residence permit: a "
                        "third-country foreigner can run a Kft. REMOTELY without a permit; to "
                        "reside physically, that is a distinct (and post-reform uncertain) "
                        "permit. Share capital ~3 M HUF (~7 600 €, contribution deferrable if the "
                        "deed allows). Indicative amounts, reconvert at the current rate."
                    ),
                    "es": (
                        "⚠️ EMPRESA ≠ RESIDENCIA. Constituir una Kft. no da ningún título de "
                        "residencia: un extranjero de país tercero puede dirigir una Kft. A "
                        "DISTANCIA sin título; para residir físicamente, es un título distinto (e "
                        "incierto tras la reforma). Capital social ~3 M HUF (~7 600 €, aporte "
                        "diferible si el acta lo prevé). Importes indicativos, reconvertir al "
                        "tipo del día."
                    ),
                },
            ),
            (
                {
                    "en": (
                        "Incorporation deed & registration with the commercial register "
                        "(Cégbíróság)"
                    ),
                    "es": "Acta constitutiva e inscripción en el registro mercantil (Cégbíróság)",
                },
                {
                    "en": "Registration with the Cégbíróság (commercial register).",
                    "es": "Inscripción en el Cégbíróság (registro mercantil).",
                },
            ),
            (
                {
                    "en": "Tax number, VAT & registers",
                    "es": "Número fiscal, IVA y registros",
                },
                {
                    "en": (
                        "🟠 IS 9 % (the lowest in the EU), 0 % withholding on outbound dividends — "
                        "indicative rates to recheck (NAV). VAT (ÁFA) exemption under ~18 M "
                        "HUF/year (~45 000 €), otherwise 27 %. Local tax (HIPA). KIVA possible "
                        "with a high payroll."
                    ),
                    "es": (
                        "🟠 IS 9 % (el más bajo de la UE), 0 % de retención sobre dividendos "
                        "salientes — tasas indicativas a reverificar (NAV). Franquicia de IVA "
                        "(ÁFA) bajo ~18 M HUF/año (~45 000 €), si no 27 %. Impuesto local (HIPA). "
                        "KIVA posible con una masa salarial alta."
                    ),
                },
            ),
            (
                {
                    "en": "Business bank account",
                    "es": "Cuenta bancaria profesional",
                },
                {
                    "en": (
                        "🟠 BOTTLENECK: account opening for a foreign manager/UBO — physical "
                        "presence often required, the slowest step."
                    ),
                    "es": (
                        "🟠 CUELLO DE BOTELLA: apertura de cuenta para gerente/UBO extranjero — "
                        "presencia física a menudo exigida, el paso más lento."
                    ),
                },
            ),
        ],
    },
    AE_GV_NAME: {
        "name": {
            "en": "Dubai (UAE) — Golden Visa (10 years)",
            "es": "Dubái (EAU) — Golden Visa (10 años)",
        },
        "steps": [
            (
                {
                    "en": "Check the eligibility gateway",
                    "es": "Verificar la puerta de elegibilidad",
                },
                {
                    "en": (
                        "🟠 Gateways (volatile AED amounts, recheck u.ae / icp.gov.ae): investor ≥ "
                        "2 M AED (approved fund or property) · talents salary ≥ 30 000 AED/month "
                        "+ degree + MOHRE level 1/2 classification · entrepreneur project ≥ 500 "
                        "000 AED or incubator validation · real estate ≥ 2 M AED."
                    ),
                    "es": (
                        "🟠 Puertas (importes AED volátiles, reverificar u.ae / icp.gov.ae): "
                        "inversionista ≥ 2 M AED (fondo aprobado o bien) · talentos salario ≥ 30 "
                        "000 AED/mes + título + clasificación MOHRE nivel 1/2 · emprendedor "
                        "proyecto ≥ 500 000 AED o validación de incubadora · inmueble ≥ 2 M AED."
                    ),
                },
            ),
            (
                {"en": "Build the nomination file", "es": "Preparar el expediente de nominación"},
                {
                    "en": (
                        "Nomination file to assemble according to the chosen eligibility gateway."
                    ),
                    "es": (
                        "Expediente de nominación a preparar según la puerta de elegibilidad "
                        "elegida."
                    ),
                },
            ),
            (
                {"en": "Medical examination & Emirates ID", "es": "Examen médico y Emirates ID"},
                {
                    "en": (
                        "Medical examination + Emirates ID mandatory (common to every UAE route). "
                        "Presence required (biometrics)."
                    ),
                    "es": (
                        "Examen médico + Emirates ID obligatorios (transversales a todo recorrido "
                        "en EAU). Presencia requerida (biometría)."
                    ),
                },
            ),
            (
                {
                    "en": "Issuance of the 10-year Golden Visa",
                    "es": "Emisión del Golden Visa de 10 años",
                },
                {
                    "en": (
                        "10 years renewable, autonomous (no sponsor), exempt from the 6-month "
                        "absence rule — suited to highly mobile profiles. Can sponsor the family."
                    ),
                    "es": (
                        "10 años renovable, autónomo (sin sponsor), exento de la regla de "
                        "ausencia de 6 meses — adecuado para perfiles muy móviles. Puede "
                        "patrocinar a la familia."
                    ),
                },
            ),
        ],
    },
    AE_FZ_NAME: {
        "name": {
            "en": "Dubai (UAE) — Residence via a free zone company",
            "es": "Dubái (EAU) — Residencia por empresa free zone",
        },
        "steps": [
            (
                {
                    "en": "Choose the free zone & activity, reserve the name",
                    "es": "Elegir la free zone y la actividad, reservar el nombre",
                },
                {
                    "en": (
                        "Activity outside the UAE domestic market / international B2B / holding / "
                        "digital. To sell on the local market → mainland (separate journey). "
                        "Carried out via an approved provider, to assign on the dossier."
                    ),
                    "es": (
                        "Actividad fuera del mercado interno de EAU / B2B internacional / holding "
                        "/ digital. Para vender en el mercado local → mainland (otro recorrido). "
                        "Realizado vía un proveedor autorizado, a asignar en el expediente."
                    ),
                },
            ),
            (
                {"en": "Licence & establishment card", "es": "Licencia y establishment card"},
                {
                    "en": (
                        "🟠 Visa quota depends on the package/office (~1 visa/9 m², varies by "
                        "authority). Costs = commercial sources, cross-check 2-3 providers."
                    ),
                    "es": (
                        "🟠 Cupo de visas según la fórmula/oficina (~1 visa/9 m², variable según "
                        "la autoridad). Costes = fuentes comerciales, contrastar 2-3 proveedores."
                    ),
                },
            ),
            (
                {
                    "en": "Entry permit → medical examination → Emirates ID",
                    "es": "Entry permit → examen médico → Emirates ID",
                },
                {
                    "en": "Medical + Emirates ID mandatory. Presence required.",
                    "es": "Médico + Emirates ID obligatorios. Presencia requerida.",
                },
            ),
            (
                {
                    "en": "Residence visa stamped (2-3 years, renewable)",
                    "es": "Visa de residencia estampada (2-3 años, renovable)",
                },
                {
                    "en": (
                        "🟠 The sponsor is the COMPANY: as long as it is active, the visa holds. "
                        "Taxation: see the company journey (0 % QFZP NOT automatic). EU client: "
                        "document the tax exit from the country of origin (France side)."
                    ),
                    "es": (
                        "🟠 El sponsor es la EMPRESA: mientras esté activa, el visa se mantiene. "
                        "Fiscalidad: ver el recorrido de empresa (0 % QFZP NO automático). "
                        "Cliente UE: documentar la salida fiscal del país de origen (lado "
                        "Francia)."
                    ),
                },
            ),
        ],
    },
    AE_RE_NAME: {
        "name": {
            "en": "Dubai (UAE) — Real estate visa (2 years)",
            "es": "Dubái (EAU) — Visa inmobiliaria (2 años)",
        },
        "steps": [
            (
                {
                    "en": "Acquisition & qualification of the property",
                    "es": "Adquisición y calificación del inmueble",
                },
                {
                    "en": (
                        "🟠 Property ≥ 750 000 AED (indicative amount, DLD). DO NOT confuse with "
                        "the real-estate Golden Visa (≥ 2 M AED / 10 years)."
                    ),
                    "es": (
                        "🟠 Inmueble ≥ 750 000 AED (importe indicativo, DLD). NO confundir con el "
                        "Golden Visa inmobiliario (≥ 2 M AED / 10 años)."
                    ),
                },
            ),
            (
                {"en": "Real estate visa application", "es": "Solicitud de visa inmobiliaria"},
                {
                    "en": "Application filed via an approved provider, to assign on the dossier.",
                    "es": (
                        "Solicitud presentada vía un proveedor autorizado, a asignar en el "
                        "expediente."
                    ),
                },
            ),
            (
                {"en": "Medical examination & Emirates ID", "es": "Examen médico y Emirates ID"},
                {
                    "en": "Medical + Emirates ID mandatory. Presence required.",
                    "es": "Médico + Emirates ID obligatorios. Presencia requerida.",
                },
            ),
            (
                {
                    "en": "Residence visa stamped (2 years renewable)",
                    "es": "Visa de residencia estampada (2 años renovable)",
                },
                {
                    "en": (
                        "🟠 The sponsor is the PROPERTY: residence holds as long as the property "
                        "is held. Above 2 M AED, prefer the Golden Visa (10 years + absence-rule "
                        "exemption)."
                    ),
                    "es": (
                        "🟠 El sponsor es el INMUEBLE: la residencia se mantiene mientras se posea "
                        "el bien. Por encima de 2 M AED, preferir el Golden Visa (10 años + "
                        "exención de la regla de ausencia)."
                    ),
                },
            ),
        ],
    },
    AE_RW_NAME: {
        "name": {
            "en": "Dubai (UAE) — Remote work visa (1 year)",
            "es": "Dubái (EAU) — Visa de remote work (1 año)",
        },
        "steps": [
            (
                {
                    "en": "Check eligibility & gather the file",
                    "es": "Verificar la elegibilidad y reunir el expediente",
                },
                {
                    "en": (
                        "🟠 Indicative threshold. ⚠️ Remote work does NOT lead to long-term "
                        "residence (1 year) — for a durable base + tax optimization, prefer a "
                        "free zone company from the start. Say so before the client gets locked "
                        "in."
                    ),
                    "es": (
                        "🟠 Umbral indicativo. ⚠️ El remote work NO conduce a una residencia larga "
                        "(1 año) — para una base duradera + optimización fiscal, preferir una "
                        "empresa free zone desde el inicio. Decirlo antes de que el cliente se "
                        "encierre en ello."
                    ),
                },
            ),
            (
                {"en": "Remote work visa application", "es": "Solicitud del visa de remote work"},
                {
                    "en": "Remote work visa application on the basis of the assembled file.",
                    "es": (
                        "Solicitud del visa de remote work sobre la base del expediente reunido."
                    ),
                },
            ),
            (
                {"en": "Medical examination & Emirates ID", "es": "Examen médico y Emirates ID"},
                {
                    "en": "Medical + Emirates ID mandatory. Presence required.",
                    "es": "Médico + Emirates ID obligatorios. Presencia requerida.",
                },
            ),
            (
                {"en": "Visa issuance (1 year)", "es": "Emisión del visa (1 año)"},
                {
                    "en": "Issuance of the remote work visa, valid 1 year.",
                    "es": "Emisión del visa de remote work, válido 1 año.",
                },
            ),
        ],
    },
    AE_RET_NAME: {
        "name": {
            "en": "Dubai (UAE) — Retiree visa (5 years, 55+)",
            "es": "Dubái (EAU) — Visa de jubilado (5 años, 55 y +)",
        },
        "steps": [
            (
                {
                    "en": "Check the financial criterion (one suffices)",
                    "es": "Verificar el criterio financiero (basta uno)",
                },
                {
                    "en": (
                        "🟠 One of three (indicative amounts, recheck): income ≥ 20 000 AED/month "
                        "· OR savings ≥ 1 M AED · OR property ≥ 1 M AED. Reserved for 55+. Above "
                        "2 M AED of wealth, prefer the Golden Visa (10 years + absence-rule "
                        "exemption)."
                    ),
                    "es": (
                        "🟠 Uno de tres (importes indicativos, reverificar): ingresos ≥ 20 000 "
                        "AED/mes · O ahorro ≥ 1 M AED · O inmueble ≥ 1 M AED. Reservado a los 55 "
                        "y +. Por encima de 2 M AED de patrimonio, preferir el Golden Visa (10 "
                        "años + exención de la regla de ausencia)."
                    ),
                },
            ),
            (
                {"en": "Build the file", "es": "Preparar el expediente"},
                {
                    "en": (
                        "Application file to assemble on the basis of the chosen financial "
                        "criterion."
                    ),
                    "es": (
                        "Expediente de solicitud a preparar sobre la base del criterio financiero "
                        "elegido."
                    ),
                },
            ),
            (
                {"en": "Medical examination & Emirates ID", "es": "Examen médico y Emirates ID"},
                {
                    "en": "Medical + Emirates ID mandatory. Presence required.",
                    "es": "Médico + Emirates ID obligatorios. Presencia requerida.",
                },
            ),
            (
                {
                    "en": "Retiree visa stamped (5 years renewable)",
                    "es": "Visa de jubilado estampada (5 años renovable)",
                },
                {
                    "en": "Retiree visa stamped, valid 5 years renewable.",
                    "es": "Visa de jubilado estampada, válida 5 años renovable.",
                },
            ),
        ],
    },
    AE_CO_NAME: {
        "name": {
            "en": "Dubai (UAE) — Company formation (free zone / mainland)",
            "es": "Dubái (EAU) — Creación de empresa (free zone / mainland)",
        },
        "steps": [
            (
                {
                    "en": "Decide free zone vs mainland (filter question)",
                    "es": "Decidir free zone vs mainland (pregunta filtro)",
                },
                {
                    "en": (
                        "FILTER QUESTION: does the client sell directly on the UAE domestic "
                        "market? YES → mainland (DET): onshore access + public tenders. NO "
                        "(international / B2B / holding / digital / residence goal) → free zone: "
                        "100 % ownership + visa self-sponsoring. 100 % foreign ownership now "
                        "allowed for many mainland activities (a regulated strategic-impact "
                        "activities list — check with the DET)."
                    ),
                    "es": (
                        "PREGUNTA FILTRO: ¿el cliente vende directamente en el mercado interno de "
                        "EAU? SÍ → mainland (DET): acceso onshore + licitaciones públicas. NO "
                        "(internacional / B2B / holding / digital / objetivo residencia) → free "
                        "zone: 100 % propiedad + auto-patrocinio del visa. 100 % de propiedad "
                        "extranjera ahora permitido para muchas actividades mainland (lista de "
                        "actividades de impacto estratégico regulada — verificar ante el DET)."
                    ),
                },
            ),
            (
                {
                    "en": "Name reservation & activity approval",
                    "es": "Reserva del nombre y aprobación de la actividad",
                },
                {
                    "en": (
                        "Name reservation and activity approval via an approved provider, to "
                        "assign on the dossier."
                    ),
                    "es": (
                        "Reserva del nombre y aprobación de la actividad vía un proveedor "
                        "autorizado, a asignar en el expediente."
                    ),
                },
            ),
            (
                {"en": "Licence & establishment", "es": "Licencia y establecimiento"},
                {
                    "en": (
                        "🔴 Costs (free zone package / DET fees / establishment card) = mostly "
                        "commercial sources → cross-check 2-3 providers + authorities (DMCC, "
                        "IFZA, Meydan, DET). Never quote on a single marketing figure."
                    ),
                    "es": (
                        "🔴 Costes (paquete free zone / tasas DET / establishment card) = "
                        "mayoritariamente fuentes comerciales → contrastar 2-3 proveedores + "
                        "autoridades (DMCC, IFZA, Meydan, DET). Nunca cotizar sobre una sola "
                        "cifra de marketing."
                    ),
                },
            ),
            (
                {
                    "en": "Tax registration (corporate tax / VAT) & bank account",
                    "es": "Registro fiscal (corporate tax / IVA) y cuenta bancaria",
                },
                {
                    "en": (
                        "🟢 Corporate tax 0 % up to 375 000 AED of profit, 9 % above. VAT 5 % "
                        "mandatory if turnover > 375 000 AED (voluntary from 187 500). Small "
                        "Business Relief if turnover < 3 M AED (until fiscal years ending "
                        "31/12/2026). ⚠️ The 0 % FREE ZONE (QFZP) is NOT automatic: requires "
                        "substance, qualifying B2B income, de minimis compliance (min 5 M AED / 5 "
                        "% of turnover), transfer pricing, AUDITED FINANCIAL STATEMENTS. Local "
                        "B2C trading in a free zone generally does not qualify. NEVER promise the "
                        "0 % without validation."
                    ),
                    "es": (
                        "🟢 Corporate tax 0 % hasta 375 000 AED de beneficio, 9 % por encima. IVA "
                        "5 % obligatorio si la facturación > 375 000 AED (voluntario desde 187 "
                        "500). Small Business Relief si la facturación < 3 M AED (hasta los "
                        "ejercicios cerrados al 31/12/2026). ⚠️ El 0 % FREE ZONE (QFZP) NO es "
                        "automático: exige sustancia, qualifying income B2B, respeto del de "
                        "minimis (mín. 5 M AED / 5 % de la facturación), precios de "
                        "transferencia, ESTADOS FINANCIEROS AUDITADOS. Un trading B2C local en "
                        "free zone generalmente no da derecho. NUNCA prometer el 0 % sin "
                        "validación."
                    ),
                },
            ),
        ],
    },
    MU_OPI_NAME: {
        "name": {
            "en": "Mauritius — Occupation Permit Investor (entrepreneur)",
            "es": "Mauricio — Occupation Permit Investor (emprendedor)",
        },
        "steps": [
            (
                {
                    "en": "Set up the company & the contribution",
                    "es": "Constituir la empresa y el aporte",
                },
                {
                    "en": (
                        "🟠 Contribution ≥ 50 000 USD + turnover ≥ 4 M MUR expected from year 3 "
                        "(indicative thresholds, revised at the annual Budget ~June). Choice of "
                        "vehicle (Domestic / GBC / Authorised): see the company journey. Carried "
                        "out via a Mauritian adviser, to assign on the dossier."
                    ),
                    "es": (
                        "🟠 Aporte ≥ 50 000 USD + facturación ≥ 4 M MUR esperada desde el año 3 "
                        "(umbrales indicativos, revisados en el Budget anual ~junio). Elección "
                        "del vehículo (Domestic / GBC / Authorised): ver el recorrido de empresa. "
                        "Realizado vía un asesor mauriciano, a asignar en el expediente."
                    ),
                },
            ),
            (
                {"en": "Submission to the EDB", "es": "Presentación ante el EDB"},
                {
                    "en": "Occupation Permit application submitted to the EDB.",
                    "es": "Solicitud de Occupation Permit presentada ante el EDB.",
                },
            ),
            (
                {
                    "en": "Occupation Permit grant & PIO registration",
                    "es": "Otorgamiento del Occupation Permit y registro en el PIO",
                },
                {
                    "en": (
                        "The EDB processes, the PIO issues. Single residence + activity permit. "
                        "Family on a dependent permit. Biometrics required."
                    ),
                    "es": (
                        "El EDB tramita, el PIO emite. Título único de residencia + actividad. "
                        "Familia en título derivado. Biometría requerida."
                    ),
                },
            ),
        ],
    },
    MU_OPP_NAME: {
        "name": {
            "en": "Mauritius — Occupation Permit Professional (employee)",
            "es": "Mauricio — Occupation Permit Professional (asalariado)",
        },
        "steps": [
            (
                {
                    "en": "Contract & salary-threshold check",
                    "es": "Contrato y verificación del umbral salarial",
                },
                {
                    "en": (
                        "🔴 Minimum salary 30 000 vs 60 000 MUR/month depending on period/sector — "
                        "THE most unstable threshold, recheck imperatively. ICT/BPO exceptions "
                        "possibly lower (🟠)."
                    ),
                    "es": (
                        "🔴 Salario mínimo 30 000 vs 60 000 MUR/mes según período/sector — EL "
                        "umbral más inestable, reverificar imperativamente. Excepciones TIC/BPO "
                        "posiblemente más bajas (🟠)."
                    ),
                },
            ),
            (
                {
                    "en": "Submission to the EDB (carried by the employer)",
                    "es": "Presentación ante el EDB (llevada por el empleador)",
                },
                {
                    "en": "Application carried by the employer to the EDB.",
                    "es": "Solicitud llevada por el empleador ante el EDB.",
                },
            ),
            (
                {
                    "en": "OP grant & PIO registration",
                    "es": "Otorgamiento del OP y registro en el PIO",
                },
                {
                    "en": (
                        "Single residence + work permit (no separate permit), up to 10 years. "
                        "Biometrics required."
                    ),
                    "es": (
                        "Título único de residencia + trabajo (sin permiso aparte), hasta 10 "
                        "años. Biometría requerida."
                    ),
                },
            ),
        ],
    },
    MU_OPS_NAME: {
        "name": {
            "en": "Mauritius — Occupation Permit Self-Employed (solo consultant)",
            "es": "Mauricio — Occupation Permit Self-Employed (consultor individual)",
        },
        "steps": [
            (
                {
                    "en": "Check eligibility & the contribution",
                    "es": "Verificar la elegibilidad y el aporte",
                },
                {
                    "en": (
                        "🟠 Contribution ≈ 35 000 USD + activity income ≈ 800 000 MUR expected "
                        "(year 2/3). Indicative thresholds, revised at the annual Budget."
                    ),
                    "es": (
                        "🟠 Aporte ≈ 35 000 USD + ingresos de actividad ≈ 800 000 MUR esperados "
                        "(año 2/3). Umbrales indicativos, revisados en el Budget anual."
                    ),
                },
            ),
            (
                {"en": "Submission to the EDB", "es": "Presentación ante el EDB"},
                {
                    "en": "Occupation Permit application submitted to the EDB.",
                    "es": "Solicitud de Occupation Permit presentada ante el EDB.",
                },
            ),
            (
                {
                    "en": "OP grant & PIO registration",
                    "es": "Otorgamiento del OP y registro en el PIO",
                },
                {
                    "en": (
                        "Single residence + activity permit, up to 10 years. Biometrics required."
                    ),
                    "es": (
                        "Título único de residencia + actividad, hasta 10 años. Biometría "
                        "requerida."
                    ),
                },
            ),
        ],
    },
    MU_PV_NAME: {
        "name": {
            "en": "Mauritius — Premium Visa (nomad / foreign passive income)",
            "es": "Mauricio — Premium Visa (nómada / renta pasiva extranjera)",
        },
        "steps": [
            (
                {
                    "en": "Check eligibility (foreign income)",
                    "es": "Verificar la elegibilidad (renta extranjera)",
                },
                {
                    "en": (
                        "🟠 Income ≥ 1 500 USD/month (+ ~500/dependent). Announced free. ⚠️ Local "
                        "market prohibited (foreign-source income only). Indicative thresholds, "
                        "annual Budget."
                    ),
                    "es": (
                        "🟠 Ingresos ≥ 1 500 USD/mes (+ ~500/dependiente). Anunciado gratuito. ⚠️ "
                        "Mercado local prohibido (renta de fuente extranjera únicamente). "
                        "Umbrales indicativos, Budget anual."
                    ),
                },
            ),
            (
                {"en": "Online application (EDB)", "es": "Solicitud en línea (EDB)"},
                {
                    "en": "Premium Visa application online with the EDB.",
                    "es": "Solicitud de Premium Visa en línea ante el EDB.",
                },
            ),
            (
                {"en": "Premium Visa grant", "es": "Otorgamiento del Premium Visa"},
                {
                    "en": (
                        "1 year renewable. Does not give access to long-term residence — for "
                        "stability, consider real estate ≥ 375k USD (separate journey)."
                    ),
                    "es": (
                        "1 año renovable. No da acceso a una residencia larga — para la "
                        "estabilidad, considerar el inmueble ≥ 375k USD (otro recorrido)."
                    ),
                },
            ),
        ],
    },
    MU_RE_NAME: {
        "name": {
            "en": "Mauritius — Residence by real estate investment (≥ 375k USD)",
            "es": "Mauricio — Residencia por inversión inmobiliaria (≥ 375k USD)",
        },
        "steps": [
            (
                {
                    "en": "Selection of a qualifying property",
                    "es": "Selección de un inmueble calificado",
                },
                {
                    "en": (
                        "🟠 Threshold ≥ 375 000 USD opening residence (IRS/RES/PDS/Smart "
                        "City/eligible G+2 schemes). Below: purchase possible but WITHOUT "
                        "automatic residence. Registration duties ~5 % (to confirm). Carried out "
                        "via an adviser, to assign on the dossier."
                    ),
                    "es": (
                        "🟠 Umbral ≥ 375 000 USD que abre la residencia (esquemas "
                        "IRS/RES/PDS/Smart City/G+2 elegible). Por debajo: compra posible pero "
                        "SIN residencia automática. Derechos de registro ~5 % (a confirmar). "
                        "Realizado vía un asesor, a asignar en el expediente."
                    ),
                },
            ),
            (
                {"en": "Acquisition & registration", "es": "Adquisición y registro"},
                {
                    "en": "Property acquisition and registration.",
                    "es": "Adquisición del inmueble y registro.",
                },
            ),
            (
                {
                    "en": "Residence application (EDB) & PIO registration",
                    "es": "Solicitud de residencia (EDB) y registro PIO",
                },
                {
                    "en": (
                        "Residence as long as the property is held (permit up to 20 years for "
                        "real estate ≥ 375k). No capital gains tax nor inheritance duties in "
                        "Mauritius. Biometrics required."
                    ),
                    "es": (
                        "Residencia mientras se posea el bien (título hasta 20 años para el "
                        "inmueble ≥ 375k). Sin impuesto sobre plusvalías ni derechos de sucesión "
                        "en Mauricio. Biometría requerida."
                    ),
                },
            ),
        ],
    },
    MU_CO_NAME: {
        "name": {
            "en": "Mauritius — Company formation (Domestic / GBC / Authorised)",
            "es": "Mauricio — Creación de empresa (Domestic / GBC / Authorised)",
        },
        "steps": [
            (
                {
                    "en": "Choose the vehicle (filter question)",
                    "es": "Elegir el vehículo (pregunta filtro)",
                },
                {
                    "en": (
                        "LOCAL MARKET → Domestic Company (IS 15 %, ≥ 1 resident director). "
                        "INTERNATIONAL + need for tax treaties (DTAA) → GBC (~3 % effective via "
                        "the 80 % partial exemption). INTERNATIONAL WITHOUT need for DTAA → "
                        "Authorised Company (0 % in Mauritius, MRA filing, no DTAA access)."
                    ),
                    "es": (
                        "MERCADO LOCAL → Domestic Company (IS 15 %, ≥ 1 administrador residente). "
                        "INTERNACIONAL + necesidad de los convenios fiscales (DTAA) → GBC (~3 % "
                        "efectivo vía la exención parcial del 80 %). INTERNACIONAL SIN necesidad "
                        "de los DTAA → Authorised Company (0 % en Mauricio, declaración MRA, sin "
                        "acceso a los DTAA)."
                    ),
                },
            ),
            (
                {
                    "en": "Incorporation & registration (CBRD)",
                    "es": "Constitución y registro (CBRD)",
                },
                {
                    "en": (
                        "GBC → 2 resident directors + an FSC-licensed management company "
                        "mandatory + annual audit. Authorised → licensed registered agent, "
                        "management/control outside Mauritius."
                    ),
                    "es": (
                        "GBC → 2 administradores residentes + management company autorizada por "
                        "la FSC obligatoria + auditoría anual. Authorised → registered agent "
                        "autorizado, gestión/control fuera de Mauricio."
                    ),
                },
            ),
            (
                {
                    "en": "FSC licence (GBC) / tax registration & VAT",
                    "es": "Licencia FSC (GBC) / registro fiscal e IVA",
                },
                {
                    "en": (
                        "🟠 Standard VAT 15 % (defer below the threshold). CCR Levy 2 % above a "
                        "turnover threshold. No capital gains nor inheritance duties."
                    ),
                    "es": (
                        "🟠 IVA estándar 15 % (diferir bajo el umbral). CCR Levy 2 % por encima de "
                        "un umbral de facturación. Sin plusvalías ni derechos de sucesión."
                    ),
                },
            ),
            (
                {"en": "Substance & governance (if GBC)", "es": "Sustancia y gobernanza (si GBC)"},
                {
                    "en": (
                        "⚠️ NEVER set up a GBC as a letterbox: without real substance (2 resident "
                        "directors, local spending / CIGA, governance in Mauritius), the 80 % "
                        "exemption (~3 %) FALLS and a requalification risk exists."
                    ),
                    "es": (
                        "⚠️ NUNCA montar una GBC como buzón: sin sustancia real (2 "
                        "administradores residentes, gasto local / CIGA, gobernanza en Mauricio), "
                        "la exención del 80 % (~3 %) DECAE y existe un riesgo de recalificación."
                    ),
                },
            ),
        ],
    },
    TH_LTR_NAME: {
        "name": {
            "en": "Thailand — Long-Term Resident (LTR, 10 years)",
            "es": "Tailandia — Long-Term Resident (LTR, 10 años)",
        },
        "steps": [
            (
                {"en": "Identify the LTR category", "es": "Identificar la categoría LTR"},
                {
                    "en": (
                        "🟠 4 categories (indicative thresholds, recheck ltr.boi.go.th): Wealthy "
                        "Global Citizen (high wealth + investment) · Wealthy Pensioner (50+, "
                        "passive income ≥ 80 000 USD/year, or 40-80k with a 250k USD investment) "
                        "· Work-from-Thailand Professional (income ≥ 80 000 USD/year + listed "
                        "employer or > 150 M USD turnover; grants a digital work permit) · "
                        "Highly-Skilled Professional (targeted sectors). 2024-2025 relaxations to "
                        "confirm."
                    ),
                    "es": (
                        "🟠 4 categorías (umbrales indicativos, reverificar ltr.boi.go.th): "
                        "Wealthy Global Citizen (patrimonio elevado + inversión) · Wealthy "
                        "Pensioner (50+, renta pasiva ≥ 80 000 USD/año, o 40-80k con inversión de "
                        "250k USD) · Work-from-Thailand Professional (renta ≥ 80 000 USD/año + "
                        "empleador cotizado o > 150 M USD de facturación; otorga un work permit "
                        "digital) · Highly-Skilled Professional (sectores específicos). "
                        "Relajaciones 2024-2025 por confirmar."
                    ),
                },
            ),
            (
                {
                    "en": "Qualification application to the BOI",
                    "es": "Solicitud de calificación ante el BOI",
                },
                {
                    "en": "Qualification application filed with the BOI.",
                    "es": "Solicitud de calificación presentada ante el BOI.",
                },
            ),
            (
                {"en": "LTR visa issuance & registration", "es": "Emisión del visa LTR y registro"},
                {
                    "en": (
                        "10 years, annual reporting (instead of 90 days). Work-from-Thailand "
                        "includes a digital work permit."
                    ),
                    "es": (
                        "10 años, reporte anual (en lugar de 90 días). Work-from-Thailand incluye "
                        "un work permit digital."
                    ),
                },
            ),
        ],
    },
    TH_OA_NAME: {
        "name": {
            "en": "Thailand — Retiree visa (O-A, 50+)",
            "es": "Tailandia — Visa de jubilado (O-A, 50 y +)",
        },
        "steps": [
            (
                {
                    "en": "Check age & financial criterion",
                    "es": "Verificar la edad y el criterio financiero",
                },
                {
                    "en": (
                        "🟠 ≥ 50 years + deposit 800 000 THB OR income 65 000 THB/month + health "
                        "insurance (~3 M THB coverage). Indicative thresholds. NOTE: the O-X (up "
                        "to 10 years) exists for certain eligible nationalities (US, Canada, "
                        "Australia, UK, Japan…), 3 M THB threshold — list to confirm. ⚠️ Does not "
                        "lead to PR."
                    ),
                    "es": (
                        "🟠 ≥ 50 años + depósito 800 000 THB O ingresos 65 000 THB/mes + seguro de "
                        "salud (cobertura ~3 M THB). Umbrales indicativos. NOTA: el O-X (hasta 10 "
                        "años) existe para ciertas nacionalidades elegibles (US, Canadá, "
                        "Australia, UK, Japón…), umbral 3 M THB — lista por confirmar. ⚠️ No "
                        "conduce a la PR."
                    ),
                },
            ),
            (
                {
                    "en": "O-A visa application (consulate)",
                    "es": "Solicitud de visa O-A (consulado)",
                },
                {
                    "en": "O-A visa application filed at the consulate.",
                    "es": "Solicitud de visa O-A presentada en el consulado.",
                },
            ),
            (
                {
                    "en": "Issuance & registration on arrival",
                    "es": "Emisión y registro a la llegada",
                },
                {
                    "en": "Address reporting every 90 days. Renewable annually.",
                    "es": "Reporte de domicilio cada 90 días. Renovable anualmente.",
                },
            ),
        ],
    },
    TH_PRIV_NAME: {
        "name": {
            "en": "Thailand — Thailand Privilege (paid residence card)",
            "es": "Tailandia — Thailand Privilege (tarjeta de residencia de pago)",
        },
        "steps": [
            (
                {"en": "Choose the membership tier", "es": "Elegir el nivel de membresía"},
                {
                    "en": (
                        "🟠 2026 tiers (indicative, thailandprivilege.co.th): Bronze ~650k / Gold "
                        "~900k / Platinum ~1,5M / Diamond ~2,5M / Reserve ~5M THB. ⚠️ Does NOT "
                        "grant the right to work. Does NOT lead to PR."
                    ),
                    "es": (
                        "🟠 Niveles 2026 (indicativos, thailandprivilege.co.th): Bronze ~650k / "
                        "Gold ~900k / Platinum ~1,5M / Diamond ~2,5M / Reserve ~5M THB. ⚠️ NO da "
                        "el derecho a trabajar. NO conduce a la PR."
                    ),
                },
            ),
            (
                {"en": "Membership application & payment", "es": "Solicitud de membresía y pago"},
                {
                    "en": "Membership application and payment of the chosen tier.",
                    "es": "Solicitud de membresía y pago del nivel elegido.",
                },
            ),
            (
                {
                    "en": "Card & Privilege visa issuance",
                    "es": "Emisión de la tarjeta y del visa Privilege",
                },
                {
                    "en": (
                        "Long-stay according to the tier, services included (airport fast-track, "
                        "assistance). Simplified visa renewal."
                    ),
                    "es": (
                        "Estancia larga según el nivel, servicios incluidos (fast-track en "
                        "aeropuerto, asistencia). Renovación de visa simplificada."
                    ),
                },
            ),
        ],
    },
    TH_NONB_NAME: {
        "name": {
            "en": "Thailand — Non-B + Work Permit (employee)",
            "es": "Tailandia — Non-B + Work Permit (asalariado)",
        },
        "steps": [
            (
                {
                    "en": "The employer checks capital & ratio",
                    "es": "El empleador verifica capital y ratio",
                },
                {
                    "en": (
                        "🟠 Employer side: capital 2 M THB per foreign position (1 M if married to "
                        "a Thai) + ratio 4 Thai employees : 1 foreigner. S-Curve sector + high "
                        "salary → SMART Visa possible (SMART-T ≥ 100 000 THB/month, without a "
                        "separate work permit — beware, many sources still cite 200k)."
                    ),
                    "es": (
                        "🟠 Lado empleador: capital 2 M THB por puesto extranjero (1 M si está "
                        "casado con un·a tailandés·a) + ratio 4 empleados tailandeses : 1 "
                        "extranjero. Sector S-Curve + salario alto → SMART Visa posible (SMART-T "
                        "≥ 100 000 THB/mes, sin work permit aparte — atención, muchas fuentes aún "
                        "citan 200k)."
                    ),
                },
            ),
            (
                {"en": "Non-B visa (consulate)", "es": "Visa Non-B (consulado)"},
                {
                    "en": "Non-B visa application filed at the consulate.",
                    "es": "Solicitud de visa Non-B presentada en el consulado.",
                },
            ),
            (
                {
                    "en": "Work Permit (Department of Employment) & registration",
                    "es": "Work Permit (Department of Employment) y registro",
                },
                {
                    "en": (
                        "90-day reporting. After 3 consecutive years under Non-B + work permit → "
                        "PR application possible (quota ~100/nationality/year). Only this track "
                        "leads to PR."
                    ),
                    "es": (
                        "Reporte cada 90 días. Tras 3 años consecutivos bajo Non-B + work permit "
                        "→ solicitud de PR posible (cupo ~100/nacionalidad/año). Solo esta vía "
                        "conduce a la PR."
                    ),
                },
            ),
        ],
    },
    TH_CO_NAME: {
        "name": {
            "en": "Thailand — Company formation (FBA: 100 % / BOI / Amity / FBL)",
            "es": "Tailandia — Creación de empresa (FBA: 100 % / BOI / Amity / FBL)",
        },
        "steps": [
            (
                {
                    "en": "Qualify the activity in the FBA tree",
                    "es": "Calificar la actividad en el árbol FBA",
                },
                {
                    "en": (
                        "DECISION TREE: (a) activity OUTSIDE the 3 lists (industry/manufacturing) "
                        "→ 100 % foreign, no exemption needed. (b) List 3 activity (services, the "
                        "consultant case) → exemption required: US citizen → US Treaty of Amity "
                        "(100 %, except excluded sectors); promotable activity → BOI (100 % + IS "
                        "exemption up to 8 years/13 for cutting-edge + facilitated work permits, "
                        "exempt from the 4:1 ratio); otherwise → FBL (discretionary, slow, "
                        "capital 3 M THB) OR a genuine Thai partner ≥ 51 %. (c) List 1 = "
                        'forbidden, List 2 = Cabinet approval (rare). 🔴 THE "THAI NOMINEE '
                        'SHAREHOLDERS" SETUP IS ILLEGAL (art. 36 FBA — fines and possible '
                        "prison, divestment order). NEVER propose it."
                    ),
                    "es": (
                        "ÁRBOL DE DECISIÓN: (a) actividad FUERA de las 3 listas "
                        "(industria/manufactura) → 100 % extranjero, sin excepción. (b) actividad "
                        "Lista 3 (servicios, el caso del consultor) → excepción requerida: "
                        "ciudadano US → US Treaty of Amity (100 %, salvo sectores excluidos); "
                        "actividad promovible → BOI (100 % + exención de IS hasta 8 años/13 para "
                        "la punta + work permits facilitados, exento del ratio 4:1); si no → FBL "
                        "(discrecional, lento, capital 3 M THB) O un socio tailandés real ≥ 51 %. "
                        "(c) Lista 1 = prohibido, Lista 2 = aprobación del Gabinete (raro). 🔴 EL "
                        'MONTAJE "ACCIONISTAS TAILANDESES NOMINEE" ES ILEGAL (art. 36 FBA — '
                        "multas y posible prisión, orden de cesión). NUNCA proponerlo."
                    ),
                },
            ),
            (
                {"en": "Incorporation & registration (DBD)", "es": "Constitución y registro (DBD)"},
                {
                    "en": (
                        "🟠 DBD fees ~5 000-6 000 THB. BOI / FBL promotion = additional procedure "
                        "depending on the route chosen in step 1."
                    ),
                    "es": (
                        "🟠 Tasas DBD ~5 000-6 000 THB. Promoción BOI / FBL = procedimiento "
                        "adicional según la vía elegida en el paso 1."
                    ),
                },
            ),
            (
                {"en": "Tax registration & VAT", "es": "Registro fiscal e IVA"},
                {
                    "en": (
                        "🟠 SME IS (paid-up capital ≤ 5 M THB AND turnover ≤ 30 M THB): scale 0 % "
                        "/ 15 % / 20 %; otherwise 20 % flat. VAT 7 % mandatory if turnover > 1,8 "
                        "M THB/year. Indicative rates (rd.go.th)."
                    ),
                    "es": (
                        "🟠 IS PYME (capital desembolsado ≤ 5 M THB Y facturación ≤ 30 M THB): "
                        "escala 0 % / 15 % / 20 %; si no 20 % plano. IVA 7 % obligatorio si la "
                        "facturación > 1,8 M THB/año. Tasas indicativas (rd.go.th)."
                    ),
                },
            ),
            (
                {
                    "en": "Foreign director's visa/permit",
                    "es": "Visa/permiso del directivo extranjero",
                },
                {
                    "en": (
                        "Outside the BOI, running one's company requires Non-B + work permit "
                        "(capital 2 M THB/position + ratio 4:1). BOI = facilitated work permits, "
                        "exempt from the ratio."
                    ),
                    "es": (
                        "Fuera del BOI, dirigir su empresa exige Non-B + work permit (capital 2 M "
                        "THB/puesto + ratio 4:1). BOI = work permits facilitados, exento del "
                        "ratio."
                    ),
                },
            ),
        ],
    },
    ID_RW_NAME: {
        "name": {
            "en": "Indonesia — Remote Worker KITAS (E33G, nomad)",
            "es": "Indonesia — Remote Worker KITAS (E33G, nómada)",
        },
        "steps": [
            (
                {
                    "en": "Check eligibility (foreign income)",
                    "es": "Verificar la elegibilidad (renta extranjera)",
                },
                {
                    "en": (
                        "🟠 Foreign income ~60 000 USD/year (indicative, evisa.imigrasi.go.id). ⚠️ "
                        "Work ONLY for clients/employers OUTSIDE Indonesia. No higher tier like "
                        "the LTR — E33G is the only nomad route."
                    ),
                    "es": (
                        "🟠 Renta extranjera ~60 000 USD/año (indicativo, evisa.imigrasi.go.id). "
                        "⚠️ Trabajo ÚNICAMENTE para clientes/empleadores FUERA de Indonesia. Sin "
                        "nivel superior tipo LTR — E33G es la única vía del nómada."
                    ),
                },
            ),
            (
                {
                    "en": "e-visa application (self-sponsoring by income)",
                    "es": "Solicitud e-visa (autopatrocinio por ingresos)",
                },
                {
                    "en": "e-visa application, self-sponsoring through foreign income.",
                    "es": "Solicitud e-visa, autopatrocinio a través de los ingresos extranjeros.",
                },
            ),
            (
                {
                    "en": "KITAS issuance & registration on arrival",
                    "es": "Emisión del KITAS y registro a la llegada",
                },
                {
                    "en": "~1 year renewable. Does not lead to the KITAP. Biometrics required.",
                    "es": "~1 año renovable. No conduce al KITAP. Biometría requerida.",
                },
            ),
        ],
    },
    ID_SH_NAME: {
        "name": {
            "en": "Indonesia — Second Home Visa (rentier)",
            "es": "Indonesia — Second Home Visa (rentista)",
        },
        "steps": [
            (
                {
                    "en": "Check the deposit / proof of funds",
                    "es": "Verificar el depósito / proof of funds",
                },
                {
                    "en": (
                        "🟠 Deposit ~IDR 2 bn (≈ 130 000 USD) — amount diverging across sources, "
                        "recheck evisa.imigrasi.go.id. No age requirement. ⚠️ No right to work."
                    ),
                    "es": (
                        "🟠 Depósito ~IDR 2 mil millones (≈ 130 000 USD) — importe divergente "
                        "según las fuentes, reverificar evisa.imigrasi.go.id. Sin requisito de "
                        "edad. ⚠️ Sin derecho a trabajar."
                    ),
                },
            ),
            (
                {
                    "en": "e-visa application (self-sponsoring by funds)",
                    "es": "Solicitud e-visa (autopatrocinio por fondos)",
                },
                {
                    "en": "e-visa application, self-sponsoring through the deposited funds.",
                    "es": "Solicitud e-visa, autopatrocinio a través de los fondos depositados.",
                },
            ),
            (
                {"en": "Visa issuance (5 or 10 years)", "es": "Emisión del visa (5 o 10 años)"},
                {
                    "en": (
                        "5 or 10 years depending on the file. Higher capital + long horizon → "
                        "compare with the Golden Visa."
                    ),
                    "es": (
                        "5 o 10 años según el expediente. Capital más alto + horizonte largo → "
                        "comparar con el Golden Visa."
                    ),
                },
            ),
        ],
    },
    ID_RET_NAME: {
        "name": {
            "en": "Indonesia — Retirement KITAS (E33F, 55+)",
            "es": "Indonesia — Retirement KITAS (E33F, 55 y +)",
        },
        "steps": [
            (
                {
                    "en": "Check age & appoint a licensed sponsor agent",
                    "es": "Verificar la edad y mandatar un agente patrocinador autorizado",
                },
                {
                    "en": (
                        "🟠 ≥ 55 years + minimum pension + health insurance. LICENSED SPONSOR "
                        "AGENT MANDATORY (sometimes employing a local is required — practice "
                        "varies). ⚠️ No right to work."
                    ),
                    "es": (
                        "🟠 ≥ 55 años + pensión mínima + seguro de salud. AGENTE PATROCINADOR "
                        "AUTORIZADO OBLIGATORIO (a veces se exige emplear a un local — práctica "
                        "variable). ⚠️ Sin derecho a trabajar."
                    ),
                },
            ),
            (
                {"en": "KITAS application via the agent", "es": "Solicitud de KITAS vía el agente"},
                {
                    "en": "KITAS application carried by the licensed sponsor agent.",
                    "es": "Solicitud de KITAS llevada por el agente patrocinador autorizado.",
                },
            ),
            (
                {"en": "KITAS issuance & registration", "es": "Emisión del KITAS y registro"},
                {
                    "en": (
                        "1 year renewable, a possible chain toward the KITAP. ⚠️ SPONSOR RISK: "
                        "the permit falls if the sponsor (agent) stops — plan a fallback route. "
                        "Biometrics required."
                    ),
                    "es": (
                        "1 año renovable, posible cadena hacia el KITAP. ⚠️ RIESGO SPONSOR: el "
                        "título decae si el sponsor (agente) cesa — prever una vía de repliegue. "
                        "Biometría requerida."
                    ),
                },
            ),
        ],
    },
    ID_WORK_NAME: {
        "name": {
            "en": "Indonesia — Work KITAS (E23, employee)",
            "es": "Indonesia — Work KITAS (E23, asalariado)",
        },
        "steps": [
            (
                {
                    "en": "The employer obtains the RPTKA (foreign-worker plan)",
                    "es": "El empleador obtiene el RPTKA (plan de empleo de extranjeros)",
                },
                {
                    "en": (
                        "Sponsor employer MANDATORY, position open to foreigners. DKP-TKA ~100 "
                        "USD/month (~1 200/year) borne by the employer."
                    ),
                    "es": (
                        "Empleador patrocinador OBLIGATORIO, puesto abierto a extranjeros. "
                        "DKP-TKA ~100 USD/mes (~1 200/año) a cargo del empleador."
                    ),
                },
            ),
            (
                {"en": "Work visa & KITAS issuance", "es": "Visa de trabajo y emisión del KITAS"},
                {
                    "en": "Work visa then KITAS issuance.",
                    "es": "Visa de trabajo y luego emisión del KITAS.",
                },
            ),
            (
                {"en": "Registration & work permit", "es": "Registro y permiso de trabajo"},
                {
                    "en": (
                        "6 months to 2 years renewable. After 3-4 continuous years → KITAP "
                        "possible. ⚠️ SPONSOR RISK: the KITAS falls at the end of the contract — "
                        "plan a fallback. Biometrics required."
                    ),
                    "es": (
                        "6 meses a 2 años renovable. Tras 3-4 años continuos → KITAP posible. ⚠️ "
                        "RIESGO SPONSOR: el KITAS decae al final del contrato — prever un "
                        "repliegue. Biometría requerida."
                    ),
                },
            ),
        ],
    },
    ID_INV_NAME: {
        "name": {
            "en": "Indonesia — Investor KITAS (E28A) + PT PMA",
            "es": "Indonesia — Investor KITAS (E28A) + PT PMA",
        },
        "steps": [
            (
                {
                    "en": "Set up the PT PMA (prerequisite)",
                    "es": "Constituir la PT PMA (requisito previo)",
                },
                {
                    "en": (
                        'See the "PT PMA company" journey for the detail (KBLI, capital). The '
                        "company sponsors its director's visa. Carried out via a notary/adviser, "
                        "to assign on the dossier."
                    ),
                    "es": (
                        "Ver el recorrido «Sociedad PT PMA» para el detalle (KBLI, capital). La "
                        "empresa patrocina el visa de su director. Realizado vía un "
                        "notario/asesor, a asignar en el expediente."
                    ),
                },
            ),
            (
                {
                    "en": "Check the role & shareholding threshold",
                    "es": "Verificar el rol y el umbral de participación",
                },
                {
                    "en": (
                        "🔴 Shareholding ~IDR 1 bn (sometimes 1.125 bn) — mostly an agency source, "
                        "recheck. ACTIVE DIRECTOR → may work (Investor KITAS); PASSIVE "
                        "SHAREHOLDER → holding only, no right to work."
                    ),
                    "es": (
                        "🔴 Participación ~IDR 1 mil millones (a veces 1,125 mil millones) — "
                        "mayoritariamente fuente de agencias, reverificar. DIRECTOR ACTIVO → "
                        "puede trabajar (Investor KITAS); ACCIONISTA PASIVO → solo tenencia, sin "
                        "derecho a trabajar."
                    ),
                },
            ),
            (
                {
                    "en": "Investor KITAS application (sponsor = PT PMA)",
                    "es": "Solicitud de Investor KITAS (sponsor = PT PMA)",
                },
                {
                    "en": "Investor KITAS application, the PT PMA acting as sponsor.",
                    "es": "Solicitud de Investor KITAS, la PT PMA actuando como sponsor.",
                },
            ),
            (
                {"en": "KITAS issuance & registration", "es": "Emisión del KITAS y registro"},
                {
                    "en": (
                        "1-2 years renewable, chain toward KITAP. ⚠️ SPONSOR RISK: dissolving the "
                        "PT PMA cancels the KITAS. Biometrics required."
                    ),
                    "es": (
                        "1-2 años renovable, cadena hacia KITAP. ⚠️ RIESGO SPONSOR: la disolución "
                        "de la PT PMA anula el KITAS. Biometría requerida."
                    ),
                },
            ),
        ],
    },
    ID_CO_NAME: {
        "name": {
            "en": "Indonesia — Company formation (PT PMA)",
            "es": "Indonesia — Creación de empresa (PT PMA)",
        },
        "steps": [
            (
                {
                    "en": "Identify the KBLI & check the Positive Investment List",
                    "es": "Identificar el KBLI y verificar la Positive Investment List",
                },
                {
                    "en": (
                        "Identify the KBLI 2020 code (5 digits). CLOSED (~6 sectors) → no PT PMA. "
                        "CAPPED → max foreign % + local partner. OPEN (most cases since the 2020 "
                        "Omnibus) → 100 % foreign. 🔴 THE NOMINEE (Indonesian frontman) IS ILLEGAL "
                        "AND VOID (art. 33 UU 25/2007): the agreement is void, possible loss of "
                        "the investment, the frontman is the legal owner. NEVER propose it (max "
                        "risk on Bali real estate)."
                    ),
                    "es": (
                        "Identificar el código KBLI 2020 (5 dígitos). CERRADA (~6 sectores) → sin "
                        "PT PMA. LIMITADA → % máx. extranjero + socio local. ABIERTA (la mayoría "
                        "de los casos desde el Omnibus 2020) → 100 % extranjero. 🔴 EL NOMINEE "
                        "(testaferro indonesio) ES ILEGAL Y NULO (art. 33 UU 25/2007): nulidad "
                        "del acuerdo, posible pérdida de la inversión, el testaferro es el "
                        "propietario legal. NUNCA proponerlo (riesgo máx. sobre el inmobiliario "
                        "en Bali)."
                    ),
                },
            ),
            (
                {
                    "en": "Check the capital & the OSS risk level",
                    "es": "Verificar el capital y el nivel de riesgo OSS",
                },
                {
                    "en": (
                        "🟠 Investment plan > IDR 10 bn (excluding land/building) per "
                        "KBLI/location + paid-up capital ~IDR 10 bn. ⚠️ STOP USING the old 2.5 bn "
                        "threshold (pre-2021) — a frequent agency error. OSS risk level: low → "
                        "NIB suffices; high → NIB + izin."
                    ),
                    "es": (
                        "🟠 Plan de inversión > IDR 10 mil millones (excluyendo terreno/edificio) "
                        "por KBLI/localización + capital desembolsado ~IDR 10 mil millones. ⚠️ "
                        "DEJAR DE USAR el antiguo umbral de 2,5 mil millones (pre-2021) — error "
                        "frecuente de las agencias. Nivel de riesgo OSS: bajo → NIB basta; alto → "
                        "NIB + izin."
                    ),
                },
            ),
            (
                {
                    "en": "Incorporation (notary) & OSS registration (NIB)",
                    "es": "Constitución (notario) y registro OSS (NIB)",
                },
                {
                    "en": "Incorporation before a notary and OSS registration (NIB).",
                    "es": "Constitución ante notario y registro OSS (NIB).",
                },
            ),
            (
                {"en": "Tax registration & VAT", "es": "Registro fiscal e IVA"},
                {
                    "en": (
                        "🟠 IS 22 % standard (art. 31E reduction ≈ 11 % effective if turnover ≤ "
                        "IDR 50 bn). Final SME regime 0.5 % of turnover if turnover ≤ IDR 4.8 bn "
                        "(max 3 years for a PT). VAT mandatory if turnover > IDR 4.8 bn (~11 % "
                        "effective, the most volatile point). Indicative rates (pajak.go.id)."
                    ),
                    "es": (
                        "🟠 IS 22 % estándar (reducción art. 31E ≈ 11 % efectivo si la facturación "
                        "≤ IDR 50 mil millones). Régimen PYME final 0,5 % de la facturación si la "
                        "facturación ≤ IDR 4,8 mil millones (máx. 3 años para una PT). IVA "
                        "obligatorio si la facturación > IDR 4,8 mil millones (~11 % efectivo, el "
                        "punto más volátil). Tasas indicativas (pajak.go.id)."
                    ),
                },
            ),
        ],
    },
    PH_SRRV_NAME: {
        "name": {
            "en": "Philippines — SRRV (residence by deposit, via PRA)",
            "es": "Filipinas — SRRV (residencia por depósito, vía PRA)",
        },
        "steps": [
            (
                {
                    "en": "Choose the variant & the deposit",
                    "es": "Elegir la variante y el depósito",
                },
                {
                    "en": (
                        "🟠 Variants (USD deposits, indicative, pra.gov.ph): Smile 20k (not "
                        "convertible to real estate) · Classic 35-49 yrs 50k (convertible to "
                        "condo/lease) · Classic 50+ WITH pension ≥ 800 USD/month (1 000 couple) "
                        "10k · Classic 50+ without pension 20k · Human Touch 10k (+1 500 "
                        "USD/month). ⚠️ RESIDING ≠ WORKING: the SRRV does NOT grant the right to "
                        "work (DOLE AEP required in addition). NOTE: a Digital Nomad Visa (EO 86, "
                        "2025) exists on paper but is NOT operational — do not propose it until "
                        "issuance is confirmed."
                    ),
                    "es": (
                        "🟠 Variantes (depósitos USD, indicativos, pra.gov.ph): Smile 20k (no "
                        "convertible a inmueble) · Classic 35-49 años 50k (convertible a "
                        "condo/arrendamiento) · Classic 50+ CON pensión ≥ 800 USD/mes (1 000 "
                        "pareja) 10k · Classic 50+ sin pensión 20k · Human Touch 10k (+1 500 "
                        "USD/mes). ⚠️ RESIDIR ≠ TRABAJAR: el SRRV NO da el derecho a trabajar "
                        "(AEP del DOLE requerido además). NOTA: un Digital Nomad Visa (EO 86, "
                        "2025) existe en el papel pero NO es operativo — no proponerlo hasta "
                        "confirmar su emisión."
                    ),
                },
            ),
            (
                {
                    "en": "Build the file & transfer the deposit",
                    "es": "Preparar el expediente y transferir el depósito",
                },
                {
                    "en": (
                        "File assembly and transfer of the deposit to the PRA-designated account."
                    ),
                    "es": (
                        "Preparación del expediente y transferencia del depósito a la cuenta "
                        "designada por la PRA."
                    ),
                },
            ),
            (
                {"en": "SRRV grant (PRA) & ID", "es": "Otorgamiento del SRRV (PRA) e ID"},
                {
                    "en": (
                        "🟠 PRA fees ~1 400 USD + ~300/dependent + ~360/year. The deposit can be "
                        "converted into a condo (not into land: foreigners cannot own land; "
                        "condos capped at 40 % of the building)."
                    ),
                    "es": (
                        "🟠 Tasas PRA ~1 400 USD + ~300/dependiente + ~360/año. El depósito puede "
                        "convertirse en un condo (no en terreno: los extranjeros no pueden poseer "
                        "terreno; condos limitados al 40 % del edificio)."
                    ),
                },
            ),
        ],
    },
    PH_SIRV_NAME: {
        "name": {
            "en": "Philippines — SIRV (investor visa, via BOI)",
            "es": "Filipinas — SIRV (visa de inversionista, vía BOI)",
        },
        "steps": [
            (
                {"en": "Check the eligible investment", "es": "Verificar la inversión admisible"},
                {
                    "en": (
                        "🟠 ~75 000 USD invested and maintained (a mere real-estate purchase "
                        "generally does not qualify — eligible assets defined by the BOI). ⚠️ "
                        "RESIDING ≠ WORKING: investor status, not employee — running one's "
                        "company as an employee requires a 9(g) + AEP."
                    ),
                    "es": (
                        "🟠 ~75 000 USD invertidos y mantenidos (una simple compra inmobiliaria "
                        "generalmente no califica — activos admisibles definidos por el BOI). ⚠️ "
                        "RESIDIR ≠ TRABAJAR: estatus de inversionista, no asalariado — dirigir su "
                        "empresa como asalariado exige un 9(g) + AEP."
                    ),
                },
            ),
            (
                {
                    "en": "Make the investment & file the application (BOI)",
                    "es": "Realizar la inversión y presentar la solicitud (BOI)",
                },
                {
                    "en": "Making the investment and filing the application with the BOI.",
                    "es": "Realización de la inversión y presentación de la solicitud ante el BOI.",
                },
            ),
            (
                {
                    "en": "SIRV grant (BI on BOI endorsement) & ID",
                    "es": "Otorgamiento del SIRV (BI sobre endoso del BOI) e ID",
                },
                {
                    "en": "Residence as long as the investment is maintained.",
                    "es": "Residencia mientras se mantenga la inversión.",
                },
            ),
        ],
    },
    PH_13A_NAME: {
        "name": {
            "en": "Philippines — 13(a) Visa (spouse of a Philippine national)",
            "es": "Filipinas — Visa 13(a) (cónyuge de un·a nacional filipino·a)",
        },
        "steps": [
            (
                {
                    "en": "Check reciprocity & the marriage",
                    "es": "Verificar la reciprocidad y el matrimonio",
                },
                {
                    "en": (
                        "🟠 The 13(a) is subject to RECIPROCITY: open to nationals of countries "
                        "granting an equivalent right to Filipinos (most Western countries have "
                        "it — to verify by nationality). Valid marriage to a Philippine national "
                        "required."
                    ),
                    "es": (
                        "🟠 El 13(a) está sujeto a RECIPROCIDAD: abierto a los nacionales de "
                        "países que otorgan un derecho equivalente a los filipinos (la mayoría de "
                        "los países occidentales lo tienen — a verificar por nacionalidad). "
                        "Matrimonio válido con un·a filipino·a requerido."
                    ),
                },
            ),
            (
                {
                    "en": "File the application (BI) — 1-year probationary status",
                    "es": "Presentar la solicitud (BI) — estatus probatorio de 1 año",
                },
                {
                    "en": "Application filed with the BI; one-year probationary status.",
                    "es": "Solicitud presentada ante el BI; estatus probatorio de un año.",
                },
            ),
            (
                {
                    "en": "Conversion to permanent resident (after 1 year of probation)",
                    "es": "Conversión en residente permanente (tras 1 año de probación)",
                },
                {
                    "en": (
                        "Permanent resident, exempt from AEP to work (to confirm). ACR I-Card + "
                        "Annual Report."
                    ),
                    "es": (
                        "Residente permanente, exento de AEP para trabajar (a confirmar). ACR "
                        "I-Card + Annual Report."
                    ),
                },
            ),
        ],
    },
    PH_CO_NAME: {
        "name": {
            "en": "Philippines — Company formation (60/40 / FINL / export / DME)",
            "es": "Filipinas — Creación de empresa (60/40 / FINL / export / DME)",
        },
        "steps": [
            (
                {
                    "en": "Qualify the activity (FINL) & the market mode",
                    "es": "Calificar la actividad (FINL) y el modo de mercado",
                },
                {
                    "en": (
                        "TREE: (a) activity on FINL List A (land, resources, public utilities, "
                        "media, certain professions) → 60/40 with a REAL majority Philippine "
                        "partner. (b) export ≥ 60 % → 100 % foreign, exempt from the 200k USD "
                        "threshold (capital ~5 000 PHP; obligation to keep 60 % export). (c) "
                        "domestic market, foreign majority → DME, capital 200 000 USD (reducible "
                        "to 100 000 if advanced tech/endorsed startup/≥ 50 Philippine employees). "
                        "🔴 ANTI-DUMMY LAW (CA 108): the façade 60/40 (Philippine frontman, hidden "
                        "voting trust, share-backed loans) is ILLEGAL — criminal penalties for "
                        "the foreigner AND the frontman. The 60/40 must reflect REAL Philippine "
                        "economic control. NEVER propose it."
                    ),
                    "es": (
                        "ÁRBOL: (a) actividad en la Lista A de la FINL (suelo, recursos, public "
                        "utilities, medios, ciertas profesiones) → 60/40 con un socio filipino "
                        "mayoritario REAL. (b) export ≥ 60 % → 100 % extranjero, exento del "
                        "umbral de 200k USD (capital ~5 000 PHP; obligación de mantener 60 % de "
                        "export). (c) mercado interno, extranjero mayoritario → DME, capital 200 "
                        "000 USD (reducible a 100 000 si tech avanzada/startup endosada/≥ 50 "
                        "empleados filipinos). 🔴 ANTI-DUMMY LAW (CA 108): el 60/40 de fachada "
                        "(testaferro filipino, voting trust oculto, préstamos respaldados por "
                        "acciones) es ILEGAL — sanciones penales para el extranjero Y el "
                        "testaferro. El 60/40 debe reflejar un control económico filipino REAL. "
                        "NUNCA proponerlo."
                    ),
                },
            ),
            (
                {"en": "Incorporation & SEC registration", "es": "Constitución y registro SEC"},
                {
                    "en": "Incorporation and registration with the SEC.",
                    "es": "Constitución y registro ante la SEC.",
                },
            ),
            (
                {"en": "Tax registration (BIR) & VAT", "es": "Registro fiscal (BIR) e IVA"},
                {
                    "en": (
                        "🟠 CIT 25 % standard (20 % if taxable income ≤ 5 M PHP AND assets ≤ 100 M "
                        "PHP excluding land). VAT 12 % if turnover > 3 M PHP (otherwise "
                        "percentage tax 3 %). Indicative rates (bir.gov.ph)."
                    ),
                    "es": (
                        "🟠 CIT 25 % estándar (20 % si la renta imponible ≤ 5 M PHP Y los activos "
                        "≤ 100 M PHP excluyendo terreno). IVA 12 % si la facturación > 3 M PHP "
                        "(si no, percentage tax 3 %). Tasas indicativas (bir.gov.ph)."
                    ),
                },
            ),
            (
                {"en": "(Optional) BOI/PEZA incentives", "es": "(Opcional) incentivos BOI/PEZA"},
                {
                    "en": (
                        "If eligible activity (SIPP): ITH 4-7 years then 5 % SCIT or Enhanced "
                        "Deductions. Possible link with the director's SIRV/9(g)."
                    ),
                    "es": (
                        "Si la actividad es elegible (SIPP): ITH 4-7 años luego 5 % SCIT o "
                        "Enhanced Deductions. Vínculo posible con el SIRV/9(g) del directivo."
                    ),
                },
            ),
        ],
    },
    PT_CRUE_NAME: {
        "name": {
            "en": "Portugal — EU residence registration (CRUE)",
            "es": "Portugal — Registro de residencia UE (CRUE)",
        },
        "steps": [
            (
                {"en": "Obtain the NIF (tax number)", "es": "Obtener el NIF (número fiscal)"},
                {
                    "en": (
                        "NIF required for a lease, bank, formalities. NISS (social security) "
                        "depending on activity."
                    ),
                    "es": (
                        "NIF requerido para arrendamiento, banco, gestiones. NISS (seguridad "
                        "social) según la actividad."
                    ),
                },
            ),
            (
                {
                    "en": "CRUE application at the town hall (Câmara Municipal)",
                    "es": "Solicitud de CRUE en el ayuntamiento (Câmara Municipal)",
                },
                {
                    "en": (
                        "Certificate often issued the same day. Issued by the town hall, NOT by "
                        "AIMA → outside the backlog. Presence required."
                    ),
                    "es": (
                        "Certificado a menudo emitido el mismo día. Emitido por el ayuntamiento, "
                        "NO por AIMA → fuera del atraso. Presencia requerida."
                    ),
                },
            ),
            (
                {
                    "en": "Permanent residence (at 5 years)",
                    "es": "Residencia permanente (a los 5 años)",
                },
                {
                    "en": (
                        "⚠️ Naturalization at 5 years today, but a 2025 reform underway may "
                        "extend it (7/10 years) — a regulatory risk, not a given."
                    ),
                    "es": (
                        "⚠️ Naturalización a los 5 años hoy, pero una reforma 2025 en curso "
                        "podría alargarla (7/10 años) — riesgo regulatorio, no un derecho "
                        "adquirido."
                    ),
                },
            ),
        ],
    },
    PT_D8_NAME: {
        "name": {
            "en": "Portugal — D8 Visa (digital nomad, non-EU)",
            "es": "Portugal — Visa D8 (nómada digital, fuera de la UE)",
        },
        "steps": [
            (
                {"en": "NIF + Portuguese bank account", "es": "NIF + cuenta bancaria portuguesa"},
                {
                    "en": "Tax representative required for a non-EU non-resident.",
                    "es": (
                        "Representante fiscal obligatorio para un no residente de fuera de la UE."
                    ),
                },
            ),
            (
                {
                    "en": "D8 visa application at the consulate",
                    "es": "Solicitud de visa D8 en el consulado",
                },
                {
                    "en": (
                        "🟠 Threshold ~4× the SMN (~3 480 €/month 2025, to be confirmed). ⚠️ D8 = "
                        "ACTIVE foreign income (passive income falls under the D7). 2 variants: "
                        "temporary stay (~1 year) OR residence visa (counts toward the 5 years). "
                        "Choose the residence variant for a settlement project."
                    ),
                    "es": (
                        "🟠 Umbral ~4× el SMN (~3 480 €/mes 2025, a confirmar). ⚠️ D8 = renta "
                        "ACTIVA extranjera (la renta pasiva corresponde al D7). 2 variantes: "
                        "estancia temporal (~1 año) O visa de residencia (cuenta para los 5 "
                        "años). Elegir la variante residencia para un proyecto de instalación."
                    ),
                },
            ),
            (
                {
                    "en": "Conversion to a residence permit at AIMA",
                    "es": "Conversión en permiso de residencia en AIMA",
                },
                {
                    "en": (
                        "🔴 Real AIMA processing time (backlog), months to > 1 year, not "
                        "guaranteed. Biometrics required."
                    ),
                    "es": (
                        "🔴 Plazo real de AIMA (atraso), de meses a > 1 año, no garantizado. "
                        "Biometría requerida."
                    ),
                },
            ),
        ],
    },
    PT_GV_NAME: {
        "name": {
            "en": "Portugal — Golden Visa / ARI (passive investor, post-2023)",
            "es": "Portugal — Golden Visa / ARI (inversionista pasivo, post-2023)",
        },
        "steps": [
            (
                {
                    "en": "Choose the investment route (post-2023)",
                    "es": "Elegir la vía de inversión (post-2023)",
                },
                {
                    "en": (
                        "🟠 CURRENT routes (indicative amounts): qualifying funds ≥ 500 000 € · "
                        "creation of 10 jobs · R&D ≥ 500 000 € · cultural support ≥ 250 000 € · "
                        "company capitalization ≥ 500 000 €. ⚠️ REAL ESTATE and plain capital "
                        "transfer were REMOVED in 2023 (Mais Habitação law) — any brochure citing "
                        "a property purchase (280k/350k/500k) is FALSE."
                    ),
                    "es": (
                        "🟠 Vías ACTUALES (importes indicativos): fondos calificados ≥ 500 000 € · "
                        "creación de 10 empleos · I+D ≥ 500 000 € · apoyo cultural ≥ 250 000 € · "
                        "capitalización de empresa ≥ 500 000 €. ⚠️ El INMOBILIARIO y la simple "
                        "transferencia de capital fueron RETIRADOS en 2023 (ley Mais Habitação) — "
                        "cualquier folleto que cite la compra inmobiliaria (280k/350k/500k) es "
                        "FALSO."
                    ),
                },
            ),
            (
                {"en": "Make the investment + NIF", "es": "Realizar la inversión + NIF"},
                {
                    "en": "Making the chosen investment and obtaining the NIF.",
                    "es": "Realización de la inversión elegida y obtención del NIF.",
                },
            ),
            (
                {"en": "ARI application at AIMA", "es": "Solicitud de ARI ante AIMA"},
                {
                    "en": (
                        "🔴 Fees ~5 300 € + ~600 €. Real AIMA processing time (backlog), not "
                        "guaranteed. Minimum presence ~7 days/year. ARI time counts toward "
                        "permanent residence/nationality (subject to the 2025 citizenship "
                        "reform)."
                    ),
                    "es": (
                        "🔴 Tasas ~5 300 € + ~600 €. Plazo real de AIMA (atraso), no garantizado. "
                        "Presencia mínima ~7 días/año. El tiempo ARI cuenta para la residencia "
                        "permanente/nacionalidad (sujeto a la reforma de ciudadanía 2025)."
                    ),
                },
            ),
        ],
    },
    VN_WP_NAME: {
        "name": {
            "en": "Vietnam — Work Permit + TRC (employee)",
            "es": "Vietnam — Work Permit + TRC (asalariado)",
        },
        "steps": [
            (
                {
                    "en": "The employer obtains approval of the foreign-labor need",
                    "es": (
                        "El empleador obtiene la aprobación de la necesidad de mano de obra "
                        "extranjera"
                    ),
                },
                {
                    "en": (
                        "🔴 Work permit issuing authority UNCERTAIN since the 2025 administrative "
                        "reorganization (DOLISA → Ministry of the Interior?) — to confirm "
                        "province by province. Quota + qualification (~3 years of experience for "
                        '"expert"). Permit exemption (LD1) if capital contributed ≥ ~3 bn VND.'
                    ),
                    "es": (
                        "🔴 Autoridad emisora del work permit INCIERTA desde la reorganización "
                        "administrativa 2025 (DOLISA → Ministerio del Interior?) — a confirmar "
                        "provincia por provincia. Cupo + cualificación (~3 años de experiencia "
                        'para "experto"). Exención de permiso (LD1) si el capital aportado ≥ ~3 '
                        "mil millones VND."
                    ),
                },
            ),
            (
                {
                    "en": "Work Permit + LD2 visa (or LD1 if exempt)",
                    "es": "Work Permit + visa LD2 (o LD1 si exento)",
                },
                {
                    "en": "Issuance of the Work Permit and the LD2 visa (LD1 if exempt).",
                    "es": "Emisión del Work Permit y del visa LD2 (LD1 si exento).",
                },
            ),
            (
                {
                    "en": "Temporary residence card (TRC)",
                    "es": "Tarjeta de residencia temporal (TRC)",
                },
                {
                    "en": (
                        "TRC up to 2 years, tied to the employer. After 3 years of continuous TRC "
                        '+ sponsor → PRC possible (rare, discretionary). ⚠️ For a "retirement" or '
                        '"nomad" need, Vietnam has no route — redirect '
                        "(Thailand/Indonesia/Philippines). Biometrics required."
                    ),
                    "es": (
                        "TRC hasta 2 años, ligada al empleador. Tras 3 años de TRC continua + "
                        "sponsor → PRC posible (raro, discrecional). ⚠️ Para una necesidad de "
                        '"jubilación" o "nómada", Vietnam no tiene vía — reorientar '
                        "(Tailandia/Indonesia/Filipinas). Biometría requerida."
                    ),
                },
            ),
        ],
    },
    VN_INV_NAME: {
        "name": {
            "en": "Vietnam — Investor TRC (DT1-DT4)",
            "es": "Vietnam — Investor TRC (DT1-DT4)",
        },
        "steps": [
            (
                {
                    "en": "Set up the company (prerequisite) & calibrate the capital",
                    "es": "Constituir la empresa (requisito previo) y calibrar el capital",
                },
                {
                    "en": (
                        'See the "Company (FDI LLC)" journey for the detail (IRC→ERC, OMC, DICA). '
                        "🟠 CAPITAL ↔ TRC: DT1 ≥ 100 bn VND (~3.9 M USD) → TRC 10 years (+ PRC "
                        "route) · DT2 50-100 bn → 5 years · DT3 3-50 bn (~120k USD) → 3 years "
                        "(practical minimum for a TRC) · DT4 < 3 bn → NO TRC (visa ≤ 12 months). "
                        "Calibrate the capital to the targeted residence horizon. Carried out via "
                        "a lawyer, to assign on the dossier."
                    ),
                    "es": (
                        'Ver el recorrido "Sociedad (LLC FDI)" para el detalle (IRC→ERC, OMC, '
                        "DICA). 🟠 CAPITAL ↔ TRC: DT1 ≥ 100 mil millones VND (~3,9 M USD) → TRC 10 "
                        "años (+ vía PRC) · DT2 50-100 mil millones → 5 años · DT3 3-50 mil "
                        "millones (~120k USD) → 3 años (mínimo práctico para una TRC) · DT4 < 3 "
                        "mil millones → SIN TRC (visa ≤ 12 meses). Calibrar el capital al "
                        "horizonte de residencia buscado. Realizado vía un abogado, a asignar en "
                        "el expediente."
                    ),
                },
            ),
            (
                {
                    "en": "Investor visa application (DTx category)",
                    "es": "Solicitud de visa de inversionista (categoría DTx)",
                },
                {
                    "en": "Investor visa application according to the calibrated DTx category.",
                    "es": "Solicitud de visa de inversionista según la categoría DTx calibrada.",
                },
            ),
            (
                {
                    "en": "Temporary residence card (TRC)",
                    "es": "Tarjeta de residencia temporal (TRC)",
                },
                {
                    "en": (
                        "Duration according to the DTx category. DT4 does not give a TRC. "
                        "Biometrics required."
                    ),
                    "es": "Duración según la categoría DTx. DT4 no da TRC. Biometría requerida.",
                },
            ),
        ],
    },
    VN_TT_NAME: {
        "name": {
            "en": "Vietnam — Family TRC (TT, spouse of a Vietnamese national)",
            "es": "Vietnam — TRC familiar (TT, cónyuge de un·a vietnamita)",
        },
        "steps": [
            (
                {
                    "en": "Gather the marriage & sponsor documents",
                    "es": "Reunir los documentos de matrimonio y del sponsor",
                },
                {
                    "en": (
                        "Gathering of the marriage documents and the Vietnamese sponsor's identity."
                    ),
                    "es": (
                        "Reunión de los documentos de matrimonio y de la identidad del sponsor "
                        "vietnamita."
                    ),
                },
            ),
            (
                {
                    "en": "TT visa application (sponsored by the spouse)",
                    "es": "Solicitud de visa TT (patrocinada por el cónyuge)",
                },
                {
                    "en": "TT visa application sponsored by the Vietnamese spouse.",
                    "es": "Solicitud de visa TT patrocinada por el cónyuge vietnamita.",
                },
            ),
            (
                {
                    "en": "Family temporary residence card (TRC)",
                    "es": "Tarjeta de residencia temporal familiar (TRC)",
                },
                {
                    "en": (
                        "TRC up to 3 years. PRC accessible after 3 years of continuous TRC "
                        "(Vietnamese family sponsor). Does not in itself grant the right to work "
                        "(separate work permit required for paid employment). Biometrics "
                        "required."
                    ),
                    "es": (
                        "TRC hasta 3 años. PRC accesible tras 3 años de TRC continua (sponsor "
                        "familiar vietnamita). No da por sí misma el derecho a trabajar (work "
                        "permit aparte requerido para empleo asalariado). Biometría requerida."
                    ),
                },
            ),
        ],
    },
    VN_RO_NAME: {
        "name": {
            "en": "Vietnam — Representative Office",
            "es": "Vietnam — Representative Office (oficina de representación)",
        },
        "steps": [
            (
                {
                    "en": "Check the parent company's eligibility",
                    "es": "Verificar la elegibilidad de la casa matriz",
                },
                {
                    "en": (
                        "Parent company existing for ≥ 1 year (Decree 07/2016). The RO CANNOT "
                        "generate direct commercial revenue — liaison/representation function "
                        "only."
                    ),
                    "es": (
                        "Casa matriz existente desde ≥ 1 año (Decreto 07/2016). La RO NO puede "
                        "generar ingresos comerciales directos — función de enlace/representación "
                        "únicamente."
                    ),
                },
            ),
            (
                {"en": "RO licence application", "es": "Solicitud de licencia de RO"},
                {
                    "en": "Filing of the Representative Office licence application.",
                    "es": "Presentación de la solicitud de licencia de Representative Office.",
                },
            ),
            (
                {
                    "en": "Licence issuance & registration",
                    "es": "Emisión de la licencia y registro",
                },
                {
                    "en": (
                        "5-year renewable licence. The foreign chief of office obtains a "
                        "visa/permit tied to the RO. To generate revenue, switch to an FDI LLC "
                        "(dedicated journey)."
                    ),
                    "es": (
                        "Licencia de 5 años renovable. El jefe de oficina extranjero obtiene un "
                        "visa/permiso ligado a la RO. Para generar ingresos, cambiar a una LLC "
                        "FDI (recorrido dedicado)."
                    ),
                },
            ),
        ],
    },
    US_E2_NAME: {
        "name": {
            "en": "United States — E-2 Visa (treaty investor)",
            "es": "Estados Unidos — Visa E-2 (inversionista de tratado)",
        },
        "steps": [
            (
                {
                    "en": "Check treaty eligibility & structure the investment",
                    "es": "Verificar la elegibilidad de tratado y estructurar la inversión",
                },
                {
                    "en": (
                        "🟠 France is an E-2 treaty country. NO fixed legal threshold: the "
                        'investment must be "substantial" relative to the cost of the business '
                        "and NON-MARGINAL (the ~100k USD often cited is observed, NOT a rule). No "
                        "passive/speculative real-estate investment. US lawyer indispensable "
                        "(fees ~8-20k+ USD)."
                    ),
                    "es": (
                        "🟠 Francia es un país de tratado E-2. SIN umbral legal fijo: la inversión "
                        'debe ser "sustancial" en relación con el coste del negocio y NO MARGINAL '
                        "(los ~100k USD a menudo citados son observados, NO una regla). Sin "
                        "inversión pasiva/inmobiliaria especulativa. Abogado US indispensable "
                        "(honorarios ~8-20k+ USD)."
                    ),
                },
            ),
            (
                {
                    "en": "Create/acquire the US business & commit the funds",
                    "es": "Crear/adquirir el negocio US y comprometer los fondos",
                },
                {
                    "en": (
                        'See the "Company (LLC / C-Corp)" journey. The C-Corp facilitates '
                        'demonstrating a real business. Funds irrevocably committed ("at risk").'
                    ),
                    "es": (
                        'Ver el recorrido "Sociedad (LLC / C-Corp)". La C-Corp facilita demostrar '
                        'un negocio real. Fondos comprometidos irrevocablemente ("at risk").'
                    ),
                },
            ),
            (
                {
                    "en": "File the application (US consulate, DS-160 + DS-156E)",
                    "es": "Presentar la solicitud (consulado US, DS-160 + DS-156E)",
                },
                {
                    "en": "Application filed at the US consulate (DS-160 + DS-156E forms).",
                    "es": "Solicitud presentada en el consulado US (formularios DS-160 + DS-156E).",
                },
            ),
            (
                {"en": "Consular interview & issuance", "es": "Entrevista consular y emisión"},
                {
                    "en": (
                        "🔴 DISCRETIONARY decision (substantiality/marginality scrutinized), never "
                        "a firm outcome or timeline. Renewable as long as the business is active. "
                        "Dual intent delicate (not formally admitted). Presence required."
                    ),
                    "es": (
                        "🔴 Decisión DISCRECIONAL (sustancialidad/marginalidad examinadas), nunca "
                        "un resultado ni un plazo firme. Renovable mientras el negocio esté "
                        "activo. Dual intent delicado (no admitido formalmente). Presencia "
                        "requerida."
                    ),
                },
            ),
        ],
    },
    US_L1_NAME: {
        "name": {
            "en": "United States — L-1 Visa (intra-company transfer)",
            "es": "Estados Unidos — Visa L-1 (transferencia intraempresa)",
        },
        "steps": [
            (
                {
                    "en": "Check the inter-entity relationship & tenure",
                    "es": "Verificar la relación inter-entidades y la antigüedad",
                },
                {
                    "en": (
                        "1 year of continuous employment abroad in the related entity within the "
                        "last 3 years. Qualifying relationship (parent/subsidiary/affiliate). "
                        "L-1A executive (≤ 7 years) / L-1B specialized knowledge (≤ 5 years, more "
                        "scrutinized)."
                    ),
                    "es": (
                        "1 año de empleo continuo en el extranjero en la entidad vinculada en los "
                        "últimos 3 años. Relación calificante (matriz/filial/afiliada). L-1A "
                        "directivo (≤ 7 años) / L-1B conocimiento especializado (≤ 5 años, más "
                        "examinado)."
                    ),
                },
            ),
            (
                {
                    "en": "I-129 petition to USCIS (carried by the US employer)",
                    "es": "Petición I-129 ante USCIS (llevada por el empleador US)",
                },
                {
                    "en": (
                        '"New office L-1" possible to open a US entity (reinforced conditions, '
                        "review at 1 year)."
                    ),
                    "es": (
                        '"New office L-1" posible para abrir una entidad US (condiciones '
                        "reforzadas, revisión al año)."
                    ),
                },
            ),
            (
                {"en": "Visa at the consulate & entry", "es": "Visa en el consulado y entrada"},
                {
                    "en": (
                        "🔴 Discretionary decision (L-1B particularly scrutinized). Possible route "
                        "toward the EB-1C green card (multinational executive)."
                    ),
                    "es": (
                        "🔴 Decisión discrecional (L-1B particularmente examinado). Vía posible "
                        "hacia la green card EB-1C (directivo multinacional)."
                    ),
                },
            ),
        ],
    },
    US_O1_NAME: {
        "name": {
            "en": "United States — O-1 Visa (extraordinary ability)",
            "es": "Estados Unidos — Visa O-1 (capacidades extraordinarias)",
        },
        "steps": [
            (
                {"en": "Assess the evidence file", "es": "Evaluar el expediente de pruebas"},
                {
                    "en": (
                        "🟠 A major recognized award OR at least 3 regulatory criteria "
                        "(publications, press, critical role, high remuneration, peer judgment…). "
                        "Quality of evidence decisive. US sponsor or agent required."
                    ),
                    "es": (
                        "🟠 Un premio mayor reconocido O al menos 3 criterios reglamentarios "
                        "(publicaciones, prensa, rol crítico, remuneración elevada, juicio de "
                        "pares…). Calidad de las pruebas decisiva. Sponsor US o agente requerido."
                    ),
                },
            ),
            (
                {
                    "en": "I-129 petition + peer-group consultation",
                    "es": "Petición I-129 + consulta de un peer group",
                },
                {
                    "en": "I-129 petition accompanied by the advisory opinion of a peer group.",
                    "es": "Petición I-129 acompañada del dictamen consultivo de un peer group.",
                },
            ),
            (
                {"en": "Visa at the consulate & entry", "es": "Visa en el consulado y entrada"},
                {
                    "en": (
                        "🔴 Discretionary decision (quality of evidence). Up to 3 years, "
                        "renewable. Dual intent delicate. Profile often transposable to EB-1A "
                        "(green card, self-petition)."
                    ),
                    "es": (
                        "🔴 Decisión discrecional (calidad de las pruebas). Hasta 3 años, "
                        "renovable. Dual intent delicado. Perfil a menudo transponible a EB-1A "
                        "(green card, autopetición)."
                    ),
                },
            ),
        ],
    },
    US_H1B_NAME: {
        "name": {
            "en": "United States — H-1B Visa (specialty occupation)",
            "es": "Estados Unidos — Visa H-1B (specialty occupation)",
        },
        "steps": [
            (
                {
                    "en": "Lottery registration (employer)",
                    "es": "Registro en la lotería (empleador)",
                },
                {
                    "en": (
                        "🔴 Annual quota 65 000 + 20 000 (US master's) → LOTTERY: selection NOT "
                        "guaranteed. ⚠️ Proclamation of 19/09/2025 imposing a 100 000 USD fee — "
                        "scope/exemptions/judicial status UNCERTAIN, the n°1 point to verify. "
                        "Registration fee to confirm (FY2027)."
                    ),
                    "es": (
                        "🔴 Cupo anual 65 000 + 20 000 (máster US) → LOTERÍA: selección NO "
                        "garantizada. ⚠️ Proclamación del 19/09/2025 que impone una tasa de 100 "
                        "000 USD — alcance/exenciones/estatus judicial INCIERTOS, el punto n.º 1 "
                        "a verificar. Tasa de registro por confirmar (FY2027)."
                    ),
                },
            ),
            (
                {
                    "en": "(If selected) Labor Condition Application (DOL) + I-129 petition",
                    "es": "(Si es seleccionado) Labor Condition Application (DOL) + petición I-129",
                },
                {
                    "en": "After selection: LCA at the DOL then I-129 petition.",
                    "es": "Tras la selección: LCA ante el DOL y luego petición I-129.",
                },
            ),
            (
                {"en": "Visa at the consulate & entry", "es": "Visa en el consulado y entrada"},
                {
                    "en": (
                        "3 years + 3 years. Tied to the employer. Possible route toward a green "
                        "card (PERM → EB-2/EB-3)."
                    ),
                    "es": (
                        "3 años + 3 años. Ligado al empleador. Vía posible hacia una green card "
                        "(PERM → EB-2/EB-3)."
                    ),
                },
            ),
        ],
    },
    US_EB5_NAME: {
        "name": {
            "en": "United States — EB-5 green card (immigrant investor)",
            "es": "Estados Unidos — Green card EB-5 (inversionista inmigrante)",
        },
        "steps": [
            (
                {
                    "en": "Structure the investment & verify the source of funds",
                    "es": "Estructurar la inversión y verificar el origen de los fondos",
                },
                {
                    "en": (
                        "🟢 800 000 USD in a targeted area (TEA) / 1 050 000 USD outside TEA + "
                        "creation of 10 full-time jobs. Reindexation planned for 1/1/2027. Lawful "
                        "traceability of funds required (severely scrutinized). Direct investment "
                        "OR via a Regional Center. Lawyer fees ~15-50k+ USD."
                    ),
                    "es": (
                        "🟢 800 000 USD en una zona objetivo (TEA) / 1 050 000 USD fuera de TEA + "
                        "creación de 10 empleos a tiempo completo. Reindexación prevista para el "
                        "1/1/2027. Trazabilidad lícita de los fondos exigida (severamente "
                        "examinada). Inversión directa O vía un Regional Center. Honorarios de "
                        "abogado ~15-50k+ USD."
                    ),
                },
            ),
            (
                {"en": "I-526E petition (USCIS)", "es": "Petición I-526E (USCIS)"},
                {
                    "en": "Filing of the I-526E petition with USCIS.",
                    "es": "Presentación de la petición I-526E ante USCIS.",
                },
            ),
            (
                {
                    "en": "Conditional green card (2 years) — consulate or adjustment of status",
                    "es": "Green card condicional (2 años) — consulado o ajuste de estatus",
                },
                {
                    "en": (
                        "Conditional 2-year green card, by consular processing or adjustment of "
                        "status."
                    ),
                    "es": "Green card condicional de 2 años, por vía consular o ajuste de estatus.",
                },
            ),
            (
                {"en": "Removal of conditions (I-829)", "es": "Levantamiento de condición (I-829)"},
                {
                    "en": (
                        "After ~2 years, prove the maintenance of the investment and the 10 jobs "
                        "→ permanent green card. USCIS timelines long and variable."
                    ),
                    "es": (
                        "Tras ~2 años, probar el mantenimiento de la inversión y de los 10 "
                        "empleos → green card permanente. Plazos USCIS largos y variables."
                    ),
                },
            ),
        ],
    },
    US_NIW_NAME: {
        "name": {
            "en": "United States — EB-2 NIW / EB-1A green card (by merit)",
            "es": "Estados Unidos — Green card EB-2 NIW / EB-1A (por mérito)",
        },
        "steps": [
            (
                {"en": "Qualify the route", "es": "Calificar la vía"},
                {
                    "en": (
                        "🟠 EB-1A = extraordinary ability (a major award OR ≥ 3 of 10 criteria). "
                        "EB-2 NIW = advanced degree/exceptional ability + the 3 Dhanasar prongs "
                        "(merit & national importance, well positioned to advance, benefit of "
                        "waiving the job offer). Both allow self-petition."
                    ),
                    "es": (
                        "🟠 EB-1A = capacidades extraordinarias (un premio mayor O ≥ 3 de 10 "
                        "criterios). EB-2 NIW = título avanzado/aptitud excepcional + los 3 "
                        "prongs Dhanasar (mérito e importancia nacional, buena posición para "
                        "avanzar, beneficio de renunciar a la oferta de empleo). Ambas permiten "
                        "la autopetición."
                    ),
                },
            ),
            (
                {"en": "Build the evidence file", "es": "Preparar el expediente de pruebas"},
                {
                    "en": "Assembly of the evidence file of excellence/national interest.",
                    "es": "Preparación del expediente de pruebas de excelencia/interés nacional.",
                },
            ),
            (
                {"en": "I-140 petition (USCIS)", "es": "Petición I-140 (USCIS)"},
                {
                    "en": "Filing of the I-140 petition with USCIS.",
                    "es": "Presentación de la petición I-140 ante USCIS.",
                },
            ),
            (
                {
                    "en": "Green card (visa bulletin / adjustment of status)",
                    "es": "Green card (visa bulletin / ajuste de estatus)",
                },
                {
                    "en": (
                        "🔴 Discretionary decision (quality of evidence). Timelines and backlogs "
                        "according to the visa bulletin."
                    ),
                    "es": (
                        "🔴 Decisión discrecional (calidad de las pruebas). Plazos y atrasos según "
                        "el visa bulletin."
                    ),
                },
            ),
        ],
    },
    US_CO_NAME: {
        "name": {
            "en": "United States — Company formation (LLC / C-Corp)",
            "es": "Estados Unidos — Creación de empresa (LLC / C-Corp)",
        },
        "steps": [
            (
                {
                    "en": "Choose the structure & the State",
                    "es": "Elegir la estructura y el Estado",
                },
                {
                    "en": (
                        "LLC (pass-through, simple) → a light operating structure without setup "
                        "(US invoicing, e-commerce, consulting, holding). C-Corp (21 % federal "
                        "tax, double taxation) → VC fundraising OR support for an E-2/L-1 visa "
                        "with setup (Delaware standard). ⚠️ S-Corp CLOSED to non-residents → the "
                        "real choice = LLC vs C-Corp. State: Delaware (VC) / Wyoming (low costs, "
                        "no State tax) / State of actual activity. ⚠️ registering in DE/WY does "
                        "NOT exempt from registering where the company operates (nexus)."
                    ),
                    "es": (
                        "LLC (pass-through, simple) → una estructura operativa ligera sin "
                        "instalación (facturación US, e-commerce, consultoría, holding). C-Corp "
                        "(impuesto federal 21 %, doble imposición) → levantamiento de fondos VC O "
                        "soporte de un visa E-2/L-1 con instalación (estándar Delaware). ⚠️ "
                        "S-Corp CERRADA a los no residentes → la elección real = LLC vs C-Corp. "
                        "Estado: Delaware (VC) / Wyoming (costes bajos, sin impuesto estatal) / "
                        "Estado de actividad real. ⚠️ inscribirse en DE/WY NO exime de "
                        "registrarse donde la empresa opera (nexus)."
                    ),
                },
            ),
            (
                {"en": "Formation & registered agent", "es": "Formación y registered agent"},
                {
                    "en": "Formation of the entity and designation of a registered agent.",
                    "es": "Formación de la entidad y designación de un registered agent.",
                },
            ),
            (
                {"en": "EIN, ITIN & bank account", "es": "EIN, ITIN y cuenta bancaria"},
                {
                    "en": (
                        "EIN (Form SS-4; several weeks without an SSN, fax/mail route), ITIN "
                        "(W-7) often needed. Account via fintech (Mercury/Wise/Relay) if no "
                        "travel. State tax registrations."
                    ),
                    "es": (
                        "EIN (Form SS-4; varias semanas sin SSN, vía fax/correo), ITIN (W-7) a "
                        "menudo necesario. Cuenta vía fintech (Mercury/Wise/Relay) si no hay "
                        "desplazamiento. Registros fiscales estatales."
                    ),
                },
            ),
            (
                {
                    "en": "Foreign-ownership compliance (from year 1)",
                    "es": "Cumplimiento de la propiedad extranjera (desde el año 1)",
                },
                {
                    "en": (
                        "🟢 Single-member LLC owned by a foreigner → Form 5472 + pro forma 1120 "
                        "(deadline April 15, PENALTY 25 000 USD). C-Corp → 1120; 5472 if a "
                        "related foreign shareholder ≥ 25 %; dividend withholding 30 % → 15 % "
                        "(US-France treaty). 🔴 BOI/Corporate Transparency Act: FinCEN rule of "
                        "March 2025 refocused on foreign entities — scope to verify on "
                        "fincen.gov/boi."
                    ),
                    "es": (
                        "🟢 Single-member LLC poseída por un extranjero → Form 5472 + 1120 pro "
                        "forma (plazo 15 de abril, MULTA 25 000 USD). C-Corp → 1120; 5472 si un "
                        "accionista extranjero vinculado ≥ 25 %; retención de dividendos 30 % → "
                        "15 % (convenio US-Francia). 🔴 BOI/Corporate Transparency Act: regla "
                        "FinCEN de marzo 2025 reenfocada en las entidades extranjeras — alcance a "
                        "verificar en fincen.gov/boi."
                    ),
                },
            ),
        ],
    },
    CH_BNA_NAME: {
        "name": {
            "en": "Switzerland — Permit B non-active (rentier/retiree EU/EFTA)",
            "es": "Suiza — Permiso B no activo (rentista/jubilado UE/AELC)",
        },
        "steps": [
            (
                {
                    "en": "Gather proof of means & insurance",
                    "es": "Reunir las pruebas de medios y de seguro",
                },
                {
                    "en": (
                        "🟠 Sufficient financial means (threshold indexed to the LPC supplementary "
                        "benefits, to confirm by canton) + health insurance covering Switzerland. "
                        "No age requirement for an EU/EFTA national."
                    ),
                    "es": (
                        "🟠 Medios financieros suficientes (umbral indexado a las prestaciones "
                        "complementarias LPC, a confirmar por cantón) + seguro de salud que cubra "
                        "Suiza. Sin requisito de edad para un nacional UE/AELC."
                    ),
                },
            ),
            (
                {
                    "en": "Arrival declaration at the commune (within 14 days)",
                    "es": "Declaración de llegada en el municipio (en 14 días)",
                },
                {
                    "en": "Arrival declaration at the commune within 14 days. Presence required.",
                    "es": (
                        "Declaración de llegada en el municipio en un plazo de 14 días. Presencia "
                        "requerida."
                    ),
                },
            ),
            (
                {"en": "Issuance of the B permit", "es": "Emisión del permiso B"},
                {
                    "en": (
                        "B permit (5 years). 🟠 Lump-sum taxation available in most cantons (a "
                        "distinct TAX regime, to negotiate separately with the cantonal tax "
                        "office — not a residence right). The canton is decisive (taxation)."
                    ),
                    "es": (
                        "Permiso B (5 años). 🟠 Tributación a tanto alzado disponible en la "
                        "mayoría de los cantones (un régimen FISCAL distinto, a negociar por "
                        "separado con la oficina fiscal cantonal — no un derecho de residencia). "
                        "El cantón es determinante (fiscalidad)."
                    ),
                },
            ),
        ],
    },
    CH_EMP_NAME: {
        "name": {
            "en": "Switzerland — Permit L/B employee (EU/EFTA)",
            "es": "Suiza — Permiso L/B asalariado (UE/AELC)",
        },
        "steps": [
            (
                {"en": "Signed employment contract", "es": "Contrato de trabajo firmado"},
                {
                    "en": (
                        "Permit type by contract duration: < 3 months = simple notification · "
                        "3-12 months = L permit · ≥ 12 months = B permit (5 years). No quota or "
                        "labor-market test for an EU/EFTA national."
                    ),
                    "es": (
                        "Tipo de título según la duración del contrato: < 3 meses = notificación "
                        "simple · 3-12 meses = permiso L · ≥ 12 meses = permiso B (5 años). Sin "
                        "cupo ni prueba del mercado para un nacional UE/AELC."
                    ),
                },
            ),
            (
                {
                    "en": "Notification/application to the commune & canton",
                    "es": "Notificación/solicitud al municipio y al cantón",
                },
                {
                    "en": "Notification/application to the commune and the canton.",
                    "es": "Notificación/solicitud ante el municipio y el cantón.",
                },
            ),
            (
                {"en": "Issuance of the L or B permit", "es": "Emisión del permiso L o B"},
                {
                    "en": (
                        "C permit (settlement) at 5 years for EU/EFTA nationals (reciprocity). "
                        "The canton determines personal taxation."
                    ),
                    "es": (
                        "Permiso C (establecimiento) a los 5 años para los nacionales UE/AELC "
                        "(reciprocidad). El cantón determina la fiscalidad personal."
                    ),
                },
            ),
        ],
    },
    CH_IND_NAME: {
        "name": {
            "en": "Switzerland — Self-employed / entrepreneur (EU/EFTA)",
            "es": "Suiza — Autónomo / emprendedor (UE/AELC)",
        },
        "steps": [
            (
                {
                    "en": "Demonstrate a real and viable self-employed activity",
                    "es": "Demostrar una actividad independiente real y viable",
                },
                {
                    "en": (
                        "Business plan, forecast accounts, premises/clients — the activity must "
                        "be effective (not fictitious). AVS affiliation as self-employed."
                    ),
                    "es": (
                        "Business plan, contabilidad previsional, locales/clientes — la actividad "
                        "debe ser efectiva (no ficticia). Afiliación AVS como autónomo."
                    ),
                },
            ),
            (
                {
                    "en": "Notification to the commune & B permit application",
                    "es": "Notificación al municipio y solicitud de permiso B",
                },
                {
                    "en": "Notification to the commune and B permit application (self-employed).",
                    "es": "Notificación al municipio y solicitud de permiso B (autónomo).",
                },
            ),
            (
                {
                    "en": "Issuance of the B permit (self-employed)",
                    "es": "Emisión del permiso B (autónomo)",
                },
                {
                    "en": (
                        "Possibility to set up a Sàrl/SA in parallel (see the company journey). "
                        "The canton determines the tax burden."
                    ),
                    "es": (
                        "Posibilidad de constituir una Sàrl/SA en paralelo (ver el recorrido de "
                        "empresa). El cantón determina la carga fiscal."
                    ),
                },
            ),
        ],
    },
    CH_RET_NAME: {
        "name": {
            "en": "Switzerland — Non-EU rentier (55+, art. 28 LEI)",
            "es": "Suiza — Rentista no-UE (55 y +, art. 28 LEI)",
        },
        "steps": [
            (
                {
                    "en": "Assess eligibility & choose a welcoming canton",
                    "es": "Evaluar la elegibilidad y elegir un cantón acogedor",
                },
                {
                    "en": (
                        "🔴 Art. 28 LEI / art. 25 OASA: ≥ 55 years + PARTICULAR personal ties with "
                        "Switzerland + no gainful activity + sufficient means + effective "
                        "transfer of the center of life. VERY discretionary: some cantons "
                        "welcoming, others restrictive — the choice of canton is decisive. A "
                        "non-EU rentier UNDER 55 has no clear route."
                    ),
                    "es": (
                        "🔴 Art. 28 LEI / art. 25 OASA: ≥ 55 años + lazos personales PARTICULARES "
                        "con Suiza + ninguna actividad lucrativa + medios suficientes + "
                        "transferencia efectiva del centro de vida. MUY discrecional: algunos "
                        "cantones acogedores, otros restrictivos — la elección del cantón es "
                        "determinante. Un rentista no-UE de MENOS de 55 años no tiene vía clara."
                    ),
                },
            ),
            (
                {
                    "en": "File the application with the cantonal migration authority",
                    "es": "Presentar la solicitud ante la autoridad cantonal de migraciones",
                },
                {
                    "en": "Filing of the application with the cantonal migration authority.",
                    "es": "Presentación de la solicitud ante la autoridad cantonal de migraciones.",
                },
            ),
            (
                {
                    "en": "Grant of the B permit (no activity) & lump-sum taxation",
                    "es": "Otorgamiento del permiso B (sin actividad) y tributación a tanto alzado",
                },
                {
                    "en": (
                        "🟠 Target audience for lump-sum taxation (a distinct tax regime, to "
                        "negotiate via a cantonal ruling BEFORE settling — not a permit in "
                        "itself)."
                    ),
                    "es": (
                        "🟠 Público objetivo de la tributación a tanto alzado (un régimen fiscal "
                        "distinto, a negociar mediante un ruling cantonal ANTES de instalarse — "
                        "no un título en sí mismo)."
                    ),
                },
            ),
        ],
    },
    CH_TCN_NAME: {
        "name": {
            "en": "Switzerland — Non-EU employee (art. 18-23 LEI)",
            "es": "Suiza — Asalariado no-UE (art. 18-23 LEI)",
        },
        "steps": [
            (
                {
                    "en": "Check the conditions (the bottleneck)",
                    "es": "Verificar las condiciones (el cuello de botella)",
                },
                {
                    "en": (
                        "🔴 Cumulative conditions: economic interest + "
                        "EXECUTIVE/SPECIALIST/QUALIFIED profile + customary salary and conditions "
                        "+ PRIORITY of the domestic/EU-EFTA market (the employer must prove the "
                        "absence of a Swiss/EU candidate) + annual QUOTA (blocking risk if the "
                        "quota is exhausted). Without an employer and without an "
                        "executive/specialist profile, this route is effectively CLOSED."
                    ),
                    "es": (
                        "🔴 Condiciones acumulativas: interés económico + perfil "
                        "DIRECTIVO/ESPECIALISTA/CUALIFICADO + salario y condiciones usuales + "
                        "PRIORIDAD del mercado nacional/UE-AELC (el empleador debe probar la "
                        "ausencia de un candidato suizo/UE) + CUPO anual (riesgo de bloqueo si el "
                        "cupo se agota). Sin empleador y sin perfil directivo/especialista, esta "
                        "vía está de hecho CERRADA."
                    ),
                },
            ),
            (
                {
                    "en": "The employer files the application (cantonal authority + SEM)",
                    "es": "El empleador presenta la solicitud (autoridad cantonal + SEM)",
                },
                {
                    "en": (
                        "Application carried by the employer to the cantonal authority and the SEM."
                    ),
                    "es": "Solicitud llevada por el empleador ante la autoridad cantonal y el SEM.",
                },
            ),
            (
                {
                    "en": "Visa D & L/B permit (charged to the quota)",
                    "es": "Visa D y permiso L/B (imputado al cupo)",
                },
                {
                    "en": "🟠 Permit charged to the annual quota for third-country nationals.",
                    "es": "🟠 Permiso imputado al cupo anual de los nacionales de terceros países.",
                },
            ),
        ],
    },
    CH_CO_NAME: {
        "name": {
            "en": "Switzerland — Company formation (Sàrl / SA)",
            "es": "Suiza — Creación de empresa (Sàrl / SA)",
        },
        "steps": [
            (
                {
                    "en": "Decide resident director, canton & structure",
                    "es": "Decidir directivo residente, cantón y estructura",
                },
                {
                    "en": (
                        "⚠️ RESIDENT DIRECTOR MANDATORY: at least one person domiciled in "
                        "Switzerland with signing authority (art. 814 para. 3 / 718 para. 4 CO) — "
                        "local hire, fiduciary administrator, or the founder settling. Without "
                        "them, no company. CANTON = tax lever n°1: profit tax ~11.5 % "
                        "(Zug/Nidwalden) to ~21 % (Bern); Geneva ~14 % (NO LONGER a high-tax "
                        "canton). Structure: Sàrl (capital 20 000 CHF paid up, registered "
                        "partners) / SA (100 000 CHF subscribed, min 50 000 paid up, unregistered "
                        "shareholders)."
                    ),
                    "es": (
                        "⚠️ DIRECTIVO RESIDENTE OBLIGATORIO: al menos una persona domiciliada en "
                        "Suiza con poder de firma (art. 814 ap. 3 / 718 ap. 4 CO) — contratación "
                        "local, administrador fiduciario, o instalación del fundador. Sin él, no "
                        "hay empresa. CANTÓN = palanca fiscal n°1: impuesto sobre beneficios "
                        "~11,5 % (Zug/Nidwalden) a ~21 % (Berna); Ginebra ~14 % (YA NO es un "
                        "cantón de alta imposición). Estructura: Sàrl (capital 20 000 CHF "
                        "desembolsado, socios inscritos) / SA (100 000 CHF suscrito, mín. 50 000 "
                        "desembolsado, accionistas no inscritos)."
                    ),
                },
            ),
            (
                {
                    "en": "Statutes by notarial deed & capital paid up",
                    "es": "Estatutos por acta notarial y desembolso del capital",
                },
                {
                    "en": (
                        "Authentic deed mandatory + deposit of the capital into an escrow account "
                        "(bank attestation)."
                    ),
                    "es": (
                        "Acta auténtica obligatoria + depósito del capital en una cuenta de "
                        "consignación (certificación bancaria)."
                    ),
                },
            ),
            (
                {
                    "en": "Registration with the commercial register (Zefix)",
                    "es": "Inscripción en el registro mercantil (Zefix)",
                },
                {
                    "en": "Registration of the company with the commercial register (Zefix).",
                    "es": "Inscripción de la empresa en el registro mercantil (Zefix).",
                },
            ),
            (
                {"en": "VAT & social insurance", "es": "IVA y seguros sociales"},
                {
                    "en": (
                        "🟠 IFD 8.5 % statutory (~7.83 % effective) + cantonal/communal (see step "
                        "1). VAT 8.1 % if turnover > 100 000 CHF. Withholding tax 35 % on "
                        "dividends (residual rates by treaty). Stamp duty 1 % above 1 M CHF of "
                        "contribution. NOTE lump-sum taxation: a regime for a foreign rentier "
                        "without activity (federal floor 400 000 CHF / 7× rent, cantonal ruling) "
                        "— distinct, not a residence permit; abolished in "
                        "Zurich/Basel/Schaffhausen/Appenzell AR."
                    ),
                    "es": (
                        "🟠 IFD 8,5 % estatutario (~7,83 % efectivo) + cantonal/comunal (ver paso "
                        "1). IVA 8,1 % si la facturación > 100 000 CHF. Impuesto anticipado 35 % "
                        "sobre dividendos (tasas residuales por convenio). Derecho de timbre 1 % "
                        "por encima de 1 M CHF de aporte. NOTA tributación a tanto alzado: "
                        "régimen para un rentista extranjero sin actividad (suelo federal 400 000 "
                        "CHF / 7× alquiler, ruling cantonal) — distinto, no un título de "
                        "residencia; abolido en Zúrich/Basilea/Schaffhausen/Appenzell AR."
                    ),
                },
            ),
        ],
    },
    CA_EE_NAME: {
        "name": {
            "en": "Canada — Express Entry (federal permanent residence)",
            "es": "Canadá — Express Entry (residencia permanente federal)",
        },
        "steps": [
            (
                {
                    "en": "Check eligibility & estimate the CRS",
                    "es": "Verificar la elegibilidad y estimar el CRS",
                },
                {
                    "en": (
                        "🟠 FSW = score 67/100 minimum. CEC = ~1 year of qualified experience in "
                        "Canada. Occupation (TEER level), language (CLB/NCLC), age, diplomas "
                        'score the CRS (max 1200). ⚠️ FRENCH = MAJOR ASSET: "French proficiency" '
                        "draws at much lower CRS thresholds. A PNP nomination adds +600 CRS "
                        "(near-guaranteed invitation). No retiree/investor visa in Canada."
                    ),
                    "es": (
                        "🟠 FSW = puntuación 67/100 mínima. CEC = ~1 año de experiencia "
                        "cualificada en Canadá. Profesión (nivel TEER), idioma (CLB/NCLC), edad, "
                        "diplomas puntúan el CRS (máx. 1200). ⚠️ FRANCÉS = ACTIVO MAYOR: sorteos "
                        'de "competencia en francés" con umbrales CRS mucho más bajos. Una '
                        "nominación PNP añade +600 CRS (invitación casi garantizada). Sin visa de "
                        "jubilado/inversionista en Canadá."
                    ),
                },
            ),
            (
                {
                    "en": "Language tests, diploma equivalence (ECA) & profile in the pool",
                    "es": "Pruebas de idioma, equivalencia de diplomas (ECA) y perfil en el pool",
                },
                {
                    "en": (
                        "Language tests, ECA of diplomas, and creation of the profile in the pool."
                    ),
                    "es": (
                        "Pruebas de idioma, ECA de los diplomas, y creación del perfil en el pool."
                    ),
                },
            ),
            (
                {
                    "en": "Invitation to apply (ITA) & PR application",
                    "es": "Invitación a presentar una solicitud (ITA) y solicitud de RP",
                },
                {
                    "en": (
                        "🟠 PR fees ~950 $ + RPRF 575 $ + biometrics 85 $. CRS thresholds of the "
                        "rounds very volatile (canada.ca/IRCC), to reconfirm."
                    ),
                    "es": (
                        "🟠 Tasas RP ~950 $ + RPRF 575 $ + biometría 85 $. Umbrales CRS de las "
                        "rondas muy volátiles (canada.ca/IRCC), a reconfirmar."
                    ),
                },
            ),
        ],
    },
    CA_PNP_NAME: {
        "name": {
            "en": "Canada — Provincial Nominee Program (PNP)",
            "es": "Canadá — Provincial Nominee Program (PNP)",
        },
        "steps": [
            (
                {
                    "en": "Identify the province & the stream matching the profile",
                    "es": "Identificar la provincia y el componente adaptado al perfil",
                },
                {
                    "en": (
                        "🔴 Each province has its own streams and criteria (often tied to an "
                        "in-demand occupation, a local job offer, or a tie to the province). 2025 "
                        "PNP allocation reduced (~55 000) — stream availability volatile, to "
                        "confirm by province (OINP/BC PNP/AAIP…)."
                    ),
                    "es": (
                        "🔴 Cada provincia tiene sus propios componentes y criterios (a menudo "
                        "ligados a una profesión en demanda, una oferta de empleo local, o un "
                        "vínculo con la provincia). Asignación PNP 2025 reducida (~55 000) — "
                        "disponibilidad de los componentes volátil, a confirmar por provincia "
                        "(OINP/BC PNP/AAIP…)."
                    ),
                },
            ),
            (
                {
                    "en": "Expression of interest / provincial application",
                    "es": "Declaración de interés / candidatura provincial",
                },
                {
                    "en": "Expression of interest or application to the targeted province.",
                    "es": "Declaración de interés o candidatura ante la provincia elegida.",
                },
            ),
            (
                {
                    "en": "Provincial nomination → federal PR application",
                    "es": "Nominación provincial → solicitud de RP federal",
                },
                {
                    "en": (
                        "The nomination adds +600 CRS (via Express Entry, aligned stream) OR "
                        'constitutes a "base" PNP route outside Express Entry, then a PR '
                        "application to IRCC."
                    ),
                    "es": (
                        "La nominación añade +600 CRS (vía Express Entry, componente alineado) O "
                        'constituye una vía PNP "base" fuera de Express Entry, luego una '
                        "solicitud de RP ante IRCC."
                    ),
                },
            ),
        ],
    },
    CA_QC_NAME: {
        "name": {
            "en": "Quebec — PSTQ / Arrima (Quebec selection, then PR)",
            "es": "Quebec — PSTQ / Arrima (selección quebequense, luego RP)",
        },
        "steps": [
            (
                {
                    "en": "Create an Arrima profile (expression of interest)",
                    "es": "Crear un perfil Arrima (declaración de interés)",
                },
                {
                    "en": (
                        "⚠️ Quebec system SEPARATE from Express Entry. PSTQ = Skilled Worker "
                        "Selection Program (distinct streams). 🟠 FRENCH is a major lever "
                        "(thresholds and points). Stream labels and thresholds to confirm "
                        "(Québec.ca/MIFI)."
                    ),
                    "es": (
                        "⚠️ Sistema quebequense SEPARADO de Express Entry. PSTQ = Programa de "
                        "selección de trabajadores cualificados (componentes distintos). 🟠 El "
                        "FRANCÉS es una palanca mayor (umbrales y puntos). Denominaciones de los "
                        "componentes y umbrales a confirmar (Québec.ca/MIFI)."
                    ),
                },
            ),
            (
                {
                    "en": "Quebec invitation & CSQ application (MIFI)",
                    "es": "Invitación de Quebec y solicitud de CSQ (MIFI)",
                },
                {
                    "en": (
                        "🟠 MIFI fees to confirm. The CSQ = Quebec Selection Certificate "
                        "(provincial selection)."
                    ),
                    "es": (
                        "🟠 Tarifas MIFI a confirmar. El CSQ = Certificado de selección de Quebec "
                        "(selección provincial)."
                    ),
                },
            ),
            (
                {
                    "en": "Federal PR application (IRCC) with the CSQ",
                    "es": "Solicitud de RP federal (IRCC) con el CSQ",
                },
                {
                    "en": (
                        "PR is still issued by the federal government, but the SELECTION is from "
                        "Quebec. NOTE: the PEQ (Quebec Experience Program) is an accelerated "
                        "route for graduates/workers already in Quebec."
                    ),
                    "es": (
                        "La RP sigue siendo emitida por el gobierno federal, pero la SELECCIÓN es "
                        "quebequense. NOTA: el PEQ (Programa de la experiencia quebequense) es "
                        "una vía acelerada para diplomados/trabajadores ya en Quebec."
                    ),
                },
            ),
        ],
    },
    CA_WP_NAME: {
        "name": {
            "en": "Canada — Work permit → Canadian experience → PR",
            "es": "Canadá — Permiso de trabajo → experiencia canadiense → RP",
        },
        "steps": [
            (
                {
                    "en": "Obtain the work permit (IMP or LMIA)",
                    "es": "Obtener el permiso de trabajo (IMP o LMIA)",
                },
                {
                    "en": (
                        "Two routes: IMP (LMIA-exempt: intra-company transfer C12, trade "
                        "agreements, young pros/IEC-PVT for eligible French citizens) OR TFWP "
                        "(with an LMIA impact study, heavier). 🟠 The removal of CRS points for a "
                        "job offer (spring 2025) makes the PNP more central than the job offer "
                        "alone."
                    ),
                    "es": (
                        "Dos vías: IMP (exento de LMIA: transferencia intraempresa C12, acuerdos "
                        "comerciales, jóvenes pros/IEC-PVT para los franceses elegibles) O TFWP "
                        "(con estudio de impacto LMIA, más pesado). 🟠 La retirada de los puntos "
                        "CRS por oferta de empleo (primavera 2025) hace el PNP más central que la "
                        "oferta de empleo sola."
                    ),
                },
            ),
            (
                {
                    "en": "Work in Canada & accumulate qualified experience",
                    "es": "Trabajar en Canadá y acumular experiencia cualificada",
                },
                {
                    "en": (
                        "~1 year of qualified experience (TEER 0/1/2/3) opens the CEC (Canadian "
                        "Experience Class)."
                    ),
                    "es": (
                        "~1 año de experiencia cualificada (TEER 0/1/2/3) abre la CEC (Canadian "
                        "Experience Class)."
                    ),
                },
            ),
            (
                {
                    "en": "PR application via Express Entry (CEC)",
                    "es": "Solicitud de RP vía Express Entry (CEC)",
                },
                {
                    "en": (
                        "The CEC is the fastest route to PR for those who already have Canadian "
                        "experience. French = asset (dedicated draws)."
                    ),
                    "es": (
                        "La CEC es la vía más rápida hacia la RP para quien ya tiene experiencia "
                        "canadiense. Francés = activo (sorteos dedicados)."
                    ),
                },
            ),
        ],
    },
    CA_SUV_NAME: {
        "name": {
            "en": "Canada — Start-up Visa (SUV, entrepreneur)",
            "es": "Canadá — Start-up Visa (SUV, emprendedor)",
        },
        "steps": [
            (
                {
                    "en": "Obtain the support of a designated organization",
                    "es": "Obtener el apoyo de una organización designada",
                },
                {
                    "en": (
                        "🟠 Designated organization: venture capital ≥ 200 000 $ / angel investor "
                        "≥ 75 000 $ / incubator (no funds required). Support letter required. ⚠️ "
                        "No investor/golden visa in Canada — this is the project route."
                    ),
                    "es": (
                        "🟠 Organización designada: capital-riesgo ≥ 200 000 $ / inversionista "
                        "ángel ≥ 75 000 $ / incubadora (sin fondos requeridos). Carta de apoyo "
                        "requerida. ⚠️ Sin visa de inversionista/golden en Canadá — esta es la "
                        "vía de proyecto."
                    ),
                },
            ),
            (
                {"en": "Build the SUV file", "es": "Preparar el expediente SUV"},
                {
                    "en": "Assembly of the Start-up Visa file.",
                    "es": "Preparación del expediente Start-up Visa.",
                },
            ),
            (
                {
                    "en": "PR application (and temporary work permit meanwhile)",
                    "es": "Solicitud de RP (y permiso de trabajo temporal entretanto)",
                },
                {
                    "en": (
                        "PR is direct (not conditional). A work permit can be obtained to start "
                        "while the PR is being processed."
                    ),
                    "es": (
                        "La RP es directa (no condicional). Se puede obtener un permiso de "
                        "trabajo para empezar mientras se tramita la RP."
                    ),
                },
            ),
        ],
    },
}


def _merge_i18n(
    blob: dict[str, str], scalar: str | None, extra: dict[str, str] | None
) -> dict[str, str]:
    """Merge EN/ES variants into an i18n blob WITHOUT touching FR. Ensures the
    "fr" key (from the scalar) is present, then adds only NON-EMPTY en/es — an
    absent/empty variant is left out (FR fallback). Returns a NEW dict so
    SQLAlchemy detects the change."""
    out = dict(blob or {})
    if scalar is not None:
        out.setdefault("fr", scalar)
    for lang, value in (extra or {}).items():
        if value:
            out[lang] = value
    return out


def _apply_sample_i18n(
    tpl: JourneyTemplate, db_steps: list[JourneyTemplateStep], name: str
) -> None:
    """Populate the EN/ES i18n blobs of a sample (template name + per-step name
    and content_note) from _SAMPLE_I18N, by position. Idempotent: re-running
    re-asserts the same keys. The scalar FR + "fr" blob key are preserved. A
    sample with no translation entry still gets its "fr" key normalized."""
    tr = _SAMPLE_I18N.get(name, {})
    raw_name = tr.get("name")
    name_tr = raw_name if isinstance(raw_name, dict) else None
    tpl.name_i18n = _merge_i18n(tpl.name_i18n, tpl.name, name_tr)
    raw_steps = tr.get("steps")
    steps_list: list[Any] = raw_steps if isinstance(raw_steps, list) else []
    for i, st in enumerate(db_steps):
        nm, note = steps_list[i] if i < len(steps_list) else ({}, {})
        st.name_i18n = _merge_i18n(st.name_i18n, st.name, nm)
        st.content_note_i18n = _merge_i18n(st.content_note_i18n, st.content_note, note)


async def _reconcile_existing(
    db: AsyncSession, tpl: JourneyTemplate, country: str, steps: list[_Step]
) -> None:
    """An already-seeded sample: refresh country, and BACKFILL the agency doer
    (type=agent) on steps that have no participant yet — for the rows seeded
    before the "agency in general" participant existed. Idempotent: adds only
    what is missing. Samples are read-only for agencies, so this never fights a
    user edit. Steps match the spec by position (stable)."""
    if tpl.country != country:
        tpl.country = country
    db_steps = list(
        (
            await db.execute(
                select(JourneyTemplateStep)
                .where(JourneyTemplateStep.template_id == tpl.id)
                .order_by(JourneyTemplateStep.position)
            )
        ).scalars()
    )
    existing = (
        await db.execute(
            select(JourneyStepParticipant.step_id).where(
                JourneyStepParticipant.step_id.in_([s.id for s in db_steps])
            )
        )
    ).scalars()
    steps_with_participant = set(existing)
    for db_step, (_n, _d, _note, role, _docs) in zip(db_steps, steps, strict=False):
        if role is None and db_step.id not in steps_with_participant:
            _add_participant(db, db_step.id, None)
    # EN/ES variants — added to the i18n blobs (FR untouched). Idempotent.
    _apply_sample_i18n(tpl, db_steps, tpl.name)
    await db.commit()


async def _seed_one(db: AsyncSession, name: str, country: str, steps: list[_Step]) -> None:
    """Idempotent: keyed on (agency_id IS NULL, is_sample, name). If it exists,
    reconcile in place (country + backfill the agency doer); else create it."""
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
        await _reconcile_existing(db, existing, country, steps)
        return

    tpl = JourneyTemplate(
        id=uuid.uuid4(), agency_id=None, is_sample=True, name=name, country=country
    )
    db.add(tpl)
    await db.flush()  # template before its children FK it

    step_ids: list[uuid.UUID] = []
    step_objs: list[JourneyTemplateStep] = []
    for position, (step_name, days, note, _role, _docs) in enumerate(steps):
        sid = uuid.uuid4()
        step_ids.append(sid)
        obj = JourneyTemplateStep(
            id=sid,
            template_id=tpl.id,
            name=step_name,
            position=position,
            estimated_days=days,
            content_note=note,
            default_validated_by_type="agent",  # validé par l'agence
        )
        step_objs.append(obj)
        db.add(obj)
    await db.flush()  # steps before prerequisites / participants / requirements
    # EN/ES variants on creation too (fresh DB / new deploy). FR untouched.
    _apply_sample_i18n(tpl, step_objs, name)

    # Linear AND chain: step i requires step i-1.
    for i in range(1, len(step_ids)):
        db.add(StepPrerequisite(step_id=step_ids[i], prerequisite_step_id=step_ids[i - 1]))

    for i, (_step_name, _days, _note, role, docs) in enumerate(steps):
        _add_participant(db, step_ids[i], role)
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
