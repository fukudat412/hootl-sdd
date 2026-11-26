# 技術選定（決め打ち）

## 1. ハーネス

| 用途 | 選定 | 理由 |
|---|---|---|
| 言語 | Python 3.11+、標準ライブラリのみ | 依存ゼロで Mac でもコンテナでも動く。設定は `tomllib` |
| キュー・状態 | SQLite（`.hootl/state.db`） | 1ファイル、ロック付き、後から集計しやすい |
| 作業分離 | git worktree | タスクごとに独立した作業ツリー。並列でも衝突しない |
| 実行ログ | `.hootl/runs/<ID>/<attempt>/` に全部ファイルで残す | 中間ファイルを品質の境界にする |
| テスト | pytest ＋偽エージェント | 実エージェントなしで全経路を検証できる |
| 通知 | Discord Webhook（任意） | 夜間ラン終了時に要約だけ送る |

## 2. エージェント

| 役割 | 選定 | 呼び方 |
|---|---|---|
| 計画（1回） | Claude Code（Opus 系） | `claude -p`、仕様からタスク案 JSONL を出す |
| 実装 | Claude Code（Sonnet 系） | `claude -p --output-format json --max-turns 80`、プロンプトは標準入力 |
| レビュー | Codex CLI | `codex exec --model <モデル> --sandbox read-only`、最後に verdict JSON を出させる |

- コマンドは `hootl.toml` の配列で持つので、モデルやベンダーの差し替えは設定だけで済む。
- **認証はサブスクのログインを使う。** `--bare` は OAuth を読まないので使わない（API キー前提になる）。
- 権限は `.claude/settings.json` の allow リストで絞る。権限確認を全部飛ばす `--dangerously-skip-permissions` は、コンテナ内で動かすときだけ使う。
- CLI のフラグは更新が速いので、導入時に `claude --help` と `codex exec --help` で一度確認する。
- codex のモデルは `--model` で明示する。既定（`~/.codex/config.toml`）に任せると、そのモデルが提供終了したときに codex が何も出さずに終わり、レビューが全部 fail になる。使えるモデルは `~/.codex/models_cache.json` の slug で確認できる。

## 3. 対象アプリの既定スタック

学習サイト・業務仕分けアプリなど、これから無人で作るものの既定。既存プロジェクトはそちらに合わせ、ゲートのコマンドだけ差し替える。

| 層 | 選定 |
|---|---|
| 言語 | TypeScript（strict） |
| パッケージ | pnpm |
| フロント | Vite ＋ React |
| ユニットテスト | Vitest |
| e2e | Playwright（Chromium のみ、スマホ幅も1本） |
| lint・整形 | Biome（1ツールで両方、速い） |
| ホスティング | Cloudflare（Workers の静的アセット、データは D1） |

対応するゲート（`hootl.toml`）:

```toml
[[gates]]
name = "lint"
cmd = "pnpm biome check ."
[[gates]]
name = "typecheck"
cmd = "pnpm tsc --noEmit"
[[gates]]
name = "unit"
cmd = "pnpm vitest run"
[[gates]]
name = "e2e"
cmd = "pnpm playwright test"
timeout_sec = 900
```

## 4. 実行場所

| 場所 | 使いどころ |
|---|---|
| Mac（そのまま） | まず1タスクで動作確認するとき。並列は 1〜2 |
| Docker コンテナ | 夜間に並列 3〜4 で回すとき。権限確認を飛ばしてよいのはここだけ |
| クラウドの作業環境 | Mac を閉じても進めたいとき |
