# Cloudflare R2

**Estado: testado contra um servidor S3 real (moto), com assinatura conferida com o `botocore`. Ainda não validado contra o R2.**

1. No painel da Cloudflare, crie um bucket e um token de API restrito a ele.
2. Preencha `JARVIS_R2_*` no `.env`.
3. Envie um backup: `scripts/jarvis-backup upload --key-file ~/jarvis-recovery.key ARQUIVO.jrvb`.

O backup já sai criptografado da sua máquina. As operações são contadas por classe, e o Jarvis para antes de sair da faixa gratuita (limites em `storage.max_class_a_month` e `max_class_b_month`).

## Preços e limites (consultados em 06/11/2026)

Faixa gratuita mensal: 10 GB, 1 milhão de operações classe A e 10 milhões de classe B. Depois: US$ 4,50 por milhão de classe A, US$ 0,36 por milhão de classe B e US$ 0,015 por GB-mês. Chaves têm até 1024 bytes, com uma escrita por segundo na mesma chave. Confira em [developers.cloudflare.com/r2/pricing](https://developers.cloudflare.com/r2/pricing/).

## Objetos de zero byte (experimental)

`storage/zerokey.py` guarda registros pequenos no **nome** de objetos vazios, selados com AES-GCM: o nome não revela nada e o mesmo registro gera a mesma chave. Um índice local responde às leituras sem custo, e pode ser reconstruído a partir do bucket.

**Não é grátis.** Cada registro é um PUT, ou seja, uma operação classe A, e a Cloudflare não documenta como cobra o armazenamento desse caso.

| 5 milhões de registros/mês | Classe A | Custo de escrita |
|---|---|---|
| Zero byte | 5.000.000 | US$ 18,00 |
| Agrupados de 1000 em 1000 | 5.000 | US$ 0,00 |

Compare com `GET /api/jarvis/storage/zero-byte/costs`. Faz sentido para poucos registros que precisam ser objetos individuais. Não é uma forma de memória principal, e o R2 não faz busca semântica.
