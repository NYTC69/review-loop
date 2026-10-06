#!/usr/bin/env python3
"""Lexer and parser for the shell subset of the M7 D-b1 scanner v2 (paired_session/docs/m7-scanner-v2.md §3.2).

A word is a list of parts that keep their quoting: Lit(text, quoted), Var(name, quoted), Sub(tree, quoted) for $(…) and
backticks, and Bad(reason) for a construct outside the subset (the scanner fails closed on it). Heredoc bodies are read
at the newline that follows their operator. The parser builds:
  ("list", [(and_or, separator), …])        separator: ";", "&", "\n" or None
  ("andor", pipeline, [(op, pipeline), …])   op: "&&" or "||"
  ("pipe", [command, …])
  ("simple", assigns, words, redirs)        assigns: [(name, word)]; redirs: [Redir]
  ("subshell" | "group", list, redirs)
  ("if", [(condition list, body list), …], else list or None, redirs)
  ("for", name, words, body list, redirs)
  ("loop", condition list, body list, redirs)   while / until
  ("case", subject word, [branch list, …], redirs)
  ("bad", reason)
Anything outside the subset (functions, here-strings, $'…' escapes, ~user) becomes a "bad" node or a Bad part;
the parser itself never raises on input text."""
import re
from collections import namedtuple

Lit = namedtuple("Lit", "text quoted")
Var = namedtuple("Var", "name quoted")   # name "#arith" is $((…)); "#opaque" is a ${…} the subset does not model
Param = namedtuple("Param", "name default quoted")   # ${NAME:-word} and its - := = variants: the variable or the word
Sub = namedtuple("Sub", "tree quoted")
Bad = namedtuple("Bad", "reason")
Redir = namedtuple("Redir", "op fd target body expanding")   # body: heredoc text or None

RESERVED = {"if", "then", "elif", "else", "fi", "for", "in", "do", "done", "{", "}", "!", "while", "until", "case",
            "esac", "function", "select", "time", "coproc"}
UNSUPPORTED = {"function", "select", "coproc"}
REDIR_RE = re.compile(r"(\d*)(<<<|<<-|<<|>>|>&|<&|&>>|&>|>\||<>|>|<)")
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SPECIAL = "$?!#@*-0123456789"


