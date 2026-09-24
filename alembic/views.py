# mypy: allow-untyped-defs, allow-incomplete-defs
"""Declarative model for views that are managed by Alembic autogenerate.

This module provides the first version of view support, oriented towards
plain SQL views (e.g. SQLite ``CREATE VIEW``).  Materialized views,
routines, and automatic parsing of arbitrary SQL bodies to discover
dependencies are intentionally out of scope.

A collection of :class:`.ViewDefinition` and/or :class:`.RemovedView`
objects is passed to :meth:`.EnvironmentContext.configure` via the
``managed_views`` parameter, e.g.::

    from alembic.views import ViewDefinition, RemovedView

    context.configure(
        # ...
        target_metadata=mymetadata,
        managed_views=[
            ViewDefinition("v_base", "SELECT id, data FROM base_table"),
            ViewDefinition(
                "v_summary",
                "SELECT id, upper(data) AS data FROM v_base",
                depends_on=["v_base"],
            ),
            # a view that used to be managed and should now be dropped:
            RemovedView("v_legacy"),
        ],
    )

Only the views declared in this collection are managed.  Views that exist
in the database but are not declared are left untouched, and in particular
are never dropped.  To remove a view that was previously managed, declare
it with :class:`.RemovedView`; simply omitting it from the collection
makes it unmanaged, which preserves it.

"""

from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Sequence
import re

from . import util

__all__ = [
    "ViewDefinition",
    "RemovedView",
    "ManagedViews",
    "normalize_view_select",
]


_CREATE_VIEW_PREFIX = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?VIEW\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:"
    r'"(?:[^"]|"")*"'  # "double quoted" name
    r"|\[[^\]]*\]"  # [bracketed] name
    r"|`(?:[^`]|``)*`"  # `backquoted` name
    r"|[^\s]+"  # bare, possibly schema-qualified, name
    r")\s+AS\s+",
    re.IGNORECASE,
)


def normalize_view_select(sql: str) -> str:
    """Reduce a view definition to its bare SELECT text.

    Only reliable, structural differences are eliminated: a leading
    ``CREATE VIEW [IF NOT EXISTS] <name> AS`` wrapper, trailing
    semicolons, and leading/trailing whitespace.  The body of the
    statement is otherwise left verbatim - whitespace inside string
    literals is never collapsed, quoted identifiers are never unquoted,
    and keywords are never case-folded.
    """
    sql = sql.replace("\r\n", "\n").replace("\r", "\n").strip()
    match = _CREATE_VIEW_PREFIX.match(sql)
    if match:
        sql = sql[match.end() :]
    sql = sql.rstrip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


class ViewDefinition:
    """Declare a single plain view to be managed by Alembic.

    :param name: the view name.  The name is quoted appropriately for the
     target dialect when DDL is rendered, so names that require quoting
     (e.g. names containing spaces) are supported.
    :param selectable: the SQL ``SELECT`` statement for the view, as a
     plain string.  A leading ``CREATE VIEW ... AS`` wrapper is tolerated
     and is discarded when definitions are compared; the statement text
     itself is otherwise used and compared verbatim.
    :param depends_on: optional sequence of names of other *managed* views
     that this view selects from.  Dependencies must be declared
     explicitly; Alembic does not parse the SQL body to infer them.
     Tables and unmanaged views referenced by the SELECT do not need to
     be declared.  Every name given here must correspond to another
     :class:`.ViewDefinition` in the same collection, and dependencies
     must not form a cycle; both conditions raise an error before any
     SQL is executed.

    """

    def __init__(
        self,
        name: str,
        selectable: str,
        *,
        depends_on: Sequence[str] = (),
    ) -> None:
        if not name:
            raise util.CommandError("View definition requires a name")
        if not selectable or not selectable.strip():
            raise util.CommandError(
                f"View {name!r} requires a non-empty SELECT statement"
            )
        self.name = name
        self.selectable = selectable
        self.depends_on = tuple(depends_on)

    def __repr__(self) -> str:
        return f"ViewDefinition({self.name!r})"


