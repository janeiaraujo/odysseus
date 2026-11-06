# Aprendizado

O motor usa regras, então não gasta tokens. Conversa não vira fato sozinha:

| Entrada | Resultado |
|---|---|
| "Jarvis, lembre que..." | Fato, com origem registrada |
| "Esqueça que..." | Remove a melhor correspondência, se for clara |
| "Prefiro...", "moro em..." | Fila de revisão |
| "Na verdade..." | Revisão; aceita, corrige a memória e guarda a versão anterior |
| Perguntas e pedidos | Nada |
| "Talvez...", "acho que..." | Revisão com baixa confiança |
| E-mail ou web | Revisão como não confiável; linhas com cara de instrução são descartadas |
| Sugestão do agente | Revisão; o agente não grava fatos direto |

Conversas anônimas não ensinam nada. Desligue tudo em **Ajustes**.
