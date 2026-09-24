# Changelog

## 0.2.2 (não lançado)
- **Aviso de letras aparecia só no modo sequencial.** Bug de gating: `if not self._parallel` fazia
  a busca de letra rodar 100% muda no modo paralelo (o padrão pra álbuns com >1 item). Agora avisa
  nos dois modos, mostrando a origem (Tidal/LRCLIB), com um `asyncio.Lock` só na IMPRESSÃO das duas
  linhas (busca + resultado) pra não intercalar com a de outra faixa concorrente.
- **Letra sincronizada (LRC) agora vai pra tag `LYRICS`** quando existe, em vez de sempre gravar só a
  versão plana mesmo quando havia sincronia disponível. Nova tag `UNSYNCEDLYRICS` com a versão plana
  à parte. O `.lrc` avulso (`save_lrc`) continua independente disso, como já era.
- **"Em Progresso" não aparecia pra vídeos.** Mesmo bug de gating do item acima, só que no vídeo;
  corrigido seguindo a mesma convenção do áudio (sequencial avisa antes, paralelo avisa com a
  contagem de segmentos).
- **Timeout no ffmpeg** (`_ffmpeg_remux`/`_ffmpeg_remux_generic`, 120s + kill do processo) -- proteção
  contra o subprocess travar pra sempre em ambientes de subprocess mais restritos.
- **Nome da pasta com qualidade errada, de verdade desta vez.** Antes só a linha do terminal usava a
  qualidade real; o NOME DA PASTA ainda vinha só do palpite por tier. Agora, quando o álbum termina
  com sucesso, o nome final é recalculado com o bit_depth/sample_rate REAL da primeira faixa de áudio
  baixada.
- **Dolby Atmos (`eac3`/`ac4`) nunca batia.** O identificador de codec no manifesto normalmente vem
  hifenizado (`ec-3`, `ac-4`), não colado -- `DOLBY_CODECS` nunca dava match e a faixa caía no
  tratamento de áudio comum. Lista de codecs corrigida; e quando a própria faixa já informa
  `DOLBY_ATMOS` como tier, o download agora tenta pedir esse tier explicitamente à API antes da
  escada normal (evita receber um downmix estéreo comum). Essa segunda parte é best-effort -- não
  testada contra a API real.
- **Modo interativo reorganizado igual ao qobuz-dl-ultra**: `search`/`i`/`fun` SEM argumentos agora
  abre um fluxo em telas separadas -- escolhe o TIPO (Álbuns/Faixas/Artistas/Playlists/Favoritos),
  depois o TERMO (prompt_toolkit, com loop pra buscar de novo), só então a tabela de resultados.
  Favoritos tem sub-tela própria e busca a lista direto (sem termo). Chamado COM termo na linha de
  comando continua indo direto pra busca (scripts/uso não-interativo). Novos endpoints de favoritos
  de artista/playlist em `api.py`.
- "Qualidade solicitada" renomeado pra "Qualidade alvo" no cabeçalho do álbum.

## 0.2.1 (não lançado)
- **Terminal não mente mais sobre a qualidade real.** O cabeçalho do álbum mostrava um bit_depth
  *adivinhado* a partir do tier reportado pelo álbum (que pode não bater com o que o Tidal entrega
  de fato por faixa) -- agora mostra a qualidade *solicitada* (honesta), e a linha "Concluído" de
  cada faixa passa a usar `bit_depth`/`sample_rate` REAIS do stream resolvido em vez do nome do tier
  (`format_real_quality`, novo helper em `downloader.py`).
- **Faixas Dolby Atmos (`eac3`/`ac4`) rotuladas como tal** (`DOLBY_CODECS` em `constants.py`) em vez
  de herdar um bit_depth/sample_rate PCM que não faz sentido pra elas.
- **Vídeos embutidos num álbum não são mais descartados.** `api.py` ignorava silenciosamente
  qualquer item do tipo "vídeo" dentro de `albums/{id}/items`; `get_album_items()` agora traz faixas
  e vídeos juntos, contabilizados no cabeçalho, e os vídeos vão pra MESMA pasta do álbum (vídeos
  avulsos continuam indo pra `video_directory`, via `_download_video_one`, núcleo compartilhado).
- **Pasta de vídeos agora respeita a auto-detecção de `~/Documents` no a-Shell**, igual à pasta de
  música principal (`default_video_folder()`, espelhando `default_download_folder()`); antes tinha
  um default relativo fixo que só ganhava o prefixo com a env var `TIDAL_DL_IOS_HOME` setada à mão.
- **Barra de progresso de vídeo mostra tamanho baixado, não mais um contador solto.** A playlist HLS
  é resolvida antes de abrir a barra (total real de segmentos), e o tamanho acumulado em MB aparece
  como postfix -- igual ao download de áudio.
