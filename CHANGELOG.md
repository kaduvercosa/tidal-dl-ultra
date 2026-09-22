# Changelog

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
