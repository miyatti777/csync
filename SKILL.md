# csync — Confluence Sync Skill

任意のConfluenceページ配下をローカルMarkdownにミラーリングし、差分同期・Push・Pullを行う汎用ツール。

## When to Use

- Confluenceスペースの一部をローカルにCloneしたいとき
- Confluenceページの変更をローカルに差分同期（Pull）したいとき
- ローカルで編集したMarkdownをConfluenceに反映（Push）したいとき
- Confluenceの内容をMarkdownベースで管理・編集したいとき

## How to Invoke（依頼の仕方）

### Cursor IDE チャットから

自然言語で依頼するだけで、適切なコマンドが実行されます:

| 依頼例 | 実行されるコマンド |
|--------|-------------------|
| 「Confluence同期して」 | `sync` |
| 「Confluenceの最新を取得して」 | `sync --refresh` |
| 「全ページ再ダウンロードして」 | `sync --force` |
| 「このファイルをConfluenceに反映して」 | `push <file>` |
| 「Meeting Notesだけ取得して」 | `pull -r <url>` |
| 「新しいページを作って」 | `new <parent> "Title"` |
| 「企画書をConfluenceに反映して（子ページも）」 | `push -r <file>` |
| 「同期状態を確認して」 | `status` |

### Claude Code / CLI から

```bash
# 双方向同期
python3 <skill_dir>/scripts/csync.py sync

# 特定ページをPush
python3 <skill_dir>/scripts/csync.py push path/to/page.md

# 同期状態確認
python3 <skill_dir>/scripts/csync.py status
```

## Architecture

```
<skill_dir>/              # .claude/skills/csync/ (プロジェクト or $HOME)
├── SKILL.md              # このファイル
├── README.md             # 詳細ドキュメント
├── .env                  # 共通認証情報（.gitignore 対象）
└── scripts/
    └── csync.py          # ツール本体（3,501行）

<output_dir>/             # ターゲットごとに init で生成
├── .csync.yaml           # 設定ファイル
├── csync.sh              # thin wrapper (自動生成・ポータブル)
├── _csync/               # csync専用状態ディレクトリ（nsyncの _nsync/ と共存可能）
│   ├── .env              # ローカル認証情報（省略可、共通が使われる）
│   ├── tree_cache.json   # クロール結果キャッシュ
│   └── sync_state.json   # 差分同期状態
├── Page名/
│   ├── Page名.md         # 同期されたページ
│   └── _assets/          # ダウンロードした添付ファイル
└── ...
```

## Token Management

認証情報は以下の優先順位で解決される:

1. `CONFLUENCE_EMAIL` + `CONFLUENCE_API_TOKEN` 環境変数
2. `<output_dir>/_csync/.env` — ワークスペース固有（異なるインスタンス用）
3. `<skill_dir>/.env` — 共通認証情報（通常はこれだけでOK）

認証方式: **Basic Auth**（email + API Token → base64エンコード）

初回セットアップ:

```bash
cat > <skill_dir>/.env << 'EOF'
CONFLUENCE_BASE_URL=https://your-domain.atlassian.net/wiki
CONFLUENCE_EMAIL=your-email@example.com
CONFLUENCE_API_TOKEN=your-api-token
EOF
```

API Tokenの発行: https://id.atlassian.com/manage-profile/security/api-tokens

## Quick Start

### 1. 新規ワークスペースのセットアップ

```bash
python3 <skill_dir>/scripts/csync.py init <confluence-page-url> [output-dir]
```

`init` は以下を自動実行:
- `.csync.yaml` 生成（Confluence APIからページタイトルを取得してlabel設定）
- `csync.sh` 生成（共通認証情報へのフォールバック付き）
- `_sync/.env` に認証情報をコピー（共通認証情報がある場合）

### 2. 同期

```bash
cd <output_dir>
./csync.sh sync     # 差分同期（初回はツリー取得も自動実行）
```

## Commands

