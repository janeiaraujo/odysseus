# Home Assistant

**Estado: testado com simulações; ainda não validado contra um Home Assistant real.**

No painel, aba **Dispositivos**: informe a URL e um token de longa duração, depois descubra ou cadastre dispositivos. Comandos: "ligue a televisão da sala", "ajuste o ar do quarto para 22 graus".

## Segurança independente do modelo

Texto nunca chega a um dispositivo. O comando vira uma ação estruturada, passa por uma tabela fixa de operações permitidas e depois pela política de aprovação:

- Fechaduras, portões e alarmes sempre pedem aprovação, e nenhuma regra muda isso.
- Marque como **crítico** qualquer tomada que alimente algo perigoso.
- Temperatura fica entre 16 e 30 graus.
- Comando ambíguo ("ligue a televisão", com duas TVs) não é executado.

Câmeras e sensores são cadastrados e lidos, mas não têm tela própria ainda. MQTT e ESP32 estão planejados: hoje alcançam o Jarvis pelo Home Assistant.
