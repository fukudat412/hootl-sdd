"""ひな形カタログ（stacks/）。アプリの「型」と、そこに差し込む「オプション」と「ゲート部品」。

量産するアプリは hootl.toml に [stack] だけを書き、ゲート・準備・安全柵・環境変数はここから組み立てる。
カタログを直せば、同じ型のアプリ全部に効く（ゲートの中身を各アプリにコピーしない）。

    [stack]
    name = "web-next"
    options = ["e2e"]
    gates = ["lint", "typecheck", "unit", "build"]   # 使うゲート（必須のものは書かなくても入る）
    [stack.vars]
    port = 3101
"""

from __future__ import annotations

import copy
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STACKS_DIR = ROOT / "stacks"


def default_dir() -> Path:
    """カタログの置き場所。HOOTL_STACKS_DIR で別のカタログ（自分用に増やしたもの）を指せる。"""
    env = os.environ.get("HOOTL_STACKS_DIR")
    return Path(env).expanduser() if env else STACKS_DIR
COMMON = "_common"
PORT_LABELS = {"port": "開発サーバー", "e2e_port": "E2E", "db_port": "DB"}
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
VAR_RE = re.compile(r"\{\{(\w+)\}\}")


@dataclass
class GateDef:
    name: str
    title: str
    cmd: str
    timeout_sec: int = 600
    default: bool = True
    required: bool = False
    option: str | None = None      # オプションが持ち込んだゲートなら、その id
    after: str | None = None

    def as_raw(self) -> dict:
        return {"name": self.name, "cmd": self.cmd, "timeout_sec": self.timeout_sec}


@dataclass
class Option:
    id: str
    title: str
    summary: str
    raw: dict
    requires: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass
class Stack:
    name: str
    title: str
    tech: str
    summary: str
    order: int
    requires: list[str]
    notes: str
    raw: dict
    options: dict[str, Option]

    def gates(self, options: list[str] | None = None) -> list[GateDef]:
        """この型の全ゲート（選んだオプションが持ち込むものを含む）を、実行順に。"""
        out = [_gate(g) for g in self.raw.get("gates", [])]
        for oid in options or []:
            for g in self.options[oid].raw.get("gates", []):
                gd = _gate(g, oid)
                names = [x.name for x in out]
                if gd.after in names:
                    out.insert(names.index(gd.after) + 1, gd)
                else:
                    out.append(gd)
        return out

    def default_gates(self, options: list[str] | None = None) -> list[str]:
        return [g.name for g in self.gates(options) if g.default or g.required]

    def to_public(self) -> dict:
        def shown(cmd: str) -> str:   # 画面に出す形（まだ決まっていない名前やポートは仮の文字で）
            return substitute(cmd, {"pkg": "<名前>", "name": "<名前>", "python": "python3",
                                    "hootl": "hootl-sdd", "port": "<ポート>"}).replace('"python3" "hootl-sdd/hootl/boot.py"', "boot.py")
        return {
            "name": self.name, "title": self.title, "tech": self.tech, "summary": self.summary,
            "requires": self.requires,
            "options": [{"id": o.id, "title": o.title, "summary": o.summary, "conflicts": o.conflicts,
                         "gates": [g.name for g in self.gates([o.id]) if g.option == o.id]}
                        for o in self.options.values()],
            "gates": [{"name": g.name, "title": g.title, "default": g.default, "required": g.required,
                       "option": g.option, "cmd": shown(g.cmd)}
                      for g in self.gates(list(self.options))],
        }


def _gate(g: dict, option: str | None = None) -> GateDef:
    return GateDef(name=g["name"], title=g.get("title", g["name"]), cmd=g["cmd"],
                   timeout_sec=int(g.get("timeout_sec", 600)), default=bool(g.get("default", True)),
                   required=bool(g.get("required", False)), option=option, after=g.get("after"))


