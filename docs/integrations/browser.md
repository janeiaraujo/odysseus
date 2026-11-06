# Navegador

**Estado: testado com Chrome real, incluindo bloqueio de endereços internos. Perfil Docker `jarvis-browser` ainda não executado.**

Cada requisição da página, inclusive subrecursos e redirecionamentos, é verificada: endereços privados, loopback e de metadados são bloqueados. Sessões ficam criptografadas por usuário e nunca são mostradas a um modelo. CAPTCHA interrompe a execução e pede uma pessoa. Enviar formulário pede aprovação, e compras contam como risco financeiro. O texto da página chega ao modelo como dado não confiável.

```bash
docker compose --profile browser up -d
# no .env: JARVIS_BROWSER_WS=ws://jarvis-browser:3000/
```

O container precisa da dependência opcional: `INSTALL_OPTIONAL=true` no build. O perfil não publica portas, remove capacidades e limita memória.

O agente também tem o navegador MCP já existente no Odysseus (Playwright MCP), que **não** passa por estas proteções.
