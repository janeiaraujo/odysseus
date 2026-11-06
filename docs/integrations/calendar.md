# Google Calendar

**Estado: testado com simulações; escrita nas tabelas locais testada de verdade.**

Eventos sincronizados entram nas tabelas de calendário do Odysseus com `origin="google"`, aparecendo na agenda existente, nos comandos e nas ferramentas. A sincronização usa `syncToken`; um erro 410 do Google reinicia o ciclo, como a documentação manda.

| Ação | Política |
|---|---|
| Consultar, buscar, conflitos, horários livres | Automático, sem modelo |
| Criar ou alterar evento só seu | Reversível, automático |
| Evento com convidados, ou remarcação que os afete | Pede aprovação |

Os convidados são lidos da cópia do Google, não informados por quem chama. Assim, ninguém escapa da aprovação dizendo que não há convidados.

Limitações: editar um evento do Google pela tela de agenda do Odysseus não envia a mudança ao Google, e a primeira sincronização cobre a partir de 30 dias atrás. Comandos: "tenho algo marcado amanhã?", "existe algum conflito na minha agenda?", "encontre um horário livre de duas horas".
