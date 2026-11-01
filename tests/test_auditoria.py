"""Registro de auditoria das tools de escrita."""

import json
import os
import stat

import pytest

import mcp_seipro.server as srv
from mcp_seipro import auditoria


def _linhas(caminho):
    return [json.loads(l) for l in caminho.read_text().splitlines()]


def _tool(nome):
    return srv.mcp._tool_manager._tools[nome]


def test_so_tools_de_escrita_sao_auditadas():
    assert getattr(_tool("sei_assinar_documento").fn, "_auditada", False)
    assert getattr(_tool("sei_alterar_documento_interno").fn, "_auditada", False)
    assert not getattr(_tool("sei_ler_documento").fn, "_auditada", False)
    assert not getattr(_tool("sei_pesquisar_blocos_internos").fn, "_auditada", False)


@pytest.mark.asyncio
async def test_escrita_registra_autor_alvo_e_resultado(monkeypatch, tmp_path):
    destino = tmp_path / "aud.jsonl"
    monkeypatch.setenv("SEI_AUDITORIA_LOG", str(destino))
    monkeypatch.setattr(srv, "_autor_auditoria", lambda: {"usuario": "10924687738", "sessao": "abc"})

    class C:
        async def alterar_bloco_interno(self, id_bloco, descricao):
            return {"mensagem": "Bloco alterado"}

    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    await _tool("sei_alterar_bloco_interno").fn(id_bloco="655886", descricao="Novo nome", ctx=None)

    [ev] = _linhas(destino)
    assert ev["usuario"] == "10924687738"
    assert ev["tool"] == "sei_alterar_bloco_interno"
    assert ev["args"] == {"id_bloco": "655886", "descricao": "Novo nome"}
    assert ev["ok"] is True
    assert stat.S_IMODE(os.stat(destino).st_mode) == 0o600


@pytest.mark.asyncio
async def test_erro_da_tool_fica_registrado(monkeypatch, tmp_path):
    destino = tmp_path / "aud.jsonl"
    monkeypatch.setenv("SEI_AUDITORIA_LOG", str(destino))
    monkeypatch.setattr(srv, "_autor_auditoria", lambda: {"usuario": "u"})

    class C:
        async def alterar_bloco_interno(self, id_bloco, descricao):
            raise Exception("Bloco não encontrado")

    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    await _tool("sei_alterar_bloco_interno").fn(id_bloco="1", descricao="x", ctx=None)

    [ev] = _linhas(destino)
    assert ev["ok"] is False
    assert "Bloco não encontrado" in ev["resultado"]["erro"]


def test_conteudo_e_segredo_nao_entram():
    args = auditoria.resumir_argumentos({
        "id_documento": "1", "secoes": [{"conteudo": "<p>sigiloso</p>"}],
        "arquivo_base64": "A" * 5000, "senha": "x", "descricao": "d" * 500, "ctx": object(),
    })
    assert args["id_documento"] == "1"
    assert args["secoes"].startswith("<omitido")
    assert args["arquivo_base64"].startswith("<omitido")
    assert args["senha"].startswith("<omitido")
    assert "sigiloso" not in json.dumps(args)
    assert len(args["descricao"]) < 200
    assert "ctx" not in args


def test_desligada_nao_grava(monkeypatch, tmp_path):
    destino = tmp_path / "aud.jsonl"
    monkeypatch.setenv("SEI_AUDITORIA_LOG", str(destino))
    monkeypatch.setenv("SEI_AUDITORIA", "0")
    auditoria.registrar({"tool": "x"})
    assert not destino.exists()


def test_identidade_nao_expoe_sid():
    ident = auditoria.identificar("sid-secreto-123", "10924687738")
    assert ident["usuario"] == "10924687738"
    assert "sid-secreto" not in json.dumps(ident) and len(ident["sessao"]) == 12
