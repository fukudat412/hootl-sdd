# RUNBOOK: 新しいプロジェクトに hootl を導入する

Claude Code が上から順に実行するための手順書。人はそばにいる前提で、**止まる条件に当たったら推測で回避せず、状況と選択肢を添えて人に聞く**。各手順の「確認」が通らなければ次に進まない。

最初のプロジェクト（2026-11）で初回ランを通すまでに踏んだ落とし穴は、末尾の「落とし穴」にまとめてあり、手順の中で先回りして潰す。

## 入力（依頼文で受け取る）

| 名前 | 例 | 意味 |
| --- | --- | --- |
| `NAME` | `my-app` | キットと専用 clone の名前（英小文字とハイフン） |
| `PROJECT_DIR` | `~/work/my-app` | 人が普段作業している clone（リポジトリのルート） |
| `APP_DIR` | `.` / `apps/web` | hootl で回すアプリのディレクトリ（リポジトリ内の相対パス）。省略時は `.` |

以下、`HOOTL=<hootl-sdd を clone した場所>`、`KIT=$HOOTL/kits/$NAME`、`RUN_DIR=~/hootl-runs/$NAME`（`hootl setup` が決めた `projects_dir` の下）とする。`kits/<NAME>/` はそのプロジェクトだけの設定なので、コミットしなくてよい。

**`APP_DIR` が `.` 以外（モノレポの中の1アプリ）のとき**: hootl はリポジトリのルートで動く。`[[setup]]` と `[[gates]]` のコマンドは `cd <APP_DIR> && ...` の形にし、`protected_paths`・`always_allowed`・`test_globs`・タスクの `paths` はすべてリポジトリのルートからのパス（`<APP_DIR>/...`）で書く。

## 守ること

- `.env` などの秘密情報の中身を表示・出力・コピーしない。中身はホスト名だけ確かめる（`grep -o '@[^/:]*' .env` 程度）
- 本番の DB・外部サービスに書き込む操作をしない
- `git push --force` をしない。push は手順 8 の1回だけ
- `$HOOTL` のハーネスのコード（`hootl/`・`prompts/`）は変えない。作るのは `kits/$NAME/` だけ
- `$KIT` か `$RUN_DIR` が既にあれば、上書きせずに止まって聞く

---

## 手順 1: 道具がそろっているか確かめる

```bash
cd $HOOTL && python3 -m pytest -q     # 全部 passed
which hootl claude codex git
claude --help | grep -E -- '--(print|output-format|max-turns|model|settings)'
codex exec --help | grep -E -- '--(model|sandbox)'
cd $PROJECT_DIR && git status -sb && git remote -v
```

**確認**: pytest が全部通る。3つのコマンドにパスがある。`claude` に `--print` `--output-format` `--max-turns` `--model` `--settings`、`codex exec` に `--model` `--sandbox` がある。`PROJECT_DIR` が main にいて、未コミットの変更がない。

**止まる**:
- 未コミットの変更がある（人の作業途中かもしれない）
- `git status -sb` が `ahead N` を示す → hootl の clone は GitHub から作るので、push されていない変更は無人ループから見えない。push してよいか人に聞く（手順 2 の自動デプロイの確認を先に済ませる）

## 手順 2: 外の世界への影響を棚卸しする（ここが一番大事）

hootl は main にマージして push する。push や DB 操作が外の世界に届くかを先に洗い出す。

1. **push で自動デプロイされるか**
   - リポジトリ内: `vercel.json`、`netlify.toml`、`wrangler.toml` / `wrangler.jsonc`、`.github/workflows/` の deploy 系ジョブ（`on: push` で main が対象のもの）
   - リポジトリの外（Vercel・Cloudflare Pages・Netlify の Git 連携）は中から見えないので、README・CLAUDE.md の記述と、上のファイルの有無から推測し、人に確かめる
