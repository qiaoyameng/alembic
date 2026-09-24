.. change::
    :tags: usecase, operations

    Added the :paramref:`~.Operations.batch_alter_table.column_transfers`
    parameter to :meth:`.Operations.batch_alter_table`, allowing explicit
    SQL expressions to be given for populating columns of the recreated
    table when batch mode uses "move and copy".  The mapping is keyed by
    final column name and is evaluated by the database against the
    existing table as part of the ``INSERT INTO ... SELECT`` copy, so
    values can be transformed (e.g. ``trim``/``nullif``/``cast``) or newly
    added columns can be backfilled from existing row contents without
    reading data into Python.  Dropped columns may be referenced as
    expression sources, explicit expressions for added columns take
    precedence over server defaults, and unknown targets, generated-column
    targets, and cross-table references are rejected before any DDL is
    emitted.  Works both online with reflection and offline with
    ``copy_from``.  See :ref:`batch_column_transfers` for details.