- **Download de letras deixou de ser silencioso**: avisa "Procurando letras para: ..." e o resultado
  (sincronizada / sem sincronia / não encontrada) no terminal, igual ao qobuz-dl-ultra.
- **`--reset` bem-sucedido cai direto na tela inicial** em vez de só voltar pro shell.
- **Tela inicial (comandos/flags) com espaçamento consistente**, igual ao qobuz-dl-ultra.
- **Modo interativo (`search`/`i`/`fun`) reescrito com prompt_toolkit**: tabela em tela cheia (cartões
  em terminais estreitos), seleção múltipla, atalhos de teclado (↑↓/jk, PageUp/PageDown, g/G, 1-9,
  Ctrl+C/Esc), no mesmo estilo do qobuz-dl-ultra (`interactive_ui.py` + `_tui_select` portados).
  **Nova dependência: `prompt_toolkit`.**
- Removida a linha final solta `Concluído: N ok, M com falha.` do comando `dl`.


- **Download de vídeo, refeito.** A versão anterior ignorava criptografia AES-128 dos segmentos HLS
  e mandava um valor de qualidade que a API não reconhece (`1080p` em vez de `LOW/MEDIUM/HIGH`) --
  o resultado era um arquivo corrompido, baixado em silêncio. Agora: leitura correta de master/media
  playlist, decriptação AES-128 quando o CDN usa (novo módulo `hls.py`, testado com round-trip real
  de criptografia), escolha de variante por `--video-quality`, barra de progresso por segmento, remux
  para `.mp4` via ffmpeg quando disponível (fica `.ts` sem ele) e nome de arquivo `Artista - Título (Ano)`.
- **Barras de progresso e organização dos downloads**, no estilo do qobuz-dl-ultra: modo sequencial
  (uma faixa por vez, com barra tqdm/própria) e paralelo (`--max-workers N`, com linhas "Em Progresso"/
  "Concluído" em vez de barras sobrepostas); cabeçalho antes de cada álbum/faixa/playlist/vídeo; resumo
  final (📊) com sucesso/puladas/fallback/falhas; `--delay` força modo sequencial; `--no-progress`
  desliga as barras.
- **Retomada de download por faixa** (HTTP Range/segmento DASH): uma queda de rede no meio de um
  arquivo não obriga a rebaixar tudo. CTRL+C agora sinaliza um cancelamento limpo (sem deixar
  arquivos `~tmp_` para trás) em vez de travar o processo.
- **`-r`/`--reset`** (roda o assistente de configuração) e **`-p`/`--purge`** (apaga o
  `tidal_dl.db`) no nível global, com o assistente rodando sozinho na primeira execução -- igual
  ao qobuz-dl-ultra. O antigo `config --reset` foi removido: ele colidia com essa flag nova e tinha
  um significado diferente (apagava sem perguntar nada), o que causava trava esperando teclado.
- **Fallback de letras via LRCLIB** (assíncrono, nunca bloqueia) quando o Tidal não tem letra para
  a faixa. Desativável com `--no-lyrics-fallback`.
- `doctor`: menciona `cryptography` como dependência opcional (só necessária para vídeos
  criptografados); extra `pip install "tidal-dl-ultra[video]"`.

## 0.1.1
- Login PKCE: a URL de autorização não usa mais `%3A`/`%2F` (o a-Shell/Safari a recodificavam para
  `%253A` e o Tidal recusava o `redirect_uri`, então o login nunca terminava).
- Login PKCE: detecta quando a URL de LOGIN foi colada no lugar da URL de RETORNO e explica; pergunta de
  novo (até 3 vezes) sem gerar outro par PKCE; aceita também o deep link `tidal://...?code=`.
- Tela inicial no estilo do qobuz-dl-ultra: logo em blocos (adapta a tela estreita), SESSÃO
  (login/qualidade/pasta/baixados), PRIMEIROS PASSOS, COMANDOS e FLAGS em português.
- `--help` traduzido e colorido (`ColoredArgumentParser`).

## 0.1.0
- Primeira versão: login PKCE e device-code, download de álbuns/faixas/playlists/artistas,
  fallback de qualidade, remux FLAC-em-MP4 em Python puro, tags completas (ReplayGain, IDs),
  letras, retomada com `[IN PROGRESS]`/`[INCOMPLETE]`, dedup + sentinela `.streamrip.json`,
  catálogo local com `sync-favorites`, `scan`, `library`, `doctor` e `stats`.
- Núcleo Python puro (`httpx`, `mutagen`, `colorama`), compatível com o a-Shell (iOS).
