"""Ferramentas especializadas de LEITURA para o servidor MCP SGP2RR.

Integração por injeção de dependência: nenhuma sessão ou credencial global é usada.
O servidor hospedado deve fornecer backend_factory() com cliente autenticado
da REQUISIÇÃO ATUAL. Não funciona isoladamente sem esse adaptador.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlsplit

BASE = "areas/unidades/"
SEARCH = BASE + "pesquisa.php"
RESULT = BASE + "pesquisa_resultadoVULGO.php"
HISTORY = BASE + "HistCar_LER.php"
CERTIFICATE = "areas/impressoes/UND_CertidaoCarceraria.php"
FICHA = BASE + "Ficha_Preso_index.php"
SITUATION = BASE + "Ficha_Preso_index_SitAtual.php"

# Apenas caminhos de documentos exibidos pelo histórico e dentro da sessão autenticada.
PDF_PATH = re.compile(r"^fotos/presos_doc/[A-Za-z0-9_-]{8,100}\.pdf$", re.I)
PDF_LINK = re.compile(r"(?m)^\[\d+\].*?→\s*(fotos/presos_doc/[A-Za-z0-9_-]+\.pdf)\s*$", re.I)
PAGE_NUM = re.compile(r"pageNum_rsHistcar=(\d+)")
ROW = re.compile(
    r"(?m)^\|\s*UND\s*\|\s*([^|]*)\|\s*DATA\s*\|\s*(\d{2}/\d{2}/\d{4})"
    r"\s*\|\s*CONDUTA\s*\|\s*([^|]*)\|"
)
MAX_PDF_BYTES = 12 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_TEXT_CHARS = 12000
UNITS = {"", "CABV", "CME", "CPBV", "CPFBV", "CPP", "CPSLA", "DESIPE",
         "DICAP", "FAZ ESP", "PAMC", "UPRRO"}


class Backend(Protocol):
    """Implementar usando a sessão autenticada que o servidor já gerencia."""

    def abrir_pagina(self, caminho: str) -> str: ...
    def enviar_formulario(
        self, formulario: int, campos: dict[str, str], botao: str
    ) -> str: ...
    def baixar_anexo(self, caminho: str) -> tuple[bytes, str]: ...


def _codigo(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > 999999999:
        raise ValueError("Código de cadastro inválido")
    return value


def _nome(value: str) -> str:
    value = " ".join(value.strip().split())
    if not 3 <= len(value) <= 120 or re.search(r"[\x00-\x1f<>&?=#/\\]", value):
        raise ValueError("Informe um nome válido (3 a 120 caracteres)")
    return value


def _field(source: str, label: str) -> str | None:
    # Trabalha com o texto gerado pelo atual sgp_abrir_pagina, sem depender de HTML bruto.
    m = re.search(rf"(?mi)^{re.escape(label)}\s*:?\s*\n([^\n]+)", source)
    return m.group(1).strip() if m else None


def _only_text(page: str) -> str:
    return page.split("## Texto\n", 1)[-1].split("\n## Tabelas", 1)[0]


def _redact(text: str) -> str:
    # Evita devolver endereços, contatos, documentos ou links em resumos do histórico.
    text = re.sub(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", "[DOCUMENTO]", text)
    text = re.sub(r"(?i)\b(?:telefone|celular|CPF|CNPJ|endereço)\s*:?\s*[^\n]+", "[DADO SUPRIMIDO]", text)
    text = re.sub(r"https?://\S+", "[LINK]", text)
    return text[:900]


@dataclass
class ServicoSGP:
    backend: Backend

    def pesquisar_reeducando(self, nome: str, unidade: str = "") -> dict:
        nome = _nome(nome)
        unidade = unidade.upper().strip()
        if unidade not in UNITS:
            raise ValueError("Unidade não reconhecida")
        self.backend.abrir_pagina(SEARCH)
        page = self.backend.enviar_formulario(
            1, {"busca1": "nome", "busca2": nome, "busca3": unidade}, "Pesquisar"
        )
        items = []
        for code, full_name in re.findall(r"(?m)^ID\s+(\d+)\s*\n([^\n]+)", page):
            items.append({"codigo": int(code), "nome": full_name.strip()})
        match = re.search(r"(\d+)\s+REEDUCANDO\(S\)\s+CADASTRADO\(S\)", page)
        return {"busca": nome, "unidade": unidade or None,
                "total_informado": int(match.group(1)) if match else None,
                "resultados": items[:30], "limitado": len(items) > 30}

    def consultar_ficha(self, codigo: int) -> dict:
        codigo = _codigo(codigo)
        main = self.backend.abrir_pagina(f"{FICHA}?id_cad_preso={codigo}")
        status = self.backend.abrir_pagina(f"{SITUATION}?id_cad_preso={codigo}")
        m = re.search(r"(?m)^Unidade\s*\n([^\n]+)\s*\nAla\s*\n([^\n]+)(?:\s*\n(\d+))?", main)
        raw_status = _only_text(status)
        current = next((line.strip() for line in raw_status.splitlines()
                        if line.strip() in {"EM CUMPRIMENTO DE PENA", "LIBERADO", "FORAGIDO",
                                            "TRANSFERIDO", "ALVARÁ DE SOLTURA"}), None)
        return {
            "codigo": codigo,
            "nome": _field(main, "Nome"),
            "unidade": m.group(1).strip() if m else None,
            "ala": (" ".join(filter(None, [m.group(2).strip(), m.group(3)]))
                    if m else None),
            "situacao": current,
            "fonte": "ficha principal e situação atual",
        }

    def _historico_paginas(self, codigo: int) -> list[str]:
        codigo = _codigo(codigo)
        first = self.backend.abrir_pagina(f"{HISTORY}?id_cad_preso={codigo}")
        pages = [first]
        indices = sorted({int(v) for v in PAGE_NUM.findall(first) if int(v) <= 20})
        # Não ultrapassar o limite para evitar coleta indiscriminada.
        for idx in indices[:9]:
            page = self.backend.abrir_pagina(
                f"{HISTORY}?pageNum_rsHistcar={idx}&id_cad_preso={codigo}"
            )
            if page not in pages:
                pages.append(page)
        return pages

    def consultar_historico(self, codigo: int) -> dict:
        pages = self._historico_paginas(codigo)
        registros = []
        seen = set()
        for page in pages:
            for unidade, data, conduta in ROW.findall(page):
                key = (unidade.strip(), data, conduta.strip())
                if key not in seen:
                    registros.append({"unidade": key[0] or None, "data": data,
                                      "conduta": key[2] or None})
                    seen.add(key)
        # Não devolve, por padrão, narrativas completas e dados de terceiros.
        return {"codigo": _codigo(codigo), "registros": registros,
                "total_identificado": len(registros),
                "observacao": "Resumo de datas e conduta, sem textos integrais."}

    def consultar_certidao(self, codigo: int) -> dict:
        codigo = _codigo(codigo)
        page = self.backend.abrir_pagina(f"{CERTIFICATE}?id_cad_preso={codigo}")
        info = _only_text(page)
        if not re.search(r"CERTIDÃO CARCERÁRIA", info, re.I):
            raise RuntimeError("Certidão carcerária não localizada na página")
        record = re.search(r"CERTIDÃO CARCERÁRIA\s+N[º°]\s*(\d+)", info, re.I)
        proc = re.search(r"(?m)^PROCESSO\s*\nPENA IMPOSTA\s*\nREGIME\s*\n([^\n]+)\n([^\n]+)\n([^\n]+)", info)
        dates = list(dict.fromkeys(re.findall(r"\b\d{2}/\d{2}/\d{4}\b", info)))
        return {
            "codigo": codigo,
            "numero_certidao": record.group(1) if record else None,
            "nome": _field(info, "Reeducando"),
            "situacao_atual": _field(info, "Situação Atual"),
            "conduta_atual": _field(info, "Conduta Atual"),
            "processo": proc.group(1) if proc else None,
            "pena_informada": proc.group(2) if proc else None,
            "regime": proc.group(3) if proc else None,
            "datas_mencionadas": dates[:40],
            "nota": "Leitura do documento apresentado pelo sistema; não comprova assinatura.",
        }

    def _anexos(self, codigo: int) -> list[str]:
        paths: list[str] = []
        for page in self._historico_paginas(codigo):
            for path in PDF_LINK.findall(page):
                if PDF_PATH.fullmatch(path) and path not in paths:
                    paths.append(path)
        return paths[:30]

    def listar_anexos(self, codigo: int) -> dict:
        arquivos = self._anexos(codigo)
        return {"codigo": _codigo(codigo), "total": len(arquivos),
                "anexos": [{"indice": i, "formato": "PDF"} for i in range(1, len(arquivos)+1)],
                "observacao": "Índices valem para a consulta atual. Nenhum arquivo foi baixado."}

    def ler_anexo(self, codigo: int, indice: int, paginas: int = 5) -> dict:
        codigo = _codigo(codigo)
        if not isinstance(indice, int) or indice < 1:
            raise ValueError("Índice de anexo inválido")
        if not isinstance(paginas, int) or paginas < 1 or paginas > MAX_PDF_PAGES:
            raise ValueError(f"Informe de 1 a {MAX_PDF_PAGES} páginas")
        paths = self._anexos(codigo)
        if indice > len(paths):
            raise ValueError("Anexo não encontrado para este cadastro")
        path = paths[indice - 1]
        if not PDF_PATH.fullmatch(path):
            raise ValueError("Formato ou caminho do anexo não permitido")
        data, mime = self.backend.baixar_anexo(path)
        if len(data) > MAX_PDF_BYTES:
            raise ValueError("O arquivo excede o limite permitido de 12 MiB")
        if not data.startswith(b"%PDF-") or not mime.lower().startswith("application/pdf"):
            raise ValueError("O anexo não foi validado como PDF")
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data), strict=False)
            count = len(reader.pages)
            chunks = []
            for p in range(min(count, paginas)):
                chunks.append(reader.pages[p].extract_text() or "")
            combined = "\n".join(chunks).strip()[:MAX_TEXT_CHARS]
        except Exception as exc:
            # Não registrar dados internos, respostas ou bytes sensíveis.
            raise RuntimeError("Falha na extração do PDF; verifique o arquivo no sistema") from exc
        return {
            "codigo": codigo,
            "indice": indice,
            "paginas_total": count,
            "paginas_lidas": min(count, paginas),
            "texto": _redact(combined) if combined else "",
            "observacao": ("PDF possivelmente digitalizado; OCR não implementado."
                           if not combined else "Texto extraído das primeiras páginas; resumido."),
        }


def registrar_ferramentas(mcp: object, backend_factory: Callable[[], Backend]) -> None:
    """Acoplar ao MCP existente, com backend_factory isolado pela sessão OAuth.

    O método decorador .tool() é compatível com FastMCP; verificar a assinatura
    na versão do servidor antes do deploy. Não chamar esta função duas vezes.
    """
    def service() -> ServicoSGP:
        return ServicoSGP(backend_factory())

    @mcp.tool()
    def sgp_pesquisar_reeducando(nome: str, unidade: str = "") -> dict:
        """Pesquisa por nome completo/parcial, com filtro opcional de unidade."""
        return service().pesquisar_reeducando(nome, unidade)

    @mcp.tool()
    def sgp_consultar_ficha(codigo: int) -> dict:
        """Consulta resumo não sensível da ficha, unidade, ala e situação."""
        return service().consultar_ficha(codigo)

    @mcp.tool()
    def sgp_consultar_historico(codigo: int) -> dict:
        """Lista datas e condutas dos registros históricos do cadastro."""
        return service().consultar_historico(codigo)

    @mcp.tool()
    def sgp_consultar_certidao(codigo: int) -> dict:
        """Lê resumo da certidão carcerária, sem reproduzir dados pessoais."""
        return service().consultar_certidao(codigo)

    @mcp.tool()
    def sgp_listar_anexos(codigo: int) -> dict:
        """Lista os índices dos anexos PDF presentes no histórico carcerário."""
        return service().listar_anexos(codigo)

    @mcp.tool()
    def sgp_ler_anexo(codigo: int, indice: int, paginas: int = 5) -> dict:
        """Lê PDF autorizado do histórico, com limites de tamanho e páginas."""
        return service().ler_anexo(codigo, indice, paginas)
