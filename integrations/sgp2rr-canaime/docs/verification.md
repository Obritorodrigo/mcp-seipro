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
