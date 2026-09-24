.. _ops:

===================
Operation Reference
===================

This file provides documentation on Alembic migration directives.

The directives here are used within user-defined migration files,
within the ``upgrade()`` and ``downgrade()`` functions, as well as
any functions further invoked by those.

All directives exist as methods on a class called :class:`.Operations`.
When migration scripts are run, this object is made available
to the script via the ``alembic.op`` datamember, which is
a *proxy* to an actual instance of :class:`.Operations`.
Currently, ``alembic.op`` is a real Python module, populated
with individual proxies for each method on :class:`.Operations`,
so symbols can be imported safely from the ``alembic.op`` namespace.

The :class:`.Operations` system is also fully extensible.  See
:ref:`operation_plugins` for details on this.

A key design philosophy to the :ref:`alembic.operations.toplevel` methods is that
to the greatest degree possible, they internally generate the
appropriate SQLAlchemy metadata, typically involving
:class:`~sqlalchemy.schema.Table` and :class:`~sqlalchemy.schema.Constraint`
objects.  This so that migration instructions can be
given in terms of just the string names and/or flags involved.
The exceptions to this
rule include the :meth:`~.Operations.add_column` and :meth:`~.Operations.create_table`
directives, which require full :class:`~sqlalchemy.schema.Column`
objects, though the table metadata is still generated here.

The functions here all require that a :class:`.MigrationContext` has been
configured within the ``env.py`` script first, which is typically
via :meth:`.EnvironmentContext.configure`.   Under normal
circumstances they are called from an actual migration script, which
itself would be invoked by the :meth:`.EnvironmentContext.run_migrations`
method.

.. _ops_autocommit:

Running Operations Outside of a Transaction
===========================================

Some database DDL directives must be run outside of any transaction block;
PostgreSQL ``ALTER TYPE ... ADD VALUE`` is the most common example.  The
:meth:`.Operations.autocommit_block` directive runs a series of operations
within an "autocommit" block, which is a shorthand for
:meth:`.MigrationContext.autocommit_block`::

    def upgrade():
        with op.autocommit_block():
            op.execute("ALTER TYPE mood ADD VALUE 'soso'")

.. warning::

    Entering an autocommit block **unconditionally commits** the database
    transaction that precedes it.  If any operation inside the block fails,
    statements that were already committed, whether before the block was
    entered or within the block itself, are **not** rolled back.  When
    using autocommit blocks, consider
    :paramref:`.EnvironmentContext.configure.transaction_per_migration`
    so that migrations are expected to commit in shorter units.

Autocommit blocks may not be nested, and may not be used inside of a
:meth:`.Operations.batch_alter_table` block.

For autogenerate and other operation-stream rewriting use cases, the
:class:`.AutocommitOp` object groups a series of
:class:`.MigrateOperation` objects so that they render and execute as an
autocommit block.  This is typically used from a
:paramref:`.EnvironmentContext.configure.process_revision_directives`
callable (see :ref:`customizing_revision`)::

    from alembic.operations import ops

    def process_revision_directives(context, revision, directives):
        for script in directives:
            script.upgrade_ops.ops[:] = [
                ops.AutocommitOp(ops=[op])
                if isinstance(op, ops.ExecuteSQLOp)
                else op
                for op in script.upgrade_ops.ops
            ]

The above group renders into a migration script as a
``with op.autocommit_block():`` block containing the grouped operations,
and executes its sub-operations in order through the standard
:class:`.Operations` dispatch.  When the group is reversed, its
sub-operations are reversed in reverse order; a group that contains an
operation with no reverse, such as :class:`.ExecuteSQLOp`, raises an
error rather than producing an empty, seemingly-downgradeable block.

.. module:: alembic.operations

.. class:: AbstractOperations

    Base class for :class:`.Operations` and :class:`.BatchOperations`.

    See :class:`.Operations` for full list of members

.. autoclass:: Operations
    :members:
    :inherited-members:

.. autoclass:: BatchOperations
    :members:
