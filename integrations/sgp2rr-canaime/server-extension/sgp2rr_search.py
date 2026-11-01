"""Pesquisa especializada no SGP2RR: apenas formulários GET de leitura.

Recebe backend autenticado por requisição; não gerencia cookies ou credenciais.
O formulário principal tem 12 opções e o iframe de código usa outro GET.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

FORMULARIO = "areas/unidades/pesquisa.php"
FORMULARIO_CODIGO = "areas/unidades/pesquisa_Iframe%20CODIGO.php"
FICHA = "areas/unidades/Ficha_Preso_index.php"

CRITERIOS = {
    "nome": "Nome",
    "vulgo": "Vulgo",
    "mae": "Nome da mãe",
    "pai": "Nome do pai",
    "pais_orig": "País de origem",
    "rg": "RG",
    "cpf": "CPF",
    "telefone": "Telefone",
    "celular": "Celular",
    "endereco": "Endereço",
    "bairro": "Bairro",
    "foto": "Foto",
    "codigo": "Código do cadastro",
}
UNIDADES = ("CABV", "CME", "CPBV", "CPFBV", "CPP", "CPSLA",
            "DESIPE", "DICAP", "FAZ ESP", "PAMC", "UPRRO")
ALIAS = {
    "mae": "mae", "nome da mae": "mae",
    "pai": "pai", "nome do pai": "pai",
    "pais de origem": "pais_orig", "pais origem": "pais_orig",
    "endereco": "endereco", "logradouro": "endereco",
    "codigo": "codigo", "id": "codigo", "matricula": "codigo",
    "apelido": "vulgo",
}
SIGILOSOS = {"cpf", "rg", "telefone", "celular", "endereco", "bairro"}
RESULTADO = re.compile(r"(?m)^ID\s+(\d+)\s*\n([^\n]+)")
TOTAL = re.compile(r"(\d+)\s+REEDUCANDO\(S\)\s+CADASTRADO\(S\)", re.I)
CODIGO_NA_FICHA = re.compile(r"(?m)^Código\s*\n(\d+)\s*$")
NOME_NA_FICHA = re.compile(r"(?m)^Nome\s*\n([^\n]+)")
FICHA_LINK = re.compile(r"(?:Ficha_Preso_index|menu_cadastro)\.php\?id_cad_preso=(\d+)")


class BackendPesquisa(Protocol):
    def abrir_pagina(self, caminho: str) -> str: ...
    def enviar_formulario(self, formulario: int, campos: dict[str, str],
                         botao: str) -> str: ...


def _normalizar_chave(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("Critério inválido")
    raw = " ".join(raw.strip().casefold().split())
    raw = "".join(ch for ch in unicodedata.normalize("NFD", raw)
                  if unicodedata.category(ch) != "Mn")
    return ALIAS.get(raw, raw)


def _codigo(raw: str | int) -> int:
    if isinstance(raw, bool):
        raise ValueError("Código inválido")
    value = str(raw).strip()
    if not re.fullmatch(r"[0-9]{1,9}", value) or int(value) == 0:
        raise ValueError("Código deve conter de 1 a 9 dígitos")
    return int(value)


def _validar_valor(chave: str, valor: str) -> str:
    if not isinstance(valor, str):
        raise ValueError("Informe o valor como texto")
    valor = " ".join(valor.strip().split())
    if not valor or len(valor) > 120 or any(ord(c) < 32 for c in valor):
        raise ValueError("Valor de pesquisa obrigatório (até 120 caracteres)")
    if chave == "cpf":
        digits = re.sub(r"[.\-\s]", "", valor)
        if not re.fullmatch(r"\d{11}", digits):
            raise ValueError("CPF precisa conter exatamente 11 dígitos")
        return digits
    if chave in {"telefone", "celular"}:
        digits = re.sub(r"[()\-\s+]", "", valor)
        if not re.fullmatch(r"\d{8,13}", digits):
            raise ValueError("Número de telefone inválido")
        return digits
    if chave == "rg" and not re.fullmatch(r"[\w.\/-]{2,30}", valor, re.UNICODE):
        raise ValueError("RG com formato inválido")
    # Texto deve ser tratado como VALOR pelo backend HTTP; nunca interpolar em URL.
    return valor


@dataclass
class PesquisaSGP:
    backend: BackendPesquisa

    @staticmethod
    def criterios_disponiveis() -> dict:
        return {
            "criterios": [{"codigo": key, "descricao": value}
                          for key, value in CRITERIOS.items()],
            "unidades": list(UNIDADES),
            "observacoes": [
                "Código abre ficha por formulário GET independente; não aceita unidade.",
                "O critério foto aparece no formulário, mas seu comportamento deve ser homologado.",
                "Filtros de unidade disponíveis para os 12 critérios do formulário geral.",
            ],
        }

    def buscar(self, criterio: str, valor: str, unidade: str = "",
              limite: int = 30) -> dict:
        chave = _normalizar_chave(criterio)
        if chave not in CRITERIOS:
            raise ValueError("Critério indisponível. Consulte sgp_criterios_pesquisa")
        if isinstance(limite, bool) or not isinstance(limite, int) or not 1 <= limite <= 30:
            raise ValueError("Limite deve ser entre 1 e 30")
        if not isinstance(unidade, str):
            raise ValueError("Unidade inválida")
        unidade = unidade.upper().strip()
        if unidade and unidade not in UNIDADES:
            raise ValueError("Unidade não consta no formulário do Canaimé")
        if chave == "codigo":
            if unidade:
                raise ValueError("Pesquisa por código não oferece filtro de unidade")
            return self._buscar_codigo(valor)

        valor = _validar_valor(chave, valor)
        form = self.backend.abrir_pagina(FORMULARIO)
        if "[F1] GET pesquisa_resultadoVULGO.php" not in form:
            raise RuntimeError("Formulário de pesquisa não está disponível")
        if f"{chave}=" not in form:
            raise RuntimeError("Critério não confirmado no formulário da sessão")
        page = self.backend.enviar_formulario(
            1, {"busca1": chave, "busca2": valor, "busca3": unidade}, "Pesquisar"
        )
        # Identificadores e nomes apenas, sem reproduzir outros campos do HTML.
        matches = []
        seen = set()
        for codigo, nome in RESULTADO.findall(page):
            if codigo not in seen and re.fullmatch(r"[0-9]{1,9}", codigo):
                matches.append({"codigo": int(codigo), "nome": nome.strip()})
                seen.add(codigo)
        counted = TOTAL.search(page)
        total = int(counted.group(1)) if counted else None
        return {
            "criterio": chave,
            "consulta": "[valor omitido]" if chave in SIGILOSOS else valor,
            "unidade": unidade or None,
            "total_informado": total,
            "resultados": matches[:limite],
            "limite": limite,
            "resultado_parcial": (len(matches) > limite or
                                  (total is not None and total > len(matches))),
            "nota": "Consulta da página de resultados atual; nenhuma alteração foi realizada.",
        }

    def _buscar_codigo(self, valor: str) -> dict:
        codigo = _codigo(valor)
        form = self.backend.abrir_pagina(FORMULARIO_CODIGO)
        if "[F1] GET Ficha_Menu.php" not in form or "id_cad_preso (text)" not in form:
            raise RuntimeError("Formulário de pesquisa por código não disponível")
        opened = self.backend.enviar_formulario(
            1, {"id_cad_preso": str(codigo)}, "Abrir Ficha"
        )
        # Não tomar uma página de erro por cadastro encontrado.
        linked_ids = set(FICHA_LINK.findall(opened))
        if str(codigo) not in linked_ids:
            return {
                "criterio": "codigo", "consulta": str(codigo), "unidade": None,
                "total_informado": 0, "resultados": [], "resultado_parcial": False,
                "nota": "O formulário não confirmou uma ficha para esse código.",
            }
        ficha = self.backend.abrir_pagina(f"{FICHA}?id_cad_preso={codigo}")
        codigo_ficha = CODIGO_NA_FICHA.search(ficha)
        nome = NOME_NA_FICHA.search(ficha)
        if not codigo_ficha or int(codigo_ficha.group(1)) != codigo or not nome:
            raise RuntimeError("Identificação da ficha não pôde ser confirmada")
        return {
            "criterio": "codigo", "consulta": str(codigo), "unidade": None,
            "total_informado": 1,
            "resultados": [{"codigo": codigo, "nome": nome.group(1).strip()}],
            "resultado_parcial": False,
            "nota": "Cadastro identificado por formulário GET e validado na ficha.",
        }
