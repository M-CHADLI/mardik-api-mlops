"""Exporte les documents de conception en PDF, pour la remise.

Pas de pandoc ni de LaTeX dans l'environnement : la conversion passe par le
navigateur déjà présent (Chrome en mode *headless*, `--print-to-pdf`). C'est le
seul moteur disponible qui rende correctement les deux choses dont ces documents
dépendent — les tableaux Markdown et les schémas en art ASCII, qui exigent une
chasse fixe et un retour à la ligne désactivé, faute de quoi les flèches des
diagrammes se décalent d'une ligne à l'autre et le schéma devient illisible.

    python -m scripts.exporter_pdf                 # tous les documents
    python -m scripts.exporter_pdf docs/mon.md     # un document précis

Les PDF sont écrits à côté du Markdown source. Le Markdown reste la source :
on régénère, on ne corrige pas un PDF.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent

# Les documents de conception, ceux que l'instructeur lit en PDF. Le sommaire
# `livrables/README.md` n'y est pas : il est fait pour être lu sur GitHub, où
# ses liens relatifs fonctionnent — un PDF les casserait.
DOCUMENTS = [
    RACINE / "docs" / "dossier-de-conception.md",
    RACINE / "docs" / "exploitation.md",
]

CHEMINS_CHROME = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

STYLE = """
@page { size: A4; margin: 18mm 15mm; }
body { font: 10.5pt/1.55 "Segoe UI", system-ui, sans-serif; color: #15161a; }
h1 { font-size: 19pt; margin: 0 0 .3em; border-bottom: 2px solid #2b6cb0; padding-bottom: .2em; }
h2 { font-size: 14pt; margin: 1.6em 0 .5em; color: #1a365d; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin: 1.2em 0 .4em; page-break-after: avoid; }
h2 + p, h3 + p, h2 + table, h3 + table { page-break-before: avoid; }
p, li { orphans: 3; widows: 3; }
em { color: #44474f; }
code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 9pt;
       background: #f1f2f5; padding: .1em .3em; border-radius: 3px; }
/* Les schémas en art ASCII : chasse fixe, aucun repli de ligne, jamais coupés
   sur deux pages — un diagramme scindé perd son sens. */
pre { font-family: "Cascadia Mono", Consolas, monospace; font-size: 8.2pt; line-height: 1.32;
      background: #f7f8fa; border: 1px solid #e2e4e9; border-left: 3px solid #2b6cb0;
      padding: .7em .9em; border-radius: 4px; white-space: pre; overflow: visible;
      page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: inherit; }
table { border-collapse: collapse; width: 100%; margin: .8em 0; font-size: 9.3pt;
        page-break-inside: avoid; }
th, td { border: 1px solid #d9dbe0; padding: .35em .55em; text-align: left; vertical-align: top; }
th { background: #eef1f5; font-weight: 600; }
blockquote { margin: .8em 0; padding: .5em .9em; border-left: 3px solid #b9c2cc;
             background: #f7f8fa; color: #3d4049; page-break-inside: avoid; }
hr { border: 0; border-top: 1px solid #dfe1e6; margin: 1.6em 0; }
a { color: #2b6cb0; text-decoration: none; }
"""


def trouver_chrome() -> str:
    for chemin in CHEMINS_CHROME:
        if chemin and Path(chemin).exists():
            return chemin
    trouve = shutil.which("chrome") or shutil.which("chromium") or shutil.which("msedge")
    if trouve:
        return trouve
    raise RuntimeError("aucun navigateur trouvé pour produire le PDF")


def en_html(source: Path) -> str:
    import markdown

    corps = markdown.markdown(
        source.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "sane_lists", "toc"],
    )
    return (
        "<!doctype html><html lang=fr><head><meta charset=utf-8>"
        f"<title>{source.stem}</title><style>{STYLE}</style></head>"
        f"<body>{corps}</body></html>"
    )


def exporter(source: Path, chrome: str | None = None) -> Path:
    chrome = chrome or trouver_chrome()
    cible = source.with_suffix(".pdf")
    with tempfile.TemporaryDirectory() as temporaire:
        html = Path(temporaire) / f"{source.stem}.html"
        html.write_text(en_html(source), encoding="utf-8")
        subprocess.run(
            [
                chrome,
                "--headless",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={cible}",
                html.as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    if not cible.exists():
        raise RuntimeError(f"le PDF n'a pas été produit : {cible}")
    return cible


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    sources = [Path(a) for a in argv] or DOCUMENTS
    chrome = trouver_chrome()
    for source in sources:
        if not source.exists():
            print(f"absent : {source}", file=sys.stderr)
            return 1
        cible = exporter(source, chrome)
        print(f"{source.relative_to(RACINE)}  ->  {cible.name}  "
              f"({cible.stat().st_size // 1024} Ko)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
