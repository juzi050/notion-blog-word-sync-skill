from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def normalize_id(value: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", value or "").lower()


def short_id(value: str) -> str:
    normalized = normalize_id(value)
    return normalized[-12:] if len(normalized) >= 12 else normalized


def slug_for(node: dict[str, Any]) -> str:
    return f"notion-{short_id(str(node.get('id') or node.get('url') or node.get('title') or 'page'))}"


def sanitize_part(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value or "untitled")
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or "untitled"


def plain_text(markdown: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", markdown or "")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_>#~\-\[\]()`]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def yaml_scalar(value: Any) -> str:
    text = "" if value is None else str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def yaml_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "\n" + "\n".join(f"  - {yaml_scalar(item)}" for item in values)


def frontmatter(data: dict[str, Any]) -> str:
    lines: list[str] = ["---"]
    for key in [
        "title",
        "date",
        "description",
        "tags",
        "cover",
        "source",
        "notionId",
        "notionUrl",
        "notionPath",
        "archiveOnly",
    ]:
        value = data.get(key)
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list):
            lines.append(f"{key}: {yaml_list([str(v) for v in value])}")
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def node_title(node: dict[str, Any]) -> str:
    return str(node.get("title") or node.get("name") or "Untitled").strip() or "Untitled"


def node_content(node: dict[str, Any]) -> str:
    raw = node.get("content") or node.get("markdown") or node.get("body") or ""
    if isinstance(raw, list):
        return "\n".join(str(line) for line in raw).strip()
    return str(raw).strip()


def iter_roots(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("nodes"), list):
            return payload["nodes"]
        if isinstance(payload.get("tree"), list):
            return payload["tree"]
        if isinstance(payload.get("tree"), dict):
            if payload.get("includeRoot"):
                return [payload["tree"]]
            children = payload["tree"].get("children")
            return children if isinstance(children, list) else [payload["tree"]]
        return [payload]
    if isinstance(payload, list):
        return payload
    raise ValueError("Input must be a JSON object or array.")


def build_exports(
    node: dict[str, Any],
    parent_path: list[str],
    articles_dir: Path,
    tree_nodes: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
) -> int:
    title = node_title(node)
    children = [child for child in node.get("children", []) if isinstance(child, dict)]
    directory_path = [*parent_path, title]
    current_path = "/".join(directory_path)
    tree_node = {
        "id": str(node.get("id") or ""),
        "title": title,
        "url": str(node.get("url") or ""),
        "path": current_path,
        "children": [],
    }
    tree_nodes.append(tree_node)

    if children:
        count = 0
        for child in children:
            count += build_exports(child, directory_path, articles_dir, tree_node["children"], manifest)
        tree_node["count"] = count
        return count

    slug = slug_for(node)
    content = node_content(node)
    description = str(node.get("description") or plain_text(content)[:80])
    date = str(node.get("lastEditedTime") or node.get("date") or datetime.now(timezone.utc).isoformat())
    article_path = articles_dir.joinpath(*[sanitize_part(p) for p in parent_path], f"{slug}.md")
    article_path.parent.mkdir(parents=True, exist_ok=True)

    tags = ["Notion", *parent_path]
    fm = {
        "title": title,
        "date": date,
        "description": description,
        "tags": tags,
        "cover": str(node.get("cover") or ""),
        "source": "notion",
        "notionId": str(node.get("id") or ""),
        "notionUrl": str(node.get("url") or ""),
        "notionPath": "/".join(parent_path),
        "archiveOnly": True,
    }
    article_path.write_text(f"{frontmatter(fm)}\n\n{content}\n", encoding="utf-8")

    item = {
        **fm,
        "slug": slug,
        "title": title,
        "path": article_path.relative_to(articles_dir.parent).as_posix(),
    }
    manifest.append(item)
    tree_node["slug"] = slug
    tree_node["path"] = current_path
    tree_node["articlePath"] = item["path"]
    tree_node["count"] = 1
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Write Notion leaf pages to a blog-word repo.")
    parser.add_argument("--input", required=True, help="JSON page tree generated from the Codex Notion connector.")
    parser.add_argument("--repo", required=True, help="Local blog-word repository path.")
    parser.add_argument("--clean", action="store_true", help="Remove existing articles before writing.")
    args = parser.parse_args()

    source_path = Path(args.input)
    repo_path = Path(args.repo).resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    if not repo_path.exists():
        repo_path.mkdir(parents=True)

    articles_dir = repo_path / "articles"
    if args.clean and articles_dir.exists():
        if articles_dir.resolve() == repo_path.resolve() or repo_path.name == "":
            raise RuntimeError("Refusing to remove unsafe articles directory.")
        shutil.rmtree(articles_dir)
    articles_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(source_path.read_text(encoding="utf-8"))
    manifest: list[dict[str, Any]] = []
    tree: list[dict[str, Any]] = []
    for root in iter_roots(payload):
        build_exports(root, [], articles_dir, tree, manifest)

    manifest.sort(key=lambda item: (item.get("notionPath", ""), item.get("title", "")))
    (repo_path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (repo_path / "tree.json").write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"success": True, "articles": len(manifest), "repo": str(repo_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
