from __future__ import annotations

import datetime as dt
import unittest
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from scripts.refresh_profile import (
    BlogPost,
    CodingSnapshot,
    ContributionDay,
    GitHubPublicActivitySource,
    StackOnwardRssSource,
    longest_contribution_streak,
    render_blog_posts,
    render_programming_stats,
    replace_generated_region,
    summarize_contribution_weekdays,
    summarize_recent_months,
)


class StaticHttpClient:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text(self, url: str) -> str:
        return self._text

    def get_json(self, url: str) -> object:
        raise AssertionError("JSON should not be requested in RSS tests")

    def post_json(self, url: str, payload: dict[str, object]) -> object:
        raise AssertionError("JSON should not be requested in RSS tests")


class StaticGitHubClient:
    def get_text(self, url: str) -> str:
        raise AssertionError("Text should not be requested in GitHub tests")

    def get_json(self, url: str) -> object:
        if "/users/1yhy/repos" in url:
            return [
                {
                    "full_name": "1yhy/example",
                    "stargazers_count": 7,
                    "fork": False,
                    "archived": False,
                    "size": 10,
                }
            ]
        if url.endswith("/repos/1yhy/example/languages"):
            return {"Go": 90, "TypeScript": 10}
        raise AssertionError(f"Unexpected GitHub URL: {url}")

    def post_json(self, url: str, payload: dict[str, object]) -> object:
        return {
            "data": {
                "user": {
                    "contributionsCollection": {
                        "contributionCalendar": {
                            "totalContributions": 6,
                            "weeks": [
                                {
                                    "contributionDays": [
                                        {"date": "2026-08-10", "contributionCount": 2},
                                        {"date": "2026-08-11", "contributionCount": 4},
                                    ]
                                }
                            ],
                        }
                    }
                }
            }
        }


class ProfileRefreshTest(unittest.TestCase):
    def test_rss_filters_non_post_pages_and_sorts_newest_first(self) -> None:
        rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Older</title><link>https://stackonward.com/posts/older/</link><pubDate>Mon, 01 Jan 2024 08:00:00 +0800</pubDate></item>
  <item><title>About</title><link>https://stackonward.com/about/</link><pubDate>Tue, 02 Jan 2024 08:00:00 +0800</pubDate></item>
  <item><title>Newer</title><link>https://stackonward.com/posts/newer/</link><pubDate>Wed, 03 Jan 2024 08:00:00 +0800</pubDate></item>
</channel></rss>"""
        posts = StackOnwardRssSource(StaticHttpClient(rss), "https://example.test/feed").latest_posts(5)

        self.assertEqual([post.title for post in posts], ["Newer", "Older"])

    def test_generated_region_requires_exact_markers(self) -> None:
        document = "before\n<!-- START -->\nold\n<!-- END -->\nafter\n"

        updated = replace_generated_region(document, "<!-- START -->", "<!-- END -->", "new")

        self.assertEqual(updated, "before\n<!-- START -->\nnew\n<!-- END -->\nafter\n")
        with self.assertRaises(ValueError):
            replace_generated_region("missing", "<!-- START -->", "<!-- END -->", "new")

    def test_contribution_rhythm_uses_calendar_counts(self) -> None:
        contribution_days = [
            ContributionDay(dt.date(2026, 8, 9), 2),
            ContributionDay(dt.date(2026, 8, 10), 1),
            ContributionDay(dt.date(2026, 8, 11), 3),
            ContributionDay(dt.date(2026, 8, 12), 0),
            ContributionDay(dt.date(2026, 8, 13), 4),
        ]

        self.assertEqual(longest_contribution_streak(contribution_days), 3)
        self.assertEqual(
            summarize_contribution_weekdays(contribution_days),
            (
                ("周一", 1),
                ("周二", 3),
                ("周三", 0),
                ("周四", 4),
                ("周五", 0),
                ("周六", 0),
                ("周日", 2),
            ),
        )
        self.assertEqual(
            summarize_recent_months(contribution_days, dt.date(2026, 8, 13))[-1],
            ("2026-08", 10),
        )

    def test_github_source_aggregates_public_data_without_repository_details(self) -> None:
        generated_at = dt.datetime(2026, 8, 11, 18, 0, tzinfo=dt.timezone.utc)

        snapshot = GitHubPublicActivitySource(StaticGitHubClient(), "1yhy").snapshot(
            generated_at
        )

        self.assertEqual(snapshot.contribution_count, 6)
        self.assertEqual(snapshot.active_days, 2)
        self.assertEqual(snapshot.longest_streak, 2)
        self.assertEqual(snapshot.stars, 7)
        self.assertEqual(snapshot.languages, (("Go", 90), ("TypeScript", 10)))

    def test_rendered_content_is_valid_and_escaped(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")
        posts = [
            BlogPost(
                title="A [safe] title",
                url="https://stackonward.com/posts/safe/",
                published_at=dt.datetime(2026, 8, 11, tzinfo=timezone),
            )
        ]
        snapshot = CodingSnapshot(
            generated_at=dt.datetime(2026, 8, 11, tzinfo=timezone),
            source_repositories=8,
            stars=55,
            contribution_count=7015,
            active_days=263,
            longest_streak=21,
            monthly_contributions=(("2026-07", 500), ("2026-08", 600)),
            weekdays=(
                ("周一", 1000),
                ("周二", 900),
                ("周三", 800),
                ("周四", 700),
                ("周五", 600),
                ("周六", 500),
                ("周日", 400),
            ),
            languages=(("TypeScript", 80), ("Vue", 20)),
        )

        self.assertIn("A \\[safe\\] title", render_blog_posts(posts))
        svg = render_programming_stats(snapshot)
        ET.fromstring(svg)
        self.assertIn("7,015", svg)
        self.assertIn("TypeScript 80%", svg)


if __name__ == "__main__":
    unittest.main()
