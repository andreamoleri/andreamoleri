#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
today.py — versione robusta

Correzioni principali:
- Convalida variabili d'ambiente (ACCESS_TOKEN o GITHUB_TOKEN) e USER_NAME.
- Header HTTP completi per GitHub GraphQL (Authorization Bearer, Accept, Content-Type, User-Agent).
- Gestione chiara dell'errore 401 "Bad credentials" con messaggi utili.
- Retry/backoff per errori transitori (502/5xx) e rate limit (403 con retry-after).
- Fix per default mutabili (edges=None).
- Creazione automatica della cartella cache/.
- Migliorata la gestione della cache e chiusura sicura file.
- Migliore formattazione dei numeri e dei tempi.
"""

import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib
from typing import Optional, Tuple, Any, Dict, List

# =========================
# Config / Utility
# =========================

SESSION = requests.Session()
REQUEST_TIMEOUT = 30  # secondi
GRAPHQL_ENDPOINT = 'https://api.github.com/graphql'

def _env(var: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(var, default)
    return val.strip() if isinstance(val, str) else val

def get_token() -> str:
    """
    Recupera il token da ACCESS_TOKEN o GITHUB_TOKEN (ordine di preferenza).
    Lancia ValueError con messaggio chiaro se manca o appare mascherato.
    """
    token = _env('ACCESS_TOKEN') or _env('GITHUB_TOKEN')
    if not token:
        raise ValueError(
            "Nessun token trovato. Imposta ACCESS_TOKEN (consigliato) o GITHUB_TOKEN nelle variabili d'ambiente."
        )
    # In molti CI si vedono '***' nei log; se davvero è '***', è token mascherato/non iniettato.
    if token.strip('*') == '':
        raise ValueError(
            "Il token letto vale '***' (mascherato) o vuoto. Assicurati che il secret sia effettivamente passato al job."
        )
    return token

def build_headers(token: str) -> Dict[str, str]:
    return {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'User-Agent': 'today.py/robust-1.0'
    }

USER_NAME = _env('USER_NAME', 'andreamoleri')
QUERY_COUNT = {
    'graph_commits': 0,
    'graph_repos_stars': 0,
    'recursive_loc': 0,
    'loc_query': 0,
    'user_getter': 0,
    'follower_getter': 0
}
OWNER_ID: Optional[str] = None  # sarà l'ID utente GitHub (stringa)

def ensure_cache_dir() -> None:
    os.makedirs('cache', exist_ok=True)

def format_int(n: int) -> str:
    return f"{n:,}"

def format_plural(unit: int) -> str:
    return 's' if unit != 1 else ''

def query_count(funct_id: str) -> None:
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1

# =========================
# Funzioni Data/Output
# =========================

def daily_readme(birthday: datetime.date) -> str:
    """
    Restituisce il tempo trascorso dalla data di nascita,
    nel formato 'XX years, XX months, XX days' + 🎂 nel giorno del compleanno.
    """
    today = datetime.date.today()
    diff = relativedelta.relativedelta(today, birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else ''
    )

# =========================
# HTTP / GraphQL Helpers
# =========================

def _request_with_retries(payload: Dict[str, Any], headers: Dict[str, str], func_name: str,
                          max_retries: int = 5, base_delay: float = 1.5) -> requests.Response:
    """
    Esegue POST GraphQL con retry/backoff su 5xx e gestione 403/401.
    Solleva Exception con messaggio chiaro se fallisce definitivamente.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = SESSION.post(
                GRAPHQL_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as e:
            if attempt >= max_retries:
                raise Exception(f"{func_name} — errore di rete non recuperabile: {e}") from e
            time.sleep(base_delay * attempt)
            continue

        # Gestione codici
        if resp.status_code == 200:
            return resp

        if resp.status_code == 401:
            raise Exception(
                f"{func_name} — 401 Bad credentials. "
                "Controlla che il token GitHub sia valido e abbia i permessi necessari. "
                "Nel CI assicurati che il secret sia passato correttamente al job."
            )

        if resp.status_code == 403:
            # potenziale rate limit; prova ad attendere se presente header Retry-After
            retry_after = resp.headers.get('Retry-After')
            if attempt >= max_retries:
                raise Exception(
                    f"{func_name} — 403 Forbidden/Rate limited. Risposta: {resp.text}"
                )
            delay = float(retry_after) if retry_after and retry_after.isdigit() else base_delay * attempt
            time.sleep(delay)
            continue

        if 500 <= resp.status_code < 600:
            if attempt >= max_retries:
                raise Exception(f"{func_name} — {resp.status_code} dopo vari retry. Risposta: {resp.text}")
            time.sleep(base_delay * attempt)
            continue

        # Altri errori
        raise Exception(f"{func_name} — {resp.status_code}. Risposta: {resp.text}")

def simple_request(func_name: str, query: str, variables: Dict[str, Any], headers: Dict[str, str]) -> requests.Response:
    """
    POST all'API GitHub GraphQL e restituisce la risposta.
    Gestisce errori di payload GraphQL.
    """
    resp = _request_with_retries({'query': query, 'variables': variables}, headers, func_name)
    data = resp.json()
    if 'errors' in data:
        raise Exception(f"{func_name} — GraphQL errors: {data['errors']}")
    if 'data' not in data:
        raise Exception(f"{func_name} — risposta senza 'data': {data}")
    return resp

# =========================
# Query GitHub
# =========================

def graph_commits(headers: Dict[str, str], start_date: str, end_date: str) -> int:
    """
    Numero totale di contributi (commit) effettuati in un intervallo.
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables, headers)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])

def graph_repos_stars(headers: Dict[str, str], count_type: str, owner_affiliation: List[str],
                      cursor: Optional[str] = None) -> int:
    """
    Restituisce numero totale repository o somma stelle per owner_affiliation.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers { totalCount }
                        }
                    }
                }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables, headers)
    js = request.json()['data']['user']['repositories']
    if count_type == 'repos':
        return int(js['totalCount'])
    elif count_type == 'stars':
        return stars_counter(js['edges'])
    else:
        raise ValueError("count_type deve essere 'repos' o 'stars'.")

