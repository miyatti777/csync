# csync — Confluence Sync Tool

任意のConfluenceページ配下をローカルMarkdownにミラーリングし、差分同期・Push・Pullを行うCLIツール。

**外部依存ゼロ**（Python 3.10+ 標準ライブラリのみ）。PyYAML はオプション。

[nsync](https://github.com/miyatti777/nsync) (Notion Sync Tool) のアーキテクチャを Confluence Cloud REST API v2 向けに移植。

## 特徴

- **Bidirectional Sync**: ローカル変更を自動検出→Push後、Confluence側の変更をPull（SHA256ハッシュで正確な差分検出）
- **Pull / Push**: 特定ページの個別取得・反映（XHTML ⇔ Markdown 変換）
- **Hierarchical Bootstrap**: ローカルのディレクトリ階層をConfluenceに自動再現
- **Title Deduplication**: Confluenceのスペース内タイトル一意制約に対応（`[親ディレクトリ名]` サフィックス自動付与）
- **Path-based Pre-linking**: 既存Confluenceページをパスベースで正確にマッチ
- **New Page Creation**: ローカルでページ構造を作成→Confluenceに一括反映
- **Claude Skill**: AIアシスタント（Claude / Cursor）から直接呼び出し可能
- **nsync共存**: nsyncと同一ディレクトリで同時運用可能（`_csync/` と `_nsync/` の分離）

## インストール

### Claude Skill として使う場合（推奨）

プロジェクトルートで:

```bash
mkdir -p .claude/skills
git clone https://github.com/miyatti777/csync.git .claude/skills/csync
```

または `$HOME/.claude/skills/` にグローバルインストール:

```bash
mkdir -p ~/.claude/skills
git clone https://github.com/miyatti777/csync.git ~/.claude/skills/csync
```

### スタンドアロンで使う場合

```bash
curl -o csync.py https://raw.githubusercontent.com/miyatti777/csync/main/scripts/csync.py
chmod +x csync.py
```

## 初期セットアップ

### 1. Confluence API Token の取得

1. [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens) にアクセス
2. 「API トークンを作成」をクリック
3. トークンをコピー

### 2. トークンの設定

```bash
cat > .claude/skills/csync/.env << 'EOF'
CONFLUENCE_BASE_URL=https://your-domain.atlassian.net/wiki
CONFLUENCE_EMAIL=your-email@example.com
CONFLUENCE_API_TOKEN=your-api-token-here
EOF
```

### 3. ワークスペースの初期化

```bash
python3 .claude/skills/csync/scripts/csync.py init \
  "https://your-domain.atlassian.net/wiki/spaces/SPACE/pages/12345" \
  ./my-confluence-docs
```

`init` は以下を自動実行:
- `.csync.yaml` 生成（Confluence APIからページタイトルを取得してlabel設定）
- `csync.sh` ラッパースクリプト生成
- `_csync/` ディレクトリ作成（同期メタデータ格納）
- `.env` を skill ディレクトリからコピー

### 4. 同期開始

```bash
cd my-confluence-docs
./csync.sh sync
```

## Quick Start

```bash
# 初回: ページツリーをクロール→ローカルにPull
./csync.sh sync --refresh

# 通常: 差分のみ同期
./csync.sh sync

# ローカル編集をPush
./csync.sh push path/to/page.md

# 再帰Push（子ページ含む）
./csync.sh push -r path/to/root.md

# 特定ページだけPull
./csync.sh pull <page-id-or-url>

# 再帰Pull
./csync.sh pull -r <page-id-or-url>

# 新規ページ作成
./csync.sh new <parent-page-id> "New Page Title"

# 同期状態確認
./csync.sh status

# sync_state再構築
./csync.sh init-state

# Dry-run（変更せずプレビュー）
./csync.sh sync --dry-run
```

## コマンド一覧

| コマンド | 説明 |
|---------|------|
| `init <url> [dir]` | ワークスペース初期化 |
| `sync [options]` | 双方向同期 |
| `pull <file\|url>` | 単一ページPull |
| `pull -r <url>` | 再帰Pull（サブツリー全体） |
| `push <file>` | 単一ページPush |
| `push -r <file>` | 再帰Push（子ページ含む） |
| `new <parent> "Title"` | 新規ページscaffold作成 |
| `status` | 同期状態表示 |
| `init-state` | sync_state.json再構築 |
| `full` | init-state + sync 一括実行 |

### sync オプション

| オプション | 説明 |
|-----------|------|
| `--refresh` | ページツリーを最新化してから同期 |
| `--force` | 全ページを強制再Pull |
| `--full` | init-state + sync |
| `--dry-run` | 変更を行わずプレビュー |
| `--no-push` | Pull のみ実行 |

## 設定ファイル (.csync.yaml)

`init` で自動生成。手動編集も可能。

```yaml
root_page_id: "196701"
confluence_base_url: "https://your-domain.atlassian.net/wiki"
label: "My Documentation"
crawl_max_depth: 10
rate_limit_delay: 0.35
exclude_paths:
  - "_csync"
  - "_nsync"
  - "_archived"
  - "_sync"
  - "_backup"
```

| キー | 説明 | デフォルト |
|------|------|----------|
| `root_page_id` | ルートページのID | (必須) |
| `confluence_base_url` | Confluence URL | (必須) |
| `label` | ワークスペースのラベル | ページタイトル |
| `crawl_max_depth` | クロール最大深さ | 10 |
| `rate_limit_delay` | API呼び出し間隔（秒） | 0.35 |
| `exclude_paths` | 同期から除外するパス | `["_csync", ...]` |

## ディレクトリ構造

```
my-confluence-docs/             # initで生成されるワークスペース
├── .csync.yaml                 # 設定
├── csync.sh                    # ラッパースクリプト
├── _csync/                     # 同期メタデータ
│   ├── .env                    # ローカル認証情報
│   ├── tree_cache.json         # ページツリーキャッシュ
│   └── sync_state.json         # 差分同期状態
├── Root Page/
│   ├── Root Page.md            # ページ本体
│   ├── Child Page/
│   │   └── Child Page.md
│   └── _assets/                # 添付ファイル
└── ...
```

## XHTML ⇔ Markdown 変換

### サポートされる要素

| 要素 | Pull (XHTML→MD) | Push (MD→XHTML) |
|------|:---:|:---:|
| 見出し (h1-h6) | ✅ | ✅ |
| 段落 | ✅ | ✅ |
| リスト (ol/ul/task) | ✅ | ✅ |
| テーブル | ✅ | ✅ |
| コードブロック | ✅ | ✅ |
| インラインコード | ✅ | ✅ |
| 太字/斜体/取消線 | ✅ | ✅ |
| リンク | ✅ | ✅ |
| 画像 | ✅ | ✅ |
| 引用 | ✅ | ✅ |
| 水平線 | ✅ | ✅ |
| Confluenceマクロ (info/note/warning/tip/expand/panel) | ✅ | ✅ |

## nsyncとの共存

csync と [nsync](https://github.com/miyatti777/nsync) は同一ディレクトリで同時に使用できます:

- csync: `_csync/` にメタデータ格納
- nsync: `_nsync/` にメタデータ格納
- frontmatter: 各ツール固有のフィールドが共存（`confluence_id` / `notion_id` 等）
- ハッシュ計算: frontmatterを除外して計算するため、相互の変更で偽陽性が発生しない

## トラブルシューティング

### 認証エラー (401)
- API Tokenが正しくコピーされているか確認
- `CONFLUENCE_EMAIL` が正しいか確認
- PAT（`ATATT3...`形式）ではなく、id.atlassian.com の API Token を使用すること

### タイトル衝突
- Confluenceはスペース内でタイトルの一意性を要求
- csyncは自動的に `[親ディレクトリ名]` サフィックスで一意化
- 例: `tasks.yaml` → `tasks.yaml [SG2]`

### 409 Conflict
- version競合時は自動リトライ（最大3回）
- 頻発する場合は `sync --force` で強制再同期

### Rate Limit (429)
- `.csync.yaml` の `rate_limit_delay` を増やす
- Confluence Free プランではレート制限が厳しい場合がある

## 技術スタック

- **Python 3.10+** / 単一ファイル (4,083行)
- **Confluence Cloud REST API v2** / Basic Auth
- **xml.etree.ElementTree** — XHTML ⇔ Markdown 変換
- **外部依存ゼロ** — PyYAML はオプション（内蔵簡易パーサーで代替）

## ライセンス

MIT License — [LICENSE](LICENSE)
