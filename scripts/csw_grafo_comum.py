"""Peças compartilhadas pelos geradores de grafo CSW (cliente e módulo padrão).

O que vive aqui é o que os dois modos fazem igual: varrer fontes IRIS, indexar nomes do
jeito que uma referência ObjectScript os escreve, e resolver o fechamento de dependência.

A ideia do fechamento: uma referência nomeia uma rotina/classe/include, nunca um caminho,
e o resolvedor de `graphify` casa esses nomes DEPOIS que o corpus inteiro foi extraído —
então arquivo fora da MESMA extração vira stub. Como o grafo já aponta o que faltou (os nós
`ref_iris_*`), dá para resolver esses nomes contra as árvores padrão e re-extrair só o
necessário, em vez de arrastar o ERP inteiro.
"""
from __future__ import annotations

import os
from pathlib import Path

SUFFIXES = (".mac", ".cls", ".inc")
# Fallback quando o pomcs.xml da versão não declara o release de `cswutil`.
COMPONENTES_PADRAO = {"7": "COMP-7.0", "8": "COMP-8.0"}


def coletar(raiz: Path, pular: tuple[str, ...] = ()) -> list[Path]:
    """Todos os fontes IRIS sob `raiz`, ignorando diretórios que casem `pular`."""
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
    """(rotinas/includes por nome canônico, classes pelo nome pontuado do path)."""
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
    """(arquivos que atendem os nomes externos, nomes que ninguém atende)."""
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
    """Nomes que o resolvedor não achou no escopo — a lista do que falta trazer."""
    return [
        str(n.get("label", "")) for n in resultado.get("nodes", [])
        if str(n.get("id", "")).startswith("ref") and n.get("label")
    ]


# ── versões e árvores ────────────────────────────────────────────────────────

def versoes_disponiveis(workspace: Path) -> list[str]:
    return sorted(
        d.name for d in workspace.iterdir()
        if d.is_dir() and d.name[:1].isdigit() and "." in d.name
    )


def releases_pomcs(pom: Path, sistema: str) -> list[tuple[int, ...]]:
    """Releases declarados para um `sistemaId` num pomcs.xml, como tuplas ordenáveis."""
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


def componentes_da_versao(workspace: Path, versao: str, pom_cliente: Path | None = None) -> str:
    """Pasta de componentes a usar, e quem manda quando as fontes discordam.

    Duas fontes declaram `cswutil`, e elas NÃO concordam: `7.5/csw75/pomcs.xml` pede
    `8.0.21` (o trunk do ERP é construído contra um release novo dos componentes),
    enquanto `DESENV/custom/gb/pomcs.xml` — o cliente que roda essa 7.5 — pede `7.0.130`.
    Misturar ERP 7.5 com componentes 8.0 seria errado em silêncio: os nomes de rotina
    existem nas duas árvores, então o fechamento "fecharia" com o conteúdo da versão
    errada e nada apareceria como falha.

    Por isso: a família da versão do ERP decide (7.x -> COMP-7.0, 8.x -> COMP-8.0, que é a
    convenção documentada no CLAUDE.md do workspace); o pomcs do CLIENTE, quando existe,
    tem precedência porque é o que descreve o que está implantado; e uma divergência sai
    na tela em vez de ser resolvida por chute.
    """
    familia = COMPONENTES_PADRAO.get(versao[:1], "COMP-7.0")
    escolhido = familia
    fonte = f"convenção de família ({versao[:1]}.x)"
    if pom_cliente is not None and pom_cliente.is_file():
        util = releases_pomcs(pom_cliente, "cswutil")
        if util:
            maior = max(util)
            escolhido = f"COMP-{maior[0]}.{maior[1]}"
            fonte = f"pomcs.xml do cliente (cswutil {'.'.join(map(str, maior))})"
    pom_erp = workspace / versao / f"csw{versao.replace('.', '')}" / "pomcs.xml"
    util_erp = releases_pomcs(pom_erp, "cswutil")
    if util_erp:
        maior_erp = max(util_erp)
        do_erp = f"COMP-{maior_erp[0]}.{maior_erp[1]}"
        if do_erp != escolhido:
            print(f"      ! a árvore {versao} declara cswutil "
                  f"{'.'.join(map(str, maior_erp))} ({do_erp}), mas vou usar {escolhido} "
                  f"pela {fonte}; passe --comp para forçar")
    return escolhido


