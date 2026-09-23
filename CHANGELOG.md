# Changelog

## 0.2.0
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
