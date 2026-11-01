"""Adaptador de integração da extensão SGP2RR ao MCP existente.

NÃO gerencia autenticação. obter_cliente_atual() deve entregar, em CADA
chamada MCP, as funções da sessão Canaimé da identidade OAuth atual.
Opera com navegação existente sem fazer outro login.

Não registrar termos de pesquisa, URLs, cookies ou arquivos PDF.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from sgp2rr_tools import registrar_ferramentas

BASE_HTTPS = "https://www.canaime.com.br/sgp2rr/"
PDF_PATH = re.compile(r"fotos/presos_doc/[A-Za-z0-9_-]{8,100}\.pdf\Z", re.I)
MAX_PDF_BYTES = 12 * 1024 * 1024
MAX_BODY_CHUNK = 64 * 1024

# Rotas estritamente de consulta; nenhum formulário de gravação é exposto.
READ_PAGES = {
    "areas/unidades/pesquisa.php",
    "areas/unidades/pesquisa_Iframe CODIGO.php",
    "areas/unidades/Ficha_Preso_index.php",
    "areas/unidades/Ficha_Preso_index_SitAtual.php",
    "areas/unidades/HistCar_LER.php",
    "areas/impressoes/UND_CertidaoCarceraria.php",
}
READ_PARAMETERS = {
    "areas/unidades/pesquisa.php": set(),
    "areas/unidades/pesquisa_Iframe CODIGO.php": set(),
    "areas/unidades/Ficha_Preso_index.php": {"id_cad_preso"},
    "areas/unidades/Ficha_Preso_index_SitAtual.php": {"id_cad_preso"},
    "areas/unidades/HistCar_LER.php": {"id_cad_preso", "pageNum_rsHistcar"},
    "areas/impressoes/UND_CertidaoCarceraria.php": {"id_cad_preso"},
}


def _url_consulta_permitida(caminho: str) -> None:
    """Rejeita hosts externos, traversal, parâmetros inesperados e mutações."""
    if not isinstance(caminho, str) or not caminho or len(caminho) > 320:
        raise ValueError("Caminho de consulta inválido")
    if caminho.startswith("/") or "://" in caminho or "#" in caminho or "\\" in caminho:
        raise ValueError("Acesso fora das rotas de leitura permitidas")
    parsed = urlsplit(caminho)
    raw = unquote(parsed.path)
    if raw not in READ_PAGES or ".." in raw or "%" in raw:
        raise ValueError("Rota fora da lista de consultas permitidas")
    if raw != "areas/unidades/pesquisa_Iframe CODIGO.php" and " " in raw:
        raise ValueError("Caminho inválido")
    try:
        params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError("Parâmetros inválidos") from exc
    if not set(params).issubset(READ_PARAMETERS[raw]):
        raise ValueError("Parâmetro não permitido")
    for name, values in params.items():
        if len(values) != 1:
            raise ValueError("Parâmetro duplicado")
        max_len = 9 if name == "id_cad_preso" else 2
        if not re.fullmatch(r"\d{1," + str(max_len) + r"}", values[0]):
            raise ValueError("Parâmetro numérico inválido")
    if raw not in {"areas/unidades/pesquisa.php",
                   "areas/unidades/pesquisa_Iframe CODIGO.php"}:
        if "id_cad_preso" not in params:
            raise ValueError("Código do cadastro obrigatório")


@dataclass(frozen=True)
class ClienteAutenticado:
    """Operações da sessão da requisição atual, nunca objetos globais.

    abrir e enviar devem reproduzir o texto das ferramentas existentes.
    sessao_http: objeto requests.Session autenticado desse usuário (opcional).
    """
    abrir: Callable[[str], Any]
    enviar: Callable[[int, dict[str, str], str], Any]
    sessao_http: Any | None = None


def _texto(resultado: Any) -> str:
    if isinstance(resultado, str):
        return resultado
    if isinstance(resultado, dict):
        candidate = resultado.get("result")
        if isinstance(candidate, str):
            return candidate
    raise RuntimeError("Ferramenta de navegação não retornou texto válido")


class AdaptadorSGP2RR:
    """Implementa sgp2rr_tools com operações da sessão isolada."""

    def __init__(self, cliente: ClienteAutenticado):
        if not isinstance(cliente, ClienteAutenticado):
            raise RuntimeError("Cliente autenticado da requisição não disponível")
        if not callable(cliente.abrir) or not callable(cliente.enviar):
            raise RuntimeError("Operações autenticadas não disponíveis")
        self.cliente = cliente
        self._formulario_atual: str | None = None

    def abrir_pagina(self, caminho: str) -> str:
        _url_consulta_permitida(caminho)
        self._formulario_atual = None
        resposta = _texto(self.cliente.abrir(caminho))
        normalized = caminho.split("?", 1)[0]
        if normalized == "areas/unidades/pesquisa.php":
            self._formulario_atual = "pesquisa"
        elif normalized == "areas/unidades/pesquisa_Iframe%20CODIGO.php":
            self._formulario_atual = "codigo"
        return resposta

    def enviar_formulario(
        self, formulario: int, campos: dict[str, str], botao: str
    ) -> str:
        if formulario != 1 or not isinstance(campos, dict):
            raise ValueError("Somente o formulário de pesquisa GET é permitido")
        if self._formulario_atual == "pesquisa":
            valid = (
                botao == "Pesquisar"
                and set(campos) == {"busca1", "busca2", "busca3"}
                and all(isinstance(campos[k], str) for k in campos)
            )
        elif self._formulario_atual == "codigo":
            valid = (
                botao == "Abrir Ficha"
                and set(campos) == {"id_cad_preso"}
                and isinstance(campos["id_cad_preso"], str)
                and bool(re.fullmatch(r"[1-9]\d{0,8}", campos["id_cad_preso"]))
            )
        else:
            valid = False
        if not valid:
            raise PermissionError("Operação fora dos formulários de leitura autorizados")
        self._formulario_atual = None
        return _texto(self.cliente.enviar(formulario, campos, botao))

    def baixar_anexo(self, caminho: str) -> tuple[bytes, str]:
        """Baixa PDF pela sessão atual, sem redirecionamentos entre hosts."""
        if not isinstance(caminho, str) or not PDF_PATH.fullmatch(caminho):
            raise ValueError("Caminho PDF não permitido")
        sessao = self.cliente.sessao_http
        if sessao is None or not callable(getattr(sessao, "get", None)):
            raise RuntimeError(
                "A sessão HTTP autenticada ainda não foi integrada para leitura de PDFs"
            )
        resposta = sessao.get(
            BASE_HTTPS + caminho, timeout=(5, 25), stream=True,
            allow_redirects=False,
        )
        try:
            if resposta.status_code != 200:
                raise RuntimeError("Anexo não disponível na sessão autenticada")
            mime = resposta.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if mime != "application/pdf":
                raise RuntimeError("O servidor não retornou um PDF")
            length = resposta.headers.get("Content-Length")
            if length and length.isdecimal() and int(length) > MAX_PDF_BYTES:
                raise ValueError("PDF maior que 12 MiB")
            chunks: list[bytes] = []
            size = 0
            for piece in resposta.iter_content(chunk_size=MAX_BODY_CHUNK):
                if not piece:
                    continue
                size += len(piece)
                if size > MAX_PDF_BYTES:
                    raise ValueError("PDF maior que 12 MiB")
                chunks.append(piece)
            data = b"".join(chunks)
            if not data.startswith(b"%PDF-"):
                raise ValueError("Arquivo não possui assinatura PDF válida")
            return data, mime
        finally:
            resposta.close()


def integrar_sgp2rr(
    mcp: Any, obter_cliente_atual: Callable[[], ClienteAutenticado]
) -> None:
    """Registra oito ferramentas no MESMO FastMCP durante a inicialização.

    A fábrica só será chamada dentro da execução da tool MCP. Ela deve resolver
    a identidade OAuth vigente e recuperar a sessão daquela identidade.
    """
    if not callable(obter_cliente_atual):
        raise TypeError("A fábrica de clientes da requisição é obrigatória")

    def backend_factory() -> AdaptadorSGP2RR:
        return AdaptadorSGP2RR(obter_cliente_atual())

    registrar_ferramentas(mcp, backend_factory)