class Lexer:
    def __init__(self, src):
        self.src, self.pos, self.pending = src, 0, []

    # --- words -----------------------------------------------------------------------------------------------------
    def dollar(self, quoted):
        s, i = self.src, self.pos
        nxt = s[i + 1:i + 2]
        if nxt == "'" and not quoted:   # $'…': literal only without backslash escapes
            end = s.find("'", i + 2)
            if end < 0:
                self.pos = len(s); return Bad("unterminated $'")
            text = s[i + 2:end]; self.pos = end + 1
            return Bad("$'...' escape") if "\\" in text else Lit(text, True)
        if s.startswith("$((", i):   # arithmetic: a number, so a segment (census-driven, design §2)
            self.pos = self.skip_parens(i + 1); return Var("#arith", quoted)
        if nxt == "(":
            sub = Lexer(s); sub.pos = i + 2
            tokens = sub.tokens(stop_paren=True)
            self.pos = sub.pos
            return Sub(parse(tokens), quoted)
        if nxt == "{":
            depth, end = 0, None
            for j in range(i + 1, len(s)):
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                    if depth == 0:
                        end = j; break
            if end is None:
                self.pos = len(s); return Bad("unterminated ${")
            body = s[i + 2:end]; self.pos = end + 1
            if NAME_RE.fullmatch(body):
                return Var(body, quoted)
            default = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):?[-=](.*)", body, re.S)
            if default:   # ${NAME:-word}: the variable's value or the word's
                return Param(default.group(1), body_parts(default.group(2)), quoted)
            return Var("#opaque", quoted)   # ${#x}, ${x%y}, ${PIPESTATUS[0]}, …: an opaque value
        name = NAME_RE.match(s, i + 1)
        if name:
            self.pos = name.end(); return Var(name.group(0), quoted)
        if nxt and nxt in SPECIAL:
            self.pos = i + 2; return Var(nxt, quoted)
        self.pos = i + 1
        return Lit("$", quoted)

    def skip_parens(self, i):   # past a balanced (…) starting at i; quotes are not special here (it is a Bad part)
        depth = 0
        while i < len(self.src):
            depth += {"(": 1, ")": -1}.get(self.src[i], 0)
            i += 1
            if depth == 0:
                break
        return i

    def backtick(self, quoted):
        s, i, out = self.src, self.pos + 1, []
        while i < len(s) and s[i] != "`":
            if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in "$`\\":
                out.append(s[i + 1]); i += 2
            else:
                out.append(s[i]); i += 1
        self.pos = i + 1
        if i >= len(s):
            return Bad("unterminated backtick")
        return Sub(parse(Lexer("".join(out)).tokens()), quoted)

    def double_quoted(self, parts, heredoc=False):
        """Inside "…" (or an expanding heredoc body when heredoc=True, which runs to the end of the text)."""
        s = self.src
        while self.pos < len(s):
            c = s[self.pos]
            if c == '"' and not heredoc:
                self.pos += 1; return
            if c == "\\" and self.pos + 1 < len(s) and s[self.pos + 1] in ('$`\\\n' + ("" if heredoc else '"')):
                if s[self.pos + 1] != "\n":
                    parts.append(Lit(s[self.pos + 1], True))
                self.pos += 2
            elif c == "$":
                parts.append(self.dollar(True))
            elif c == "`":
                parts.append(self.backtick(True))
            else:
                parts.append(Lit(c, True)); self.pos += 1
        if not heredoc:
            parts.append(Bad("unterminated double quote"))

    def word(self):
        s, parts = self.src, []
        while self.pos < len(s):
            c = s[self.pos]
            if c in " \t\n;&|()<>":
                break
            if c == "\\":
                if s[self.pos + 1:self.pos + 2] == "\n":
                    self.pos += 2; continue
                parts.append(Lit(s[self.pos + 1:self.pos + 2], True)); self.pos += 2
            elif c == "'":
                end = s.find("'", self.pos + 1)
                if end < 0:
                    parts.append(Bad("unterminated single quote")); self.pos = len(s); break
                parts.append(Lit(s[self.pos + 1:end], True)); self.pos = end + 1
            elif c == '"':
                self.pos += 1; self.double_quoted(parts)
            elif c == "$":
                parts.append(self.dollar(False))
            elif c == "`":
                parts.append(self.backtick(False))
            else:
                parts.append(Lit(c, False)); self.pos += 1
        return merge(parts)

    # --- tokens ----------------------------------------------------------------------------------------------------
    def heredocs(self):
        """At a newline: read the body of every pending heredoc, in order."""
        s = self.src
        for redir_index, tokens, delim, strip in self.pending:
            lines, found = [], False
            while self.pos < len(s):
                end = s.find("\n", self.pos)
                line = s[self.pos:end if end >= 0 else len(s)]
                self.pos = end + 1 if end >= 0 else len(s)
                check = line.lstrip("\t") if strip else line
                if check == delim:
                    found = True; break
                lines.append(check)
            old = tokens[redir_index]
            body = "\n".join(lines) + ("\n" if lines else "")
            tokens[redir_index] = ("redir", old[1]._replace(body=body if found else None))
        self.pending = []

    def tokens(self, stop_paren=False):
        s, out, depth = self.src, [], 0
        while self.pos < len(s):
            c = s[self.pos]
            if c in " \t":
                self.pos += 1; continue
            if c == "#" and (not out or out[-1][0] == "op" or s[self.pos - 1] in " \t\n"):
                end = s.find("\n", self.pos); self.pos = end if end >= 0 else len(s); continue
            if c == "\n":
                self.pos += 1
                out.append(("op", "\n"))
                if self.pending:
                    self.heredocs()
                continue
            if c == ")" and stop_paren and depth == 0:
                self.pos += 1; return out
            two = s[self.pos:self.pos + 2]
            redir = REDIR_RE.match(s, self.pos)
            if redir and (redir.group(1) == "" or s[self.pos:redir.start(2)].isdigit()) and not two in ("&&",) \
                    and not (c == "&" and redir.group(2) not in ("&>", "&>>")):
                self.pos = redir.end()
                op, fd = redir.group(2), redir.group(1)
                while self.pos < len(s) and s[self.pos] in " \t":
                    self.pos += 1
                target = self.word()
                if op == "<<<":
                    out.append(("bad", "here-string")); continue
                if op in ("<<", "<<-"):
                    delim = "".join(p.text for p in target if isinstance(p, Lit))
                    expanding = all(isinstance(p, Lit) and not p.quoted for p in target)
                    out.append(("redir", Redir(op, fd, target, None, expanding)))
                    self.pending.append((len(out) - 1, out, delim, op == "<<-"))
                else:
                    out.append(("redir", Redir(op, fd, target, None, False)))
                continue
            if two in ("&&", "||", ";;"):
                out.append(("op", two)); self.pos += 2; continue
            if two == "|&":
                out.append(("op", "|")); self.pos += 2; continue
            if c in ";&|":
                out.append(("op", c)); self.pos += 1; continue
            if c == "(":
                depth += 1; out.append(("op", "(")); self.pos += 1; continue
            if c == ")":
                depth -= 1; out.append(("op", ")")); self.pos += 1; continue
            out.append(("word", self.word()))
        if self.pending:
            self.heredocs()
        if stop_paren:
            out.append(("bad", "unterminated command substitution"))
        return out


