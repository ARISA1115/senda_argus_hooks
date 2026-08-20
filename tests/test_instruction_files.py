"""永続する指示ファイルの書き込みと、指示本文の突合可能な表現の検証。

伝播の検知は内容を読まずに行う。書かれた本文と、後から指示に現れた本文が同じであることだけを
ダイジェストの一致で見る。書き込み側と指示側で正規化の規則が違うと同じ行が違うダイジェストに
なり突合が成立しないため、規則を 1 箇所に置いていることを固定する。
"""

from __future__ import annotations

from senda_argus_hooks.core.instruction_files import (
    MAX_LINE_DIGESTS,
    collect_instruction_sources,
    MIN_LINE_LENGTH,
    classify_instruction_write,
    instruction_file_name,
    line_digests,
    system_prompt_line_digests,
)

_LONG = "この行は突合の対象になるだけの十分な長さを持っている行です"
_LONG2 = "こちらも同様に十分な長さを備えたもう一つの行として扱われます"


def test_known_instruction_file_is_recognized_by_basename() -> None:
    assert instruction_file_name("/repo/SOUL.md") == "SOUL.md"
    assert instruction_file_name("C:\\work\\MEMORY.md") == "MEMORY.md"
    assert instruction_file_name("/repo/.cursorrules") == ".cursorrules"


def test_nested_known_path_is_recognized_by_suffix() -> None:
    """区切りを含む名前は末尾の一致で認める。基底名だけでは取りこぼすため。"""
    assert (
        instruction_file_name("/repo/.github/copilot-instructions.md")
        == ".github/copilot-instructions.md"
    )


def test_unrelated_file_is_not_recognized() -> None:
    assert instruction_file_name("/repo/notes.md") is None
    assert instruction_file_name("") is None
    assert instruction_file_name(None) is None


def test_write_to_instruction_file_yields_digests_without_body() -> None:
    """本文そのものは返さず、突合できる形だけを返す。"""
    result = classify_instruction_write({"path": "/repo/SOUL.md", "content": _LONG})
    assert result is not None
    assert result["instruction_file_name"] == "SOUL.md"
    assert result["written_content_hash"].startswith("sha256:")
    assert result["written_line_hashes"]
    assert _LONG not in str(result)


def test_call_without_body_argument_is_not_classified() -> None:
    """本文にあたる引数を持たない呼び出しは分類しない。

    読み取り系のツールは本文を引数に取らない。判別はパスと本文の組で行い、ツール名では行わない。
    名前は提供元ごとに自由で、部分一致にすると無関係な名前まで拾い、逆に取りこぼしも増える。
    """
    assert classify_instruction_write({"path": "/repo/SOUL.md"}) is None
    assert classify_instruction_write({"path": "/repo/SOUL.md", "content": ""}) is None
    assert classify_instruction_write({"path": "/repo/SOUL.md", "content": 123}) is None


def test_write_to_unrelated_path_is_not_classified() -> None:
    assert classify_instruction_write({"path": "/repo/notes.md", "content": _LONG}) is None


def test_body_key_variants_are_accepted() -> None:
    """提供元ごとに本文の引数名が異なるため、代表的な名前を受ける。"""
    for key in ("content", "text", "new_string", "contents"):
        assert classify_instruction_write({"file_path": "/x/AGENTS.md", key: _LONG})


def test_written_and_system_side_use_the_same_rule() -> None:
    """書き込み側と指示側で同じ行が同じダイジェストになる。

    連結されても行は保たれるため、集合の重なりとして現れる。
    """
    written = classify_instruction_write({"path": "/r/SOUL.md", "content": _LONG})
    prompt = system_prompt_line_digests(
        messages=[{"role": "system", "content": f"前置きの文言\n{_LONG}\n後置きの文言"}]
    )
    assert set(written["written_line_hashes"]) & set(prompt)


def test_system_kwarg_shape_is_supported() -> None:
    """指示が独立した引数で渡される形も、種別つきの塊の並びも受ける。"""
    from_str = system_prompt_line_digests(system=_LONG)
    from_blocks = system_prompt_line_digests(system=[{"type": "text", "text": _LONG}])
    assert from_str and from_str == from_blocks


def test_short_lines_are_excluded() -> None:
    """短い行は無関係な文書どうしでも一致するため、証拠にしない。"""
    assert line_digests("短い\nもっと短い") == []
    assert len("あ" * MIN_LINE_LENGTH) >= MIN_LINE_LENGTH
    assert line_digests("あ" * MIN_LINE_LENGTH)


def test_repeated_lines_are_collapsed() -> None:
    assert len(line_digests("\n".join([_LONG, _LONG, _LONG2]))) == 2


def test_line_digest_count_is_bounded() -> None:
    """送出量と受け取り側の保持量に上限を置く。"""
    body = "\n".join(f"{_LONG}{i}" for i in range(MAX_LINE_DIGESTS * 3))
    assert len(line_digests(body)) == MAX_LINE_DIGESTS


def test_malformed_input_does_not_raise() -> None:
    """観測の後処理が本来の呼び出しを壊さない。"""
    assert line_digests(None) == []
    assert line_digests(123) == []
    assert classify_instruction_write(None) is None
    assert system_prompt_line_digests(messages="not-a-list") == []
    assert system_prompt_line_digests() == []


