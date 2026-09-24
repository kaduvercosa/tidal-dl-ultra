# Manutencao tecnica — setembro de 2026

## Alteracoes

- O progresso de HLS agora contabiliza bytes escritos, em vez de contar segmentos como se fossem segundos. Como o manifesto HLS nao informa necessariamente o tamanho total em bytes, a barra opera em modo de total desconhecido e exibe bytes transferidos/tempo decorrido.
- A chamada CLI `dl` deixou de imprimir o resumo global redundante `Concluido: ...`; os resumos detalhados por album/playlist continuam sendo produzidos pelo downloader.

## Validacao

Executar `python -m compileall -q tidal_dl tests` e `pytest -q` no ambiente com as dependencias de desenvolvimento instaladas. Validar tambem em a-Shell, especialmente a saida de progresso HLS e a escrita de arquivos de video.

## Itens ainda pendentes

A integracao de videos em albuns mistos, a verificacao da qualidade real por faixa (bit depth/sample rate), a exibicao do resultado de letras, o fluxo de reset/configuracao e a interface interativa com prompt_toolkit exigem alteracoes adicionais e testes especificos; nao sao declarados como concluidos nesta revisao.

### Continuação — feedback de letras no terminal

- O downloader agora informa, por faixa, quando encontrou letra e identifica a origem (Tidal ou LRCLIB), além de indicar se há LRC sincronizada.
- Quando nenhuma letra é encontrada, mostra um aviso informativo sem interromper o download.
- A letra continua sendo incorporada às tags e/ou salva em `.lrc` conforme as opções existentes; não houve mudança no formato dos arquivos.
- Validação: `compileall` passou. A suíte pytest ainda não pôde ser coletada neste ambiente porque a dependência `colorama` não está instalada; portanto, não declarar testes unitários aprovados.
