# 𝗧𝗜𝗗𝗔𝗟-𝗗𝗟 𝗨𝗟𝗧𝗥𝗔

__Baixe músicas Lossless e Hi-Res do [Tidal](https://tidal.com/) direto do terminal — inclusive no iPhone/iPad com o [a-Shell](https://holzschu.github.io/a-Shell_iOS/).__

Irmão do **qobuz-dl-ultra**: mesma organização de código, mesma pasta/nome de arquivo, mesmos marcadores `[IN PROGRESS]`/`[INCOMPLETE]`, mesmo catálogo local (`library.db`), scan, sync de favoritos e `doctor`. Uma biblioteca que mistura os dois serviços funciona com os dois programas.

## 📑 Índice

* [✨ Funcionalidades](#-funcionalidades)
* [📥 Instalação](#-instalação)
  * [📱 iPhone/iPad (a-Shell)](#-iphoneipad-a-shell)
  * [💻 Desktop / servidor](#-desktop--servidor)
* [🔑 Login](#-login)
* [💻 Uso](#-uso)
* [🗂️ Catálogo local, scan e sync](#️-catálogo-local-scan-e-sync)
* [⚙️ Configuração](#️-configuração)
* [🔧 Solução de problemas](#-solução-de-problemas)
* [🧪 Desenvolvimento](#-desenvolvimento)
* [⚠️ Aviso legal](#️-aviso-legal)

## ✨ Funcionalidades

* **Lossless e Hi-Res** (FLAC até 24-bit/192 kHz conforme o plano) e AAC. Se a qualidade pedida não existir para uma faixa, o programa **desce um degrau por vez** até uma que sirva (`--no-fallback` desliga).
* **Python puro, sem ffmpeg obrigatório.** O Tidal entrega Hi-Res como DASH (FLAC dentro de MP4 fragmentado); o remux para FLAC nativo é feito em Python (`fmp4.py`), sem re-encode. ffmpeg é só plano B.
* **Funciona no a-Shell**: núcleo com `httpx`, `mutagen` e `colorama` (todos Python puro), sem keyring (token em arquivo `0600`), sem `aiosqlite`/`tenacity`/`tqdm`, saída adaptada a tela estreita.
* **Tags completas**: título/artista/álbum, faixa/disco, data, ISRC, `BARCODE` (UPC), copyright, BPM, **ReplayGain de faixa e álbum**, capa, letra e IDs do Tidal (`TIDALTRACKID`, `TIDALALBUMID`) — os IDs permitem ao `scan` reconhecer seus álbuns com certeza. FLAC e M4A.
* **Letras**: embutidas nas tags e, quando sincronizadas, também em `.lrc`.
* **Retomada inteligente**: pasta `[IN PROGRESS]` durante o download; se algo falha vira `[INCOMPLETE]` e a próxima execução só baixa o que faltou. Arquivos temporários usam `~tmp_` (sem ponto, para o app Arquivos do iOS).
* **Álbuns, faixas, playlists (com `.m3u8`) e artistas**, multi-disco em `CD 01`, `CD 02`.
* **Dedup** por banco (`tidal_dl.db`) + **sentinela** `.streamrip.json` em cada álbum completo.
* **Somente streams sem criptografia.** Se o Tidal devolver um stream protegido, o programa tenta a qualidade abaixo; não há descriptografia no projeto.

## 📥 Instalação

### 📱 iPhone/iPad (a-Shell)

1. Instale o **a-Shell** na App Store.
2. Instale as dependências (todas Python puro):
   ```sh
   pip install httpx mutagen colorama
   ```
3. Instale o programa (PyPI, quando publicado) **ou** copie a pasta do projeto para dentro do a-Shell:
   ```sh
   pip install tidal-dl-ultra
   # ou, a partir da pasta do projeto:
   pip install .
   ```
4. Diga onde ficam config e downloads (a pasta `~/Documents` é visível no app Arquivos):
   ```sh
   export TIDAL_DL_IOS_HOME="$HOME/Documents"
   ```
   Se não definir, o a-Shell é detectado sozinho e `~/Documents` é usado.
5. Use `python3 -m tidal_dl ...` (se o comando `tidal-dl` não existir no PATH do a-Shell — se aparecer `login: command not found`, é isso):
   ```sh
   python3 -m tidal_dl            # tela inicial: sessão, comandos e primeiros passos
   python3 -m tidal_dl login
   python3 -m tidal_dl dl https://tidal.com/browse/album/123456
   ```
   Para digitar só `tidal-dl`, crie um atalho (e coloque a mesma linha no arquivo de inicialização do a-Shell para valer sempre):
   ```sh
   alias tidal-dl='python3 -m tidal_dl'
   ```

Dicas para o a-Shell:
* Deixe a tela ligada durante downloads longos (o iOS suspende apps em segundo plano).
* Em rede móvel ruim use `--concurrency 1`.
* `python -m tidal_dl doctor` mostra o que falta, sem mexer em nada.
* Sem ffmpeg tudo funciona: o remux FLAC é interno.

### 💻 Desktop / servidor

```sh
pip install tidal-dl-ultra          # ou: pip install ".[all]" na pasta do projeto
tidal-dl login
```

Docker (NAS): `docker build -t tidal-dl-ultra . && docker run -it -v ./config:/config -v ./downloads:/downloads tidal-dl-ultra login`.

## 🔑 Login

```sh
tidal-dl login            # PKCE: Lossless / Hi-Res (recomendado)
tidal-dl login --device   # código de dispositivo: só AAC 320 kbps
tidal-dl user             # conta, país, plano e qualidade máxima
tidal-dl logout           # apaga o token
```

No login PKCE o programa mostra uma URL **de login**. Abra no navegador (pode ser o Safari do próprio iPhone), **entre na conta e autorize**. No fim o Tidal redireciona para uma página que dá erro ou fica em branco (é normal): **copie a URL da barra de endereço nesse momento** e cole no terminal. Ela começa com `https://tidal.com/android/login/auth?code=...`. O token renova sozinho.

Erros comuns: colar de volta a URL de login que o programa mostrou (o programa avisa e pergunta de novo, até 3 vezes, sem precisar recomeçar); ou, se o app do Tidal estiver instalado, ele abrir sozinho no fim do login e "engolir" o redirecionamento — nesse caso abra o link de novo em outro navegador ou em aba anônima.

O token fica em `credentials.json` (permissão 0600) ao lado do `config.ini`, ou no keyring do sistema quando existe. **Nunca compartilhe esse arquivo.**

## 💻 Uso

```sh
tidal-dl dl https://tidal.com/browse/album/123456          # álbum
tidal-dl dl https://tidal.com/browse/track/123456 -q 2     # faixa em FLAC 16-bit
tidal-dl dl https://tidal.com/browse/playlist/UUID         # playlist (+ .m3u8)
tidal-dl dl https://tidal.com/browse/artist/123 --eps      # discografia (+ EPs/singles)
tidal-dl dl lista.txt                                      # várias URLs, uma por linha

tidal-dl search daft punk                # busca de álbuns; escolha por número (1,3-5)
tidal-dl search -t track get lucky       # tipos: album | track | artist | playlist
tidal-dl lucky -t album -n 3 pink floyd  # baixa direto os 3 primeiros

tidal-dl stats                           # estatísticas do que você baixou
```

Qualidades (`-q`): `0` AAC 96 · `1` AAC 320 · `2` FLAC 16/44.1 · `3` Hi-Res legado · `4` FLAC até 24/192.

Opções úteis: `-d PASTA`, `-ff` / `-tf` (formatos), `--concurrency N`, `--delay SEG`, `--no-db`, `--no-lyrics`, `--no-cover`, `--no-sentinel`, `--remux auto|python|ffmpeg|none`.

### Variáveis de formatação

Pasta (`folder_format`): `{release_type}` `{album_artist}` `{album_title}` `{year}` `{format}` `{bit_depth}` `{sampling_rate}` `{album_id}` `{quality}`
Faixa (`track_format`): `{track_number}` `{disc_number}` `{track_title}` `{track_title_base}` `{track_artist}` `{album_artist}` `{explicit}` `{track_id}`

Padrão da pasta: `{release_type}/{album_artist} - {album_title} ({year}) [{format} {bit_depth}]`.

## 🗂️ Catálogo local, scan e sync

O `library.db` (ao lado do `config.ini`) responde: *"o que eu tenho na conta vs. o que eu tenho no disco?"*.

```sh
tidal-dl sync-favorites                    # diff (novos/removidos) e atualiza o catálogo
tidal-dl sync-favorites --download-new     # baixa o que foi favoritado desde a última vez
tidal-dl sync-favorites --download-missing --limit 20 -y
tidal-dl sync-favorites --download-new --every 60    # modo contínuo (NAS)

tidal-dl scan "/musica"                    # casa pastas do disco com o catálogo (offline)
tidal-dl scan --dry-run --json rel.json

tidal-dl library                           # status
tidal-dl library missing                   # favoritos ainda não baixados
tidal-dl library history                   # últimas sincronizações
tidal-dl library reconcile [DIR] [--fix]   # sentinelas do disco ⇄ catálogo
tidal-dl library reset-stuck               # destrava álbuns presos após CTRL+C
tidal-dl library unmark <ID>

tidal-dl doctor [--json]                   # diagnóstico (somente leitura, sem segredos)
```

**Biblioteca grande:** rode `sync-favorites` (sem download), depois `scan`, e só então `--download-missing`.

O `scan` decide por: tag `TIDALALBUMID` → UPC → nome exato → fuzzy. Só marca sozinho quando o match é único e a contagem de faixas bate; dúvida vai para revisão manual. Pastas `[INCOMPLETE]` nunca viram "completas". Multi-disco conta como um álbum.

## ⚙️ Configuração

`tidal-dl config` abre um assistente; `tidal-dl config --show` mostra tudo; `--reset` apaga. O arquivo é o `config.ini` (seção `[tidal]`), em:

| Ambiente | Pasta |
|---|---|
| a-Shell | `$TIDAL_DL_IOS_HOME/tidal-dl/` (ou `~/Documents/tidal-dl/`) |
| Linux/macOS | `~/.config/tidal-dl/` |
| Windows | `%APPDATA%\tidal-dl\` |
| Qualquer | `$CONFIG_DIR/tidal-dl/` (tem prioridade) |

Chaves principais: `directory`, `quality`, `allow_quality_fallback`, `folder_format`, `track_format`, `embed_art`, `save_cover_file`, `lyrics`, `save_lrc`, `concurrency`, `retries`, `remux`, `no_database`, `write_sentinel`, `disable_keyring`. Argumento na linha de comando > `config.ini` > padrão.

## 🔧 Solução de problemas

* **"Você não está logado"** → `tidal-dl login`.
* **Só baixa AAC** → você entrou com `--device`; refaça o login PKCE. Confira também `tidal-dl user` (plano e qualidade máxima).
* **"Apenas prévia (PREVIEW)"** → assinatura inativa ou sem direito ao conteúdo.
* **Álbum ficou `[INCOMPLETE]`** → rode o mesmo comando de novo; só o que falhou é baixado.
* **Erro de remux** → `--remux ffmpeg` (se houver ffmpeg) ou `--remux none` para guardar o `.mp4` bruto.
* **Faixas sem tags** → falta `mutagen` (`pip install mutagen`).
* **Qualquer dúvida de ambiente** → `tidal-dl doctor`.

## 🧪 Desenvolvimento

```sh
pip install -e ".[dev]"
pytest
```

Os testes usam um Tidal falso (`tests/unit/fakes.py`, `scenario.py`): nunca tocam a rede. A camada HTTP (`net.py`) é injetável, por isso a lógica é testável sem `httpx`.

## ⚠️ Aviso legal

Este projeto é independente e **não é afiliado ao Tidal**. Use apenas com a sua própria assinatura, para uso pessoal, e respeite os Termos de Serviço e as leis de direitos autorais do seu país. Você é responsável pelo uso que fizer. As credenciais de cliente OAuth embutidas são as usadas pela comunidade de ferramentas Tidal e podem ser substituídas por variáveis de ambiente (`TIDAL_DL_CLIENT_ID`, `TIDAL_DL_CLIENT_SECRET`, `TIDAL_DL_CLIENT_ID_PKCE`, `TIDAL_DL_CLIENT_SECRET_PKCE`).

Licença: GPL-3.0 (ver `LICENSE`).
