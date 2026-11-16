# Radar TT Brasil

Coleta os trending topics do X no Brasil **toda hora** e guarda o histórico. Depois você pesquisa um termo ("shampoo") e vê:

- se ele já foi tendência no período (30 dias, 6 meses, 12 meses);
- quantas horas ficou na lista, a melhor posição e o quanto foi forte em relação aos outros termos do período;
- cada episódio com data e duração, e o **contexto**: a frase "Porque…" quando ela foi capturada na hora, ou os termos que subiram junto, mais um link para os posts daquela data no X.

A busca acha variações: "shampoo" encontra `Shampoo`, `#ShampooChallenge` e `shampoo seco`, sem diferenciar acentos ou maiúsculas.

---

## 1. Estudo de viabilidade do histórico

### Conclusão

**É viável e barato.** Dá para ter 6 meses de histórico **no primeiro dia**, importando o arquivo público por hora do getdaytrends, e depois acumular sozinho com uma coleta por hora no GitHub Actions. O volume de dados é pequeno. O ponto fraco é o **contexto do passado**: o "porquê" de algo que foi tendência há 4 meses não pode ser recuperado com a mesma qualidade de quando é capturado na hora.

### De onde vem cada dado

| Necessidade | Fonte | Histórico? | Custo | Observação |
|---|---|---|---|---|
| Lista de trends agora | API oficial do X (`/2/trends/by/woeid/23424768`) | Não, só o momento | ~US$ 0,01 por chamada | Oficial e estável. 1×/hora ≈ US$ 7/mês |
| Lista de trends agora | twitterapi.io (terceiro) | Não | ~US$ 0,00015 por chamada | Centavos por mês; serviço não oficial |
| Lista de trends agora **e passado** | getdaytrends.com (páginas públicas por hora, top 50) | **Sim**, por data e hora | Grátis | Viabiliza o backfill de 6 meses. Raspagem de site: confira os termos de uso antes e mantenha pausas entre páginas (o script já espera 2s) |
| "Porquê" de um episódio novo | Busca recente da API do X (10 posts mais relevantes) + resumo por IA | Só últimos 7 dias | ~US$ 0,05 por episódio | Feito na hora, só para quem chega ao top 15 |
| "Porquê" de um episódio antigo | Termos que subiram junto + link de busca do X filtrado por data | Sim | Grátis | É uma pista, não uma explicação; o link leva aos posts da época |

### Tamanho do histórico

- Um levantamento da BuzzMonitor contou **5.805 termos distintos** nos TTs do Brasil em um único mês (jan/2023, leitura a cada 5 minutos). Ou seja, por volta de 6 mil termos novos por mês.
- 6 meses de leituras horárias = 4.320 horas × 50 posições ≈ **216 mil linhas**, ou cerca de **10 MB** em CSV. Cabe em qualquer lugar; o SQLite reconstrói tudo em segundos.
- A página de busca carrega um arquivo compacto de ~0,6 a 1 MB com 6 meses de dados e faz toda a conta no navegador.

### Como o "volume" é medido

Hoje o X não informa a quantidade de posts de boa parte das tendências. Por isso o volume é medido pelo que sempre existe, **tempo e posição na lista**:

- **Horas em trend**: quantas leituras horárias tiveram o termo no top 50.
- **Pontos de atenção**: cada hora vale pela posição (1º = 50 pontos, 50º = 1). É o "volume" do gráfico.
- **Força relativa** (o "volume relativo à janela de 6 meses"): a porcentagem de termos que passaram pelos TTs na mesma janela e somaram menos pontos. "Mais forte que 86%" quer dizer que ficou no topo 14% daquele semestre.
- Quando a fonte informa a contagem de posts, ela é guardada (`tweet_count`) para análises futuras.

### Riscos e limites

1. **Layout do getdaytrends**: o leitor da página **foi testado contra o site real em 16/11/2026** (o top 50 vive no container `#trends`, dividido em duas tabelas; os rankings do dia ficam fora e são ignorados). Há um teste com uma cópia da página real em `tests/fixtures/`. Se o site mudar e a coleta trouxer menos de 10 termos, ela falha de propósito e o GitHub avisa por e-mail.
   - **Arquivo por data instável**: no teste, as páginas de arquivo (`/brazil/AAAA-MM-DD/H/`) respondiam erro 520; as páginas "agora" e das últimas 23h (`/brazil/N/`) funcionavam normalmente. Por isso: (a) toda coleta roda também um *catch-up* que recompõe, pelas páginas das últimas 23h, qualquer hora que tenha falhado — o histórico novo não fica com buracos; (b) o backfill de 6 meses tenta com pausas e novas tentativas, e depende de o arquivo do site estar no ar — se falhar hoje, rode de novo outro dia, que ele continua de onde parou.
