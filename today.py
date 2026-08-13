import datetime
import hashlib
import os
import time

import requests
from dateutil import relativedelta
from lxml import etree

HEADERS = {"authorization": "token " + os.environ.get("ACCESS_TOKEN", "")}
USER_NAME = os.environ.get("USER_NAME", "TorresVisual")
WAKATIME_API_KEY = os.environ.get("WAKATIME_API_KEY", "")
BIRTHDAY = datetime.datetime(2007, 5, 17)

# GitHub's GraphQL API occasionally 502s on expensive queries (e.g. walking
# a repo with a lot of commit history) under transient backend load. These
# are worth retrying; other statuses (403 anti-abuse, 4xx client errors)
# are not, and are handled separately by each caller.
RETRYABLE_STATUS_CODES = {502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

SVG_NS = "{http://www.w3.org/2000/svg}"

# The info column is a plain monospace character grid. Every Consolas glyph --
# including '.', ' ' and '-' -- advances 0.5498em, which the templates'
# "size-adjust: 109%" scales to 0.599em so Consolas lines up with the
# DejaVu/Liberation Mono fallback used on machines without it. At the column's
# 13px that is ~7.79px per character, so x=507.5..962.5 holds exactly 58 of
# them. That start x (and the ASCII art's x=22.5) center the whole card
# horizontally: 22.5px on the left of the ASCII art balances the 22.5px
# between the info column's right edge and the card's 985px width.
INFO_COLUMN_X = "507.5"
INFO_COLUMN_CHARS = 58
MIN_DOTS = 3
# A blank column either side of the dots, so the leader never touches the
# colon or the value.
LEADER_PADDING = " "


def format_number(value):
    return f"{value:,}"


def format_plural(unit):
    return "s" if unit != 1 else ""


def daily_age(birthday, now=None):
    """Returns a human-readable age string ('X years, Y months, Z days')
    as of `now` (defaults to today)."""
    if now is None:
        now = datetime.datetime.today()
    diff = relativedelta.relativedelta(now, birthday)
    return "{} {}, {} {}, {} {}".format(
        diff.years, "year" + format_plural(diff.years),
        diff.months, "month" + format_plural(diff.months),
        diff.days, "day" + format_plural(diff.days),
    )


def find_and_replace(root, element_id, new_text):
    """Finds the element in the SVG file and replaces its text with a new value."""
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def dot_leader(text_around_dots, column_chars=INFO_COLUMN_CHARS):
    """Dots that pad an info line out to the full width of the column.
    text_around_dots is everything else on that line -- the ". " gutter, the
    label, the ":" and the value -- so the padded leader spans the whole gap
    and the value lands on the column's right edge."""
    padding = len(LEADER_PADDING) * 2
    dots = max(MIN_DOTS, column_chars - len(text_around_dots) - padding)
    return LEADER_PADDING + "." * dots + LEADER_PADDING


def _visible_text(node):
    """A tspan's own text plus the loose text following it, minus the newlines
    that only exist to keep the template readable."""
    return ((node.text or "") + (node.tail or "")).replace("\n", "")


def info_lines(text_element):
    """Group the info block's flat tspan children into visual lines. A line
    starts at each tspan that resets x to the left edge of the column."""
    lines = []
    for node in text_element:
        if node.get("x") == INFO_COLUMN_X:
            lines.append([])
        if lines:
            lines[-1].append(node)
    return lines


def refill_dot_leaders(root):
    """Resize every dot leader to match the text now sitting on its line.
    Run this after the live values are in place -- the dots are measured from
    the template itself, so static rows stay correct without being duplicated
    here, and the Lines of Code row's trailing "( ++, -- )" is counted too."""
    for text_element in root.findall(f"{SVG_NS}text"):
        for line in info_lines(text_element):
            dots = next(
                (n for n in line if (n.get("id") or "").endswith("_dots")), None
            )
            if dots is None:
                continue
            around = "".join(
                (dots.tail or "").replace("\n", "") if n is dots else _visible_text(n)
                for n in line
            )
            dots.text = dot_leader(around)


def post_graphql_with_retry(query, variables):
    """POSTs a GraphQL query, retrying transient 502/503/504 responses from
    GitHub's API with a short exponential backoff before giving up."""
    for attempt in range(MAX_RETRIES):
        request = requests.post(
            "https://api.github.com/graphql",
            json={"query": query, "variables": variables},
            headers=HEADERS,
        )
        if request.status_code not in RETRYABLE_STATUS_CODES:
            return request
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
    return request


def simple_request(func_name, query, variables):
    """Returns a request, or raises an Exception if the response does not succeed."""
    request = post_graphql_with_retry(query, variables)
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
    """Returns the total repository count, total star count, total fork
    count, or total disk usage (in KB) for the configured user, for the
    given ownership affiliation(s)."""
    if count_type not in ("repos", "stars", "forks", "disk_usage"):
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
                            forkCount
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
    if count_type == "forks":
        return sum(edge["node"]["forkCount"] for edge in data["edges"])
    return sum(edge["node"]["diskUsage"] or 0 for edge in data["edges"])


def starred_repos_counter(username):
    """Returns the number of repositories the given user has starred, or 0
    if the token lacks the scope to read it (a fine-grained PAT needs the
    "Starring" account permission)."""
    query = """
    query($login: String!){
        user(login: $login) {
            starredRepositories {
                totalCount
            }
        }
    }"""
    request = simple_request(
        starred_repos_counter.__name__, query, {"login": username}
    )
    starred = request.json()["data"]["user"]["starredRepositories"]
    return starred["totalCount"] if starred else 0


def contribution_streak(username):
    """Returns the current daily-contribution streak, in days, from the
    last year of GitHub contribution calendar data. If today has no
    contributions yet, the streak is counted from yesterday backward,
    since today isn't over."""
    query = """
    query($login: String!){
        user(login: $login) {
            contributionsCollection {
                contributionCalendar {
                    weeks {
                        contributionDays {
                            date
                            contributionCount
                        }
                    }
                }
            }
        }
    }"""
    request = simple_request(contribution_streak.__name__, query, {"login": username})
    calendar = request.json()["data"]["user"]["contributionsCollection"][
        "contributionCalendar"
    ]
    days = [day for week in calendar["weeks"] for day in week["contributionDays"]]
    days.sort(key=lambda day: day["date"])

    if days and days[-1]["contributionCount"] == 0:
        days = days[:-1]

    streak = 0
    for day in reversed(days):
        if day["contributionCount"] == 0:
            break
        streak += 1
    return streak


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
    request = post_graphql_with_retry(query, variables)
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
    disk_usage_data,
    fork_data,
    starred_data,
    streak_data,
    age_data,
):
    """Parses an SVG template and fills in the live stats. Every info row is
    padded to the same character count, so the values line up on the column's
    right edge without needing to be anchored there."""
    tree = etree.parse(filename)
    root = tree.getroot()

    fields = [
        ("age_data", age_data),
        ("repo_data", format_number(repo_data)),
        ("contrib_data", format_number(contrib_data)),
        ("star_data", format_number(star_data)),
        ("fork_data", format_number(fork_data)),
        ("follower_data", format_number(follower_data)),
        ("commit_data", format_number(commit_data)),
        ("streak_data", streak_data),
        ("starred_data", format_number(starred_data)),
        ("disk_data", disk_usage_data),
        ("wakatime_data", wakatime_total),
        ("wakatime_avg_data", wakatime_daily_average),
        ("wakatime_lang_data", wakatime_top_language),
        ("loc_data", format_number(loc_data[2])),
        ("loc_add", format_number(loc_data[0])),
        ("loc_del", format_number(loc_data[1])),
    ]
    for element_id, value in fields:
        find_and_replace(root, element_id, value)

    refill_dot_leaders(root)

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
    fork_data = graph_repos_stars("forks", ["OWNER"])
    repo_data = graph_repos_stars("repos", ["OWNER"])
    contrib_data = graph_repos_stars(
        "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
    )
    disk_usage_kb = graph_repos_stars("disk_usage", ["OWNER"])
    follower_data = follower_getter(USER_NAME)
    starred_data = starred_repos_counter(USER_NAME)
    streak_days = contribution_streak(USER_NAME)
    streak_data = f"{streak_days} day{format_plural(streak_days)}"
    wakatime_total, wakatime_daily_average, wakatime_top_language = wakatime_stats(
        WAKATIME_API_KEY
    )
    age_data = daily_age(BIRTHDAY)

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
            format_disk_usage(disk_usage_kb),
            fork_data,
            starred_data,
            streak_data,
            age_data,
        )


if __name__ == "__main__":
    main()