2. **本番の接続先が `.env` 系にあるか**: `.env*` のホスト名だけを確かめる（localhost 以外はすべて報告する）
3. **DB を使うか**: Prisma / Drizzle / TypeORM / SQL ファイル / docker-compose の有無
4. **ビルドやテストのスクリプトが外に書き込むか**: `package.json` の `build` / `test` / `postinstall` に `db push`・`migrate`・`deploy`・`upload` が含まれていないか

結果を次の表にまとめる:

| 項目 | 見つかったもの | hootl への影響 |
| --- | --- | --- |
| 自動デプロイ | | push_remote を有効にできるか |
| 本番の接続先 | ホスト名のみ | ゲートに渡してはいけない |
| DB | | テスト用 DB を用意するか |
| 外に書き込むスクリプト | | ゲートで使ってはいけないコマンド |

**止まる（必ず人に聞く）**:
- 自動デプロイがある → 選択肢を出す: ①デプロイ連携を外す ②`push_remote` を無効にして、朝に人が push する ③デプロイされてもよいと判断する（静的サイトなど）
- 本番 DB を `.env` が指している → 選択肢を出す: ①ローカルの DB に移す②テスト用 DB だけ別に用意して、本番は手元の作業でだけ使う

人の答えを表の下に書き残してから次へ進む。

## 手順 3: ゲートを main の上で実測する

プロジェクトの lint・型チェック・ユニットテスト・ビルドのコマンドを特定し、**npm scripts を経由せずツールを直接**実行する。

```bash
cd $PROJECT_DIR
cat .nvmrc 2>/dev/null; node -v
# 例（実際のツールに合わせる）
time npx eslint .
time npx tsc --noEmit
time npx vitest run
time npx next build        # ビルドは DB マイグレーションやデプロイを含まないコマンドを選ぶ
```

**確認**: 使うと決めたゲートが全部終了コード 0。それぞれの所要時間を記録する（e2e のように 5 分を超えるものは、最初は入れない）。

**止まる**:
- 既存コードのせいで lint や型チェックが落ちる → 直さずに報告（直すか、ゲートから外すか、人が決める）
- ユニットテストの仕組みがない → 手順 4 へ
- `.nvmrc` がなく、Node のプロジェクトである → 今の Node のメジャー版で `.nvmrc` を作ってよいか聞く

## 手順 4: テストの仕組みがなければ入れる

ユニットテストの仕組みがないときだけ行う。Vite・Next・Astro などの TypeScript プロジェクトなら Vitest を入れる。

1. 依存に `vitest` を追加する（パスエイリアスがあれば解決できるようにする）
2. `vitest.config.ts`: 対象は `tests/**/*.test.ts`、environment は `node`
3. `tests/smoke.test.ts` を1本だけ置く
4. CI（`.github/workflows/`）があれば、lint や型チェックと同じジョブにユニットテストを足す

**確認**: 手順 3 のゲートが全部、もう一度通る。

## 手順 5: キットを作る

```bash
mkdir -p $KIT/tasks
cp $HOOTL/kits/_template/hootl.toml $KIT/hootl.toml
cp $HOOTL/kits/_template/claude-settings.hootl.json $KIT/claude-settings.hootl.json
```

1. **`$KIT/hootl.toml`**: `<...>` をすべて埋める。
   - `--settings` のパスの `<name>` を `$NAME` に
   - `[[setup]]`: Node なら `node`、lockfile どおりの `install`、DB があればテスト用 DB のリセット
   - `[[gates]]`: 手順 3 で緑を確かめたコマンドだけ
   - `protected_paths`: 実在する lint・型・テスト・CI の設定ファイル名に合わせる
   - `always_allowed`: `package.json`・lockfile・スキーマ・生成される UI 部品など、どのタスクでも触りうるもの
   - `forbidden_patterns`: 手順 2 で見つけた本番のホスト名を足す
   - `[env]`: テスト用の接続先とダミーの秘密情報だけ。本番の値は入れない
   - `push_remote`: 手順 2 の答えで自動デプロイの心配がないときだけ残す
   - `<codex-model>` は手順 6 で埋める
