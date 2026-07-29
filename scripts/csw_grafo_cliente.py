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
    3. descobre a versao do ERP por EVIDENCIA — qual arvore (7.5/7.6/8.0/...) resolve
       mais desses nomes — ou usa a que voce passar em `--versao`;
    4. extrai de novo, agora com customizacao + fechamento, numa unica passada (e o unico
       jeito das arestas cruzarem);
    5. grava `<out>/graphify-out/graph.json` + `.graphify_root`, e opcionalmente clusteriza.

A fase 1 e a fase 4 compartilham o cache de AST, entao a segunda extracao so paga pelos
arquivos novos do fechamento.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

WORKSPACE_PADRAO = Path(os.environ.get("GRAPHIFY_CSW_WORKSPACE", r"C:\workspacecsw\projetos"))
OUT_PADRAO = Path(os.environ.get("GRAPHIFY_CSW_OUT", r"C:\graphify-csw"))
SUFFIXES = (".mac", ".cls", ".inc")
# Componentes por familia de versao (CLAUDE.md: 7.x -> cswutil70, 8.x -> cswutil80).
COMPONENTES = {"7": "COMP-7.0", "8": "COMP-8.0"}


# ── coleta e indexacao ───────────────────────────────────────────────────────

def coletar(raiz: Path, pular: tuple[str, ...] = ()) -> list[Path]:
    """Todos os fontes IRIS sob `raiz`, ignorando diretorios que casem `pular`."""
    achados: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(raiz):
        low = dirpath.lower().replace("\\", "/")
        if any(x in low for x in pular):
            continue
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SUFFIXES:
                achados.append(Path(dirpath) / fn)
    return achados


def canonico(nome: str) -> str:
    """`%CSW1UTI`, `_CSW1UTI` e `csw1uti` viram uma chave só — o `%` vira `_` no disco."""
    return nome.lstrip("%_").casefold()


def indexar(raizes: list[Path], pular: tuple[str, ...] = ()) -> tuple[dict, dict]:
    """(rotinas/includes por nome canonico, classes pelo nome pontuado do path)."""
    nomes: dict[str, list[Path]] = {}
    classes: dict[str, list[Path]] = {}
    for raiz in raizes:
        if not raiz.is_dir():
            continue
        for p in coletar(raiz, pular):
            stem, ext = os.path.splitext(p.name)
            if ext.lower() == ".cls":
                # classescls/Pkg_Sub/Nome.cls -> Pkg.Sub.Nome
                pontuado = f"{p.parent.name.replace('_', '.')}.{stem}".casefold()
                classes.setdefault(pontuado, []).append(p)
            else:
                nomes.setdefault(canonico(stem), []).append(p)
    return nomes, classes


def resolver(externos: list[str], nomes: dict, classes: dict) -> tuple[set[Path], list[str]]:
    """(arquivos que atendem os nomes externos, nomes que ninguem atende)."""
    arquivos: set[Path] = set()
    faltam: list[str] = []
    for nome in externos:
        hits = nomes.get(canonico(nome)) or classes.get(nome.casefold()) or []
        if hits:
            arquivos.update(hits)
        else:
            faltam.append(nome)
    return arquivos, faltam


def externos_de(resultado: dict) -> list[str]:
    """Nomes que o resolvedor nao achou no escopo — a lista do que falta trazer."""
    return [
        str(n.get("label", "")) for n in resultado.get("nodes", [])
        if str(n.get("id", "")).startswith("ref") and n.get("label")
    ]


# ── versoes ──────────────────────────────────────────────────────────────────

def versoes_disponiveis(workspace: Path) -> list[str]:
    return sorted(
        d.name for d in workspace.iterdir()
        if d.is_dir() and d.name[:1].isdigit() and "." in d.name
    )


def _releases(pom: Path, sistema: str) -> list[tuple[int, ...]]:
    """Releases declarados para um `sistemaId` no pomcs.xml, como tuplas ordenaveis."""
    import xml.etree.ElementTree as ET
    try:
        raiz = ET.parse(pom).getroot()
    except (OSError, ET.ParseError):
        return []
    achados: list[tuple[int, ...]] = []
    for dep in raiz.iter("dependencia"):
        sid = dep.findtext("sistemaId") or ""
        if sid.strip().casefold() != sistema:
            continue
        for rel in dep.iter("release"):
            partes = (rel.text or "").strip().split(".")
            if len(partes) >= 2 and all(p.isdigit() for p in partes[:2]):
                achados.append(tuple(int(p) for p in partes if p.isdigit()))
    return achados


