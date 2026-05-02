from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

NOTION_VERSION = "2022-06-28"


def read_user_env(name: str) -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            raw, _ = winreg.QueryValueEx(key, name)
            return str(raw or "")
    except Exception:
        return ""


def page_id_from_url(value: str) -> str:
    value = value.strip()
    dashed = re.findall(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value,
    )
    if dashed:
        return dashed[-1].replace("-", "").lower()
    compact = re.findall(r"[0-9a-fA-F]{32}", value)
    if compact:
        return compact[-1].lower()
    raise ValueError("无法从 Notion URL 中解析页面 ID")


def canonical_page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id}"


def rich_text_to_markdown(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items or []:
        text = item.get("plain_text", "")
        href = item.get("href")
        annotations = item.get("annotations") or {}
        if annotations.get("code"):
            text = f"`{text}`"
        if annotations.get("bold"):
            text = f"**{text}**"
        if annotations.get("italic"):
            text = f"*{text}*"
        if annotations.get("strikethrough"):
            text = f"~~{text}~~"
        if href:
            text = f"[{text}]({href})"
        parts.append(text)
    return "".join(parts)


def rich_text_to_plain(items: list[dict[str, Any]]) -> str:
    return "".join(item.get("plain_text", "") for item in items or "").strip()


def title_from_page(page: dict[str, Any], fallback: str = "Untitled") -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return rich_text_to_plain(prop.get("title") or []) or fallback
    return fallback


class NotionClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"https://api.notion.com/v1{path}"
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.get(url, params=params, timeout=30)
                if response.status_code >= 400:
                    raise RuntimeError(f"Notion API {response.status_code}: {response.text[:300]}")
                return response.json()
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(1.2 * (attempt + 1))
        raise RuntimeError(str(last_error))

    def page(self, page_id: str) -> dict[str, Any]:
        return self.get(f"/pages/{page_id}")

    def children(self, block_id: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.get(f"/blocks/{block_id}/children", params=params)
            results.extend(data.get("results") or [])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results


def block_text(block: dict[str, Any], key: str = "rich_text") -> str:
    block_type = block.get("type")
    return rich_text_to_markdown((block.get(block_type) or {}).get(key) or [])


def convert_block(block: dict[str, Any], client: NotionClient) -> str:
    block_type = block.get("type")
    data = block.get(block_type) or {}
    text = rich_text_to_markdown(data.get("rich_text") or [])

    if block_type == "paragraph":
        return text
    if block_type == "heading_1":
        return f"# {text}"
    if block_type == "heading_2":
        return f"## {text}"
    if block_type == "heading_3":
        return f"### {text}"
    if block_type == "bulleted_list_item":
        return f"- {text}"
    if block_type == "numbered_list_item":
        return f"1. {text}"
    if block_type == "quote":
        return f"> {text}"
    if block_type == "to_do":
        checked = "x" if data.get("checked") else " "
        return f"- [{checked}] {text}"
    if block_type == "code":
        language = data.get("language") or ""
        return f"```{language}\n{text}\n```"
    if block_type == "divider":
        return "---"
    if block_type == "image":
        image_type = data.get("type")
        image = data.get(image_type) or {}
        url = image.get("url")
        caption = rich_text_to_plain(data.get("caption") or [])
        if image_type == "file":
            return ""
        return f"![{caption}]({url})" if url else ""
    if block_type == "child_page":
        return ""

    return text


def leaf_markdown(blocks: list[dict[str, Any]], client: NotionClient) -> str:
    lines: list[str] = []
    for block in blocks:
        if block.get("type") == "child_page":
            continue
        line = convert_block(block, client)
        if line:
            lines.append(line)
    return "\n\n".join(lines).strip()


def fetch_tree(client: NotionClient, page_id: str, title_hint: str = "Untitled") -> dict[str, Any]:
    page = client.page(page_id)
    title = title_from_page(page, title_hint)
    blocks = client.children(page_id)
    child_pages = [block for block in blocks if block.get("type") == "child_page"]

    node: dict[str, Any] = {
        "id": page_id,
        "title": title,
        "url": page.get("url") or canonical_page_url(page_id),
        "lastEditedTime": page.get("last_edited_time") or "",
        "children": [],
    }
    if child_pages:
        for child in child_pages:
            child_data = child.get("child_page") or {}
            child_id = child.get("id") or ""
            node["children"].append(fetch_tree(client, child_id.replace("-", ""), child_data.get("title") or "Untitled"))
    else:
        node["content"] = leaf_markdown(blocks, client)
    return node


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a Notion page tree using NOTION_BLOG_WORD_TOKEN.")
    parser.add_argument("--root-url", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    token = read_user_env("NOTION_BLOG_WORD_TOKEN")
    if not token:
        raise RuntimeError("缺少环境变量 NOTION_BLOG_WORD_TOKEN")

    root_id = page_id_from_url(args.root_url)
    client = NotionClient(token)
    tree = fetch_tree(client, root_id)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"tree": tree}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    leaf_count = 0

    def count_leaves(node: dict[str, Any]) -> None:
        nonlocal leaf_count
        children = node.get("children") or []
        if children:
            for child in children:
                count_leaves(child)
        else:
            leaf_count += 1

    count_leaves(tree)
    print(json.dumps({"success": True, "leaves": leaf_count, "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
