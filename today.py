import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Configura l'header per l'autenticazione all'API GitHub.
# Assicurati che le variabili d'ambiente ACCESS_TOKEN e USER_NAME siano correttamente definite.
HEADERS = {
    'Authorization': 'Bearer ' + os.environ['ACCESS_TOKEN'],
    'Accept': 'application/vnd.github.v3+json'
}
USER_NAME = os.environ.get('USER_NAME', 'andreamoleri')
QUERY_COUNT = {
    'graph_commits': 0,
    'graph_repos_stars': 0,
    'recursive_loc': 0,
    'loc_query': 0,
    'user_getter': 0,
    'follower_getter': 0
}
OWNER_ID = None

def daily_readme(birthday):
    """
    Restituisce il tempo trascorso dalla data di nascita,
    nel formato 'XX years, XX months, XX days'
    """
    today = datetime.date.today()  # Usa solo la data senza orario
    diff = relativedelta.relativedelta(today, birthday)  # Ordine corretto: (end, start)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else ''
    )

def format_plural(unit):
    """
    Restituisce la "s" se unit è diverso da 1, altrimenti restituisce ''.
    """
    return 's' if unit != 1 else ''

def simple_request(func_name, query, variables):
    """
    Esegue una richiesta POST all'API GitHub GraphQL e restituisce la risposta.
    Se la risposta contiene errori o manca il campo "data", solleva un'eccezione.
    """
    request = requests.post('https://api.github.com/graphql',
                            json={'query': query, 'variables': variables},
                            headers=HEADERS)
    if request.status_code == 200:
        response_json = request.json()
        if 'errors' in response_json:
            raise Exception(func_name, ' returned errors:', response_json['errors'], QUERY_COUNT)
        if 'data' not in response_json:
            raise Exception(func_name, ' returned response without data:', response_json, QUERY_COUNT)
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)

def graph_commits(start_date, end_date):
    """
    Utilizza l'API GraphQL di GitHub per restituire il numero totale di commit effettuati.
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
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])

def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Utilizza l'API GraphQL di GitHub per restituire il numero totale di repository o il conteggio delle stelle.
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
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])

def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None, retries=3, delay=5):
    """
    Utilizza l'API GraphQL di GitHub per contare le LOC (linee di codice) in modo ricorsivo,
    gestendo eventuali errori 502 con retry.
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}

    for attempt in range(retries):
        request = requests.post('https://api.github.com/graphql',
                                json={'query': query, 'variables': variables},
                                headers=HEADERS)

        if request.status_code == 200:
            response_json = request.json()
            if response_json.get('data', {}).get('repository', {}).get('defaultBranchRef') is not None:
                return loc_counter_one_repo(owner, repo_name, data, cache_comment,
                                            response_json['data']['repository']['defaultBranchRef']['target']['history'],
                                            addition_total, deletion_total, my_commits)
            else:
                return 0  # Repository vuota

        if request.status_code == 502:
            print(f'GitHub API Error 502 - retrying ({attempt + 1}/{retries}) in {delay} seconds...')
            time.sleep(delay)
            continue

        force_close_file(data, cache_comment)

        if request.status_code == 403:
            raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')

        raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)

    force_close_file(data, cache_comment)
    raise Exception('recursive_loc() has failed after multiple retries with a 502 error', QUERY_COUNT)

