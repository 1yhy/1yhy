#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence
from zoneinfo import ZoneInfo


BLOG_REGION_START = "<!-- BLOG-POST-LIST:START -->"
BLOG_REGION_END = "<!-- BLOG-POST-LIST:END -->"
CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
LANGUAGE_COLORS = ("#F06F4F", "#7FA7DA", "#4FC08D", "#F2C94C", "#A78BFA", "#94A3B8")


@dataclass(frozen=True)
class BlogPost:
    title: str
    url: str
    published_at: dt.datetime


@dataclass(frozen=True)
class PublicRepository:
    full_name: str
    stars: int
    fork: bool
    archived: bool
    size: int


@dataclass(frozen=True)
class ContributionDay:
    date: dt.date
    count: int


@dataclass(frozen=True)
class CodingSnapshot:
    generated_at: dt.datetime
    source_repositories: int
    stars: int
    contribution_count: int
    active_days: int
    longest_streak: int
    monthly_contributions: tuple[tuple[str, int], ...]
    weekdays: tuple[tuple[str, int], ...]
    languages: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class GeneratedProfileData:
    blog_posts_markdown: str
    stats_svg: str
    post_count: int
    contribution_count: int


class HttpClient(Protocol):
    def get_text(self, url: str) -> str: ...

    def get_json(self, url: str) -> Any: ...

    def post_json(self, url: str, payload: dict[str, Any]) -> Any: ...


class BlogPostSource(Protocol):
    def latest_posts(self, limit: int) -> list[BlogPost]: ...


class CodingActivitySource(Protocol):
    def snapshot(self, generated_at: dt.datetime) -> CodingSnapshot: ...


