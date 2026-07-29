#!/usr/bin/env python3
"""Gera o grafo de conhecimento de UM cliente CSW, fechado por dependencia.

Uso:
    python scripts/csw_grafo_cliente.py            # pergunta a conta
    python scripts/csw_grafo_cliente.py CO
    python scripts/csw_grafo_cliente.py CO --versao 7.5 --cluster
    python scripts/csw_grafo_cliente.py CO --sem-fechamento   # so a customizacao

POR QUE FECHAMENTO
------------------
Uma referencia ObjectScript nomeia uma rotina/classe/include, nunca um caminho, e o
resolvedor de `graphify` casa esses nomes DEPOIS que o corpus inteiro foi extraido. Logo,
arquivo que nao entrou na MESMA extracao vira stub externo: num grafo so-da-customizacao
do cliente CO, 324.226 arestas (31% do total) morriam em stub.

Mas nao e preciso o ERP inteiro (94.904 arquivos na 7.5 — daria ~1,9M nos, inutilizavel).
O proprio grafo da customizacao diz de quem ela depende: cada no `ref_iris_*` e um nome
que o resolvedor nao achou. Resolvendo esses nomes contra as arvores padrao sai a lista
exata do que falta — para o CO, 2.081 arquivos (+9%), que derrubaram os stubs de 31% para
1,0% das arestas.

O que ele faz, em ordem:
    1. extrai a customizacao do cliente (`DESENV/custom/<conta>`);
    2. le os nomes externos do resultado;
    3. le a versao do ERP no `pomcs.xml` do cliente (ou usa `--versao`) e resolve os
       nomes contra essa arvore + os componentes + os produtos em `DESENV`;
    4. extrai de novo, agora com customizacao + fechamento, numa unica passada (e o unico
       jeito das arestas cruzarem);
    5. grava `<out>/graphify-out/graph.json` + `.graphify_root`, e opcionalmente clusteriza.

A fase 1 e a fase 4 compartilham o cache de AST, entao a segunda extracao so paga pelos
arquivos novos do fechamento.
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

WORKSPACE_PADRAO = Path(os.environ.get("GRAPHIFY_CSW_WORKSPACE", r"C:\workspacecsw\projetos"))
OUT_PADRAO = Path(os.environ.get("GRAPHIFY_CSW_OUT", r"C:\workspacecsw\graphify-csw"))


from csw_grafo_comum import (  # noqa: E402
    arvores_padrao,
    coletar,
    contar_stubs,
    escrever_grafo,
    externos_de,
    fechar,
    releases_pomcs,
    versoes_disponiveis,
)


def detectar_versao(cliente_dir: Path, workspace: Path) -> str:
    """Versao do ERP declarada no pomcs.xml do cliente.

    Cada cliente roda a sua versao, e nada na arvore de fontes diz qual — os
    `.vscode/settings.json` ficam nas pastas de VERSAO. Quem declara e o `pomcs.xml` do
    cliente: `<sistemaId>csw</sistemaId>` lista os releases do ERP e o maior e o vigente.

    Nao tente adivinhar comparando os fontes: nome de rotina e estavel entre versoes, e as
    quatro arvores do workspace resolvem exatamente os mesmos nomes — medido, 291 de 291 em
    todas. Sem o pomcs.xml a resposta e perguntar.
    """
    pom = cliente_dir / "pomcs.xml"
    csw = releases_pomcs(pom, "csw") if pom.is_file() else []
    if csw:
        maior = max(csw)
        erp = f"{maior[0]}.{maior[1]}"
        print(f"  pomcs.xml declara ERP {erp} "
              f"(releases csw: {', '.join('.'.join(map(str, r)) for r in sorted(csw))})")
    else:
        disponiveis = versoes_disponiveis(workspace)
        print(f"  {pom.name} nao declara a versao do ERP.")
        erp = input(f"  Versao do ERP {disponiveis} [7.5]: ").strip() or "7.5"
    if not (workspace / erp).is_dir():
        raise SystemExit(f"erro: arvore da versao {erp} nao existe em {workspace}")
    return erp


# ── principal ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Gera o grafo de um cliente CSW, fechado por dependencia.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("conta", nargs="?", help="conta do cliente, 2 letras (CO, FE, GB...)")
    ap.add_argument("--versao", help="versao do ERP (7.5, 7.6, 8.0...). Padrao: detecta")
    ap.add_argument("--workspace", type=Path, default=WORKSPACE_PADRAO,
                    help=f"raiz do workspace CSW (padrao: {WORKSPACE_PADRAO})")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"diretorio de saida (padrao: {OUT_PADRAO}\\<CONTA>)")
    ap.add_argument("--sem-fechamento", action="store_true",
                    help="extrai so a customizacao (rapido; deixa ~31%% das arestas em stub)")
    ap.add_argument("--hops", type=int, default=1, metavar="N",
                    help="niveis de fechamento (padrao 1; 2 fecha as dependencias do padrao)")
    ap.add_argument("--cluster", action="store_true",
                    help="roda cluster-only ao final (comunidades + GRAPH_REPORT.md)")
    ap.add_argument("--sequencial", action="store_true", help="desliga o pool de processos")
    args = ap.parse_args()

    conta = (args.conta or input("Conta do cliente (2 letras, ex.: CO): ")).strip().lower()
    if len(conta) != 2 or not conta.isalpha():
        print(f"erro: conta deve ter 2 letras (recebi {conta!r})", file=sys.stderr)
        return 1

    workspace: Path = args.workspace
    cliente_dir = workspace / "DESENV" / "custom" / conta
    if not cliente_dir.is_dir():
        existentes = sorted(
            d.name.upper() for d in (workspace / "DESENV" / "custom").iterdir() if d.is_dir()
        ) if (workspace / "DESENV" / "custom").is_dir() else []
        print(f"erro: nao achei {cliente_dir}", file=sys.stderr)
        if existentes:
            print(f"contas disponiveis: {', '.join(existentes)}", file=sys.stderr)
        return 1

    out: Path = args.out or (OUT_PADRAO / conta.upper())
    (out / "graphify-out").mkdir(parents=True, exist_ok=True)

    from graphify.extract import extract  # importado aqui para o --help nao pagar por ele

    t_inicio = time.time()
    print(f"\n=== cliente {conta.upper()} — {cliente_dir} ===")

    # FASE 1 — a customizacao sozinha. Serve para dois fins: e metade do grafo final e,
    # pelos nos externos, e a propria lista do que falta trazer do padrao.
    arquivos_cliente = coletar(cliente_dir)
    print(f"[1/4] extraindo a customizacao ({len(arquivos_cliente)} arquivos)...", flush=True)
    t0 = time.time()
    parcial = extract(arquivos_cliente, cache_root=out, root=workspace,
                      parallel=not args.sequencial)
    externos = externos_de(parcial)
    print(f"      {len(parcial['nodes'])} nos, {len(parcial['edges'])} arestas em "
          f"{time.time() - t0:.0f}s — {len(externos)} nomes externos", flush=True)

    if args.sem_fechamento:
        resultado, fechamento, faltam, versao = parcial, set(), externos, args.versao or "-"
    else:
        # FASE 2 — de quem a customizacao depende, e onde isso mora.
        print("[2/4] resolvendo o fechamento de dependencia...", flush=True)
        versao = args.versao or detectar_versao(cliente_dir, workspace)
        # ERP + core + componentes da versao, mais os produtos dos customizadores
        # (AD*, CX*, TT*), que a customizacao tambem chama. `desenv/custom` fica de fora
        # para nao arrastar outro cliente para dentro deste grafo.
        arvores = (arvores_padrao(workspace, versao, pom_cliente=cliente_dir / "pomcs.xml")
                   + [workspace / "DESENV"])
        print("      " + ", ".join(a.name for a in arvores if a.is_dir()), flush=True)

        def extrair(lista):
            print(f"[3/4] extraindo {len(lista)} arquivos...", flush=True)
            t0 = time.time()
            r = extract(lista, cache_root=out, root=workspace, parallel=not args.sequencial)
            print(f"      {len(r['nodes'])} nos, {len(r['edges'])} arestas em "
                  f"{time.time() - t0:.0f}s", flush=True)
            return r

        resultado, fechamento, faltam = fechar(
            arquivos_cliente, parcial, arvores, extrair, hops=args.hops,
            pular=("desenv/custom",))

    # FASE 4 — grava no formato que o CLI do graphify le.
    grafo = escrever_grafo(out, resultado, workspace)
    em_stub = contar_stubs(resultado)
    mb = grafo.stat().st_size / 1024 / 1024
    print(f"[4/4] gravado: {grafo}  ({mb:.0f} MB)")

    if args.cluster:
        print("      clusterizando (comunidades + GRAPH_REPORT.md)...", flush=True)
        env = dict(os.environ, GRAPHIFY_MAX_GRAPH_BYTES="4GB")
        subprocess.run([sys.executable, "-m", "graphify", "cluster-only", str(out)],
                       cwd=str(REPO), env=env, check=False)

    total = len(resultado["edges"]) or 1
    print(f"""
