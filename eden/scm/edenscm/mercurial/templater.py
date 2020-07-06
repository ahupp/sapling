# Portions Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

# templater.py - template expansion for output
#
# Copyright 2005, 2006 Matt Mackall <mpm@selenic.com>
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

from __future__ import absolute_import, print_function

import os
import re
import types
from typing import Any, Iterable

from . import (
    color,
    config,
    encoding,
    error,
    hintutil,
    minirst,
    obsutil,
    parser,
    pycompat,
    registrar,
    revset as revsetmod,
    revsetlang,
    scmutil,
    templatefilters,
    templatekw,
    util,
)
from .i18n import _


# template parsing

elements = {
    # token-type: binding-strength, primary, prefix, infix, suffix
    "(": (20, None, ("group", 1, ")"), ("func", 1, ")"), None),
    ".": (18, None, None, (".", 18), None),
    "%": (15, None, None, ("%", 15), None),
    "|": (15, None, None, ("|", 15), None),
    "*": (5, None, None, ("*", 5), None),
    "/": (5, None, None, ("/", 5), None),
    "+": (4, None, None, ("+", 4), None),
    "-": (4, None, ("negate", 19), ("-", 4), None),
    "=": (3, None, None, ("keyvalue", 3), None),
    ",": (2, None, None, ("list", 2), None),
    ")": (0, None, None, None, None),
    "integer": (0, "integer", None, None, None),
    "symbol": (0, "symbol", None, None, None),
    "string": (0, "string", None, None, None),
    "template": (0, "template", None, None, None),
    "end": (0, None, None, None, None),
}


def tokenize(program, start, end, term=None):
    """Parse a template expression into a stream of tokens, which must end
    with term if specified"""
    pos = start
    program = pycompat.bytestr(program)
    while pos < end:
        c = program[pos]
        if c.isspace():  # skip inter-token whitespace
            pass
        elif c in "(=,).%|+-*/":  # handle simple operators
            yield (c, None, pos)
        elif c in "\"'":  # handle quoted templates
            s = pos + 1
            data, pos = _parsetemplate(program, s, end, c)
            yield ("template", data, s)
            pos -= 1
        elif c == "r" and program[pos : pos + 2] in ("r'", 'r"'):
            # handle quoted strings
            c = program[pos + 1]
            s = pos = pos + 2
            while pos < end:  # find closing quote
                d = program[pos]
                if d == "\\":  # skip over escaped characters
                    pos += 2
                    continue
                if d == c:
                    yield ("string", program[s:pos], s)
                    break
                pos += 1
            else:
                raise error.ParseError(_("unterminated string"), s)
        elif c.isdigit():
            s = pos
            while pos < end:
                d = program[pos]
                if not d.isdigit():
                    break
                pos += 1
            yield ("integer", program[s:pos], s)
            pos -= 1
        elif (
            c == "\\"
            and program[pos : pos + 2] in (r"\'", r"\"")
            or c == "r"
            and program[pos : pos + 3] in (r"r\'", r"r\"")
        ):
            # handle escaped quoted strings for compatibility with 2.9.2-3.4,
            # where some of nested templates were preprocessed as strings and
            # then compiled. therefore, \"...\" was allowed. (issue4733)
            #
            # processing flow of _evalifliteral() at 5ab28a2e9962:
            # outer template string    -> stringify()  -> compiletemplate()
            # ------------------------    ------------    ------------------
            # {f("\\\\ {g(\"\\\"\")}"}    \\ {g("\"")}    [r'\\', {g("\"")}]
            #             ~~~~~~~~
            #             escaped quoted string
            if c == "r":
                pos += 1
                token = "string"
            else:
                token = "template"
            quote = program[pos : pos + 2]
            s = pos = pos + 2
            while pos < end:  # find closing escaped quote
                if program.startswith("\\\\\\", pos, end):
                    pos += 4  # skip over double escaped characters
                    continue
                if program.startswith(quote, pos, end):
                    # interpret as if it were a part of an outer string
                    data = parser.unescapestr(program[s:pos])
                    if token == "template":
                        data = _parsetemplate(data, 0, len(data))[0]
                    yield (token, data, s)
                    pos += 1
                    break
                pos += 1
            else:
                raise error.ParseError(_("unterminated string"), s)
        elif c.isalnum() or c in "_":
            s = pos
            pos += 1
            while pos < end:  # find end of symbol
                d = program[pos]
                if not (d.isalnum() or d == "_"):
                    break
                pos += 1
            sym = program[s:pos]
            yield ("symbol", sym, s)
            pos -= 1
        elif c == term:
            yield ("end", None, pos + 1)
            return
        else:
            raise error.ParseError(_("syntax error"), pos)
        pos += 1
    if term:
        raise error.ParseError(_("unterminated template expansion"), start)
    yield ("end", None, pos)


def _parsetemplate(tmpl, start, stop, quote=""):
    r"""
    >>> _parsetemplate('foo{bar}"baz', 0, 12)
    ([('string', 'foo'), ('symbol', 'bar'), ('string', '"baz')], 12)
    >>> _parsetemplate('foo{bar}"baz', 0, 12, quote=b'"')
    ([('string', 'foo'), ('symbol', 'bar')], 9)
    >>> _parsetemplate('foo"{bar}', 0, 9, quote='"')
    ([('string', 'foo')], 4)
    >>> _parsetemplate(r'foo\"bar"baz', 0, 12, quote='"')
    ([('string', 'foo"'), ('string', 'bar')], 9)
    >>> _parsetemplate(r'foo\\"bar', 0, 10, quote='"')
    ([('string', 'foo\\')], 6)
    """
    parsed = []
    sepchars = "{" + quote
    pos = start
    p = parser.parser(elements)
    try:
        while pos < stop:
            n = min(
                (tmpl.find(c, pos, stop) for c in sepchars), key=lambda n: (n < 0, n)
            )
            if n < 0:
                parsed.append(("string", parser.unescapestr(tmpl[pos:stop])))
                pos = stop
                break
            c = tmpl[n : n + 1]
            bs = (n - pos) - len(tmpl[pos:n].rstrip("\\"))
            if bs % 2 == 1:
                # escaped (e.g. '\{', '\\\{', but not '\\{')
                parsed.append(("string", parser.unescapestr(tmpl[pos : n - 1]) + c))
                pos = n + 1
                continue
            if n > pos:
                parsed.append(("string", parser.unescapestr(tmpl[pos:n])))
            if c == quote:
                return parsed, n + 1

            parseres, pos = p.parse(tokenize(tmpl, n + 1, stop, "}"))
            parsed.append(parseres)

    except error.ParseError as inst:
        if len(inst.args) > 1:  # has location
            loc = inst.args[1]
            # Offset the caret location by the number of newlines before the
            # location of the error, since we will replace one-char newlines
            # with the two-char literal r'\n'.
            offset = tmpl[:loc].count("\n")
            tmpl = tmpl.replace("\n", r"\n")
            # We want the caret to point to the place in the template that
            # failed to parse, but in a hint we get a open paren at the
            # start. Therefore, we print "loc" spaces (instead of "loc - 1")
            # to line up the caret with the location of the error.
            inst.hint = tmpl + "\n" + " " * (loc + offset) + "^ " + _("here")
        raise

    if quote:
        raise error.ParseError(_("unterminated string"), start)
    return parsed, pos


def _unnesttemplatelist(tree):
    """Expand list of templates to node tuple

    >>> def f(tree):
    ...     print(prettyformat(_unnesttemplatelist(tree)))
    >>> f((b'template', []))
    (string '')
    >>> f((b'template', [(b'string', b'foo')]))
    (string 'foo')
    >>> f((b'template', [(b'string', b'foo'), (b'symbol', b'rev')]))
    (template
      (string 'foo')
      (symbol 'rev'))
    >>> f((b'template', [(b'symbol', b'rev')]))  # template(rev) -> str
    (template
      (symbol 'rev'))
    >>> f((b'template', [(b'template', [(b'string', b'foo')])]))
    (string 'foo')
    """
    if not isinstance(tree, tuple):
        return tree
    op = tree[0]
    if op != "template":
        return (op,) + tuple(_unnesttemplatelist(x) for x in tree[1:])

    assert len(tree) == 2
    xs = tuple(_unnesttemplatelist(x) for x in tree[1])
    if not xs:
        return ("string", "")  # empty template ""
    elif len(xs) == 1 and xs[0][0] == "string":
        return xs[0]  # fast path for string with no template fragment "x"
    else:
        return (op,) + xs


def parse(tmpl):
    """Parse template string into tree"""
    parsed, pos = _parsetemplate(tmpl, 0, len(tmpl))
    assert pos == len(tmpl), "unquoted template should be consumed"
    return _unnesttemplatelist(("template", parsed))


def _parseexpr(expr):
    """Parse a template expression into tree

    >>> _parseexpr(b'"foo"')
    ('string', 'foo')
    >>> _parseexpr(b'foo(bar)')
    ('func', ('symbol', 'foo'), ('symbol', 'bar'))
    >>> _parseexpr(b'foo(')
    Traceback (most recent call last):
      ...
    ParseError: ('not a prefix: end', 4)
    >>> _parseexpr(b'"foo" "bar"')
    Traceback (most recent call last):
      ...
    ParseError: ('invalid token', 7)
    """
    p = parser.parser(elements)
    tree, pos = p.parse(tokenize(expr, 0, len(expr)))
    if pos != len(expr):
        raise error.ParseError(_("invalid token"), pos)
    return _unnesttemplatelist(tree)