def _read(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_stack(name: str, stacks_dir: Path | None = None) -> Stack:
    stacks_dir = stacks_dir or default_dir()
    if name.startswith("_") or not re.fullmatch(r"[\w-]+", name):
        raise ValueError(f"型の名前が不正です: {name}")
    path = stacks_dir / name / "stack.toml"
    if not path.exists():
        raise ValueError(f"型 {name} はカタログにありません（{stacks_dir} を見てください）")
    raw = _read(path)
    options = {}
    for o in raw.get("options", []):
        options[o["id"]] = Option(o["id"], o.get("title", o["id"]), o.get("summary", ""), o,
                                  list(o.get("requires", [])), list(o.get("conflicts", [])))
    for o in options.values():   # 「一緒に使えない」は両向きにする
        for other in o.conflicts:
            if other in options and o.id not in options[other].conflicts:
                options[other].conflicts.append(o.id)
    return Stack(name=name, title=raw.get("title", name), tech=raw.get("tech", ""),
                 summary=raw.get("summary", ""), order=int(raw.get("order", 99)),
                 requires=list(raw.get("requires", [])), notes=raw.get("notes", ""),
                 raw=raw, options=options)


def catalog(stacks_dir: Path | None = None) -> list[Stack]:
    stacks_dir = stacks_dir or default_dir()
    out = [load_stack(p.parent.name, stacks_dir) for p in sorted(stacks_dir.glob("*/stack.toml"))
           if not p.parent.name.startswith("_")]
    return sorted(out, key=lambda s: (s.order, s.name))


def check_selection(stack: Stack, options: list[str], gates: list[str] | None) -> list[str]:
    """選んだオプションとゲートが、この型で使えるか確かめる。使うゲートの名前（実行順・必須込み）を返す。"""
    for oid in options:
        if oid not in stack.options:
            raise ValueError(f"{stack.title} にはオプション {oid} がありません")
        for other in stack.options[oid].conflicts:
            if other in options:
                raise ValueError(f"{stack.options[oid].title} と {stack.options[other].title} は一緒に使えません")
    all_gates = stack.gates(options)
    names = {g.name for g in all_gates}
    chosen = set(stack.default_gates(options) if gates is None else gates)
    unknown = chosen - names
    if unknown:
        raise ValueError(f"このアプリでは使えないゲートです: {', '.join(sorted(unknown))}")
    return [g.name for g in all_gates if g.name in chosen or g.required]


def substitute(value, variables: dict[str, str]):
    """{{name}} を埋める。辞書・配列の中も。知らない変数はそのまま残す（後で気づけるように）。"""
    if isinstance(value, str):
        return VAR_RE.sub(lambda m: str(variables.get(m.group(1), m.group(0))), value)
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    return value


def config_dir() -> Path:
    """手元の設定の置き場所（管理画面のトークン・権限設定）。HOOTL_CONFIG_DIR で変えられる。"""
    return Path(os.environ.get("HOOTL_CONFIG_DIR") or "~/.config/hootl").expanduser()


def _rule_paths(path: Path) -> list[str]:
    """Claude Code の権限ルールでのパスの書き方（ホームの下は ~/…、それ以外は //… の絶対パス）。

    リンクを辿る前と後で場所が違えば、両方を返す（どちらの書き方で触られても止まるように）。
    """
    out = []
    home = Path.home()
    for p, h in ((path.expanduser().absolute(), home.absolute()), (path.expanduser().resolve(), home.resolve())):
        try:
            rule = "~/" + str(p.relative_to(h))
        except ValueError:
            rule = "//" + str(p).lstrip("/")
        if rule not in out:
            out.append(rule)
    return out


def required_denies() -> list[str]:
    """どの権限設定にも必ず足す拒否。判定する側（このハーネス）と、権限設定そのものを、エージェントに触らせない。

    - ハーネスの編集: ゲートやレビューの仕組みを書き換えれば、何でも「合格」にできてしまう
    - 設定フォルダの編集: 権限設定を書き換えれば、次のタスクから拒否が外れる
    - 設定フォルダの読み取り: 管理画面のトークンが入っている
    """
    rules = [f"Edit({p}/**)" for p in _rule_paths(ROOT)]
    for p in _rule_paths(config_dir()):
        rules += [f"Edit({p}/**)", f"Read({p}/**)"]
    return rules


def claude_settings_path() -> Path:
    """無人エージェントに渡す権限設定のファイル。毎回組み立てて、設定フォルダに置いたものを渡す。

    元にするのは、手元の版（<設定フォルダ>/claude-settings.json。自分の環境で守りたい場所を足したもの）があれば
    それ、なければ同梱の claude-settings.hootl.json。どちらを元にしても required_denies() を必ず足すので、
    手元の版が古くても・書き換えられても、ハーネスと権限設定への拒否は外れない。
    """
    shipped = ROOT / "stacks" / COMMON / "claude-settings.hootl.json"
    base = shipped
    for candidate in (os.environ.get("HOOTL_CLAUDE_SETTINGS"), config_dir() / "claude-settings.json"):
        if candidate and Path(candidate).expanduser().is_file():
            base = Path(candidate).expanduser()
            break
    try:
        data = json.loads(base.read_text(encoding="utf-8"))
        deny = data.setdefault("permissions", {}).setdefault("deny", [])
        if not isinstance(deny, list):
            raise ValueError("permissions.deny が配列ではありません")
    except (OSError, ValueError, AttributeError) as exc:
        # 読めない設定のまま、守りなしで回さない
        raise ValueError(f"無人エージェントの権限設定を読めません: {base}: {exc}") from exc
    deny += [r for r in required_denies() if r not in deny]
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    out = config_dir() / "claude-settings.rendered.json"
    try:
        if not out.is_file() or out.read_text(encoding="utf-8") != text:
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, out)
    except OSError as exc:
        raise ValueError(f"無人エージェントの権限設定を書けません: {out}: {exc}") from exc
    return out


