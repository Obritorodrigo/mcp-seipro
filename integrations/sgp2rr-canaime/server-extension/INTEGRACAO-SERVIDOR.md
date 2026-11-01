# Implantação da extensão SGP2RR no servidor MCP existente

**Status:** código preparado e testado com mocks; NÃO implantado no servidor Canaimé.

## 1. Estrutura da integração

- sgp2rr_search.py: 13 critérios de pesquisa observados no formulário real.
- sgp2rr_tools.py: oito ferramentas de consulta que serão registradas no MCP.
- sgp2rr_integration.py: interface segura entre tools e a sessão já autenticada.
- tests/: testes isolados, sem requisição ao Canaimé.

O servidor existente, localizado no ambiente do operador, não está no GitHub.
Por isso **não é correto presumir** os nomes de suas funções internas nem editar
server.py sem inspecioná-lo. Essa é a única ligação que permanece dependente
do código real do servidor.

## 2. Instalar os arquivos de extensão em cópia de homologação

No Ubuntu, **fora do diretório público de produção**, por exemplo:

~~~bash
set -e
cd /home/ubuntu
git clone --depth 1 --branch feat/sgp2rr-canaime-chatgpt \
  https://github.com/Obritorodrigo/mcp-seipro.git sgp2rr-plugin-stage
cd sgp2rr-plugin-stage/integrations/sgp2rr-canaime/server-extension

python3 -m compileall -q sgp2rr_tools.py sgp2rr_search.py sgp2rr_integration.py
python3 -m unittest discover -s tests -v
~~~

O comando não instala nada no servidor nem reinicia serviços. Evite adicionar
quaisquer credenciais ou registros do SGP ao clone público.

## 3. Conectar ao FastMCP existente

Inspecione a função que recupera o usuário autenticado e a sessão HTTP no
servidor em /home/ubuntu/sgp2rr-mcp. Recupere a sessão **da identidade OAuth
da requisição atual**, nunca uma sessão global ou uma conta de serviço.

Este é o bloco de integração a inserir NA INICIALIZAÇÃO do FastMCP existente
(antes do mcp.run), ajustando SOMENTE os nomes internos do servidor:

~~~python
# Ajustar sys.path ou instalar o módulo no ambiente Python do SGP2RR.
from sgp2rr_integration import ClienteAutenticado, integrar_sgp2rr

def obter_cliente_atual() -> ClienteAutenticado:
    # ADAPTAR para as funções reais do sgp2rr-mcp.
    # Deve recuperar a sessão web do usuário OAuth da chamada MCP EM CURSO.
    estado = recuperar_estado_sgp_da_requisicao_mcp()
    if estado is None or not estado.autenticado:
        raise PermissionError("Sessão SGP2RR não autenticada")

    return ClienteAutenticado(
        # Funções já implementadas no servidor para ler páginas.
        # DEVEM devolver a mesma representação textual de sgp_abrir_pagina.
        abrir=lambda caminho: estado.abrir_pagina(caminho),

        # A extensão faz a pesquisa por GET direto nos endpoints já mapeados;
        # não depende do estado do último formulário nem executa POST.

        # A MESMA sessão autenticada; é opcional até ativar leitura PDF.
        # Não criar requests.Session nova: não carregaria o login atual.
        sessao_http=estado.sessao_http,
    )

integrar_sgp2rr(mcp, obter_cliente_atual)
~~~

ATENÇÃO: recuperar_estado_sgp_da_requisicao_mcp(), estado.abrir_pagina(),
estado.autenticado e estado.sessao_http são
**nomes ilustrativos**. Não basta copiar esse bloco sem vinculá-los à
implementação real. Se o servidor usa funções ASYNC/await, as oito tools
desta primeira versão ainda são SÍNCRONAS e exigem adaptação do wrapper;
não execute coroutines com asyncio.run() dentro de um event loop ativo.

## 4. Verificações obrigatórias antes da ativação

1. Executar testes locais na cópia de homologação.
2. Confirmar que todas as chamadas passam pela autenticação OAuth de cada
   usuário e não reutilizam a sessão de outro usuário.
3. Conferir que a pesquisa direta navega apenas pelos endpoints GET
   pesquisa_resultadoVULGO.php e Ficha_Menu.php. Nenhum POST de gravação.
4. Confirmar que a sessão HTTP para PDFs é a mesma do usuário e que redirects
   externos são recusados (o módulo usa allow_redirects=False).
5. Não registrar valores de CPF, RG, nomes, endereço, query string GET, cookies,
   respostas e PDFs nos logs do aplicativo, proxy ou serviços de monitoramento.
6. Executar leitura de teste para cada filtro com registros sintéticos ou
   devidamente autorizados, e validar a paginação dos resultados.
7. Conferir a compatibilidade dos módulos com a versão do FastMCP em uso.
8. Após homologação, efetuar backup do servidor e preparar rollback antes de
   qualquer reinício do serviço sgp2rr-mcp.

## 5. Resultado esperado

O servidor deverá passar de cinco ferramentas atualmente observadas para
TREZE (cinco existentes + oito de extensão), se não houver nomes duplicados:

- sgp_status, sgp_abrir_pagina, sgp_seguir_link, sgp_ver_parte,
  sgp_enviar_formulario (ferramentas existentes);
- sgp_pesquisar_reeducando, sgp_consultar_ficha, sgp_consultar_historico,
  sgp_consultar_certidao, sgp_listar_anexos, sgp_ler_anexo,
  sgp_criterios_pesquisa, sgp_pesquisar_cadastro (extensão).

Esse resultado **não foi observado em produção**. Para instalação precisa,
é indispensável revisar a implementação atual de server.py e do gestor de
sessões OAuth no servidor sgp2rr-mcp.