2. **`$KIT/claude-settings.hootl.json`**: `deny` の `Read(...)` に、手順 2 で見つけた秘密情報のファイルを足す。
3. **CLAUDE.md**: `$HOOTL/kits/_template/CLAUDE.hootl.md` の内容を、`$PROJECT_DIR/CLAUDE.md` の末尾に追記する（なければ CLAUDE.md を作る）。`<!-- RUNBOOK 手順 5 -->` のコメント箇所を、このプロジェクトに合わせて埋めてからコメントを消す。
4. **`spec.md`**: `$PROJECT_DIR/spec.md` に下書きを作る。README・docs・TODO から「目的」「変えない前提（技術構成）」「今期やること（優先順の箇条書き）」「やらないこと」を拾う。冒頭に「下書き。人が直して凍結する」と書く。
5. **`.gitignore`**: `.hootl/` と `hootl.toml` を足す。

**確認**: `grep -n '<' $KIT/hootl.toml` で `<...>` が `<codex-model>` 以外に残っていない。`python3 -c "import tomllib;tomllib.load(open('$KIT/hootl.toml','rb'))"` が通る。

## 手順 6: エージェントが無人で応答するか確かめる

ここを飛ばすと、初回ランでレビューが全部 fail になる（実際に起きた）。

1. **codex のモデルを選ぶ**: `~/.codex/models_cache.json` から使える slug を一覧し、汎用の最上位モデルを1つ選ぶ。

   ```bash
   grep -o '"slug": *"[^"]*"' ~/.codex/models_cache.json | sort -u
   ```

2. **レビュー役の疎通**: 選んだモデルで、hootl と同じ読み方で verdict が取れるか試す。

   ```bash
   cd $PROJECT_DIR
   echo '最後の行に {"verdict": "pass", "findings": []} とだけ出力してください。' \
     | codex exec --model <選んだslug> --sandbox read-only - > /tmp/hootl-codex-smoke.txt 2>&1; echo "exit $?"
   python3 -c "import sys;sys.path.insert(0,'$HOOTL');from hootl.agents import parse_verdict;v=parse_verdict(open('/tmp/hootl-codex-smoke.txt').read());print('verdict ok' if v.raw_ok and v.passed else 'NG', v.findings)"
   ```

3. **実装役の疎通**: `--settings` 付きで、確認待ちにならず JSON が返るか試す。

   ```bash
   echo 'このリポジトリの package.json の name を1行で答えて' \
     | claude -p --model sonnet --output-format json --max-turns 3 --settings $KIT/claude-settings.hootl.json \
     | tail -1 | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print('claude ok', d.get('is_error'), str(d.get('result'))[:80])"
   ```

**確認**: `verdict ok` と `claude ok False` が出る。`$KIT/hootl.toml` の `<codex-model>` を選んだ slug に置き換える。

**止まる**: どちらかが NG。codex の出力末尾のエラー文を添えて報告する（モデル名の誤り、ログイン切れなど）。

## 手順 7: 最初のタスクを2件作る

初回ランは「壊れても被害が出ない」タスクで、ハーネスとプロジェクトの噛み合わせを確かめる。`src/lib` などにある**純粋関数**を2つ選び、次の形にする。

- 時刻や乱数を内部で直接使っていれば、引数で注入できるようにする（既定値は今の挙動のまま）
- 今の挙動を固定するテストを書く（ロジックは変えない）

`$KIT/tasks/first-night.jsonl` に1行1タスクで書く。形式は `$HOOTL/kits/_template/tasks/first-night.example.jsonl` と同じ（`id`・`title`・`description`・`acceptance`（テストで確かめられる文の配列）・`paths`・`depends_on`・`epic`・`priority`）。

**確認**:

```bash
python3 -c "
import json
for i,l in enumerate(open('$KIT/tasks/first-night.jsonl'),1):
    t=json.loads(l); miss=[k for k in ('id','title','description','acceptance','paths') if not t.get(k)]
    print(t['id'], 'ok' if not miss else f'missing {miss}', len(t['acceptance']), 'AC')
"
```

## 手順 8: プロジェクト側の変更をコミットして push する