def prettyformat(tree):
    return parser.prettyformat(tree, ("integer", "string", "symbol"))


def compileexp(exp, context, curmethods):
    """Compile parsed template tree to (func, data) pair"""
    t = exp[0]
    if t in curmethods:
        return curmethods[t](exp, context)
    raise error.ParseError(_("unknown method '%s'") % t)


# template evaluation


def getsymbol(exp):
    if exp[0] == "symbol":
        return exp[1]
    raise error.ParseError(_("expected a symbol, got '%s'") % exp[0])


def getlist(x):
    if not x:
        return []
    if x[0] == "list":
        return getlist(x[1]) + [x[2]]
    return [x]


def gettemplate(exp, context):
    """Compile given template tree or load named template from map file;
    returns (func, data) pair"""
    if exp[0] in ("template", "string"):
        return compileexp(exp, context, methods)
    if exp[0] == "symbol":
        # unlike runsymbol(), here 'symbol' is always taken as template name
        # even if it exists in mapping. this allows us to override mapping
        # by web templates, e.g. 'changelogtag' is redefined in map file.
        return context._load(exp[1])
    raise error.ParseError(_("expected template specifier"))


def findsymbolicname(arg):
    """Find symbolic name for the given compiled expression; returns None
    if nothing found reliably"""
    while True:
        func, data = arg
        if func is runsymbol:
            return data
        elif func is runfilter:
            arg = data[0]
        else:
            return None


def evalrawexp(context, mapping, arg):
    """Evaluate given argument as a bare template object which may require
    further processing (such as folding generator of strings)"""
    func, data = arg
    return func(context, mapping, data)


def evalfuncarg(context, mapping, arg):
    """Evaluate given argument as value type"""
    thing = evalrawexp(context, mapping, arg)
    thing = templatekw.unwrapvalue(thing)
    # evalrawexp() may return string, generator of strings or arbitrary object
    # such as date tuple, but filter does not want generator.
    if isinstance(thing, types.GeneratorType):
        thing = stringify(thing)
    return thing


def evalboolean(context, mapping, arg):
    """Evaluate given argument as boolean, but also takes boolean literals"""
    func, data = arg
    if func is runsymbol:
        thing = func(context, mapping, data, default=None)
        if thing is None:
            # not a template keyword, takes as a boolean literal
            thing = util.parsebool(data)
    else:
        thing = func(context, mapping, data)
    thing = templatekw.unwrapvalue(thing)
    if isinstance(thing, bool):
        return thing
    # other objects are evaluated as strings, which means 0 is True, but
    # empty dict/list should be False as they are expected to be ''
    return bool(stringify(thing))


def evalinteger(context, mapping, arg, err=None):
    v = evalfuncarg(context, mapping, arg)
    try:
        return int(v)
    except (TypeError, ValueError):
        raise error.ParseError(err or _("not an integer"))


def evalstring(context, mapping, arg):
    return stringify(evalrawexp(context, mapping, arg))


def evalstringliteral(context, mapping, arg):
    """Evaluate given argument as string template, but returns symbol name
    if it is unknown"""
    func, data = arg
    if func is runsymbol:
        thing = func(context, mapping, data, default=data)
    else:
        thing = func(context, mapping, data)
    return stringify(thing)


_evalfuncbytype = {bool: evalboolean, bytes: evalstring, int: evalinteger}


def evalastype(context, mapping, arg, typ):
    """Evaluate given argument and coerce its type"""
    try:
        f = _evalfuncbytype[typ]
    except KeyError:
        raise error.ProgrammingError("invalid type specified: %r" % typ)
    return f(context, mapping, arg)


def runinteger(context, mapping, data):
    return int(data)


def runstring(context, mapping, data):
    return data


def _recursivesymbolblocker(key):
    def showrecursion(**args):
        raise error.Abort(_("recursive reference '%s' in template") % key)

    return showrecursion


def _runrecursivesymbol(context, mapping, key):
    raise error.Abort(_("recursive reference '%s' in template") % key)


def runsymbol(context, mapping, key, default=""):
    v = mapping.get(key)
    if v is None:
        v = context._defaults.get(key)
    if v is None:
        # put poison to cut recursion. we can't move this to parsing phase
        # because "x = {x}" is allowed if "x" is a keyword. (issue4758)
        safemapping = mapping.copy()
        safemapping[key] = _recursivesymbolblocker(key)
        try:
            v = context.process(key, safemapping)
        except TemplateNotFound:
            v = default
    if callable(v):
        try:
            v = util.checksignature(v)(**mapping)
        except error.SignatureError:
            # If certain data requested is missing. Pretend the template
            # function does not exist.
            v = default

    return v


def buildtemplate(exp, context):
    ctmpl = [compileexp(e, context, methods) for e in exp[1:]]
    return (runtemplate, ctmpl)


def runtemplate(context, mapping, template):
    for arg in template:
        yield evalrawexp(context, mapping, arg)


def buildfilter(exp, context):
    n = getsymbol(exp[2])
    if n in context._filters:
        filt = context._filters[n]
        arg = compileexp(exp[1], context, methods)
        return (runfilter, (arg, filt))
    if n in funcs:
        f = funcs[n]
        args = _buildfuncargs(exp[1], context, methods, n, f._argspec)
        return (f, args)
    raise error.ParseError(_("unknown function '%s'") % n)


def runfilter(context, mapping, data):
    arg, filt = data
    thing = evalfuncarg(context, mapping, arg)
    try:
        return filt(thing)
    except (ValueError, AttributeError, TypeError):
        sym = findsymbolicname(arg)
        if sym:
            msg = _("template filter '%s' is not compatible with keyword '%s'") % (
                filt.__name__,
                sym,
            )
        else:
            msg = _("incompatible use of template filter '%s'") % filt.__name__
        raise error.Abort(msg)


def buildmap(exp, context):
    darg = compileexp(exp[1], context, methods)
    targ = gettemplate(exp[2], context)
    return (runmap, (darg, targ))


def runmap(context, mapping, data):
    darg, targ = data
    d = evalrawexp(context, mapping, darg)
    if util.safehasattr(d, "itermaps"):
        diter = d.itermaps()
    else:
        try:
            diter = iter(d)
        except TypeError:
            sym = findsymbolicname(darg)
            if sym:
                raise error.ParseError(_("keyword '%s' is not iterable") % sym)
            else:
                raise error.ParseError(_("%r is not iterable") % d)

    for i, v in enumerate(diter):
        lm = mapping.copy()
        lm["index"] = i
        if isinstance(v, dict):
            lm.update(v)
            lm["originalnode"] = mapping.get("node")
            yield evalrawexp(context, lm, targ)
        else:
            # v is not an iterable of dicts, this happen when 'key'
            # has been fully expanded already and format is useless.
            # If so, return the expanded value.
            yield v


def buildmember(exp, context):
    darg = compileexp(exp[1], context, methods)
    memb = getsymbol(exp[2])
    return (runmember, (darg, memb))


def runmember(context, mapping, data):
    darg, memb = data
    d = evalrawexp(context, mapping, darg)
    if util.safehasattr(d, "tomap"):
        lm = mapping.copy()
        lm.update(d.tomap())
        return runsymbol(context, lm, memb)
    if util.safehasattr(d, "get"):
        return _getdictitem(d, memb)

    sym = findsymbolicname(darg)
    if sym:
        raise error.ParseError(_("keyword '%s' has no member") % sym)
    else:
        raise error.ParseError(_("%r has no member") % d)


def buildnegate(exp, context):
    arg = compileexp(exp[1], context, exprmethods)
    return (runnegate, arg)


def runnegate(context, mapping, data):
    data = evalinteger(context, mapping, data, _("negation needs an integer argument"))
    return -data


def buildarithmetic(exp, context, func):
    left = compileexp(exp[1], context, exprmethods)
    right = compileexp(exp[2], context, exprmethods)
    return (runarithmetic, (func, left, right))


def runarithmetic(context, mapping, data):
    func, left, right = data
    left = evalinteger(context, mapping, left, _("arithmetic only defined on integers"))
    right = evalinteger(
        context, mapping, right, _("arithmetic only defined on integers")
    )
    try:
        return func(left, right)
    except ZeroDivisionError:
        raise error.Abort(_("division by zero is not defined"))


def buildfunc(exp, context):
    n = getsymbol(exp[1])
    if n in funcs:
        f = funcs[n]
        args = _buildfuncargs(exp[2], context, exprmethods, n, f._argspec)
        return (f, args)
    if n in context._filters:
        args = _buildfuncargs(exp[2], context, exprmethods, n, argspec=None)
        if len(args) != 1:
            raise error.ParseError(_("filter %s expects one argument") % n)
        f = context._filters[n]
        return (runfilter, (args[0], f))
    raise error.ParseError(_("unknown function '%s'") % n)


