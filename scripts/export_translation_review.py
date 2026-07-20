"""Export lisible de TOUTES les chaines d'une langue pour relecture par
un locuteur (le livrable Eric du lot hongrois — reutilisable pour toute
langue future).

Usage: uv run python scripts/export_translation_review.py hu > relecture-hu.md
Groupe par contexte : les 21 catalogues email, puis le catalogue de
champs (libelles + options). La colonne FR est la reference de lecture.
"""

import ast
import sys
from pathlib import Path


def _catalogs(path: str) -> list[tuple[str, dict]]:
    tree = ast.parse(Path(path).read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            try:
                d = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(d, dict) and "fr" in d:
                name = getattr(node.targets[0], "id", f"L{node.lineno}")
                out.append((name, d))
    return out


def _blobs(path: str) -> list[dict]:
    tree = ast.parse(Path(path).read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            try:
                d = ast.literal_eval(node)
            except (ValueError, SyntaxError):
                continue
            if isinstance(d, dict) and "fr" in d and "en" in d:
                out.append(d)
    return out


def main() -> None:
    lang = sys.argv[1] if len(sys.argv) > 1 else "hu"
    print(f"# Relecture {lang.upper()} — toutes les chaines produites\n")
    print("> Colonne FR = la reference ; corriger la colonne cible et renvoyer.\n")
    print("## Gabarits email (src/core/email_templates.py)\n")
    for name, d in _catalogs("src/core/email_templates.py"):
        fr, target = d["fr"], d.get(lang)
        if target is None:
            continue
        print(f"### {name}\n")
        print("| clé | FR | " + lang.upper() + " |\n|---|---|---|")
        if isinstance(fr, dict):
            for k in fr:
                print(f"| {k} | {fr[k]} | {target.get(k, 'MANQUANT')} |")
        else:
            print(f"| (texte) | {fr} | {target} |")
        print()
    print("## Catalogue de champs (src/journeys/field_catalog.py)\n")
    print("| FR | " + lang.upper() + " |\n|---|---|")
    for d in _blobs("src/journeys/field_catalog.py"):
        fr, target = d["fr"], d.get(lang, "MANQUANT")
        if isinstance(fr, list):
            fr = " / ".join(fr)
            target = " / ".join(target) if isinstance(target, list) else target
        print(f"| {fr} | {target} |")


if __name__ == "__main__":
    main()
