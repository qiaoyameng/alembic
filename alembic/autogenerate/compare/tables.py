# mypy: allow-untyped-calls

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import logging
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy import schema as sa_schema
from sqlalchemy.util import OrderedSet

from .util import _InspectorConv
from ... import util
from ...operations import ops
from ...util import PriorityDispatchResult

if TYPE_CHECKING:
    from sqlalchemy.engine.reflection import Inspector
    from sqlalchemy.sql.elements import quoted_name
    from sqlalchemy.sql.schema import Table

    from ...autogenerate.api import AutogenContext
    from ...operations.ops import ModifyTableOps
    from ...operations.ops import UpgradeOps
    from ...runtime.plugins import Plugin


log = logging.getLogger(__name__)


def _autogen_for_tables(
    autogen_context: AutogenContext,
    upgrade_ops: UpgradeOps,
    schemas: set[str | None],
) -> PriorityDispatchResult:
    inspector = autogen_context.inspector

    conn_table_names: set[tuple[str | None, str]] = set()
    conn_available_tables: set[tuple[str | None, str]] = set()

    version_table_schema = (
        autogen_context.migration_context.version_table_schema
    )
    version_table = autogen_context.migration_context.version_table

    for schema_name in schemas:
        tables = available = set(inspector.get_table_names(schema=schema_name))
        conn_available_tables.update(
            (schema_name, tname) for tname in available
        )
        if schema_name == version_table_schema:
            tables = tables.difference(
                [autogen_context.migration_context.version_table]
            )

        tablenames = [
            tname
            for tname in tables
            if autogen_context.run_name_filters(
                tname, "table", {"schema_name": schema_name}
            )
        ]

        conn_table_names.update((schema_name, tname) for tname in tablenames)

        inspector = autogen_context.inspector
        insp = _InspectorConv(inspector)
        insp.pre_cache_tables(schema_name, tablenames, available)

    metadata_table_names = OrderedSet(
        [(table.schema, table.name) for table in autogen_context.sorted_tables]
    ).difference([(version_table_schema, version_table)])

    if autogen_context.column_renames:
        _validate_column_renames(
            autogen_context,
            conn_available_tables,
            conn_table_names,
        )

    _compare_tables(
        conn_table_names,
        metadata_table_names,
        inspector,
        upgrade_ops,
        autogen_context,
    )

    return PriorityDispatchResult.CONTINUE


def _validate_column_renames(
    autogen_context: AutogenContext,
    conn_available_tables: set[tuple[str | None, str]],
    conn_table_names: set[tuple[str | None, str]],
) -> None:
    """Validate all ``column_renames`` hints up front.

    Every hint has to be consistent with both the reflected database
    structure and the target metadata; any invalid hint raises
    before any operations are generated, so that hints are never
    partially accepted.   Hints that refer to tables excluded by
    ``include_name`` / ``include_object`` filters are ignored rather
    than being pulled back into the comparison.

    """
    inspector = autogen_context.inspector
    default_schema = inspector.bind.dialect.default_schema_name

    metadata_tables = {}
    for table in autogen_context.sorted_tables:
        table_schema = None if table.schema == default_schema else table.schema
        metadata_tables.setdefault((table_schema, table.name), table)

    errors = []
    for (schema, tname), mapping in sorted(
        autogen_context.column_renames.items(),
        key=lambda item: (item[0][0] or "", item[0][1]),
    ):
        name = "%s.%s" % (schema, tname) if schema else tname
        if (schema, tname) not in conn_available_tables:
            errors.append(
                "column_renames hint for table %r: table does not "
                "exist in the database" % (name,)
            )
            continue
        if (schema, tname) not in metadata_tables:
            errors.append(
                "column_renames hint for table %r: table is not "
                "present in the target metadata" % (name,)
            )
            continue
        if (schema, tname) not in conn_table_names:
            # table is excluded by include_name / include_object
            # filters; the hint is ignored
            continue

        reflected_columns = {
            c["name"] for c in inspector.get_columns(tname, schema=schema)
        }
        metadata_table = metadata_tables[(schema, tname)]
        metadata_columns = {c.name for c in metadata_table.c if not c.system}

        new_names = list(mapping.values())
        if len(set(new_names)) != len(new_names):
            errors.append(
                "column_renames hint for table %r: multiple columns "
                "may not be renamed to the same target name" % (name,)
            )
        if set(mapping).intersection(new_names):
            errors.append(
                "column_renames hint for table %r: rename cycles "
                "are not supported" % (name,)
            )
        for old_name, new_name in mapping.items():
            if old_name not in reflected_columns:
                errors.append(
                    "column_renames hint for table %r: column %r does "
                    "not exist in the database" % (name, old_name)
                )
            if new_name not in metadata_columns:
                errors.append(
                    "column_renames hint for table %r: column %r does "
                    "not exist in the target metadata" % (name, new_name)
                )
            if old_name in metadata_columns:
                errors.append(
                    "column_renames hint for table %r: column %r is "
                    "still present in the target metadata" % (name, old_name)
                )
            if new_name in reflected_columns:
                errors.append(
                    "column_renames hint for table %r: column %r already "
                    "exists in the database" % (name, new_name)
                )

    if errors:
        raise util.CommandError("; ".join(errors))


