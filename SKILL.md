---
name: notion-blog-word-sync
description: Sync a Notion page tree into the juzi050/blog-word GitHub repo as Markdown, then import it into Juzi blog local drafts. Use when the user asks to pull Notion child pages into the blog archive/drafts without storing Notion tokens locally.
---

# Notion Blog Word Sync

## Purpose

Use this skill to synchronize a Notion root page into Juzi's blog workflow:

1. Fetch the Notion page tree from the published public Blog page when possible, otherwise use the Codex Notion connector or the token fallback.
2. Convert only leaf pages to Markdown articles.
3. Push `articles/`, `manifest.json`, and `tree.json` to `juzi050/blog-word`.
4. Pull that repo locally and import articles into `my-blog-manager/manager_data/drafts`.
5. Preserve public Notion page images when exporting leaf articles, and mirror them to the configured Juzi OSS image bed when available.

Prefer the published public Blog URL when the original `www.notion.so` URL is unavailable to the integration. Otherwise prefer the Codex Notion connector. If the connector cannot start and the user has supplied `NOTION_BLOG_WORD_TOKEN`, use the bundled fallback script. Do not store Notion tokens in the blog repo or the skill.

## Environment

- `NOTION_BLOG_WORD_TOKEN`: optional local Notion integration token supplied by the user. Read it from the process/user environment only. Never write the token value into Skill files, repo files, generated Markdown, logs, or commits.

## Default Paths

- Blog workspace: `D:\software\juzi-blog`
- Content cache: `D:\software\juzi-blog\.cache\blog-word`
- Content repo: `https://github.com/juzi050/blog-word`
- Public Blog URL: `https://saber-storm-523.notion.site/Blog-b528860c657a83ea858301631a850a1c`
- Drafts directory: `D:\software\juzi-blog\my-blog-manager\manager_data\drafts`
- OSS image bed config: `D:\software\juzi-blog\my-blog-manager\data\picbed_config.local.json`
- Published post checks:
  - `D:\software\juzi-blog\my-blog-manager\posts`
  - `D:\software\juzi-blog\XHBlogs\posts`

## Workflow

1. Prefer the public Blog URL (`https://saber-storm-523.notion.site/Blog-b528860c657a83ea858301631a850a1c`) when the original `www.notion.so` URL is unavailable to the integration.
2. Use the Notion connector `fetch` tool on the selected root page URL.
3. Recursively fetch child pages referenced by `<page url="...">` tags.
4. Build a JSON input tree with each node:
   - `id`
   - `title`
   - `url`
   - `lastEditedTime` if available
   - `content` for leaf pages
   - `children`
5. If the Notion connector is unavailable and the page is shared with the integration, build the same JSON input with:

```powershell
$env:NOTION_BLOG_WORD_TOKEN = [Environment]::GetEnvironmentVariable('NOTION_BLOG_WORD_TOKEN', 'User')
python C:\Users\86136\.codex\skills\notion-blog-word-sync\scripts\fetch_notion_tree.py `
  --root-url "https://saber-storm-523.notion.site/Blog-b528860c657a83ea858301631a850a1c" `
  --output D:\software\juzi-blog\.cache\blog-word-source.json
```

6. Run:

```powershell
python C:\Users\86136\.codex\skills\notion-blog-word-sync\scripts\write_blog_word_articles.py `
  --input D:\software\juzi-blog\.cache\blog-word-source.json `
  --repo D:\software\juzi-blog\.cache\blog-word `
  --manager-root D:\software\juzi-blog\my-blog-manager `
  --clean
```

7. Commit and push the content repo:

```powershell
git -C D:\software\juzi-blog\.cache\blog-word add .
git -C D:\software\juzi-blog\.cache\blog-word commit -m "Sync Notion archive"
git -C D:\software\juzi-blog\.cache\blog-word push
```

8. Pull the content repo and import drafts:

```powershell
git -C D:\software\juzi-blog\.cache\blog-word pull --ff-only
python C:\Users\86136\.codex\skills\notion-blog-word-sync\scripts\import_blog_word_to_drafts.py `
  --repo D:\software\juzi-blog\.cache\blog-word `
  --manager-root D:\software\juzi-blog\my-blog-manager `
  --xhblogs-root D:\software\juzi-blog\XHBlogs
```

## Export Rules

- Pages with child pages are directory nodes only.
- Pages without child pages become Markdown articles.
- Article slug is `notion-<last-12-normalized-page-id-chars>`.
- Article path is `articles/<Notion directory path>/<slug>.md`.
- Public Notion image links in article Markdown should be mirrored to the configured OSS image bed when `--manager-root` or `--picbed-config` is available; otherwise keep the original Notion image URL.
- Frontmatter must include:
  - `title`
  - `date`
  - `description`
  - `tags`
  - `cover`
  - `source: notion`
  - `notionId`
  - `notionUrl`
  - `notionPath`
  - `archiveOnly: true`

## Draft Import Rules

- Draft ID is the Markdown slug.
- Re-importing an unpublished draft overwrites the same JSON file.
- If `my-blog-manager/posts/<slug>.md` or `XHBlogs/posts/<slug>.md` exists, skip draft import by default to avoid duplicate archive entries.
- Use `--include-published` only when the user explicitly wants to overwrite local drafts for already published Notion articles.

## Safety

- Keep `.cache/`, local drafts, and any temporary input JSON out of the main blog git repo.
- Before pushing, verify no Notion token or private config was written.
- The content repo is public by user choice, so only export pages the user intends to publish.
