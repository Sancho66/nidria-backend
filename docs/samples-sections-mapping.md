**# Samples × sections d'Informations — mapping de référence (phase A)

> Rapport de la phase A (inspection + mapping, 2026-07-05). Sert d'asset de
> référence pour la phase B (seed des sections/champs sur les 77 samples).
> Statut : VALIDÉ par Alexandre le 2026-07-05, avec deux amendements
> intégrés ci-dessous : (1) sample 24 (Dubaï Golden Visa) prend `company` à
> la place de `family_situation` ; (2) la famille Immigration qualifiée
> gagne `contact` (6 sections — seule famille à 6, exception assumée).
> CE FICHIER EST L'ASSET DE RÉFÉRENCE de la phase B.

## 1. Mécanique — constats et verdicts

### Modèle back : prêt à ~90 %
- `journey_section` : `name` + `name_i18n`/`description_i18n` (6 langues OK),
  **mais aucune clé stable**. Verdict : ajouter une colonne additive
  `seed_key` (nullable, String) pour l'ancrage de réconciliation — l'ancrage
  par nom est exclu (exigence du ticket, et leçon de la purge cadratins :
  renommer un asset ancré par nom casse l'idempotence).
- `journey_template_field` : `kind ∈ {base_field, custom_field}` +
  `reference` + `section_id` + `position`, avec UNIQUE
  `(template_id, kind, reference)` = **clé stable naturelle**, la
  réconciliation des champs est gratuite.
- `clone_template` copie déjà sections + champs avec remapping d'ids.

### Catalogue : aujourd'hui FRONT UNIQUEMENT
- Les **11 sections types** vivent dans
  `nidria-frontend/src/lib/sectionTemplates.ts` — clés stables : `identity`,
  `family_situation`, `immigration`, `professional`, `company`, `housing`,
  `tax`, `language`, `education`, `vehicle`, `contact`. Libellés dans l'i18n
  front (fr/en/es seulement — **ru/pt/it à produire**).
- Les **champs** : ~10 natifs (`base_field`, colonnes `case_person`,
  whitelist `COLLECTABLE_BASE_FIELDS`) + ~57 presets catalogue
  (`fieldCatalog.ts` : clé, type, options), soit 67 slots répartis sur les 11
  sections.
- **Point dur** : un champ catalogue = une `custom_field_definition` PAR
  AGENCE. Un sample n'a pas d'agence → les lignes seedées
  `kind=custom_field` référencent des clés sans définition (légal en base,
  pas de FK). **Au clone, il faut matérialiser les définitions manquantes
  dans l'agence cloneuse** (type + options + libellés 6 langues), sinon
  l'agence hérite de références orphelines.

### Verdict source de vérité
Créer l'asset back `src/journeys/field_catalog.py` : les 11 sections types +
les presets de champs (clés stables, types, options, libellés 6 langues). Le
seed des samples ET la matérialisation au clone lisent cet asset. Le front
garde sa copie à court terme (pointeur documenté dans les deux fichiers),
migration ultérieure possible vers un `GET /journeys/field-catalog`. Pas de
duplication aveugle : le back devient LA référence.

### À vérifier en phase B
Le rendu de l'onglet Informations d'un sample AVANT clone (la résolution des
définitions se fait côté agence viewer — les clés catalogue n'y existent pas
encore).

## 2. Règles de familles (proposition)

Base quasi-universelle : `identity` + `immigration` + `contact`. La création
de société pure n'a pas `immigration`. 3 à 5 sections par parcours.

| Famille | Sections |
|---|---|
| Création de société | identity, company, tax, contact |
| Enregistrement UE | identity, immigration, family_situation, housing, contact |
| Nomade numérique | identity, immigration, professional, tax, contact |
| Retraite / rentier / revenus passifs | identity, immigration, family_situation, tax, contact |
| Investisseur corporate | identity, immigration, company, tax, contact |
| Investisseur immobilier | identity, immigration, housing, tax, contact |
| Salarié / permis de travail | identity, immigration, professional, family_situation, contact |
| Indépendant / freelance | identity, immigration, professional, tax, contact |
| Famille / conjoint | identity, immigration, family_situation, housing, contact |
| Immigration qualifiée / par points | identity, immigration, professional, education, language, contact |
| Résidence générale | identity, immigration, family_situation, tax, contact |
| Résidence par société | identity, immigration, company, professional, contact |
| Entrepreneur-visa | identity, immigration, company, education, contact |

## 3. Le tableau complet — 77 samples

| # | Sample | Famille | Sections |
|---|---|---|---|
| 1 | Paraguay : Résidence temporaire + Cédula | Résidence générale | identity, immigration, family_situation, tax, contact |
| 2 | Paraguay : Création de société (RUC) | Création de société | identity, company, tax, contact |
| 3 | Chypre : Enregistrement de résidence UE (Yellow Slip, MEU1) | Enregistrement UE | identity, immigration, family_situation, housing, contact |
| 4 | Paraguay : Résidence permanente (changement de catégorie) | Résidence générale | identity, immigration, family_situation, tax, contact |
| 5 | Chypre : Résidence hors-UE revenus passifs (Pink Slip + Catégorie F) | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 6 | Chypre : Digital Nomad Visa (hors-UE) | Nomade numérique | identity, immigration, professional, tax, contact |
| 7 | Chypre : Création de société (LTD) | Création de société | identity, company, tax, contact |
| 8 | Chypre : Société LTD + permis dirigeant hors-UE (FIC/BFU) | Résidence par société | identity, immigration, company, professional, contact |
| 9 | Panama : Résidence Nations Amies (Friendly Nations) | Résidence générale | identity, immigration, family_situation, tax, contact |
| 10 | Panama : Visa Pensionado (retraité) | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 11 | Panama : Investisseur Qualifié (Golden Visa) | Investisseur corporate | identity, immigration, company, tax, contact |
| 12 | Panama : Visa nomade numérique (Trabajador Remoto) | Nomade numérique | identity, immigration, professional, tax, contact |
| 13 | Panama : Création de société (S.A. / SRL) | Création de société | identity, company, tax, contact |
| 14 | Bulgarie : Enregistrement de résidence UE | Enregistrement UE | identity, immigration, family_situation, housing, contact |
| 15 | Bulgarie : Résidence retraité hors-UE | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 16 | Bulgarie : Visa nomade digital (hors-UE) | Nomade numérique | identity, immigration, professional, tax, contact |
| 17 | Bulgarie : Freelance / profession libérale (hors-UE) | Indépendant / freelance | identity, immigration, professional, tax, contact |
| 18 | Bulgarie : Création de société (EOOD / OOD) | Création de société | identity, company, tax, contact |
| 19 | Hongrie : Enregistrement de séjour UE | Enregistrement UE | identity, immigration, family_situation, housing, contact |
| 20 | Hongrie : White Card (nomade digital, hors-UE) | Nomade numérique | identity, immigration, professional, tax, contact |
| 21 | Hongrie : Guest Investor (golden visa, hors-UE) | Investisseur corporate | identity, immigration, company, tax, contact |
| 22 | Hongrie : Autorisation unique (salarié hors-UE) | Salarié | identity, immigration, professional, family_situation, contact |
| 23 | Hongrie : Création de société (Kft.) | Création de société | identity, company, tax, contact |
| 24 | Dubaï (EAU) : Golden Visa (10 ans) | Investisseur (amendement) | identity, immigration, company, tax, contact |
| 25 | Dubaï (EAU) : Résidence par société free zone | Résidence par société | identity, immigration, company, professional, contact |
| 26 | Dubaï (EAU) : Visa immobilier (2 ans) | Investisseur immobilier | identity, immigration, housing, tax, contact |
| 27 | Dubaï (EAU) : Visa remote work (1 an) | Nomade numérique | identity, immigration, professional, tax, contact |
| 28 | Dubaï (EAU) : Visa retraité (5 ans, 55 ans et +) | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 29 | Dubaï (EAU) : Création de société (free zone / mainland) | Création de société | identity, company, tax, contact |
| 30 | Maurice : Occupation Permit Investor (entrepreneur) | Investisseur corporate | identity, immigration, company, tax, contact |
| 31 | Maurice : Occupation Permit Professional (salarié) | Salarié | identity, immigration, professional, family_situation, contact |
| 32 | Maurice : Occupation Permit Self-Employed (consultant solo) | Indépendant / freelance | identity, immigration, professional, tax, contact |
| 33 | Maurice : Premium Visa (nomade / revenu passif étranger) | Nomade numérique (ambigu) | identity, immigration, professional, tax, contact |
| 34 | Maurice : Résidence par investissement immobilier (≥ 375k USD) | Investisseur immobilier | identity, immigration, housing, tax, contact |
| 35 | Maurice : Création de société (Domestic / GBC / Authorised) | Création de société | identity, company, tax, contact |
| 36 | Thaïlande : Destination Thailand Visa (DTV, nomade) | Nomade numérique | identity, immigration, professional, tax, contact |
| 37 | Thaïlande : Long-Term Resident (LTR, 10 ans) | Résidence générale (ambigu) | identity, immigration, family_situation, tax, contact |
| 38 | Thaïlande : Visa retraité (O-A, 50 ans et +) | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 39 | Thaïlande : Thailand Privilege (carte de séjour payante) | Résidence générale (ambigu) | identity, immigration, family_situation, tax, contact |
| 40 | Thaïlande : Non-B + Work Permit (salarié) | Salarié | identity, immigration, professional, family_situation, contact |
| 41 | Thaïlande : Création de société (FBA : 100 % / BOI / Amity / FBL) | Création de société | identity, company, tax, contact |
| 42 | Indonésie : Remote Worker KITAS (E33G, nomade) | Nomade numérique | identity, immigration, professional, tax, contact |
| 43 | Indonésie : Second Home Visa (rentier) | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 44 | Indonésie : Retirement KITAS (E33F, 55 ans et +) | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 45 | Indonésie : Work KITAS (E23, salarié) | Salarié | identity, immigration, professional, family_situation, contact |
| 46 | Indonésie : Investor KITAS (E28A) + PT PMA | Investisseur corporate (ambigu) | identity, immigration, company, tax, contact |
| 47 | Indonésie : Création de société (PT PMA) | Création de société | identity, company, tax, contact |
| 48 | Philippines : SRRV (résidence par dépôt, via PRA) | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 49 | Philippines : SIRV (visa investisseur, via BOI) | Investisseur corporate | identity, immigration, company, tax, contact |
| 50 | Philippines : Visa 13(a) (conjoint de ressortissant·e philippin·e) | Famille / conjoint | identity, immigration, family_situation, housing, contact |
| 51 | Philippines : Création de société (60/40 / FINL / export / DME) | Création de société | identity, company, tax, contact |
| 52 | Portugal : Enregistrement de résidence UE (CRUE) | Enregistrement UE | identity, immigration, family_situation, housing, contact |
| 53 | Portugal : Visa D7 (revenu passif / retraité, hors-UE) | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 54 | Portugal : Visa D8 (nomade digital, hors-UE) | Nomade numérique | identity, immigration, professional, tax, contact |
| 55 | Portugal : Golden Visa / ARI (investisseur passif, post-2023) | Investisseur immobilier | identity, immigration, housing, tax, contact |
| 56 | Vietnam : Work Permit + TRC (salarié) | Salarié | identity, immigration, professional, family_situation, contact |
| 57 | Vietnam : Investor TRC (DT1-DT4) | Investisseur corporate | identity, immigration, company, tax, contact |
| 58 | Vietnam : TRC familiale (TT, conjoint de Vietnamien·ne) | Famille / conjoint | identity, immigration, family_situation, housing, contact |
| 59 | Vietnam : Representative Office (bureau de représentation) | Création de société (ambigu) | identity, company, tax, contact |
| 60 | États-Unis : Visa E-2 (investisseur de traité) | Investisseur corporate | identity, immigration, company, tax, contact |
| 61 | États-Unis : Visa L-1 (transfert intra-entreprise) | Salarié | identity, immigration, professional, family_situation, contact |
| 62 | États-Unis : Visa O-1 (capacités extraordinaires) | Immigration qualifiée | identity, immigration, professional, education, language, contact |
| 63 | États-Unis : Visa H-1B (specialty occupation) | Salarié | identity, immigration, professional, family_situation, contact |
| 64 | États-Unis : Green card EB-5 (investisseur immigrant) | Investisseur corporate | identity, immigration, company, tax, contact |
| 65 | États-Unis : Green card EB-2 NIW / EB-1A (par le mérite) | Immigration qualifiée | identity, immigration, professional, education, language, contact |
| 66 | États-Unis : Création de société (LLC / C-Corp) | Création de société | identity, company, tax, contact |
| 67 | Suisse : Permis B non-actif (rentier/retraité UE/AELE) | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 68 | Suisse : Permis L/B salarié (UE/AELE) | Salarié | identity, immigration, professional, family_situation, contact |
| 69 | Suisse : Indépendant / entrepreneur (UE/AELE) | Indépendant / freelance (ambigu) | identity, immigration, professional, tax, contact |
| 70 | Suisse : Rentier hors-UE (55 ans et +, art. 28 LEI) | Retraite / rentier | identity, immigration, family_situation, tax, contact |
| 71 | Suisse : Salarié hors-UE (art. 18-23 LEI) | Salarié | identity, immigration, professional, family_situation, contact |
| 72 | Suisse : Création de société (Sàrl / SA) | Création de société | identity, company, tax, contact |
| 73 | Canada : Express Entry (résidence permanente fédérale) | Immigration qualifiée | identity, immigration, professional, education, language, contact |
| 74 | Canada : Provincial Nominee Program (PNP) | Immigration qualifiée | identity, immigration, professional, education, language, contact |
| 75 | Québec : PSTQ / Arrima (sélection québécoise, puis RP) | Immigration qualifiée | identity, immigration, professional, education, language, contact |
| 76 | Canada : Permis de travail → expérience canadienne → RP | Immigration qualifiée | identity, immigration, professional, education, language, contact |
| 77 | Canada : Start-up Visa (SUV, entrepreneur) | Entrepreneur-visa | identity, immigration, company, education, contact |

## 4. Cas ambigus (proposition en gras)

- Maurice Premium Visa : nomade OU rentier → **nomade numérique** (l'usage
  dominant), tax incluse couvre le volet revenu passif.
- Thaïlande LTR : 4 sous-catégories officielles (riche/retraité/pro/nomade) →
  **résidence générale** (le tronc commun), tax incluse.
- Thaïlande Privilege : carte payante, ni travail ni investissement →
  **résidence générale**.
- Dubaï Golden Visa 10 ans : TRANCHÉ par amendement (2026-07-05) →
  identity, immigration, company, tax, contact.
- Indonésie Investor KITAS + PT PMA : investisseur ET société → **investisseur
  corporate** (company couvre le volet PT PMA).
- Vietnam Representative Office : société sans immigration personnelle →
  **création de société** (pas d'immigration).
- Canada Start-up Visa : entrepreneur → famille dédiée **entrepreneur-visa**
  (company + education, pas de tax).
- Suisse Indépendant UE/AELE : frontière freelance/société → **indépendant /
  freelance**.**
