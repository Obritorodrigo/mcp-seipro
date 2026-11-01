# Validação técnica — 2026-11-01

## Escopo

Validação somente de leitura realizada na integração ChatGPT com o servidor MCP SGP2RR/Canaimé. Sem alteração de dados no sistema de origem.

## Verificações observadas

| Verificação | Resultado |
| --- | --- |
| Descoberta de cinco ferramentas MCP | OK na sessão testada |
| `sgp_status` | Respondeu com conexão autenticada |
| `sgp_abrir_pagina` | Abriu a estrutura inicial do SGP2RR |
| `sgp_seguir_link` | Navegou pelos quadros até o menu |
| Leitura do menu principal | OK |
| Envio de formulários | Não testado |
| Gravações ou alterações | Não realizadas |
| Compatibilidade multiusuário | Não homologada |
| Renovação de tokens, revogação, reconexão | Não testadas |

### Ferramentas identificadas

- `sgp_status` — consulta de status da conexão e sessão.
- `sgp_abrir_pagina` — leitura de página autorizada (caminho opcional).
- `sgp_seguir_link` — segue um link apresentado pela navegação.
- `sgp_ver_parte` — lê outra parte da última página aberta.
- `sgp_enviar_formulario` — envia formulário; **pode alterar dados** e não deve ser usado em testes sem autorização explícita.

### Observações

- A página de entrada utiliza quadros (frames).
- O menu acessível na sessão testada apresentou áreas de Pesquisa (Reeducando, Visitante, Visitação), Trabalhos (Chamada/Tranca, Trabalho Interno) e relatórios/livro de entradas e saídas.
- Estas verificações demonstram apenas acesso naquela sessão, não segurança, disponibilidade contínua ou cobertura de todas as funcionalidades.
- Nenhum login, identificador pessoal, URL de sessão, token ou dado do sistema foi incluído neste relatório.

## Não incluído nesta entrega

O código do serviço `sgp2rr-mcp` hospedado no servidor, automações de implantação, configuração privada de Cloudflare e credenciais de usuários **não foram disponibilizados para versionamento**. A adição destes arquivos requer revisão separada de licenças, privacidade e segredos, preferencialmente em repositório privado.

## Ampliação da pesquisa — 2026-11-01 (implementação, sem deploy)

A sessão SGP2RR autorizada confirmou **12 opções** do formulário `GET pesquisa_resultadoVULGO.php` (`busca1`, `busca2`, `busca3`): nome, vulgo, mãe, pai, país de origem, RG, CPF, telefone, celular, endereço, bairro e foto. O campo `busca3` permite restringir unidades conforme as opções apresentadas. Confirmou-se também o formulário separado `GET Ficha_Menu.php`, com `id_cad_preso` e botão "Abrir Ficha".

Foram adicionadas as ferramentas propostas `sgp_pesquisar_cadastro` e `sgp_criterios_pesquisa` à extensão de servidor. A pesquisa `foto` foi mapeada por existir no formulário, mas sua semântica ainda precisa ser validada em homologação. Para identificadores sensíveis, o retorno não reproduz o termo da busca. Em produção, ajustar o logging do servidor HTTP/reverse proxy para **não registrar parâmetros sensíveis da consulta GET**.

**CI:** [Execução GitHub Actions — testes do SGP2RR](https://github.com/Obritorodrigo/mcp-seipro/actions/runs/41325887162): 18 testes unitários passaram, inclusive os 12 critérios, busca por código, validação de filtros e isolamento do conteúdo de teste. Nenhum dado real foi utilizado nesses testes. Este resultado **não equivale** a implantação nem homologação com o site real. As cinco ferramentas originais seguem ativas até que o servidor MCP incorpore a extensão.


## Homologação de compatibilidade no Ubuntu — 01/11/2026

**Evidências fornecidas pelo operador a partir do terminal Ubuntu**, sem documentos pessoais e sem copiar o código privado do servidor para este repositório:

| Verificação | Resultado observado | Limite da evidência |
| --- | --- | --- |
| Instância atual `sgp2rr-mcp.service` | `active (running)` | O serviço ainda executa a versão original |
| Testes da extensão dentro do ambiente virtual real | **30/30 OK** | Testes com mocks; não substituem testes contra o sistema real |
| Geração da cópia de homologação via `prepare_integration.py` | **OK** | Arquivo gerado fora do diretório de produção |
| Integridade do `sgp2rr_mcp.py` original | **OK** | Comparação confirmou que a única modificação na cópia é o bloco de registro |
| `MCPServer` instalado | **OK** | Oito novas ferramentas registradas em instância isolada |
| Carregamento da cópia completa via `runpy.run_path` | **13/13 ferramentas registradas** | Não iniciou `uvicorn` e não simulou sessão OAuth real |
| Inicialização do `MCPServer` | Linhas 85 e 98; extensão após as cinco tools, na linha 526 da cópia | Ordem estática compatível |
| Dependências `httpx`, `bs4`, `mcp` | **OK** | Presença confirmada na venv |
| Dependência `pypdf` | **Ausente** | Extração de texto de PDFs continua bloqueada até instalação e teste |
| Backup do servidor antes de alterações | **Criado**, conforme saída do operador | Backup deve ser mantido no próprio servidor privado; não publicar |

### Consulta fictícia em GET de leitura

Foi executada consulta pontual no formulário `pesquisa_resultadoVULGO.php` usando termo sintético deliberadamente inexistente, sem dados de pessoa real, e a página informou **0 REEDUCANDO(S) CADASTRADO(S)**. Isso confirma apenas a resposta do endpoint GET, **não** a precisão de todos os 13 filtros e **não** o funcionamento das novas ferramentas no serviço público.

### Pendências reais (não marcar como concluídas)

1. Determinar a configuração de host/porta do bloco `uvicorn.run` e preparar serviço de teste verdadeiramente isolado; não abrir outra instância com as mesmas credenciais, dados ou portas de produção.
2. Testar a aplicação real da pesquisa com registros sintéticos/autorizados e revisar a paginação, formato de ficha e o filtro `foto`.
3. Homologar dois usuários distintos, isolamento, validade e revogação de tokens em ambiente controlado.
4. Verificar anonimização/omissão de query strings com CPF, RG, endereço, telefone etc. em logs do aplicativo, proxy e observabilidade antes da ativação.
5. Instalar e testar `pypdf` apenas no ambiente homologado antes de habilitar leitura de anexos.
6. Verificar o rollback e documentar aprovação para implantação antes de alterar `sgp2rr-mcp.service`.

**Situação da produção:** cinco ferramentas originais ativas; as oito novas estão somente em cópia de homologação. O sucesso de testes e de `runpy` não equivale a deploy.
