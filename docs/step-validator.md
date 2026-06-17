# « Action validée par » — refonte de `completion_mode` (note de conception)

> V1 backend : modèle + migration + moteur de complétion + endpoints de
> validation client/externe. Les boutons « valider » côté front et le
> renommage des labels (« Responsable par défaut » → « Action à réaliser
> par », « Mode de complétion » → « Action validée par ») = vagues front.

## Le modèle

Deux notions par étape, **symétriques** (réutilisent le même pattern que le
responsable, on n'invente pas de mécanisme) :

- **« Action à réaliser par »** = l'ex-« Responsable par défaut », inchangé
  sur le fond (renommage front seulement). Backend : `default_responsible_*`
  au template, `responsible_*` à l'instance.
- **« Action validée par »** = NOUVEAU, absorbe l'ex-« Mode de complétion ».
  Type au **template**, personne précise au **dossier**.

`StepValidatorType` = `none | expat | agent | external` :

| Valeur produit | type | qui clôt l'étape |
|----------------|------|------------------|
| personne (auto)| `none`     | se clôt seule quand tous les réquisits sont fournis (= ex-`auto`) |
| client         | `expat`    | le principal du dossier clique « valider » |
| agence         | `agent`    | un membre interne (désigné, ou n'importe lequel) clôt (= ex-`agency_validation`) |
| prestataire    | `external` | un prestataire **désigné** (Agent `is_external`) clique « valider » |

> Le validateur « prestataire » est un **Agent `is_external`** dans
> `validated_by_agent_id` (comme le responsable prestataire de la vague
> contenu), **pas** un `external_contact` : un validateur doit pouvoir se
> connecter et cliquer. Une seule colonne FK suffit (interne pour `agent`,
> provider pour `external`, distingués par le type).

## Structure (D1 : instance figée)

- Template `journey_template_step` : `default_validated_by_type` (NOT NULL,
  défaut `agent`) + `default_validated_by_agent_id` (FK agent, optionnel).
- Instance `case_step_progress` : `validated_by_type` (NOT NULL) +
  `validated_by_agent_id` (FK agent). **Copié du template à l'assignation et
  FIGÉ** (`_initial_validator`) — éditer un parcours ne change PAS
  rétroactivement les dossiers en cours (même invariant « droit sur le
  dossier » que le contenu d'étape).

CHECK `validated_by_type_matches_fk` (D2, **plus souple** que le responsable) :
`none`/`expat` ⟹ agent_id NULL ; `agent` ⟹ agent_id **optionnel** (NULL =
l'agence en général) ; `external` ⟹ agent_id **NOT NULL** (provider désigné).

## Migration `b2e9d6a4c8f1` (additive, réversible, zéro perte)

1. colonnes nullable → 2. backfill set-based déterministe depuis
`completion_mode` (`auto→none`, `agency_validation→agent`, agent_id NULL) →
3. SET NOT NULL + `server_default 'agent'` → 4. CHECK.
**`completion_mode` conservé** tout V1 (filet de secours rollback, jamais
touché → downgrade restaure 100 %). DROP de `completion_mode` = vague
ultérieure après validation prod. Tourne au boot (start.sh) ; backfill
idempotent couvrant toutes les lignes (preuve : `test_migration_step_validator`).

## Moteur de complétion

`recompute_active` lit le `validated_by_type` **de l'instance** :
- `none` → auto-DONE quand tous-réquisits-remplis (= ex-`auto`, **inchangé**).
- `agent` → sur pending→met, mail « prêt à valider » au owner ; clôture via le
  PATCH agent existant (= ex-`agency_validation`, **inchangé**).
- `expat`/`external` → **ne s'auto-complètent jamais** ; clôture par l'action
  de validation dédiée (`POST /expat|external/cases/{id}/steps/{pid}/validate`).

Le **PATCH done agent reste toujours autorisé** quel que soit le validateur
(sécurité opérationnelle : l'agence peut toujours débloquer un dossier coincé).

## Endpoints

- `PUT /cases/{id}/steps/{pid}/validator` (agence, `case.edit`) — désigne le
  validateur au dossier (« personne précise au dossier »), symétrique à
  `…/responsible`. `external` exige le provider assigné au dossier.
- `POST /expat/cases/{id}/steps/{pid}/validate` — le client valide une étape
  `expat` de SON dossier (404 si pas le sien, 409 si pas validée par le client).
- `POST /external/cases/{id}/steps/{pid}/validate` (`external.step.validate`) —
  le provider **désigné** valide (`validated_by_agent_id == external.id`, sinon
  404 — évasion serveur-side, jamais front-masquée).

Faces : `validated_by_type`/`validated_by_agent_id` sur la timeline agence ;
`can_validate` (bool, calculé serveur-side) sur les timelines expat/externe.
