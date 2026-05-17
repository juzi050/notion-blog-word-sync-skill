from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse
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


def compact_page_id(value: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", value or "").lower()


def dashed_page_id(value: str) -> str:
    normalized = compact_page_id(value)
    if len(normalized) != 32:
        return value
    return f"{normalized[:8]}-{normalized[8:12]}-{normalized[12:16]}-{normalized[16:20]}-{normalized[20:]}"


def canonical_page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id}"


def is_public_notion_site(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.endswith(".notion.site")


def public_site_domain(url: str) -> str:
    host = urlparse(url).hostname or ""
    if not host.endswith(".notion.site"):
        raise ValueError("不是公开 Notion 站点 URL")
    return host.rsplit(".notion.site", 1)[0]


def canonical_public_url(domain: str, page_id: str) -> str:
    return f"https://{domain}.notion.site/{compact_page_id(page_id)}"


def _format_annotations(text: str, annotations: list[list[Any]] | None) -> str:
    href: str | None = None
    code = False
    bold = False
    italic = False
    strike = False
    for item in annotations or []:
        if not item:
            continue
        kind = str(item[0])
        if kind == "a" and len(item) > 1:
            href = str(item[1])
        elif kind == "c":
            code = True
        elif kind == "b":
            bold = True
        elif kind == "i":
            italic = True
        elif kind == "s":
            strike = True
    if code:
        text = f"`{text}`"
    if bold:
        text = f"**{text}**"
    if italic:
        text = f"*{text}*"
    if strike:
        text = f"~~{text}~~"
    if href:
        text = f"[{text}]({href})"
    return text


def _segment_text(item: Any) -> str:
    if isinstance(item, dict):
        text = item.get("plain_text", "")
        annotations = item.get("annotations") or {}
        href = item.get("href")
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
        return text
    if isinstance(item, (list, tuple)):
        text = str(item[0]) if item else ""
        annotations = item[1] if len(item) > 1 and isinstance(item[1], list) else []
        return _format_annotations(text, annotations)
    return str(item)


def rich_text_to_markdown(items: list[dict[str, Any]]) -> str:
    return "".join(_segment_text(item) for item in items or [])


def rich_text_to_plain(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            parts.append(item.get("plain_text", ""))
        elif isinstance(item, (list, tuple)):
            parts.append(str(item[0]) if item else "")
        else:
            parts.append(str(item))
    return "".join(parts).strip()


def escape_plain_line_starts(text: str) -> str:
    return re.sub(r"(?m)^([ \t]*)(#{1,6})(?=.)", r"\1\\\2", text)


def title_from_page(page: dict[str, Any], fallback: str = "Untitled") -> str:
    props = page.get("properties") or {}
    direct_title = props.get("title")
    if isinstance(direct_title, list):
        return rich_text_to_plain(direct_title) or fallback
    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return rich_text_to_plain(prop.get("title") or []) or fallback
    return fallback


def public_title_from_page(page: dict[str, Any], fallback: str = "Untitled") -> str:
    props = page.get("properties") or {}
    title = props.get("title")
    if isinstance(title, list):
        return rich_text_to_plain(title) or fallback
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


def public_image_url(domain: str, raw_block: dict[str, Any]) -> str | None:
    block_id = dashed_page_id(compact_page_id(str(raw_block.get("id") or "")))
    space_id = dashed_page_id(str(raw_block.get("spaceId") or raw_block.get("space_id") or ""))
    properties = raw_block.get("properties") or {}
    attachment = rich_text_to_plain(properties.get("source") or [])
    if not attachment:
        attachment = str((raw_block.get("format") or {}).get("display_source") or "").strip()
    if not attachment:
        return None
    block_width = (raw_block.get("format") or {}).get("block_width")
    width = 860
    if isinstance(block_width, (int, float)) and block_width > 0:
        width = max(1, int(round(float(block_width) * 2 / 10) * 10))
    encoded_attachment = quote(attachment, safe="")
    return (
        f"https://{domain}.notion.site/image/{encoded_attachment}"
        f"?table=block&id={block_id}&spaceId={space_id}&width={width}&userId=&cache=v2&imgBuildSrc=requestProxiedImageUrl"
    )


def normalize_public_block(raw_block: dict[str, Any], domain: str) -> dict[str, Any] | None:
    block_type = str(raw_block.get("type") or "")
    block_id = compact_page_id(str(raw_block.get("id") or ""))
    properties = raw_block.get("properties") or {}
    title = properties.get("title") or []
    has_children = bool(raw_block.get("content"))

    if block_type == "page":
        public_id = block_id
        canonical_id = compact_page_id(str(raw_block.get("copied_from") or public_id))
        return {
            "type": "child_page",
            "id": canonical_id or public_id,
            "publicId": public_id,
            "child_page": {"title": rich_text_to_plain(title) or "Untitled"},
            "has_children": has_children,
        }
    if block_type in {"text", "paragraph"}:
        return {
            "type": "paragraph",
            "id": block_id,
            "paragraph": {"rich_text": title},
            "has_children": has_children,
        }
    if block_type in {"bulleted_list", "bulleted_list_item"}:
        return {
            "type": "bulleted_list_item",
            "id": block_id,
            "bulleted_list_item": {"rich_text": title},
            "has_children": has_children,
        }
    if block_type in {"numbered_list", "numbered_list_item"}:
        return {
            "type": "numbered_list_item",
            "id": block_id,
            "numbered_list_item": {"rich_text": title},
            "has_children": has_children,
        }
    if block_type in {"header", "heading_1"}:
        return {
            "type": "heading_1",
            "id": block_id,
            "heading_1": {"rich_text": title},
            "has_children": has_children,
        }
    if block_type in {"sub_header", "heading_2"}:
        return {
            "type": "heading_2",
            "id": block_id,
            "heading_2": {"rich_text": title},
            "has_children": has_children,
        }
    if block_type in {"sub_sub_header", "heading_3"}:
        return {
            "type": "heading_3",
            "id": block_id,
            "heading_3": {"rich_text": title},
            "has_children": has_children,
        }
    if block_type == "quote":
        return {
            "type": "quote",
            "id": block_id,
            "quote": {"rich_text": title},
            "has_children": has_children,
        }
    if block_type == "to_do":
        return {
            "type": "to_do",
            "id": block_id,
            "to_do": {"rich_text": title, "checked": bool(properties.get("checked"))},
            "has_children": has_children,
        }
    if block_type == "code":
        return {
            "type": "code",
            "id": block_id,
            "code": {
                "rich_text": title,
                "language": rich_text_to_plain(properties.get("language") or []),
            },
            "has_children": has_children,
        }
    if block_type == "image":
        url = public_image_url(domain, raw_block)
        if not url:
            return None
        return {
            "type": "image",
            "id": block_id,
            "image": {"type": "external", "external": {"url": url}, "caption": title},
            "has_children": has_children,
        }
    if block_type == "divider":
        return {"type": "divider", "id": block_id, "divider": {}, "has_children": has_children}

    return {
        "type": "paragraph",
        "id": block_id,
        "paragraph": {"rich_text": title},
        "has_children": has_children,
    }


class PublicNotionClient:
    def __init__(self, root_url: str):
        self.root_url = root_url.rstrip("/")
        self.domain = public_site_domain(root_url)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/json",
                "Notion-Client-Version": "23.13.20260517.0502",
                "Referer": self.root_url,
            }
        )
        self._cache: dict[str, dict[str, Any]] = {}
        self._block_maps: dict[str, dict[str, Any]] = {}

    def get(self, page_id: str) -> dict[str, Any]:
        normalized = compact_page_id(page_id)
        if normalized in self._cache:
            return self._cache[normalized]
        url = f"https://{self.domain}.notion.site/api/v3/loadCachedPageChunkV2"
        payload = {"page": {"id": dashed_page_id(normalized)}, "cursor": {"stack": []}, "verticalColumns": False}
        last_error: Exception | None = None
        data: dict[str, Any] | None = None
        for attempt in range(4):
            try:
                response = self.session.post(url, json=payload, timeout=30)
                if response.status_code >= 400:
                    raise RuntimeError(f"公开 Notion 接口 {response.status_code}: {response.text[:300]}")
                data = response.json()
                break
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(1.2 * (attempt + 1))
        if data is None:
            raise RuntimeError(str(last_error))
        self._cache[normalized] = data
        record_map = data.get("recordMap") or {}
        blocks = record_map.get("block") or {}
        if isinstance(blocks, dict):
            self._block_maps[normalized] = blocks
        return data

    def _block_map(self, page_id: str) -> dict[str, Any]:
        normalized = compact_page_id(page_id)
        if normalized in self._block_maps:
            return self._block_maps[normalized]
        self.get(page_id)
        return self._block_maps.get(normalized, {})

    def page(self, page_id: str) -> dict[str, Any]:
        blocks = self._block_map(page_id)
        current_id = compact_page_id(page_id)
        entry = blocks.get(dashed_page_id(current_id)) or blocks.get(current_id)
        if not entry:
            raise RuntimeError(f"无法读取公开 Notion 页面: {page_id}")
        value = (entry.get("value") or {}).get("value") or {}
        public_id = compact_page_id(str(value.get("id") or current_id))
        canonical_id = compact_page_id(str(value.get("copied_from") or public_id))
        return {
            "id": canonical_id or public_id,
            "publicId": public_id,
            "title": public_title_from_page(value),
            "url": canonical_public_url(self.domain, public_id),
            "last_edited_time": value.get("last_edited_time") or "",
            "children": [],
        }

    def children(self, page_or_id: dict[str, Any] | str) -> list[dict[str, Any]]:
        page_id = page_or_id.get("publicId") if isinstance(page_or_id, dict) else page_or_id
        if not page_id:
            return []
        blocks = self._block_map(str(page_id))
        current_id = compact_page_id(str(page_id))
        entry = blocks.get(dashed_page_id(current_id)) or blocks.get(current_id)
        if not entry:
            return []
        value = (entry.get("value") or {}).get("value") or {}
        content_ids = value.get("content") or []
        children: list[dict[str, Any]] = []
        for child_id in content_ids:
            child_key = dashed_page_id(str(child_id))
            child_entry = blocks.get(child_key) or blocks.get(compact_page_id(str(child_id)))
            if not child_entry:
                continue
            raw_child = (child_entry.get("value") or {}).get("value") or {}
            normalized = normalize_public_block(raw_child, self.domain)
            if normalized:
                children.append(normalized)
        return children


