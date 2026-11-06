# Decisões

**D1. SQLite segue como banco principal; Firestore é réplica opcional.** O Odysseus tem 28 modelos SQLAlchemy com joins, transações e migrações; trocá-los por um banco de documentos seria uma reescrita e colocaria dados existentes em risco. O Firestore recebe uma cópia seletiva por uma fila persistente, com o texto das memórias criptografado por padrão. PostgreSQL só faz sentido com vários processos escrevendo ao mesmo tempo, o que não ocorre hoje.

**D2. Banco próprio para o Jarvis.** Isola falhas e permite atualizar o Odysseus sem conflito de esquema.

**D3. Cadeia de fallback configurada conta como autorização.** O modo padrão `configured` respeita fallbacks pagos que o usuário listou. O roteador automático nunca escolhe um modelo pago fora dessa lista. Atingir o orçamento bloqueia pagos em qualquer modo. O modo `never` proíbe pagos como fallback.

**D4. Preço desconhecido fica desconhecido.** Sem preço configurado ou publicado pelo OpenRouter, o custo fica nulo e a interface conta essas chamadas à parte. Consequência: o orçamento só enxerga chamadas com preço conhecido.

**D5. Aprendizado por regras, sem modelo.** Não gasta tokens e pode ser testado. Só "lembre que..." vira fato direto; o restante vai para revisão. Texto de e-mail, web e documentos é hipótese, e linhas com cara de instrução são descartadas.

**D6. Mensagem de e-mail com Message-ID determinístico.** A API do Gmail não oferece chave de idempotência. Um envio interrompido consulta esse ID antes de qualquer nova tentativa.

**D7. Backup lógico do ChromaDB pela API.** Exportar coleções em JSONL independe do layout do volume e da versão do servidor. O problema de persistência que foi corrigido em `/data` mostra o risco de depender desse layout.

**D8. Objetos de zero byte são experimentais.** Cada registro custa uma operação classe A, e a Cloudflare não documenta a cobrança de armazenamento desse caso. Para volume alto, agrupar registros em um objeto é mais barato.

**D9. Palavra de ativação por transcrição.** O navegador detecta fala localmente e envia só esses trechos; o servidor age se o texto começar com "Jarvis". Funciona sem modelo extra, mas o áudio chega ao seu servidor antes da decisão. openWakeWord é o próximo passo.

**D10. Login com Google recusa contas com 2FA.** Do contrário, viraria um atalho em torno do segundo fator.
