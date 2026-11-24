"""Builds assets/streak.svg and assets/langs.svg from the GitHub API.

Runs in GitHub Actions (see .github/workflows/stats.yml). With a personal
token in the GH_STATS_TOKEN secret it also counts PRIVATE repos and private
contributions. No extra packages needed (standard library only).
"""
import datetime as dt
import json
import os
import urllib.request
from html import escape

TOKEN = os.environ["GH_TOKEN"]
TZ_OFFSET = dt.timedelta(hours=float(os.environ.get("TZ_OFFSET_HOURS", "5.5")))  # IST
EXCLUDE_LANGS = {"HTML", "CSS", "Jupyter Notebook", "Makefile", "Batchfile", "PowerShell", "Shell"}
TOP_N = 6

# theme (same colors as the README)
BG, PINK, CYAN, PURPLE, TEXT = "#1A1B2E", "#FF5FA2", "#4FD1E8", "#C77DFF", "#F8E8F0"
LANG_COLORS = [PINK, CYAN, PURPLE, "#7CFFB2", "#FFD166", "#FF9F6B"]
FONT = "'Segoe UI', Ubuntu, 'Helvetica Neue', sans-serif"


def gql(query, **variables):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    if out.get("errors"):
        raise SystemExit(f"GraphQL error: {out['errors']}")
    return out["data"]