def merge(parts):
    out = []
    for part in parts:
        if out and isinstance(part, Lit) and isinstance(out[-1], Lit) and out[-1].quoted == part.quoted:
            out[-1] = Lit(out[-1].text + part.text, part.quoted)
        else:
            out.append(part)
    return out


def plain(word):
    """The text of a word made only of unquoted literals, else None (for reserved words and names)."""
    if len(word) == 1 and isinstance(word[0], Lit) and not word[0].quoted:
        return word[0].text
    return None


def body_parts(body):
    """An expanding heredoc body (or a ${NAME:-word} default) as word parts (double-quote rules, `"` literal)."""
    lexer = Lexer(body)
    parts = []
    lexer.double_quoted(parts, heredoc=True)
    return merge(parts)


# --- parser -------------------------------------------------------------------------------------------------------
class Parser:
    def __init__(self, tokens):
        self.t, self.i = tokens, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def word_is(self, *texts):
        tok = self.peek()
        return tok is not None and tok[0] == "word" and plain(tok[1]) in texts

    def skip_newlines(self):
        while self.peek() == ("op", "\n"):
            self.i += 1

    def parse_list(self, stops=()):
        items = []
        while True:
            self.skip_newlines()
            tok = self.peek()
            if tok is None or tok in (("op", ")"), ("op", ";;")) or (tok[0] == "word" and plain(tok[1]) in stops):
                return ("list", items)
            node = self.and_or(stops)
            sep = None
            tok = self.peek()
            if tok is not None and tok[0] == "op" and tok[1] in (";", "&", "\n"):
                sep = tok[1]; self.i += 1
            items.append((node, sep))
            if sep is None and not (self.peek() is None or self.peek() in (("op", ")"), ("op", ";;"))
                                    or (self.peek()[0] == "word" and plain(self.peek()[1]) in stops)):
                items.append((("bad", "unexpected token"), None)); self.i += 1

    def and_or(self, stops):
        first, rest = self.pipeline(stops), []
        while self.peek() in (("op", "&&"), ("op", "||")):
            op = self.peek()[1]; self.i += 1
            self.skip_newlines()
            rest.append((op, self.pipeline(stops)))
        return ("andor", first, rest)

    def pipeline(self, stops):
        while self.word_is("!", "time"):   # prefixes that change no path
            self.i += 1
        cmds = [self.command(stops)]
        while self.peek() == ("op", "|"):
            self.i += 1; self.skip_newlines()
            cmds.append(self.command(stops))
        return ("pipe", cmds)

    def redirs(self):
        out = []
        while self.peek() is not None and self.peek()[0] == "redir":
            out.append(self.peek()[1]); self.i += 1
        return out

    def expect(self, text):
        if self.word_is(text):
            self.i += 1; return True
        return False

    def command(self, stops):
        tok = self.peek()
        if tok is None:
            return ("bad", "missing command")
        if tok[0] == "bad":
            self.i += 1; return ("bad", tok[1])
        if tok == ("op", "("):
            self.i += 1
            body = self.parse_list()
            if self.peek() != ("op", ")"):
                return ("bad", "unclosed subshell")
            self.i += 1
            return ("subshell", body, self.redirs())
        head = plain(tok[1]) if tok[0] == "word" else None
        if head in UNSUPPORTED:
            self.skip_to_end(); return ("bad", head)
        if head == "{":
            self.i += 1
            body = self.parse_list(stops=("}",))
            if not self.expect("}"):
                return ("bad", "unclosed group")
            return ("group", body, self.redirs())
        if head == "if":
            self.i += 1
            branches, otherwise = [], None
            cond = self.parse_list(stops=("then",))
            if not self.expect("then"):
                return ("bad", "if without then")
            branches.append((cond, self.parse_list(stops=("elif", "else", "fi"))))
            while self.word_is("elif"):
                self.i += 1
                cond = self.parse_list(stops=("then",))
                if not self.expect("then"):
                    return ("bad", "elif without then")
                branches.append((cond, self.parse_list(stops=("elif", "else", "fi"))))
            if self.expect("else"):
                otherwise = self.parse_list(stops=("fi",))
            if not self.expect("fi"):
                return ("bad", "if without fi")
            return ("if", branches, otherwise, self.redirs())
        if head == "case":   # census-driven: every branch is checked, then the union (as if/else; design §2)
            self.i += 1
            subject = self.peek()
            if subject is None or subject[0] != "word":
                return ("bad", "case without a word")
            self.i += 1
            self.skip_newlines()
            if not self.expect("in"):
                return ("bad", "case without in")
            branches = []
            while True:
                self.skip_newlines()
                if self.expect("esac"):
                    return ("case", subject[1], branches, self.redirs())
                if self.peek() == ("op", "("):
                    self.i += 1
                while self.peek() is not None and (self.peek()[0] == "word" or self.peek() == ("op", "|")):
                    self.i += 1   # the patterns: matched against the subject, never paths
                if self.peek() != ("op", ")"):
                    return ("bad", "case pattern without )")
                self.i += 1
                branches.append(self.parse_list(stops=("esac",)))
                if self.peek() == ("op", ";;"):
                    self.i += 1
        if head in ("while", "until"):   # census-driven: the condition and the body are checked once (design §2)
            self.i += 1
            cond = self.parse_list(stops=("do",))
            if not self.expect("do"):
                return ("bad", head + " without do")
            body = self.parse_list(stops=("done",))
            if not self.expect("done"):
                return ("bad", head + " without done")
            return ("loop", cond, body, self.redirs())
        if head == "for":
            self.i += 1
            name_tok = self.peek()
            name = plain(name_tok[1]) if name_tok and name_tok[0] == "word" else None
            if not name or not NAME_RE.fullmatch(name):
                return ("bad", "for without a name")
            self.i += 1
            if not self.expect("in"):
                self.skip_to_end(); return ("bad", "for without a literal list")
            words = []
            while self.peek() is not None and self.peek()[0] == "word" and plain(self.peek()[1]) != "do":
                words.append(self.peek()[1]); self.i += 1
            if self.peek() is not None and self.peek()[0] == "op" and self.peek()[1] in (";", "\n"):
                self.i += 1
            self.skip_newlines()
            if not self.expect("do"):
                return ("bad", "for without do")
            body = self.parse_list(stops=("done",))
            if not self.expect("done"):
                return ("bad", "for without done")
            return ("for", name, words, body, self.redirs())
        assigns, words, redirs = [], [], []
        while True:
            tok = self.peek()
            if tok is None or tok[0] == "op" or (tok[0] == "word" and not words and not assigns
                                                and plain(tok[1]) in stops):
                break
            if tok[0] == "bad":
                self.i += 1; return ("bad", tok[1])
            if tok[0] == "redir":
                redirs.append(tok[1]); self.i += 1; continue
            word = tok[1]
            first = word[0] if word else None
            match = NAME_RE.match(first.text) if isinstance(first, Lit) and not first.quoted else None
            if not words and match and first.text[match.end():match.end() + 1] == "=":
                rest = first.text[match.end() + 1:]
                assigns.append((match.group(0), merge(([Lit(rest, False)] if rest else []) + list(word[1:]))))
            else:
                words.append(word)
            self.i += 1
        if words and self.peek() == ("op", "(") and len(words) == 1:   # name () { …; }: a function definition
            self.skip_to_end(); return ("bad", "function definition")
        return ("simple", assigns, words, redirs)

    def skip_to_end(self):
        self.i = len(self.t)


def parse(tokens):
    parser = Parser(tokens)
    tree = parser.parse_list()
    if parser.i < len(parser.t):
        tree[1].append((("bad", "unbalanced parenthesis"), None))
    return tree


def parse_command(text):
    return parse(Lexer(text).tokens())
