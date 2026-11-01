"""Testes unitários com dados fictícios, sem conexão ao Canaimé."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgp2rr_tools import ServicoSGP, registrar_ferramentas


class FakeBackend:
    def __init__(self):
        self.visited = []
        self.downloaded = []

    def abrir_pagina(self, caminho):
        self.visited.append(caminho)
        if "pesquisa.php" in caminho:
            return "[F1] GET pesquisa_resultadoVULGO.php name=form1"
        if "Ficha_Preso_index_SitAtual.php" in caminho:
            return "## Texto\nEM CUMPRIMENTO DE PENA"
        if "Ficha_Preso_index.php" in caminho:
            return "## Texto\nUnidade\nCPP\nAla\nCPP\n04\nCódigo\n77\nNome\nPESSOA EXEMPLO"
        if "UND_CertidaoCarceraria.php" in caminho:
            return (
                "## Texto\nCERTIDÃO CARCERÁRIA Nº 77\n"
                "Reeducando:\nPESSOA EXEMPLO\nSituação Atual:\nEM CUMPRIMENTO DE PENA\n"
                "Conduta Atual:\nNÃO OBSERVADO\n"
                "PROCESSO\nPENA IMPOSTA\nREGIME\n0000000-00.0000.0.00.0000\n"
                "3 ano(s)\nSEMI ABERTO\n"
            )
        if "HistCar_LER.php" in caminho and "pageNum_rsHistcar=1" in caminho:
            return (
                "## Texto\n## Tabelas\n"
                "| UND | CPP | DATA | 01/01/2026 | CONDUTA | BOA |  |\n"
                "## Links\n[1] anexo → fotos/presos_doc/aaaabbbbcccc1111.pdf\n"
            )
        if "HistCar_LER.php" in caminho:
            return (
                "## Texto\n## Tabelas\n"
                "| UND | CPP | DATA | 02/01/2026 | CONDUTA | NÃO OBSERVADO |  |\n"
                "## Links\n[1] anexo → fotos/presos_doc/aaaabbbbcccc1111.pdf\n"
                "[2] /sgp2rr/areas/unidades/HistCar_LER.php?pageNum_rsHistcar=1"
                "&totalRows_rsHistcar=2&id_cad_preso=77 → URL\n"
            )
        raise AssertionError("Caminho não esperado: " + caminho)

    def enviar_formulario(self, formulario, campos, botao):
        assert formulario == 1
        assert botao == "Pesquisar"
        assert campos["busca1"] == "nome"
        return "1 REEDUCANDO(S) CADASTRADO(S)\nID 77\nPESSOA EXEMPLO\n"

    def baixar_anexo(self, caminho):
        self.downloaded.append(caminho)
        return b"%PDF-1.7\nconteudo-exemplo", "application/pdf"


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.service = ServicoSGP(self.backend)

    def test_search_name_uses_only_read_form(self):
        data = self.service.pesquisar_reeducando("Pessoa Exemplo", "CPP")
        self.assertEqual(data["resultados"], [{"codigo": 77, "nome": "PESSOA EXEMPLO"}])
        self.assertEqual(data["total_informado"], 1)
        self.assertEqual(data["unidade"], "CPP")

    def test_search_rejects_invalid_inputs(self):
        with self.assertRaises(ValueError):
            self.service.pesquisar_reeducando("A")
        with self.assertRaises(ValueError):
            self.service.pesquisar_reeducando("Pessoa Exemplo", "INEXISTENTE")
        with self.assertRaises(ValueError):
            self.service.consultar_ficha(-1)

    def test_ficha_minimal(self):
        data = self.service.consultar_ficha(77)
        self.assertEqual(data["unidade"], "CPP")
        self.assertEqual(data["situacao"], "EM CUMPRIMENTO DE PENA")
        self.assertNotIn("cpf", data)
        self.assertNotIn("rji", data)

    def test_historico_pagination(self):
        data = self.service.consultar_historico(77)
        self.assertEqual(data["total_identificado"], 2)
        self.assertEqual({r["data"] for r in data["registros"]},
                         {"01/01/2026", "02/01/2026"})

    def test_certidao(self):
        data = self.service.consultar_certidao(77)
        self.assertEqual(data["numero_certidao"], "77")
        self.assertEqual(data["regime"], "SEMI ABERTO")
        self.assertEqual(data["situacao_atual"], "EM CUMPRIMENTO DE PENA")

    def test_anexos_are_deduplicated(self):
        data = self.service.listar_anexos(77)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["anexos"], [{"indice": 1, "formato": "PDF"}])
        self.assertEqual(self.backend.downloaded, [])

    def test_pdf_requires_valid_index(self):
        with self.assertRaises(ValueError):
            self.service.ler_anexo(77, 2)
        self.assertEqual(self.backend.downloaded, [])

    def test_pdf_validated_and_extracted(self):
        class Page:
            def extract_text(self):
                return "Documento de exemplo sem informações reais."
        class PdfReader:
            def __init__(self, _stream, strict=False):
                self.pages = [Page()]
        with patch.dict(sys.modules, {"pypdf": SimpleNamespace(PdfReader=PdfReader)}):
            data = self.service.ler_anexo(77, 1)
        self.assertEqual(data["paginas_total"], 1)
        self.assertIn("Documento de exemplo", data["texto"])
        self.assertEqual(len(self.backend.downloaded), 1)

    def test_register_all_tools(self):
        class FakeMCP:
            def __init__(self):
                self.tools = []
            def tool(self):
                def decorator(fn):
                    self.tools.append(fn.__name__)
                    return fn
                return decorator
        mcp = FakeMCP()
        registrar_ferramentas(mcp, lambda: FakeBackend())
        self.assertEqual(len(mcp.tools), 6)
        self.assertIn("sgp_ler_anexo", mcp.tools)


if __name__ == "__main__":
    unittest.main()