# ---------------------------------------------------------------- data
def contribution_days(created_at, user_id):
    days, seen_private = {}, {}  # seen_private: private commits the calendar already shows
    start = created_at
    now = dt.datetime.now(dt.timezone.utc)
    while start < now:
        end = min(start + dt.timedelta(days=365), now)
        data = gql(
            """query($from: DateTime!, $to: DateTime!) { viewer { contributionsCollection(from: $from, to: $to) {
                 contributionCalendar { weeks { contributionDays { date contributionCount } } }
                 commitContributionsByRepository(maxRepositories: 100) { repository { isPrivate }
                   contributions(first: 100) { nodes { occurredAt commitCount } } } } } }""",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        cc = data["viewer"]["contributionsCollection"]
        for w in cc["contributionCalendar"]["weeks"]:
            for d in w["contributionDays"]:
                days[dt.date.fromisoformat(d["date"])] = d["contributionCount"]
        for repo in cc["commitContributionsByRepository"]:
            if repo["repository"]["isPrivate"]:
                for n in repo["contributions"]["nodes"]:
                    day = dt.date.fromisoformat(n["occurredAt"][:10])
                    seen_private[day] = seen_private.get(day, 0) + n["commitCount"]
        start = end

    # A fine-grained token can't see private contributions in the calendar,
    # but it can read private repos' commit history. Add whatever is missing.
    for day, n in private_commits(created_at, user_id).items():
        missing = n - seen_private.get(day, 0)
        if missing > 0:
            days[day] = days.get(day, 0) + missing
    return days


def private_commits(since, user_id):
    per_day, repo_cursor = {}, None
    while True:
        data = gql(
            """query($after: String) { viewer { repositories(first: 50, after: $after, ownerAffiliations: OWNER, privacy: PRIVATE) {
                 pageInfo { hasNextPage endCursor } nodes { nameWithOwner } } } }""",
            after=repo_cursor,
        )
        repos = data["viewer"]["repositories"]
        for r in repos["nodes"]:
            owner, name = r["nameWithOwner"].split("/")
            cursor = None
            while True:
                h = gql(
                    """query($o: String!, $n: String!, $id: ID!, $since: GitTimestamp!, $after: String) {
                         repository(owner: $o, name: $n) { defaultBranchRef { target { ... on Commit {
                           history(first: 100, after: $after, since: $since, author: {id: $id}) {
                             pageInfo { hasNextPage endCursor } nodes { authoredDate } } } } } } }""",
                    o=owner, n=name, id=user_id, since=since.isoformat(), after=cursor,
                )
                ref = h["repository"]["defaultBranchRef"]
                if not ref:
                    break  # empty repo
                hist = ref["target"]["history"]
                for c in hist["nodes"]:
                    t = dt.datetime.fromisoformat(c["authoredDate"].replace("Z", "+00:00")) + TZ_OFFSET
                    per_day[t.date()] = per_day.get(t.date(), 0) + 1
                if not hist["pageInfo"]["hasNextPage"]:
                    break
                cursor = hist["pageInfo"]["endCursor"]
        if not repos["pageInfo"]["hasNextPage"]:
            return per_day
        repo_cursor = repos["pageInfo"]["endCursor"]


def streaks(days):
    today = (dt.datetime.now(dt.timezone.utc) + TZ_OFFSET).date()
    total = sum(days.values())
    first = min(days) if days else today

    # longest
    longest, l_range, run, run_start = 0, None, 0, None
    d = first
    while d <= today:
        if days.get(d, 0) > 0:
            run_start = d if run == 0 else run_start
            run += 1
            if run > longest:
                longest, l_range = run, (run_start, d)
        else:
            run = 0
        d += dt.timedelta(days=1)

    # current (today with 0 doesn't break the streak yet)
    d = today if days.get(today, 0) > 0 else today - dt.timedelta(days=1)
    cur, cur_end = 0, d
    while days.get(d, 0) > 0:
        cur += 1
        d -= dt.timedelta(days=1)
    c_range = (d + dt.timedelta(days=1), cur_end) if cur else None
    return total, first, today, cur, c_range, longest, l_range


def languages():
    totals, cursor = {}, None
    while True:
        data = gql(
            """query($after: String) { viewer { repositories(first: 100, after: $after, ownerAffiliations: OWNER, isFork: false) {
                 pageInfo { hasNextPage endCursor }
                 nodes { languages(first: 20, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } } } } } }""",
            after=cursor,
        )
        repos = data["viewer"]["repositories"]
        for r in repos["nodes"]:
            for e in r["languages"]["edges"]:
                name = e["node"]["name"]
                if name not in EXCLUDE_LANGS:
                    totals[name] = totals.get(name, 0) + e["size"]
        if not repos["pageInfo"]["hasNextPage"]:
            return totals
        cursor = repos["pageInfo"]["endCursor"]


# ---------------------------------------------------------------- svg
def fmt(d):
    return f"{d:%b} {d.day}" if d else ""


def rng(r, today):
    if not r:
        return "no streak yet"
    a, b = r
    end = "Present" if b >= today - dt.timedelta(days=1) else fmt(b)
    return fmt(a) if a == b else f"{fmt(a)} - {end}"


def streak_svg(total, first, today, cur, c_range, longest, l_range):
    def col(x, num, label, sub, color):
        return f"""
  <text x="{x}" y="88" text-anchor="middle" font-size="30" font-weight="700" fill="{TEXT}">{num}</text>
  <text x="{x}" y="122" text-anchor="middle" font-size="14" fill="{color}">{label}</text>
  <text x="{x}" y="145" text-anchor="middle" font-size="11" fill="{PURPLE}">{escape(sub)}</text>"""

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="495" height="195" viewBox="0 0 495 195" font-family="{FONT}">
  <rect width="495" height="195" rx="12" fill="{BG}"/>
  <line x1="165" y1="40" x2="165" y2="155" stroke="{PURPLE}" stroke-opacity=".5"/>
  <line x1="330" y1="40" x2="330" y2="155" stroke="{PURPLE}" stroke-opacity=".5"/>
  {col(82.5, total, "Total Contributions", f"{fmt(first)} - Present", CYAN)}
  <circle cx="247.5" cy="78" r="38" fill="none" stroke="{PINK}" stroke-width="5"/>
  <text x="247.5" y="36" text-anchor="middle" font-size="18">🔥</text>
  <text x="247.5" y="88" text-anchor="middle" font-size="30" font-weight="700" fill="{TEXT}">{cur}</text>
  <text x="247.5" y="145" text-anchor="middle" font-size="14" font-weight="700" fill="{PINK}">Current Streak</text>
  <text x="247.5" y="165" text-anchor="middle" font-size="11" fill="{PURPLE}">{escape(rng(c_range, today))}</text>
  {col(412.5, longest, "Longest Streak", rng(l_range, today), CYAN)}
  <text x="485" y="187" text-anchor="end" font-size="9" fill="{PURPLE}" fill-opacity=".6">updated {today:%b %d}</text>
</svg>
"""


def langs_svg(totals):
    top = sorted(totals.items(), key=lambda kv: -kv[1])[:TOP_N]
    s = sum(v for _, v in top) or 1
    rows = (len(top) + 1) // 2
    h = 95 + rows * 26
    bar, x = [], 25.0
    for i, (_, v) in enumerate(top):
        w = 445 * v / s
        bar.append(f'<rect x="{x:.2f}" y="55" width="{w:.2f}" height="10" fill="{LANG_COLORS[i % 6]}"/>')
        x += w
    items = []
    for i, (name, v) in enumerate(top):
        cx, cy = 30 + (i % 2) * 225, 92 + (i // 2) * 26
        items.append(
            f'<circle cx="{cx}" cy="{cy - 4}" r="5" fill="{LANG_COLORS[i % 6]}"/>'
            f'<text x="{cx + 12}" y="{cy}" font-size="13" fill="{TEXT}">{escape(name)} '
            f'<tspan fill="{PURPLE}">{100 * v / s:.1f}%</tspan></text>'
        )
    body = "\n  ".join(items) if top else f'<text x="25" y="90" font-size="13" fill="{TEXT}">no code yet ✿</text>'
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="495" height="{h}" viewBox="0 0 495 {h}" font-family="{FONT}">
  <rect width="495" height="{h}" rx="12" fill="{BG}"/>
  <text x="25" y="35" font-size="18" font-weight="600" fill="{PINK}">⚡ most used</text>
  <clipPath id="c"><rect x="25" y="55" width="445" height="10" rx="5"/></clipPath>
  <g clip-path="url(#c)">{"".join(bar)}</g>
  {body}
</svg>
"""


def main():
    me = gql("{ viewer { id createdAt } }")["viewer"]
    created = dt.datetime.fromisoformat(me["createdAt"].replace("Z", "+00:00"))
    os.makedirs("assets", exist_ok=True)
    s = streaks(contribution_days(created, me["id"]))
    with open("assets/streak.svg", "w", encoding="utf-8") as f:
        f.write(streak_svg(*s))
    t = languages()
    with open("assets/langs.svg", "w", encoding="utf-8") as f:
        f.write(langs_svg(t))
    print("streak:", s[0], "total,", s[3], "current,", s[5], "longest | langs:", t)


if __name__ == "__main__":
    main()
