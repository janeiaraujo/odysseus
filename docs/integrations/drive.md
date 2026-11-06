# Google Drive

**Estado: testado com simulações.**

Listar e buscar exige só o escopo somente leitura. **Indexar exige que a pasta esteja na lista autorizada** (aba Documentos). Arquivos movidos para fora ou excluídos saem do índice.

Lê Google Docs, Sheets e Slides por exportação, PDFs por `pypdf` e arquivos de texto. Limite de 20 MB. Usa a Changes API e não baixa de novo o que não mudou. Duplicatas são detectadas por MD5, o que exclui documentos nativos do Google, que não têm checksum. O conteúdo indexado é tratado como dado não confiável.