```bash
cd $PROJECT_DIR
git add -A && git status --short
```

**確認**: `.env` 系（`.env.example` は可）、`backups/`、`.claude/settings.local.json`、`hootl.toml` が含まれていない。含まれていたら `git reset` して止まる。

```bash
git commit -m "chore: hootl の導入準備（CLAUDE.md の無人ループ節・spec.md の下書き・テスト基盤）"
git push origin main
```

**止まる**: 手順 2 で「push すると自動デプロイされる」と分かっていて、人が③（デプロイされてもよい）を選んでいない。

## 手順 9: 専用の clone を作ってタスクを入れる

```bash
mkdir -p ~/hootl-runs && cd ~/hootl-runs
git clone "$(git -C $PROJECT_DIR remote get-url origin)" $NAME && cd $NAME
cp $KIT/hootl.toml .
test ! -e .env && echo "no .env (ok)"
git status --short                   # 何も出ない（hootl.toml は .gitignore 済み）
hootl import $KIT/tasks/first-night.jsonl
hootl status
```

**確認**: `.env` がない。`git status --short` が空。`hootl status` が `todo 2`。

## 手順 10: 人に引き継ぐ

初回ランは長時間かかり、中で `claude` と `codex` を起動するので、このセッションからは実行しない。次を報告して終える。

- 手順 2 の表と、人が選んだ対応
- 手順 3 のゲートと所要時間
- 手順 6 で選んだ codex のモデル
- 手順 7 のタスク2件のタイトル
- 手順 8 のコミットハッシュ
- 人が別のターミナルで打つコマンド:

  ```bash
  cd ~/hootl-runs/<NAME>
  nvm use && caffeinate -i hootl run --concurrency 1   # .nvmrc がなければ nvm use は外す
  ```

---

## 落とし穴（初回ランで踏んだもの）

| 症状 | 原因 | 先回りの手順 |
| --- | --- | --- |
| レビューが全部「verdict JSON が読めませんでした」で blocked | codex の既定モデルが提供終了していて、何も出さずに終わっていた | 手順 6 で `--model` を明示し、疎通を確かめる |
| 準備の `node` で止まる、または古い Node のまま全部通ってしまう | 古いターミナルから `hootl run` した | `.nvmrc` を置き、準備で版を確かめる。`nvm use` してから回す |
| エージェントが `--force-reset` / `--accept-data-loss` を使えず詰まる | Prisma が AI エージェントからの実行を拒否する | データが消える反映はハーネス側（準備とゲート）でやる。CLAUDE.md にそう書く |
| `--skip-generate` がない、`DIRECT_URL` が効かない | Prisma 7 で廃止 | テンプレートの Prisma の例は 7 前提。古い版なら合わせる |
| PostgreSQL 18 のコンテナでデータが消える | 18 からデータの置き場所が `/var/lib/postgresql/18/docker` に変わった | volume は `/var/lib/postgresql` に当てる。ポートは `127.0.0.1:` を付けて Mac の中だけにする |
| 人の Claude Code まで npm install や git commit を拒否される | 無人用の制限をリポジトリの `.claude/settings.json` に置いた | 制限は `--settings` で無人エージェントにだけ渡す（テンプレートの形） |
| 依存の追加やスキーマ変更のたびに差し戻しが続く | 行動を禁止しすぎた | 守るのは判定する側だけ。範囲外の変更は `out_of_scope = "review"` でレビューに判断させる |
| 移行スクリプトの取り込みが1文目で止まる | ダンプが `CREATE SCHEMA public` から始まる | 取り込み前に空の `public` を消す |
| 無人ループが古いコードの上で作業する | 手元の main を push していなかった（clone は GitHub から作る） | 手順 1 で `ahead` を確認し、先に push する |
| テストの期待値を書き換えて「通す」 | 期待値の変更は削除でも skip でもないので安全柵をすり抜ける | 決定論の基準値（golden など）を持つプロジェクトは、CLAUDE.md とレビュー観点に「期待値を変えるのは、受け入れ条件に挙動の変化が明記されたタスクだけ」と書く |