def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Aggrega il conteggio LOC per i commit effettuati dall'utente specificato.
    """
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(owner, repo_name, data, cache_comment,
                             addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])

def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Utilizza l'API GraphQL di GitHub per interrogare le repository a cui l'utente ha accesso
    e restituisce il conteggio totale delle linee di codice.
    """
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
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    response_data = request.json()['data']['user']['repositories']
    if response_data['pageInfo']['hasNextPage']:
        edges += response_data['edges']
        return loc_query(owner_affiliation, comment_size, force_cache, response_data['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + response_data['edges'], comment_size, force_cache)

def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Controlla per ogni repository se è stato aggiornato rispetto all'ultima cache.
    Se sì, aggiorna il conteggio LOC per quella repository.
    """
    cached = True  # Assume che tutte le repository siano in cache
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError:
                data[index] = repo_hash + ' 0 0 0 0\n'

    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]

def flush_cache(edges, filename, comment_size):
    """
    Ripulisce il file di cache.
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')

def add_archive():
    """
    Aggiunge repository che sono state cancellate, utilizzando i dati salvati.
    """
    with open('cache/repository_archive.txt', 'r') as f:
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
    added_commits += int(old_data[-1].split()[4][:-1])
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]

def force_close_file(data, cache_comment):
    """
    Salva e chiude il file in caso di errore, preservando i dati parziali.
    """
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('Si è verificato un errore durante la scrittura della cache. Il file', filename, 'è stato chiuso con i dati parziali salvati.')

def stars_counter(data):
    """
    Conta il totale delle stelle nelle repository.
    """
    total_stars = 0
    for node in data:
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Aggiorna il file SVG con i dati relativi ad età, commit, stelle, repository e LOC.
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    a = 22 - len(perf_counter(commit_counter, 7))
    justify_format(root, 'commit_data', commit_data, a)

    x = 53 - len(perf_counter(daily_readme, datetime.date(2000, 7, 6)))
    justify_format(root, 'age_data', age_data, x)

    y = 22 - len(perf_counter(graph_repos_stars, 'stars', ['OWNER']))
    justify_format(root, 'star_data', star_data, y)

    z = 7 - len(perf_counter(graph_repos_stars, 'repos', ['OWNER']))
    justify_format(root, 'repo_data', repo_data, z)

    justify_format(root, 'contrib_data', contrib_data)

    b = 18 - len(perf_counter(follower_getter, USER_NAME))
    justify_format(root, 'follower_data', follower_data, b)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)

def justify_format(root, element_id, new_text, length=0):
    """
    Aggiorna e formatta il testo dell'elemento SVG,
    regolando anche la quantità di punti per l'allineamento.
    """
    if isinstance(new_text, int):
        new_text = f"{new_text:,}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    dot_count = max(0, length - len(new_text))
    dot_string = '.' * dot_count
    find_and_replace(root, f"{element_id}_dots", dot_string)

def find_and_replace(root, element_id, new_text):
    """
    Trova l'elemento con l'id specificato nell'SVG e ne sostituisce il testo.
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text

def commit_counter(comment_size):
    """
    Conta il totale dei commit utilizzando il file di cache generato.
    """
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'r') as f:
        data = f.readlines()
    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits

def user_getter(username):
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
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(username):
    """
    Restituisce il numero di follower dell'utente.
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])

def query_count(funct_id):
    """
    Incrementa il contatore per il numero di chiamate all'API GraphQL.
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1

def perf_counter(funct, *args):
    """
    Misura il tempo di esecuzione di una funzione.
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start

def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Stampa il tempo di esecuzione formattato.
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    if difference > 1:
        print('{:>12}'.format('%.4f' % difference + ' s '))
    else:
        print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return

if __name__ == '__main__':
    """
    Esecuzione principale per l'utente specificato.
    """
    print('Calculation times:')
    # Imposta OWNER_ID e ottiene la data di creazione dell'account
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)

    age_data, age_time = perf_counter(daily_readme, datetime.date(2000, 7, 6))
    formatter('age calculation', age_time)

    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    if total_loc[-1]:
        formatter('LOC (cached)', loc_time)
    else:
        formatter('LOC (no cache)', loc_time)

    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    # Aggiunge repository archiviate per l'utente specifico
    if OWNER_ID == {'id': 'MDQ6VXNlcjU3MzMxMTM0'}:
        archived_data = add_archive()
        for index in range(len(total_loc)-1):
            total_loc[index] += archived_data[index]
        contrib_data += archived_data[-1]
        commit_data += int(archived_data[-2])

    for index in range(len(total_loc)-1):
        total_loc[index] = '{:,}'.format(total_loc[index])

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    # Aggiorna la stampa finale dei tempi di esecuzione
    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
          '{:<21}'.format('Total function time:'),
          '{:>11}'.format('%.4f' % (user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time)),
          ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E',
          sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items():
        print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
