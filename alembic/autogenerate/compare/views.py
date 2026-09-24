# mypy: allow-untyped-calls

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...operations import ops
from ...util import DispatchPriority
from ...util import PriorityDispatchResult
from ...views import ManagedViews
from ...views import normalize_view_select

if TYPE_CHECKING:
    from ...autogenerate.api import AutogenContext
    from ...operations.ops import UpgradeOps
    from ...runtime.plugins import Plugin


log = logging.getLogger(__name__)


def _autogen_for_views(
    autogen_context: AutogenContext,
    upgrade_ops: UpgradeOps,
    schemas: set[str | None],
) -> PriorityDispatchResult:
    managed = ManagedViews.coerce(autogen_context.opts.get("managed_views"))
    if not managed or None not in schemas:
        return PriorityDispatchResult.CONTINUE

    inspector = autogen_context.inspector

    conn_views: dict[str, str] = {}
    for view_name in inspector.get_view_names(schema=None):
        if not autogen_context.run_name_filters(
            view_name, "view", {"schema_name": None}
        ):
            continue
        definition = inspector.get_view_definition(view_name, schema=None)
        if definition is None:
            continue
        conn_views[view_name] = definition

    _compare_views(autogen_context, upgrade_ops, managed, conn_views)
    return PriorityDispatchResult.CONTINUE


def _compare_views(
    autogen_context: AutogenContext,
    upgrade_ops: UpgradeOps,
    managed: ManagedViews,
    conn_views: dict[str, str],
) -> None:
    # only views declared in managed_views are compared; views that are
    # present in the database but not declared are never dropped.
    changed = {
        name
        for name, defn in managed.definitions.items()
        if name in conn_views
        and normalize_view_select(conn_views[name])
        != normalize_view_select(defn.selectable)
    }

    # when a view changes, every managed view that directly or
    # transitively depends on it must be dropped and recreated as well.
    affected = set(changed) | managed.dependents_closure(changed)

    # drop in dependency order, dependents first
    for name in managed.drop_order():
        if name in affected and name in conn_views:
            old_select = normalize_view_select(conn_views[name])
            log.info("Detected changed view '%s'", name)
            upgrade_ops.ops.append(
                ops.DropViewOp(
                    name,
                    selectable=old_select,
                    depends_on=managed.definitions[name].depends_on,
                )
            )

    # explicitly removed views; the SELECT read from the database keeps
    # the generated downgrade able to recreate them.
    for name in managed.removed:
        if name in conn_views:
            old_select = normalize_view_select(conn_views[name])
            log.info("Detected removed view '%s'", name)
            upgrade_ops.ops.append(
                ops.DropViewOp(name, selectable=old_select)
            )

    # (re)create in the opposite direction, dependencies first
    for name in managed.creation_order():
        if name in affected or name not in conn_views:
            defn = managed.definitions[name]
            if name not in conn_views:
                log.info("Detected added view '%s'", name)
            upgrade_ops.ops.append(
                ops.CreateViewOp(
                    defn.name, defn.selectable, depends_on=defn.depends_on
                )
            )


def setup(plugin: Plugin) -> None:
    plugin.add_autogenerate_comparator(
        _autogen_for_views,
        "schema",
        "views",
        priority=DispatchPriority.LAST,
    )
