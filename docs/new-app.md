# 新しいアプリを作る（ひな形カタログ）

同じ作りのアプリを量産するための仕組み。管理画面の「＋ 新しいアプリ」から、作りたいもの・名前・型・オプション・ゲートを選んで「作る」を押す。

## 押すと起きること

1. 名前と道具を確かめる（node・docker など、その型が使うもの）
2. `~/hootl-runs/<名前>` にフォルダを切り、`git init`（main）
3. ひな形を置く（共通 → 型 → オプション の順に重ねる）
4. 依存を入れる（メジャー版を固定して入れ、lockfile で完全に固定する）
5. **ループと同じ条件で**、毎回の準備と全部のゲートを通す
6. 起動確認（Web は本番ビルドを起動して `/api/health`、スマホは Web 版を書き出して開く、バッチは `--dry-run`、Workers は `wrangler dev`）
7. `hootl.toml` を置いて最初のコミット
8. 作りたいものを書いていれば、仕様の下書きを作る（「仕様」タブに出る）
9. 「できたら、そのまま実装まで進める（おまかせ）」が付いていれば（既定）、下書きを凍結 → 計画 → 確かめて取り込み → 回す、まで続ける

途中で失敗したら、そのフォルダは `hootl-runs/.failed/<名前>-<日時>` に退避する（消さない）。同じ名前でもう一度作れる。
`hootl.toml` は最後に置くので、出来上がるまでは管理画面のプロジェクト一覧に出ない。

おまかせを外したときは、できあがったら「開く」→「仕様」タブで下書きを読んで「凍結して、あとはおまかせ」（または「凍結してタスク案まで」→「計画と承認」→ 回す）。ほかのプロジェクトと同じ。

## 型とオプション

| 型 | 中身 | ゲート（■は既定で使う、□は使わない） | 起動確認 |
| --- | --- | --- | --- |
| Webアプリ（web-next） | Next.js 16・React 19・TypeScript・Tailwind 4・Vitest | ■lint ■型 ■単体（必須） ■ビルド □起動 | 本番ビルドを起動して `/api/health` |
| スマホアプリ（mobile-expo） | Expo SDK 58・expo-router・Jest | ■lint ■型 ■単体（必須） ■iOS 向けの書き出し □依存の版 | Web 版を書き出して開く |
| バッチ（batch-python） | Python 3.11+・pytest・ruff・mypy | ■lint ■書式 ■型 ■単体（必須） ■`--dry-run` | `--dry-run` で1回流す |
| Cloudflare Workers（worker-cf） | Hono 4・wrangler 4・Vitest。HTTP と Cron Triggers | ■lint ■型 ■単体（必須） ■wrangler の dry-run □起動 | `wrangler dev` で `/api/health` |

| オプション | 型 | 足されるもの |
| --- | --- | --- |
| E2E テスト | Webアプリ | Playwright。ゲート「E2E」（ビルドの後） |
| Cloudflare Workers に載せる | Webアプリ | OpenNext・wrangler。ゲート「Cloudflare 向けビルド」。デプロイは人が `npm run deploy` |
| データベース | Webアプリ | Docker の PostgreSQL 18・Prisma 7。テスト用 DB を毎回まっさらに。ゲート「スキーマ」（必須） |
| Mac で毎日動かす | バッチ | `ops/launchd.plist`。登録は人が行う |

Cloudflare とデータベース（PostgreSQL）は一緒に使えない（Workers から Postgres へは Hyperdrive が要るため。必要になったら足す）。

## ゲートの切り替え

- 作るとき: 画面のゲートの一覧でチェックを外す（必須のものは外せない）
- あとから: プロジェクトの「設定」タブで切り替えて「保存して main にコミット」。次のタスクから効く
- CLI: `hootl -c hootl.toml gates --set lint,unit,build --commit`
- このアプリだけのゲート: `hootl.toml` に `[[gates]]` を書き足す（カタログのゲートの後に走る）

アプリの `hootl.toml` には、型・オプション・使うゲートの名前だけが入る:

```toml
[stack]
name = "web-next"
options = ["e2e"]
gates = ["lint", "typecheck", "unit", "build", "e2e"]
```

ゲートの中身（コマンド）・準備・安全柵・環境変数は `stacks/<型>/stack.toml` にあり、**同じ型のアプリ全部で共通**。カタログを直すと、次のタスクから全部のアプリに効く。

## カタログを増やす・直す

```
stacks/
  _common/            全部の型に共通（エージェントの設定・安全柵・CLAUDE.md・spec.md のひな形）
  web-next/
    stack.toml        型の定義（作る手順・毎回の準備・ゲート・起動確認・安全柵・オプション）
    files/            置くファイル（.tmpl は {{name}} などを埋める。gitignore.tmpl → .gitignore）
    options/<id>/files/  オプションで足すファイル
```

- ゲートを足す: `stack.toml` に `[[gates]]` を足す。`default = false` なら既存のアプリには入らず、設定タブで選べるようになる
- 型を足す: `stacks/<新しい型>/stack.toml` と `files/` を作る。必須のゲート（`required = true`）と `[[verify]]`（起動確認）を必ず持たせる
- 試す: `hootl new try-it --stack <型> --runs-dir /tmp/runs` で、作る → 全ゲート → 起動確認 → コミットまで流れるか確かめる
- 自分用の別カタログを使う: 環境変数 `HOOTL_STACKS_DIR` で場所を指す

埋められる値: `{{name}}`（フォルダ名）・`{{pkg}}`（ハイフンを _ に）・`{{title}}`（表示名）・`{{port}}`／`{{e2e_port}}`／`{{db_port}}`（アプリごとに重ならないよう自動で決める）・`{{python}}`・`{{hootl}}`・`{{repo}}`・`{{today}}`。

## 材料はアプリの中に置く

エージェント（仕様の下書き・実装・レビュー）は、そのアプリのフォルダの外を読めない。作りたいものの説明に「〜の資料を見て」と書いても読めないので、資料はアプリの中（例: `materials/`）に写してコミットしておく。写した元と日付を添えると、あとで写し直す区切りが分かる。

## 人がやること（自動ではやらない）

- 本番へのデプロイ・ストアへの提出（Cloudflare の `npm run deploy`、EAS Build など）。初回の `npx wrangler login` も人
- GitHub などのリモートを作って push 先にすること（作ったアプリは手元の git だけ。push したくなったら remote を足して `hootl.toml` の `[run]` に `push_remote = "origin"`）
- iOS シミュレーターや実機での確認（`npx expo start` → `i`）
- launchd への登録（バッチ）

## 確かめたこと（2026-11-26）

作業環境で、4つの型すべてについて「作る → 全ゲート → 起動確認 → コミット」まで通した。Cloudflare オプションも同様。
E2E は、ブラウザの取得先が作業環境から遮断されていたため、手元の Chromium で Playwright のテストが通ることを確かめた（Mac では `npx playwright install chromium` がそのまま動く）。
データベースは、Prisma のエンジンの取得先が作業環境から遮断されていたため、最後まで流していない（ファイルと手順は、別のプロジェクトで動いているものと同じ）。最初に使うときは、作成画面のチェックリストで「DB を作る」が通るかを見ること。
バッチのアプリでは、偽のエージェントでループを1周（準備 → 実装 → 5つのゲート → レビュー → マージ）回した。
