# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics

from __future__ import annotations

import re
from typing import Any
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import exc as sa_exc
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import Index
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import MetaData
from sqlalchemy import PrimaryKeyConstraint
from sqlalchemy import schema as sql_schema
from sqlalchemy import select
from sqlalchemy import Table
from sqlalchemy import types as sqltypes
from sqlalchemy.sql.elements import conv
from sqlalchemy.sql.schema import SchemaEventTarget
from sqlalchemy.util import OrderedDict
from sqlalchemy.util import topological

from .. import util
from ..util import exc
from ..util.sqla_compat import _constraint_name_for_table
from ..util.sqla_compat import _columns_for_constraint
from ..util.sqla_compat import _conv_constraint_names
from ..util.sqla_compat import _copy
from ..util.sqla_compat import _copy_expression
from ..util.sqla_compat import _ensure_scope_for_ddl
from ..util.sqla_compat import _fk_is_self_referential
from ..util.sqla_compat import _fk_target_table_key
from ..util.sqla_compat import _fk_target_tokens
from ..util.sqla_compat import _get_table_key
from ..util.sqla_compat import _idx_table_bound_expressions
from ..util.sqla_compat import _is_type_bound
from ..util.sqla_compat import _name_type_bound_constraints
from ..util.sqla_compat import _remove_column_from_collection
from ..util.sqla_compat import _resolve_for_variant
from ..util.sqla_compat import constraint_name_defined
from ..util.sqla_compat import constraint_name_string

if TYPE_CHECKING:
    from typing import Literal

    from sqlalchemy.engine import Dialect
    from sqlalchemy.sql.elements import ColumnClause
    from sqlalchemy.sql.elements import quoted_name
    from sqlalchemy.sql.schema import Constraint
    from sqlalchemy.sql.type_api import TypeEngine

    from ..ddl.base import _ServerDefaultType
    from ..ddl.impl import DefaultImpl


