"""Testes com registros inteiramente fictícios. Nenhuma chamada ao Canaimé."""
import unittest
from sgp2rr_search import PesquisaSGP, CRITERIOS


FORM_GERAL = (
    "## Formulários\n[F1] GET pesquisa_resultadoVULGO.php name=form1\n"
    "  - busca1 (select) opções: "
    + "; ".join(k + "=" + v.upper() for k, v in CRITERIOS.items() if k != "codigo")
    + "\n  - busca2 (text)\n  - busca3 (select)\n  botões: 'Pesquisar'"
)
FORM_CODIGO = (
    "## Formulários\n[F1] GET Ficha_Menu.php name=form1\n"
    "  - id_cad_preso (text) \"\" = ''\n  botões: 'Abrir Ficha'"
)
RESULTADO = (
    "1 REEDUCANDO(S) CADASTRADO(S)\n"
    "ID 77\nPESSOA FICTICIA\n/\nMÃE: NÃO EXIBIR\nCPP\n"
)


class FakeBackend:
    def __init__(self):
        self.visited = []
        self.submitted = []
        self.codigo_encontrado = True
        self.codigo_troca = False

    def abrir_pagina(self, caminho):
        self.visited.append(caminho)
        if caminho.endswith("pesquisa.php"):
            return FORM_GERAL
        if "pesquisa_Iframe" in caminho:
            return FORM_CODIGO
        if "Ficha_Preso_index.php" in caminho:
            codigo = "88" if self.codigo_troca else "77"
            return f"## Texto\nCódigo\n{codigo}\nNome\nPESSOA FICTICIA\n"
        raise AssertionError("Página não esperada")

    def enviar_formulario(self, formulario, campos, botao):
        self.submitted.append((formulario, campos, botao))
        if botao == "Pesquisar":
            return RESULTADO
        if botao == "Abrir Ficha":
            return (
                "## Links\n[1] (quadro) ficha → "
                "areas/unidades/Ficha_Preso_index.php?id_cad_preso=77"
                if self.codigo_encontrado else "## Texto\nCadastro não encontrado"
            )
        raise AssertionError("Operação inesperada")


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.service = PesquisaSGP(self.backend)

    def test_lists_all_thirteen_criteria(self):
        data = self.service.criterios_disponiveis()
        self.assertEqual(len(data["criterios"]), 13)
        self.assertIn({"codigo": "mae", "descricao": "Nome da mãe"}, data["criterios"])
        self.assertIn({"codigo": "foto", "descricao": "Foto"}, data["criterios"])
        self.assertIn("CPP", data["unidades"])

    def test_all_twelve_general_options(self):
        for criterio in CRITERIOS:
            if criterio == "codigo":
                continue
            with self.subTest(criterio=criterio):
                self.backend.submitted.clear()
                value = "123.456.789-09" if criterio == "cpf" else (
                    "11999998888" if criterio in {"telefone", "celular"}
                    else "12345" if criterio == "rg"
                    else "Termo Ficticio"
                )
                result = self.service.buscar(criterio, value, "CPP")
                _, campos, botao = self.backend.submitted[-1]
                self.assertEqual(campos["busca1"], criterio)
                self.assertEqual(campos["busca3"], "CPP")
                self.assertEqual(botao, "Pesquisar")
                self.assertEqual(result["resultados"],
                                 [{"codigo": 77, "nome": "PESSOA FICTICIA"}])
                self.assertEqual(result["total_informado"], 1)

    def test_casefold_accents_and_aliases(self):
        for entrada, expected in [("MÃE", "mae"), ("nome do pai", "pai"),
                                  ("PAÍS DE ORIGEM", "pais_orig"),
                                  ("Endereço", "endereco"), ("apelido", "vulgo")]:
            with self.subTest(entrada=entrada):
                self.assertEqual(self.service.buscar(entrada, "EXEMPLO")["criterio"],
                                 expected)

    def test_sensitive_search_never_echoes_query(self):
        for criterio, valor in [("cpf", "123.456.789-09"),
                                ("endereco", "RUA FICTICIA, 123"),
                                ("celular", "11988887777")]:
            with self.subTest(criterio=criterio):
                result = self.service.buscar(criterio, valor)
                self.assertEqual(result["consulta"], "[valor omitido]")
                self.assertNotIn(valor, str(result))
                self.assertNotIn("NÃO EXIBIR", str(result))

    def test_search_code_via_dedicated_get_form_and_checks_record(self):
        result = self.service.buscar("código", "77")
        self.assertEqual(result["resultados"],
                         [{"codigo": 77, "nome": "PESSOA FICTICIA"}])
        self.assertEqual(self.backend.submitted,
                         [(1, {"id_cad_preso": "77"}, "Abrir Ficha")])
        self.assertIn("pesquisa_Iframe", self.backend.visited[0])

    def test_code_missing_is_not_false_positive(self):
        self.backend.codigo_encontrado = False
        result = self.service.buscar("codigo", "77")
        self.assertEqual(result["resultados"], [])
        self.assertEqual(result["total_informado"], 0)

    def test_code_mismatch_rejected(self):
        self.backend.codigo_troca = True
        with self.assertRaises(RuntimeError):
            self.service.buscar("codigo", "77")

    def test_invalid_searches_do_not_submit_anything(self):
        cases = [
            ("não existe", "123", ""),
            ("cpf", "123", ""),
            ("telefone", "abcdef", ""),
            ("nome", "", ""),
            ("nome", "a" * 121, ""),
            ("nome", "EXEMPLO", "UNIDADE_INVALIDA"),
            ("codigo", "77", "CPP"),
            ("codigo", "-5", ""),
            ("codigo", "0", ""),
        ]
        for criterio, valor, unidade in cases:
            with self.subTest(criterio=criterio, valor=valor):
                self.backend.submitted.clear()
                with self.assertRaises(ValueError):
                    self.service.buscar(criterio, valor, unidade)
                self.assertEqual(self.backend.submitted, [])

    def test_only_current_page_reported(self):
        self.service = PesquisaSGP(self.backend)
        result = self.service.buscar("mae", "Nome Ficticio")
        self.assertEqual(result["limite"], 30)
        self.assertFalse(result["resultado_parcial"])


if __name__ == "__main__":
    unittest.main()
