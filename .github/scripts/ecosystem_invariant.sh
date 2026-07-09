#!/usr/bin/env bash
# Garde-fou ECOSYSTEME (advisory) — voir SETUP.md > "Invariant ecosysteme".
#
# Detecte si l'un des 7 contrats de lecture cross-agence a bouge dans le
# push / la PR. NE BLOQUE PAS le pipeline : ce script tourne dans un
# workflow SEPARE de ci.yml, absent du `needs` du job deploy, donc un
# resultat rouge ici ne peut structurellement pas bloquer CI/deploy.
# Il AVERTIT : job rouge + annotations visibles, SAUF aveu explicite
# (label PR 'touche-ecosysteme' OU ligne 'ECOSYSTEME: <raison>' dans un
# commit de la plage). Jamais un echec muet, jamais un blocage.
#
# Entrees (variables d'env, posees par le workflow) :
#   EVENT      github.event_name        (push | pull_request)
#   BASE_SHA   base de comparaison      (before du push / base de la PR)
#   HEAD_SHA   tete de comparaison      (after du push / head de la PR)
#   HAS_LABEL  'true' si le label PR 'touche-ecosysteme' est pose
set -uo pipefail

# Les 7 contrats de lecture ecosysteme. "dashboard" = tout le read-layer
# (manager KPI + repository agregats pays + schema de reponse).
ECOSYSTEM=(
  "src/cases/cases_repository.py"   # tri / filtres dossiers (scoping agence)
  "src/cases/filter_builder.py"     # construction des filtres partages
  "src/cases/case_export.py"        # export PDF (contrat de lecture)
  "src/views/views_schema.py"       # colonnes exposees aux vues
  "src/expat/expat_schema.py"       # contrat de lecture face expat
  "src/external/external_schema.py" # contrat de lecture face externe
  "src/dashboard/"                  # KPI / agregats pays (read-layer, dossier)
)

# --- Auto-verification : la liste surveillee est-elle encore valide ? ------
# Les 7 chemins sont en dur. Si l'un est renomme / deplace / supprime, le
# garde-fou deviendrait AVEUGLE en silence (il ne verrait plus jamais une
# touche a ce contrat). On CRIE (rouge, visible, non bloquant) et on
# s'arrete : la liste doit etre remise a jour avant que le garde-fou
# reprenne son role.
MISSING=()
for e in "${ECOSYSTEM[@]}"; do
  [ -e "$e" ] || MISSING+=("$e")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  for m in "${MISSING[@]}"; do
    echo "::error::Chemin surveille introuvable : ${m} — le garde-fou ecosysteme ne surveille plus ce fichier, mets a jour la liste (.github/scripts/ecosystem_invariant.sh) et SETUP.md."
  done
  echo "ECHEC - Garde-fou AVEUGLE : ${#MISSING[@]} chemin(s) surveille(s) disparu(s). Job ROUGE (visible), non bloquant."
  exit 1
fi

BASE="${BASE_SHA:-}"
HEAD="${HEAD_SHA:-}"

# Nouvelle branche / premier push : BASE absent ou tout-zeros -> parent de HEAD.
if [ -z "$BASE" ] || [ "$BASE" = "0000000000000000000000000000000000000000" ]; then
  BASE="$(git rev-parse "${HEAD}^" 2>/dev/null || true)"
fi
if [ -z "$BASE" ]; then
  echo "::notice::Base de comparaison introuvable — invariant non evalue (ni bloquant, ni rouge)."
  exit 0
fi

# PR : merge-base (3 points), on isole ce que la PR a change.
# push : delta du push (2 points).
if [ "${EVENT:-}" = "pull_request" ]; then
  FILE_RANGE="${BASE}...${HEAD}"
else
  FILE_RANGE="${BASE}..${HEAD}"
fi
# Les messages de commit se lisent toujours en 2 points (commits ajoutes).
LOG_RANGE="${BASE}..${HEAD}"

echo "Evenement : ${EVENT:-?}   Plage : ${FILE_RANGE}"

CHANGED="$(git diff --name-only "${FILE_RANGE}" 2>/dev/null || true)"

TOUCHED=()
while IFS= read -r f; do
  [ -z "$f" ] && continue
  for e in "${ECOSYSTEM[@]}"; do
    case "$e" in
      */) if [ "${f#"$e"}" != "$f" ]; then TOUCHED+=("$f"); break; fi ;;  # prefixe dossier
      *)  if [ "$f" = "$e" ]; then TOUCHED+=("$f"); break; fi ;;           # fichier exact
    esac
  done
done <<< "$CHANGED"

if [ "${#TOUCHED[@]}" -eq 0 ]; then
  echo "OK - Aucun fichier ecosysteme touche. Invariant intact."
  exit 0
fi

echo "::group::Fichiers ecosysteme touches (${#TOUCHED[@]})"
for f in "${TOUCHED[@]}"; do echo " - $f"; done
echo "::endgroup::"

echo "::group::Diff ecosysteme"
git diff "${FILE_RANGE}" -- "${ECOSYSTEM[@]}" || true
echo "::endgroup::"

# Annotations par fichier — visibles dans l'onglet "Files changed" de la PR.
for f in "${TOUCHED[@]}"; do
  echo "::warning file=${f},line=1::Contrat de lecture ecosysteme modifie (invariant 'diff logique nul'). Voir SETUP.md > Invariant ecosysteme."
done

# Aveu explicite : label PR OU ligne 'ECOSYSTEME:' dans un commit de la plage.
ACK=""
if [ "${HAS_LABEL:-false}" = "true" ]; then
  ACK="label PR 'touche-ecosysteme'"
fi
REASON_LINE="$(git log --format=%B "${LOG_RANGE}" 2>/dev/null | grep -iE '^[[:space:]]*ECOSYSTEME:' | head -n1 || true)"
if [ -n "$REASON_LINE" ]; then
  REASON_LINE="$(printf '%s' "$REASON_LINE" | sed -E 's/^[[:space:]]*//')"
  ACK="${ACK:+$ACK + }commit \"${REASON_LINE}\""
fi

if [ -n "$ACK" ]; then
  echo "::notice::Touche ecosysteme ASSUMEE explicitement (${ACK}). Franchissement delibere — le job passe au vert."
  echo "OK - Contournement explicite reconnu : ${ACK}"
  exit 0
fi

echo "::error::Invariant ecosysteme franchi SANS aveu explicite (${#TOUCHED[@]} fichier(s)). Pose le label PR 'touche-ecosysteme' OU ajoute une ligne 'ECOSYSTEME: <raison>' a un commit du push/de la PR."
echo "ECHEC - Aucun aveu explicite : job ROUGE (visible, jamais muet). Ce check n'est PAS bloquant, mais il ne doit pas rester rouge sans raison : assume-le ou reviens en arriere."
exit 1
