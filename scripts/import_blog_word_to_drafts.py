from __future__ import annotations

import argparse
import html
import json
import re
import time
from pathlib import Path
from typing import Any

try:
    import markdown
except Exception:  # pragma: no cover
    markdown = None


META_KEYS = ["source", "notionId", "notionUrl", "notionPath", "archiveOnly"]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw = parts[1].strip()
    body = parts[2].lstrip()
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, []).append(line[4:].strip().strip('"'))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if value == "":
            data[key] = []
        elif value.lower() == "true":
            data[key] = True
        elif value.lower() == "false":
            data[key] = False
        elif value == "[]":
            data[key] = []
        else:
            data[key] = value.strip('"')
    return data, body


def markdown_to_html(body: str) -> str:
    if markdown is not None:
        return markdown.markdown(body, extensions=["fenced_code", "tables", "nl2br"])
    escaped = html.escape(body)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", escaped) if p.strip()]
    return "\n".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import blog-word Markdown files into local Juzi blog drafts.")
    parser.add_argument("--repo", required=True, help="Local blog-word repository path.")
    parser.add_argument("--manager-root", required=True, help="my-blog-manager root path.")
    parser.add_argument("--xhblogs-root", required=True, help="XHBlogs root path.")
    parser.add_argument("--include-published", action="store_true", help="Import even if a published Markdown file exists.")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    manager_root = Path(args.manager_root).resolve()
    xhblogs_root = Path(args.xhblogs_root).resolve()
    manifest_path = repo / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)

    drafts_dir = manager_root / "manager_data" / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    imported: list[str] = []
    skipped: list[str] = []
    for item in manifest:
        slug = item.get("slug")
        rel_path = item.get("path")
        if not slug or not rel_path:
            continue
        manager_post = manager_root / "posts" / f"{slug}.md"
        xhblogs_post = xhblogs_root / "posts" / f"{slug}.md"
        if not args.include_published and (manager_post.exists() or xhblogs_post.exists()):
            skipped.append(slug)
            continue

        article_path = repo / rel_path
        raw = article_path.read_text(encoding="utf-8")
        data, body = parse_frontmatter(raw)
        draft = {
            "id": slug,
            "type": "post",
            "title": data.get("title", item.get("title", "")),
            "description": data.get("description", item.get("description", "")),
            "content": markdown_to_html(body),
            "cover": data.get("cover", item.get("cover", "")),
            "tags": data.get("tags", item.get("tags", [])),
            "mood": data.get("mood", ""),
            "date": data.get("date", item.get("date", "")),
            "lastModified": int(time.time() * 1000),
        }
        for key in META_KEYS:
            if key in data:
                draft[key] = data[key]
            elif key in item:
                draft[key] = item[key]

        (drafts_dir / f"{slug}.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        imported.append(slug)

    print(json.dumps({"success": True, "imported": imported, "skipped": skipped}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
