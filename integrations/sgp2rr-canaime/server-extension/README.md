# Extensão de ferramentas SGP2RR (fase 1)

Código adicional projetado para ser **integrado ao servidor MCP SGP2RR existente**.
O pacote do plugin ChatGPT, por si só, NÃO instala estas novas ferramentas no
servidor hospedado.

## Ferramentas propostas

| Ferramenta | Uso | Método |
| --- | --- | --- |
| sgp_pesquisar_reeducando(nome, unidade="") | Pesquisa por nome (compatibilidade) | Formulário GET de pesquisa |
| sgp_criterios_pesquisa() | Lista todos os critérios e unidades disponíveis | Sem consulta a cadastros |
| sgp_pesquisar_cadastro(criterio, valor, unidade="", limite=30) | Pesquisa por 13 critérios, incluindo código, CPF, filiação, endereço, vulgo, telefone e demais opções | Dois formulários GET, conforme o critério |
| sgp_consultar_ficha(codigo) | Unidade, ala e situação atual | Leitura de duas páginas |
| sgp_consultar_historico(codigo) | Datas/unidade/conduta | Leitura paginada do histórico |
| sgp_consultar_certidao(codigo) | Resumo da certidão carcerária | Leitura da página de impressão |
| sgp_listar_anexos(codigo) | Relaciona anexos PDF do histórico | Sem baixar arquivos |
| sgp_ler_anexo(codigo, indice, paginas=5) | Extrai texto do PDF da sessão | Download autenticado e extração local |

## Pesquisa multícritério (fase 2)

A consulta do formulário real do Canaimé apresentou os critérios: `nome`, `vulgo`, `mae`, `pai`, `pais_orig`, `rg`, `cpf`, `telefone`, `celular`, `endereco`, `bairro`, `foto`. Existe, separadamente, pesquisa direta pelo `codigo` (`id_cad_preso`).

Exemplos com informações **fictícias**, usando a nova ferramenta `sgp_pesquisar_cadastro`:

~~~text
criterio="nome", valor="PESSOA FICTICIA", unidade="CPP"
criterio="mae", valor="NOME EXEMPLO"
criterio="pai", valor="NOME EXEMPLO"
criterio="cpf", valor="123.456.789-09"
criterio="endereco", valor="RUA FICTICIA"
criterio="bairro", valor="BAIRRO EXEMPLO"
criterio="codigo", valor="77"          # não suporta filtro de unidade
~~~

**Atenção:** o formulário mostra opção `foto`, mas seu significado operacional ainda precisa de homologação real. O módulo envia o critério ao formulário GET sem fazer download de imagens. Documentos e endereços são aceitos somente como filtros, não retornados como campos de identificação na resposta; os resultados apresentam nome e código. Os resultados são limitados a 30 cadastros por consulta; paginação completa não foi implementada. O backend deve preservar isolamento de sessão e aplicar autorização, auditoria e limites de consultas conforme a política institucional.

A rotina de leitura de PDF resolve a limitação da ferramenta HTML
sgp_abrir_pagina, que reconhece o arquivo mas não consegue ler application/pdf.
A extração de textos digitalizados por imagem (OCR) **não está implementada**.

## Contrato do backend autenticado

O código usa injeção de dependências para **não criar nem compartilhar** sessões
entre servidores, usuários ou clientes OAuth. O código-fonte do servidor
sgp2rr-mcp ainda não está versionado neste repositório; antes do deploy é
necessário mapear a implementação real dos métodos abaixo.

Backend.abrir_pagina(caminho: str) -> str
: Abre **apenas** páginas do SGP2RR pela sessão web autenticada e retorna a
  mesma representação textual com campos e links do sgp_abrir_pagina atual.

Backend.enviar_formulario(formulario: int, campos: dict, botao: str) -> str
: Envia **exclusivamente a pesquisa GET autorizada** neste módulo.
  Deve preservar os campos ocultos do formulário conforme o servidor atual.
  Não reutilize esta rota para cadastros, alteração de situação, exclusões
  ou POSTs administrativos.

Backend.baixar_anexo(caminho: str) -> tuple[bytes, str]
: Baixa bytes com o cookie da MESMA sessão autenticada do usuário atual,
  verificando caminho fixo dentro de fotos/presos_doc e tipo MIME. Deverá
  utilizar timeout, limitar redirects ao host original, tamanho máximo de
  resposta e nunca registrar binários ou URLs de sessão em logs.
  Nenhum endpoint público de download não autenticado deve ser exposto.

### Exemplo de integração (pseudocódigo)

~~~python
from sgp2rr_tools import registrar_ferramentas

def backend_factory():
    # IMPLEMENTAR: recupere o cliente autenticado vinculado à requisição MCP atual.
    # NUNCA use um requests.Session global ou cookies de outra pessoa.
    return adaptador_do_servidor_da_requisicao_atual()

registrar_ferramentas(mcp, backend_factory)
~~~

Não executar o pseudocódigo acima até implementar a função de fábrica.

## Segurança e privacidade

- O código **não contém** qualquer dado real de reeducando, credencial, token,
  cookie, PDF, nome de usuário, chave privada ou registro do sistema.
- Consultas precisam seguir as permissões da identidade autenticada e a finalidade
  de serviço. O usuário pode ver somente seus próprios registros permitidos.
- As funções de pesquisa e consulta são somente leitura. O módulo não oferece
  ferramentas de escrita.
- PDFs: caminhos limitados a anexos listados no histórico do próprio cadastro;
  máximo de 12 MiB, 10 páginas por leitura e texto resumido/redigido.
- O índice do anexo é validado por consulta de histórico antes de baixar.
- Não usar o GitHub público para guardar documentos prisionais, capturas
  autenticadas, dados pessoais ou os resultados das consultas.

## Testes

Requisitos de Python 3.10 ou posterior e a biblioteca pypdf (somente para
sgp_ler_anexo):

~~~bash
python -m pip install "pypdf>=5,<7"
cd integrations/sgp2rr-canaime/server-extension
python -m unittest discover -s tests -v
~~~

Os testes usam registros fictícios e mocks. **Não executam requests ao
Canaimé** e não equivalem à homologação integrada. Validar manualmente o
parse do HTML/texto do servidor e a fronteira entre sessões OAuth.

## Homologação pendente antes do deploy

1. Conferir o código-fonte efetivo do sgp2rr-mcp e adaptar backend_factory.
2. Testar a extração autenticada de PDFs com arquivos de teste e registro do
   tipo MIME, sem persistir bytes ou dados pessoais.
3. Testar separação de sessões com dois usuários de teste e negar acesso cruzado.
4. Testar paginação completa, limites, timeout e falhas de login.
5. Revisar proteção contra acesso a outros hosts por redirecionamento.
6. Executar testes e homologação em ambiente controlado antes de instalar em produção.
7. Após deploy, verificar as seis ferramentas MCP com ações somente leitura.

Este código é uma **proposta de integração**, não um deploy realizado.