| コマンド | API必要 | 説明 |
|---------|---------|------|
| `init <url> [dir]` | Yes | 新規ワークスペース作成 |
| `sync` | Yes | 双方向同期（ローカル変更Push→リモート変更Pull） |
| `sync --refresh` | Yes | ページ一覧を最新化してから同期 |
| `sync --force` | Yes | 全ページ強制再ダウンロード |
| `sync --full` | Yes | `--refresh` + `--force`（完全再同期） |
| `sync --dry-run` | Yes | 変更検出のみ（ダウンロードしない） |
| `sync --no-push` | Yes | ローカル変更の自動Pushをスキップ |
| `pull <file>` | Yes | 特定ページをConfluenceから再取得 |
| `pull -r <url>` | Yes | Confluence URLのサブツリーを再帰的にPull |
| `push <file>` | Yes | ローカルファイルをConfluenceに反映 |
| `push -r <file>` | Yes | 再帰Push（子ページも作成・更新） |
| `push --dry-run <file>` | No | Push内容のプレビュー |
| `new <parent> "Title"` | No | 新規ページ構造をローカルに生成（scaffold） |
| `status` | No | 同期状態のサマリー表示 |
| `init-state` | No | 既存ファイルから同期状態を再構築 |
| `full` | Yes | `init-state` + `sync`（一括実行） |

## Pull Quality (Confluence XHTML → Markdown)

### インライン装飾

Confluence XHTML Storage Format のインライン要素をMarkdownに変換:

| XHTML | Markdown |
|-------|----------|
| `<strong>` | `**Bold**` |
| `<em>` | `*Italic*` |
| `<del>` | `~~Strikethrough~~` |
| `<code>` | `` `Code` `` |
| `<a href="...">` | `[text](url)` |
| `<sub>` | `<sub>text</sub>` |
| `<sup>` | `<sup>text</sup>` |

### 対応ブロック要素

| XHTML要素 | Markdown出力 |
|-----------|-------------|
| `<h1>`〜`<h6>` | `#` 〜 `######` |
| `<p>` | テキスト |
| `<ul>/<li>` | `- text` |
| `<ol>/<li>` | `1. text` |
| `<ac:task-list>` | `- [x]` / `- [ ]` |
| `<blockquote>` | `> text` |
| `<hr>` | `---` |
| `<table>` | パイプ区切りテーブル |
| `<ac:image>` | `![alt](_assets/file.png)` |

### Confluenceマクロ対応

| マクロ | Markdown出力 |
|--------|-------------|
| `ac:structured-macro name="code"` | フェンスドコードブロック（言語付き） |
| `ac:structured-macro name="info"` | `> **ℹ️ INFO**: text` |
| `ac:structured-macro name="note"` | `> **📝 NOTE**: text` |
| `ac:structured-macro name="warning"` | `> **⚠️ WARNING**: text` |
| `ac:structured-macro name="tip"` | `> **💡 TIP**: text` |
| `ac:structured-macro name="expand"` | `<details><summary>title</summary>` |
| `ac:structured-macro name="panel"` | `> **PANEL**: text` |

### 添付ファイル

Pull時、Confluenceの添付ファイルを `_assets/` に自動ダウンロード。
画像・PDF・ドキュメント等を相対パスで参照可能。

## Pull (single page)

特定ページだけをConfluenceから再取得する。front matter の `confluence_id` を使って対象を特定。

```bash
./csync.sh pull path/to/page.md              # 実行
```

`sync` が全ページの差分同期なのに対し、`pull` は指定した1ファイルだけを即座に更新する。
`push` と対になるコマンド。

## Pull Recursive (subtree)

Confluence URL を指定して、そのページ配下のサブツリーだけを再帰的にクロール＆ダウンロードする。
`sync --refresh` がルート全体を再クロールするのに対し、`pull -r` は**対象ページ以下だけ**を処理するため高速。

```bash
./csync.sh pull -r "https://your-domain.atlassian.net/wiki/spaces/SPACE/pages/12345"
```

- サブツリーをクロールし、既存の tree_cache.json にマージ
- sync_state も更新されるので、次回 `sync` で二重ダウンロードされない

## Push Quality (Markdown → Confluence XHTML)

### インライン装飾

`**bold**`, `*italic*`, `~~strike~~`, `` `code` ``, `[link](url)` をConfluence XHTML Storage Formatに変換。

### 対応構文

見出し(H1-H6), リスト, チェックボックス, コードブロック, 引用, 区切り線, 画像, テーブル, 段落。
ローカル画像は Confluence Attachment API で自動アップロード。

### バージョン管理

Push時、Confluenceの `version.number` を自動インクリメント。
同時編集による競合（409 Conflict）は自動リトライで対応（最新version取得→再PUT）。

### dry-run モード

```bash
./csync.sh push --dry-run path/to/page.md
```

Confluenceに書き込まず、変換結果をプレビュー表示。

## New Page Creation（新規ページ作成）

### 1. Scaffold 生成

```bash
./csync.sh new "Parent Page" "企画書"
```

親はConfluence URL、ページタイトル、ローカル .md ファイルのいずれかで指定可能。
生成されるファイルには `confluence_parent` と `title` がfront matterに設定される。

