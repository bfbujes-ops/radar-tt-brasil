#!/usr/bin/env python3
"""Gera 180 dias de tendências SIMULADAS para testar o Radar sem coletar nada.

O comportamento imita o dos TTs reais: ~8 termos novos por hora, a maioria dura
poucas horas, futebol em noites de jogo, programas de TV à noite. Alguns episódios
de exemplo (como "shampoo") são injetados para demonstrar a busca.
Nada aqui é dado real.
"""
import datetime as dt
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RADAR_DB", str(Path(__file__).resolve().parents[1] / "data" / "demo.db"))
import radar  # noqa: E402

random.seed(42)
DAYS = 180

NOMES = ("Ana Bia Caio Duda Enzo Felipe Gabi Heitor Igor Juliana Kaique Larissa Luna Maique Nina Otávio Paula "
         "Rafa Sabrina Thiago Ursula Vitor Wanda Yasmin Zeca Bruna Davi Érika Fábio Gustavo Helena Isis João "
         "Kátia Léo Marina Natan Olívia Pedro Renata Sofia Tiago Valentina Wesley Alice Bernardo Cecília").split()
SOBRENOMES = "Silva Souza Lima Costa Rocha Alves Pereira Duarte Moura Teixeira Barros Freitas Campos".split()
PALAVRAS = ("Calor Chuva Feriado Pix Enem Horário Apagão Show Ingresso Final Golaço Pênalti VAR Árbitro Treta "
            "Live Clipe Álbum Turnê Trailer Série Episódio Spoiler Vacina Gasolina Salário Greve Metrô Trânsito "
            "Cabelo Meme Print Áudio Vídeo Fofoca Cancelado Votação Eliminação Paredão Prova Festa Casamento "
            "Divórcio Namoro Assalto Golpe Promoção Cupom Aplicativo Instabilidade Senado Câmara STF Ministro").split()
TIMES = ("Flamengo Corinthians Palmeiras São Paulo Vasco Grêmio Inter Cruzeiro Botafogo Fluminense Santos "
         "Bahia Fortaleza Atlético").split(" ")
TIMES = ["São Paulo" if t == "São" else t for t in TIMES if t != "Paulo"]


def novo_termo():
    r = random.random()
    if r < 0.35:
        return random.choice(NOMES)
    if r < 0.50:
        return f"{random.choice(NOMES)} {random.choice(SOBRENOMES)}"
    if r < 0.75:
        return random.choice(PALAVRAS)
    if r < 0.90:
        a, b = random.sample(PALAVRAS + NOMES, 2)
        return f"#{a}{b}".replace(" ", "")
    return f"{random.choice(PALAVRAS).upper()} {random.choice(['SIM', 'NÃO', 'JÁ', 'AGORA'])}"


# episódios de exemplo para a demonstração: (dias atrás, hora UTC, duração h, melhor posição, termo, resumo)
EXEMPLOS = [
    (134, 15, 9, 4, "Shampoo",
     '"Shampoo" Porque um vídeo testando shampoo de cavalo no cabelo viralizou e virou debate (exemplo fictício).',
     ["Cabelo", "#TesteDeBeleza", "Vídeo"]),
    (97, 23, 3, 27, "Shampoo", None, ["Paredão", "Eliminação", "#RealityAoVivo"]),
    (61, 18, 14, 2, "#ShampooChallenge",
     '"#ShampooChallenge" Porque um desafio de trocar o shampoo por produtos inusitados tomou o TikTok e chegou ao X (exemplo fictício).',
     ["TikTok", "Desafio", "Caspa"]),
    (19, 12, 3, 31, "shampoo seco",
     '"shampoo seco" Porque uma marca fictícia anunciou recall de um lote de shampoo seco (exemplo fictício).',
     ["Recall", "Anvisa", "Lote"]),
    (5, 13, 20, 1, "Enem", '"Enem" Porque foi o primeiro dia de provas e o tema da redação dominou a conversa (exemplo).',
     ["Redação", "Gabarito", "INEP"]),
    (40, 1, 6, 3, "Apagão", '"Apagão" Porque usuários de várias cidades relataram falta de luz ao mesmo tempo (exemplo fictício).',
     ["Luz", "Enel", "Calor"]),
]


