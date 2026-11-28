"""Compilation and replacement budgets for untrusted standard Python regexes."""
from collections import Counter

import regex

try:
    from re import _constants, _parser
except ImportError:  # Python 3.10
    import sre_constants as _constants
    import sre_parse as _parser


MAX_REPEAT_COUNT = 1000
MAX_COMPILE_COMPLEXITY = 4096
MAX_PARSE_DEPTH = 64


class RegexBudgetError(ValueError):
    pass


def _pattern_cost(pattern, depth=0):
    if depth > MAX_PARSE_DEPTH:
        raise RegexBudgetError('正则表达式嵌套层数超过上限')
    repeat_ops = {_constants.MAX_REPEAT, _constants.MIN_REPEAT}
    possessive = getattr(_constants, 'POSSESSIVE_REPEAT', None)
    if possessive is not None:
        repeat_ops.add(possessive)
    cost = 0
    for operation, argument in pattern:
        item_cost = 1
        if operation in repeat_ops:
            minimum, maximum, child = argument
            count = max(1, minimum) if maximum == _constants.MAXREPEAT else max(1, maximum)
            if count > MAX_REPEAT_COUNT:
                raise RegexBudgetError('正则表达式重复次数超过编译上限')
            item_cost += count * _pattern_cost(child, depth + 1)
        elif operation == _constants.SUBPATTERN:
            item_cost += _pattern_cost(argument[-1], depth + 1)
        elif operation == _constants.BRANCH:
            for branch in argument[1]:
                item_cost += _pattern_cost(branch, depth + 1)
                if item_cost > MAX_COMPILE_COMPLEXITY:
                    raise RegexBudgetError('正则表达式编译复杂度超过上限')
        elif operation in {_constants.ASSERT, _constants.ASSERT_NOT}:
            item_cost += _pattern_cost(argument[1], depth + 1)
        elif operation == _constants.GROUPREF_EXISTS:
            item_cost += _pattern_cost(argument[1], depth + 1)
            if argument[2] is not None:
                item_cost += _pattern_cost(argument[2], depth + 1)
        elif operation == getattr(_constants, 'ATOMIC_GROUP', None):
            item_cost += _pattern_cost(argument, depth + 1)
        elif operation == _constants.IN:
            item_cost += len(argument)
        cost += item_cost
        if cost > MAX_COMPILE_COMPLEXITY:
            raise RegexBudgetError('正则表达式编译复杂度超过上限')
    return cost


def compile_bounded_pattern(pattern, flags=0, *, stdlib_flags=0, max_groups=32):
    # Parse with the compact stdlib representation before the regex extension
    # allocates its compiled repeat structures. Unsupported extensions fail closed.
    try:
        parsed = _parser.parse(pattern, stdlib_flags)
    except OverflowError as exc:
        raise RegexBudgetError('正则表达式重复次数超过编译上限') from exc
    if parsed.state.groups - 1 > max_groups:
        raise RegexBudgetError('捕获组数量超过上限')
    _pattern_cost(parsed)
    # User-controlled patterns must not accumulate in regex's global cache.
    return regex.compile(pattern, flags, cache_pattern=False)


def replacement_template(source, compiled):
    references, literals = _parser.parse_template(source, compiled)
    return (
        tuple(literals), dict(references), Counter(group for _index, group in references),
        sum(len(value) for value in literals if value is not None),
    )


def expand_bounded_replacement(match, template, max_length):
    if max_length < 0:
        raise RegexBudgetError('替换结果超过大小上限')
    literals, references, counts, literal_length = template
    # Resolve each capture only once. Repeating a large capture in the template
    # must be rejected before match.expand or a giant joined string is allocated.
    captures = {group: match.group(group) or '' for group in counts}
    length = literal_length + sum(len(captures[group]) * count for group, count in counts.items())
    if length > max_length:
        raise RegexBudgetError('替换结果超过大小上限')
    return ''.join(
        captures[references[index]] if value is None else value
        for index, value in enumerate(literals)
    )
