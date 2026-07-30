"""Quem chama uma label/metodo — inclusive quando o nome se repete em varias rotinas.

O `graphify affected` casa o no por NOME e desiste quando ha empate ("No unique node
match"), o que em ObjectScript acontece muito: `0000`, `9999`, `1100ON` existem em quase
toda rotina de tela. Aqui o alvo pode ser qualificado pelo arquivo, entao `9999` de uma
rotina especifica fica enderecavel.

Quando nada casa, em vez de so dizer "nenhum no", sugere: onde o rotulo existe se o
--em filtrou demais, quem contem o pedaco digitado, e quem se parece (typo ou
transposicao — "^TCMEORL" quando a global e "^TMCEORL").

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

import difflib
import json
import sys
from pathlib import Path


def dedup_por_id(nodes: list[dict]) -> list[dict]:
    """Colapsa nos repetidos pelo `id`.

    No de global entra uma vez por arquivo que a referencia — `^TMCEORL` aparece 29x
    com id identico no grafo do texil 8.1. Sem colapsar, o relatorio de referencias
    sai repetido 29 vezes (187 KB no lugar de 7 KB).
    """
    unicos: dict[str, dict] = {}
    for n in nodes:
        unicos.setdefault(n["id"], n)
    return list(unicos.values())


def sugerir(nodes: list[dict], alvo: str, arquivo: str | None, limite: int = 10) -> None:
    """Imprime pistas quando nenhum rotulo casa exato — nao decide nada, so orienta.

    Cobre as tres formas de errar o alvo, em ordem de probabilidade:
      1. o rotulo existe, mas nao no arquivo do --em;
      2. o que foi digitado e um pedaco do nome real;
      3. o nome tem typo/transposicao ("^TCMEORL" -> "^TMCEORL").

    Global so e comparada com global e label so com label: o `^` inicial separa os
    dois universos e cortar por ele evita sugerir label para quem procurou global.
    """
    e_global = alvo.startswith("^")
    pedaco = alvo.lstrip("^").rstrip(")").rstrip("(").upper()

    if arquivo:
        onde = sorted({
            str(n.get("source_file")) for n in nodes
            if n.get("label") in (alvo, alvo + "()")
        })
        if onde:
            print(f"{alvo!r} existe, mas nao em arquivo contendo {arquivo!r}. Esta em:")
            for f in onde[:limite]:
                print(f"  {f}")
            if len(onde) > limite:
                print(f"  ... +{len(onde) - limite} arquivo(s)")
            return

    pool: dict[str, str] = {}
    for n in nodes:
        lbl = str(n.get("label") or "")
        if lbl and lbl.startswith("^") == e_global:
            pool.setdefault(lbl, str(n.get("source_file") or "no compartilhado"))
    universo = "global(is)" if e_global else "label(is)"

    contem = sorted(l for l in pool if pedaco in l.upper())
    if contem:
        print(f"nenhum rotulo exatamente {alvo!r}. Contem {pedaco!r}:")
        for l in contem[:limite]:
            print(f"  {l:34s} {pool[l]}")
        if len(contem) > limite:
            print(f"  ... +{len(contem) - limite}")
        return

    # Chave em maiuscula para o difflib tambem tolerar erro de caixa no alvo.
    por_caixa = {l.upper(): l for l in pool}
    parecidos = difflib.get_close_matches(alvo.upper(), por_caixa, n=limite, cutoff=0.7)
    # Transposicao ("^TCMEORL"/"^TMCEORL") e o typo mais comum aqui e da anagrama
    # exato; o difflib nao a privilegia, entao sobe na frente sem reordenar o resto.
    anagrama = sorted(alvo.upper())
    parecidos.sort(key=lambda k: sorted(k) != anagrama)
    if parecidos:
        print(f"nenhum rotulo exatamente {alvo!r} nem contendo {pedaco!r}. Parecidos:")
        for k in parecidos:
            l = por_caixa[k]
            print(f"  {l:34s} {pool[l]}")
        return

    print(f"nenhum no com rotulo {alvo!r}"
          + (f" em arquivo contendo {arquivo!r}" if arquivo else "")
          + f", nem contendo {pedaco!r}, nem parecido, entre {len(pool)} {universo}"
          " do grafo")


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
    alvos = dedup_por_id(alvos)
    if not alvos:
        sugerir(nodes, alvo, arquivo)
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