def block_text(block: dict[str, Any], key: str = "rich_text") -> str:
    block_type = block.get("type")
    return rich_text_to_markdown((block.get(block_type) or {}).get(key) or [])


def convert_block(block: dict[str, Any], client: NotionClient) -> str:
    block_type = block.get("type")
    data = block.get(block_type) or {}
    text = rich_text_to_markdown(data.get("rich_text") or [])

    if block_type == "paragraph":
        return escape_plain_line_starts(text)
    if block_type == "heading_1":
        return f"# {text}"
    if block_type == "heading_2":
        return f"## {text}"
    if block_type == "heading_3":
        return f"### {text}"
    if block_type == "bulleted_list_item":
        return f"- {escape_plain_line_starts(text)}"
    if block_type == "bulleted_list":
        return f"- {escape_plain_line_starts(text)}"
    if block_type == "numbered_list_item":
        return f"1. {escape_plain_line_starts(text)}"
    if block_type == "numbered_list":
        return f"1. {escape_plain_line_starts(text)}"
    if block_type == "quote":
        return f"> {escape_plain_line_starts(text)}"
    if block_type == "to_do":
        checked = "x" if data.get("checked") else " "
        return f"- [{checked}] {escape_plain_line_starts(text)}"
    if block_type == "code":
        language = data.get("language") or ""
        code_text = rich_text_to_plain(data.get("rich_text") or [])
        return f"```{language}\n{code_text}\n```"
    if block_type == "divider":
        return "---"
    if block_type == "image":
        image_type = data.get("type")
        image = data.get(image_type) or {}
        url = image.get("url")
        caption = rich_text_to_plain(data.get("caption") or [])
        return f"![{caption}]({url})" if url else ""
    if block_type == "child_page":
        return ""

    return text


