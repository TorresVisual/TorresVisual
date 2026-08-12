import hashlib
import os

import requests
from lxml import etree

HEADERS = {"authorization": "token " + os.environ.get("ACCESS_TOKEN", "")}
USER_NAME = os.environ.get("USER_NAME", "TorresVisual")
WAKATIME_API_KEY = os.environ.get("WAKATIME_API_KEY", "")


def format_number(value):
    return f"{value:,}"


def find_and_replace(root, element_id, new_text):
    """Finds the element in the SVG file and replaces its text with a new value."""
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def justify_format(root, element_id, new_text, length=0):
    """Updates the element's text and pads the sibling '_dots' element so
    the value stays right-justified at a fixed column width."""
    if isinstance(new_text, int):
        new_text = format_number(new_text)
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: "", 1: " ", 2: ". "}
        dot_string = dot_map[just_len]
    else:
        dot_string = " " + ("." * just_len) + " "
    find_and_replace(root, f"{element_id}_dots", dot_string)


def simple_request(func_name, query, variables):
    """Returns a request, or raises an Exception if the response does not succeed."""
    request = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=HEADERS,
    )
    if request.status_code == 200:
        return request
    raise Exception(func_name, "has failed with a", request.status_code, request.text)


def user_getter(username):
    """Returns the GraphQL node ID of the given user."""
    query = """
    query($login: String!){
        user(login: $login) {
            id
        }
    }"""
    request = simple_request(user_getter.__name__, query, {"login": username})
    return {"id": request.json()["data"]["user"]["id"]}


def follower_getter(username):
    """Returns the number of followers of the given user."""
    query = """
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }"""
    request = simple_request(follower_getter.__name__, query, {"login": username})
    return int(request.json()["data"]["user"]["followers"]["totalCount"])


