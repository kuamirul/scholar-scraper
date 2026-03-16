"""
Tests for pure functions in scraper.py.
No network calls — all tests run offline.

Run:  pytest test_scraper.py -v
"""

import pytest

from scraper import (
    extract_user_id,
    extract_org_id,
    format_summary,
    format_per_year,
    parse_batch_csv,
)


# ── extract_user_id ────────────────────────────────────────────────────────────

class TestExtractUserId:
    def test_raw_id(self):
        assert extract_user_id("YA43PbsAAAAJ") == "YA43PbsAAAAJ"

    def test_full_url(self):
        url = "https://scholar.google.com/citations?user=YA43PbsAAAAJ&hl=en"
        assert extract_user_id(url) == "YA43PbsAAAAJ"

    def test_url_with_extra_params(self):
        url = "https://scholar.google.com/citations?user=YA43PbsAAAAJ&hl=en&oi=sra"
        assert extract_user_id(url) == "YA43PbsAAAAJ"

    def test_url_user_param_first(self):
        # user= appears before other params
        url = "https://scholar.google.com/citations?hl=en&user=YA43PbsAAAAJ"
        assert extract_user_id(url) == "YA43PbsAAAAJ"

    def test_strips_whitespace(self):
        assert extract_user_id("  YA43PbsAAAAJ  ") == "YA43PbsAAAAJ"

    def test_url_strips_whitespace(self):
        url = "  https://scholar.google.com/citations?user=YA43PbsAAAAJ  "
        assert extract_user_id(url) == "YA43PbsAAAAJ"

    def test_url_missing_user_param_raises(self):
        with pytest.raises(ValueError, match="user"):
            extract_user_id("https://scholar.google.com/citations?hl=en")

    def test_url_wrong_page_raises(self):
        # org URL has no user= param
        with pytest.raises(ValueError):
            extract_user_id("https://scholar.google.com/citations?view_op=view_org&org=123")


# ── extract_org_id ─────────────────────────────────────────────────────────────

class TestExtractOrgId:
    def test_raw_id(self):
        assert extract_org_id("8426414521267289432") == "8426414521267289432"

    def test_full_url(self):
        url = "https://scholar.google.com/citations?view_op=view_org&hl=en&org=8426414521267289432"
        assert extract_org_id(url) == "8426414521267289432"

    def test_strips_whitespace(self):
        assert extract_org_id("  8426414521267289432  ") == "8426414521267289432"

    def test_url_missing_org_param_raises(self):
        with pytest.raises(ValueError, match="org"):
            extract_org_id("https://scholar.google.com/citations?view_op=view_org&hl=en")

    def test_profile_url_raises(self):
        # profile URL has no org= param
        with pytest.raises(ValueError):
            extract_org_id("https://scholar.google.com/citations?user=YA43PbsAAAAJ")


# ── format_summary ─────────────────────────────────────────────────────────────

class TestFormatSummary:
    def test_full_author(self):
        author = {
            "citedby": 136863, "citedby5y": 100069,
            "hindex": 34,      "hindex5y": 25,
            "i10index": 46,    "i10index5y": 32,
        }
        rows = format_summary(author)
        assert len(rows) == 3
        assert rows[0] == {"metric": "Citations",  "all_time": 136863, "since_5y": 100069}
        assert rows[1] == {"metric": "h-index",    "all_time": 34,     "since_5y": 25}
        assert rows[2] == {"metric": "i10-index",  "all_time": 46,     "since_5y": 32}

    def test_empty_author_defaults_to_empty_string(self):
        rows = format_summary({})
        assert rows[0]["all_time"] == ""
        assert rows[0]["since_5y"] == ""
        assert rows[1]["all_time"] == ""
        assert rows[2]["all_time"] == ""

    def test_partial_author(self):
        # Only all-time values present, no 5y
        rows = format_summary({"citedby": 500, "hindex": 10, "i10index": 15})
        assert rows[0]["all_time"] == 500
        assert rows[0]["since_5y"] == ""
        assert rows[1]["all_time"] == 10
        assert rows[1]["since_5y"] == ""

    def test_always_returns_three_rows(self):
        assert len(format_summary({})) == 3
        assert len(format_summary({"citedby": 1})) == 3

    def test_metric_labels(self):
        rows = format_summary({})
        assert [r["metric"] for r in rows] == ["Citations", "h-index", "i10-index"]