def _buildfuncargs(exp, context, curmethods, funcname, argspec):
    """Compile parsed tree of function arguments into list or dict of
    (func, data) pairs

    >>> context = engine(lambda t: (runsymbol, t))
    >>> def fargs(expr, argspec):
    ...     x = _parseexpr(expr)
    ...     n = getsymbol(x[1])
    ...     return _buildfuncargs(x[2], context, exprmethods, n, argspec)
    >>> list(fargs(b'a(l=1, k=2)', b'k l m').keys())
    ['l', 'k']
    >>> args = fargs(b'a(opts=1, k=2)', b'**opts')
    >>> list(args.keys()), list(args[b'opts'].keys())
    (['opts'], ['opts', 'k'])
    """

    def compiledict(xs):
        return util.sortdict(
            (k, compileexp(x, context, curmethods)) for k, x in pycompat.iteritems(xs)
        )

    def compilelist(xs):
        return [compileexp(x, context, curmethods) for x in xs]

    if not argspec:
        # filter or function with no argspec: return list of positional args
        return compilelist(getlist(exp))

    # function with argspec: return dict of named args
    _poskeys, varkey, _keys, optkey = argspec = parser.splitargspec(argspec)
    treeargs = parser.buildargsdict(
        getlist(exp), funcname, argspec, keyvaluenode="keyvalue", keynode="symbol"
    )
    compargs = util.sortdict()
    if varkey:
        compargs[varkey] = compilelist(treeargs.pop(varkey))
    if optkey:
        compargs[optkey] = compiledict(treeargs.pop(optkey))
    compargs.update(compiledict(treeargs))
    return compargs


def buildkeyvaluepair(exp, content):
    raise error.ParseError(_("can't use a key-value pair in this context"))


# dict of template built-in functions
funcs = {}

templatefunc = registrar.templatefunc(funcs)


@templatefunc("date(date[, fmt])")
def date(context, mapping, args):
    """Format a date. See :hg:`help dates` for formatting
    strings. The default is a Unix date format, including the timezone:
    "Mon Sep 04 15:13:13 2006 0700"."""
    if not (1 <= len(args) <= 2):
        # i18n: "date" is a keyword
        raise error.ParseError(_("date expects one or two arguments"))

    date = evalfuncarg(context, mapping, args[0])
    fmt = None
    if len(args) == 2:
        fmt = evalstring(context, mapping, args[1])
    try:
        if fmt is None:
            return util.datestr(date)
        else:
            return util.datestr(date, fmt)
    except (TypeError, ValueError):
        # i18n: "date" is a keyword
        raise error.ParseError(_("date expects a date information"))


@templatefunc("dict([[key=]value...])", argspec="*args **kwargs")
def dict_(context, mapping, args):
    """Construct a dict from key-value pairs. A key may be omitted if
    a value expression can provide an unambiguous name."""
    data = util.sortdict()

    for v in args["args"]:
        k = findsymbolicname(v)
        if not k:
            raise error.ParseError(_("dict key cannot be inferred"))
        if k in data or k in args["kwargs"]:
            raise error.ParseError(_("duplicated dict key '%s' inferred") % k)
        data[k] = evalfuncarg(context, mapping, v)

    data.update(
        (k, evalfuncarg(context, mapping, v))
        for k, v in pycompat.iteritems(args["kwargs"])
    )
    return templatekw.hybriddict(data)


@templatefunc("diff([includepattern [, excludepattern]])")
def diff(context, mapping, args):
    """Show a diff, optionally
    specifying files to include or exclude."""
    if len(args) > 2:
        # i18n: "diff" is a keyword
        raise error.ParseError(_("diff expects zero, one, or two arguments"))

    def getpatterns(i):
        if i < len(args):
            s = evalstring(context, mapping, args[i]).strip()
            if s:
                return [s]
        return []

    ctx = mapping["ctx"]
    chunks = ctx.diff(match=ctx.match([], getpatterns(0), getpatterns(1)))

    return pycompat.decodeutf8(b"".join(chunks))


@templatefunc("extdata(source)", argspec="source")
def extdata(context, mapping, args):
    """Show a text read from the specified extdata source. (EXPERIMENTAL)"""
    if "source" not in args:
        # i18n: "extdata" is a keyword
        raise error.ParseError(_("extdata expects one argument"))

    source = evalstring(context, mapping, args["source"])
    cache = mapping["cache"].setdefault("extdata", {})
    ctx = mapping["ctx"]
    if source in cache:
        data = cache[source]
    else:
        data = cache[source] = scmutil.extdatasource(ctx.repo(), source)
    return data.get(ctx.rev(), "")


@templatefunc("files(pattern)")
def files(context, mapping, args):
    """All files of the current changeset matching the pattern. See
    :hg:`help patterns`."""
    if not len(args) == 1:
        # i18n: "files" is a keyword
        raise error.ParseError(_("files expects one argument"))

    raw = evalstring(context, mapping, args[0])
    ctx = mapping["ctx"]
    m = ctx.match([raw])
    files = list(ctx.matches(m))
    return templatekw.showlist("file", files, mapping)


@templatefunc("fill(text[, width[, initialident[, hangindent]]])")
def fill(context, mapping, args):
    """Fill many
    paragraphs with optional indentation. See the "fill" filter."""
    if not (1 <= len(args) <= 4):
        # i18n: "fill" is a keyword
        raise error.ParseError(_("fill expects one to four arguments"))

    text = evalstring(context, mapping, args[0])
    width = 76
    initindent = ""
    hangindent = ""
    if 2 <= len(args) <= 4:
        width = evalinteger(
            context,
            mapping,
            args[1],
            # i18n: "fill" is a keyword
            _("fill expects an integer width"),
        )
        try:
            initindent = evalstring(context, mapping, args[2])
            hangindent = evalstring(context, mapping, args[3])
        except IndexError:
            pass

    return templatefilters.fill(text, width, initindent, hangindent)


@templatefunc("formatnode(node)")
def formatnode(context, mapping, args):
    """Obtain the preferred form of a changeset hash. (DEPRECATED)"""
    if len(args) != 1:
        # i18n: "formatnode" is a keyword
        raise error.ParseError(_("formatnode expects one argument"))

    ui = mapping["ui"]
    node = evalstring(context, mapping, args[0])
    if ui.debugflag:
        return node
    return templatefilters.short(node)


@templatefunc(
    "pad(text, width[, fillchar=' '[, left=False]])", argspec="text width fillchar left"
)
def pad(context, mapping, args):
    """Pad text with a
    fill character."""
    if "text" not in args or "width" not in args:
        # i18n: "pad" is a keyword
        raise error.ParseError(_("pad() expects two to four arguments"))

    width = evalinteger(
        context,
        mapping,
        args["width"],
        # i18n: "pad" is a keyword
        _("pad() expects an integer width"),
    )

    text = evalstring(context, mapping, args["text"])

    left = False
    fillchar = " "
    if "fillchar" in args:
        fillchar = evalstring(context, mapping, args["fillchar"])
        fillchar_width = encoding.colwidth(color.stripeffects(fillchar))
        if fillchar_width != 1:
            # i18n: "pad" is a keyword
            raise error.ParseError(_("pad() expects a single fill character"))
    if "left" in args:
        left = evalboolean(context, mapping, args["left"])

    fillwidth = width - encoding.colwidth(color.stripeffects(text))
    if fillwidth <= 0:
        return text
    if left:
        return fillchar * fillwidth + text
    else:
        return text + fillchar * fillwidth


@templatefunc("indent(text, indentchars[, firstline])")
def indent(context, mapping, args):
    """Indents all non-empty lines
    with the characters given in the indentchars string. An optional
    third parameter will override the indent for the first line only
    if present."""
    if not (2 <= len(args) <= 3):
        # i18n: "indent" is a keyword
        raise error.ParseError(_("indent() expects two or three arguments"))

    text = evalstring(context, mapping, args[0])
    indent = evalstring(context, mapping, args[1])

    if len(args) == 3:
        firstline = evalstring(context, mapping, args[2])
    else:
        firstline = indent

    # the indent function doesn't indent the first line, so we do it here
    return templatefilters.indent(firstline + text, indent)


@templatefunc("get(dict, key[, default])")
def get(context, mapping, args):
    """Get an attribute/key from an object. Some keywords
    are complex types. This function allows you to obtain the value of an
    attribute on these types."""
    if not (2 <= len(args) <= 3):
        # i18n: "get" is a keyword
        raise error.ParseError(_("get() expects two or three arguments"))

    dictarg = evalfuncarg(context, mapping, args[0])
    if not util.safehasattr(dictarg, "get"):
        # i18n: "get" is a keyword
        raise error.ParseError(_("get() expects a dict as first argument"))

    key = evalfuncarg(context, mapping, args[1])
    value = _getdictitem(dictarg, key)
    if value is None and len(args) == 3:
        value = evalrawexp(context, mapping, args[2])
    return value


def _getdictitem(dictarg, key):
    val = dictarg.get(key)
    if val is None:
        return None
    return templatekw.wraphybridvalue(dictarg, key, val)


@templatefunc("if(expr, then[, else])")
def if_(context, mapping, args):
    """Conditionally execute based on the result of
    an expression."""
    if not (2 <= len(args) <= 3):
        # i18n: "if" is a keyword
        raise error.ParseError(_("if expects two or three arguments"))

    test = evalboolean(context, mapping, args[0])
    if test:
        yield evalrawexp(context, mapping, args[1])
    elif len(args) == 3:
        yield evalrawexp(context, mapping, args[2])