def _compare_tables(
    conn_table_names: set[tuple[str | None, str]],
    metadata_table_names: set[tuple[str | None, str]],
    inspector: Inspector,
    upgrade_ops: UpgradeOps,
    autogen_context: AutogenContext,
) -> None:
    default_schema = inspector.bind.dialect.default_schema_name

    # tables coming from the connection will not have "schema"
    # set if it matches default_schema_name; so we need a list
    # of table names from local metadata that also have "None" if schema
    # == default_schema_name.  Most setups will be like this anyway but
    # some are not (see #170)
    metadata_table_names_no_dflt_schema = OrderedSet(
        [
            (schema if schema != default_schema else None, tname)
            for schema, tname in metadata_table_names
        ]
    )

    # to adjust for the MetaData collection storing the tables either
    # as "schemaname.tablename" or just "tablename", create a new lookup
    # which will match the "non-default-schema" keys to the Table object.
    tname_to_table = {
        no_dflt_schema: autogen_context.table_key_to_table[
            sa_schema._get_table_key(tname, schema)
        ]
        for no_dflt_schema, (schema, tname) in zip(
            metadata_table_names_no_dflt_schema, metadata_table_names
        )
    }
    metadata_table_names = metadata_table_names_no_dflt_schema

    for s, tname in metadata_table_names.difference(conn_table_names):
        name = "%s.%s" % (s, tname) if s else tname
        metadata_table = tname_to_table[(s, tname)]
        if autogen_context.run_object_filters(
            metadata_table, tname, "table", False, None
        ):
            upgrade_ops.ops.append(
                ops.CreateTableOp.from_table(metadata_table)
            )
            log.info("Detected added table %r", name)
            modify_table_ops = ops.ModifyTableOps(tname, [], schema=s)

            autogen_context.comparators.dispatch(
                "table", qualifier=autogen_context.dialect.name
            )(
                autogen_context,
                modify_table_ops,
                s,
                tname,
                None,
                metadata_table,
            )
            if not modify_table_ops.is_empty():
                upgrade_ops.ops.append(modify_table_ops)

    removal_metadata = sa_schema.MetaData()
    for s, tname in conn_table_names.difference(metadata_table_names):
        name = sa_schema._get_table_key(tname, s)

        # a name might be present already if a previous reflection pulled
        # this table in via foreign key constraint
        exists = name in removal_metadata.tables
        t = sa_schema.Table(tname, removal_metadata, schema=s)

        if not exists:
            event.listen(
                t,
                "column_reflect",
                # fmt: off
                autogen_context.migration_context.impl.
                _compat_autogen_column_reflect
                (inspector),
                # fmt: on
            )
            _InspectorConv(inspector).reflect_table(t)
        if autogen_context.run_object_filters(t, tname, "table", True, None):
            modify_table_ops = ops.ModifyTableOps(tname, [], schema=s)

            autogen_context.comparators.dispatch(
                "table", qualifier=autogen_context.dialect.name
            )(autogen_context, modify_table_ops, s, tname, t, None)
            if not modify_table_ops.is_empty():
                upgrade_ops.ops.append(modify_table_ops)

            upgrade_ops.ops.append(ops.DropTableOp.from_table(t))
            log.info("Detected removed table %r", name)

    existing_tables = conn_table_names.intersection(metadata_table_names)

    existing_metadata = sa_schema.MetaData()
    conn_column_info = {}
    for s, tname in existing_tables:
        name = sa_schema._get_table_key(tname, s)
        exists = name in existing_metadata.tables

        # a name might be present already if a previous reflection pulled
        # this table in via foreign key constraint
        t = sa_schema.Table(tname, existing_metadata, schema=s)
        if not exists:
            event.listen(
                t,
                "column_reflect",
                # fmt: off
                autogen_context.migration_context.impl.
                _compat_autogen_column_reflect(inspector),
                # fmt: on
            )
            _InspectorConv(inspector).reflect_table(t)

        conn_column_info[(s, tname)] = t

    if autogen_context.column_renames:
        _setup_column_renames(
            autogen_context, existing_tables, conn_column_info, tname_to_table
        )

    for s, tname in sorted(existing_tables, key=lambda x: (x[0] or "", x[1])):
        s = s or None
        name = "%s.%s" % (s, tname) if s else tname
        metadata_table = tname_to_table[(s, tname)]
        conn_table = existing_metadata.tables[name]

        if autogen_context.run_object_filters(
            metadata_table, tname, "table", False, conn_table
        ):
            modify_table_ops = ops.ModifyTableOps(tname, [], schema=s)
            with _compare_columns(
                s,
                tname,
                conn_table,
                metadata_table,
                modify_table_ops,
                autogen_context,
                inspector,
            ):
                autogen_context.comparators.dispatch(
                    "table", qualifier=autogen_context.dialect.name
                )(
                    autogen_context,
                    modify_table_ops,
                    s,
                    tname,
                    conn_table,
                    metadata_table,
                )

            if not modify_table_ops.is_empty():
                upgrade_ops.ops.append(modify_table_ops)


