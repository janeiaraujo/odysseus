# Estado da implementação

Atualizado em 06/11/2026. Testado "com simulação" **não** significa que funcione contra o serviço real.

Legenda: ✅ testado de verdade · 🧪 testado com simulação · 🟡 parcial · 🚫 bloqueado por dependência externa · 📋 planejado

| Área | Estado | Evidência ou lacuna |
|---|---|---|
| Identidade e persona | ✅ | Chat real com modelo falso local: persona presente nos modos chat e agente |
| Painel web (13 abas) | ✅ | Chrome real, desktop e mobile, sem overflow; ações testadas pela API |
| Política e auditoria | ✅ | Testes e fluxo real pela API |
| Comandos diretos | ✅ | Chat real: nenhuma chamada ao modelo |
| Roteador: filtro, uso, ranking | ✅ | Testes e chat real; preços reais do OpenRouter só com simulação |
| Roteador automático no chat | 📋 | Seleção manual continua; ranking em missões e voz |
| Memória híbrida | ✅ | ChromaDB real, reinício e importação de 130 itens sem perda |
| Aprendizado | ✅ | Testes e chat real |
| Missões e worker | ✅ | Reinício real; execução de passos em testes |
| Backup e restauração | ✅ | Dados reais restaurados em ambiente limpo |
| Persistência do ChromaDB | ✅ | Migração feita nesta instalação, com contagens conferidas |
| Scripts | ✅ | `doctor`, `backup` e `train --dry-run` executados; `install` e `update` só com checagem de sintaxe |
| Imagem Docker | ✅ | Compilada e executada com dados temporários, antes dos ajustes finais do Compose |
| Navegador | ✅ | Chrome real, bloqueio de SSRF real |
| Perfil Docker do navegador | 🟡 | Configuração validada; nunca executado |
| Google OAuth | 🧪 🚫 | Faltam credenciais |
| Calendar | 🧪 🚫 | Faltam credenciais; tabelas locais testadas de verdade |
| Gmail e fila de envio | 🧪 🚫 | Faltam credenciais; anexos e push não implementados |
| Drive | 🧪 🚫 | Faltam credenciais |
| Fotos | 🧪 🚫 | Faltam credenciais; galeria na interface não implementada |
| Cloudflare R2 | 🟡 🚫 | Testado contra servidor S3 real (moto); falta o R2 |
| Objetos de zero byte | 🟡 | Testado contra S3 real; custo real no R2 não medido |
| Firestore | 🟡 | Projeto criado, regras negam acesso anônimo (403 real); código validado no emulador; **primeira gravação real pendente da chave** (`scripts/jarvis-firebase-check`) |
| FCM | 🧪 🚫 | Implementado; não testado |
| Home Assistant | 🧪 🚫 | Falta um servidor |
| MQTT e ESP32 | 📋 | |
| Voz e modo conversa | 🟡 | Navegador real com fala sintetizada em português (Piper) como microfone: botão de microfone, modo conversa, interrupção por fala, ruído e cliques ignorados, botão de ouvir. Modelo do chat falso nesse teste. **Falta validar com voz humana e microfone físico** |
| Palavra de ativação | 🟡 | Por transcrição; sem teste com áudio real |
| Fine-tuning | 🟡 | Dados testados; treino nunca executado (sem GPU) |
| Android | 🟡 | App próprio em `janeiaraujo/jarvis-android`; versão só com Firestore em branch, ainda não instalada |

## Cenários obrigatórios

| # | Cenário | Resultado |
|---|---|---|
| 1 | Inicia preservando o ambiente | ✅ Com dados restaurados; stack atual intocada |
| 2 | Memória sobrevive ao reinício | ✅ Aplicação real e ChromaDB real |
| 3 | App conversa com o backend | Não se aplica: o app roda sozinho, sem este servidor |
| 4 | Modelo indisponível não derruba | ✅ Provedor derrubado de verdade: erro claro, saúde preservada |
| 5 | Fallback respeita custo | 🧪 Testes do filtro; sem falha real de provedor |
| 6 | Evento consultado corretamente | ✅ Tabelas reais e recorrência |
| 7 | E-mail classificado sem envio | 🧪 |
| 8 | Sem envio duplicado | 🧪 Inclui queda no meio do envio |
| 9 | Token inválido pede reconexão | 🧪 |
| 10 | Tarefa sobrevive ao reinício | ✅ Aplicação real |
| 11 | Backup restaurado em outro ambiente | ✅ Dados reais |
| 12 | Dados privados fora dos logs | 🧪 Testes de redação |
| 13 | Conteúdo malicioso não altera instruções | 🧪 Sem teste adversarial contra modelo real |
| 14 | Interface funciona após as mudanças | ✅ Chrome real; suíte sem regressões |

## Testes

Suíte completa: as mesmas 4 falhas que já existiam antes desta branch, todas fora do código do Jarvis (`test_model_routes`, `test_research_report_read` (2), `test_session_tools_registry`).