@templatefunc("ifcontains(needle, haystack, then[, else])")
def ifcontains(context, mapping, args):
    """Conditionally execute based
    on whether the item "needle" is in "haystack"."""
    if not (3 <= len(args) <= 4):
        # i18n: "ifcontains" is a keyword
        raise error.ParseError(_("ifcontains expects three or four arguments"))

    haystack = evalfuncarg(context, mapping, args[1])
    try:
        needle = evalastype(
            context, mapping, args[0], getattr(haystack, "keytype", None) or bytes
        )
        found = needle in haystack
    except error.ParseError:
        found = False

    if found:
        yield evalrawexp(context, mapping, args[2])
    elif len(args) == 4:
        yield evalrawexp(context, mapping, args[3])


@templatefunc("ifeq(expr1, expr2, then[, else])")
def ifeq(context, mapping, args):
    """Conditionally execute based on
    whether 2 items are equivalent."""
    if not (3 <= len(args) <= 4):
        # i18n: "ifeq" is a keyword
        raise error.ParseError(_("ifeq expects three or four arguments"))

    test = evalstring(context, mapping, args[0])
    match = evalstring(context, mapping, args[1])
    if test == match:
        yield evalrawexp(context, mapping, args[2])
    elif len(args) == 4:
        yield evalrawexp(context, mapping, args[3])


@templatefunc("ifgt(expr1, expr2, then[, else])")
def ifgt(context, mapping, args):
    """Conditionally execute based on whether expr1 is greater than expr2.
    Both expr1 and expr2 need to be integers."""
    if not (3 <= len(args) <= 4):
        # i18n: "ifgt" is a keyword
        raise error.ParseError(_("ifgt expects three or four arguments"))

    expr1 = evalstring(context, mapping, args[0])
    expr2 = evalstring(context, mapping, args[1])
    try:
        gt = int(expr1) > int(expr2)
    except ValueError:
        raise error.ParseError(_("ifgt can only compare integers"))

    if gt:
        yield evalrawexp(context, mapping, args[2])
    elif len(args) == 4:
        yield evalrawexp(context, mapping, args[3])


@templatefunc("join(list, sep)")
def join(context, mapping, args):
    """Join items in a list with a delimiter."""
    if not (1 <= len(args) <= 2):
        # i18n: "join" is a keyword
        raise error.ParseError(_("join expects one or two arguments"))

    # TODO: perhaps this should be evalfuncarg(), but it can't because hgweb
    # abuses generator as a keyword that returns a list of dicts.
    joinset = evalrawexp(context, mapping, args[0])
    joinset = templatekw.unwrapvalue(joinset)
    joinfmt = getattr(joinset, "joinfmt", pycompat.identity)
    joiner = " "
    if len(args) > 1:
        joiner = evalstring(context, mapping, args[1])

    first = True
    for x in joinset:
        if first:
            first = False
        else:
            yield joiner
        yield joinfmt(x)


@templatefunc("label(label, expr)")
def label(context, mapping, args):
    """Apply a label to generated content. Content with
    a label applied can result in additional post-processing, such as
    automatic colorization."""
    if len(args) != 2:
        # i18n: "label" is a keyword
        raise error.ParseError(_("label expects two arguments"))

    ui = mapping["ui"]
    thing = evalstring(context, mapping, args[1])
    # preserve unknown symbol as literal so effects like 'red', 'bold',
    # etc. don't need to be quoted
    label = evalstringliteral(context, mapping, args[0])

    return ui.label(thing, label)


@templatefunc("localdate(date[, tz])")
def localdate(context, mapping, args):
    """Converts a date to the specified timezone.
    The default is local date."""
    if not (1 <= len(args) <= 2):
        # i18n: "localdate" is a keyword
        raise error.ParseError(_("localdate expects one or two arguments"))

    date = evalfuncarg(context, mapping, args[0])
    try:
        date = util.parsedate(date)
    except AttributeError:  # not str nor date tuple
        # i18n: "localdate" is a keyword
        raise error.ParseError(_("localdate expects a date information"))
    if len(args) >= 2:
        tzoffset = None
        tz = evalfuncarg(context, mapping, args[1])
        if isinstance(tz, str):
            tzoffset, remainder = util.parsetimezone(tz)
            if remainder:
                tzoffset = None
        if tzoffset is None:
            try:
                tzoffset = int(tz)
            except (TypeError, ValueError):
                # i18n: "localdate" is a keyword
                raise error.ParseError(_("localdate expects a timezone"))
    else:
        tzoffset = util.makedate()[1]
    return (date[0], tzoffset)


@templatefunc("max(iterable)")
def max_(context, mapping, args, **kwargs):
    """Return the max of an iterable"""
    if len(args) != 1:
        # i18n: "max" is a keyword
        raise error.ParseError(_("max expects one argument"))

    iterable = evalfuncarg(context, mapping, args[0])
    try:
        x = max(iterable)
    except (TypeError, ValueError):
        # i18n: "max" is a keyword
        raise error.ParseError(_("max first argument should be an iterable"))
    return templatekw.wraphybridvalue(iterable, x, x)


@templatefunc("min(iterable)")
def min_(context, mapping, args, **kwargs):
    """Return the min of an iterable"""
    if len(args) != 1:
        # i18n: "min" is a keyword
        raise error.ParseError(_("min expects one argument"))

    iterable = evalfuncarg(context, mapping, args[0])
    try:
        x = min(iterable)
    except (TypeError, ValueError):
        # i18n: "min" is a keyword
        raise error.ParseError(_("min first argument should be an iterable"))
    return templatekw.wraphybridvalue(iterable, x, x)


@templatefunc("mod(a, b)")
def mod(context, mapping, args):
    """Calculate a mod b such that a / b + a mod b == a"""
    if not len(args) == 2:
        # i18n: "mod" is a keyword
        raise error.ParseError(_("mod expects two arguments"))

    func = lambda a, b: a % b
    return runarithmetic(context, mapping, (func, args[0], args[1]))


@templatefunc("smallcommitmeta(category)")
def showsmallcommitmeta(context, mapping, args):
    """String. The small commit metadata in the provided category associated with the changeset."""
    category = evalstring(context, mapping, args[0])
    repo = mapping["repo"]
    ctx = mapping["ctx"]
    commitmeta = repo.smallcommitmetadata
    try:
        return commitmeta.read(ctx.node(), category)
    except KeyError:
        return ""


@templatefunc("capitalize(word)")
def capitalize(context, mapping, args):
    """Capitalize the first letter"""
    if len(args) != 1:
        raise error.ParseError(_("capitalize expects one argument"))
    word = evalfuncarg(context, mapping, args[0])
    return word.capitalize()


@templatefunc("relpath(path)")
def relpath(context, mapping, args):
    """Convert a repository-absolute path into a filesystem path relative to
    the current working directory."""
    if len(args) != 1:
        # i18n: "relpath" is a keyword
        raise error.ParseError(_("relpath expects one argument"))

    repo = mapping["repo"]
    path = evalstring(context, mapping, args[0])
    return repo.pathto(path)


@templatefunc("revset(query[, formatargs...])")
def revset(context, mapping, args):
    """Execute a revision set query. See
    :hg:`help revset`."""
    if not len(args) > 0:
        # i18n: "revset" is a keyword
        raise error.ParseError(_("revset expects one or more arguments"))

    raw = evalstring(context, mapping, args[0])
    ctx = mapping["ctx"]
    repo = ctx.repo()

    def query(expr):
        m = revsetmod.match(repo.ui, expr, repo=repo)
        return m(repo)

    if len(args) > 1:
        formatargs = [evalfuncarg(context, mapping, a) for a in args[1:]]
        revs = query(revsetlang.formatspec(raw, *formatargs))
        revs = list(revs)
    else:
        revsetcache = mapping["cache"].setdefault("revsetcache", {})
        if raw in revsetcache:
            revs = revsetcache[raw]
        else:
            revs = query(raw)
            revs = list(revs)
            revsetcache[raw] = revs

    return templatekw.showrevslist("revision", revs, **mapping)


@templatefunc("rstdoc(text, style)")
def rstdoc(context, mapping, args):
    """Format reStructuredText."""
    if len(args) != 2:
        # i18n: "rstdoc" is a keyword
        raise error.ParseError(_("rstdoc expects two arguments"))

    text = evalstring(context, mapping, args[0])
    style = evalstring(context, mapping, args[1])

    return minirst.format(text, style=style, keep=["verbose"])


@templatefunc("separate(sep, args)", argspec="sep *args")
def separate(context, mapping, args):
    """Add a separator between non-empty arguments."""
    if "sep" not in args:
        # i18n: "separate" is a keyword
        raise error.ParseError(_("separate expects at least one argument"))

    sep = evalstring(context, mapping, args["sep"])
    first = True
    for arg in args["args"]:
        argstr = evalstring(context, mapping, arg)
        if not argstr:
            continue
        if first:
            first = False
        else:
            yield sep
        yield argstr


@templatefunc("shortest(node, minlength=4)")
def shortest(context, mapping, args):
    """Obtain the shortest representation of
    a node."""
    if not (1 <= len(args) <= 2):
        # i18n: "shortest" is a keyword
        raise error.ParseError(_("shortest() expects one or two arguments"))

    node = evalstring(context, mapping, args[0])

    minlength = 4
    if len(args) > 1:
        minlength = evalinteger(
            context,
            mapping,
            args[1],
            # i18n: "shortest" is a keyword
            _("shortest() expects an integer minlength"),
        )

    # _partialmatch() of filtered changelog could take O(len(repo)) time,
    # which would be unacceptably slow. so we look for hash collision in
    # unfiltered space, which means some hashes may be slightly longer.
    cl = mapping["ctx"]._repo.changelog
    return cl.shortest(node, minlength)