def main():
    db = Path(os.environ["RADAR_DB"])
    if db.exists():
        db.unlink()
    con = radar.connect(db)

    end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    start = end - dt.timedelta(days=DAYS)

    injected = {}  # hora -> [(termo, posição)]
    contexts = []
    for dias, hora, dur, best, termo, resumo, cotermos in EXEMPLOS:
        t0 = (end - dt.timedelta(days=dias)).replace(hour=hora)
        contexts.append((termo, radar.hour_bucket(t0), resumo))
        for i in range(dur):
            ts = radar.hour_bucket(t0 + dt.timedelta(hours=i))
            # sobe até o pico no 1º terço e depois cai
            pico = max(1, dur // 3)
            pos = best + abs(i - pico) * random.randint(2, 5)
            if pos <= 50:
                injected.setdefault(ts, []).append((termo, pos))
            for j, ct in enumerate(cotermos):
                injected.setdefault(ts, []).append((ct, min(50, best + 2 + j * 3 + random.randint(0, 6))))

    ativos = {}  # termo -> [força, horas restantes]
    t = start
    while t <= end:
        local = (t - dt.timedelta(hours=3))  # horário de Brasília
        atividade = 0.4 if 2 <= local.hour < 8 else 1.0
        for _ in range(int(random.gauss(8, 2) * atividade) + 1):
            ativos[novo_termo()] = [random.lognormvariate(0, 1), max(1, int(random.expovariate(1 / 5)))]
        # futebol: qua/qui à noite e fim de semana à tarde
        if (local.weekday() in (2, 3) and 19 <= local.hour <= 23) or (local.weekday() >= 5 and 15 <= local.hour <= 21):
            for time_ in random.sample(TIMES, 4):
                ativos[time_] = [random.lognormvariate(1, 0.6), 3]
        if 22 <= local.hour or local.hour < 1:
            ativos["#RealityAoVivo"] = [random.lognormvariate(1.3, 0.4), 2]
        if local.day in (5, 6, 20) and 8 <= local.hour <= 12:
            ativos["Salário"] = [random.lognormvariate(0.8, 0.3), 2]

        ranking = sorted(ativos, key=lambda k: ativos[k][0] * random.uniform(0.7, 1.3), reverse=True)
        ts = radar.hour_bucket(t)
        fixos = injected.get(ts, [])
        fixos_nomes = {radar.normalize(n) for n, _ in fixos}
        lista = [n for n in ranking if radar.normalize(n) not in fixos_nomes][:50]
        for termo, pos in sorted(fixos, key=lambda x: x[1]):
            lista.insert(min(pos - 1, len(lista)), termo)
        lista = lista[:50]
        radar.save_snapshot(con, ts, [{"term": n, "rank": i + 1, "tweet_count": None} for i, n in enumerate(lista)],
                            "demo")

        for k in list(ativos):
            ativos[k][0] *= random.uniform(0.6, 1.05)
            ativos[k][1] -= 1
            if ativos[k][1] <= 0:
                del ativos[k]
        t += dt.timedelta(hours=1)

    for termo, ts, resumo in contexts:
        if resumo:
            con.execute("INSERT OR REPLACE INTO contexts VALUES (?,?,?,?,?)",
                        (radar.normalize(termo), ts, resumo, "[]", ts))
    con.commit()
    n = con.execute("SELECT COUNT(*), COUNT(DISTINCT norm) FROM snapshots").fetchone()
    print(f"demo: {n[0]} linhas, {n[1]} termos distintos, {DAYS} dias → {db}")


if __name__ == "__main__":
    main()