def graph_repos_stars(count_type, owner_affiliation):
    """Returns the total repository count, total star count, or total disk
    usage (in KB) for the configured user, for the given ownership
    affiliation(s)."""
    if count_type not in ("repos", "stars", "disk_usage"):
        raise ValueError(f"unknown count_type: {count_type}")
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!) {
        user(login: $login) {
            repositories(first: 100, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            stargazers {
                                totalCount
                            }
                            diskUsage
                        }
                    }
                }
            }
        }
    }"""
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    data = request.json()["data"]["user"]["repositories"]
    if count_type == "repos":
        return data["totalCount"]
    if count_type == "stars":
        return sum(edge["node"]["stargazers"]["totalCount"] for edge in data["edges"])
    return sum(edge["node"]["diskUsage"] or 0 for edge in data["edges"])


def gist_counter(username):
    """Returns the number of public gists owned by the given user."""
    query = """
    query($login: String!){
        user(login: $login) {
            gists(privacy: PUBLIC) {
                totalCount
            }
        }
    }"""
    request = simple_request(gist_counter.__name__, query, {"login": username})
    return int(request.json()["data"]["user"]["gists"]["totalCount"])


def format_disk_usage(kilobytes):
    """Formats a disk usage size (in KB, as returned by GitHub's diskUsage
    field) as a human-readable MB/GB string."""
    megabytes = kilobytes / 1024
    if megabytes >= 1024:
        return f"{megabytes / 1024:.1f} GB"
    return f"{megabytes:.1f} MB"


def flush_cache(edges, cache_file):
    """Wipes the cache file and writes one zeroed-out line per repository."""
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    with open(cache_file, "w") as f:
        for edge in edges:
            repo_hash = hashlib.sha256(
                edge["node"]["nameWithOwner"].encode("utf-8")
            ).hexdigest()
            f.write(f"{repo_hash} 0 0 0 0\n")


def force_close_file(data, cache_file):
    """Saves whatever cache data is currently held in memory before the
    program exits abnormally (e.g. a GitHub anti-abuse rate limit)."""
    with open(cache_file, "w") as f:
        f.writelines(data)
    print(
        "There was an error while writing to the cache file.",
        cache_file,
        "has had the partial data saved and closed.",
    )


def recursive_loc(
    owner,
    repo_name,
    owner_id,
    cache_file,
    data,
    addition_total=0,
    deletion_total=0,
    my_commits=0,
    cursor=None,
):
    """Walks a repository's default-branch commit history 100 commits at a
    time via GraphQL cursor pagination, summing additions/deletions for
    commits authored by owner_id."""
    query = """
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
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    additions
                                    deletions
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
    }"""
    variables = {"repo_name": repo_name, "owner": owner, "cursor": cursor}
    request = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=HEADERS,
    )
    if request.status_code != 200:
        force_close_file(data, cache_file)
        if request.status_code == 403:
            raise Exception(
                "Too many requests in a short amount of time! "
                "You've hit the non-documented anti-abuse limit!"
            )
        raise Exception(
            "recursive_loc() has failed with a", request.status_code, request.text
        )

    default_branch = request.json()["data"]["repository"]["defaultBranchRef"]
    if default_branch is None:
        return 0, 0, 0

    history = default_branch["target"]["history"]
    for edge in history["edges"]:
        if edge["node"]["author"]["user"] == owner_id:
            my_commits += 1
            addition_total += edge["node"]["additions"]
            deletion_total += edge["node"]["deletions"]

    if not history["edges"] or not history["pageInfo"]["hasNextPage"]:
        return addition_total, deletion_total, my_commits
    return recursive_loc(
        owner,
        repo_name,
        owner_id,
        cache_file,
        data,
        addition_total,
        deletion_total,
        my_commits,
        history["pageInfo"]["endCursor"],
    )


def cache_builder(edges, owner_id, cache_file, force_cache=False):
    """Checks each repository in edges against the cache; re-walks only
    the repositories whose commit count has changed since the last run."""
    cached = True
    try:
        with open(cache_file, "r") as f:
            data = f.readlines()
    except FileNotFoundError:
        flush_cache(edges, cache_file)
        with open(cache_file, "r") as f:
            data = f.readlines()

    if len(data) != len(edges) or force_cache:
        cached = False
        flush_cache(edges, cache_file)
        with open(cache_file, "r") as f:
            data = f.readlines()

    for index in range(len(edges)):
        repo_hash, total_commits, *_rest = data[index].split()
        default_branch = edges[index]["node"]["defaultBranchRef"]
        if default_branch is None:
            data[index] = f"{repo_hash} 0 0 0 0\n"
            continue
        current_total = default_branch["target"]["history"]["totalCount"]
        if int(total_commits) != current_total:
            cached = False
            owner, repo_name = edges[index]["node"]["nameWithOwner"].split("/")
            additions, deletions, my_commits = recursive_loc(
                owner, repo_name, owner_id, cache_file, data
            )
            data[index] = f"{repo_hash} {current_total} {my_commits} {additions} {deletions}\n"

    with open(cache_file, "w") as f:
        f.writelines(data)

    loc_add = sum(int(line.split()[3]) for line in data)
    loc_del = sum(int(line.split()[4]) for line in data)
    return [loc_add, loc_del, loc_add - loc_del, cached]


def loc_query(owner_affiliation, owner_id, cache_file, cursor=None, edges=None):
    """Fetches every repository (paginated 60 at a time) for the given
    affiliation(s), then delegates to cache_builder for the LOC totals."""
    if edges is None:
        edges = []
    query = """
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
    }"""
    variables = {
        "owner_affiliation": owner_affiliation,
        "login": USER_NAME,
        "cursor": cursor,
    }
    request = simple_request(loc_query.__name__, query, variables)
    repositories = request.json()["data"]["user"]["repositories"]
    edges = edges + repositories["edges"]
    if repositories["pageInfo"]["hasNextPage"]:
        return loc_query(
            owner_affiliation,
            owner_id,
            cache_file,
            repositories["pageInfo"]["endCursor"],
            edges,
        )
    return cache_builder(edges, owner_id, cache_file)


def commit_counter(cache_file):
    """Sums the 'my commits' column across every cached repository line."""
    with open(cache_file, "r") as f:
        data = f.readlines()
    return sum(int(line.split()[2]) for line in data)


def wakatime_stats(api_key):
    """Returns (total, daily_average, top_language) from WakaTime's
    all-time stats: human-readable total coding time, human-readable
    average coding time per day, and the name of the most-used language
    ('N/A' if no language data is available)."""
    response = requests.get(
        "https://wakatime.com/api/v1/users/current/stats/all_time",
        params={"api_key": api_key},
    )
    if response.status_code != 200:
        raise Exception(
            "wakatime_stats() has failed with a", response.status_code, response.text
        )
    data = response.json()["data"]
    languages = data.get("languages") or []
    top_language = languages[0]["name"] if languages else "N/A"
    return data["human_readable_total"], data["human_readable_daily_average"], top_language


def svg_overwrite(
    filename,
    commit_data,
    star_data,
    repo_data,
    contrib_data,
    follower_data,
    loc_data,
    wakatime_total,
    wakatime_daily_average,
    wakatime_top_language,
    gist_data,
    disk_usage_data,
):
    """Parses an SVG template and fills in the live stats."""
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, "repo_data", repo_data, 10)
    justify_format(root, "contrib_data", contrib_data, 10)
    justify_format(root, "star_data", star_data, 10)
    justify_format(root, "follower_data", follower_data, 10)
    justify_format(root, "commit_data", commit_data, 14)
    justify_format(root, "loc_data", loc_data[2], 10)
    find_and_replace(root, "loc_add", format_number(loc_data[0]))
    justify_format(root, "loc_del", loc_data[1], 8)
    justify_format(root, "wakatime_data", wakatime_total, 14)
    justify_format(root, "wakatime_avg_data", wakatime_daily_average, 14)
    justify_format(root, "wakatime_lang_data", wakatime_top_language, 14)
    justify_format(root, "gist_data", gist_data, 10)
    justify_format(root, "disk_data", disk_usage_data, 10)
    tree.write(filename, encoding="utf-8", xml_declaration=True)


def main():
    owner_id = user_getter(USER_NAME)
    cache_file = os.path.join(
        "cache", hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt"
    )

    loc_data = loc_query(
        ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"], owner_id, cache_file
    )
    commit_data = commit_counter(cache_file)
    star_data = graph_repos_stars("stars", ["OWNER"])
    repo_data = graph_repos_stars("repos", ["OWNER"])
    contrib_data = graph_repos_stars(
        "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
    )
    disk_usage_kb = graph_repos_stars("disk_usage", ["OWNER"])
    follower_data = follower_getter(USER_NAME)
    gist_data = gist_counter(USER_NAME)
    wakatime_total, wakatime_daily_average, wakatime_top_language = wakatime_stats(
        WAKATIME_API_KEY
    )

    for svg_file in ("dark_mode.svg", "light_mode.svg"):
        svg_overwrite(
            svg_file,
            commit_data,
            star_data,
            repo_data,
            contrib_data,
            follower_data,
            loc_data[:3],
            wakatime_total,
            wakatime_daily_average,
            wakatime_top_language,
            gist_data,
            format_disk_usage(disk_usage_kb),
        )


if __name__ == "__main__":
    main()