@templatefunc("strip(text[, chars])")
def strip(context, mapping, args):
    """Strip characters from a string. By default,
    strips all leading and trailing whitespace."""
    if not (1 <= len(args) <= 2):
        # i18n: "strip" is a keyword
        raise error.ParseError(_("strip expects one or two arguments"))

    text = evalstring(context, mapping, args[0])
    if len(args) == 2:
        chars = evalstring(context, mapping, args[1])
        return text.strip(chars)
    return text.strip()


@templatefunc("sub(pattern, replacement, expression)")
def sub(context, mapping, args):
    """Perform text substitution
    using regular expressions."""
    if len(args) != 3:
        # i18n: "sub" is a keyword
        raise error.ParseError(_("sub expects three arguments"))

    pat = evalstring(context, mapping, args[0])
    rpl = evalstring(context, mapping, args[1])
    src = evalstring(context, mapping, args[2])
    try:
        patre = re.compile(pat)
    except re.error:
        # i18n: "sub" is a keyword
        raise error.ParseError(_("sub got an invalid pattern: %s") % pat)
    try:
        yield patre.sub(rpl, src)
    except re.error:
        # i18n: "sub" is a keyword
        raise error.ParseError(_("sub got an invalid replacement: %s") % rpl)


@templatefunc("startswith(pattern, text)")
def startswith(context, mapping, args):
    """Returns the value from the "text" argument
    if it begins with the content from the "pattern" argument."""
    if len(args) != 2:
        # i18n: "startswith" is a keyword
        raise error.ParseError(_("startswith expects two arguments"))

    patn = evalstring(context, mapping, args[0])
    text = evalstring(context, mapping, args[1])
    if text.startswith(patn):
        return text
    return ""


@templatefunc("truncate(text, maxlines, [suffix])")
def truncate(context, mapping, args):
    """Truncate text to no more than "maxlines" in length. If "suffix" is
    supplied, then it replaces the last line if, and only if, the text was
    truncated. """
    if not (2 <= len(args) <= 3):
        # i18n: "truncate" is a keyword
        raise error.ParseError(
            _("truncate expects two or three arguments, got %d") % len(args)
        )
    text = evalstring(context, mapping, args[0])
    maxlines = evalinteger(
        context,
        mapping,
        args[1],
        # i18n: "truncate" is a keyword
        _("truncate expects an integer line count"),
    )
    if len(args) == 3:
        suffix = evalstring(context, mapping, args[2])
        truncatelines = maxlines - 1
    else:
        suffix = None
        truncatelines = maxlines

    lines = text.splitlines(True)
    if len(lines) <= maxlines:
        return text

    lines = lines[0:truncatelines]
    if suffix:
        lines.append(suffix)
    return "".join(lines)


@templatefunc("truncatelonglines(text, maxwidth, [suffix])")
def truncatelonglines(context, mapping, args):
    """Truncate lines in text to no more than "maxwidth" in width. If "suffix"
    is supplied, then it replaces the last characters on the line if, and only
    if, the lines was truncated. """
    if not (2 <= len(args) <= 3):
        # i18n: "truncatelonglines" is a keyword
        raise error.ParseError(
            _("truncatelonglines expects two or three arguments, got %d") % len(args)
        )
    text = evalstring(context, mapping, args[0])
    maxwidth = evalinteger(
        context,
        mapping,
        args[1],
        # i18n: "truncatelonglines" is a keyword
        _("truncatelonglines expects an integer line width"),
    )
    truncatedwidth = maxwidth
    if len(args) == 3:
        suffix = evalstring(context, mapping, args[2])
        if isinstance(suffix, bytes):
            # Python 2
            truncatedwidth -= len(suffix.decode("utf-8"))
        else:
            # Python 3
            truncatedwidth -= len(suffix)
    else:
        suffix = None

    if truncatedwidth < 0:
        truncatedwidth = 0

    lines = text.splitlines(True)
    output = []
    for line in lines:
        stripped = line.rstrip("\r\n")
        if len(stripped) > maxwidth:
            eol = line[len(stripped) :]
            line = stripped[:truncatedwidth]
            if suffix is not None:
                line += suffix
            line += eol

        output.append(line)

    return "".join(output)


@templatefunc("word(number, text[, separator])")
def word(context, mapping, args):
    """Return the nth word from a string."""
    if not (2 <= len(args) <= 3):
        # i18n: "word" is a keyword
        raise error.ParseError(
            _("word expects two or three arguments, got %d") % len(args)
        )

    num = evalinteger(
        context,
        mapping,
        args[0],
        # i18n: "word" is a keyword
        _("word expects an integer index"),
    )
    text = evalstring(context, mapping, args[1])
    if len(args) == 3:
        splitter = evalstring(context, mapping, args[2])
    else:
        splitter = None

    tokens = text.split(splitter)
    if num >= len(tokens) or num < -len(tokens):
        return ""
    else:
        return tokens[num]


@templatefunc("triggerhint(name)")
def triggerhint(context, mapping, args):
    """Trigger the hint named by the argument.  Expands to the empty string."""
    if len(args) != 1:
        raise error.ParseError(
            _("triggerhint expects one argument, got %s") % len(args)
        )
    name = evalstring(context, mapping, args[0])
    hintutil.trigger(name)
    return ""


@templatefunc("hyperlink(link, title)")
def hyperlink(context, mapping, args):
    """Render hyperlink in terminal"""
    if len(args) != 2:
        raise error.ParseError("hyperlink expects two arguments, got %s" % len(args))

    title = evalstring(context, mapping, args[1])
    if not title:
        return ""

    ui = mapping["ui"]
    hyperlink = ui.formatted and ui.configbool("ui", "hyperlink", False)

    if hyperlink:
        return "\x1b]8;;{link}\x1b\\{title}\x1b]8;;\x1b\\".format(
            link=evalstring(context, mapping, args[0]), title=title
        )
    return title


