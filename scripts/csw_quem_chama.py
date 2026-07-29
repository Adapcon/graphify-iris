"""Quem chama uma label/metodo — inclusive quando o nome se repete em varias rotinas.

O `graphify affected` casa o no por NOME e desiste quando ha empate ("No unique node
match"), o que em ObjectScript acontece muito: `0000`, `9999`, `1100ON` existem em quase
toda rotina de tela. Aqui o alvo pode ser qualificado pelo arquivo, entao `9999` de uma
rotina especifica fica enderecavel.

Uso:
  python scripts/csw_quem_chama.py <graph.json> "<Nome>"                    # todos os homonimos
  python scripts/csw_quem_chama.py <graph.json> "<Nome>" --em RGCOTCP020RG   # so o desse arquivo
  python scripts/csw_quem_chama.py <graph.json> "<Nome>" --relacao references  # quem LE/GRAVA (globals)
  python scripts/csw_quem_chama.py <graph.json> "^CGIGEN" --relacao references

Exemplos (grafos gerados por scripts/csw_grafo_cliente.py):
  python scripts/csw_quem_chama.py C:/workspacecsw/graphify-csw/CO/graphify-out/graph.json "ObterItemComprado()"
  python scripts/csw_quem_chama.py C:/workspacecsw/graphify-csw/CO/graphify-out/graph.json "9999()" --em RGCOCNT600
  python scripts/csw_quem_chama.py C:/workspacecsw/graphify-csw/CO/graphify-out/graph.json "^ASCOECO" --relacao references
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    graph_path, alvo = sys.argv[1], sys.argv[2]
    arquivo = relacao = None
    args = sys.argv[3:]
    for i, a in enumerate(args):
        if a == "--em" and i + 1 < len(args):
            arquivo = args[i + 1]
        elif a == "--relacao" and i + 1 < len(args):
            relacao = args[i + 1]

    g = json.loads(Path(graph_path).read_text(encoding="utf-8"))
    nodes = g.get("nodes", [])
    edges = g.get("edges") or g.get("links") or []
    by_id = {n["id"]: n for n in nodes}

    # O alvo aceita o rotulo exato ("9999()") ou sem os parenteses ("9999").
    querido = alvo if alvo.endswith(("()", ")")) or alvo.startswith("^") else alvo + "()"
    alvos = [
        n for n in nodes
        if n.get("label") in (alvo, querido)
        and (arquivo is None or arquivo.lower() in str(n.get("source_file", "")).lower())
    ]
    if not alvos:
        print(f"nenhum no com rotulo {alvo!r}"
              + (f" em arquivo contendo {arquivo!r}" if arquivo else ""))
        return 1

    for alvo_node in alvos:
        nid = alvo_node["id"]
        titulo = f"{alvo_node['label']}  [{alvo_node.get('source_file') or 'no compartilhado'}"
        loc = alvo_node.get("source_location")
        print(f"\n=== {titulo}{':' + loc if loc else ''}] ===")
        entradas = [
            e for e in edges
            if e.get("target") == nid
            and e.get("relation") != "defines"
            and (relacao is None or e.get("relation") == relacao)
        ]
        if not entradas:
            print("  (ninguem chama/referencia)")
            continue
        for e in sorted(entradas, key=lambda e: (str(e.get("source_file")), str(e.get("source_location")))):
            origem = by_id.get(e.get("source"), {})
            ctx = e.get("context") or e.get("relation")
            print(f"  {origem.get('label', e.get('source')):34s} [{ctx:18s}] "
                  f"{e.get('source_file')}:{e.get('source_location')}")
        print(f"  -- {len(entradas)} referencia(s), "
              f"{len({e.get('source_file') for e in entradas})} arquivo(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