class RemovedView:
    """Declare that a previously managed view should be dropped.

    Use this in the ``managed_views`` collection in place of the former
    :class:`.ViewDefinition` of the view when the view should be removed
    from the database.  The downgrade direction of the generated migration
    recreates the view using the SELECT statement read from the database
    at autogenerate time.

    """

    def __init__(self, name: str) -> None:
        if not name:
            raise util.CommandError("Removed view requires a name")
        self.name = name

    def __repr__(self) -> str:
        return f"RemovedView({self.name!r})"


class ManagedViews:
    """A validated collection of managed view declarations.

    Validates the declaration up front - before any comparison or SQL
    execution takes place - including duplicate names, dependencies on
    views that are not part of the collection, and dependency cycles.
    """

    def __init__(
        self, views: Sequence[ViewDefinition | RemovedView] = ()
    ) -> None:
        definitions: dict[str, ViewDefinition] = {}
        removed: dict[str, RemovedView] = {}
        for view in views:
            if isinstance(view, ViewDefinition):
                target = definitions
            elif isinstance(view, RemovedView):
                target = removed
            else:
                raise util.CommandError(
                    "managed_views entries must be ViewDefinition or "
                    f"RemovedView objects, got {view!r}"
                )
            if view.name in definitions or view.name in removed:
                raise util.CommandError(
                    f"View {view.name!r} is declared more than once in "
                    "managed_views"
                )
            target[view.name] = view

        self.definitions = definitions
        """mapping of view name to :class:`.ViewDefinition`, for views
        that should exist."""

        self.removed = removed
        """mapping of view name to :class:`.RemovedView`, for views that
        should be dropped."""

        self._creation_order = self._topological_order()

    @classmethod
    def coerce(
        cls,
        obj: "ManagedViews | Sequence[ViewDefinition | RemovedView] | None",
    ) -> "ManagedViews | None":
        """Coerce user input into a :class:`.ManagedViews` collection."""
        if obj is None or isinstance(obj, cls):
            return obj
        return cls(obj)

    def _topological_order(self) -> list[str]:
        dependencies = {
            name: set(defn.depends_on)
            for name, defn in self.definitions.items()
        }
        for name, deps in dependencies.items():
            for dep in sorted(deps):
                if dep not in self.definitions:
                    raise util.CommandError(
                        f"View {name!r} declares a dependency on view "
                        f"{dep!r}, which is not part of the managed_views "
                        "collection.  Dependencies must be other managed "
                        "views; tables and unmanaged views referenced by "
                        "the SELECT do not need to be declared."
                    )

        order: list[str] = []
        remaining = dict(dependencies)
        ready = [name for name in self.definitions if not dependencies[name]]
        while ready:
            name = ready.pop(0)
            order.append(name)
            del remaining[name]
            for other, deps in remaining.items():
                if name in deps:
                    deps.discard(name)
                    if not deps:
                        ready.append(other)
        if remaining:
            raise util.CommandError(
                "Dependency cycle detected among managed views: "
                + ", ".join(sorted(remaining))
            )
        return order

    def creation_order(self) -> Iterator[str]:
        """Yield managed view names so that dependencies come first."""
        return iter(self._creation_order)

    def drop_order(self) -> Iterator[str]:
        """Yield managed view names so that dependents come first."""
        return reversed(self._creation_order)

    def dependents_closure(self, names: set[str]) -> set[str]:
        """Return all managed views that directly or transitively depend
        on any of the given view names."""
        dependents: dict[str, list[str]] = {}
        for name, defn in self.definitions.items():
            for dep in defn.depends_on:
                dependents.setdefault(dep, []).append(name)
        closure: set[str] = set()
        stack = list(names)
        while stack:
            name = stack.pop()
            for dependent in dependents.get(name, ()):
                if dependent not in closure:
                    closure.add(dependent)
                    stack.append(dependent)
        return closure
