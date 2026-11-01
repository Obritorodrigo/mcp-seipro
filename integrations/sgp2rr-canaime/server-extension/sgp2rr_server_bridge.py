"""Ponte para sgp2rr_mcp.py; usa a classe Sessao e as funções reais do servidor.

Observações confirmadas por inspeção AST do servidor em 2026-11-01:
* atual() resolve a sessão pelo token OAuth da requisição MCP;
* Sessao.client é httpx.Client, com cookies individuais;
* Sessao.carregar("GET", sessao._url(...)) retorna Pagina;
* renderizar(Pagina) produz a representação que as ferramentas usam;
* mcp.tool() registra ferramentas em MCPServer.

A ponte não possui credenciais e não muda as cinco ferramentas originais.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable

from sgp2rr_integration import ClienteAutenticado, integrar_sgp2rr


class _HttpxComLock:
    """Conserva o lock da sessão durante leitura de PDFs em streaming."""

    def __init__(self, sessao: Any):
        self._sessao = sessao

    @contextmanager
    def stream(self, method: str, url: str, **kwargs):
        if method != "GET":
            raise PermissionError("Apenas leitura GET autorizada")
        with self._sessao.lock:
            # O cliente já possui os cookies do usuário que atual() devolveu.
            with self._sessao.client.stream(method, url, **kwargs) as resposta:
                yield resposta


def registrar_sgp2rr_no_servidor(
    mcp: Any, atual: Callable[[], Any], renderizar: Callable[..., str]
) -> None:
    """Chamar uma vez na inicialização, antes da execução do servidor MCP.

    atual() SOMENTE é chamado DENTRO de cada execução de ferramenta:
    assim os contextos de token OAuth não são capturados no startup.
    """

    def obter_cliente_atual() -> ClienteAutenticado:
        sessao = atual()
        if sessao is None or not sessao.logado:
            raise PermissionError("Sessão Canaimé não autenticada")

        def abrir(caminho: str) -> str:
            # A allowlist das páginas e dos formulários GET está no adaptador.
            with sessao.lock:
                try:
                    pag = sessao.carregar("GET", sessao._url(caminho))
                    return renderizar(pag)
                except Exception:
                    # Evita devolver query strings com CPF/endereço nos erros.
                    raise RuntimeError("Falha na leitura autenticada do Canaimé") from None

        return ClienteAutenticado(
            abrir=abrir,
            sessao_http=_HttpxComLock(sessao),
        )

    integrar_sgp2rr(mcp, obter_cliente_atual)
