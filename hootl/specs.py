"""仕様の流れ: 作りたいもの → 仕様の下書き → 凍結（main にコミット）→ タスク案 → 取り込み。

  下書き  .hootl/spec-drafts/<slug>.md（＋ <slug>.idea.txt に元のことば）。git 管理外
  凍結    specs/<slug>.md として main にコミットする（push_remote があれば push も）
  全体    spec.md（あれば）は、どの仕様の計画にも前提として渡す

おまかせ（hootl auto）では、人が読んで直す代わりに、ここの機械の確認（check_plan など）で先へ進める。
通らないものは取り込まずに残し、人の承認に回す。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import gitops, ideas
from .config import Config
from .queue import REQUIRED, Queue

SLUG = re.compile(r"^[0-9]{8}-[0-9]{6}$")
SPECS_DIR = "specs"
TASK_ID = re.compile(r"^[\w.-]{1,64}$")

# 仕様の下書きで、決めきれない点をどう扱うか（{undecided}）
ASK = ("  - `## 決めてほしいこと` 持ち主の書いたことだけでは決められない点。推測で埋めず、ここに質問として並べる"
       "（無ければ「なし」）")
DECIDE = ("  - `## 自分で決めたこと` 持ち主の書いたことだけでは決められなかった点。この仕様は人の確認なしで"
          "そのまま実装に進むので、質問は残さない。今のコードと持ち主のことばに一番合う、小さくて後から変えやすい案に"
          "自分で決め、「何を・なぜそう決めたか」を並べる（無ければ「なし」）")


def drafts_dir(cfg: Config) -> Path:
    return cfg.state_dir / "spec-drafts"


def new_slug(cfg: Config | None = None) -> str:
    """下書きの名前（日時）。cfg を渡すと、すでにある下書き・凍結済みの仕様と重ならない名前にする。

    同じ秒に続けて作ると名前が重なり、前の仕様と同じファイルを凍結しようとして止まるので、1秒ずつ先へずらす。
    """
    t = time.time()
    while True:
        slug = time.strftime("%Y%m%d-%H%M%S", time.localtime(t))
        if cfg is None or not (any(drafts_dir(cfg).glob(f"{slug}.*")) or (cfg.repo / SPECS_DIR / f"{slug}.md").exists()):
            return slug
        t += 1


def title_of(text: str, fallback: str = "名前のない仕様") -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:60] or fallback
    return fallback


CLI_NOTICE = re.compile(r"\n+(?:Warning|Note|Notice|Error): [^\n]*(?:\n[^\n#]*)*\s*$")


def strip_fences(text: str) -> str:
    """モデルが ```markdown で囲んで返したときに外す。末尾に CLI のお知らせ（ログインの期限など）が混ざったら落とす。"""
    t = text.strip()
    t = CLI_NOTICE.sub("", t).strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", t, re.S)
    return (m.group(1) if m else t).strip() + "\n"


# ---- タスクの番号 --------------------------------------------------------------
def next_task_id(queue: Queue) -> str:
    nums = [int(m.group(1)) for t in queue.all() if (m := re.fullmatch(r"T-(\d+)", t.id))]
    return f"T-{(max(nums) + 1) if nums else 1:03d}"


def existing_summary(queue: Queue, limit: int = 200) -> str:
    tasks = queue.all()
    if not tasks:
        return "（まだない）"
    lines = [f"- {t.id} {t.title}（{t.status}{'・' + t.epic if t.epic else ''}）" for t in tasks[-limit:]]
    return "\n".join(lines)


# ---- プロンプト ----------------------------------------------------------------
def plan_prompt(cfg: Config, spec_path: Path, queue: Queue) -> str:
    spec_text = spec_path.read_text(encoding="utf-8")
    base = cfg.repo / "spec.md"
    context = ""
    if base.is_file() and base.resolve() != spec_path.resolve():
        context = "# 全体の前提（spec.md）\n\n" + base.read_text(encoding="utf-8")
    return (cfg.prompts_dir / "plan.md").read_text(encoding="utf-8").format(
        spec=spec_text, context=context, existing=existing_summary(queue),
        next_id=next_task_id(queue), epic=title_of(spec_text, spec_path.stem),
        ideas=ideas.fragment(cfg),
    )


def spec_prompt(cfg: Config, idea: str, decide: bool = False) -> str:
    """decide: 決めきれない点を、質問として残さず自分で決めさせる（人が読まずに進むおまかせ用）。"""
    base = "spec.md" if (cfg.repo / "spec.md").is_file() else "（なし）"
    past = sorted((cfg.repo / SPECS_DIR).glob("*.md"))
    past_list = "、".join(f"{SPECS_DIR}/{p.name}" for p in past) or "（なし）"
    return (cfg.prompts_dir / "spec.md").read_text(encoding="utf-8").format(
        idea=idea.strip(), base_spec=base, past_specs=past_list,
        undecided=DECIDE if decide else ASK, ideas=ideas.fragment(cfg))


def spec_text_of(output: str) -> str:
    """仕様づくりの出力から本文だけを取り出す（ネタ帳の行・囲み・CLI のお知らせを落とす）。"""
    return strip_fences(ideas.strip(strip_fences(output)))


# ---- タスク案の確かめ ------------------------------------------------------------
def validate_jsonl(text: str) -> list[dict]:
    """形の確かめ（人が承認するときも、おまかせのときも通す）。"""
    tasks = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{n}行目が JSON として読めません: {exc.msg}") from exc
        missing = [k for k in REQUIRED if not t.get(k)]
        if missing:
            raise ValueError(f"{n}行目（{t.get('id', '?')}）に {', '.join(missing)} がありません")
        if not TASK_ID.match(str(t["id"])):
            raise ValueError(f"{n}行目の id に使えない文字があります")
        if not isinstance(t["acceptance"], list) or not isinstance(t["paths"], list):
            raise ValueError(f"{n}行目（{t['id']}）の acceptance と paths は配列にしてください")
        tasks.append(t)
    if not tasks:
        raise ValueError("タスクが1件もありません")
    ids = [t["id"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("id が重複しています")
    return tasks


def check_plan(tasks: list[dict], queue: Queue, max_tasks: int) -> list[str]:
    """人の承認の代わりにする確かめ。問題を並べて返す（空なら取り込んでよい）。

    見るのは「取り込むと、気づかれないまま止まる・壊す」もの: すでにあるタスクの上書き、
    行き先のない依存（永久に待ちのまま）、依存の輪、1つの仕様にしては多すぎる件数（発散）。
    """
    problems = []
    existing = {t.id for t in queue.all()}
    ids = [str(t["id"]) for t in tasks]
    clash = [i for i in ids if i in existing]
    if clash:
        problems.append(f"すでにある id と重なっています: {', '.join(clash)}")
    if len(tasks) > max_tasks:
        problems.append(f"タスクが {len(tasks)} 件あります（おまかせで取り込むのは {max_tasks} 件まで）")
    known = existing | set(ids)
    deps = {str(t["id"]): [str(d) for d in t.get("depends_on") or []] for t in tasks}
    for tid, ds in deps.items():
        unknown = [d for d in ds if d not in known]
        if unknown:
            problems.append(f"{tid} が、ないタスクに依存しています: {', '.join(unknown)}")
    # 依存の輪（案の中だけで見る。すでにあるタスクは案に依存しない）
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2 or node not in deps:
            return False
        state[node] = 1
        looped = any(visit(d) for d in deps[node])
        state[node] = 2
        return looped
    if any(visit(i) for i in ids):
        problems.append("依存が輪になっています（どれも始められません）")
    for t in tasks:
        if not all(isinstance(a, str) and a.strip() for a in t["acceptance"]):
            problems.append(f"{t['id']} の受け入れ条件に空のものがあります")
    return problems


def proposed_path(cfg: Config) -> Path:
    return cfg.state_dir / "proposed-tasks.jsonl"


def import_proposed(cfg: Config, queue: Queue) -> int:
    """タスク案を取り込み、案は控え（imported-<日時>.jsonl）に回す。"""
    path = proposed_path(cfg)
    if not path.exists():
        raise ValueError("タスク案がありません")
    validate_jsonl(path.read_text(encoding="utf-8"))
    n = queue.import_jsonl(path)
    path.rename(path.with_name(f"imported-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
    return n


# ---- 下書き・凍結 ----------------------------------------------------------------
def list_specs(cfg: Config) -> dict:
    def entry(path: Path, kind: str, rel: str) -> dict:
        text = path.read_text(encoding="utf-8")
        return {"kind": kind, "path": rel, "slug": path.stem, "title": title_of(text, path.stem),
                "mtime": path.stat().st_mtime}
    drafts = [entry(p, "draft", f"draft:{p.stem}") for p in sorted(drafts_dir(cfg).glob("*.md"), reverse=True)
              if SLUG.match(p.stem)]
    frozen = [entry(p, "frozen", f"{SPECS_DIR}/{p.name}")
              for p in sorted((cfg.repo / SPECS_DIR).glob("*.md"), reverse=True)]
    base = cfg.repo / "spec.md"
    return {"drafts": drafts, "frozen": frozen,
            "base": entry(base, "base", "spec.md") if base.is_file() else None}


def resolve(cfg: Config, ref: str) -> Path:
    """画面から来た参照を、読んでよいファイルに限って実パスにする。"""
    if ref == "spec.md":
        path = cfg.repo / "spec.md"
    elif ref.startswith("draft:") and SLUG.match(ref[6:]):
        path = drafts_dir(cfg) / f"{ref[6:]}.md"
    elif re.fullmatch(rf"{SPECS_DIR}/[\w.-]+\.md", ref):
        path = cfg.repo / ref
    else:
        raise LookupError("見られない仕様です")
    if not path.is_file():
        raise LookupError("仕様がありません")
    return path


def save_draft(cfg: Config, slug: str, text: str) -> Path:
    if not SLUG.match(slug):
        raise ValueError("下書きの名前が不正です")
    if not text.strip():
        raise ValueError("中身が空です")
    path = drafts_dir(cfg) / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


def freeze(cfg: Config, slug: str) -> Path:
    """下書きを specs/<slug>.md として main にコミットする。push_remote があれば push する。"""
    draft = drafts_dir(cfg) / f"{slug}.md"
    if not SLUG.match(slug) or not draft.is_file():
        raise LookupError("下書きがありません")
    text = draft.read_text(encoding="utf-8")
    if "## やること" not in text:
        raise ValueError("「## やること」の見出しがありません。下書きを直してから凍結してください")
    repo = cfg.repo
    branch = gitops.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != cfg.main_branch:
        raise ValueError(f"専用 clone が {cfg.main_branch} 以外（{branch}）にいます")
    # 凍結でコミットするのは仕様の1ファイルだけ。止めるのは、追跡中のファイルに未コミットの変更があるときだけ
    dirty = [ln for ln in gitops.git(repo, "status", "--porcelain", "--untracked-files=no").splitlines() if ln.strip()]
    if dirty:
        raise ValueError("専用 clone に未コミットの変更があります: " + ", ".join(d[3:] for d in dirty[:5]))
    if cfg.push_remote:
        ok, info = gitops.sync_main(repo, cfg.push_remote, cfg.main_branch)
        if not ok:
            raise ValueError(f"main を remote と同期できません: {info}")
    target = repo / SPECS_DIR / f"{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    rel = f"{SPECS_DIR}/{slug}.md"
    gitops.git(repo, "add", rel)
    gitops.git(repo, "-c", "user.name=hootl", "-c", "user.email=hootl@localhost",
               "commit", "-q", "-m", f"spec: {title_of(text)}", "--", rel)
    if cfg.push_remote:
        ok, info = gitops.push_main(repo, cfg.push_remote, cfg.main_branch)
        if not ok:
            raise ValueError(f"コミットはしたが push に失敗しました: {info[:200]}")
    draft.rename(draft.with_suffix(".frozen"))   # 下書きは控えとして残す
    return target


def ensure_main_clean(cfg: Config, allow: str | None = None) -> None:
    """main にいて、追跡中のファイルに（allow 以外の）未コミットの変更がないこと。"""
    repo = cfg.repo
    branch = gitops.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != cfg.main_branch:
        raise ValueError(f"専用 clone が {cfg.main_branch} 以外（{branch}）にいます")
    dirty = [ln[3:] for ln in gitops.git(repo, "status", "--porcelain", "--untracked-files=no").splitlines()
             if ln.strip() and ln[3:] != allow]
    if dirty:
        raise ValueError("専用 clone に未コミットの変更があります: " + ", ".join(dirty[:5]))


def commit_file(cfg: Config, path: Path, message: str) -> None:
    """1ファイルだけを main にコミットする（設定の切り替えなど、人が画面でした変更）。"""
    rel = str(path.resolve().relative_to(cfg.repo.resolve()))
    ensure_main_clean(cfg, allow=rel)
    gitops.git(cfg.repo, "add", rel)
    gitops.git(cfg.repo, "-c", "user.name=hootl", "-c", "user.email=hootl@localhost",
               "commit", "-q", "-m", message, "--", rel)
    if cfg.push_remote:
        ok, info = gitops.push_main(cfg.repo, cfg.push_remote, cfg.main_branch)
        if not ok:
            raise ValueError(f"コミットはしたが push に失敗しました: {info[:200]}")