# ── format_per_year ────────────────────────────────────────────────────────────

class TestFormatPerYear:
    def test_sorted_ascending(self):
        author = {"cites_per_year": {2022: 500, 2019: 200, 2021: 400, 2020: 300}}
        rows = format_per_year(author)
        assert [r["year"] for r in rows] == [2019, 2020, 2021, 2022]
        assert [r["citations"] for r in rows] == [200, 300, 400, 500]

    def test_single_year(self):
        rows = format_per_year({"cites_per_year": {2024: 1000}})
        assert rows == [{"year": 2024, "citations": 1000}]

    def test_empty_cites_per_year(self):
        assert format_per_year({"cites_per_year": {}}) == []

    def test_missing_cites_per_year_key(self):
        assert format_per_year({}) == []

    def test_other_fields_ignored(self):
        author = {"name": "Alice", "cites_per_year": {2023: 100}}
        rows = format_per_year(author)
        assert len(rows) == 1
        assert rows[0] == {"year": 2023, "citations": 100}


# ── parse_batch_csv ────────────────────────────────────────────────────────────

BASE_URL = "https://scholar.google.com/citations?user="

class TestParseBatchCsv:
    def test_bare_urls_no_header(self):
        text = f"{BASE_URL}AAA\n{BASE_URL}BBB\n{BASE_URL}CCC"
        assert parse_batch_csv(text) == ["AAA", "BBB", "CCC"]

    def test_raw_ids_no_header(self):
        text = "YA43PbsAAAAJ\nXXXXXXXXXXXX"
        assert parse_batch_csv(text) == ["YA43PbsAAAAJ", "XXXXXXXXXXXX"]

    def test_extra_columns_uses_first_only(self):
        text = f"{BASE_URL}AAA,Alice Smith\n{BASE_URL}BBB,Bob Jones"
        assert parse_batch_csv(text) == ["AAA", "BBB"]

    def test_quoted_first_column(self):
        text = f'"{BASE_URL}AAA","Alice"\n"{BASE_URL}BBB","Bob"'
        assert parse_batch_csv(text) == ["AAA", "BBB"]

    def test_empty_lines_skipped(self):
        text = f"{BASE_URL}AAA\n\n{BASE_URL}BBB\n\n"
        assert parse_batch_csv(text) == ["AAA", "BBB"]

    def test_crlf_line_endings(self):
        text = f"{BASE_URL}AAA\r\n{BASE_URL}BBB\r\n"
        assert parse_batch_csv(text) == ["AAA", "BBB"]

    def test_bom_prefix_stripped(self):
        text = f"\ufeff{BASE_URL}AAA\n{BASE_URL}BBB"
        assert parse_batch_csv(text) == ["AAA", "BBB"]

    def test_invalid_url_missing_user_param_skipped(self):
        # URL that starts with http but has no user= → ValueError → skipped
        text = f"https://scholar.google.com/citations?hl=en\n{BASE_URL}AAA"
        assert parse_batch_csv(text) == ["AAA"]

    def test_completely_empty_file(self):
        assert parse_batch_csv("") == []

    def test_only_empty_lines(self):
        assert parse_batch_csv("\n\n\n") == []

    def test_text_header_included_as_raw_id(self):
        # Plain-text headers (e.g. "profile_url") are NOT automatically skipped —
        # they are passed through as raw IDs. They will fail at the scholarly
        # fetch stage and appear as error rows in the output.
        text = f"profile_url\n{BASE_URL}AAA"
        result = parse_batch_csv(text)
        assert result == ["profile_url", "AAA"]

    def test_mixed_urls_and_raw_ids(self):
        text = f"{BASE_URL}AAA\nYA43PbsAAAAJ"
        assert parse_batch_csv(text) == ["AAA", "YA43PbsAAAAJ"]