class UrlLibHttpClient:
    def __init__(self, token: str | None = None) -> None:
        self._token = token

    def get_text(self, url: str) -> str:
        request = self._request(url)
        return self._read(request)

    def get_json(self, url: str) -> Any:
        return json.loads(self.get_text(url))

    def post_json(self, url: str, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = self._request(url, data=body, content_type="application/json")
        return json.loads(self._read(request))

    def _request(
        self,
        url: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> urllib.request.Request:
        headers = {
            "Accept": "application/vnd.github+json, application/rss+xml, application/xml, text/xml",
            "User-Agent": "1yhy-profile-refresh",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return urllib.request.Request(url, headers=headers, data=data)

    def _read(self, request: urllib.request.Request) -> str:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")


class StackOnwardRssSource:
    def __init__(self, client: HttpClient, feed_url: str) -> None:
        self._client = client
        self._feed_url = feed_url

    def latest_posts(self, limit: int) -> list[BlogPost]:
        root = ET.fromstring(self._client.get_text(self._feed_url))
        posts: list[BlogPost] = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            url = (item.findtext("link") or "").strip()
            published_text = (item.findtext("pubDate") or "").strip()
            parsed_url = urllib.parse.urlparse(url)
            if (
                not title
                or parsed_url.netloc != "stackonward.com"
                or not parsed_url.path.startswith("/posts/")
            ):
                continue
            if not published_text:
                raise ValueError(f"RSS post has no publication date: {url}")
            published_at = email.utils.parsedate_to_datetime(published_text)
            if published_at is None or published_at.tzinfo is None:
                raise ValueError(f"RSS post has no timezone: {url}")
            posts.append(BlogPost(title=title, url=url, published_at=published_at))

        posts.sort(key=lambda post: post.published_at, reverse=True)
        if not posts:
            raise ValueError("StackOnward RSS did not contain any public posts")
        return posts[:limit]


class GitHubPublicActivitySource:
    def __init__(
        self,
        client: HttpClient,
        username: str,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self._client = client
        self._username = username
        self._api_base_url = api_base_url.rstrip("/")

    def snapshot(self, generated_at: dt.datetime) -> CodingSnapshot:
        repositories = self._repositories()
        source_repositories = [
            repository
            for repository in repositories
            if not repository.fork and not repository.archived and repository.size > 0
        ]

        language_bytes: Counter[str] = Counter()
        period_end = generated_at.astimezone(dt.timezone.utc)
        period_start = period_end - dt.timedelta(days=365)

        for repository in source_repositories:
            language_data = self._client.get_json(
                f"{self._api_base_url}/repos/{repository.full_name}/languages"
            )
            if not isinstance(language_data, dict):
                raise TypeError(f"GitHub languages response is invalid for {repository.full_name}")
            for language, byte_count in language_data.items():
                language_bytes[str(language)] += int(byte_count)

        contribution_count, contribution_days = self._contribution_calendar(period_start, period_end)
        active_days = sum(day.count > 0 for day in contribution_days)

        return CodingSnapshot(
            generated_at=generated_at,
            source_repositories=len(source_repositories),
            stars=sum(repository.stars for repository in source_repositories),
            contribution_count=contribution_count,
            active_days=active_days,
            longest_streak=longest_contribution_streak(contribution_days),
            monthly_contributions=summarize_recent_months(
                contribution_days,
                generated_at.astimezone(CHINA_TIMEZONE).date(),
            ),
            weekdays=summarize_contribution_weekdays(contribution_days),
            languages=tuple(language_bytes.most_common(6)),
        )

    def _repositories(self) -> list[PublicRepository]:
        payloads = self._paginated_json(
            f"{self._api_base_url}/users/{self._username}/repos",
            {"type": "owner", "sort": "updated"},
        )
        return [
            PublicRepository(
                full_name=str(payload["full_name"]),
                stars=int(payload["stargazers_count"]),
                fork=bool(payload["fork"]),
                archived=bool(payload["archived"]),
                size=int(payload["size"]),
            )
            for payload in payloads
        ]

    def _contribution_calendar(
        self,
        period_start: dt.datetime,
        period_end: dt.datetime,
    ) -> tuple[int, list[ContributionDay]]:
        query = """
query ProfileContributions($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            contributionCount
            date
          }
        }
      }
    }
  }
}
"""
        payload = self._client.post_json(
            f"{self._api_base_url}/graphql",
            {
                "query": query,
                "variables": {
                    "login": self._username,
                    "from": format_github_timestamp(period_start),
                    "to": format_github_timestamp(period_end),
                },
            },
        )
        if not isinstance(payload, dict):
            raise TypeError("GitHub GraphQL response is not an object")
        if payload.get("errors"):
            raise RuntimeError(f"GitHub GraphQL returned errors: {payload['errors']}")
        calendar = payload["data"]["user"]["contributionsCollection"]["contributionCalendar"]
        contribution_days = [
            ContributionDay(
                date=dt.date.fromisoformat(str(day["date"])),
                count=int(day["contributionCount"]),
            )
            for week in calendar["weeks"]
            for day in week["contributionDays"]
        ]
        return int(calendar["totalContributions"]), contribution_days

    def _paginated_json(
        self,
        url: str,
        query: dict[str, str],
        max_pages: int = 25,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            page_query = dict(query)
            page_query.update({"per_page": "100", "page": str(page)})
            page_url = f"{url}?{urllib.parse.urlencode(page_query)}"
            payload = self._client.get_json(page_url)
            if not isinstance(payload, list):
                raise TypeError(f"GitHub paginated response is not a list: {url}")
            records.extend(payload)
            if len(payload) < 100:
                return records
        raise RuntimeError(f"GitHub pagination exceeded {max_pages} pages: {url}")


def format_github_timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def longest_contribution_streak(days: Sequence[ContributionDay]) -> int:
    longest = 0
    current = 0
    previous_date: dt.date | None = None
    for day in sorted(days, key=lambda item: item.date):
        if previous_date is not None and day.date != previous_date + dt.timedelta(days=1):
            current = 0
        if day.count > 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
        previous_date = day.date
    return longest


def summarize_contribution_weekdays(days: Sequence[ContributionDay]) -> tuple[tuple[str, int], ...]:
    counts: Counter[int] = Counter()
    for day in days:
        counts[day.date.weekday()] += day.count
    return tuple((label, counts[index]) for index, label in enumerate(WEEKDAYS))


def summarize_recent_months(
    days: Sequence[ContributionDay],
    end_date: dt.date,
    month_count: int = 12,
) -> tuple[tuple[str, int], ...]:
    month_keys = tuple(
        month_key(add_months(end_date.replace(day=1), offset))
        for offset in range(1 - month_count, 1)
    )
    counts: Counter[str] = Counter()
    for day in days:
        counts[month_key(day.date)] += day.count
    return tuple((key, counts[key]) for key in month_keys)


def add_months(value: dt.date, offset: int) -> dt.date:
    month_index = value.year * 12 + value.month - 1 + offset
    year, zero_based_month = divmod(month_index, 12)
    return dt.date(year, zero_based_month + 1, 1)


def month_key(value: dt.date) -> str:
    return value.strftime("%Y-%m")


def render_blog_posts(posts: Sequence[BlogPost]) -> str:
    lines = []
    for post in posts:
        title = post.title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        published_date = post.published_at.astimezone(CHINA_TIMEZONE).date().isoformat()
        lines.append(f"- [{title}]({post.url}) · {published_date}")
    return "\n".join(lines)


def replace_generated_region(document: str, start: str, end: str, content: str) -> str:
    if document.count(start) != 1 or document.count(end) != 1:
        raise ValueError(f"Generated region markers must occur exactly once: {start} / {end}")
    pattern = re.compile(rf"{re.escape(start)}.*?{re.escape(end)}", re.DOTALL)
    return pattern.sub(f"{start}\n{content.rstrip()}\n{end}", document)


def render_programming_stats(snapshot: CodingSnapshot) -> str:
    month_chart = render_month_chart(snapshot.monthly_contributions)
    weekday_rows = render_bar_rows(snapshot.weekdays, x=520, y=250, width=380, row_height=22)
    language_bar, language_legend = render_languages(snapshot.languages)
    generated_date = snapshot.generated_at.astimezone(CHINA_TIMEZONE).date().isoformat()

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="590" viewBox="0 0 1000 590" role="img" aria-labelledby="title description">
  <title id="title">1YHY GitHub 贡献统计</title>
  <desc id="description">过去 365 天的 GitHub 贡献、活跃天数、月度分布、星期分布与公开仓库语言统计</desc>
  <style>
    text {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC", sans-serif; }}
    .surface {{ fill: #ffffff; stroke: #e2e8f0; }}
    .title {{ fill: #0a1f44; font-size: 26px; font-weight: 700; }}
    .kicker {{ fill: #f06f4f; font-size: 12px; font-weight: 700; letter-spacing: 1.6px; }}
    .section {{ fill: #0a1f44; font-size: 16px; font-weight: 700; }}
    .body {{ fill: #334155; font-size: 13px; }}
    .muted {{ fill: #64748b; font-size: 12px; }}
    .metric-value {{ fill: #0a1f44; font-size: 24px; font-weight: 700; }}
    .metric-label {{ fill: #64748b; font-size: 12px; }}
    .track {{ fill: #eef2f7; }}
    .bar {{ fill: #7fa7da; }}
    .bar-accent {{ fill: #f06f4f; }}
    .divider {{ stroke: #e2e8f0; }}
    @media (prefers-color-scheme: dark) {{
      .surface {{ fill: #0d1117; stroke: #30363d; }}
      .title, .section, .metric-value {{ fill: #f0f6fc; }}
      .body {{ fill: #c9d1d9; }}
      .muted, .metric-label {{ fill: #8b949e; }}
      .track {{ fill: #21262d; }}
      .divider {{ stroke: #30363d; }}
    }}
  </style>
  <rect class="surface" x="1" y="1" width="998" height="588" rx="18" />
  <text class="kicker" x="42" y="48">GITHUB CONTRIBUTION RHYTHM</text>
  <text class="title" x="42" y="82">过去 365 天的 GitHub 活动</text>
  <text class="muted" x="958" y="48" text-anchor="end">更新于 {generated_date}</text>

  {render_metric(42, "Contributions", snapshot.contribution_count)}
  {render_metric(282, "活跃天数", snapshot.active_days)}
  {render_metric(522, "最长连续活跃", snapshot.longest_streak)}
  {render_metric(762, "获得 Stars", snapshot.stars)}

  <line class="divider" x1="42" y1="202" x2="958" y2="202" />
  <text class="section" x="42" y="228">最近 12 个月</text>
  {month_chart}

  <text class="section" x="520" y="228">一周节奏</text>
  {weekday_rows}

  <line class="divider" x1="42" y1="432" x2="958" y2="432" />
  <text class="section" x="42" y="462">公开仓库语言</text>
  <text class="muted" x="958" y="462" text-anchor="end">按 GitHub 语言字节统计</text>
  {language_bar}
  {language_legend}
  <text class="muted" x="42" y="568">贡献数据来自 GitHub 公开贡献日历；语言来自 {snapshot.source_repositories} 个本人名下的公开源码仓库</text>
</svg>
'''


def render_metric(x: int, label: str, value: int) -> str:
    return f'''<g transform="translate({x} 112)">
    <rect class="track" width="196" height="68" rx="12" />
    <text class="metric-value" x="16" y="31">{value:,}</text>
    <text class="metric-label" x="16" y="52">{html.escape(label)}</text>
  </g>'''


def render_bar_rows(
    values: Sequence[tuple[str, int]],
    *,
    x: int,
    y: int,
    width: int,
    row_height: int,
) -> str:
    maximum = max((value for _, value in values), default=0)
    total = sum(value for _, value in values)
    label_width = 46
    value_width = 72
    bar_width = width - label_width - value_width
    rows = []
    for index, (label, value) in enumerate(values):
        row_y = y + index * row_height
        fill_width = 0 if maximum == 0 else max(3, round(bar_width * value / maximum))
        percentage = 0 if total == 0 else value / total * 100
        bar_class = "bar-accent" if value == maximum and value > 0 else "bar"
        rows.append(
            f'''<g transform="translate({x} {row_y})">
    <text class="body" x="0" y="11">{html.escape(label)}</text>
    <rect class="track" x="{label_width}" y="2" width="{bar_width}" height="10" rx="5" />
    <rect class="{bar_class}" x="{label_width}" y="2" width="{fill_width}" height="10" rx="5" />
    <text class="muted" x="{width}" y="11" text-anchor="end">{value:,} · {percentage:.0f}%</text>
  </g>'''
        )
    return "\n  ".join(rows)


def render_month_chart(months: Sequence[tuple[str, int]]) -> str:
    maximum = max((value for _, value in months), default=0)
    chart_left = 42
    chart_top = 254
    chart_height = 118
    bar_width = 23
    gap = 11
    bars = []
    for index, (month, value) in enumerate(months):
        x = chart_left + index * (bar_width + gap)
        height = 0 if maximum == 0 else max(3, round(chart_height * value / maximum))
        y = chart_top + chart_height - height
        bar_class = "bar-accent" if value == maximum and value > 0 else "bar"
        bars.append(
            f'''<g>
    <rect class="track" x="{x}" y="{chart_top}" width="{bar_width}" height="{chart_height}" rx="6" />
    <rect class="{bar_class}" x="{x}" y="{y}" width="{bar_width}" height="{height}" rx="6" />
    <text class="muted" x="{x + bar_width / 2:.1f}" y="397" text-anchor="middle">{month[-2:]}</text>
  </g>'''
        )
    return "\n  ".join(bars)


def render_languages(languages: Sequence[tuple[str, int]]) -> tuple[str, str]:
    total = sum(byte_count for _, byte_count in languages)
    if total == 0:
        return (
            '<rect class="track" x="42" y="482" width="916" height="14" rx="7" />',
            '<text class="muted" x="42" y="528">暂无公开语言数据</text>',
        )

    segments = []
    legends = []
    current_x = 42.0
    available_width = 916.0
    for index, (language, byte_count) in enumerate(languages):
        color = LANGUAGE_COLORS[index % len(LANGUAGE_COLORS)]
        segment_width = available_width * byte_count / total
        percentage = byte_count / total * 100
        segments.append(
            f'<rect x="{current_x:.1f}" y="482" width="{segment_width:.1f}" height="14" fill="{color}" />'
        )
        legend_x = 42 + index * 150
        legends.append(
            f'''<g transform="translate({legend_x} 518)">
    <circle cx="5" cy="5" r="5" fill="{color}" />
    <text class="muted" x="16" y="9">{html.escape(language)} {percentage:.0f}%</text>
  </g>'''
        )
        current_x += segment_width
    return "\n  ".join(segments), "\n  ".join(legends)


def write_text_atomically(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
    return True


def generate_profile_data(
    blog_source: BlogPostSource,
    coding_source: CodingActivitySource,
    generated_at: dt.datetime,
) -> GeneratedProfileData:
    posts = blog_source.latest_posts(limit=5)
    snapshot = coding_source.snapshot(generated_at)
    stats_svg = render_programming_stats(snapshot)
    ET.fromstring(stats_svg)
    return GeneratedProfileData(
        blog_posts_markdown=render_blog_posts(posts),
        stats_svg=stats_svg,
        post_count=len(posts),
        contribution_count=snapshot.contribution_count,
    )


def refresh(repository_root: Path, generated_at: dt.datetime) -> tuple[bool, bool, int, int]:
    token = os.environ.get("PROFILE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("PROFILE_GITHUB_TOKEN or GITHUB_TOKEN is required")
    client = UrlLibHttpClient(token=token)
    generated_data = generate_profile_data(
        StackOnwardRssSource(client, "https://stackonward.com/index.xml"),
        GitHubPublicActivitySource(client, "1yhy"),
        generated_at,
    )

    readme_path = repository_root / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    updated_readme = replace_generated_region(
        readme,
        BLOG_REGION_START,
        BLOG_REGION_END,
        generated_data.blog_posts_markdown,
    )

    readme_changed = write_text_atomically(readme_path, updated_readme)
    stats_changed = write_text_atomically(
        repository_root / "generated" / "programming-stats.svg",
        generated_data.stats_svg,
    )
    return (
        readme_changed,
        stats_changed,
        generated_data.post_count,
        generated_data.contribution_count,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the public data shown on the 1YHY profile")
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    arguments = parser.parse_args()
    generated_at = dt.datetime.now(tz=CHINA_TIMEZONE)
    readme_changed, stats_changed, post_count, contribution_count = refresh(
        arguments.repository_root.resolve(),
        generated_at,
    )
    print(
        "profile refreshed: "
        f"posts={post_count} contributions={contribution_count} "
        f"readme_changed={str(readme_changed).lower()} "
        f"stats_changed={str(stats_changed).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