def arvores_padrao(workspace: Path, versao: str, pom_cliente: Path | None = None,
                   comp: str | None = None) -> list[Path]:
    """Árvores onde uma referência do padrão pode morar, na ordem em que se procura.

    Três, não uma: o ERP (`csw81`), o core (`cswcore81` — árvore irmã pequena, mas é onde
    vivem as rotinas `DD*`) e os componentes (`COMP-x.y`).
    """
    vv = versao.replace(".", "")
    return [
        workspace / versao / f"csw{vv}",
        workspace / versao / f"cswcore{vv}",
        workspace / (comp or componentes_da_versao(workspace, versao, pom_cliente)),
    ]


def fechar(base, parcial, arvores, extrair, hops=1, pular=(), log=print):
    """Fecha o grafo por dependência, `hops` níveis, re-extraindo a cada nível.

    `base` são os arquivos do escopo (customização ou módulos), `parcial` o resultado de
    já tê-los extraído, `extrair(lista) -> resultado` a função de extração e `arvores` onde
    procurar o que faltou.

    Por que mais de um nível pode valer: o hop 1 traz o que o SEU código chama; essas
    rotinas trazidas chamam outras, que ficam em stub.

    Cada hop resolve as arestas que o nível anterior deixou pendentes, mas os arquivos que
    ele traz têm a própria fronteira — então o que melhora é a PROPORÇÃO, não a contagem
    absoluta. Medido no cliente GB: hop 1 = 949 arquivos e 4,7% das arestas em stub; hop 2
    = 2.038 arquivos e 2,9%, com o total de arestas passando de 94 mil para 221 mil. Ou
    seja: mais contexto real no grafo, com o stub virando uma fatia menor dele.

    O laço para sozinho quando um nível não acha arquivo novo, e o resíduo nunca vai a zero:
    sobram nomes que não existem em árvore alguma — os placeholders `*zzz` do framework e
    referências mortas no fonte.

    Retorna `(resultado, fechamento, faltam)`.
    """
    nomes, classes = indexar(arvores, pular)
    base = set(base)
    resultado = parcial
    fechamento: set = set()
    faltam: list[str] = []
    for hop in range(1, hops + 1):
        externos = externos_de(resultado)
        novos, faltam = resolver(externos, nomes, classes)
        novos -= base | fechamento
        log(f"      hop {hop}: {len(externos)} nomes externos -> {len(novos)} arquivos novos "
            f"({len(faltam)} nomes sem dono)")
        if not novos:
            break
        fechamento |= novos
        resultado = extrair(sorted(base | fechamento))
    return resultado, fechamento, faltam


# ── saída ────────────────────────────────────────────────────────────────────

def escrever_grafo(out: Path, resultado: dict, workspace: Path) -> Path:
    """Grava graph.json no formato que o CLI do graphify lê, + a âncora do corpus."""
    import json
    destino = out / "graphify-out"
    destino.mkdir(parents=True, exist_ok=True)
    grafo = destino / "graph.json"
    grafo.write_text(
        json.dumps({"nodes": resultado["nodes"], "edges": resultado["edges"],
                    "hyperedges": [], "input_tokens": 0, "output_tokens": 0},
                   ensure_ascii=False),
        encoding="utf-8")
    # `.graphify_root` aponta o corpus, não o diretório de saída: é o que faz os
    # path:linha do relatório abrirem no workspace (e o grafo poder ser movido).
    (destino / ".graphify_root").write_text(str(workspace), encoding="utf-8")
    return grafo


def contar_stubs(resultado: dict) -> int:
    ref = {n["id"] for n in resultado["nodes"] if str(n["id"]).startswith("ref")}
    return sum(1 for e in resultado["edges"] if e.get("target") in ref)