def recursive_loc(headers: Dict[str, str], owner: str, repo_name: str, data_lines: List[str], cache_comment: List[str],
                  addition_total: int = 0, deletion_total: int = 0, my_commits: int = 0,
                  cursor: Optional[str] = None, retries: int = 5, delay: float = 1.5):
    """
    Conta le LOC (linee di codice) dai commit dell'utente sul branch di default del repository (ricorsivo).
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author { user { id } }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo { endCursor hasNextPage }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}

    attempt = 0
    while True:
        attempt += 1
        try:
            resp = SESSION.post(GRAPHQL_ENDPOINT, json={'query': query, 'variables': variables},
                                headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt >= retries:
                force_close_file(data_lines, cache_comment)
                raise Exception(f"recursive_loc — errore di rete: {e}") from e
            time.sleep(delay * attempt)
            continue

        if resp.status_code == 200:
            response_json = resp.json()
            repo_data = response_json.get('data', {}).get('repository', {})
            dbr = repo_data.get('defaultBranchRef')
            if dbr is None:
                return 0, 0, 0  # repository vuota o senza default branch
            history = dbr['target']['history']
            return loc_counter_one_repo(headers, owner, repo_name, data_lines, cache_comment, history,
                                        addition_total, deletion_total, my_commits)

        if resp.status_code in (502, 503, 504):
            if attempt >= retries:
                force_close_file(data_lines, cache_comment)
                raise Exception('recursive_loc — fallito dopo vari retry su 5xx.')
            time.sleep(delay * attempt)
            continue

        if resp.status_code == 403:
            force_close_file(data_lines, cache_comment)
            raise Exception('Too many requests in a short time! Anti-abuse limit (403).')

        force_close_file(data_lines, cache_comment)
        raise Exception(f"recursive_loc — HTTP {resp.status_code}: {resp.text}")

def loc_counter_one_repo(headers: Dict[str, str], owner: str, repo_name: str, data_lines: List[str],
                         cache_comment: List[str], history: Dict[str, Any],
                         addition_total: int, deletion_total: int, my_commits: int):
    """
    Aggrega il conteggio LOC per i commit effettuati dall'utente specificato (OWNER_ID).
    """
    global OWNER_ID
    for edge in history['edges']:
        node = edge['node']
        author_user = node.get('author', {}).get('user')
        if author_user and author_user.get('id') == OWNER_ID:
            my_commits += 1
            addition_total += int(node.get('additions', 0))
            deletion_total += int(node.get('deletions', 0))

    if not history['edges'] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(headers, owner, repo_name, data_lines, cache_comment,
                             addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])

def loc_query(headers: Dict[str, str], owner_affiliation: List[str], comment_size: int = 0,
              force_cache: bool = False, cursor: Optional[str] = None, edges: Optional[List[Dict[str, Any]]] = None):
    """
    Interroga le repository a cui l'utente ha accesso e restituisce il conteggio totale delle linee di codice.
    """
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history { totalCount }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables, headers)
    repositories = request.json()['data']['user']['repositories']
    edges.extend(repositories['edges'])
    if repositories['pageInfo']['hasNextPage']:
        return loc_query(headers, owner_affiliation, comment_size, force_cache, repositories['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(headers, edges, comment_size, force_cache)

def cache_builder(headers: Dict[str, str], edges: List[Dict[str, Any]], comment_size: int,
                  force_cache: bool, loc_add: int = 0, loc_del: int = 0):
    """
    Verifica se ogni repo è aggiornata rispetto alla cache; se no, aggiorna la riga.
    """
    ensure_cache_dir()
    cached = True
    filename = os.path.join('cache', hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt')

    if not os.path.exists(filename):
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w', encoding='utf-8') as f:
            f.writelines(data)
        # inizializza corpo cache
        flush_cache(edges, filename, comment_size)

    with open(filename, 'r', encoding='utf-8') as f:
        data = f.readlines()

    # Se dimensioni diverse o forzata rigenera base
    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r', encoding='utf-8') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    body = data[comment_size:]

    for index in range(len(edges)):
        repo_hash, commit_count, *_rest = body[index].split()
        name_with_owner = edges[index]['node']['nameWithOwner']
        expected_hash = hashlib.sha256(name_with_owner.encode('utf-8')).hexdigest()
        if repo_hash == expected_hash:
            try:
                current_total = int(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount'])
            except (TypeError, KeyError):
                # repo senza defaultBranchRef o dati mancanti
                body[index] = f"{expected_hash} 0 0 0 0\n"
                continue

            if int(commit_count) != current_total:
                owner, repo_name = name_with_owner.split('/')
                loc_add_sum, loc_del_sum, my_commits = recursive_loc(headers, owner, repo_name, body, cache_comment)
                body[index] = f"{expected_hash} {current_total} {my_commits} {loc_add_sum} {loc_del_sum}\n"

    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(cache_comment)
        f.writelines(body)

    for line in body:
        parts = line.split()
        if len(parts) >= 5:
            loc_add += int(parts[3])
            loc_del += int(parts[4])

    return [loc_add, loc_del, loc_add - loc_del, cached]

def flush_cache(edges: List[Dict[str, Any]], filename: str, comment_size: int) -> None:
    """
    Ripulisce e riempie il file di cache con righe iniziali per ogni repo.
    """
    header = []
    if os.path.exists(filename) and comment_size > 0:
        with open(filename, 'r', encoding='utf-8') as f:
            header = f.readlines()[:comment_size]

    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(header)
        for node in edges:
            name = node['node']['nameWithOwner']
            f.write(hashlib.sha256(name.encode('utf-8')).hexdigest() + ' 0 0 0 0\n')

def add_archive():
    """
    Aggiunge repository cancellate utilizzando dati salvati (se presenti).
    """
    path = os.path.join('cache', 'repository_archive.txt')
    if not os.path.exists(path):
        # Nessun archivio: ritorna zeri
        return [0, 0, 0, 0, 0]

    with open(path, 'r', encoding='utf-8') as f:
        data = f.readlines()

    old_data = data
    data = data[7:len(data)-3]
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        repo_hash, total_commits, my_commits, *loc = line.split()
        added_loc += int(loc[0])
        deleted_loc += int(loc[1])
        if my_commits.isdigit():
            added_commits += int(my_commits)
    try:
        added_commits += int(old_data[-1].split()[4][:-1])
    except Exception:
        pass
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]

def force_close_file(data: List[str], cache_comment: List[str]) -> None:
    """
    Salva e chiude il file in caso di errore, preservando i dati parziali.
    """
    ensure_cache_dir()
    filename = os.path.join('cache', hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt')
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.writelines(cache_comment)
            f.writelines(data)
        print('⚠️  Errore durante la scrittura della cache. File salvato parzialmente in', filename)
    except Exception as e:
        print('⚠️  Impossibile salvare la cache parziale:', e)

def stars_counter(data: List[Dict[str, Any]]) -> int:
    """
    Somma il totale delle stelle nelle repository.
    """
    total_stars = 0
    for node in data:
        total_stars += int(node['node']['stargazers']['totalCount'])
    return total_stars

# =========================
# SVG Helpers
# =========================

def svg_overwrite(filename: str, age_data: str, commit_data: int, star_data: int, repo_data: int,
                  contrib_data: int, follower_data: int, loc_data: List[Any]) -> None:
    """
    Aggiorna il file SVG con i dati relativi ad età, commit, stelle, repository, contribuzioni, follower e LOC.
    """
    tree = etree.parse(filename)
    root = tree.getroot()

    a = 22 - len(str(commit_data))
    justify_format(root, 'commit_data', commit_data, a)

    x = 53 - len(age_data)
    justify_format(root, 'age_data', age_data, x)

    y = 22 - len(str(star_data))
    justify_format(root, 'star_data', star_data, y)

    z = 7 - len(str(repo_data))
    justify_format(root, 'repo_data', repo_data, z)

    justify_format(root, 'contrib_data', contrib_data)

    b = 18 - len(str(follower_data))
    justify_format(root, 'follower_data', follower_data, b)

    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)

    tree.write(filename, encoding='utf-8', xml_declaration=True)

def justify_format(root, element_id: str, new_text: Any, length: int = 0) -> None:
    """
    Aggiorna e formatta il testo dell'elemento SVG regolando anche i puntini per l'allineamento.
    """
    if isinstance(new_text, int):
        new_text = format_int(new_text)
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    dot_count = max(0, length - len(new_text))
    dot_string = '.' * dot_count
    find_and_replace(root, f"{element_id}_dots", dot_string)

def find_and_replace(root, element_id: str, new_text: str) -> None:
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text

# =========================
# Cache counters
# =========================

def commit_counter(comment_size: int) -> int:
    """
    Conta il totale dei commit utilizzando il file di cache generato.
    """
    filename = os.path.join('cache', hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt')
    if not os.path.exists(filename):
        return 0
    total_commits = 0
    with open(filename, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    data = lines[comment_size:]
    for line in data:
        parts = line.split()
        if len(parts) >= 3 and parts[2].isdigit():
            total_commits += int(parts[2])
    return total_commits

# =========================
# Utente / Follower
# =========================

def user_getter(headers: Dict[str, str], username: str) -> Tuple[Dict[str, str], str]:
    """
    Restituisce l'ID dell'utente e la data di creazione dell'account.
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables, headers)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(headers: Dict[str, str], username: str) -> int:
    """
    Restituisce il numero di follower dell'utente.
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers { totalCount }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username}, headers)
    return int(request.json()['data']['user']['followers']['totalCount'])

# =========================
# Timing/Formatter
# =========================

def perf_counter(funct, *args):
    """
    Misura il tempo di esecuzione di una funzione e restituisce (return, seconds).
    """
    start = time.perf_counter()
    result = funct(*args)
    elapsed = time.perf_counter() - start
    return result, elapsed

def formatter(query_type: str, difference: float, funct_return: Optional[Any] = False, whitespace: int = 0):
    """
    Stampa il tempo di esecuzione formattato e opzionalmente ritorna una stringa con padding.
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    if difference > 1:
        print('{:>12}'.format(f'{difference:.4f} s '))
    else:
        print('{:>12}'.format(f'{difference*1000:.4f} ms'))
    if whitespace:
        if isinstance(funct_return, int):
            s = format_int(funct_return)
        else:
            s = str(funct_return)
        return f"{s: <{whitespace}}"
    return funct_return