def detectar_versao(cliente_dir: Path, workspace: Path) -> tuple[str, str]:
    """(versao do ERP, diretorio de componentes) declarados no pomcs.xml do cliente.

    Cada cliente roda a sua versao, e nada na arvore de fontes diz qual — os
    `.vscode/settings.json` ficam nas pastas de VERSAO. Quem declara e o `pomcs.xml` do
    cliente: `<sistemaId>csw</sistemaId>` lista os releases do ERP (o maior e o vigente)
    e `<sistemaId>cswutil</sistemaId>` o dos componentes, que da a pasta `COMP-x.y`.

    Nao tente adivinhar comparando os fontes: nome de rotina e estavel entre versoes, e
    as quatro arvores do workspace resolvem exatamente os mesmos nomes — medido, 291 de
    291 em todas. Sem o pomcs.xml a resposta e perguntar.
    """
    pom = cliente_dir / "pomcs.xml"
    erp = comp = ""
    if pom.is_file():
        csw = _releases(pom, "csw")
        util = _releases(pom, "cswutil")
        if csw:
            maior = max(csw)
            erp = f"{maior[0]}.{maior[1]}"
        if util:
            maior_util = max(util)
            comp = f"COMP-{maior_util[0]}.{maior_util[1]}"
        if erp:
            print(f"  pomcs.xml declara: ERP {erp}"
                  + (f", componentes {comp}" if comp else "")
                  + f" (releases csw: {', '.join('.'.join(map(str, r)) for r in sorted(csw))})")
    if not erp:
        # Sem declaracao: perguntar, porque comparar fontes nao discrimina versao.
        disponiveis = versoes_disponiveis(workspace)
        print(f"  {pom.name} nao declara a versao do ERP.")
        escolha = input(f"  Versao do ERP {disponiveis} [7.5]: ").strip() or "7.5"
        erp = escolha
    if not comp:
        comp = COMPONENTES.get(erp[:1], "COMP-7.0")
    if not (workspace / erp).is_dir():
        raise SystemExit(f"erro: arvore da versao {erp} nao existe em {workspace}")
    return erp, comp


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
        if args.versao:
            versao = args.versao
            comp_nome = COMPONENTES.get(versao[:1], "COMP-7.0")
        else:
            versao, comp_nome = detectar_versao(cliente_dir, workspace)
        erp = workspace / versao / f"csw{versao.replace('.', '')}"
        comp = workspace / comp_nome
        # Produtos dos customizadores (AD*, CX*, TT*) tambem sao chamados pela
        # customizacao; entram excluindo `custom/` para nao arrastar outro cliente.
        produtos = workspace / "DESENV"
        nomes, classes = indexar([erp, comp, produtos], pular=("desenv/custom",))
        fechamento, faltam = resolver(externos, nomes, classes)
        print(f"      ERP {versao} + {comp.name} + produtos DESENV -> "
              f"{len(fechamento)} arquivos ({len(faltam)} nomes sem dono)", flush=True)

        # FASE 3 — a unica passada que faz as arestas cruzarem. Os arquivos do cliente
        # vem do cache da fase 1, entao aqui se paga so pelo fechamento.
        alvos = sorted(set(arquivos_cliente) | fechamento)
        print(f"[3/4] extraindo customizacao + fechamento ({len(alvos)} arquivos)...", flush=True)
        t0 = time.time()
        resultado = extract(alvos, cache_root=out, root=workspace, parallel=not args.sequencial)
        print(f"      {len(resultado['nodes'])} nos, {len(resultado['edges'])} arestas em "
              f"{time.time() - t0:.0f}s", flush=True)

    # FASE 4 — grava no formato que o CLI do graphify le.
    grafo = out / "graphify-out" / "graph.json"
    grafo.write_text(
        json.dumps({"nodes": resultado["nodes"], "edges": resultado["edges"],
                    "hyperedges": [], "input_tokens": 0, "output_tokens": 0},
                   ensure_ascii=False),
        encoding="utf-8")
    # Ancora o corpus real: e o que faz os path:linha do relatorio abrirem no workspace.
    (out / "graphify-out" / ".graphify_root").write_text(str(workspace), encoding="utf-8")

    ref_ids = {n["id"] for n in resultado["nodes"] if str(n["id"]).startswith("ref")}
    em_stub = sum(1 for e in resultado["edges"] if e.get("target") in ref_ids)
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
