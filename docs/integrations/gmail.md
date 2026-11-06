# Gmail

**Estado: testado com simulações; ainda não validado contra o Gmail real.**

- **Triagem:** classificação gratuita por regras em Trabalho, Pessoal, Financeiro, Viagens, Compras, Notificações e Outros, com sinalização de urgente, aguardando resposta e suspeito. Categorias próprias: `google.email_categories` na configuração.
- **Incremental:** usa a History API; um cursor expirado reinicia limitado às últimas 50 mensagens. Nunca varre a caixa inteira.
- **Envio:** pela fila em `mailqueue.py`, com aprovação por padrão, cancelamento, fuso horário e auditoria. Uma queda no meio do envio não duplica a mensagem; veja os [diagramas](../architecture/diagrams.md).
- **Segurança:** o conteúdo do e-mail é dado. Nada nele dispara ação, e texto enviado a modelos vai marcado como não confiável.

Não implementado ainda: notificações push do Gmail (usa consulta agendada) e análise do conteúdo de anexos. Para classificar por rotina, peça "Jarvis, monitore meus e-mails de trabalho".