# =========================
# Main
# =========================

if __name__ == '__main__':
    print('Calculation times:')

    # Prepara token e header
    try:
        TOKEN = get_token()
    except ValueError as e:
        # Messaggio chiaro, uscita con non-zero code tramite eccezione
        raise SystemExit(f"❌ Configurazione token non valida: {e}")

    HEADERS = build_headers(TOKEN)

    # 1) Dati account (ID e createdAt)
    try:
        user_data, user_time = perf_counter(user_getter, HEADERS, USER_NAME)
    except Exception as e:
        # Mostra contesto migliore per il classico 401
        raise SystemExit(f"❌ user_getter fallito: {e}")
    formatter('account data', user_time)

    # Imposta OWNER_ID come stringa ID
    OWNER_ID = user_data['id']

    # 2) Età "daily_readme"
    age_data, age_time = perf_counter(daily_readme, datetime.date(2000, 7, 6))
    formatter('age calculation', age_time)

    # 3) LOC (cache)
    try:
        total_loc, loc_time = perf_counter(loc_query, HEADERS, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    except Exception as e:
        raise SystemExit(f"❌ loc_query fallita: {e}")
    if total_loc[-1]:
        formatter('LOC (cached)', loc_time)
    else:
        formatter('LOC (no cache)', loc_time)

    # 4) Commit dalla cache
    commit_data, commit_time = perf_counter(commit_counter, 7)

    # 5) Stelle e Repo
    try:
        star_data, star_time = perf_counter(graph_repos_stars, HEADERS, 'stars', ['OWNER'])
        repo_data, repo_time = perf_counter(graph_repos_stars, HEADERS, 'repos', ['OWNER'])
        contrib_data, contrib_time = perf_counter(graph_repos_stars, HEADERS, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    except Exception as e:
        raise SystemExit(f"❌ graph_repos_stars fallita: {e}")

    # 6) Follower
    try:
        follower_data, follower_time = perf_counter(follower_getter, HEADERS, USER_NAME)
    except Exception as e:
        raise SystemExit(f"❌ follower_getter fallita: {e}")

    # 7) Aggiunge repository archiviate per ID specifico (opzionale)
    if OWNER_ID == 'MDQ6VXNlcjU3MzMxMTM0':
        archived_data = add_archive()
        for index in range(len(total_loc)-1):
            total_loc[index] += archived_data[index]
        contrib_data += archived_data[-1]
        commit_data += int(archived_data[-2])

    # 8) Formatta LOC per scrittura SVG
    for index in range(len(total_loc)-1):
        total_loc[index] = format_int(total_loc[index])

    # 9) Aggiorna SVG (se presenti nella directory)
    for svg_file in ('dark_mode.svg', 'light_mode.svg'):
        if os.path.exists(svg_file):
            svg_overwrite(svg_file, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    # 10) Stampa riepilogo tempi
    total_time = user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time
    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
          '{:<21}'.format('Total function time:'),
          '{:>11}'.format(f'{total_time:.4f}'),
          ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E',
          sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items():
        print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