class BatchOperationsImpl:
    def __init__(
        self,
        operations,
        table_name,
        schema,
        recreate,
        copy_from,
        table_args,
        table_kwargs,
        reflect_args,
        reflect_kwargs,
        naming_convention,
        partial_reordering,
    ):
        self.operations = operations
        self.table_name = table_name
        self.schema = schema
        if recreate not in ("auto", "always", "never"):
            raise ValueError(
                "recreate may be one of 'auto', 'always', or 'never'."
            )
        self.recreate = recreate
        self.copy_from = copy_from
        self.table_args = table_args
        self.table_kwargs = dict(table_kwargs)
        self.reflect_args = reflect_args
        self.reflect_kwargs = dict(reflect_kwargs)
        self.reflect_kwargs.setdefault(
            "listeners", list(self.reflect_kwargs.get("listeners", ()))
        )
        self.reflect_kwargs["listeners"].append(
            ("column_reflect", operations.impl.autogen_column_reflect)
        )
        self.naming_convention = naming_convention
        self.partial_reordering = partial_reordering
        self.batch = []
        self._renamed_to: str | None = None

    @property
    def dialect(self) -> Dialect:
        return self.operations.impl.dialect

    @property
    def impl(self) -> DefaultImpl:
        return self.operations.impl

    def _should_recreate(self) -> bool:
        if self.recreate == "auto":
            return self.operations.impl.requires_recreate_in_batch(self)
        elif self.recreate == "always":
            return True
        else:
            return False

    def _check_rename_table(self, should_recreate: bool) -> None:
        """Validate a pending rename against the live database before any
        table-altering DDL has been emitted.

        Offline (``--sql``) mode can't inspect a database, so these checks
        are skipped there; the emitted SQL is validated by the database
        when it runs.

        """
        assert self._renamed_to is not None
        if self.operations.migration_context.as_sql:
            return

        bind = self.operations.get_bind()
        inspector = sa_inspect(bind)
        if self._renamed_to in inspector.get_table_names(schema=self.schema):
            raise exc.CommandError(
                f"Can't rename table '{self.table_name}' to "
                f"'{self._renamed_to}'; the target table already exists. "
                "Drop or rename the existing table first, or choose a "
                "different target name."
            )

        if should_recreate and self.impl.dialect.name == "sqlite":
            fk_enabled = bind.exec_driver_sql("PRAGMA foreign_keys").scalar()
            if fk_enabled:
                raise exc.CommandError(
                    f"Renaming table '{self.table_name}' within a batch "
                    "table recreate requires SQLite's foreign key "
                    "enforcement to be disabled, as the original table "
                    "must be dropped while other tables may still refer "
                    "to it.  Disable enforcement before running the "
                    "migration, e.g. issue 'PRAGMA foreign_keys=OFF' on "
                    "the connection (outside of a transaction), or run "
                    "the migration with foreign key enforcement off."
                )

    def flush(self) -> None:
        should_recreate = self._should_recreate()

        with _ensure_scope_for_ddl(self.impl.connection):
            if not should_recreate:
                if self._renamed_to is not None:
                    self._check_rename_table(should_recreate)
                for opname, arg, kw in self.batch:
                    if opname == "rename_table":
                        # the rename is applied after all other operations
                        # so that those operations may continue to refer
                        # to the table by its original name regardless of
                        # the order in which they were invoked.
                        continue
                    fn = getattr(self.operations.impl, opname)
                    fn(*arg, **kw)
                for opname, arg, kw in self.batch:
                    if opname != "rename_table":
                        continue
                    kw = dict(kw)
                    if kw.pop("reapply_naming_convention", False):
                        util.warn(
                            "The reapply_naming_convention parameter of "
                            "batch rename_table() has no effect when the "
                            "table is not recreated; constraint and index "
                            "names are left as-is by ALTER TABLE RENAME."
                        )
                    self.operations.impl.rename_table(*arg, **kw)
            else:
                if self._renamed_to is not None:
                    self._check_rename_table(should_recreate)
                m1 = MetaData()

                if self.copy_from is not None:
                    existing_table = self.copy_from
                    reflected = False
                else:
                    if self.operations.migration_context.as_sql:
                        raise exc.CommandError(
                            f"This operation cannot proceed in --sql mode; "
                            f"batch mode with dialect "
                            f"{self.operations.migration_context.dialect.name} "  # noqa: E501
                            f"requires a live database connection with which "
                            f'to reflect the table "{self.table_name}". '
                            f"To generate a batch SQL migration script using "
                            "table "
                            '"move and copy", a complete Table object '
                            f'should be passed to the "copy_from" argument '
                            "of the batch_alter_table() method so that table "
                            "reflection can be skipped."
                        )

                    existing_table = Table(
                        self.table_name,
                        m1,
                        schema=self.schema,
                        autoload_with=self.operations.get_bind(),
                        *self.reflect_args,
                        **self.reflect_kwargs,
                    )
                    reflected = True

                    if self.naming_convention:
                        # names that came back from reflection are already
                        # the names that are in the database, so mark them
                        # as final; the naming convention is then applied
                        # only to those constraints that were reflected
                        # without a name, which is what this parameter is
                        # for.  see the docstring for
                        # sqlalchemy.schema.conv.
                        _conv_constraint_names(existing_table)
                        existing_table = existing_table.to_metadata(
                            MetaData(naming_convention=self.naming_convention)
                        )

                batch_impl = ApplyBatchImpl(
                    self.impl,
                    existing_table,
                    self.table_args,
                    self.table_kwargs,
                    reflected,
                    partial_reordering=self.partial_reordering,
                )
                for opname, arg, kw in self.batch:
                    fn = getattr(batch_impl, opname)
                    fn(*arg, **kw)

                batch_impl._create(self.impl)

    def alter_column(self, *arg, **kw) -> None:
        self.batch.append(("alter_column", arg, kw))

    def add_column(self, *arg, **kw) -> None:
        if (
            "insert_before" in kw or "insert_after" in kw
        ) and not self._should_recreate():
            raise exc.CommandError(
                "Can't specify insert_before or insert_after when using "
                "ALTER; please specify recreate='always'"
            )
        self.batch.append(("add_column", arg, kw))

    def drop_column(self, *arg, **kw) -> None:
        self.batch.append(("drop_column", arg, kw))

    def add_constraint(self, const: Constraint) -> None:
        self.batch.append(("add_constraint", (const,), {}))

    def drop_constraint(self, const: Constraint) -> None:
        self.batch.append(("drop_constraint", (const,), {}))

    def rename_table(
        self,
        table_name: str,
        new_table_name: str,
        schema: str | None = None,
        reapply_naming_convention: bool = False,
    ) -> None:
        if self._renamed_to is not None:
            raise exc.CommandError(
                f"Table '{self.table_name}' is already being renamed to "
                f"'{self._renamed_to}' in this batch; only one "
                "rename_table() operation may be issued per "
                "batch_alter_table() block."
            )
        if new_table_name == self.table_name:
            raise exc.CommandError(
                f"Can't rename table '{self.table_name}' to itself; "
                "the new table name must be different from the current "
                "name."
            )
        self._renamed_to = new_table_name
        self.batch.append(
            (
                "rename_table",
                (table_name, new_table_name),
                {
                    "schema": schema,
                    "reapply_naming_convention": reapply_naming_convention,
                },
            )
        )

    def create_index(self, idx: Index, **kw: Any) -> None:
        self.batch.append(("create_index", (idx,), kw))

    def drop_index(self, idx: Index, **kw: Any) -> None:
        self.batch.append(("drop_index", (idx,), kw))

    def create_table_comment(self, table):
        self.batch.append(("create_table_comment", (table,), {}))

    def drop_table_comment(self, table):
        self.batch.append(("drop_table_comment", (table,), {}))

    def create_table(self, table):
        raise NotImplementedError("Can't create table in batch mode")

    def drop_table(self, table):
        raise NotImplementedError("Can't drop table in batch mode")

    def create_column_comment(self, column):
        self.batch.append(("create_column_comment", (column,), {}))