### 2. Confluence に Push

```bash
./csync.sh push --dry-run -r 企画書/企画書.md    # 構造検証
./csync.sh push -r 企画書/企画書.md               # 一括作成
```

- `confluence_id` がない → `confluence_parent` に基づいて新規ページ作成
- `confluence_parent` もない → ディレクトリ構造から親ページを自動推定
- 作成後 `confluence_id` をfront matterに自動書き戻し
- `tree_cache` と `sync_state` も自動更新

### Front Matter（新規ページ用）

```yaml
---
confluence_parent: "12345"    # 作成先の親ページID
title: 企画書                  # ページタイトル
---
```

## Child Page Links

Pull時、子ページは相対パスのMarkdownリンクとして出力される。
VS Code/CursorでCmd+クリックでファイルに直接ジャンプ可能。

```markdown
[📄 Page Title](./Page Title/Page Title.md)
```

Push時はリンク位置を解析して、Confluence側の子ブロック順序を正確に復元。

## Config (.csync.yaml)

```yaml
root_page_id: "196701"
confluence_base_url: "https://your-domain.atlassian.net/wiki"
label: "My Space"
crawl_max_depth: 10
rate_limit_delay: 0.35
exclude_paths:
  - "_sync"
  - "_archived"
```

| Key | 説明 | デフォルト |
|-----|------|-----------|
| `root_page_id` | 同期対象のConfluenceルートページID | (必須) |
| `confluence_base_url` | Confluenceインスタンスのベース URL | (必須) |
| `label` | 表示用ラベル | ページタイトル or ディレクトリ名 |
| `crawl_max_depth` | クロールの最大深度 | `10` |
| `rate_limit_delay` | API呼び出し間の待機秒数 | `0.35` |
| `exclude_paths` | rglob検索から除外するパス文字列 | `["_sync", "_archived"]` |

## Data Model

### ページ → Markdown

YAML front matter付きで保存。front matterに `confluence_id`, `confluence_path`, `synced_at` を記録。
本文が空のコンテナページも正常に保存される。

### 差分同期状態

`_sync/sync_state.json` でSHA256ハッシュベースのファイル追跡を行う。
`_sync/tree_cache.json` にページツリーのキャッシュを保持。

## Typical Workflows

### 日次同期（双方向）
```bash
./csync.sh                     # ローカル変更→Push、リモート変更→Pull
./csync.sh sync --no-push      # Confluence→ローカルのみ（Push スキップ）
```

### 新規ページ作成
```bash
# 1. ローカルにscaffold生成
./csync.sh new "Parent Page" "新企画書"

# 2. MDファイルを編集

# 3. 構造検証 → Confluenceに作成
./csync.sh push --dry-run -r 新企画書/新企画書.md
./csync.sh push -r 新企画書/新企画書.md
```

### Push前のプレビュー
```bash
./csync.sh push --dry-run path/to/page.md
./csync.sh push path/to/page.md    # 確認後に実Push
```

### 全データ再取得
```bash
./csync.sh sync --full
```

### 初回セットアップ（既存ファイルあり）
```bash
./csync.sh init-state    # 既存ファイルからsync_state再構築
./csync.sh sync          # 差分同期開始
```

## Notes

- Confluence Cloud REST API v2 を使用（Basic Auth認証）
- `push` は子ページリンクを保護し、Position-aware Push で構造を復元
- `push -r` で新規ページ+子ページの一括作成に対応
- `new` でscaffold生成（フォルダ構造+front matter付きMD）
- PyYAMLがインストールされていない環境でも動作する簡易YAMLパーサー内蔵
- `.env` ファイルは `.gitignore` で除外済み（秘密情報は含まれない）
- クロールは50件ごとにチェックポイントを保存。中断しても次回途中から再開
- Rate Limit (429) は `Retry-After` ヘッダーを尊重してリトライ
- 完了時に API 統計を表示（呼び出し数、429回数、エラー数）
- `sync` はSHA256ハッシュで正確な差分検出（mtime偽陽性を排除）
- ローカル・リモート両方で変更されたファイルは CONFLICT として安全にスキップ
- リモート変更検出は Confluence `version.number` 比較で実現
- リネーム検出: Confluence側のページ名変更をsync時に検出し、ローカルファイルを自動移動
- XHTML ⇔ Markdown変換は xml.etree.ElementTree ベースの自前パーサーで実装
- Confluenceマクロ（code, info, note, warning, tip, expand, panel）に対応

## License

MIT License — 詳細は [LICENSE](LICENSE) を参照。