def variables_for(stack_section: dict, repo: Path | None = None) -> dict[str, str]:
    name = stack_section.get("app") or (repo.name if repo else "app")
    v = {"hootl": str(ROOT), "name": name, "pkg": name.replace("-", "_"),
         "title": stack_section.get("title", name), "python": "python3",
         "claude_settings": str(claude_settings_path())}
    v.update({k: str(x) for k, x in stack_section.get("vars", {}).items()})
    return v


def _merge_list(a: list, b: list) -> list:
    return a + [x for x in b if x not in a]


def compose(stack_section: dict, repo: Path | None = None, stacks_dir: Path | None = None) -> dict:
    """[stack] から hootl.toml 相当の辞書を作る（共通 → 型 → オプション の順に重ねる）。"""
    stacks_dir = stacks_dir or default_dir()
    stack = load_stack(stack_section["name"], stacks_dir)
    options = list(stack_section.get("options", []))
    gate_names = check_selection(stack, options, stack_section.get("gates"))
    base = copy.deepcopy(_read(stacks_dir / COMMON / "base.toml"))

    layers = [stack.raw] + [stack.options[o].raw for o in options]
    setup: list[dict] = []
    guard = base.setdefault("guard", {})
    env = base.setdefault("env", {})
    for layer in layers:
        setup += [dict(s) for s in layer.get("setup", [])]
        for k, v in layer.get("guard", {}).items():
            guard[k] = _merge_list(guard.get(k, []), v) if isinstance(v, list) else v
        env.update(layer.get("env", {}))
        for k, v in layer.get("run", {}).items():
            base.setdefault("run", {})[k] = v
    base["setup"] = setup
    by_name = {g.name: g for g in stack.gates(options)}
    base["gates"] = [by_name[n].as_raw() for n in gate_names]
    return substitute(base, variables_for(stack_section, repo))


def merge_config(composed: dict, raw: dict) -> dict:
    """hootl.toml に書いたものを、組み立てた設定の上に重ねる。[[gates]]・[[setup]] は後ろに足す（独自のゲート）。"""
    out = copy.deepcopy(composed)
    for k, v in raw.items():
        if k == "stack":
            continue
        if k in ("gates", "setup"):
            out[k] = out.get(k, []) + list(v)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            if k == "guard":
                for gk, gv in v.items():
                    out[k][gk] = _merge_list(out[k].get(gk, []), gv) if isinstance(gv, list) else gv
            elif k == "agents":
                out[k].update(v)          # 役ごとに丸ごと置き換え
            else:
                out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


# ---- hootl.toml の [stack].gates を書き換える（ゲートの切り替え）----------------
GATES_LINE = re.compile(r"^gates\s*=\s*\[[^\]]*\]\s*$", re.M)


def render_gates_line(names: list[str]) -> str:
    return "gates = [" + ", ".join(f'"{n}"' for n in names) + "]"


def set_gates(hootl_toml: Path, names: list[str]) -> list[str]:
    """使うゲートを書き換える。カタログで確かめてから書く。書いたゲート名（実行順）を返す。"""
    text = hootl_toml.read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    sec = raw.get("stack")
    if not sec:
        raise ValueError("このプロジェクトはカタログから作られていないので、ゲートは hootl.toml を直接直してください")
    stack = load_stack(sec["name"])
    chosen = check_selection(stack, list(sec.get("options", [])), names)
    if not GATES_LINE.search(text):
        raise ValueError("hootl.toml の [stack] に gates = [...] の行が見つかりません")
    new = GATES_LINE.sub(render_gates_line(chosen), text, count=1)
    tomllib.loads(new)  # 壊していないこと
    hootl_toml.write_text(new, encoding="utf-8")
    return chosen


def gate_status(raw: dict) -> dict | None:
    """設定画面用: このアプリの型・オプションと、ゲートごとの使う／使わない。"""
    sec = raw.get("stack")
    if not sec:
        return None
    stack = load_stack(sec["name"])
    options = list(sec.get("options", []))
    on = set(check_selection(stack, options, sec.get("gates")))
    return {
        "stack": {"name": stack.name, "title": stack.title, "tech": stack.tech},
        "options": [{"id": o, "title": stack.options[o].title} for o in options],
        "gates": [{"name": g.name, "title": g.title, "cmd": substitute(g.cmd, variables_for(sec)), "required": g.required,
                   "on": g.name in on, "option": g.option} for g in stack.gates(options)],
        "custom": [{"name": g["name"], "cmd": g["cmd"]} for g in raw.get("gates", [])],
        "vars": sec.get("vars", {}),
        # この型（とオプション）が実際に使うポートだけ
        "ports": [{"label": PORT_LABELS.get(k, k), "port": sec.get("vars", {}).get(k)}
                  for layer in [stack.raw] + [stack.options[o].raw for o in options]
                  for k in layer.get("ports", []) if sec.get("vars", {}).get(k)],
    }