2. **Termos de uso**: raspar páginas públicas costuma ser tolerado em volume baixo, mas não é garantido. Para a coleta contínua, a opção mais segura é a API oficial ou o twitterapi.io (variável `RADAR_SOURCE`). O getdaytrends fica só para o backfill.
3. **Agendamento do GitHub**: tarefas agendadas podem atrasar alguns minutos em horários de pico. Para um histórico de 1 leitura por hora isso não tem impacto.
4. **Contexto antigo é pista, não resposta**: termos curtos e genéricos (nomes próprios como "Igor") aparecem por motivos diferentes em cada episódio. Por isso o contexto é mostrado por episódio, não por termo.
5. **GitHub Pages grátis exige repositório público**. Os dados são públicos de qualquer forma; as chaves ficam em *Secrets* e nunca aparecem. Um repositório privado com Pages exige o plano GitHub Pro.

### Custo mensal estimado

| Configuração | Lista de trends | Contexto | Total aproximado |
|---|---|---|---|
| **Grátis** | getdaytrends | Termos que subiram junto + link do X | R$ 0 |
| **Recomendada** | twitterapi.io 1×/hora | API do X, só top 15 (~20–30 episódios/dia) + resumo Claude Haiku | ~US$ 30–45 |
| **Toda oficial** | API do X 1×/hora | Igual à recomendada | ~US$ 40–55 |

Os valores de contexto são estimativas; o número real depende de quantos assuntos novos chegam ao top 15 por dia. O limite fica em `--max-rank` e `--max` (episódios por execução).

---

## 2. Colocar no ar (20 minutos)

1. Crie um repositório **público** no GitHub e envie esta pasta inteira.
2. Em **Settings → Pages**, em *Source*, escolha **GitHub Actions**.
3. (Opcional) Em **Settings → Secrets and variables → Actions**:
   - *Variables*: `RADAR_SOURCE` = `getdaytrends` (padrão), `twitterapi_io` ou `x_api`
   - *Secrets*: `TWITTERAPI_IO_KEY`, `X_BEARER_TOKEN` (liga o "porquê" automático), `ANTHROPIC_API_KEY` (liga o resumo em uma frase)
4. Em **Actions → Importar histórico (backfill)**, clique em *Run workflow* com 180 dias. Leva de 2 a 4 horas (são 4.320 páginas com pausa).
5. Em **Actions → Coleta horária**, clique em *Run workflow* uma vez. A partir daí ela roda sozinha todo minuto 7 de cada hora e atualiza a página em `https://SEU-USUARIO.github.io/NOME-DO-REPO/`.

## 3. Usar no computador

```bash
pip install -r requirements.txt
python radar.py collect --dry-run          # testa a fonte sem gravar
python radar.py backfill --days 7          # importa a última semana
python radar.py build-db                   # monta o SQLite a partir dos CSVs
python radar.py search shampoo             # busca no terminal (180 dias)
python radar.py search shampoo --days 30 --exact --json
python radar.py export                     # gera web/data/index.json
cd web && python -m http.server            # abre a busca em http://localhost:8000
```

Para ver a demonstração com dados simulados: `python tools/make_demo_data.py && RADAR_DB=data/demo.db python radar.py export --demo`.

## 4. Arquivos

| Arquivo | Para que serve |
|---|---|
| `radar.py` | Tudo: coleta, backfill, contexto, busca e exportação |
| `data/snapshots/AAAA-MM-DD.csv` | O histórico: uma linha por termo por hora. É o que vai para o git |
| `data/contexts.jsonl` | Frases "Porque…" e links dos posts de cada episódio |
| `web/index.html` | A página de busca (sem dependências; lê `web/data/index.json`) |
| `.github/workflows/collect.yml` | Coleta horária + publicação da página |
| `.github/workflows/backfill.yml` | Importação do histórico, sob demanda |
| `tools/make_demo_data.py` | Gera 6 meses de dados simulados para testar |
| `tests/` | Testes (`python -m pytest tests`) |

## 5. Próximos passos naturais

- **Alertas**: avisar no celular quando um termo monitorado ("shampoo", nomes de marcas, de clientes) entrar nos TTs.
- **Cidades**: o mesmo coletor serve para São Paulo (WOEID 455827) e Rio (455825).
- **Google Trends ao lado**: comparar a curva de buscas no Google com a presença nos TTs do X, para separar assunto de rede social de interesse amplo.