def test_every_llm_emitter_attaches_system_prompt_digests() -> None:
    """推論要求を出す全てのモジュールが、指示の行ダイジェストを載せる。

    計装の層だけでなく統合の層も対象にする。片方の層だけ対応すると、その層を通るエージェント
    だけ伝播が見えなくなる。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src/senda_argus_hooks"
    def emits_llm_request(path) -> bool:
        """推論要求を実際に送出しているモジュールかどうかを、送出の呼び出しから判定する。

        説明文や分岐で名前に触れているだけのモジュールを対象にすると、載せる先が無いのに
        検査が失敗する。構文木で送出の呼び出しを辿り、その引数に名前が現れるものだけを採る。
        """
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
            if name != "emit_event":
                continue
            for node in ast.walk(call):
                if isinstance(node, ast.Constant) and node.value == "llm.request":
                    return True
        return False

    emitting = [p for p in sorted(root.rglob("*.py")) if emits_llm_request(p)]
    assert emitting, "推論要求を出すモジュールが見つからない"
    missing = [
        str(p.relative_to(root))
        for p in emitting
        if "system_prompt_line_hashes" not in p.read_text(encoding="utf-8")
    ]
    assert not missing, f"指示の行ダイジェストを載せていないモジュールがある: {missing}"


def test_extraction_yields_values_for_each_provider_call_shape() -> None:
    """提供元ごとの実際の呼び出し形で、値が空にならないことを固定する。

    「載せていること」だけを見るテストは、取り出し元を取り違えても緑のまま通る。実際に 4 系統で
    常に空を返す状態が機械レビューまで残った。ここでは各系統が実際に使う渡し方を再現し、
    結果が空でないことを確かめる。
    """

    class _ModelHolder:
        _system_instruction = _LONG

    shapes = {
        "役割つきの列": collect_instruction_sources({"messages": [{"role": "system", "content": _LONG}]}),
        "独立した引数": collect_instruction_sources({"system": _LONG}),
        "応答系の指示": collect_instruction_sources({"instructions": _LONG}),
        "応答系の入力": collect_instruction_sources(
            {"input": [{"role": "system", "content": [{"type": "input_text", "text": _LONG}]}]}
        ),
        "位置引数": collect_instruction_sources(None, ([{"role": "system", "content": _LONG}],)),
        "保持元の属性": collect_instruction_sources(None, None, _ModelHolder()),
        "種別つきの塊": collect_instruction_sources({"system": [{"type": "text", "text": _LONG}]}),
    }
    empty = [name for name, sources in shapes.items() if not system_prompt_line_digests(*sources)]
    assert not empty, f"値が空になる渡し方がある: {empty}"


def test_non_instruction_roles_do_not_produce_digests() -> None:
    """指示でない役割は取り出さない。利用者の入力を指示として扱わない。"""
    sources = collect_instruction_sources({"messages": [{"role": "user", "content": _LONG}]})
    assert system_prompt_line_digests(*sources) == []


def test_patch_body_matches_the_resulting_instruction_line() -> None:
    """差分で書かれた場合も、適用後に指示へ現れる行と突合できる。

    行の先頭に付く記号を落とさないと、指示側に載る記号の無い行と一致しない。
    """
    patch = "\n".join([
        "--- a/AGENTS.md",
        "+++ b/AGENTS.md",
        "@@ -1,2 +1,3 @@",
        " 既存の行はそのまま残ります十分な長さです",
        f"+{_LONG}",
        "-削除される行はここに書かれています十分長い",
    ])
    written = classify_instruction_write({"path": "/r/AGENTS.md", "patch": patch})
    assert written is not None
    prompt = system_prompt_line_digests(
        *collect_instruction_sources({"messages": [{"role": "system", "content": f"前置き\n{_LONG}"}]})
    )
    assert set(written["written_line_hashes"]) & set(prompt)


def test_removed_patch_lines_are_not_recorded() -> None:
    """削除された行は適用後に残らないため、書き込みとして控えない。"""
    patch = "\n".join(["@@ -1 +1 @@", f"-{_LONG}"])
    written = classify_instruction_write({"path": "/r/AGENTS.md", "patch": patch})
    assert written is None or not written["written_line_hashes"]


def test_instrumentors_do_not_read_arguments_from_a_missing_name() -> None:
    """取り出し元の名前が、その場所から見える名前であることを確かめる。

    存在しない名前から取り出すと実行時に落ちるか、握りつぶされて常に空になる。空でも検知は
    静かに成立しなくなるだけで、テストがなければ気付けない。実際にこの形の誤りが 1 系統で
    起きており、呼び出し規約が他と違う経路で取り出し元を取り違えていた。

    入れ子の関数からは外側の名前も見えるため、呼び出し位置から親をたどって可視名を集める。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src/senda_argus_hooks/instrumentors"
    problems: list[str] = []

    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node

        def visible_from(node: ast.AST) -> set[str]:
            names: set[str] = set()
            cur: ast.AST | None = node
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    a = cur.args
                    for group in (a.posonlyargs, a.args, a.kwonlyargs):
                        names |= {x.arg for x in group}
                    if a.vararg:
                        names.add(a.vararg.arg)
                    if a.kwarg:
                        names.add(a.kwarg.arg)
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                    for stmt in ast.walk(cur):
                        if isinstance(stmt, ast.Assign):
                            names |= {t.id for t in stmt.targets if isinstance(t, ast.Name)}
                cur = parent.get(cur)
            return names

        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            if not (isinstance(call.func, ast.Name) and call.func.id == "system_prompt_line_digests"):
                continue
            scope = visible_from(call)
            for kw in call.keywords:
                src = kw.value
                if isinstance(src, ast.Call) and isinstance(src.func, ast.Attribute):
                    base = src.func.value
                    if isinstance(base, ast.Name) and base.id not in scope:
                        problems.append(f"{path.name} は {base.id} を参照できない")

    assert not problems, sorted(set(problems))
