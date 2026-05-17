from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

try:
    import oss2
except Exception:  # pragma: no cover
    oss2 = None


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
    hash_placeholder = "\u0000HASH\u0000"
    text = text.replace(r"\#", hash_placeholder)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", " ", text)
    text = re.sub(r"[*_>~\-\[\]()`\\]", " ", text)
    text = text.replace(hash_placeholder, "#")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_public_base_url(value: str | None) -> str:
    base_url = (value or "").strip().rstrip("/")
    if not base_url:
        return ""
    base_url = re.sub(r"^(https?):/([^/])", r"\1://\2", base_url)
    if not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    return base_url.rstrip("/")


def normalize_endpoint(value: str | None) -> str:
    endpoint = (value or "").strip().rstrip("/")
    if not endpoint:
        return ""
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    if endpoint.startswith("oss-"):
        return f"https://{endpoint}.aliyuncs.com"
    return f"https://oss-{endpoint}.aliyuncs.com"


def public_base_url(config: dict[str, Any]) -> str:
    explicit = normalize_public_base_url(config.get("publicBaseUrl"))
    if explicit:
        return explicit
    endpoint = normalize_endpoint(config.get("endpoint"))
    host = re.sub(r"^https?://", "", endpoint).strip("/")
    return f"https://{str(config.get('bucket', '')).strip()}.{host}"


def load_picbed_config(manager_root: Path | None, config_path: Path | None) -> dict[str, Any] | None:
    candidates: list[Path] = []
    if config_path:
        candidates.append(config_path)
    if manager_root:
        candidates.append(manager_root / "data" / "picbed_config.local.json")

    for candidate in candidates:
        if not candidate.exists():
            continue
        data = json.loads(candidate.read_text(encoding="utf-8"))
        required = ["bucket", "endpoint", "accessKeyId", "accessKeySecret"]
        if all(str(data.get(key) or "").strip() for key in required):
            data["endpoint"] = normalize_endpoint(data.get("endpoint"))
            data["publicBaseUrl"] = public_base_url(data)
            data["prefix"] = str(data.get("prefix") or "blog").strip().strip("/") or "blog"
            return data
    return None


def is_notion_image_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return (
        (host.endswith("notion.site") and parsed.path.startswith("/image/"))
        or host.endswith("notionusercontent.com")
        or host.startswith("prod-files-secure")
    )


def filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return "notion-image"
    name = unquote(parts[-1])
    if ":" in name:
        name = name.rsplit(":", 1)[-1]
    return re.sub(r'[^a-zA-Z0-9._-]+', "-", name).strip(".-") or "notion-image"


def extension_for(filename: str, content_type: str | None = None) -> str:
    ext = Path(filename).suffix.lower()
    if re.match(r"^\.[a-z0-9]+$", ext):
        return ext
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    return guessed if guessed and re.match(r"^\.[a-z0-9]+$", guessed) else ".jpg"


class NotionImageMirror:
    def __init__(self, config: dict[str, Any] | None):
        self.config = config
        self.cache: dict[str, str] = {}
        self.count = 0
        self.enabled = bool(config and oss2 is not None)
        self.bucket = None
        if self.enabled:
            auth = oss2.Auth(config["accessKeyId"].strip(), config["accessKeySecret"].strip())
            self.bucket = oss2.Bucket(
                auth,
                config["endpoint"],
                config["bucket"].strip(),
                connect_timeout=15,
            )

    def object_key(self, url: str, filename: str, slug: str, content_type: str | None = None) -> str:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        ext = extension_for(filename, content_type)
        return str(PurePosixPath(self.config["prefix"], "notion", slug, f"{digest}{ext}"))

    def download(self, url: str) -> tuple[bytes, str]:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
        )
        with urlopen(request, timeout=30) as response:
            return response.read(), response.headers.get("Content-Type", "image/jpeg")

    def mirror(self, url: str, slug: str) -> str:
        url = url.strip()
        if not self.enabled or not is_notion_image_url(url):
            return url
        if url in self.cache:
            return self.cache[url]

        filename = filename_from_url(url)
        content, content_type = self.download(url)
        object_key = self.object_key(url, filename, slug, content_type)
        safe_filename = filename.replace('"', "")
        self.bucket.put_object(
            object_key,
            content,
            headers={
                "Content-Type": content_type,
                "Content-Disposition": f'inline; filename="{safe_filename}"',
            },
        )
        mirrored_url = f"{self.config['publicBaseUrl']}/{object_key}"
        self.cache[url] = mirrored_url
        self.count += 1
        return mirrored_url


def mirror_markdown_images(markdown: str, mirror: NotionImageMirror, slug: str) -> str:
    image_re = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(\s+\"[^\"]*\")?\)")

    def replace(match: re.Match[str]) -> str:
        alt = match.group(1)
        url = match.group(2)
        title = match.group(3) or ""
        return f"![{alt}]({mirror.mirror(url, slug)}{title})"

    return image_re.sub(replace, markdown)


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
    image_mirror: NotionImageMirror,
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
            count += build_exports(child, directory_path, articles_dir, tree_node["children"], manifest, image_mirror)
        tree_node["count"] = count
        return count

    slug = slug_for(node)
    content = mirror_markdown_images(node_content(node), image_mirror, slug)
    description = str(node.get("description") or plain_text(content)[:80])
    date = str(node.get("lastEditedTime") or node.get("date") or datetime.now(timezone.utc).isoformat())
    article_path = articles_dir.joinpath(*[sanitize_part(p) for p in parent_path], f"{slug}.md")
    article_path.parent.mkdir(parents=True, exist_ok=True)

    tags = parent_path or ["未分类"]
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
    parser.add_argument("--manager-root", help="my-blog-manager root path. Used to read local OSS picbed config.")
    parser.add_argument("--picbed-config", help="Explicit OSS picbed config path.")
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
    image_config = load_picbed_config(
        Path(args.manager_root).resolve() if args.manager_root else None,
        Path(args.picbed_config).resolve() if args.picbed_config else None,
    )
    image_mirror = NotionImageMirror(image_config)
    if image_config and not image_mirror.enabled:
        print("OSS image mirroring skipped because oss2 is unavailable.", file=sys.stderr)
    for root in iter_roots(payload):
        build_exports(root, [], articles_dir, tree, manifest, image_mirror)

    manifest.sort(key=lambda item: (item.get("notionPath", ""), item.get("title", "")))
    (repo_path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (repo_path / "tree.json").write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"success": True, "articles": len(manifest), "mirroredImages": image_mirror.count, "repo": str(repo_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
