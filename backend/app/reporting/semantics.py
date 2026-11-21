"""Deterministic verification that a validated query *means* the requested report.

Executing correctly is not enough: a perfectly summed wrong window, an all-status
total, gross labelled as net or a line-level join that double counts orders must all
fail. This module reads the meaning off the parsed tree and compares it with the
trusted intent and tenant profile. It never compares with a stored answer, so it
works for any supported period.

Supported shape (anything else is rejected as unverifiable and sent back for repair):

    SELECT count(*) AS order_count,
           sum(o.subtotal) AS net_sales, sum(o.tax) AS tax_amount, sum(o.total) AS gross_sales
      FROM reporting.sales_orders o [JOIN <view> ON <key equality>]...
     WHERE o.order_date >= DATE 'start' AND o.order_date <= DATE 'end'   -- or < next day, BETWEEN
       AND o.status = 'FULFILLED'                                       -- or IN / NOT IN
       [AND o.currency = 'USD']

A join-multiplicity probe (``probe_sql``) is executed with the same role and context:
if the joined rows outnumber distinct orders, the aggregates double count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from pglast import ast, parse_sql
from pglast.enums import A_Expr_Kind, BoolExprType, SetOperation
from pglast.stream import RawStream

from app.reporting.contracts import OUTPUT_COLUMNS, ReportProfile, SalesReportIntent
from app.reporting.sql_guard import ValidatedSql, walk

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
FACT = "sales_orders"
DATE_COLUMNS = {
    "sales_orders": {"order_date"},
    "sales_invoices": {"issue_date", "due_date"},
    "sales_customers": {"since_date"},
}
# Allowed join keys: (relation, column) pairs that identify an order or a parent row.
JOIN_KEYS = {
    ("sales_orders", "id"): {("sales_order_lines", "order_id"), ("sales_invoices", "order_id")},
    ("sales_orders", "customer_id"): {("sales_customers", "id")},
    ("sales_order_lines", "product_id"): {("sales_products", "id")},
}
# measure expression (on the fact relation) -> meaning
MEASURES = {
    ("col", "subtotal"): "net",
    ("-", "total", "tax"): "net",
    ("col", "tax"): "tax",
    ("-", "total", "subtotal"): "tax",
    ("col", "total"): "gross",
    ("+", "subtotal", "tax"): "gross",
    ("+", "tax", "subtotal"): "gross",
}
EXPECTED_MEANING = {"net_sales": "net", "tax_amount": "tax", "gross_sales": "gross"}
MISREAD = {
    ("net_sales", "gross"): "gross_described_as_net",
    ("gross_sales", "net"): "net_described_as_gross",
}


@dataclass
class QuerySemantics:
    failures: list[str] = field(default_factory=list)
    fact_alias: str = FACT
    window: tuple[str | None, str | None] = (None, None)
    statuses: list[str] | None = None
    currency_filter: str | None = None
    joins: list[str] = field(default_factory=list)
    measures: dict[str, str] = field(default_factory=dict)
    probe_sql: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failures

    def fail(self, code: str) -> None:
        if code not in self.failures:
            self.failures.append(code)

    def as_record(self) -> dict[str, Any]:
        return {
            "failures": list(self.failures),
            "window": list(self.window),
            "statuses": self.statuses,
            "currency_filter": self.currency_filter,
            "joins": self.joins,
            "measures": self.measures,
        }


class _Scope:
    """Relations in the FROM clause by alias."""

    def __init__(self) -> None:
        self.by_alias: dict[str, str] = {}

    def add(self, node: ast.RangeVar) -> str:
        alias = node.alias.aliasname if node.alias is not None else node.relname
        self.by_alias[alias] = node.relname
        return alias

    def resolve(self, ref: ast.ColumnRef) -> tuple[str, str] | None:
        names = [f.sval for f in ref.fields if isinstance(f, ast.String)]
        if len(names) == 2 and names[0] in self.by_alias:
            return self.by_alias[names[0]], names[1]
        if len(names) == 1 and len(set(self.by_alias.values())) == 1:
            return next(iter(self.by_alias.values())), names[0]
        return None


def _conjuncts(node: Any) -> list[Any]:
    if node is None:
        return []
    if isinstance(node, ast.BoolExpr) and node.boolop == BoolExprType.AND_EXPR:
        out: list[Any] = []
        for arg in node.args:
            out.extend(_conjuncts(arg))
        return out
    return [node]


def _unwrap_cast(node: Any) -> Any:
    while isinstance(node, ast.TypeCast):
        node = node.arg
    return node


def _date_literal(node: Any) -> date | None:
    node_type_date = (
        isinstance(node, ast.TypeCast)
        and node.typeName is not None
        and [n.sval for n in node.typeName.names][-1] == "date"
    )
    inner = node.arg if isinstance(node, ast.TypeCast) else node
    if isinstance(inner, ast.A_Const) and isinstance(inner.val, ast.String):
        text = inner.val.sval
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            try:
                return date.fromisoformat(text)
            except ValueError:
                return None
        return None
    if isinstance(inner, ast.FuncCall) and not node_type_date:
        name = [n.sval for n in inner.funcname][-1]
        args = inner.args or ()
        if name == "make_date" and len(args) == 3:
            values = [_int_literal(a) for a in args]
            if all(v is not None for v in values):
                try:
                    return date(*[int(v) for v in values if v is not None])
                except ValueError:
                    return None
    return None


def _int_literal(node: Any) -> int | None:
    node = _unwrap_cast(node)
    if isinstance(node, ast.A_Const) and isinstance(node.val, ast.Integer):
        return int(node.val.ival)
    return None


def _str_literal(node: Any) -> str | None:
    node = _unwrap_cast(node)
    if isinstance(node, ast.A_Const) and isinstance(node.val, ast.String):
        return str(node.val.sval)
    return None


def cast_preserves_value(node: ast.TypeCast, kind: str) -> bool:
    """True only for casts that cannot change a report value.

    Money (sums of ``numeric(14,2)``): ``numeric`` without a type modifier, or
    ``numeric(p, 2)``; any other precision/scale rounds or truncates. Counts:
    ``bigint``/``int8``/``numeric`` without modifiers. Every other cast (integer, float,
    text, ``numeric(p)``, ``numeric(p, 0)``...) is value-changing and rejected.
    """

    type_name = node.typeName
    if type_name is None or type_name.arrayBounds:
        return False
    name = [n.sval for n in type_name.names][-1].lower()
    mods = [_int_literal(m) for m in type_name.typmods or ()]
    if any(m is None for m in mods):
        return False
    if kind == "count":
        return name in ("int8", "bigint", "numeric") and not mods
    return name == "numeric" and (not mods or (len(mods) == 2 and mods[1] == 2))


def _op(node: ast.A_Expr) -> str:
    return ".".join(n.sval for n in node.name)


_FLIP = {">=": "<=", "<=": ">=", ">": "<", "<": ">", "=": "="}


class SemanticVerifier:
    def __init__(self, profile: ReportProfile, intent: SalesReportIntent) -> None:
        self.profile = profile
        self.intent = intent

    def verify(self, validated: ValidatedSql) -> QuerySemantics:
        result = QuerySemantics()
        stmt = validated.statement
        if stmt.op != SetOperation.SETOP_NONE or stmt.withClause is not None:
            result.fail("unverifiable_structure")
            return result
        if any(isinstance(n, ast.SubLink | ast.RangeSubselect) for n in walk(stmt)):
            result.fail("unverifiable_structure")
            return result
        if stmt.groupClause or stmt.havingClause is not None or stmt.distinctClause:
            result.fail("wrong_grain")
        if stmt.limitOffset is not None:
            result.fail("unverifiable_structure")

        scope = _Scope()
        if not self._from_clause(stmt.fromClause, scope, result):
            return result
        facts = [a for a, rel in scope.by_alias.items() if rel == FACT]
        if len(facts) != 1:
            result.fail("fact_relation_missing_or_repeated")
            return result
        result.fact_alias = facts[0]
        if not _IDENT.match(result.fact_alias):
            result.fail("unverifiable_structure")
            return result

        self._where(stmt.whereClause, scope, result)
        self._targets(stmt.targetList, scope, result)
        if result.ok or set(result.failures) <= {"join_fan_out"}:
            result.probe_sql = self._probe(stmt, result.fact_alias)
        return result

    # -- FROM ----------------------------------------------------------------------

    def _from_clause(self, items: Any, scope: _Scope, result: QuerySemantics) -> bool:
        if not items or len(items) != 1:
            result.fail("unverifiable_structure")  # implicit cross joins are not supported
            return False
        return self._from_item(items[0], scope, result)

    def _from_item(self, node: Any, scope: _Scope, result: QuerySemantics) -> bool:
        if isinstance(node, ast.RangeVar):
            scope.add(node)
            return True
        if isinstance(node, ast.JoinExpr):
            if node.usingClause or node.isNatural or node.quals is None:
                result.fail("unverifiable_join")
                return False
            if not (
                self._from_item(node.larg, scope, result)
                and self._from_item(node.rarg, scope, result)
            ):
                return False
            for cond in _conjuncts(node.quals):
                key = self._join_key(cond, scope)
                if key is None:
                    result.fail("filter_outside_report_definition")
                    return False
                result.joins.append(key)
            return True
        result.fail("unverifiable_structure")
        return False

    def _join_key(self, cond: Any, scope: _Scope) -> str | None:
        if not (isinstance(cond, ast.A_Expr) and cond.kind == A_Expr_Kind.AEXPR_OP):
            return None
        if _op(cond) != "=":
            return None
        if not (isinstance(cond.lexpr, ast.ColumnRef) and isinstance(cond.rexpr, ast.ColumnRef)):
            return None
        left, right = scope.resolve(cond.lexpr), scope.resolve(cond.rexpr)
        if left is None or right is None:
            return None
        for a, b in ((left, right), (right, left)):
            if b in JOIN_KEYS.get(a, set()):
                return f"{a[0]}.{a[1]}={b[0]}.{b[1]}"
        return None

    # -- WHERE ---------------------------------------------------------------------

    def _where(self, where: Any, scope: _Scope, result: QuerySemantics) -> None:
        lo: date | None = None
        hi: date | None = None
        statuses: set[str] | None = None
        domain = set(self.profile.status_domain)
        saw_date = False
        for cond in _conjuncts(where):
            kind = self._classify(cond, scope)
            if kind is None:
                result.fail("filter_outside_report_definition")
                continue
            what, column, payload = kind
            if what == "date":
                if column != (FACT, self.profile.date_basis):
                    result.fail("wrong_date_column")
                    continue
                saw_date = True
                for bound_lo, bound_hi in payload:
                    if bound_lo is not None:
                        lo = bound_lo if lo is None else max(lo, bound_lo)
                    if bound_hi is not None:
                        hi = bound_hi if hi is None else min(hi, bound_hi)
            elif what == "status":
                if column != (FACT, "status"):
                    result.fail("filter_outside_report_definition")
                    continue
                allowed = payload & domain
                statuses = allowed if statuses is None else statuses & allowed
            elif what == "currency":
                if column != (FACT, "currency") or payload != self.profile.currency:
                    result.fail("currency_mismatch")
                    continue
                result.currency_filter = payload
        result.window = (lo.isoformat() if lo else None, hi.isoformat() if hi else None)
        if not saw_date:
            result.fail("missing_date_filter")
        elif lo is None or hi is None:
            result.fail("unbounded_period")
        elif (lo, hi) != (self.intent.period.start, self.intent.period.end):
            result.fail("wrong_period")
        effective = statuses if statuses is not None else domain
        result.statuses = sorted(effective)
        if effective != set(self.intent.included_statuses):
            result.fail("wrong_status_filter")

    def _classify(self, cond: Any, scope: _Scope) -> tuple[str, tuple[str, str], Any] | None:
        if isinstance(cond, ast.A_Expr):
            if not isinstance(cond.lexpr, ast.ColumnRef):
                if cond.kind == A_Expr_Kind.AEXPR_OP and isinstance(cond.rexpr, ast.ColumnRef):
                    flipped = _FLIP.get(_op(cond))
                    if flipped is None:
                        return None
                    return self._classify(
                        ast.A_Expr(
                            kind=cond.kind,
                            name=(ast.String(sval=flipped),),
                            lexpr=cond.rexpr,
                            rexpr=cond.lexpr,
                        ),
                        scope,
                    )
                return None
            column = scope.resolve(cond.lexpr)
            if column is None:
                return None
            rel, col = column
            if col in DATE_COLUMNS.get(rel, set()):
                bounds = self._date_bounds(cond)
                return ("date", column, bounds) if bounds is not None else None
            if col == "status":
                values = self._status_values(cond)
                return ("status", column, values) if values is not None else None
            if col == "currency" and cond.kind == A_Expr_Kind.AEXPR_OP and _op(cond) == "=":
                value = _str_literal(cond.rexpr)
                return ("currency", column, value) if value is not None else None
        return None

    def _date_bounds(self, cond: ast.A_Expr) -> list[tuple[date | None, date | None]] | None:
        if cond.kind == A_Expr_Kind.AEXPR_BETWEEN:
            bounds_nodes: Any = cond.rexpr
            lo_node, hi_node = bounds_nodes[0], bounds_nodes[1]
            lo, hi = _date_literal(lo_node), _date_literal(hi_node)
            return [(lo, hi)] if lo is not None and hi is not None else None
        if cond.kind != A_Expr_Kind.AEXPR_OP:
            return None
        value = _date_literal(cond.rexpr)
        if value is None:
            return None
        one = timedelta(days=1)
        table: dict[str, list[tuple[date | None, date | None]]] = {
            ">=": [(value, None)],
            ">": [(value + one, None)],
            "<=": [(None, value)],
            "<": [(None, value - one)],
            "=": [(value, value)],
        }
        return table.get(_op(cond))

    def _status_values(self, cond: ast.A_Expr) -> set[str] | None:
        domain = set(self.profile.status_domain)
        if cond.kind == A_Expr_Kind.AEXPR_OP and _op(cond) in ("=", "<>", "!="):
            value = _str_literal(cond.rexpr)
            if value is None:
                return None
            return {value} if _op(cond) == "=" else domain - {value}
        if cond.kind == A_Expr_Kind.AEXPR_IN:
            values = [_str_literal(v) for v in cond.rexpr]
            if any(v is None for v in values):
                return None
            chosen = {v for v in values if v is not None}
            return chosen if _op(cond) == "=" else domain - chosen
        return None

    # -- SELECT list ---------------------------------------------------------------

    def _targets(self, targets: Any, scope: _Scope, result: QuerySemantics) -> None:
        names = [t.name for t in targets or ()]
        if sorted(n or "" for n in names) != sorted(OUTPUT_COLUMNS):
            result.fail("wrong_output_columns")
            return
        for target in targets:
            kind = "count" if target.name == "order_count" else "money"
            expr = self._strip_wrappers(target.val, kind, result)
            if expr is None:
                continue
            if target.name == "order_count":
                if not self._is_order_count(expr, scope):
                    result.fail("wrong_order_count")
                else:
                    result.measures["order_count"] = "count_orders"
                continue
            meaning = self._measure(expr, scope, result)
            if meaning is None:
                result.fail("measure_not_in_definition")
                continue
            result.measures[target.name] = meaning
            expected = EXPECTED_MEANING[target.name]
            if meaning != expected:
                result.fail(MISREAD.get((target.name, meaning), "wrong_tax_basis"))

    def _strip_wrappers(self, node: Any, kind: str, result: QuerySemantics) -> Any:
        """Remove value-preserving wrappers: lossless casts, ``coalesce(x, 0)`` and
        ``round(x, 2)`` (sums of 2-decimal amounts already have scale 2). A
        value-changing cast fails verification instead of being ignored."""

        while True:
            if isinstance(node, ast.TypeCast):
                if not cast_preserves_value(node, kind):
                    result.fail("value_changing_cast")
                    return None
                node = node.arg
            elif isinstance(node, ast.CoalesceExpr) and len(node.args) == 2:
                fallback = _unwrap_cast(node.args[1])
                is_zero = isinstance(fallback, ast.A_Const) and isinstance(
                    fallback.val, ast.Integer | ast.Float
                )
                if not is_zero:
                    return node
                node = node.args[0]
            elif (
                isinstance(node, ast.FuncCall)
                and [n.sval for n in node.funcname][-1] == "round"
                and len(node.args or ()) == 2
                and _int_literal(node.args[1]) == 2
            ):
                node = node.args[0]
            else:
                return node

    def _is_order_count(self, node: Any, scope: _Scope) -> bool:
        if not isinstance(node, ast.FuncCall) or [n.sval for n in node.funcname][-1] != "count":
            return False
        if node.agg_filter is not None:
            return False
        if node.agg_star:
            return True
        args: Any = node.args or ()
        if len(args) == 1 and isinstance(args[0], ast.ColumnRef):
            return scope.resolve(args[0]) == (FACT, "id")
        return False

    def _measure(self, node: Any, scope: _Scope, result: QuerySemantics) -> str | None:
        if not isinstance(node, ast.FuncCall) or [n.sval for n in node.funcname][-1] != "sum":
            return None
        if node.agg_distinct:
            result.fail("distinct_aggregate")
            return None
        if node.agg_filter is not None or len(node.args or ()) != 1:
            return None
        arg = node.args[0]
        while isinstance(arg, ast.TypeCast):
            # A cast inside the aggregate argument rounds every row before summing.
            if not cast_preserves_value(arg, "money"):
                result.fail("value_changing_cast")
                return None
            arg = arg.arg
        key: tuple[str, ...] | None = None
        if isinstance(arg, ast.ColumnRef):
            col = scope.resolve(arg)
            if col is not None and col[0] == FACT:
                key = ("col", col[1])
        elif isinstance(arg, ast.A_Expr) and arg.kind == A_Expr_Kind.AEXPR_OP:
            left = scope.resolve(arg.lexpr) if isinstance(arg.lexpr, ast.ColumnRef) else None
            right = scope.resolve(arg.rexpr) if isinstance(arg.rexpr, ast.ColumnRef) else None
            if left and right and left[0] == FACT and right[0] == FACT:
                key = (_op(arg), left[1], right[1])
        return MEASURES.get(key) if key else None

    # -- join multiplicity probe ---------------------------------------------------

    def _probe(self, stmt: ast.SelectStmt, alias: str) -> str:
        targets = parse_sql(
            f"SELECT count(*) AS joined_rows, count(DISTINCT {alias}.id) AS distinct_orders,"
            f" count(DISTINCT {alias}.currency) AS currencies,"
            f" min({alias}.currency) AS currency"
        )[0].stmt.targetList
        probe = ast.SelectStmt(
            targetList=targets,
            fromClause=stmt.fromClause,
            whereClause=stmt.whereClause,
            limitOption=stmt.limitOption,
            op=SetOperation.SETOP_NONE,
        )
        return str(RawStream()(probe))


def check_probe(
    probe_row: dict[str, Any], profile: ReportProfile, semantics: QuerySemantics
) -> None:
    """Apply the data-dependent checks to the probe's single result row."""

    joined, distinct = int(probe_row["joined_rows"]), int(probe_row["distinct_orders"])
    if joined != distinct:
        semantics.fail("join_fan_out")
    currencies = int(probe_row["currencies"])
    if currencies > 1 or (currencies == 1 and probe_row["currency"] != profile.currency):
        semantics.fail("currency_mismatch")