def leaf_markdown(blocks: list[dict[str, Any]], client: NotionClient) -> str:
    lines: list[str] = []

    def append_block(block: dict[str, Any]) -> None:
        if block.get("type") == "child_page":
            return
        line = convert_block(block, client)
        if line:
            lines.append(line)
        if block.get("has_children") and block.get("id"):
            for child in client.children(block.get("publicId") or block["id"]):
                append_block(child)

    for block in blocks:
        append_block(block)
    return "\n\n".join(lines).strip()


def fetch_tree(client: Any, page_id: str, title_hint: str = "Untitled") -> dict[str, Any]:
    page = client.page(page_id)
    title = title_from_page(page, title_hint)
    blocks = client.children(page.get("publicId") or page_id)
    child_pages = [block for block in blocks if block.get("type") == "child_page"]

    node: dict[str, Any] = {
        "id": page.get("id") or page_id,
        "title": title,
        "url": page.get("url") or canonical_page_url(page_id),
        "lastEditedTime": page.get("last_edited_time") or "",
        "children": [],
    }
    if child_pages:
        for child in child_pages:
            child_data = child.get("child_page") or {}
            child_id = child.get("publicId") or child.get("id") or ""
            node["children"].append(fetch_tree(client, child_id.replace("-", ""), child_data.get("title") or "Untitled"))
    else:
        node["content"] = leaf_markdown(blocks, client)
    return node


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a Notion page tree using NOTION_BLOG_WORD_TOKEN.")
    parser.add_argument("--root-url", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    root_id = page_id_from_url(args.root_url)
    if is_public_notion_site(args.root_url):
        client: Any = PublicNotionClient(args.root_url)
    else:
        token = read_user_env("NOTION_BLOG_WORD_TOKEN")
        if not token:
            raise RuntimeError("缺少环境变量 NOTION_BLOG_WORD_TOKEN")
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
