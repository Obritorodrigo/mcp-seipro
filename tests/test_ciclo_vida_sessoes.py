"""Testes locais de ciclo de vida dos clientes OAuth."""

import pytest
import mcp_seipro.server as srv


class ClienteFicticio:
    def __init__(self):
        self.fechamentos = 0

    async def close(self):
        self.fechamentos += 1


@pytest.mark.asyncio
async def test_lifespan_encerra_clientes_rest_e_web(monkeypatch):
    monkeypatch.setattr(srv, "_http_mode", True)

    rest_a = ClienteFicticio()
    rest_b = ClienteFicticio()
    web_a = ClienteFicticio()
    web_b = ClienteFicticio()

    async with srv.lifespan(srv.mcp) as estado:
        estado["sei"]["sid-A"] = rest_a
        estado["sei"]["sid-B"] = rest_b
        estado["sei_web"]["sid-A"] = web_a
        estado["sei_web"]["sid-B"] = web_b

        assert rest_a.fechamentos == 0
        assert rest_b.fechamentos == 0

    assert rest_a.fechamentos == 1
    assert rest_b.fechamentos == 1
    assert web_a.fechamentos == 1
    assert web_b.fechamentos == 1


@pytest.mark.asyncio
async def test_lifespan_nao_compartilha_dicionarios(monkeypatch):
    monkeypatch.setattr(srv, "_http_mode", True)

    async with srv.lifespan(srv.mcp) as estado:
        assert estado["sei"] is not estado["sei_web"]
        assert estado["sei"] == {}
        assert estado["sei_web"] == {}