# methods to interpret function arguments or inner expressions (e.g. {_(x)})
exprmethods = {
    "integer": lambda e, c: (runinteger, e[1]),
    "string": lambda e, c: (runstring, e[1]),
    "symbol": lambda e, c: (runsymbol, e[1]),
    "template": buildtemplate,
    "group": lambda e, c: compileexp(e[1], c, exprmethods),
    ".": buildmember,
    "|": buildfilter,
    "%": buildmap,
    "func": buildfunc,
    "keyvalue": buildkeyvaluepair,
    "+": lambda e, c: buildarithmetic(e, c, lambda a, b: a + b),
    "-": lambda e, c: buildarithmetic(e, c, lambda a, b: a - b),
    "negate": buildnegate,
    "*": lambda e, c: buildarithmetic(e, c, lambda a, b: a * b),
    "/": lambda e, c: buildarithmetic(e, c, lambda a, b: a // b),
}

# methods to interpret top-level template (e.g. {x}, {x|_}, {x % "y"})
methods = exprmethods.copy()
methods["integer"] = exprmethods["symbol"]  # '{1}' as variable


class _aliasrules(parser.basealiasrules):
    """Parsing and expansion rule set of template aliases"""

    _section = _("template alias")
    _parse = staticmethod(_parseexpr)

    @staticmethod
    def _trygetfunc(tree):
        """Return (name, args) if tree is func(...) or ...|filter; otherwise
        None"""
        if tree[0] == "func" and tree[1][0] == "symbol":
            return tree[1][1], getlist(tree[2])
        if tree[0] == "|" and tree[2][0] == "symbol":
            return tree[2][1], [tree[1]]


def expandaliases(tree, aliases):
    """Return new tree of aliases are expanded"""
    aliasmap = _aliasrules.buildmap(aliases)
    return _aliasrules.expand(aliasmap, tree)


# template engine

stringify = templatefilters.stringify


def _flatten(thing):
    # type: Any -> Iterable[str]
    """yield a single stream from a possibly nested set of iterators"""
    thing = templatekw.unwraphybrid(thing)
    if isinstance(thing, str):
        yield thing
    elif isinstance(thing, bytes):
        raise error.ProgrammingError(
            "Mercurial IO including templates is done with str, not bytes"
        )
    elif thing is None:
        pass
    elif not util.safehasattr(thing, "__iter__"):
        yield pycompat.bytestr(thing)
    else:
        for i in thing:
            for j in _flatten(i):
                yield j


def unquotestring(s):
    """unwrap quotes if any; otherwise returns unmodified string"""
    if len(s) < 2 or s[0] not in "'\"" or s[0] != s[-1]:
        return s
    return s[1:-1]


class engine(object):
    """template expansion engine.

    template expansion works like this. a map file contains key=value
    pairs. if value is quoted, it is treated as string. otherwise, it
    is treated as name of template file.

    templater is asked to expand a key in map. it looks up key, and
    looks for strings like this: {foo}. it expands {foo} by looking up
    foo in map, and substituting it. expansion is recursive: it stops
    when there is no more {foo} to replace.

    expansion also allows formatting and filtering.

    format uses key to expand each item in list. syntax is
    {key%format}.

    filter uses function to transform value. syntax is
    {key|filter1|filter2|...}."""

    def __init__(self, loader, filters=None, defaults=None, aliases=()):
        self._loader = loader
        if filters is None:
            filters = {}
        self._filters = filters
        if defaults is None:
            defaults = {}
        self._defaults = defaults
        self._aliasmap = _aliasrules.buildmap(aliases)
        self._cache = {}  # key: (func, data)

    def _load(self, t):
        """load, parse, and cache a template"""
        if t not in self._cache:
            # put poison to cut recursion while compiling 't'
            self._cache[t] = (_runrecursivesymbol, t)
            try:
                x = parse(self._loader(t))
                if self._aliasmap:
                    x = _aliasrules.expand(self._aliasmap, x)
                self._cache[t] = compileexp(x, self, methods)
            except:  # re-raises
                del self._cache[t]
                raise
        return self._cache[t]

    def process(self, t, mapping):
        """Perform expansion. t is name of map element to expand.
        mapping contains added elements for use during expansion. Is a
        generator."""
        func, data = self._load(t)
        return _flatten(func(self, mapping, data))


engines = {"default": engine}


_builtinmapfiles = {
    "bisect": (
        {
            "changeset": "{cset}{lbisect}{branches}{bookmarks}{parents}{user}{ldate}{summary}\\n",
            "ldate": '{label("log.date",\n"date:        {date|date}")}\\n',
            "lfiles": '{if(files,\nlabel("ui.note log.files",\n"files:       {files}\\n"))}',
            "lfile_adds": '{if(file_adds,\nlabel("ui.debug log.files",\n"files+:      {file_adds}\\n"))}',
            "manifest": '{label("ui.debug log.manifest",\n"manifest:    {node}")}\\n',
            "bookmark": '{label("log.bookmark",\n"bookmark:    {bookmark}")}\\n',
            "extra": '{label("ui.debug log.extra",\n"extra:       {key}={value|stringescape}")}\\n',
            "cset": '{labelcset("changeset:   {rev}:{node|short}")}\\n',
            "changeset_quiet": "{lshortbisect} {rev}:{node|short}\\n",
            "branch": '{label("log.branch",\n"branch:      {branch}")}\\n',
            "bisectlabel": " bisect.{word('0', bisect)}",
            "fullcset": '{labelcset("changeset:   {rev}:{node}")}\\n',
            "status": '{status} {path}\\n{if(copy, "  {copy}\\n")}',
            "lfile_copies_switch": '{if(file_copies_switch,\nlabel("ui.note log.copies",\n"copies:     {file_copies_switch\n% \' {name} ({source})\'}\\n"))}',
            "description": "{if(desc|strip, \"{label('ui.note log.description',\n'description:')}\n{label('ui.note log.description',\n'{desc|strip}')}\\n\\n\")}",
            "parent": '{label("log.parent changeset.{phase}",\n"parent:      {rev}:{node|formatnode}")}\\n',
            "lbisect": '{label("log.bisect{if(bisect, bisectlabel)}",\n"bisect:      {bisect}\\n")}',
            "lshortbisect": '{label("log.bisect{if(bisect, bisectlabel)}",\n"{bisect|shortbisect}")}',
            "user": '{label("log.user",\n"user:        {author}")}\\n',
            "lfile_dels": '{if(file_dels,\nlabel("ui.debug log.files",\n"files-:      {file_dels}\\n"))}',
            "lnode": '{label("log.node",\n"{rev}:{node|short}")}\\n',
            "summary": "{if(desc|strip, \"{label('log.summary',\n'summary:     {desc|firstline}')}\\n\")}",
            "changeset_verbose": "{cset}{lbisect}{branches}{bookmarks}{parents}{user}{ldate}{lfiles}{lfile_copies_switch}{description}\\n",
            "lphase": '{label("log.phase",\n"phase:       {phase}")}\\n',
            "changeset_debug": "{fullcset}{lbisect}{branches}{bookmarks}{lphase}{parents}{manifest}{user}{ldate}{lfile_mods}{lfile_adds}{lfile_dels}{lfile_copies_switch}{extras}{description}\\n",
            "lfile_mods": '{if(file_mods,\nlabel("ui.debug log.files",\n"files:       {file_mods}\\n"))}',
        },
        {},
        [
            (
                "labelcset(expr)",
                'label(separate(" ",\n"log.changeset",\n"changeset.{phase}",\nif(obsolete, "changeset.obsolete")),\nexpr)',
            )
        ],
    ),
    "changelog": (
        {
            "last_file": "{file}:\\n\\t",
            "changeset": "\\t* {files|stringify|fill68|tabindent}{desc|fill68|tabindent|strip}\\n\\t[{node|short}]{branches}\\n\\n",
            "file": "{file}, ",
            "branch": "{branch}, ",
            "file_del": "{file_del}, ",
            "file_add": "{file_add}, ",
            "changeset_verbose": "{date|isodate}  {author|person}  <{author|email}>  ({node|short}{branches})\\n\\n\\t* {file_adds|stringify|fill68|tabindent}{file_dels|stringify|fill68|tabindent}{files|stringify|fill68|tabindent}{desc|fill68|tabindent|strip}\\n\\n",
            "start_branches": " <",
            "header": "{date|shortdate}  {author|person}  <{author|email}>\\n\\n",
            "changeset_quiet": "\\t* {desc|firstline|fill68|tabindent|strip}\\n\\n",
            "last_branch": "{branch}>",
            "last_file_del": "{file_del}: deleted file.\\n* ",
            "header_verbose": "",
            "last_file_add": "{file_add}: new file.\\n* ",
        },
        {},
        [],
    ),
    "compact": (
        {
            "changeset": "{lrev}{bookmarks}{parents}   {lnode}   {ldate}   {luser}\\n  {ldescfirst}\\n\\n",
            "lauthor": '{label("log.user",\n"{author}")}',
            "ldate": '{label("log.date",\n"{date|isodate}")}',
            "parent": "{lrev},",
            "lrev": '{label("log.changeset changeset.{phase}",\n"{rev}")}',
            "bookmark": '{label("log.bookmark",\n"{bookmark},")}',
            "start_parents": ":",
            "changeset_quiet": "{lrev}:{lnode}\\n",
            "changeset_verbose": "{lrev}{parents}   {lnode}   {ldate}   {lauthor}\\n  {ldesc}\\n\\n",
            "ldescfirst": "{label('ui.note log.description',\n'{desc|firstline|strip}')}",
            "ldesc": "{label('ui.note log.description',\n'{desc|strip}')}",
            "last_bookmark": "{bookmark}]",
            "luser": '{label("log.user",\n"{author|user}")}',
            "start_bookmarks": "[",
            "last_parent": "{lrev}",
            "lnode": '{label("log.node",\n"{node|short}")}',
        },
        {},
        [],
    ),
    "default": (
        {
            "changeset": "{cset}{branches}{bookmarks}{parents}{user}{ldate}{summary}\\n",
            "ldate": '{label("log.date",\n"date:        {date|date}")}\\n',
            "lfiles": '{if(files,\nlabel("ui.note log.files",\n"files:       {files}\\n"))}',
            "lfile_adds": '{if(file_adds,\nlabel("ui.debug log.files",\n"files+:      {file_adds}\\n"))}',
            "manifest": '{label("ui.debug log.manifest",\n"manifest:    {node}")}\\n',
            "bookmark": '{label("log.bookmark",\n"bookmark:    {bookmark}")}\\n',
            "extra": '{label("ui.debug log.extra",\n"extra:       {key}={value|stringescape}")}\\n',
            "cset": '{labelcset("changeset:   {rev}:{node|short}")}\\n',
            "changeset_quiet": "{lnode}",
            "branch": '{label("log.branch",\n"branch:      {branch}")}\\n',
            "fullcset": '{labelcset("changeset:   {rev}:{node}")}\\n',
            "status": '{status} {path}\\n{if(copy, "  {copy}\\n")}',
            "lfile_copies_switch": '{if(file_copies_switch,\nlabel("ui.note log.copies",\n"copies:     {file_copies_switch\n% \' {name} ({source})\'}\\n"))}',
            "description": "{if(desc|strip, \"{label('ui.note log.description',\n'description:')}\n{label('ui.note log.description',\n'{desc|strip}')}\\n\\n\")}",
            "parent": '{label("log.parent changeset.{phase}",\n"parent:      {rev}:{node|formatnode}")}\\n',
            "user": '{label("log.user",\n"user:        {author}")}\\n',
            "lfile_dels": '{if(file_dels,\nlabel("ui.debug log.files",\n"files-:      {file_dels}\\n"))}',
            "lnode": '{label("log.node",\n"{rev}:{node|short}")}\\n',
            "summary": "{if(desc|strip, \"{label('log.summary',\n'summary:     {desc|firstline}')}\\n\")}",
            "changeset_verbose": "{cset}{branches}{bookmarks}{parents}{user}{ldate}{lfiles}{lfile_copies_switch}{description}\\n",
            "lphase": '{label("log.phase",\n"phase:       {phase}")}\\n',
            "changeset_debug": "{fullcset}{branches}{bookmarks}{lphase}{parents}{manifest}{user}{ldate}{lfile_mods}{lfile_adds}{lfile_dels}{lfile_copies_switch}{extras}{description}\\n",
            "lfile_mods": '{if(file_mods,\nlabel("ui.debug log.files",\n"files:       {file_mods}\\n"))}',
        },
        {},
        [
            (
                "labelcset(expr)",
                'label(separate(" ",\n"log.changeset",\n"changeset.{phase}",\nif(obsolete, "changeset.obsolete")),\nexpr)',
            )
        ],
    ),
    "phases": (
        {
            "changeset": "{cset}{branches}{bookmarks}{lphase}{parents}{user}{ldate}{summary}\\n",
            "ldate": '{label("log.date",\n"date:        {date|date}")}\\n',
            "lfiles": '{if(files,\nlabel("ui.note log.files",\n"files:       {files}\\n"))}',
            "lfile_adds": '{if(file_adds,\nlabel("ui.debug log.files",\n"files+:      {file_adds}\\n"))}',
            "manifest": '{label("ui.debug log.manifest",\n"manifest:    {node}")}\\n',
            "bookmark": '{label("log.bookmark",\n"bookmark:    {bookmark}")}\\n',
            "extra": '{label("ui.debug log.extra",\n"extra:       {key}={value|stringescape}")}\\n',
            "cset": '{labelcset("changeset:   {rev}:{node|short}")}\\n',
            "changeset_quiet": "{lnode}",
            "branch": '{label("log.branch",\n"branch:      {branch}")}\\n',
            "fullcset": '{labelcset("changeset:   {rev}:{node}")}\\n',
            "status": '{status} {path}\\n{if(copy, "  {copy}\\n")}',
            "lfile_copies_switch": '{if(file_copies_switch,\nlabel("ui.note log.copies",\n"copies:     {file_copies_switch\n% \' {name} ({source})\'}\\n"))}',
            "description": "{if(desc|strip, \"{label('ui.note log.description',\n'description:')}\n{label('ui.note log.description',\n'{desc|strip}')}\\n\\n\")}",
            "parent": '{label("log.parent changeset.{phase}",\n"parent:      {rev}:{node|formatnode}")}\\n',
            "user": '{label("log.user",\n"user:        {author}")}\\n',
            "lfile_dels": '{if(file_dels,\nlabel("ui.debug log.files",\n"files-:      {file_dels}\\n"))}',
            "lnode": '{label("log.node",\n"{rev}:{node|short}")}\\n',
            "summary": "{if(desc|strip, \"{label('log.summary',\n'summary:     {desc|firstline}')}\\n\")}",
            "changeset_verbose": "{cset}{branches}{bookmarks}{lphase}{parents}{user}{ldate}{lfiles}{lfile_copies_switch}{description}\\n",
            "lphase": '{label("log.phase",\n"phase:       {phase}")}\\n',
            "changeset_debug": "{fullcset}{branches}{bookmarks}{lphase}{parents}{manifest}{user}{ldate}{lfile_mods}{lfile_adds}{lfile_dels}{lfile_copies_switch}{extras}{description}\\n",
            "lfile_mods": '{if(file_mods,\nlabel("ui.debug log.files",\n"files:       {file_mods}\\n"))}',
        },
        {},
        [
            (
                "labelcset(expr)",
                'label(separate(" ",\n"log.changeset",\n"changeset.{phase}",\nif(obsolete, "changeset.obsolete")),\nexpr)',
            )
        ],
    ),
    "show": (
        {
            "changeset": "{cset}{branches}{bookmarks}{parents}{user}{ldate}{summary}\\n",
            "cset_namespace": '{ifeq(namespace, "branches", names_branches, names_others)}',
            "ldate": '{label("log.date",\n"date:        {date|date}")}\\n',
            "lfiles": '{if(files,\nlabel("ui.note log.files",\n"files:       {files}\\n"))}',
            "lfile_adds": '{if(file_adds,\nlabel("ui.debug log.files",\n"files+:      {file_adds}\\n"))}',
            "names_tags": "{if(names % \"{ifeq(name, 'tip', '', name)}\", \" ({label('log.{colorname}', join(names % \"{ifeq(name, 'tip', '', name)}\", ' '))})\")}",
            "manifest": '{label("ui.debug log.manifest",\n"manifest:    {node}")}\\n',
            "showbookmarks": '{if(active, "*", " ")} {pad(bookmark, longestbookmarklen + 4)}{shortest(node, nodelen)}\\n',
            "bookmark": '{label("log.bookmark",\n"bookmark:    {bookmark}")}\\n',
            "extra": '{label("ui.debug log.extra",\n"extra:       {key}={value|stringescape}")}\\n',
            "cset": '{labelcset("changeset:   {rev}:{node|short}")}\\n',
            "changeset_quiet": "{lnode}",
            "branch": '{label("log.branch",\n"branch:      {branch}")}\\n',
            "fullcset": '{labelcset("changeset:   {rev}:{node}")}\\n',
            "status": '{status} {path}\\n{if(copy, "  {copy}\\n")}',
            "lfile_copies_switch": '{if(file_copies_switch,\nlabel("ui.note log.copies",\n"copies:     {file_copies_switch\n% \' {name} ({source})\'}\\n"))}',
            "showwork": "{cset_shortnode}{namespaces % cset_namespace} {cset_shortdesc}",
            "description": "{if(desc|strip, \"{label('ui.note log.description',\n'description:')}\n{label('ui.note log.description',\n'{desc|strip}')}\\n\\n\")}",
            "parent": '{label("log.parent changeset.{phase}",\n"parent:      {rev}:{node|formatnode}")}\\n',
            "cset_shortnode": "{labelcset(shortest(node, nodelen))}",
            "user": '{label("log.user",\n"user:        {author}")}\\n',
            "names_branches": '{ifeq(branch, "default", "", " ({label(\'log.{colorname}\', branch)})")}',
            "lfile_dels": '{if(file_dels,\nlabel("ui.debug log.files",\n"files-:      {file_dels}\\n"))}',
            "lnode": '{label("log.node",\n"{rev}:{node|short}")}\\n',
            "summary": "{if(desc|strip, \"{label('log.summary',\n'summary:     {desc|firstline}')}\\n\")}",
            "names_others": "{if(names, \" ({label('log.{colorname}', join(names, ' '))})\")}",
            "changeset_verbose": "{cset}{branches}{bookmarks}{parents}{user}{ldate}{lfiles}{lfile_copies_switch}{description}\\n",
            "lphase": '{label("log.phase",\n"phase:       {phase}")}\\n',
            "showstack": "{showwork}",
            "changeset_debug": "{fullcset}{branches}{bookmarks}{lphase}{parents}{manifest}{user}{ldate}{lfile_mods}{lfile_adds}{lfile_dels}{lfile_copies_switch}{extras}{description}\\n",
            "lfile_mods": '{if(file_mods,\nlabel("ui.debug log.files",\n"files:       {file_mods}\\n"))}',
            "cset_shortdesc": '{label("log.description", desc|firstline)}',
        },
        {},
        [
            (
                "labelcset(expr)",
                'label(separate(" ",\n"log.changeset",\n"changeset.{phase}",\nif(obsolete, "changeset.obsolete")),\nexpr)',
            )
        ],
    ),
    "status": (
        {
            "changeset": "{cset}{branches}{bookmarks}{parents}{user}{ldate}{summary}{lfiles}\\n",
            "ldate": '{label("log.date",\n"date:        {date|date}")}\\n',
            "lfiles": "{if(files,\nlabel('ui.note log.files',\n'files:\\n'))}{lfile_mods}{lfile_adds}{lfile_dels}",
            "lfile_adds": '{file_adds % "{lfile_add}{lfile_src}"}',
            "manifest": '{label("ui.debug log.manifest",\n"manifest:    {node}")}\\n',
            "lfile_mod": '{label("status.modified", "M {file}\\n")}',
            "lfile_src": '{ifcontains(file, file_copies_switch,\nlabel("status.copied", "  {get(file_copies_switch, file)}\\n"))}',
            "bookmark": '{label("log.bookmark",\n"bookmark:    {bookmark}")}\\n',
            "extra": '{label("ui.debug log.extra",\n"extra:       {key}={value|stringescape}")}\\n',
            "lfile_add": '{label("status.added", "A {file}\\n")}',
            "cset": '{labelcset("changeset:   {rev}:{node|short}")}\\n',
            "changeset_quiet": "{lnode}",
            "branch": '{label("log.branch",\n"branch:      {branch}")}\\n',
            "fullcset": '{labelcset("changeset:   {rev}:{node}")}\\n',
            "status": '{status} {path}\\n{if(copy, "  {copy}\\n")}',
            "lfile_copies_switch": '{if(file_copies_switch,\nlabel("ui.note log.copies",\n"copies:     {file_copies_switch\n% \' {name} ({source})\'}\\n"))}',
            "description": "{if(desc|strip, \"{label('ui.note log.description',\n'description:')}\n{label('ui.note log.description',\n'{desc|strip}')}\\n\\n\")}",
            "parent": '{label("log.parent changeset.{phase}",\n"parent:      {rev}:{node|formatnode}")}\\n',
            "user": '{label("log.user",\n"user:        {author}")}\\n',
            "lfile_dels": "{file_dels % \"{label('status.removed', 'R {file}\\n')}\"}",
            "lnode": '{label("log.node",\n"{rev}:{node|short}")}\\n',
            "summary": "{if(desc|strip, \"{label('log.summary',\n'summary:     {desc|firstline}')}\\n\")}",
            "changeset_verbose": "{cset}{branches}{bookmarks}{parents}{user}{ldate}{description}{lfiles}\\n",
            "lphase": '{label("log.phase",\n"phase:       {phase}")}\\n',
            "changeset_debug": "{fullcset}{branches}{bookmarks}{lphase}{parents}{manifest}{user}{ldate}{extras}{description}{lfiles}\\n",
            "lfile_mods": '{file_mods % "{lfile_mod}{lfile_src}"}',
        },
        {},
        [
            (
                "labelcset(expr)",
                'label(separate(" ",\n"log.changeset",\n"changeset.{phase}",\nif(obsolete, "changeset.obsolete")),\nexpr)',
            )
        ],
    ),
    "xml": (
        {
            "changeset": '<logentry revision="{rev}" node="{node}">\\n{branches}{bookmarks}{parents}<author email="{author|email|xmlescape}">{author|person|xmlescape}</author>\\n<date>{date|rfc3339date}</date>\\n<msg xml:space="preserve">{desc|xmlescape}</msg>\\n</logentry>\\n',
            "parent": '<parent revision="{rev}" node="{node}" />\\n',
            "extra": '<extra key="{key|xmlescape}">{value|xmlescape}</extra>\\n',
            "bookmark": "<bookmark>{bookmark|xmlescape}</bookmark>\\n",
            "start_file_copies": "<copies>\\n",
            "file_mod": '<path action="M">{file_mod|xmlescape}</path>\\n',
            "file_del": '<path action="R">{file_del|xmlescape}</path>\\n',
            "changeset_verbose": '<logentry revision="{rev}" node="{node}">\\n{branches}{bookmarks}{parents}<author email="{author|email|xmlescape}">{author|person|xmlescape}</author>\\n<date>{date|rfc3339date}</date>\\n<msg xml:space="preserve">{desc|xmlescape}</msg>\\n<paths>\\n{file_adds}{file_dels}{file_mods}</paths>\\n{file_copies}</logentry>\\n',
            "file_add": '<path action="A">{file_add|xmlescape}</path>\\n',
            "branch": "<branch>{branch|xmlescape}</branch>\\n",
            "changeset_debug": '<logentry revision="{rev}" node="{node}">\\n{branches}{bookmarks}{parents}<author email="{author|email|xmlescape}">{author|person|xmlescape}</author>\\n<date>{date|rfc3339date}</date>\\n<msg xml:space="preserve">{desc|xmlescape}</msg>\\n<paths>\\n{file_adds}{file_dels}{file_mods}</paths>\\n{file_copies}{extras}</logentry>\\n',
            "docfooter": "</log>\\n",
            "end_file_copies": "</copies>\\n",
            "docheader": '<?xml version="1.0"?>\\n<log>\\n',
            "file_copy": '<copy source="{source|xmlescape}">{name|xmlescape}</copy>\\n',
        },
        {},
        [],
    ),
}

_builtinprefix = "map-cmdline."


def stylelist():
    return ", ".join(sorted(_builtinmapfiles))


def _readbuiltinstyle(path):
    # Translate paths like "a/b/map-cmdline.default" to "default",
    # Then lookup builtin style
    basename = os.path.basename(path)
    if basename.startswith(_builtinprefix):
        basename = basename[len(_builtinprefix) :]
    return _builtinmapfiles.get(basename)


def _readmapfile(mapfile):
    """Load template elements from the given map file"""
    builtin = _readbuiltinstyle(mapfile)
    if builtin is not None:
        return builtin
    if not os.path.exists(mapfile):
        raise error.Abort(
            _("style '%s' not found") % mapfile,
            hint=_("available styles: %s") % stylelist(),
        )

    base = os.path.dirname(mapfile)
    conf = config.config(includepaths=templatepaths())
    conf.read(mapfile, remap={"": "templates"})

    cache = {}
    tmap = {}
    aliases = []

    val = conf.get("templates", "__base__")
    if val and val[0] not in "'\"":
        # treat as a pointer to a base class for this style
        path = util.normpath(os.path.join(base, val))

        # fallback check in template paths
        if not os.path.exists(path):
            for p in templatepaths():
                p2 = util.normpath(os.path.join(p, val))
                if os.path.isfile(p2):
                    path = p2
                    break
                p3 = util.normpath(os.path.join(p2, "map"))
                if os.path.isfile(p3):
                    path = p3
                    break

        cache, tmap, aliases = _readmapfile(path)

    for key, val in conf["templates"].items():
        if not val:
            raise error.ParseError(_("missing value"), conf.source("templates", key))
        if val[0] in "'\"":
            if val[0] != val[-1]:
                raise error.ParseError(
                    _("unmatched quotes"), conf.source("templates", key)
                )
            cache[key] = unquotestring(val)
        elif key != "__base__":
            val = "default", val
            if ":" in val[1]:
                val = val[1].split(":", 1)
            tmap[key] = val[0], os.path.join(base, val[1])
    aliases.extend(conf["templatealias"].items())
    return cache, tmap, aliases


class TemplateNotFound(error.Abort):
    pass


class templater(object):
    def __init__(
        self,
        filters=None,
        defaults=None,
        cache=None,
        aliases=(),
        minchunk=1024,
        maxchunk=65536,
    ):
        """set up template engine.
        filters is dict of functions. each transforms a value into another.
        defaults is dict of default map definitions.
        aliases is list of alias (name, replacement) pairs.
        """
        if filters is None:
            filters = {}
        if defaults is None:
            defaults = {}
        if cache is None:
            cache = {}
        self.cache = cache.copy()
        self.map = {}
        self.filters = templatefilters.filters.copy()
        self.filters.update(filters)
        self.defaults = defaults
        self._aliases = aliases
        self.minchunk, self.maxchunk = minchunk, maxchunk
        self.ecache = {}

    @classmethod
    def frommapfile(
        cls,
        mapfile,
        filters=None,
        defaults=None,
        cache=None,
        minchunk=1024,
        maxchunk=65536,
    ):
        """Create templater from the specified map file"""
        t = cls(filters, defaults, cache, [], minchunk, maxchunk)
        cache, tmap, aliases = _readmapfile(mapfile)
        t.cache.update(cache)
        t.map = tmap
        t._aliases = aliases
        return t

    def __contains__(self, key):
        return key in self.cache or key in self.map

    def load(self, t):
        """Get the template for the given template name. Use a local cache."""
        if t not in self.cache:
            try:
                self.cache[t] = pycompat.decodeutf8(util.readfile(self.map[t][1]))
            except KeyError as inst:
                raise TemplateNotFound(_('"%s" not in template map') % inst.args[0])
            except IOError as inst:
                raise IOError(
                    inst.args[0],
                    _("template file %s: %s") % (self.map[t][1], inst.args[1]),
                )
        return self.cache[t]

    def render(self, mapping):
        """Render the default unnamed template and return result as string"""
        mapping = mapping
        return stringify(self("", **mapping))

    def __call__(self, t, **mapping):
        mapping = mapping
        ttype = t in self.map and self.map[t][0] or "default"
        if ttype not in self.ecache:
            try:
                ecls = engines[ttype]
            except KeyError:
                raise error.Abort(_("invalid template engine: %s") % ttype)
            self.ecache[ttype] = ecls(
                self.load, self.filters, self.defaults, self._aliases
            )
        proc = self.ecache[ttype]

        stream = proc.process(t, mapping)
        if self.minchunk:
            stream = util.increasingchunks(stream, min=self.minchunk, max=self.maxchunk)
        return stream


def templatepaths():
    """return locations used for template files."""
    pathsrel = ["templates"]
    paths = [os.path.normpath(os.path.join(util.datapath, f)) for f in pathsrel]
    return [p for p in paths if os.path.isdir(p)]


def isbuiltin(name):
    return name in _builtinmapfiles


def templatepath(name):
    """return location of template file. returns None if not found."""
    if name.startswith(_builtinprefix):
        # Strip the common prefix (compatibility tech debt)
        builtinname = name[len(_builtinprefix) :]
        if isbuiltin(builtinname):
            return builtinname
    if isbuiltin(name):
        return name
    for p in templatepaths():
        f = os.path.join(p, name)
        if os.path.exists(f):
            return f
    return None


def stylemap(styles, paths=None):
    """Return path to mapfile for a given style.

    Searches mapfile in the following locations:
    1. templatepath/style/map
    2. templatepath/map-style
    3. templatepath/map
    """

    if paths is None:
        paths = templatepaths()
    elif isinstance(paths, str):
        paths = [paths]

    if isinstance(styles, str):
        styles = [styles]

    for style in styles:
        # only plain name is allowed to honor template paths
        if (
            not style
            or style in (os.curdir, os.pardir)
            or pycompat.ossep in style
            or pycompat.osaltsep
            and pycompat.osaltsep in style
        ):
            continue
        locations = [os.path.join(style, "map"), "map-" + style]
        locations.append("map")

        for path in paths:
            for location in locations:
                mapfile = os.path.join(path, location)
                if os.path.isfile(mapfile):
                    return style, mapfile

    raise RuntimeError("No hgweb templates found in %r" % paths)


def loadfunction(ui, extname, registrarobj):
    """Load template function from specified registrarobj
    """
    for name, func in pycompat.iteritems(registrarobj._table):
        funcs[name] = func


# tell hggettext to extract docstrings from these functions:
i18nfunctions = funcs.values()