class ApplyBatchImpl:
    def __init__(
        self,
        impl: DefaultImpl,
        table: Table,
        table_args: tuple,
        table_kwargs: dict[str, Any],
        reflected: bool,
        partial_reordering: tuple = (),
    ) -> None:
        self.impl = impl
        self.table = table  # this is a Table object
        self.table_args = table_args
        self.table_kwargs = table_kwargs
        self.temp_table_name = self._calc_temp_name(table.name)
        self.new_table: Table | None = None

        self.partial_reordering = partial_reordering  # tuple of tuples
        self.add_col_ordering: tuple[
            tuple[str, str], ...
        ] = ()  # tuple of tuples

        self.renamed_to: str | None = None
        self.reapply_naming_convention: bool = False

        self.column_transfers = OrderedDict(
            (c.name, {"expr": c}) for c in self.table.c
        )
        self.existing_ordering = list(self.column_transfers)

        self.reflected = reflected
        self._grab_table_elements()

    @classmethod
    def _calc_temp_name(cls, tablename: quoted_name | str) -> str:
        return ("_alembic_tmp_%s" % tablename)[0:50]

    def _grab_table_elements(self) -> None:
        schema = self.table.schema
        self.columns: dict[str, Column[Any]] = OrderedDict()
        for c in self.table.c:
            c_copy = _copy(c, schema=schema)
            c_copy.unique = c_copy.index = False
            # ensure that the type object was copied,
            # as we may need to modify it in-place
            if isinstance(c.type, SchemaEventTarget):
                assert c_copy.type is not c.type
            self.columns[c.name] = c_copy
        self.named_constraints: dict[str, Constraint] = {}
        self.unnamed_constraints = []
        self.col_named_constraints = {}
        self.indexes: dict[str, Index] = {}
        self.new_indexes: dict[str, Index] = {}

        # names of constraints / indexes that are known to have been
        # generated by a naming convention, as indicated by the
        # :class:`sqlalchemy.schema.conv` marker that SQLAlchemy applies
        # when it renders the name.  Names that come back from reflection
        # have lost this information, as they are all marked with ``conv``
        # to indicate they are final; those names are therefore not
        # recorded here and are never treated as convention-generated.
        self.convention_generated_constraints: set[str] = set()
        self.convention_generated_indexes: set[str] = set()

        # a reflected CHECK constraint that has no name can't be linked
        # to the column or datatype it applies to, so it is omitted from
        # the recreate; the documented remedy is to restate it using
        # batch_alter_table.table_args.  Individual constraints can't be
        # matched up to those that were passed, as the reflected text is
        # normalized by the database, so the presence of any CheckConstraint
        # in table_args is taken to mean the case has been dealt with.
        check_constraints_in_table_args = any(
            isinstance(arg, CheckConstraint) for arg in self.table_args
        )

        for const in self.table.constraints:
            if _is_type_bound(const):
                continue
            elif (
                self.reflected
                and isinstance(const, CheckConstraint)
                and not const.name
            ):
                # TODO: we are skipping unnamed reflected CheckConstraint
                # because
                # we have no way to determine _is_type_bound() for these.
                # warn, as the constraint is otherwise dropped from the
                # table without any indication that this has occurred.
                if not check_constraints_in_table_args:
                    util.warn(
                        f"Unnamed CHECK constraint on reflected table "
                        f"'{self.table.name}' is being omitted from the "
                        f"table recreate; pass the constraint explicitly "
                        f"using batch_alter_table.table_args.  Naming CHECK "
                        f"constraints avoids this going forward."
                    )
            elif constraint_name_string(const.name):
                self.named_constraints[const.name] = const
                if not self.reflected and isinstance(const.name, conv):
                    self.convention_generated_constraints.add(const.name)
            else:
                self.unnamed_constraints.append(const)

        if not self.reflected:
            for col in self.table.c:
                for const in col.constraints:
                    if const.name:
                        self.col_named_constraints[const.name] = (col, const)

        for idx in self.table.indexes:
            self.indexes[idx.name] = idx  # type: ignore[index]
            if not self.reflected and isinstance(idx.name, conv):
                self.convention_generated_indexes.add(idx.name)

        for k in self.table.kwargs:
            self.table_kwargs.setdefault(k, self.table.kwargs[k])

    def _adjust_self_columns_for_partial_reordering(self) -> None:
        pairs = set()

        col_by_idx = list(self.columns)

        if self.partial_reordering:
            for tuple_ in self.partial_reordering:
                for index, elem in enumerate(tuple_):
                    if index > 0:
                        pairs.add((tuple_[index - 1], elem))
        else:
            for index, elem in enumerate(self.existing_ordering):
                if index > 0:
                    pairs.add((col_by_idx[index - 1], elem))

        pairs.update(self.add_col_ordering)

        # this can happen if some columns were dropped and not removed
        # from existing_ordering.  this should be prevented already, but
        # conservatively making sure this didn't happen
        pairs_list = [p for p in pairs if p[0] != p[1]]

        sorted_ = list(
            topological.sort(pairs_list, col_by_idx, deterministic_order=True)
        )
        self.columns = OrderedDict((k, self.columns[k]) for k in sorted_)
        self.column_transfers = OrderedDict(
            (k, self.column_transfers[k]) for k in sorted_
        )

    def _transfer_elements_to_new_table(self) -> None:
        assert self.new_table is None, "Can only create new table once"

        m = MetaData()
        schema = self.table.schema
        naming_context = self._final_naming_context()

        if self.partial_reordering or self.add_col_ordering:
            self._adjust_self_columns_for_partial_reordering()

        self.new_table = new_table = Table(
            self.temp_table_name,
            m,
            *(list(self.columns.values()) + list(self.table_args)),
            schema=schema,
            **self.table_kwargs,
        )

        # CHECK constraints regenerated on the new table by a SchemaType
        # such as Boolean or Enum are named using the naming convention of
        # the table being replaced; the new table is under a temporary name
        # at this point so it can't supply the convention itself.
        # when the table is being renamed and convention-generated names
        # are to be reapplied, the convention is instead resolved against
        # the final table name.
        _name_type_bound_constraints(
            new_table,
            naming_context if naming_context is not None else self.table,
        )

        for const in (
            list(self.named_constraints.values()) + self.unnamed_constraints
        ):
            const_columns = {c.key for c in _columns_for_constraint(const)}

            if not const_columns.issubset(self.column_transfers):
                continue

            const_copy: Constraint
            if isinstance(const, ForeignKeyConstraint):
                if _fk_is_self_referential(const):
                    if self.renamed_to is not None:
                        # self-referential constraint on a table that's
                        # being renamed: refer to the *final* table name,
                        # which is the name the new table will have once
                        # the recreate is complete.
                        const_copy = _copy(
                            const,
                            schema=schema,
                            target_table=self._self_ref_referent(m),
                        )
                    else:
                        # for self-referential constraint, refer to the
                        # *original* table name, and not _alembic_batch_temp.
                        # This is consistent with how we're handling
                        # FK constraints from other tables; we assume SQLite
                        # no foreign keys just keeps the names unchanged, so
                        # when we rename back, they match again.
                        const_copy = _copy(
                            const, schema=schema, target_table=self.table
                        )
                else:
                    # "target_table" for ForeignKeyConstraint.copy() is
                    # only used if the FK is detected as being
                    # self-referential, which we are handling above.
                    const_copy = _copy(const, schema=schema)
            else:
                const_copy = _copy(
                    const, schema=schema, target_table=new_table
                )
            if isinstance(const, ForeignKeyConstraint):
                self._setup_referent(m, const)
            if (
                naming_context is not None
                and const_copy.name in self.convention_generated_constraints
            ):
                new_const_name = self._reapply_convention_to_name(
                    const_copy, naming_context
                )
                if new_const_name is not None:
                    const_copy.name = conv(new_const_name)
            new_table.append_constraint(const_copy)

    def _self_ref_referent(self, metadata: MetaData) -> Table:
        """Return a stand-in table within ``metadata`` that carries the
        final table name, to be used as the referent of self-referential
        foreign keys when the table is being renamed.

        """
        assert self.renamed_to is not None
        key = _get_table_key(self.renamed_to, self.table.schema)
        try:
            return metadata.tables[key]
        except KeyError:
            return Table(self.renamed_to, metadata, schema=self.table.schema)

    def _gather_indexes_from_both_tables(self) -> list[Index]:
        assert self.new_table is not None
        idx: list[Index] = []
        naming_context = self._final_naming_context()

        for idx_existing in self.indexes.values():
            # this is a lift-and-move from Table.to_metadata

            if idx_existing._column_flag:
                continue

            idx_name = idx_existing.name
            if (
                naming_context is not None
                and idx_name in self.convention_generated_indexes
            ):
                new_idx_name = self._reapply_convention_to_name(
                    idx_existing, naming_context
                )
                if new_idx_name is not None:
                    idx_name = conv(new_idx_name)

            idx_copy = Index(
                idx_name,
                unique=idx_existing.unique,
                *[
                    _copy_expression(expr, self.new_table)
                    for expr in _idx_table_bound_expressions(idx_existing)
                ],
                _table=self.new_table,
                **idx_existing.kwargs,
            )
            idx.append(idx_copy)

        for index in self.new_indexes.values():
            idx.append(
                Index(
                    index.name,
                    unique=index.unique,
                    *[self.new_table.c[col] for col in index.columns.keys()],
                    **index.kwargs,
                )
            )
        return idx

    def _setup_referent(
        self, metadata: MetaData, constraint: ForeignKeyConstraint
    ) -> None:
        referent_schema, tname, _ = _fk_target_tokens(constraint.elements[0])

        if (
            self.renamed_to is not None
            and _fk_target_table_key(constraint.elements[0]) == self.table.key
        ):
            # a self-referential foreign key on a table that's being
            # renamed refers to the final table name
            tname = self.renamed_to

        if tname != self.temp_table_name:
            key = _get_table_key(tname, referent_schema)
            colnames = [
                name
                for name in (
                    _fk_target_tokens(elem).column_name
                    for elem in constraint.elements
                )
                if name is not None
            ]

            if key in metadata.tables:
                t = metadata.tables[key]
                for name in colnames:
                    if name not in t.c:
                        t.append_column(Column(name, sqltypes.NULLTYPE))
            else:
                Table(
                    tname,
                    metadata,
                    *[Column(name, sqltypes.NULLTYPE) for name in colnames],
                    schema=referent_schema,
                )

    def _create(self, op_impl: DefaultImpl) -> None:
        self._transfer_elements_to_new_table()

        op_impl.prep_table_for_batch(self, self.table)
        assert self.new_table is not None
        op_impl.create_table(self.new_table)

        final_table_name = self._final_table_name

        try:
            op_impl._exec(
                self.new_table.insert()
                .inline()
                .from_select(
                    list(
                        k
                        for k, transfer in self.column_transfers.items()
                        if "expr" in transfer
                    ),
                    select(
                        *[
                            transfer["expr"]
                            for transfer in self.column_transfers.values()
                            if "expr" in transfer
                        ]
                    ),
                )
            )
            if self.renamed_to is not None:
                # rename the original table to its final name before
                # dropping it.  On backends such as SQLite that rewrite
                # referring foreign keys in other tables across a rename,
                # this updates those references to the final table name;
                # the reference is then satisfied again once the new
                # table itself is renamed below.
                op_impl.rename_table(
                    self.table.name,
                    self.renamed_to,
                    schema=self.table.schema,
                )
                original_name = self.table.name
                self.table.name = self.renamed_to
                try:
                    op_impl.drop_table(self.table)
                finally:
                    self.table.name = original_name
            else:
                op_impl.drop_table(self.table)
        except:
            op_impl.drop_table(self.new_table)
            raise
        else:
            op_impl.rename_table(
                self.temp_table_name,
                final_table_name,
                schema=self.table.schema,
            )
            self.new_table.name = final_table_name
            try:
                for idx in self._gather_indexes_from_both_tables():
                    op_impl.create_index(idx)
            finally:
                self.new_table.name = self.temp_table_name

    def alter_column(
        self,
        table_name: str,
        column_name: str,
        nullable: bool | None = None,
        server_default: _ServerDefaultType | None | Literal[False] = False,
        name: str | None = None,
        type_: TypeEngine | None = None,
        autoincrement: bool | Literal["auto"] | None = None,
        comment: str | Literal[False] = False,
        **kw,
    ) -> None:
        existing = self.columns[column_name]
        existing_transfer: dict[str, Any] = self.column_transfers[column_name]
        if name is not None and name != column_name:
            # note that we don't change '.key' - we keep referring
            # to the renamed column by its old key in _create().  neat!
            existing.name = name
            existing_transfer["name"] = name

            existing_type = kw.get("existing_type", None)
            if existing_type:
                resolved_existing_type = _resolve_for_variant(
                    kw["existing_type"], self.impl.dialect
                )

                # pop named constraints for Boolean/Enum for rename
                if (
                    isinstance(resolved_existing_type, SchemaEventTarget)
                    and resolved_existing_type.name  # type: ignore[attr-defined]  # noqa E501
                ):
                    self.named_constraints.pop(
                        resolved_existing_type.name,  # type: ignore[attr-defined]  # noqa E501
                        None,
                    )

        if type_ is not None:
            type_ = sqltypes.to_instance(type_)
            # old type is being discarded so turn off eventing
            # rules. Alternatively we can
            # erase the events set up by this type, but this is simpler.
            # we also ignore the drop_constraint that will come here from
            # Operations.implementation_for(alter_column)

            if isinstance(existing.type, SchemaEventTarget):
                existing.type._create_events = (  # type: ignore[attr-defined]
                    existing.type.create_constraint  # type: ignore[attr-defined] # noqa
                ) = False

            self.impl.cast_for_batch_migrate(
                existing, existing_transfer, type_
            )

            existing.type = type_

            # we *dont* however set events for the new type, because
            # alter_column is invoked from
            # Operations.implementation_for(alter_column) which already
            # will emit an add_constraint()

        if nullable is not None:
            existing.nullable = nullable
        if server_default is not False:
            if server_default is None:
                existing.server_default = None
            else:
                sql_schema.DefaultClause(
                    server_default  # type: ignore[arg-type]
                )._set_parent(existing)
        if autoincrement is not None:
            existing.autoincrement = bool(autoincrement)

        if comment is not False:
            existing.comment = comment

    def _setup_dependencies_for_add_column(
        self,
        colname: str,
        insert_before: str | None,
        insert_after: str | None,
    ) -> None:
        index_cols = self.existing_ordering
        col_indexes = {name: i for i, name in enumerate(index_cols)}

        if not self.partial_reordering:
            if insert_after:
                if not insert_before:
                    if insert_after in col_indexes:
                        # insert after an existing column
                        idx = col_indexes[insert_after] + 1
                        if idx < len(index_cols):
                            insert_before = index_cols[idx]
                    else:
                        # insert after a column that is also new
                        insert_before = dict(self.add_col_ordering)[
                            insert_after
                        ]
            if insert_before:
                if not insert_after:
                    if insert_before in col_indexes:
                        # insert before an existing column
                        idx = col_indexes[insert_before] - 1
                        if idx >= 0:
                            insert_after = index_cols[idx]
                    else:
                        # insert before a column that is also new
                        insert_after = {
                            b: a for a, b in self.add_col_ordering
                        }[insert_before]

        if insert_before:
            self.add_col_ordering += ((colname, insert_before),)
        if insert_after:
            self.add_col_ordering += ((insert_after, colname),)

        if (
            not self.partial_reordering
            and not insert_before
            and not insert_after
            and col_indexes
        ):
            self.add_col_ordering += ((index_cols[-1], colname),)

    def add_column(
        self,
        table_name: str,
        column: Column[Any],
        insert_before: str | None = None,
        insert_after: str | None = None,
        **kw,
    ) -> None:
        self._setup_dependencies_for_add_column(
            column.name, insert_before, insert_after
        )
        # we copy the column because operations.add_column()
        # gives us a Column that is part of a Table already.
        col_copy = _copy(column, schema=self.table.schema)

        # turn off eventing rules for a SchemaType such as Boolean / Enum,
        # so that the type doesn't emit its own CHECK constraint onto the
        # new table.  Operations.implementation_for(add_column) already
        # emits an add_constraint() for this constraint, which is where
        # the naming convention has been applied.
        if isinstance(col_copy.type, SchemaEventTarget):
            col_copy.type._create_events = (  # type: ignore[attr-defined]
                col_copy.type.create_constraint  # type: ignore[attr-defined]
            ) = False

        self.columns[column.name] = col_copy
        self.column_transfers[column.name] = {}

    def drop_column(
        self,
        table_name: str,
        column: ColumnClause[Any] | Column[Any],
        **kw,
    ) -> None:
        if column.name in self.table.primary_key.columns:
            _remove_column_from_collection(
                self.table.primary_key.columns, column
            )
        del self.columns[column.name]
        del self.column_transfers[column.name]
        self.existing_ordering.remove(column.name)

        # pop named constraints for Boolean/Enum for rename
        if (
            "existing_type" in kw
            and isinstance(kw["existing_type"], SchemaEventTarget)
            and kw["existing_type"].name  # type: ignore[attr-defined]
        ):
            self.named_constraints.pop(
                kw["existing_type"].name, None  # type: ignore[attr-defined]
            )

    def create_column_comment(self, column):
        """the batch table creation function will issue create_column_comment
        on the real "impl" as part of the create table process.

        That is, the Column object will have the comment on it already,
        so when it is received by add_column() it will be a normal part of
        the CREATE TABLE and doesn't need an extra step here.

        """

    def create_table_comment(self, table):
        """the batch table creation function will issue create_table_comment
        on the real "impl" as part of the create table process.

        """

    def drop_table_comment(self, table):
        """the batch table creation function will issue drop_table_comment
        on the real "impl" as part of the create table process.

        """

    def add_constraint(self, const: Constraint) -> None:
        if not constraint_name_defined(const.name):
            raise ValueError("Constraint must have a name")
        if isinstance(const, sql_schema.PrimaryKeyConstraint):
            if self.table.primary_key in self.unnamed_constraints:
                self.unnamed_constraints.remove(self.table.primary_key)

        if constraint_name_string(const.name):
            self.named_constraints[const.name] = const
        else:
            self.unnamed_constraints.append(const)

    def drop_constraint(self, const: Constraint) -> None:
        if not const.name:
            raise ValueError("Constraint must have a name")
        try:
            if const.name in self.col_named_constraints:
                col, const = self.col_named_constraints.pop(const.name)

                for col_const in list(self.columns[col.name].constraints):
                    if col_const.name == const.name:
                        self.columns[col.name].constraints.remove(col_const)
            elif constraint_name_string(const.name):
                const = self.named_constraints.pop(const.name)
            elif const in self.unnamed_constraints:
                self.unnamed_constraints.remove(const)

        except KeyError:
            if _is_type_bound(const):
                # type-bound constraints are only included in the new
                # table via their type object in any case, so ignore the
                # drop_constraint() that comes here via the
                # Operations.implementation_for(alter_column)
                return
            raise ValueError("No such constraint: '%s'" % const.name)
        else:
            if isinstance(const, PrimaryKeyConstraint):
                for col in const.columns:
                    self.columns[col.name].primary_key = False

    def create_index(self, idx: Index) -> None:
        self.new_indexes[idx.name] = idx  # type: ignore[index]

    def drop_index(self, idx: Index) -> None:
        try:
            del self.indexes[idx.name]  # type: ignore[arg-type]
        except KeyError:
            raise ValueError("No such index: '%s'" % idx.name)

    def rename_table(
        self,
        table_name: str,
        new_table_name: str,
        schema: str | None = None,
        reapply_naming_convention: bool = False,
    ) -> None:
        if self.renamed_to is not None:
            raise exc.CommandError(
                f"Table '{self.table.name}' is already being renamed to "
                f"'{self.renamed_to}' in this batch; only one "
                "rename_table() operation may be issued per "
                "batch_alter_table() block."
            )
        self.renamed_to = new_table_name
        self.reapply_naming_convention = reapply_naming_convention

        if reapply_naming_convention and self.reflected:
            reflected_names = sorted(
                list(self.named_constraints) + list(self.indexes)
            )
            if reflected_names:
                util.warn(
                    f"Cannot determine which constraint/index names on "
                    f"reflected table '{self.table.name}' were generated "
                    "by a naming convention, as reflection does not "
                    "provide this information; existing names "
                    f"({', '.join(reflected_names)}) are preserved "
                    "as-is.  To have convention-generated names "
                    "regenerated against the new table name, pass a "
                    "Table object to batch_alter_table()'s copy_from "
                    "parameter that is defined against a MetaData that "
                    "includes the naming_convention, so that generated "
                    "names can be identified."
                )

    @property
    def _final_table_name(self) -> str:
        return (
            self.renamed_to if self.renamed_to is not None else self.table.name
        )

    def _final_naming_context(self) -> Table | None:
        """Return a throwaway :class:`.Table` carrying the final table
        name and the source table's naming convention, against which
        convention-generated constraint and index names may be
        re-rendered.  Returns ``None`` if no rename with
        ``reapply_naming_convention`` is pending or no naming convention
        is in use.

        """
        if self.renamed_to is None or not self.reapply_naming_convention:
            return None
        naming_convention = self.table.metadata.naming_convention
        if not naming_convention:
            return None
        return Table(
            self.renamed_to,
            MetaData(naming_convention=naming_convention),
            schema=self.table.schema,
        )

    def _reapply_convention_to_name(
        self, const: Constraint | Index, naming_context: Table
    ) -> str | None:
        """Re-render the naming-convention-generated name of ``const``
        against ``naming_context``.

        Returns the re-rendered name, or ``None`` if the convention
        can't be resolved, in which case the existing name should be
        preserved.  ``const`` itself is not modified.

        """
        existing_name = const.name
        try:
            const.name = None
            try:
                new_name = _constraint_name_for_table(const, naming_context)
            except sa_exc.InvalidRequestError:
                new_name = self._reapply_convention_w_constraint_name(
                    const, naming_context, existing_name
                )
        finally:
            const.name = existing_name

        if not new_name:
            util.warn(
                f"Can't regenerate the naming-convention-generated name "
                f"'{existing_name}' against the new table name "
                f"'{self.renamed_to}'; the existing name is preserved."
            )
            return None
        return str(new_name)

    #: placeholder substituted for the ``%(constraint_name)s`` token when
    #: recovering the pre-convention name of a constraint
    _CONSTRAINT_NAME_SENTINEL = "\x00alembic_constraint_name\x00"

    def _reapply_convention_w_constraint_name(
        self,
        const: Constraint | Index,
        naming_context: Table,
        existing_name: str,
    ) -> str | None:
        """Re-render a convention-generated name where the convention
        includes the ``%(constraint_name)s`` token.

        The token refers to the name the constraint had before the
        convention was applied, which is no longer present on the
        constraint.  It's recovered by rendering the convention against
        the original table with a sentinel in place of the token and
        matching the existing generated name against the result; all
        other tokens render identically for the original and the new
        table name.

        """
        const.name = self._CONSTRAINT_NAME_SENTINEL
        old_render = _constraint_name_for_table(const, self.table)
        if not old_render:
            return None
        pattern = re.escape(str(old_render)).replace(
            re.escape(self._CONSTRAINT_NAME_SENTINEL), "(.+)"
        )
        match = re.fullmatch(pattern, str(existing_name))
        if match is None:
            # the existing name was not produced by this convention
            # against the original table name, e.g. the table was
            # renamed previously; don't guess at a new name.
            return None
        const.name = match.group(1)
        new_name = _constraint_name_for_table(const, naming_context)
        return str(new_name) if new_name else None
