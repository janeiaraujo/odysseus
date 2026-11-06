# Google Fotos

**Estado: testado com simulações.**

Usa a **Picker API**: você escolhe no seletor do Google e o Jarvis só vê essas fotos. Desde março de 2025 a Library API não dá acesso à biblioteca inteira, então busca geral na biblioteca não é oferecida, e isso não é uma limitação contornável.

A análise usa o melhor modelo com visão permitido pelas regras de custo. Os bytes ficam em memória e não são armazenados. Descrições só são guardadas se pedidas, como hipótese revisável.

Limitação: a interface abre o seletor, mas ainda não mostra a galeria selecionada; a análise está disponível pela API (`/api/jarvis/photos/analyze`).
