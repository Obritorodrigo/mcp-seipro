"""Testes do SQLite e da não reutilização de refresh após reinício lógico."""
import asyncio
import os
import stat
import time

import pytest
from mcp.server.auth.provider import TokenError
import mcp_seipro.auth as auth
from mcp_seipro import revogacoes


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "S" * 48)
    auth._refresh_usados.clear()
    auth._sessoes_revogadas.clear()


def test_sqlite_novo_permissao_600():
    assert not revogacoes.DB_PATH.exists()
    assert not revogacoes.esta_revogada("novo-sid")
    assert stat.S_IMODE(os.stat(revogacoes.DB_PATH).st_mode) == 0o600


def test_consumo_refresh_atomico():
    assert revogacoes.consumir_refresh("jti-test", "sid-test", time.time()+100)
    assert not revogacoes.consumir_refresh("jti-test", "sid-test", time.time()+100)
    assert revogacoes.usado_em_refresh("jti-test") is not None


def test_refresh_nao_reutilizado_apos_limpar_estado_memoria():
    p = auth.SEIProOAuthProvider()
    sei = {"sei_url": "https://example.invalid", "sei_usuario": "teste", "sei_senha": "ficticia", "sei_orgao": "0"}
    tokens = auth._emitir(sei, "cliente-falso", [], "sid-restart", time.time())
    from mcp.shared.auth import OAuthClientInformationFull
    cliente = OAuthClientInformationFull(client_id="cliente-falso",client_secret="segredo",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],token_endpoint_auth_method="client_secret_post")
    rt = run(p.load_refresh_token(cliente, tokens.refresh_token))
    assert rt is not None
    run(p.exchange_refresh_token(cliente, rt, []))
    auth._refresh_usados.clear()  # simulação de reinício do processo
    assert run(p.load_refresh_token(cliente, tokens.refresh_token)) is None
    with pytest.raises(TokenError):
        run(p.exchange_refresh_token(cliente, rt, []))


def test_refresh_concorrente_somente_um_vencedor():
    p=auth.SEIProOAuthProvider()
    sei={"sei_url":"https://example.invalid","sei_usuario":"teste","sei_senha":"ficticia","sei_orgao":"0"}
    tokens=auth._emitir(sei,"cliente-2",[],"sid-concorrente",time.time())
    from mcp.shared.auth import OAuthClientInformationFull
    cliente=OAuthClientInformationFull(client_id="cliente-2",client_secret="segredo",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],token_endpoint_auth_method="client_secret_post")
    async def verificar():
        rt=await p.load_refresh_token(cliente,tokens.refresh_token)
        async def tentativa():
            try:
                await p.exchange_refresh_token(cliente,rt,[])
                return True
            except TokenError:
                return False
        return await asyncio.gather(tentativa(),tentativa())
    assert sorted(run(verificar())) == [False,True]


def test_caminho_configuravel_e_pasta_criada(monkeypatch, tmp_path):
    destino = tmp_path / "subpasta" / "nova" / "rev.sqlite3"
    monkeypatch.setattr(revogacoes, "DB_PATH", destino)

    revogacoes.revogar("sid-x", time.time() + 100)

    assert destino.exists()
    assert stat.S_IMODE(os.stat(destino).st_mode) == 0o600
    assert revogacoes.esta_revogada("sid-x")


def test_variavel_de_ambiente_define_caminho(monkeypatch, tmp_path):
    import importlib

    destino = tmp_path / "env.sqlite3"
    monkeypatch.setenv("SEI_REVOGACOES_DB", str(destino))
    modulo = importlib.reload(revogacoes)
    try:
        assert modulo.DB_PATH == destino
    finally:
        monkeypatch.delenv("SEI_REVOGACOES_DB")
        importlib.reload(revogacoes)
