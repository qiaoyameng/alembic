.. change::
    :tags: usecase, autogenerate, batch

    Added a new autogenerate option,
    :paramref:`.EnvironmentContext.configure.render_batch_copy_from`, which
    embeds the reflected pre-migration definition of each table that is
    subject to a batch "move and copy" directly into the generated revision
    file, passing it as the
    :paramref:`.Operations.batch_alter_table.copy_from` parameter.  A
    revision generated in this way can render complete SQL in ``--sql``
    (offline) mode for both ``upgrade()`` and ``downgrade()`` without
    connecting to the original database; the downgrade direction embeds the
    post-upgrade structure, which is computed locally from the reflected
    structure and the generated operations.  The option is off by default
    and requires :paramref:`.EnvironmentContext.configure.render_as_batch`;
    table elements that cannot be rendered into a self-contained revision
    file, such as user-defined types not covered by
    :paramref:`.EnvironmentContext.configure.render_item` or
    :paramref:`.EnvironmentContext.configure.user_module_prefix`, cause
    generation to fail explicitly rather than emitting an incomplete
    definition.
