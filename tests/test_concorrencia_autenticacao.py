"""Detecta autenticações simultâneas desnecessárias."""

import asyncio
import pytest

from mcp_seipro.sei_client import SEIClient


@pytest.mark.asyncio
async def test_duas_requisicoes_autenticam_uma_vez(monkeypatch):
    client = SEIClient(
        sei_url="https://sei.exemplo.gov.br",
        sei_usuario="teste",
        sei_senha="ficticia",
        sei_orgao="1",
        permitir_arquivo_local=False,
    )

    chamadas = 0

    async def autenticar_ficticio():
        nonlocal chamadas
        chamadas += 1
        await asyncio.sleep(0.02)
        client._token = "token-ficticio"
        return client._token

    monkeypatch.setattr(client, "autenticar", autenticar_ficticio)

    try:
        resultados = await asyncio.gather(
            client._get_headers(),
            client._get_headers(),
        )

        assert resultados == [
            {"token": "token-ficticio"},
            {"token": "token-ficticio"},
        ]

        assert chamadas == 1, (
            f"Autenticacao executada {chamadas} vezes"
        )
    finally:
        await client.close()
