"""Parsed-SQL allowlist for model-generated report queries.

The query text is parsed with PostgreSQL's own parser (libpg_query via pglast). The
tree must be one plain ``SELECT`` built only from allowlisted node types, reading only
allowlisted ``reporting`` views and calling only allowlisted functions. The executor
then runs the *deparsed* validated tree, not the original text, so what ran is what
was checked.

This is one layer. The database role, read-only transaction, row-level security and
timeouts enforce the same limits independently (ADR 0201); an LLM is never asked
whether SQL is safe.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.stream import RawStream

MAX_SQL_CHARS = 4000
MAX_NODES = 1500

ALLOWED_SCHEMA = "reporting"
ALLOWED_RELATIONS = frozenset(
    {"sales_orders", "sales_order_lines", "sales_invoices", "sales_products", "sales_customers"}
)

ALLOWED_FUNCTIONS = frozenset(
    {
        "sum",
        "count",
        "avg",
        "min",
        "max",
        "round",
        "abs",
        "date_trunc",
        "extract",
        "date_part",
        "make_date",
        "lower",
        "upper",
    }
)

ALLOWED_TYPES = frozenset(
    {
        "date",
        "numeric",
        "int2",
        "int4",
        "int8",
        "integer",
        "bigint",
        "smallint",
        "text",
        "varchar",
        "interval",
        "bool",
    }
)

FORBIDDEN_COLUMNS = frozenset({"ctid", "xmin", "xmax", "cmin", "cmax", "tableoid", "oid"})

# Node types that may appear anywhere in an accepted tree. Everything else (DML,
# utility statements, SET, locking clauses, INTO, window definitions, SQL value
# functions such as current_user, parameters, function scans, TABLESAMPLE ...) is
# rejected by not being listed.
ALLOWED_NODES: frozenset[type[ast.Node]] = frozenset(
    {
        ast.RawStmt,
        ast.SelectStmt,
        ast.ResTarget,
        ast.ColumnRef,
        ast.String,
        ast.Integer,
        ast.Float,
        ast.Boolean,
        ast.A_Star,
        ast.A_Const,
        ast.FuncCall,
        ast.A_Expr,
        ast.BoolExpr,
        ast.NullTest,
        ast.CoalesceExpr,
        ast.CaseExpr,
        ast.CaseWhen,
        ast.TypeCast,
        ast.TypeName,
        ast.RangeVar,
        ast.JoinExpr,
        ast.Alias,
        ast.SortBy,
        ast.WithClause,
        ast.CommonTableExpr,
        ast.SubLink,
        ast.RangeSubselect,
        ast.MinMaxExpr,
    }
)


class SqlRejected(ValueError):
    """The SQL is outside the supported surface. ``code`` is safe to log and to feed
    back to the model in a repair prompt; it never contains row data."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ValidatedSql:
    original: str
    statement: ast.SelectStmt
    deparsed: str
    relations: frozenset[str]


def _children(node: ast.Node) -> Iterator[Any]:
    for attr in node:
        yield getattr(node, attr)


def walk(value: Any) -> Iterator[ast.Node]:
    """Yield every AST node under ``value`` (depth first)."""

    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, ast.Node):
            yield item
            stack.extend(_children(item))
        elif isinstance(item, list | tuple):
            stack.extend(item)


def _names(items: Any) -> list[str]:
    return [i.sval for i in items or () if isinstance(i, ast.String)]


