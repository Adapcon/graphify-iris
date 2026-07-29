#!/usr/bin/env python3
"""Gera o grafo de um SUBSISTEMA do ERP Consistem padrao, fechado por dependencia.

Uso:
    python scripts/csw_grafo_modulo.py CCT --versao 8.1        # textil da 8.1
    python scripts/csw_grafo_modulo.py CCPV --versao 7.5       # pedido de venda
    python scripts/csw_grafo_modulo.py CCT --versao 8.1 --cluster
    python scripts/csw_grafo_modulo.py                         # pergunta prefixo e versao

O irmao de `csw_grafo_cliente.py`: mesma mecanica de fechamento, escopo diferente. Ali o
ponto de partida e a customizacao de um cliente; aqui e um prefixo de modulo dentro de uma
arvore de versao — `CCT` pega os 99 diretorios `rotinas/CCT*` (11.386 rotinas na 8.1, o
subsistema textil), `CCPV` pega pedido de venda, e assim por diante.

Classes NAO entram por regra de nome. O mapeamento rotina->pacote e irregular (`CCTCO` tem
`classescls/TCo`, mas `CCTTGT` nao tem `TTgt`), e adivinhar erra nas duas direcoes: perde
classe que existe e inventa pacote que nao existe. Quem decide e o fechamento — a classe
entra quando o codigo do modulo a referencia, que e tambem o unico caso em que ela importa
para este grafo.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from csw_grafo_comum import (  # noqa: E402
    arvores_padrao,
    coletar,
    contar_stubs,
    escrever_grafo,
    externos_de,
    fechar,
    versoes_disponiveis,
)

WORKSPACE_PADRAO = Path(os.environ.get("GRAPHIFY_CSW_WORKSPACE", r"C:\workspacecsw\projetos"))
OUT_PADRAO = Path(os.environ.get("GRAPHIFY_CSW_OUT", r"C:\workspacecsw\graphify-csw"))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Gera o grafo de um subsistema do ERP padrao, fechado por dependencia.")
    ap.add_argument("prefixo", nargs="?",
                    help="prefixo dos modulos, ex.: CCT (textil), CCPV, CCFT")
    ap.add_argument("--versao", help="versao do ERP (8.1, 7.5...). Padrao: pergunta")
    ap.add_argument("--workspace", type=Path, default=WORKSPACE_PADRAO)
    ap.add_argument("--out", type=Path, default=None,
                    help=f"padrao: {OUT_PADRAO}\\<VERSAO>-<PREFIXO>")
    ap.add_argument("--sem-fechamento", action="store_true",
                    help="so os modulos do prefixo (rapido, deixa muita aresta em stub)")
    ap.add_argument("--comp", help="forca a pasta de componentes (ex.: COMP-7.0)")
    ap.add_argument("--hops", type=int, default=1, metavar="N",
                    help="niveis de fechamento (padrao 1; 2 tira ~27%% dos stubs restantes)")
    ap.add_argument("--cluster", action="store_true", help="roda cluster-only ao final")
    ap.add_argument("--sequencial", action="store_true")
    args = ap.parse_args()

    workspace: Path = args.workspace
    prefixo = (args.prefixo or input("Prefixo do modulo (ex.: CCT): ")).strip().upper()
    if not prefixo.isalnum():
        print(f"erro: prefixo invalido: {prefixo!r}", file=sys.stderr)
        return 1

    disponiveis = versoes_disponiveis(workspace)
    versao = args.versao or (input(f"Versao do ERP {disponiveis}: ").strip())
    if versao not in disponiveis:
        print(f"erro: versao {versao!r} nao existe em {workspace} ({disponiveis})",
              file=sys.stderr)
        return 1

    erp = workspace / versao / f"csw{versao.replace('.', '')}"
    rotinas = erp / "rotinas"
    if not rotinas.is_dir():
        print(f"erro: nao achei {rotinas}", file=sys.stderr)
        return 1

    modulos = sorted(d for d in rotinas.iterdir() if d.is_dir() and d.name.startswith(prefixo))
    if not modulos:
        exemplos = sorted({d.name[:4] for d in rotinas.iterdir() if d.is_dir()})[:20]
        print(f"erro: nenhum modulo comeca com {prefixo!r} em {rotinas}", file=sys.stderr)
        print(f"prefixos que existem: {', '.join(exemplos)}...", file=sys.stderr)
        return 1

    out: Path = args.out or (OUT_PADRAO / f"{versao}-{prefixo}")
    (out / "graphify-out").mkdir(parents=True, exist_ok=True)

    from graphify.extract import extract

    t_inicio = time.time()
    print(f"\n=== {prefixo}* na versao {versao} — {len(modulos)} modulos ===")
    print(f"    {', '.join(d.name for d in modulos[:12])}"
          + (f" (+{len(modulos) - 12})" if len(modulos) > 12 else ""))

    alvos_modulo = [p for d in modulos for p in coletar(d)]
    print(f"[1/4] extraindo os modulos ({len(alvos_modulo)} arquivos)...", flush=True)
    t0 = time.time()
    parcial = extract(alvos_modulo, cache_root=out, root=workspace,
                      parallel=not args.sequencial)
    externos = externos_de(parcial)
    print(f"      {len(parcial['nodes'])} nos, {len(parcial['edges'])} arestas em "
          f"{time.time() - t0:.0f}s — {len(externos)} nomes externos", flush=True)

    if args.sem_fechamento:
        resultado, fechamento, faltam = parcial, set(), externos
    else:
        print(f"[2/4] fechando por dependencia (ERP + core + componentes, "
              f"{args.hops} hop(s))...", flush=True)
        arvores = arvores_padrao(workspace, versao, comp=args.comp)
        print("      " + ", ".join(a.name for a in arvores if a.is_dir()), flush=True)

        def extrair(lista):
            print(f"[3/4] extraindo {len(lista)} arquivos...", flush=True)
            t0 = time.time()
            r = extract(lista, cache_root=out, root=workspace, parallel=not args.sequencial)
            print(f"      {len(r['nodes'])} nos, {len(r['edges'])} arestas em "
                  f"{time.time() - t0:.0f}s", flush=True)
            return r

        resultado, fechamento, faltam = fechar(
            alvos_modulo, parcial, arvores, extrair, hops=args.hops)

    grafo = escrever_grafo(out, resultado, workspace)
    em_stub = contar_stubs(resultado)
    mb = grafo.stat().st_size / 1024 / 1024
    print(f"[4/4] gravado: {grafo}  ({mb:.0f} MB)")

    if args.cluster:
        print("      clusterizando...", flush=True)
        env = dict(os.environ, GRAPHIFY_MAX_GRAPH_BYTES="4GB")
        subprocess.run([sys.executable, "-m", "graphify", "cluster-only", str(out)],
                       cwd=str(REPO), env=env, check=False)

    total = len(resultado["edges"]) or 1
    print(f"""
=== RESUMO {prefixo}* {versao} ===
modulos ............. {len(modulos)} ({len(alvos_modulo)} arquivos)
fechamento .......... {len(fechamento)} arquivos
nos / arestas ....... {len(resultado['nodes'])} / {len(resultado['edges'])}
arestas em stub ..... {em_stub} ({100 * em_stub / total:.1f}%)
graph.json .......... {grafo}  ({mb:.0f} MB)
tempo ............... {time.time() - t_inicio:.0f}s

  set GRAPHIFY_MAX_GRAPH_BYTES=4GB
  python -m graphify god-nodes --graph "{grafo}" --top 15
  python scripts/csw_quem_chama.py "{grafo}" "<Label>()" --em {prefixo}XXX010
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
