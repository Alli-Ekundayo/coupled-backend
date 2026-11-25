"""Tiny KiCad S-expression parser/writer. Quoted strings -> str, bare tokens -> Sym."""
import re


class Sym(str):
    pass


_TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse(text: str) -> list:
    stack: list[list] = [[]]
    for t in _TOK.findall(text):
        if t == "(":
            stack.append([])
        elif t == ")":
            if len(stack) < 2:
                raise ValueError("unbalanced )")
            n = stack.pop()
            stack[-1].append(n)
        elif t[0] == '"':
            stack[-1].append(t[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
        else:
            stack[-1].append(Sym(t))
    if len(stack) != 1 or not stack[0]:
        raise ValueError("unbalanced (")
    return stack[0][0]


def _atom(x) -> str:
    if isinstance(x, Sym):
        return str(x)
    if isinstance(x, str):
        return '"' + x.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(x, float):
        s = f"{x:.6f}".rstrip("0").rstrip(".")
        return "0" if s in ("", "-0") else s
    return str(x)


def dump(n, level: int = 0) -> str:
    if not isinstance(n, list):
        return _atom(n)
    k = next((i for i, c in enumerate(n) if isinstance(c, list)), len(n))
    hd, rest = n[:k], n[k:]
    if level < 2 and rest:
        pad = "  " * (level + 1)
        inner = "\n".join(pad + dump(c, level + 1) for c in rest)
        return "(" + " ".join(_atom(h) for h in hd) + "\n" + inner + "\n" + "  " * level + ")"
    return "(" + " ".join(dump(c, level + 1) for c in n) + ")"


def head(n) -> str:
    return str(n[0]) if isinstance(n, list) and n and isinstance(n[0], Sym) else ""


def find(n: list, name: str):
    return next((c for c in n if isinstance(c, list) and head(c) == name), None)


def find_all(n: list, name: str):
    return [c for c in n if isinstance(c, list) and head(c) == name]


def nums(n: list) -> list[float]:
    out = []
    for c in n[1:]:
        try:
            out.append(float(c))
        except (TypeError, ValueError):
            pass
    return out