def _strip_trailing_semicolon(sql: str) -> str:
    text = sql.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def validate_sql(sql: str) -> ValidatedSql:  # noqa: PLR0915 - one flat allowlist pass
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected("empty_sql")
    if len(sql) > MAX_SQL_CHARS:
        raise SqlRejected("sql_too_long", f"limit {MAX_SQL_CHARS} characters")
    if "\x00" in sql:
        raise SqlRejected("invalid_characters")
    try:
        statements = parse_sql(sql)
    except ParseError as exc:
        raise SqlRejected("parse_error", str(exc).split("\n", 1)[0][:200]) from exc
    if len(statements) != 1:
        raise SqlRejected("multiple_statements", f"{len(statements)} statements")
    stmt = statements[0].stmt
    if not isinstance(stmt, ast.SelectStmt):
        raise SqlRejected("not_a_select", type(stmt).__name__)

    cte_names: set[str] = set()
    for node in walk(stmt):
        if isinstance(node, ast.CommonTableExpr):
            if not isinstance(node.ctequery, ast.SelectStmt):
                raise SqlRejected("writable_cte", type(node.ctequery).__name__)
            name = node.ctename
            if name in ALLOWED_RELATIONS:
                raise SqlRejected("cte_shadows_relation", name)
            cte_names.add(name)

    relations: set[str] = set()
    for count, node in enumerate(walk(stmt), start=1):
        if count > MAX_NODES:
            raise SqlRejected("sql_too_complex")
        kind = type(node)
        if kind not in ALLOWED_NODES:
            raise SqlRejected("disallowed_construct", kind.__name__)
        if isinstance(node, ast.SelectStmt):
            if node.intoClause is not None:
                raise SqlRejected("select_into")
            if node.lockingClause:
                raise SqlRejected("locking_clause")
            if node.windowClause:
                raise SqlRejected("window_clause")
            if node.valuesLists:
                raise SqlRejected("values_list")
        elif isinstance(node, ast.WithClause):
            if node.recursive:
                raise SqlRejected("recursive_cte")
        elif isinstance(node, ast.RangeVar):
            relations.add(_check_relation(node, cte_names))
        elif isinstance(node, ast.RangeSubselect):
            if node.lateral:
                raise SqlRejected("lateral_subquery")
        elif isinstance(node, ast.FuncCall):
            _check_function(node)
        elif isinstance(node, ast.TypeCast):
            _check_type(node.typeName)
        elif isinstance(node, ast.ColumnRef):
            for name in _names(node.fields):
                if name.lower() in FORBIDDEN_COLUMNS or name.lower().startswith("pg_"):
                    raise SqlRejected("system_column", name)

    deparsed = RawStream()(stmt)
    # Defence against parser/deparser drift: the deparsed text must parse back to
    # exactly one SELECT.
    reparsed = parse_sql(deparsed)
    if len(reparsed) != 1 or not isinstance(reparsed[0].stmt, ast.SelectStmt):
        raise SqlRejected("deparse_mismatch")
    return ValidatedSql(
        original=_strip_trailing_semicolon(sql),
        statement=stmt,
        deparsed=deparsed,
        relations=frozenset(relations),
    )


def _check_relation(node: ast.RangeVar, cte_names: set[str]) -> str:
    if node.catalogname:
        raise SqlRejected("catalog_qualified_relation", node.catalogname)
    schema, name = node.schemaname, node.relname
    if schema is None and name in cte_names:
        return name
    if schema not in (None, ALLOWED_SCHEMA):
        raise SqlRejected("relation_not_allowed", f"{schema}.{name}")
    if name not in ALLOWED_RELATIONS:
        raise SqlRejected("relation_not_allowed", name)
    return name


def _check_function(node: ast.FuncCall) -> None:
    parts = _names(node.funcname)
    if len(parts) == 2 and parts[0] == "pg_catalog":
        parts = parts[1:]
    if len(parts) != 1 or parts[0].lower() not in ALLOWED_FUNCTIONS:
        raise SqlRejected("function_not_allowed", ".".join(_names(node.funcname)))
    if node.over is not None:
        raise SqlRejected("window_function")
    if node.agg_within_group or node.func_variadic or node.agg_order:
        raise SqlRejected("function_form_not_allowed", parts[0])


def _check_type(type_name: ast.TypeName | None) -> None:
    if type_name is None:
        raise SqlRejected("type_not_allowed")
    parts = _names(type_name.names)
    if len(parts) == 2 and parts[0] == "pg_catalog":
        parts = parts[1:]
    if len(parts) != 1 or parts[0].lower() not in ALLOWED_TYPES or type_name.arrayBounds:
        raise SqlRejected("type_not_allowed", ".".join(_names(type_name.names)))
