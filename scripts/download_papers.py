#!/usr/bin/env python3
"""Download papers referenced by this repository into per-category folders."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


USER_AGENT = "Awesome-LLM-paper-downloader/1.0"
README_PATH = "README.md"
PAPER_LIST_DIR = "paper_list"


@dataclass(frozen=True)
class Paper:
    category: str
    title: str
    url: str
    source: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download papers from README milestone entries and paper_list categories."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Path to the repository root.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory where papers will be downloaded. Defaults to <repo-root>/papers.",
    )
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Only download selected categories. Can be passed multiple times.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit total number of papers downloaded.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Network timeout in seconds.",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="Pause between downloads in seconds.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the papers that would be downloaded without downloading them.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download files even when the target file already exists.",
    )
    return parser.parse_args()


def sanitize_name(value: str) -> str:
    value = unescape(value).strip()
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .") or "untitled"


def normalize_category(name: str) -> str:
    return sanitize_name(name).replace(" ", "_")


def extract_title_from_line(line: str) -> str | None:
    bold_match = re.search(r"\*\*(.+?)\*\*", line)
    if bold_match:
        return bold_match.group(1).strip().rstrip(".")

    line = re.sub(r"^\s*[-*]\s*", "", line)
    line = re.sub(r"^\(\d{4}-\d{2}\)\s*", "", line)
    line = line.split("[paper]", 1)[0]
    line = line.split("[Paper]", 1)[0]
    line = re.sub(r"\|\s*$", "", line).strip()
    return line or None


def parse_milestone_papers(readme_path: Path) -> list[Paper]:
    papers: list[Paper] = []
    in_section = False

    for line in readme_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "## Milestone Papers":
            in_section = True
            continue
        if in_section and stripped.startswith("## ") and stripped != "## Milestone Papers":
            break
        if not in_section or not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4 or cells[0] in {"Date", ":-------:"}:
            continue
        link_match = re.search(r"\[(.+?)\]\((https?://[^)]+)\)", cells[-1])
        if not link_match:
            continue
        papers.append(
            Paper(
                category="milestone_papers",
                title=link_match.group(1).strip(),
                url=link_match.group(2).strip(),
                source=str(readme_path.relative_to(readme_path.parent.parent if readme_path.parent.name == "" else readme_path.parent)),
            )
        )

    return papers


def parse_category_papers(category_file: Path, repo_root: Path) -> list[Paper]:
    papers: list[Paper] = []
    category = normalize_category(category_file.stem)

    for line in category_file.read_text(encoding="utf-8").splitlines():
        link_match = re.search(r"\[(?:paper|Paper)\]\((https?://[^)]+)\)", line)
        if not link_match:
            continue
        title = extract_title_from_line(line)
        if not title:
            continue
        papers.append(
            Paper(
                category=category,
                title=title,
                url=link_match.group(1).strip(),
                source=str(category_file.relative_to(repo_root)),
            )
        )

    return papers


def collect_papers(repo_root: Path) -> list[Paper]:
    readme_path = repo_root / README_PATH
    paper_list_dir = repo_root / PAPER_LIST_DIR

    papers = parse_milestone_papers(readme_path)
    for category_file in sorted(paper_list_dir.glob("*.md")):
        papers.extend(parse_category_papers(category_file, repo_root))

    unique: dict[tuple[str, str, str], Paper] = {}
    for paper in papers:
        unique[(paper.category, paper.title, paper.url)] = paper
    return list(unique.values())


def build_request(url: str) -> Request:
    return Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
        },
    )


def normalize_known_pdf_url(url: str) -> str:
    parsed = urlparse(url)

    if parsed.netloc == "arxiv.org" and parsed.path.startswith("/abs/"):
        paper_id = parsed.path.removeprefix("/abs/")
        return f"https://arxiv.org/pdf/{paper_id}.pdf"
    if parsed.netloc == "openreview.net" and parsed.path == "/forum":
        query = parsed.query
        if query:
            return f"https://openreview.net/pdf?{query}"
    if parsed.netloc == "aclanthology.org" and not parsed.path.endswith(".pdf"):
        return f"{url.rstrip('/')}.pdf"

    return url


def extract_pdf_url_from_html(html: str, base_url: str) -> str | None:
    meta_patterns = [
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
    ]
    for pattern in meta_patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return urljoin(base_url, unescape(match.group(1).strip()))

    link_patterns = [
        r'<a[^>]+href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*(?:PDF|Paper)\s*</a>',
    ]
    for pattern in link_patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            candidate = urljoin(base_url, unescape(match.group(1).strip()))
            if candidate.lower().endswith(".pdf") or "pdf" in candidate.lower():
                return candidate

    return None


def sniff_extension_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return ".pdf"
    if path.endswith(".html") or path.endswith(".htm"):
        return ".html"
    return ".pdf"


def resolve_download_url(url: str, timeout: int) -> str:
    candidate = normalize_known_pdf_url(url)

    try:
        with urlopen(build_request(candidate), timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            final_url = response.geturl()
            if content_type == "application/pdf" or final_url.lower().endswith(".pdf"):
                return final_url
            if content_type == "text/html":
                html = response.read().decode("utf-8", errors="ignore")
                extracted = extract_pdf_url_from_html(html, final_url)
                if extracted:
                    return extracted
    except (HTTPError, URLError):
        if candidate != url:
            pass
        else:
            raise

    with urlopen(build_request(url), timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        final_url = response.geturl()
        if content_type == "application/pdf" or final_url.lower().endswith(".pdf"):
            return final_url
        if content_type == "text/html":
            html = response.read().decode("utf-8", errors="ignore")
            extracted = extract_pdf_url_from_html(html, final_url)
            if extracted:
                return extracted
        raise ValueError(f"Could not resolve a downloadable PDF from {url}")


def download_file(url: str, destination: Path, timeout: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".part")

    with urlopen(build_request(url), timeout=timeout) as response, temp_path.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)

    temp_path.replace(destination)


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    counter = 2
    while True:
        candidate = path.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    output_dir = (args.output_dir or (repo_root / "papers")).resolve()
    selected_categories = {normalize_category(category) for category in args.category}

    papers = collect_papers(repo_root)
    if selected_categories:
        papers = [paper for paper in papers if paper.category in selected_categories]
    if args.limit is not None:
        papers = papers[: args.limit]

    if not papers:
        print("No papers matched the current selection.", file=sys.stderr)
        return 1

    report = {
        "downloaded": [],
        "skipped": [],
        "failed": [],
    }

    for index, paper in enumerate(papers, start=1):
        category_dir = output_dir / paper.category
        safe_name = sanitize_name(paper.title)

        try:
            resolved_url = resolve_download_url(paper.url, timeout=args.timeout)
            extension = sniff_extension_from_url(resolved_url)
            destination = category_dir / f"{safe_name}{extension}"

            if destination.exists() and not args.overwrite:
                print(f"[{index}/{len(papers)}] skip {paper.category}: {paper.title}")
                report["skipped"].append(
                    {
                        "category": paper.category,
                        "title": paper.title,
                        "path": str(destination),
                        "reason": "already exists",
                    }
                )
                continue

            if destination.exists() and args.overwrite:
                destination.unlink()
            elif args.overwrite:
                destination = ensure_unique_path(destination)

            print(f"[{index}/{len(papers)}] {'plan' if args.dry_run else 'download'} {paper.category}: {paper.title}")
            if args.dry_run:
                report["downloaded"].append(
                    {
                        "category": paper.category,
                        "title": paper.title,
                        "url": resolved_url,
                        "path": str(destination),
                        "dry_run": True,
                    }
                )
                continue

            download_file(resolved_url, destination, timeout=args.timeout)
            report["downloaded"].append(
                {
                    "category": paper.category,
                    "title": paper.title,
                    "url": resolved_url,
                    "path": str(destination),
                }
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{index}/{len(papers)}] fail {paper.category}: {paper.title} ({exc})", file=sys.stderr)
            report["failed"].append(
                {
                    "category": paper.category,
                    "title": paper.title,
                    "url": paper.url,
                    "source": paper.source,
                    "error": str(exc),
                }
            )

        if args.pause > 0:
            time.sleep(args.pause)

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "download_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"Done. Downloaded: {len(report['downloaded'])}, "
        f"skipped: {len(report['skipped'])}, failed: {len(report['failed'])}."
    )
    print(f"Report written to {report_path}")

    return 0 if not report["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
