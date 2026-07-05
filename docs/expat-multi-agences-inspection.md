# Espace client multi-agences — inspection (lecture seule)

> Inspection du 2026-07-05. Question : un `expat_user` est unique global
> (pas d'`agency_id`) et peut être principal de `client_case` dans des
> agences différentes (link-or-create). Que voit ce client à la
> connexion à son espace ? Rien n'a été modifié — rapport de comportement
> réel, chemins de code cités.

## Verdict global

Le multi-agences est solide de bout en bout : sélecteur complet
(toutes agences), branding par dossier, cloisonnement strict par
`case_id`, consentement par agence, activation du 2e dossier sans
vecteur de prise de compte. **Une seule lacune trouvée : le mail de
rappel (dispatch) est aveugle à l'agence** (§6) — **corrigée le
2026-07-05** (sujet/intro nomment l'agence, lien brandé, 6 langues).

## 1. Résolution des dossiers : le client voit TOUT, toutes agences

`GET /expat/cases` → `ExpatRepository.list_cases_for_expat(expat.id)` :

- `WHERE principal_expat_user_id = expat_id AND deleted_at IS NULL`,
  **sans aucun filtre d'agence**, jointure `Agency`, tri
  `created_at DESC` (`src/expat/expat_repository.py`).
- Chaque `ExpatCaseSummaryResponse` embarque **son** contexte d'agence :
  `{id, slug, name, has_logo, has_cover}`.
- Aucun scope au dossier d'invitation : le lien au dossier, c'est
  `principal_expat_user_id`, posé à la création du dossier ;
  l'invitation n'est que notification + trace d'audit.

## 2. Cas concret : Jean, un dossier chez A, un chez B

- Jean voit **les 2 dossiers dans le sélecteur** (plus récent en
  premier).
- **Le branding suit le dossier sélectionné** : chaque résumé porte
  l'agence du dossier ; `GET /expat/agencies/{agency_id}/logo|cover`
  est scopé « agence tenant au moins un de MES dossiers vivants »
  (`ExpatPortalManager.agency_logo/agency_cover`) → Jean charge le
  logo/cover de A **et** de B, le front pioche via l'`agency.id` du
  dossier affiché. Cohérent en multi-agences par construction
  (résolution par dossier, jamais globale).
- La page de **login** est brandée par `?agency=slug` venu du lien
  mail — donc l'agence du dernier mail cliqué (comportement attendu).

## 3. Le token expat : identité pure, pas de scope

- `create_access_token(sub=expat_id, audience=expat)` — **aucun
  case_id/agency_id** dans le token (seul claim additionnel possible :
  `impersonator_id`). L'accès est « tout ce qui m'appartient »,
  l'ownership se vérifie par requête à chaque appel.
- **2e invitation sur un compte déjà actif** (`AuthManager.
  activate_expat`) : l'invitation passe ACCEPTED et le mot de passe
  n'est **jamais** touché (`already_active=True`) — un token
  d'invitation voyage par mail, autoriser un set-password serait un
  vecteur de prise de compte. Le dossier B est déjà lié à la création
  (link-or-create par email) ; l'expat actif reçoit le mail « un
  nouveau dossier vous attend » avec lien de login brandé B.

## 4. Cloisonnement entre ses dossiers : strict, par dossier

- Tous les endpoints expat (détail, notifications, requirements,
  upload de document, case-requirements, timeline, commentaires)
  passent par `_get_owned_case(expat, case_id)` : `expat_id + case_id
  + deleted_at IS NULL`, **404 jamais 403** (zéro révélation
  d'existence d'un dossier étranger).
- Un `case_id` du dossier B passe légitimement (c'est le sien) et
  charge **exclusivement** le contexte B — toutes les données
  descendent du `case_id`, rien n'est résolu par agence ni en global.
  Pas de mélange de contextes possible.

## 5. Consentement : le clickwrap multi-agences fonctionne

- `missing_for_expat` (`src/core/rbac/consent_gate.py`) produit les
  paires `(agency_id × document actif)` pour **chaque agence tenant un
  dossier vivant**, moins les acceptations enregistrées
  `(type, version, agency_id)`.
- Le gate bloque **toutes** les routes expat (hors `CONSENT_EXEMPT`)
  tant qu'il reste une paire manquante → Jean doit accepter pour A
  **et** pour B (écrans enchaînés côté front, un par agence).
- Nuance de design (pas un bug) : le blocage est **global** — Jean ne
  peut pas consulter A tant qu'il n'a pas aussi accepté B. C'est le
  clickwrap bloquant voulu.

## 6. Notifications : bon contexte partout, sauf UNE lacune

| Flux | Contexte d'agence |
|---|---|
| Requirement / étape rouverte | ✅ `agency_name` + `space_link(slug)` résolus depuis le dossier concerné |
| Commentaires (notif de fil) | ✅ nom d'agence + lien brandé du slug de l'agence du dossier |
| **Rappels (dispatch scheduler)** | ⚠️ **générique** — sujet « Nidria : Rappel », intro neutre, sans nom d'agence ni lien brandé ; seul le corps rédigé par l'agence peut la nommer |

**Le point à fixer si souhaité** : pour Jean (2 agences), un mail de
rappel n'indique ni de quelle agence ni de quel dossier il vient, et le
lien d'espace n'est pas brandé. Fix léger : passer `agency_name` +
`space_link(slug)` au template `reminder_email`, comme les deux autres
flux (le dispatch `dispatch_due_reminders` joint déjà le dossier ; il
suffit d'y joindre l'agence).
