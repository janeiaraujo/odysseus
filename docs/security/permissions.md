# Permissões e aprovações

| Risco | Exemplos | Padrão | Pode automatizar? |
|---|---|---|---|
| Leitura | Consultar agenda | Automático | Sim |
| Reversível | Etiqueta, rascunho, luz | Automático | Sim |
| Externo | Enviar e-mail, convidar pessoas | Aprovação | Sim, por regra sua |
| Destrutivo | Exclusões irreversíveis | Aprovação | **Não** |
| Financeiro | Compra no navegador | Aprovação | **Não** |
| Físico crítico | Fechadura, portão, alarme | Aprovação | **Não** |

Regras em **Ajustes** ou `autonomy` na configuração, por usuário, ação (`gmail.send`), integração (`gmail.*`) ou risco. Aprovações expiram em 7 dias e só executam uma vez.

## Injeção de prompt

Um e-mail ou página maliciosa consegue, no máximo, criar um pedido de aprovação visível. Conteúdo externo chega ao modelo marcado como dado, e o risco vem do código, nunca do texto. Uma limitação: um pedido forjado pode parecer legítimo, então leia o resumo antes de aprovar.

## Lacunas conhecidas

As ferramentas `bash`, `python` e de arquivos do Odysseus rodam sem sandbox para administradores, e o navegador MCP não passa pelas proteções do `browser.py`. Veja `THREAT_MODEL.md`.