=== RESUMO {conta.upper()} ===
versao do ERP ....... {versao}
arquivos ............ {len(arquivos_cliente)} da customizacao + {len(fechamento)} do fechamento
nos / arestas ....... {len(resultado['nodes'])} / {len(resultado['edges'])}
arestas em stub ..... {em_stub} ({100 * em_stub / total:.1f}%)  <- fora do escopo escaneado
graph.json .......... {grafo}  ({mb:.0f} MB)
tempo ............... {time.time() - t_inicio:.0f}s

Consultas (o teto de 512 MB precisa ser levantado para grafos grandes):
  set GRAPHIFY_MAX_GRAPH_BYTES=4GB
  python -m graphify god-nodes --graph "{grafo}" --top 15
  python -m graphify affected "<Label>()" --graph "{grafo}" --relation calls --depth 1
  python -m graphify explain "<Pkg.Classe>" --graph "{grafo}"
  python scripts/csw_quem_chama.py "{grafo}" "<Label>()" --em <ROTINA>
""")
    if em_stub and not args.sem_fechamento:
        print(f"Os {em_stub} stubs restantes sao hop-2: dependencias das rotinas padrao que\n"
              f"entraram no fechamento. Para fechar mais, rode de novo — o fechamento e\n"
              f"recalculado a partir do grafo atual.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