def _setup_column_renames(
    autogen_context: AutogenContext,
    existing_tables: set[tuple[str | None, str]],
    conn_column_info: dict[tuple[str | None, str], Table],
    tname_to_table: dict[tuple[str | None, str], Table],
) -> None:
    """Resolve ``column_renames`` hints for all existing tables and
    rename the columns of the reflected tables in place.

    This runs before any per-table comparison so that foreign keys
    that refer to renamed columns in other tables resolve against the
    renamed reflected structure.   The "effective" renames exclude any
    rename where either side is omitted by include_name / include_object
    filters; filtered objects are not pulled back into the comparison
    by a hint.

    """
    for s, tname in existing_tables:
        renames = autogen_context.column_renames.get((s, tname))
        if not renames:
            continue
        conn_table = conn_column_info[(s, tname)]
        metadata_table = tname_to_table[(s, tname)]
        metadata_cols_by_name = {
            c.name: c for c in metadata_table.c if not c.system
        }

        effective_renames: dict[str, str] = {}
        for old_name, new_name in renames.items():
            if old_name not in conn_table.c:
                continue
            if new_name not in metadata_cols_by_name:
                continue
            if not autogen_context.run_name_filters(
                old_name,
                "column",
                {"table_name": tname, "schema_name": s},
            ):
                continue
            conn_col = conn_table.c[old_name]
            metadata_col = metadata_cols_by_name[new_name]
            if not autogen_context.run_object_filters(
                conn_col, old_name, "column", True, metadata_col
            ):
                continue
            if not autogen_context.run_object_filters(
                metadata_col, new_name, "column", False, conn_col
            ):
                continue
            effective_renames[old_name] = new_name

        for old_name, new_name in effective_renames.items():
            col = conn_table.c[old_name]
            conn_table._columns.remove(col)
            col.name = new_name
            col.key = new_name
            conn_table._columns.add(col)
        autogen_context._effective_column_renames[(s, tname)] = (
            effective_renames
        )


@contextlib.contextmanager
def _compare_columns(
    schema: str | None,
    tname: quoted_name | str,
    conn_table: Table,
    metadata_table: Table,
    modify_table_ops: ModifyTableOps,
    autogen_context: AutogenContext,
    inspector: Inspector,
) -> Iterator[None]:
    name = "%s.%s" % (schema, tname) if schema else tname
    metadata_col_names = OrderedSet(
        c.name for c in metadata_table.c if not c.system
    )
    metadata_cols_by_name = {
        c.name: c for c in metadata_table.c if not c.system
    }

    # effective column renames for this table were already resolved
    # and applied to the reflected table by _setup_column_renames();
    # name filters below still see the original reflected column names.
    effective_renames = autogen_context._effective_column_renames.get(
        (schema, tname), {}
    )
    old_names_by_new = {new: old for old, new in effective_renames.items()}

    conn_col_names = {
        c.name: c
        for c in conn_table.c
        if autogen_context.run_name_filters(
            old_names_by_new.get(c.name, c.name),
            "column",
            {"table_name": tname, "schema_name": schema},
        )
    }
    conn_effective_names = set(conn_col_names)

    for cname in metadata_col_names.difference(conn_effective_names):
        if autogen_context.run_object_filters(
            metadata_cols_by_name[cname], cname, "column", False, None
        ):
            modify_table_ops.ops.append(
                ops.AddColumnOp.from_column_and_tablename(
                    schema, tname, metadata_cols_by_name[cname]
                )
            )
            log.info("Detected added column '%s.%s'", name, cname)

    for colname in metadata_col_names.intersection(conn_effective_names):
        metadata_col = metadata_cols_by_name[colname]
        old_name = old_names_by_new.get(colname, colname)
        conn_col = conn_col_names[colname]
        if not autogen_context.run_object_filters(
            metadata_col, colname, "column", False, conn_col
        ):
            continue
        alter_column_op = ops.AlterColumnOp(tname, old_name, schema=schema)
        if old_name != colname:
            alter_column_op.modify_name = colname
            log.info(
                "Detected renamed column '%s.%s' to '%s'",
                name,
                old_name,
                colname,
            )

        autogen_context.comparators.dispatch(
            "column", qualifier=autogen_context.dialect.name
        )(
            autogen_context,
            alter_column_op,
            schema,
            tname,
            colname,
            conn_col,
            metadata_col,
        )

        if alter_column_op.has_changes():
            modify_table_ops.ops.append(alter_column_op)

    yield

    for cname, conn_col in conn_col_names.items():
        if cname in metadata_col_names:
            continue
        if autogen_context.run_object_filters(
            conn_col, cname, "column", True, None
        ):
            modify_table_ops.ops.append(
                ops.DropColumnOp.from_column_and_tablename(
                    schema, tname, conn_col
                )
            )
            log.info("Detected removed column '%s.%s'", name, cname)


def setup(plugin: Plugin) -> None:

    plugin.add_autogenerate_comparator(
        _autogen_for_tables,
        "schema",
        "tables",
    )
