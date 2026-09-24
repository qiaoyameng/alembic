.. change::
    :tags: feature, operations

    Added new operation group object :class:`.AutocommitOp`, which allows a
    series of ordinary migration operations to be grouped together so that
    they render and execute within a
    :meth:`.MigrationContext.autocommit_block`.  Extensions such as
    ``process_revision_directives`` hooks and :class:`.Rewriter` objects can
    wrap part of the autogenerate operation stream in an
    :class:`.AutocommitOp` so that the generated revision script emits the
    group inside ``with op.get_context().autocommit_block():``, and the group
    may also be invoked directly via the standard :class:`.Operations`
    dispatch.  Groups preserve the order of their contained operations and
    reverse them in reverse order, raising a clear error for non-reversible
    operations.  Nesting groups or combining them with
    ``op.batch_alter_table()`` blocks is rejected consistently at
    construction, render, and execution time.
