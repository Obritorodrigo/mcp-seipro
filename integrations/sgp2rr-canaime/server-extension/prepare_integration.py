"""Cria cópia de homologação do servidor com oito tools adicionais.

NÃO toca o arquivo em produção, NÃO reinicia serviços, NÃO roda o MCP.
Entrada e saída ficam exclusivamente na máquina do operador.
"""
from __future__ import annotations

import argparse
import ast
import py_compile
import shutil
from pathlib import Path

TOOL_NAMES = (
    "sgp_abrir_pagina", "sgp_seguir_link", "sgp_ver_parte",
    "sgp_enviar_formulario", "sgp_status",
)
FILES = (
    "sgp2rr_search.py", "sgp2rr_tools.py",
    "sgp2rr_integration.py", "sgp2rr_server_bridge.py",
)
MARKER = "# SGP2RR EXTENSAO MCP — adicionado por prepare_integration.py"


def preparar(source_file: Path, output_dir: Path, modules_dir: Path) -> Path:
    source_file = source_file.resolve()
    output_dir = output_dir.resolve()
    modules_dir = modules_dir.resolve()
    if not source_file.is_file() or source_file.name != "sgp2rr_mcp.py":
        raise ValueError("Arquivo fonte sgp2rr_mcp.py não encontrado")
    if source_file.parent == output_dir:
        raise ValueError("A pasta de homologação não pode ser a pasta de produção")
    if output_dir == source_file.parent or source_file.parent in output_dir.parents:
        # Permite staging separado no HOME, evita gravar no projeto original.
        raise ValueError("Não crie o staging dentro do diretório da produção")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError("A pasta de homologação deve estar vazia")
    if not all((modules_dir / f).is_file() for f in FILES):
        raise FileNotFoundError("Arquivos da extensão não estão disponíveis")

    original = source_file.read_text(encoding="utf-8")
    if MARKER in original:
        raise RuntimeError("Extensão já identificada no arquivo de origem")
    ast_tree = ast.parse(original, filename=str(source_file))
    funcs = {
        node.name: node for node in ast_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = ("atual", "renderizar", *TOOL_NAMES)
    missing = [name for name in required if name not in funcs]
    if missing:
        raise RuntimeError("Não foi possível confirmar as funções esperadas: " + ", ".join(missing))
    for name in TOOL_NAMES:
        node = funcs[name]
        if not any(
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and isinstance(dec.func.value, ast.Name)
            and dec.func.value.id == "mcp" and dec.func.attr == "tool"
            for dec in node.decorator_list
        ):
            raise RuntimeError("Decorador MCP não confirmado para " + name)
    # Verifica que a última ferramenta termina antes do início da execução
    # (sem pressupor se o projeto usa __main__, uvicorn ou outra inicialização).
    insert_line = max(funcs[name].end_lineno for name in TOOL_NAMES)
    for node in ast_tree.body:
        if isinstance(node, ast.If) and node.lineno > insert_line:
            if any(isinstance(n, ast.Name) and n.id == "__name__"
                   for n in ast.walk(node.test)):
                break
    else:
        raise RuntimeError("Ponto __main__ de inicialização não localizado; requer revisão manual")

    lines = original.splitlines(keepends=True)
    insert = (
        "\n" + MARKER + "\n"
        "from sgp2rr_server_bridge import registrar_sgp2rr_no_servidor as _registrar_sgp2rr\n"
        "_registrar_sgp2rr(mcp=mcp, atual=atual, renderizar=renderizar)\n"
        "# FIM SGP2RR EXTENSAO MCP\n"
    )
    patched = "".join(lines[:insert_line]) + insert + "".join(lines[insert_line:])
    ast.parse(patched, filename=str(source_file))

    # Se um erro acontecer antes daqui, nenhum arquivo é criado.
    output_dir.mkdir(parents=True, exist_ok=True)
    new_file = output_dir / "sgp2rr_mcp.py"
    new_file.write_text(patched, encoding="utf-8")
    for filename in FILES:
        shutil.copy2(modules_dir / filename, output_dir / filename)
    for filename in ("sgp2rr_mcp.py", *FILES):
        py_compile.compile(str(output_dir / filename), doraise=True)
    return new_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = preparar(args.source, args.output_dir, Path(__file__).resolve().parent)
    print("STAGING PRONTO:", result)
    print("Estrutura analisada e sintaxe compilada com sucesso.")
    print("A produção permanece intacta; nenhum serviço foi reiniciado.")
