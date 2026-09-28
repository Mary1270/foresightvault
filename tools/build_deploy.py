"""
Build contract_deploy.py from contract.py by removing comments and
docstrings only.

Why: GenLayer Studio's schema step has been observed to fail with a
generic `invalid_contract` error on contracts carrying a large volume of
comments/docstrings. contract.py stays the documented source of truth;
contract_deploy.py is what gets deployed.

Safety: this never uses regex on code. Docstrings are located with the
`ast` module (only true docstrings: the first statement of a module,
class or function body), comments with the `tokenize` module, and the
result is verified to have an AST identical to the original's with
docstrings removed - so string literals used as values (such as the LLM
prompt f-string) can never be altered. The runner header on line 1 is
kept verbatim.

Usage:
    python3 tools/build_deploy.py          # write contract_deploy.py
    python3 tools/build_deploy.py --check  # fail if it is out of date
"""
import ast
import io
import os
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "contract.py")
TARGET = os.path.join(ROOT, "contract_deploy.py")


def docstring_lines(tree):
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                if len(body) == 1:
                    raise SystemExit(f"Refusing to strip the only statement of {getattr(node, 'name', 'module')}")
                lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return lines


def strip(source: str) -> str:
    tree = ast.parse(source)
    drop = docstring_lines(tree)
    lines = source.splitlines()

    # Column at which each line's comment starts (tokenize knows real
    # comments from '#' characters inside strings).
    comment_col = {}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            comment_col[tok.start[0]] = tok.start[1]

    out = []
    for number, line in enumerate(lines, start=1):
        if number == 1:
            out.append(line)  # runner header, kept verbatim
            continue
        if number in drop:
            continue
        if number in comment_col:
            line = line[: comment_col[number]].rstrip()
            if not line.strip():
                continue
        out.append(line.rstrip())

    # Collapse runs of blank lines.
    compact = []
    for line in out:
        if line == "" and compact and compact[-1] == "":
            continue
        compact.append(line)
    return "\n".join(compact).rstrip() + "\n"


def without_docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:]
    return ast.dump(tree)


def build() -> str:
    source = open(SOURCE, encoding="utf-8").read()
    stripped = strip(source)
    if without_docstrings(ast.parse(source)) != ast.dump(ast.parse(stripped)):
        raise SystemExit("Stripped build is NOT structurally identical to contract.py - aborting.")
    if stripped.splitlines()[0] != source.splitlines()[0]:
        raise SystemExit("Runner header line was not preserved - aborting.")
    return stripped


def main():
    stripped = build()
    if "--check" in sys.argv:
        current = open(TARGET, encoding="utf-8").read() if os.path.exists(TARGET) else ""
        if current != stripped:
            raise SystemExit("contract_deploy.py is out of date: run python3 tools/build_deploy.py")
        print("contract_deploy.py is up to date and structurally identical to contract.py")
        return
    open(TARGET, "w", encoding="utf-8").write(stripped)
    print(f"Wrote {os.path.relpath(TARGET, ROOT)} ({len(stripped)} bytes, verified identical AST)")


if __name__ == "__main__":
    main()
